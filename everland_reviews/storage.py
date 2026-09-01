"""Persistence: verbatim raw JSON, and the normalized CSV."""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .collectors.base import CollectionResult
from .normalize.schema import NORMALIZED_FIELDS, NormalizedReview

log = logging.getLogger(__name__)

_SLUG_STRIP = re.compile(r"[^0-9A-Za-z가-힣]+")


def slugify(value: str) -> str:
    return _SLUG_STRIP.sub("-", value).strip("-").lower() or "unnamed"


def run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_raw(result: CollectionResult, raw_dir: Path, stamp: str) -> Path:
    """Dump the untouched platform payloads plus their responses.

    Keeping the original responses means a later schema change can be replayed
    offline instead of re-crawling the site.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{result.target.platform}_{slugify(result.target.name)}_{stamp}"
    path = raw_dir / f"{stem}.json"
    document = {
        "platform": result.target.platform,
        "target": {
            "name": result.target.name,
            "url": result.target.url,
            "place_id": result.target.place_id,
            "category": result.target.category,
        },
        "resolved": {
            "place_id": result.resolved_place_id,
            "place_url": result.resolved_place_url,
            "place_name": result.resolved_place_name,
        },
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "duplicates_removed": result.duplicates_removed,
        "failures": result.failures,
        "review_count": len(result.reviews),
        "reviews": [review.to_dict() for review in result.reviews],
        "raw_responses": result.raw_responses,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2, default=str)
    log.info("wrote raw payloads -> %s", path)
    return path


def write_normalized_csv(
    reviews: Sequence[NormalizedReview],
    path: Path,
    encoding: str = "utf-8",
) -> Path:
    """Write the normalized CSV.

    ``encoding`` defaults to plain UTF-8. Pass ``utf-8-sig`` if the file will be
    opened by Excel on Windows, which otherwise mis-renders Korean text.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(NORMALIZED_FIELDS))
        writer.writeheader()
        for review in reviews:
            writer.writerow(review.to_csv_row())
    log.info("wrote %d normalized reviews -> %s", len(reviews), path)
    return path


def write_summary_json(summary: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, default=str)
    return path


def load_raw(path: Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def iter_raw_files(raw_dir: Path) -> Iterable[Path]:
    return sorted(Path(raw_dir).glob("*.json"))
