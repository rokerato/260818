#!/usr/bin/env python3
"""Discover real venue targets for a configured location from Naver Map search.

Meant to run where egress to naver.com is open (the GitHub Actions workflow in
.github/workflows/collect-reviews.yml) -- the Claude Code sandbox cannot reach
Naver.

Every candidate comes from Naver's own search responses; nothing is invented.
A venue is accepted only when it verifiably belongs to the location, by the
test that location configures in config/locations.yaml: proximity to a centre
point (resolved from Naver at run time, so no coordinates are hand-written),
an exact address substring, or both. Its category must also look like food
service. Rejections are counted by reason, and raw search responses are
archived under logs/, so a thin result set can be diagnosed offline.

Usage:
    python scripts/discover_places.py --location sinlicheon
    python scripts/discover_places.py --location everland --max-targets 30
    python scripts/discover_places.py --location sinlicheon --dry-run
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

#: Fallback query set when a location defines none.
DEFAULT_QUERIES = ["카페", "맛집", "레스토랑"]

FOOD_TOKENS = (
    "음식", "식당", "레스토랑", "카페", "한식", "양식", "중식", "일식", "분식",
    "패스트푸드", "치킨", "피자", "버거", "베이커리", "디저트", "뷔페", "푸드",
    "브런치", "커피", "차", "술집", "주점", "바",
)
DEFAULT_EXCLUDE_TOKENS = ("주차장",)

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    import math

    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _as_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


#: South Korea's bounding box. A pair that lands outside it after scaling is
#: a mis-read field, not a venue -- reject rather than guess.
_LAT_RANGE = (32.0, 40.0)
_LNG_RANGE = (124.0, 132.0)

#: Naver returns coordinates as plain degrees, or as integers scaled by 1e7
#: (``mapx``/``mapy``). Some legacy endpoints return a projected TM128 pair,
#: which fits no scaling and is correctly discarded.
_COORD_SCALES = (1.0, 1e-7, 1e-6, 1e-5)


def extract_coords(node: dict[str, Any]) -> tuple[float, float] | None:
    """Best-effort (lat, lng) from a place node, across observed key names.

    The scaling is chosen by testing candidates against Korea's bounding box
    and requiring *both* coordinates to fit under the same scale, so a
    half-plausible reading cannot slip through.
    """
    candidates = [
        (node.get("y"), node.get("x")),
        (node.get("mapy"), node.get("mapx")),
        (node.get("latitude"), node.get("longitude")),
        (node.get("lat"), node.get("lng")),
    ]
    coord = node.get("coord") or node.get("coordinate")
    if isinstance(coord, dict):
        candidates.append((coord.get("y"), coord.get("x")))

    for raw_lat, raw_lng in candidates:
        lat, lng = _as_number(raw_lat), _as_number(raw_lng)
        if lat is None or lng is None or (lat == 0 and lng == 0):
            continue
        for scale in _COORD_SCALES:
            slat, slng = lat * scale, lng * scale
            if (
                _LAT_RANGE[0] < slat < _LAT_RANGE[1]
                and _LNG_RANGE[0] < slng < _LNG_RANGE[1]
            ):
                return slat, slng
    return None


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


def resolve_center(
    location: dict[str, Any], places: list[dict[str, Any]]
) -> tuple[float, float] | None:
    """Work out the centre point for a location.

    An explicit ``center: [lat, lng]`` in the config wins. Otherwise the
    centre is derived from Naver's own results for ``center_query`` by taking
    the *median* coordinate, which ignores the one stray result a text search
    usually drags in from another city. Nothing is hand-written, and the
    resolved point is printed so it can be sanity-checked.
    """
    explicit = location.get("center")
    if isinstance(explicit, (list, tuple)) and len(explicit) == 2:
        lat, lng = float(explicit[0]), float(explicit[1])
        print(f"  centre from config: {lat:.6f}, {lng:.6f}")
        return lat, lng

    coords = [c for c in (extract_coords(n) for n in places) if c]
    if not coords:
        return None
    lats = sorted(c[0] for c in coords)
    lngs = sorted(c[1] for c in coords)
    mid = len(coords) // 2
    center = (lats[mid], lngs[mid])
    print(
        f"  centre resolved from {len(coords)} result(s): "
        f"{center[0]:.6f}, {center[1]:.6f}"
    )
    return center


def filter_places(
    places: list[dict[str, Any]],
    location: dict[str, Any],
    center: tuple[float, float] | None,
) -> list[dict[str, Any]]:
    """Keep food venues that verifiably belong to this location.

    A venue must pass every test the location configures -- proximity to the
    centre, and/or an address substring -- plus the food-category test. A
    venue that cannot be verified is dropped and the reason printed, so a
    thin result set is diagnosable rather than mysterious.
    """
    radius_m = location.get("radius_m")
    address_contains = location.get("address_contains")
    exclude = tuple(location.get("exclude_tokens") or DEFAULT_EXCLUDE_TOKENS)
    if not radius_m and not address_contains:
        raise SystemExit(
            "location must set 'radius_m' (with a centre) or 'address_contains'; "
            "without one of them there is no way to verify a venue belongs here"
        )

    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

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

        if address_contains and address_contains not in address:
            reject(f"address lacks {address_contains!r}")
            continue

        distance = None
        if radius_m:
            if center is None:
                reject("no centre resolved")
                continue
            coords = extract_coords(node)
            if coords is None:
                reject("no usable coordinates")
                continue
            distance = haversine_m(center[0], center[1], coords[0], coords[1])
            if distance > float(radius_m):
                reject(f"beyond {radius_m}m")
                continue

        if any(tok in category_blob or tok in name for tok in exclude):
            reject("excluded token")
            continue
        if not categories:
            reject("no category information")
            continue
        if not any(tok in category_blob for tok in FOOD_TOKENS):
            reject(f"non-food category")
            continue

        seen.add(place_id)
        entry = {
            "name": name,
            "place_id": place_id,
            # pcmap serves cafes and bakeries under /restaurant/ as well;
            # the /cafe/ segment renders an empty document (run
            # 33468327404). The collector falls back across segments anyway,
            # so start from the one known to work.
            "category": "restaurant",
            "address": address,
            "naver_category": categories,
        }
        if distance is not None:
            entry["distance_m"] = round(distance)
        accepted.append(entry)

    if rejected:
        print("  rejected: " + ", ".join(
            f"{reason} x{count}" for reason, count in
            sorted(rejected.items(), key=lambda kv: -kv[1])
        ))
    accepted.sort(key=lambda e: e.get("distance_m", 0))
    return accepted


def merge_into_config(config_path: Path, targets: list[dict[str, Any]]) -> None:
    import yaml

    document = (
        yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if config_path.is_file()
        else {}
    )
    # Preserve any hand-tuned settings block; replace only the target list.
    document["restaurants"] = targets
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def load_location(path: Path, key: str) -> dict[str, Any]:
    import yaml

    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    locations = document.get("locations") or {}
    if key not in locations:
        raise SystemExit(
            f"unknown location {key!r}; {path} defines: {sorted(locations)}"
        )
    return locations[key]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--location", default="everland",
        help="key from config/locations.yaml (default: everland)",
    )
    parser.add_argument("--locations-file", default=str(ROOT / "config/locations.yaml"))
    parser.add_argument(
        "--query", action="append",
        help="override the location's search queries (repeatable)",
    )
    parser.add_argument("--max-targets", type=int, default=30)
    parser.add_argument(
        "--config",
        help="where to write the target list "
             "(default: config/targets/<location>.yaml)",
    )
    parser.add_argument("--chromium-path", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    location = load_location(Path(args.locations_file), args.location)
    label = location.get("label", args.location)
    queries = args.query or location.get("queries") or DEFAULT_QUERIES
    print(f"location: {label}  ({len(queries)} queries)")

    # --- resolve the centre, when this location verifies by proximity -----
    center: tuple[float, float] | None = None
    if location.get("radius_m"):
        center_query = location.get("center_query") or label
        print(f"resolving centre from: {center_query}")
        seed, raw = search_http(center_query)
        if not seed:
            seed, raw = search_browser(center_query, args.chromium_path)
        if raw is not None:
            _archive(f"{args.location}_center", raw)
        center = resolve_center(location, seed)
        if center is None:
            print(
                "\ncould not resolve a centre for this location. Raw responses "
                "are in logs/ for diagnosis; set an explicit 'center: [lat, lng]' "
                "in the locations file to proceed.",
                file=sys.stderr,
            )
            return 1

    # --- search ------------------------------------------------------------
    all_places: list[dict[str, Any]] = []
    for query in queries:
        print(f"searching: {query}")
        places, raw = search_http(query)
        if not places:
            print("  http endpoints yielded nothing; trying browser fallback")
            places, raw = search_browser(query, args.chromium_path)
        if raw is not None:
            slug = re.sub(r"\W+", "-", query)
            _archive(f"{args.location}_{slug}", raw)
        print(f"  {len(places)} raw place entries")
        all_places.extend(places)

    targets = filter_places(all_places, location, center)[: args.max_targets]
    if not targets:
        print(
            f"\nNo verified venues found for {label}. Raw responses are in "
            "logs/ for diagnosis; the target file was NOT modified.",
            file=sys.stderr,
        )
        return 1

    print(f"\nverified targets for {label} ({len(targets)}):")
    for target in targets:
        distance = target.get("distance_m")
        suffix = f"  [{distance}m]" if distance is not None else ""
        print(f"  - {target['name']}  id={target['place_id']}{suffix}")
        print(f"      {target['address']}  {target['naver_category']}")

    if args.dry_run:
        print("\n--dry-run: target file not modified")
        return 0

    config_path = Path(
        args.config or (ROOT / "config/targets" / f"{args.location}.yaml")
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    merge_into_config(config_path, targets)
    print(f"\nwrote {len(targets)} targets -> {config_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
