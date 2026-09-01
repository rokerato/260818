#!/usr/bin/env python3
"""Discover real Everland restaurant/cafe targets from Naver Map search.

Meant to run from a machine with open egress to naver.com (e.g. the GitHub
Actions workflow in .github/workflows/collect-reviews.yml) -- the Claude Code
sandbox cannot reach Naver.

Every candidate comes from Naver's own search responses; nothing is invented.
A venue is accepted only when its returned address places it on Everland's
street (에버랜드로, Yongin) and its category looks like food service. Raw search
responses are archived under logs/ so a failed or surprising run can be
diagnosed offline.

Usage:
    python scripts/discover_places.py                       # default queries
    python scripts/discover_places.py --query "에버랜드 카페" --max-targets 3
    python scripts/discover_places.py --dry-run             # print, don't write
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_QUERIES = ["에버랜드 레스토랑", "에버랜드 맛집", "에버랜드 카페"]
#: Everland's street address: 경기 용인시 처인구 포곡읍 에버랜드로 199.
ADDRESS_MARKER = "에버랜드로"
FOOD_TOKENS = (
    "음식", "식당", "레스토랑", "카페", "한식", "양식", "중식", "일식", "분식",
    "패스트푸드", "치킨", "피자", "버거", "베이커리", "디저트", "뷔페", "푸드",
)
EXCLUDE_TOKENS = ("테마파크", "놀이공원", "주차장", "호텔", "리조트", "동물원")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://map.naver.com/",
}

_LOG_DIR = ROOT / "logs"


def _http_json(url: str, timeout: float = 20.0) -> Any:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _archive(name: str, payload: Any) -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = _LOG_DIR / f"discovery_{name}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )


def _walk_place_dicts(node: Any) -> Iterator[dict[str, Any]]:
    """Yield dicts that look like place entries, wherever the envelope puts them."""
    if isinstance(node, dict):
        place_id = node.get("id") or node.get("placeId") or node.get("sid")
        name = node.get("name") or node.get("title")
        address = (
            node.get("roadAddress") or node.get("road_address")
            or node.get("address") or node.get("fullAddress")
        )
        if (
            isinstance(place_id, (str, int))
            and re.fullmatch(r"\d{5,}", str(place_id))
            and isinstance(name, str)
            and isinstance(address, str)
        ):
            yield node
        for value in node.values():
            yield from _walk_place_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_place_dicts(value)


def _clean_name(name: str) -> str:
    return re.sub(r"<[^>]+>", "", name).strip()


def _categories(node: dict[str, Any]) -> list[str]:
    raw = node.get("category") or node.get("categories") or node.get("bizhourInfo")
    if isinstance(raw, str):
        return [c.strip() for c in re.split(r"[,>]", raw) if c.strip()]
    if isinstance(raw, list):
        return [str(c).strip() for c in raw if str(c).strip()]
    return []


def search_http(query: str) -> tuple[list[dict[str, Any]], Any]:
    """Query the endpoints the Naver Map frontend itself uses."""
    encoded = urllib.parse.quote(query)
    attempts = [
        # Current map.naver.com search API (Everland-centred coordinates).
        f"https://map.naver.com/p/api/search/allSearch?query={encoded}&type=all&searchCoord=127.202324%3B37.293849",
        # Legacy mobile search API; still JSON, different envelope.
        f"https://m.map.naver.com/search2/searchMore.naver?query={encoded}&page=1&displayCount=30&type=SITE_1",
    ]
    for url in attempts:
        try:
            body = _http_json(url)
            places = list(_walk_place_dicts(body))
            if places:
                return places, {"url": url, "body": body}
            print(f"  no place entries in response from {url.split('?')[0]}")
        except Exception as exc:  # noqa: BLE001 - try the next endpoint
            print(f"  http attempt failed ({url.split('?')[0]}): {exc!r}")
    return [], None


def search_browser(query: str, chromium_path: str | None) -> tuple[list[dict[str, Any]], Any]:
    """Fallback: drive map.naver.com and intercept its own search XHRs."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright not installed; browser fallback unavailable")
        return [], None

    captured: list[Any] = []
    with sync_playwright() as pw:
        kwargs: dict[str, Any] = {"headless": True}
        if chromium_path:
            kwargs["executable_path"] = chromium_path
        browser = pw.chromium.launch(**kwargs)
        try:
            page = browser.new_context(locale="ko-KR").new_page()

            def on_response(response: Any) -> None:
                if "search" not in response.url.lower():
                    return
                try:
                    captured.append({"url": response.url, "body": response.json()})
                except Exception:  # noqa: BLE001 - non-JSON response
                    pass

            page.on("response", on_response)
            page.goto(
                f"https://map.naver.com/p/search/{urllib.parse.quote(query)}",
                timeout=45_000,
                wait_until="domcontentloaded",
            )
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)
        finally:
            browser.close()

    places: list[dict[str, Any]] = []
    for entry in captured:
        places.extend(_walk_place_dicts(entry["body"]))
    return places, captured


def filter_everland_food(places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in places:
        place_id = str(node.get("id") or node.get("placeId") or node.get("sid"))
        if place_id in seen:
            continue
        name = _clean_name(str(node.get("name") or node.get("title")))
        address = str(
            node.get("roadAddress") or node.get("road_address")
            or node.get("address") or node.get("fullAddress") or ""
        )
        categories = _categories(node)
        category_blob = " ".join(categories)

        if ADDRESS_MARKER not in address:
            continue
        if any(tok in category_blob or tok in name for tok in EXCLUDE_TOKENS):
            continue
        if categories and not any(tok in category_blob for tok in FOOD_TOKENS):
            print(f"  skipped (non-food category {categories}): {name}")
            continue
        if not categories:
            print(f"  skipped (no category information): {name} [{place_id}]")
            continue

        seen.add(place_id)
        accepted.append(
            {
                "name": name,
                "place_id": place_id,
                "category": "cafe" if "카페" in category_blob else "restaurant",
                "address": address,
                "naver_category": categories,
            }
        )
    return accepted


def merge_into_config(config_path: Path, targets: list[dict[str, Any]]) -> None:
    import yaml

    document = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    document["restaurants"] = targets
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query", action="append",
        help=f"search query, repeatable (default: {DEFAULT_QUERIES})",
    )
    parser.add_argument("--max-targets", type=int, default=3)
    parser.add_argument("--config", default=str(ROOT / "config/restaurants.yaml"))
    parser.add_argument("--chromium-path", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    all_places: list[dict[str, Any]] = []
    for query in args.query or DEFAULT_QUERIES:
        print(f"searching: {query}")
        places, raw = search_http(query)
        if not places:
            print("  http endpoints yielded nothing; trying browser fallback")
            places, raw = search_browser(query, args.chromium_path)
        if raw is not None:
            _archive(re.sub(r"\W+", "-", query), raw)
        print(f"  {len(places)} raw place entries")
        all_places.extend(places)

    targets = filter_everland_food(all_places)[: args.max_targets]
    if not targets:
        print(
            "\nNo verified Everland food venues found. Raw responses are in "
            "logs/ for diagnosis. The config file was NOT modified.",
            file=sys.stderr,
        )
        return 1

    print(f"\nverified Everland targets ({len(targets)}):")
    for target in targets:
        print(f"  - {target['name']}  id={target['place_id']}")
        print(f"      {target['address']}  {target['naver_category']}")

    if args.dry_run:
        print("\n--dry-run: config not modified")
        return 0
    merge_into_config(Path(args.config), targets)
    print(f"\nwrote {len(targets)} targets -> {args.config}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
