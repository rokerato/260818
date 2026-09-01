"""Naver Place (Naver Map) visitor-review collector.

Why this design
---------------
Naver Map does not serve review data in the HTML you get from ``map.naver.com``.
That host returns a JavaScript application shell; the place detail view is an
embedded document served from ``pcmap.place.naver.com/<category>/<id>/review/visitor``
(``m.place.naver.com`` for mobile). That document ships the *first* batch of
reviews inside an inlined Apollo cache, and every subsequent batch is fetched
client-side by POSTing to ``pcmap-api.place.naver.com/graphql``. The list is a
"load more" (``더보기``) pager, roughly ten reviews per press, not a numbered
pager and not a true infinite scroll.

Two collection strategies follow from that, and this collector implements the
first with the second as a safety net:

``graphql`` (primary)
    Drive the real review page in a browser and **intercept the GraphQL
    responses the page issues for itself**. We never have to hand-write the
    query document, know the persisted-query hash, or reproduce Naver's
    signed headers -- the page does that, and we keep the JSON it gets back.
    That makes the collector resilient to Naver rotating its query text, and it
    yields the authentic upstream payload for the raw archive.

``dom`` (fallback)
    If interception yields nothing (markup change, blocked XHR, a page that
    server-renders everything), fall back to reading the rendered review list.
    Selectors are configurable because Naver rotates its CSS class names; the
    defaults lean on text and structure rather than hashed class names.

Anything the page does not publish is left absent from the payload and becomes
``None`` after mapping. Nothing here invents a value.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Iterator

from .base import BaseCollector, CollectionResult, PlaceTarget, RawReview, utc_now_iso

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://pcmap.place.naver.com"

#: Naver place URLs seen in the wild, all carrying the numeric place id:
#:   https://map.naver.com/p/entry/place/1234567890
#:   https://map.naver.com/v5/entry/place/1234567890?c=...
#:   https://pcmap.place.naver.com/restaurant/1234567890/review/visitor
#:   https://m.place.naver.com/restaurant/1234567890/home
#:   https://naver.me/xxxxxxx  (short link -- must be resolved by the browser)
_PLACE_ID_PATTERNS = (
    re.compile(r"/(?:entry/)?place/(\d{5,})"),
    re.compile(r"/(?:restaurant|cafe|place|accommodation|attraction)/(\d{5,})"),
    re.compile(r"[?&]placeId=(\d{5,})"),
    re.compile(r"[?&]id=(\d{5,})"),
)

#: Keys observed on Naver visitor-review nodes. Used only to *recognise* a
#: review object inside an arbitrary GraphQL response -- not to build one.
_REVIEW_HINT_KEYS = frozenset(
    {
        "body",
        "rating",
        "author",
        "visited",
        "visitCount",
        "media",
        "votedKeywords",
        "created",
        "reactionStat",
        "representativeVisitDateTime",
        "originType",
        "thumbnail",
        "visitedAt",
        "reviewId",
        "userIdno",
        "highlightOffsets",
    }
)

_DEFAULT_LOAD_MORE_SELECTORS = (
    "a:has-text('더보기')",
    "button:has-text('더보기')",
    "[role='button']:has-text('더보기')",
    "a.fvwqf",
)

_DEFAULT_REVIEW_ITEM_SELECTORS = (
    "li[class*='place_apply_pui']",
    "ul#_review_list > li",
    "div.place_section_content ul > li",
)


def parse_place_id(url_or_id: str | None) -> str | None:
    """Pull the numeric Naver place id out of a URL, or pass an id through."""
    if not url_or_id:
        return None
    candidate = str(url_or_id).strip()
    if candidate.isdigit():
        return candidate
    for pattern in _PLACE_ID_PATTERNS:
        match = pattern.search(candidate)
        if match:
            return match.group(1)
    return None


def build_review_url(
    place_id: str, category: str = "restaurant", base_url: str = DEFAULT_BASE_URL
) -> str:
    return f"{base_url.rstrip('/')}/{category}/{place_id}/review/visitor"


def iter_review_nodes(node: Any, _path: str = "$") -> Iterator[tuple[str, dict]]:
    """Walk arbitrary JSON and yield ``(json_path, node)`` for review-shaped dicts.

    Recognition is structural, so it keeps working when Naver renames a wrapper
    field or reshapes the envelope around the review list.
    """
    if isinstance(node, dict):
        typename = str(node.get("__typename", ""))
        hits = _REVIEW_HINT_KEYS & node.keys()
        looks_like_review = (
            ("Review" in typename and "body" in node or "rating" in node)
            or (("id" in node or "reviewId" in node) and len(hits) >= 2)
        )
        if looks_like_review:
            yield _path, node
            # A review never nests another review; stop descending.
            return
        for key, value in node.items():
            yield from iter_review_nodes(value, f"{_path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_review_nodes(value, f"{_path}[{index}]")


def _native_id(payload: dict[str, Any]) -> str | None:
    for key in ("id", "reviewId", "seq"):
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    return None


_DOM_RATING = re.compile(r"별점\s*\n?\s*(\d(?:\.\d)?)\s*\n?\s*점")
_DOM_KOREAN_DATE = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")
_DOM_VISIT_COUNT = re.compile(r"(\d+)\s*번째\s*방문")
_DOM_VERIFIED = re.compile(r"인증\s*수단\s*\n?\s*(\S+)")


def _parse_dom_text(text: str) -> dict[str, Any]:
    """Best-effort structured fields from a rendered review's visible text.

    The rendered item concatenates reviewer name, badges, rating, the review
    body, visit metadata and the verification method into one text column.
    Only unambiguous patterns are lifted out; everything else stays in ``body``.
    """
    parsed: dict[str, Any] = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        # The card leads with the reviewer nickname.
        parsed["authorName"] = lines[0]
    match = _DOM_RATING.search(text)
    if match:
        parsed["rating"] = float(match.group(1))
    match = _DOM_KOREAN_DATE.search(text)
    if match:
        year, month, day = match.groups()
        parsed["visited"] = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    match = _DOM_VISIT_COUNT.search(text)
    if match:
        parsed["visitCount"] = int(match.group(1))
    match = _DOM_VERIFIED.search(text)
    if match:
        parsed["visitConfirmType"] = match.group(1)
    return parsed


def _is_review_photo(src: str) -> bool:
    """Keep genuine review photos; drop lazy-load placeholders and avatars."""
    if not src or src.startswith("data:"):
        return False
    # Naver serves profile avatars through the common thumbnail proxy at tiny
    # fixed sizes (e.g. type=f48_48); review photos come from the review CDN.
    if "type=f48" in src or "profileImage" in src:
        return False
    return True


def content_fingerprint(payload: dict[str, Any]) -> str:
    """Stable hash of a payload, for de-duplicating id-less reviews."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return "sha1:" + hashlib.sha1(blob.encode("utf-8")).hexdigest()


class NaverPlaceCollector(BaseCollector):
    """Collect Naver Place visitor reviews for a single place at a time."""

    platform = "naver"

    def __init__(
        self,
        *,
        max_reviews: int = 200,
        request_delay: float = 1.5,
        page_timeout_ms: int = 30_000,
        max_empty_rounds: int = 3,
        headless: bool = True,
        base_url: str = DEFAULT_BASE_URL,
        browser_executable: str | None = None,
        locale: str = "ko-KR",
        user_agent: str | None = None,
        load_more_selectors: tuple[str, ...] = _DEFAULT_LOAD_MORE_SELECTORS,
        review_item_selectors: tuple[str, ...] = _DEFAULT_REVIEW_ITEM_SELECTORS,
        keep_raw_responses: bool = True,
        debug_dir: str | None = None,
    ) -> None:
        super().__init__(
            max_reviews=max_reviews,
            request_delay=request_delay,
            page_timeout_ms=page_timeout_ms,
            max_empty_rounds=max_empty_rounds,
        )
        self.headless = headless
        self.base_url = base_url
        self.browser_executable = browser_executable
        self.locale = locale
        self.user_agent = user_agent
        self.load_more_selectors = load_more_selectors
        self.review_item_selectors = review_item_selectors
        self.keep_raw_responses = keep_raw_responses
        self.debug_dir = debug_dir

    # ------------------------------------------------------------------
    # target resolution
    # ------------------------------------------------------------------
    def resolve(self, target: PlaceTarget) -> tuple[str | None, str]:
        """Return ``(place_id, review_url)`` for a configured target."""
        place_id = parse_place_id(target.place_id) or parse_place_id(target.url)
        if place_id:
            return place_id, build_review_url(
                place_id, target.category, self.base_url
            )
        # No id in the URL (e.g. a naver.me short link): let the browser follow
        # it and read the id off the landing URL.
        if target.url:
            return None, target.url
        raise ValueError(
            f"target {target.name!r} has neither a usable url nor a place_id"
        )

    # ------------------------------------------------------------------
    # collection
    # ------------------------------------------------------------------
    def collect(self, target: PlaceTarget) -> CollectionResult:
        result = CollectionResult(target=target)
        limit = target.max_reviews or self.max_reviews

        try:
            place_id, review_url = self.resolve(target)
        except ValueError as exc:
            result.record_failure(str(exc))
            result.finished_at = utc_now_iso()
            return result

        result.resolved_place_id = place_id
        result.resolved_place_url = review_url
        result.resolved_place_name = target.name

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment issue
            result.record_failure(
                f"playwright is not installed ({exc}); run "
                "`pip install -r requirements.txt && playwright install chromium`"
            )
            result.finished_at = utc_now_iso()
            return result

        with sync_playwright() as pw:
            launch_kwargs: dict[str, Any] = {"headless": self.headless}
            if self.browser_executable:
                launch_kwargs["executable_path"] = self.browser_executable
            browser = pw.chromium.launch(**launch_kwargs)
            try:
                context = browser.new_context(
                    locale=self.locale,
                    user_agent=self.user_agent,
                    viewport={"width": 1280, "height": 1600},
                )
                page = context.new_page()
                captured: list[dict[str, Any]] = []
                api_trace: list[str] = []
                self._attach_graphql_capture(page, captured, result, api_trace)
                self._drive_page(page, review_url, limit, result, captured)
                # The landing URL is authoritative once redirects have settled.
                if not result.resolved_place_id:
                    result.resolved_place_id = parse_place_id(page.url)
                    result.resolved_place_url = page.url
                # Harvest after pagination so the inlined cache is at its fullest.
                self._harvest_inline_state(page, captured, result)
                dom_items = self._extract_dom_reviews(page, result)
                if not any(c["source"] == "graphql" for c in captured):
                    self._dump_debug(page, result, api_trace)
                context.close()
            finally:
                browser.close()

        self._assemble(result, captured, dom_items, limit)
        result.finished_at = utc_now_iso()
        return result

    # ------------------------------------------------------------------
    def _attach_graphql_capture(
        self,
        page: Any,
        captured: list[dict[str, Any]],
        result: CollectionResult,
        api_trace: list[str],
    ) -> None:
        """Record every GraphQL response body the page fetches for itself."""

        def on_request(request: Any) -> None:
            if "place.naver.com" in request.url and "/graphql" in request.url:
                api_trace.append(f"{request.method} {request.url}")

        def on_response(response: Any) -> None:
            url = response.url
            if "/graphql" not in url:
                return
            # CORS preflights carry no body; capturing them is pure noise.
            if response.request.method == "OPTIONS":
                return
            try:
                text = response.text()
                body = json.loads(text)
            except Exception as exc:  # noqa: BLE001 - non-JSON or drained body
                snippet = ""
                try:
                    snippet = (text or "")[:200]
                except Exception:  # noqa: BLE001
                    pass
                result.record_failure(
                    f"unreadable graphql response {url} "
                    f"(method={response.request.method} status={response.status} "
                    f"type={response.headers.get('content-type')!r}): {exc!r} "
                    f"body[:200]={snippet!r}"
                )
                return
            captured.append(
                {"url": url, "status": response.status, "body": body,
                 "source": "graphql", "captured_at": utc_now_iso()}
            )

        page.on("request", on_request)
        page.on("response", on_response)

    def _harvest_inline_state(
        self, page: Any, captured: list[dict[str, Any]], result: CollectionResult
    ) -> None:
        """Pull the server-rendered Apollo cache out of the page.

        pcmap pages ship the first review batch inlined in the HTML; this gets
        the fully structured records even when no GraphQL fetch is observed.
        """
        try:
            raw = page.evaluate(
                "() => { try { return JSON.stringify(window.__APOLLO_STATE__"
                " || null); } catch (e) { return null; } }"
            )
        except Exception as exc:  # noqa: BLE001
            result.record_failure(f"inline state harvest failed: {exc!r}")
            return
        if not raw or raw == "null":
            return
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            result.record_failure(f"inline state is not valid JSON: {exc!r}")
            return
        captured.append(
            {"url": "inline:__APOLLO_STATE__", "status": None, "body": body,
             "source": "inline", "captured_at": utc_now_iso()}
        )

    def _drive_page(
        self,
        page: Any,
        review_url: str,
        limit: int,
        result: CollectionResult,
        captured: list[dict[str, Any]],
    ) -> None:
        """Open the review page and press 더보기 until the limit is reached."""
        try:
            page.goto(
                review_url, timeout=self.page_timeout_ms, wait_until="domcontentloaded"
            )
        except Exception as exc:  # noqa: BLE001
            result.record_failure(f"navigation to {review_url} failed: {exc!r}")
            return

        self._settle(page)

        empty_rounds = 0
        rounds = 0
        # Each press appends roughly one page of reviews; cap the loop so a
        # never-ending pager cannot spin forever.
        max_rounds = max(2, limit // 5 + 10)

        def progress() -> int:
            # Growth shows up either as intercepted API reviews or as rendered
            # list items -- watch both so a quiet network path still paginates.
            return max(self._count_unique(captured), self._count_dom_items(page))

        while rounds < max_rounds and empty_rounds < self.max_empty_rounds:
            seen_before = progress()
            if seen_before >= limit:
                break
            rounds += 1
            if not self._click_load_more(page, result):
                # No pager button: try scrolling, in case the list is a true
                # infinite scroll on this layout.
                page.mouse.wheel(0, 20_000)
            # Conservative pacing: we are a guest on someone else's service.
            time.sleep(self.request_delay)
            self._settle(page)
            if progress() <= seen_before:
                empty_rounds += 1
            else:
                empty_rounds = 0

        if rounds >= max_rounds:
            result.record_failure(
                f"stopped after {rounds} pagination rounds without reaching "
                f"the limit of {limit}"
            )

    def _settle(self, page: Any) -> None:
        """Wait for in-flight XHRs, tolerating pages that never go idle."""
        try:
            page.wait_for_load_state("networkidle", timeout=self.page_timeout_ms)
        except Exception:  # noqa: BLE001 - long-poll/analytics keep it busy
            pass

    def _click_load_more(self, page: Any, result: CollectionResult) -> bool:
        for selector in self.load_more_selectors:
            try:
                locator = page.locator(selector).last
                if locator.count() == 0 or not locator.is_visible():
                    continue
                locator.scroll_into_view_if_needed(timeout=3_000)
                locator.click(timeout=5_000)
                log.debug("load-more clicked via %s", selector)
                return True
            except Exception as exc:  # noqa: BLE001 - selector rot is expected
                log.debug("load-more selector %s failed: %r", selector, exc)
                continue
        return False

    def _count_unique(self, captured: list[dict[str, Any]]) -> int:
        keys = set()
        for response in captured:
            for _path, node in iter_review_nodes(response.get("body")):
                keys.add(_native_id(node) or content_fingerprint(node))
        return len(keys)

    def _count_dom_items(self, page: Any) -> int:
        for selector in self.review_item_selectors:
            try:
                count = page.locator(selector).count()
            except Exception:  # noqa: BLE001
                continue
            if count:
                return count
        return 0

    def _dump_debug(
        self, page: Any, result: CollectionResult, api_trace: list[str]
    ) -> None:
        """When the primary path saw nothing, keep what the page really did."""
        if not self.debug_dir:
            return
        import pathlib

        debug_dir = pathlib.Path(self.debug_dir)
        debug_dir.mkdir(parents=True, exist_ok=True)
        stem = f"debug_{result.resolved_place_id or 'unknown'}"
        try:
            (debug_dir / f"{stem}_requests.txt").write_text(
                "\n".join(api_trace) or "(no graphql requests observed)",
                encoding="utf-8",
            )
            (debug_dir / f"{stem}_page.html").write_text(
                page.content(), encoding="utf-8"
            )
            page.screenshot(path=str(debug_dir / f"{stem}.png"), full_page=False)
            log.info("wrote debug snapshot -> %s/%s*", debug_dir, stem)
        except Exception as exc:  # noqa: BLE001
            result.record_failure(f"debug dump failed: {exc!r}")

    # ------------------------------------------------------------------
    def _extract_dom_reviews(
        self, page: Any, result: CollectionResult
    ) -> list[dict[str, Any]]:
        """Best-effort read of the rendered list, used only as a fallback.

        Only text that is actually on the page is captured. Fields that cannot
        be located are simply absent, so the mapper writes ``None``.
        """
        items: list[dict[str, Any]] = []
        for selector in self.review_item_selectors:
            try:
                nodes = page.locator(selector)
                count = nodes.count()
            except Exception as exc:  # noqa: BLE001
                log.debug("dom selector %s failed: %r", selector, exc)
                continue
            if count == 0:
                continue
            for index in range(count):
                try:
                    node = nodes.nth(index)
                    text = (node.inner_text() or "").strip()
                    if not text:
                        continue
                    images = node.locator("img").evaluate_all(
                        "els => els.map(e => e.src).filter(Boolean)"
                    )
                    item: dict[str, Any] = {
                        "_dom_index": index,
                        "_dom_selector": selector,
                        "body": text,
                        "media": [
                            {"thumbnail": src}
                            for src in images
                            if _is_review_photo(src)
                        ],
                    }
                    item.update(_parse_dom_text(text))
                    items.append(item)
                except Exception as exc:  # noqa: BLE001
                    result.record_failure(f"dom item {index} unreadable: {exc!r}")
            if items:
                break
        return items

    # ------------------------------------------------------------------
    def _assemble(
        self,
        result: CollectionResult,
        captured: list[dict[str, Any]],
        dom_items: list[dict[str, Any]],
        limit: int,
    ) -> None:
        """Turn captured traffic into de-duplicated :class:`RawReview` records."""
        seen: set[str] = set()

        def add(payload: dict[str, Any], source: str) -> bool:
            key = _native_id(payload) or content_fingerprint(payload)
            if key in seen:
                result.duplicates_removed += 1
                return False
            seen.add(key)
            result.reviews.append(
                RawReview(
                    platform=self.platform,
                    place_id=result.resolved_place_id,
                    place_name=result.resolved_place_name,
                    place_url=result.resolved_place_url,
                    payload=payload,
                    source=source,
                    native_id=_native_id(payload),
                )
            )
            return True

        for response in captured:
            if self.keep_raw_responses:
                result.raw_responses.append(response)
            for _path, node in iter_review_nodes(response.get("body")):
                if len(result.reviews) >= limit:
                    break
                add(node, response.get("source", "graphql"))

        if not result.reviews and dom_items:
            log.info(
                "[%s] no graphql reviews captured; falling back to DOM extraction",
                result.target.name,
            )
            for item in dom_items:
                if len(result.reviews) >= limit:
                    break
                add(item, "dom")

        if not result.reviews:
            result.record_failure(
                "no reviews found via graphql interception or DOM fallback"
            )
