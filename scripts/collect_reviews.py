#!/usr/bin/env python3
"""Collect Everland restaurant/cafe reviews and write raw JSON + normalized CSV.

Examples
--------
    python scripts/collect_reviews.py --config config/restaurants.yaml
    python scripts/collect_reviews.py --max-reviews 50 --only "카페"
    python scripts/collect_reviews.py --replay-raw data/raw   # no network
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from everland_reviews.config import ConfigError, load_config  # noqa: E402
from everland_reviews.pipeline import print_report, replay, run  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/restaurants.yaml")
    parser.add_argument(
        "--max-reviews", type=int,
        help="override the per-restaurant review cap from the config",
    )
    parser.add_argument(
        "--delay", type=float,
        help="seconds to wait between pagination interactions (be conservative)",
    )
    parser.add_argument(
        "--only", action="append", metavar="SUBSTRING",
        help="only collect restaurants whose name contains SUBSTRING (repeatable)",
    )
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--normalized-dir", type=Path)
    parser.add_argument("--csv-name")
    parser.add_argument(
        "--base-url",
        help="override the Naver place host (used by the offline test fixture)",
    )
    parser.add_argument(
        "--csv-bom", action="store_true",
        help="write the CSV as utf-8-sig so Excel renders Korean correctly",
    )
    parser.add_argument(
        "--headful", action="store_true", help="run the browser with a visible window"
    )
    parser.add_argument("--browser-executable")
    parser.add_argument(
        "--replay-raw", type=Path, metavar="DIR",
        help="skip collection and re-normalize archived raw JSON from DIR",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    settings = config.settings
    if args.max_reviews is not None:
        settings.max_reviews_per_restaurant = args.max_reviews
    if args.delay is not None:
        settings.request_delay_seconds = args.delay
    if args.raw_dir:
        settings.raw_dir = args.raw_dir
    if args.normalized_dir:
        settings.normalized_dir = args.normalized_dir
    if args.base_url:
        settings.base_url = args.base_url
    if args.browser_executable:
        settings.browser_executable = args.browser_executable
    if args.csv_bom:
        settings.csv_encoding = "utf-8-sig"
    if args.headful:
        settings.headless = False

    if args.replay_raw:
        path = replay(config, args.replay_raw, args.csv_name)
        if path is None:
            print("no raw reviews found to replay", file=sys.stderr)
            return 1
        print(f"replayed normalized CSV -> {path}")
        return 0

    if args.only:
        needles = [n.lower() for n in args.only]
        config.targets = [
            t for t in config.targets if any(n in t.name.lower() for n in needles)
        ]

    if not config.targets:
        print(
            "No restaurants to collect.\n"
            f"Add real Naver Place URLs to {args.config} under 'restaurants:'.",
            file=sys.stderr,
        )
        return 1

    summaries, csv_path, warnings = run(config, csv_name=args.csv_name)
    print_report(summaries, warnings)

    if csv_path:
        print(f"\nNormalized CSV: {csv_path}")
    total = sum(s.collected for s in summaries)
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
