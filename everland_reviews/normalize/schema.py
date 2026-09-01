"""The normalized review schema.

This module is deliberately free of any platform-specific knowledge. Collectors
emit raw platform payloads (see ``everland_reviews.collectors.base``); a mapper
per platform translates those payloads into this schema. Adding a platform means
adding a collector and a mapper, never editing this file.

Every field is nullable. A field is ``None`` when the platform did not publish
it for that review -- never a placeholder, never a guess.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any


#: Column order used for the normalized CSV export.
NORMALIZED_FIELDS: tuple[str, ...] = (
    "platform",
    "place_name",
    "place_id",
    "place_url",
    "review_id",
    "reviewer_name",
    "reviewer_profile_id",
    "reviewer_profile_url",
    "rating",
    "review_text",
    "review_date",
    "visit_date",
    "visit_count",
    "review_image_count",
    "review_image_urls",
    "helpful_count",
    "keywords",
    "menus",
    "verified_visit",
    "collected_at",
    "raw_ref",
)

#: Fields holding a list. They are JSON-encoded (``ensure_ascii=False``) on CSV
#: export so Korean text survives and the value round-trips back to a list.
LIST_FIELDS: frozenset[str] = frozenset(
    {"review_image_urls", "keywords", "menus"}
)


@dataclass(slots=True)
class NormalizedReview:
    """One review, in the shape every downstream analysis step consumes."""

    # --- provenance -------------------------------------------------------
    platform: str | None = None
    place_name: str | None = None
    place_id: str | None = None
    place_url: str | None = None

    # --- review identity --------------------------------------------------
    review_id: str | None = None

    # --- reviewer ---------------------------------------------------------
    reviewer_name: str | None = None
    reviewer_profile_id: str | None = None
    reviewer_profile_url: str | None = None

    # --- content ----------------------------------------------------------
    rating: float | None = None
    review_text: str | None = None
    review_date: str | None = None
    visit_date: str | None = None
    visit_count: int | None = None
    review_image_count: int | None = None
    review_image_urls: list[str] = field(default_factory=list)
    helpful_count: int | None = None
    keywords: list[str] = field(default_factory=list)
    menus: list[str] = field(default_factory=list)
    verified_visit: bool | None = None

    # --- collection metadata ---------------------------------------------
    collected_at: str | None = None
    #: Pointer back into the raw JSON dump: ``<raw_file_stem>#<index>``.
    raw_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_csv_row(self) -> dict[str, Any]:
        """Flatten to strings/scalars suitable for :mod:`csv`."""
        row: dict[str, Any] = {}
        for name in NORMALIZED_FIELDS:
            value = getattr(self, name)
            if name in LIST_FIELDS:
                # Empty list and "field not published" are both written as an
                # empty cell so downstream code never has to special-case "[]".
                row[name] = (
                    json.dumps(value, ensure_ascii=False) if value else ""
                )
            elif value is None:
                row[name] = ""
            elif isinstance(value, bool):
                row[name] = "true" if value else "false"
            else:
                row[name] = value
        return row


def schema_field_names() -> tuple[str, ...]:
    return tuple(f.name for f in fields(NormalizedReview))
