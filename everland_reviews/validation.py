"""Post-collection validation and human-readable summaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .collectors.base import CollectionResult
from .normalize.schema import LIST_FIELDS, NORMALIZED_FIELDS, NormalizedReview


def _is_present(review: NormalizedReview, name: str) -> bool:
    value = getattr(review, name)
    if value is None:
        return False
    if name in LIST_FIELDS:
        return bool(value)
    if isinstance(value, str):
        return bool(value.strip())
    return True


@dataclass(slots=True)
class RestaurantSummary:
    name: str
    platform: str
    place_id: str | None
    place_url: str | None
    collected: int = 0
    with_text: int = 0
    with_rating: int = 0
    with_date: int = 0
    duplicates_removed: int = 0
    failures: list[str] = field(default_factory=list)
    #: Per-field non-null counts, i.e. what the platform actually published.
    field_coverage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "restaurant": self.name,
            "platform": self.platform,
            "place_id": self.place_id,
            "place_url": self.place_url,
            "reviews_collected": self.collected,
            "reviews_with_text": self.with_text,
            "reviews_with_rating": self.with_rating,
            "reviews_with_date": self.with_date,
            "duplicate_reviews_removed": self.duplicates_removed,
            "collection_failures": len(self.failures),
            "failure_messages": self.failures,
            "field_coverage": self.field_coverage,
        }

    def render(self) -> str:
        lines = [
            f"Restaurant: {self.name}",
            f"Reviews collected: {self.collected}",
            f"Reviews with text: {self.with_text}",
            f"Reviews with rating: {self.with_rating}",
            f"Reviews with date: {self.with_date}",
            f"Duplicate reviews removed: {self.duplicates_removed}",
            f"Collection failures: {len(self.failures)}",
        ]
        for message in self.failures:
            lines.append(f"  - {message}")
        return "\n".join(lines)


def summarize_restaurant(
    result: CollectionResult, reviews: Sequence[NormalizedReview]
) -> RestaurantSummary:
    summary = RestaurantSummary(
        name=result.target.name,
        platform=result.target.platform,
        place_id=result.resolved_place_id,
        place_url=result.resolved_place_url,
        collected=len(reviews),
        duplicates_removed=result.duplicates_removed,
        failures=list(result.failures),
    )
    summary.with_text = sum(1 for r in reviews if _is_present(r, "review_text"))
    summary.with_rating = sum(1 for r in reviews if _is_present(r, "rating"))
    summary.with_date = sum(1 for r in reviews if _is_present(r, "review_date"))
    summary.field_coverage = {
        name: sum(1 for r in reviews if _is_present(r, name))
        for name in NORMALIZED_FIELDS
    }
    return summary


def validate_dataset(reviews: Sequence[NormalizedReview]) -> list[str]:
    """Cheap integrity checks. Returns a list of warnings (empty is good)."""
    warnings: list[str] = []
    if not reviews:
        return ["dataset is empty"]

    ids = [r.review_id for r in reviews if r.review_id]
    if len(ids) != len(set(ids)):
        warnings.append(
            f"{len(ids) - len(set(ids))} duplicate review_id value(s) survived de-duplication"
        )
    missing_id = sum(1 for r in reviews if not r.review_id)
    if missing_id:
        warnings.append(f"{missing_id} review(s) have no platform review_id")
    missing_place = sum(1 for r in reviews if not r.place_id)
    if missing_place:
        warnings.append(f"{missing_place} review(s) have no place_id")
    off_scale = [
        r.rating for r in reviews if r.rating is not None and not 0 <= r.rating <= 5
    ]
    if off_scale:
        warnings.append(f"{len(off_scale)} rating(s) outside the 0-5 range")
    empty = sum(1 for r in reviews if not _is_present(r, "review_text"))
    if empty == len(reviews):
        warnings.append("no review carries any text")
    return warnings


def render_overall(
    summaries: Sequence[RestaurantSummary],
    warnings: Sequence[str] = (),
) -> str:
    total = sum(s.collected for s in summaries)
    lines = [
        "Overall",
        f"Restaurants processed: {len(summaries)}",
        f"Restaurants with at least one review: {sum(1 for s in summaries if s.collected)}",
        f"Reviews collected: {total}",
        f"Reviews with text: {sum(s.with_text for s in summaries)}",
        f"Reviews with rating: {sum(s.with_rating for s in summaries)}",
        f"Reviews with date: {sum(s.with_date for s in summaries)}",
        f"Duplicate reviews removed: {sum(s.duplicates_removed for s in summaries)}",
        f"Collection failures: {sum(len(s.failures) for s in summaries)}",
    ]
    if total:
        lines.append("")
        lines.append("Field availability across the collected dataset:")
        for name in NORMALIZED_FIELDS:
            count = sum(s.field_coverage.get(name, 0) for s in summaries)
            pct = 100.0 * count / total
            lines.append(f"  {name:<22} {count:>6} / {total}  ({pct:5.1f}%)")
    if warnings:
        lines.append("")
        lines.append("Validation warnings:")
        lines.extend(f"  - {w}" for w in warnings)
    return "\n".join(lines)
