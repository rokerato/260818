#!/usr/bin/env python3
"""Offline checks for location-driven target discovery.

Exercises coordinate parsing, the proximity/address verification rules and
the food-category filter against synthetic search nodes. No network needed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "discover_places", ROOT / "scripts/discover_places.py"
)
dp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dp)

# A point on the strip, and venues at increasing distance from it.
CENTER = (37.2000, 127.0700)


def _node(name, pid, lat, lng, cats, address="경기 화성시 동탄"):
    return {
        "id": pid, "name": name, "y": str(lat), "x": str(lng),
        "category": cats, "roadAddress": address,
    }


def test_coordinate_encodings() -> None:
    assert dp.extract_coords({"y": "37.2", "x": "127.07"}) == (37.2, 127.07)
    scaled = dp.extract_coords({"mapy": 372000000, "mapx": 1270700000})
    assert scaled and abs(scaled[0] - 37.2) < 1e-6 and abs(scaled[1] - 127.07) < 1e-6
    assert dp.extract_coords({"coord": {"y": 37.2, "x": 127.07}}) == (37.2, 127.07)
    # Rejected: zeroes, a TM128 projected pair, and a location outside Korea.
    assert dp.extract_coords({"y": "0", "x": "0"}) is None
    assert dp.extract_coords({"mapx": 310000, "mapy": 550000}) is None
    assert dp.extract_coords({"y": "51.5", "x": "-0.12"}) is None
    print("OK  coordinate encodings parsed, bad ones rejected")


def test_radius_filter() -> None:
    location = {"radius_m": 500, "exclude_tokens": ["주차장"]}
    nodes = [
        _node("가까운카페", "1", 37.2001, 127.0701, ["음식점", "카페,디저트"]),
        _node("경계밖카페", "2", 37.2100, 127.0700, ["음식점", "카페"]),  # ~1.1km
        _node("가까운주차장", "3", 37.2001, 127.0702, ["주차장"]),
        _node("가까운학원", "4", 37.2002, 127.0701, ["학원"]),
        _node("좌표없음", "5", None, None, ["카페"]),
    ]
    nodes[4].update({"y": None, "x": None})
    kept = dp.filter_places(nodes, location, CENTER)
    assert [k["name"] for k in kept] == ["가까운카페"], kept
    assert kept[0]["distance_m"] < 20, kept[0]
    assert kept[0]["category"] == "restaurant", "always start on the working path"
    print("OK  radius filter keeps only nearby food venues")


def test_address_filter_still_works() -> None:
    """The Everland rule (exact shared address) must keep working."""
    location = {"address_contains": "에버랜드로", "exclude_tokens": ["테마파크"]}
    nodes = [
        _node("파크식당", "1", 37.29, 127.20, ["음식점", "한식"],
              "경기 용인시 처인구 포곡읍 에버랜드로 199"),
        _node("길건너식당", "2", 37.29, 127.20, ["음식점", "한식"],
              "경기 용인시 처인구 포곡읍 다른길 3"),
        _node("에버랜드", "3", 37.29, 127.20, ["테마파크"],
              "경기 용인시 처인구 포곡읍 에버랜드로 199"),
    ]
    kept = dp.filter_places(nodes, location, None)
    assert [k["name"] for k in kept] == ["파크식당"], kept
    assert "distance_m" not in kept[0], "no radius configured, so no distance"
    print("OK  address-based verification unchanged")


def test_unverifiable_location_is_refused() -> None:
    """A location with no verification rule must fail loudly, not accept all."""
    try:
        dp.filter_places([_node("아무카페", "1", 37.2, 127.07, ["카페"])], {}, None)
    except SystemExit as exc:
        assert "radius_m" in str(exc), exc
        print("OK  a location with no verification rule is refused")
        return
    raise AssertionError("expected SystemExit for an unverifiable location")


def test_center_resolution_ignores_outliers() -> None:
    location = {"radius_m": 500}
    places = [
        _node("a", "1", 37.2000, 127.0700, ["카페"]),
        _node("b", "2", 37.2002, 127.0702, ["카페"]),
        _node("c", "3", 35.1000, 129.0000, ["카페"]),  # a stray in Busan
    ]
    center = dp.resolve_center(location, places)
    assert center is not None and abs(center[0] - 37.2002) < 0.01, center
    print("OK  centre resolution ignores a stray far-away result")


def test_config_locations_are_valid() -> None:
    import yaml

    doc = yaml.safe_load((ROOT / "config/locations.yaml").read_text(encoding="utf-8"))
    for key, loc in doc["locations"].items():
        assert loc.get("queries"), f"{key} has no queries"
        assert loc.get("radius_m") or loc.get("address_contains"), (
            f"{key} has no way to verify a venue belongs to it"
        )
        if loc.get("radius_m"):
            assert loc.get("center") or loc.get("center_query"), (
                f"{key} uses a radius but gives no centre or centre query"
            )
    print(f"OK  {len(doc['locations'])} configured location(s) are valid")


if __name__ == "__main__":
    test_coordinate_encodings()
    test_radius_filter()
    test_address_filter_still_works()
    test_unverifiable_location_is_refused()
    test_center_resolution_ignores_outliers()
    test_config_locations_are_valid()
    print("\nall discovery checks passed")
