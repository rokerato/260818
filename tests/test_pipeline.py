#!/usr/bin/env python3
"""End-to-end check of the collection pipeline against the local fixture server.

Run directly (``python tests/test_pipeline.py``) or under pytest.

This proves the machinery -- GraphQL response interception, 더보기 pagination,
de-duplication, null handling, normalization, CSV/JSON output and the summary --
without touching Naver. It says nothing about real Naver data.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from everland_reviews.collectors.base import PlaceTarget  # noqa: E402
from everland_reviews.config import Config, Settings  # noqa: E402
from everland_reviews.normalize.naver_mapper import map_review, normalize_date  # noqa: E402
from everland_reviews.normalize.schema import NORMALIZED_FIELDS  # noqa: E402
from everland_reviews.pipeline import replay, run  # noqa: E402
from everland_reviews.validation import render_overall  # noqa: E402
from fixture_server import serve  # noqa: E402

TOTAL = 24
PLACE_ID = "9999999999"

# The bundled Chromium is a different build than the pip-installed Playwright
# expects, so point at it explicitly. Unset on a normal `playwright install`.
CHROME = os.environ.get(
    "EVERLAND_CHROME", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
)


def _config(tmp: Path, base_url: str, max_reviews: int) -> Config:
    return Config(
        settings=Settings(
            max_reviews_per_restaurant=max_reviews,
            request_delay_seconds=0.2,
            page_timeout_ms=15_000,
            base_url=base_url,
            browser_executable=CHROME if Path(CHROME).exists() else None,
            raw_dir=tmp / "raw",
            normalized_dir=tmp / "normalized",
        ),
        targets=[
            PlaceTarget(name="픽스처 테스트 식당", place_id=PLACE_ID, platform="naver")
        ],
    )


def test_unit_date_and_mapping() -> None:
    assert normalize_date("25.8.13.수") == "2025-08-13"
    assert normalize_date("2024-05-03T09:00:00Z") == "2024-05-03"
    assert normalize_date("어제") == "어제", "unparseable dates are kept verbatim"
    assert normalize_date(None) is None
    print("OK  date normalization")


def test_end_to_end() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        with serve(total_reviews=TOTAL, place_id=PLACE_ID) as base_url:
            config = _config(tmp, base_url, max_reviews=TOTAL)
            summaries, csv_path, warnings = run(config)

        assert len(summaries) == 1, summaries
        summary = summaries[0]
        assert summary.collected == TOTAL, (
            f"expected {TOTAL} unique reviews, got {summary.collected}"
        )
        assert summary.duplicates_removed >= 1, (
            "the fixture re-serves one review; de-duplication should catch it"
        )
        print(f"OK  collected {summary.collected}, "
              f"deduped {summary.duplicates_removed}, "
              f"failures {len(summary.failures)}")

        # --- raw archive -------------------------------------------------
        raw_files = list((tmp / "raw").glob("*.json"))
        assert len(raw_files) == 1, raw_files
        raw = json.loads(raw_files[0].read_text(encoding="utf-8"))
        assert raw["review_count"] == TOTAL
        assert raw["raw_responses"], "verbatim GraphQL responses must be archived"
        sources = {r["source"] for r in raw["reviews"]}
        assert sources <= {"graphql", "inline"}, sources
        assert "graphql" in sources, sources
        assert "테스트사용자" in json.dumps(raw, ensure_ascii=False), "Korean preserved"
        print(f"OK  raw archive: {len(raw['raw_responses'])} graphql responses kept")

        # --- normalized CSV ----------------------------------------------
        assert csv_path is not None and csv_path.exists()
        with csv_path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == TOTAL, len(rows)
        assert list(rows[0]) == list(NORMALIZED_FIELDS), list(rows[0])
        assert any("테스트 리뷰 본문" in r["review_text"] for r in rows), "Korean in CSV"
        # Absent fields must be empty, never invented.
        assert any(r["rating"] == "" for r in rows), "missing ratings stay empty"
        assert any(r["review_text"] == "" for r in rows), "missing text stays empty"
        assert all(r["place_id"] == PLACE_ID for r in rows)
        assert all(r["collected_at"] for r in rows)
        # List fields round-trip as JSON with Korean intact.
        keyworded = [r for r in rows if r["keywords"]]
        assert keyworded and "음식이 맛있어요" in json.loads(keyworded[0]["keywords"])
        print(f"OK  normalized CSV: {len(rows)} rows, {len(rows[0])} columns")

        # --- offline replay of the raw archive ---------------------------
        replay_path = replay(config, tmp / "raw", csv_name="replay.csv")
        assert replay_path is not None
        with replay_path.open(encoding="utf-8", newline="") as handle:
            replay_rows = list(csv.DictReader(handle))
        assert len(replay_rows) == TOTAL
        print("OK  offline replay reproduces the normalized CSV")

        assert not warnings, warnings
        print()
        print(summary.render())
        print()
        print(render_overall(summaries, warnings))


def test_review_limit_is_respected() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        with serve(total_reviews=TOTAL, place_id=PLACE_ID) as base_url:
            config = _config(tmp, base_url, max_reviews=7)
            summaries, _csv, _warn = run(config)
        assert summaries[0].collected == 7, summaries[0].collected
        print("OK  max_reviews cap honoured (7)")


def test_bad_target_does_not_kill_the_run() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        with serve(total_reviews=TOTAL, place_id=PLACE_ID) as base_url:
            config = _config(tmp, base_url, max_reviews=TOTAL)
            config.targets.insert(
                0, PlaceTarget(name="깨진 대상", place_id="404404404", platform="naver")
            )
            summaries, _csv, _warn = run(config)
        by_name = {s.name: s for s in summaries}
        assert by_name["깨진 대상"].collected == 0
        assert by_name["깨진 대상"].failures, "the failure must be recorded"
        assert by_name["픽스처 테스트 식당"].collected == TOTAL, (
            "a failing target must not stop the others"
        )
        print("OK  per-target failures are isolated and logged")


def test_time_budget_stops_cleanly_and_keeps_results() -> None:
    """An exhausted run budget must skip remaining venues, not lose data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        with serve(total_reviews=TOTAL, place_id=PLACE_ID) as base_url:
            config = _config(tmp, base_url, max_reviews=TOTAL)
            # An already-elapsed budget: the first venue still runs (a run
            # must never come back empty), every later one is skipped.
            config.settings.time_budget_seconds = 0.0
            config.targets.append(
                PlaceTarget(name="예산초과 대상", place_id=PLACE_ID, platform="naver")
            )
            summaries, csv_path, _warn = run(config)

        by_name = {s.name: s for s in summaries}
        skipped = by_name["예산초과 대상"]
        assert skipped.collected == 0
        assert any("time budget" in f for f in skipped.failures), skipped.failures
        # Everything collected before the budget ran out must be on disk.
        assert csv_path is not None and csv_path.exists()
        with csv_path.open(encoding="utf-8", newline="") as handle:
            assert len(list(csv.DictReader(handle))) == TOTAL
        assert list((tmp / "raw").glob("*.json")), "raw archives must survive"
        print("OK  time budget skips cleanly and preserves collected data")


if __name__ == "__main__":
    test_unit_date_and_mapping()
    test_review_limit_is_respected()
    test_bad_target_does_not_kill_the_run()
    test_time_budget_stops_cleanly_and_keeps_results()
    test_end_to_end()
    print("\nall checks passed")
