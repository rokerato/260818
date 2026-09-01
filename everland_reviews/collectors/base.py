"""Collector contract.

A collector's only job is to obtain *raw* review payloads from one platform and
hand them back verbatim. It knows nothing about :mod:`everland_reviews.normalize`
-- that boundary is what lets a second platform be added without touching the
analysis schema, and what lets the raw dump stay faithful to what the platform
actually returned.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

log = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class PlaceTarget:
    """One restaurant/cafe to collect, as declared in the config file."""

    name: str
    url: str | None = None
    place_id: str | None = None
    platform: str = "naver"
    #: Naver serves place pages under a category path segment
    #: (``restaurant``/``cafe``). Only used when a URL has to be constructed.
    category: str = "restaurant"
    max_reviews: int | None = None
    #: Anything extra from the YAML entry, passed through untouched.
    extra: dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        return f"{self.name} ({self.platform}:{self.place_id or self.url})"


@dataclass(slots=True)
class RawReview:
    """A single review exactly as the platform delivered it.

    ``payload`` is stored unmodified so that a later schema change can be
    replayed against already-collected data without re-crawling.
    """

    platform: str
    place_id: str | None
    place_name: str | None
    place_url: str | None
    #: Verbatim platform object (a GraphQL review node, or a DOM extraction).
    payload: dict[str, Any]
    #: Which strategy produced it -- ``"graphql"`` or ``"dom"``.
    source: str
    #: Platform-native identifier, used for de-duplication. ``None`` when the
    #: platform did not expose one; the caller then falls back to a content hash.
    native_id: str | None
    fetched_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RawReview":
        """Rebuild a record from a raw JSON dump, so normalization can be
        replayed offline after a schema change instead of re-crawling."""
        return cls(
            platform=data.get("platform", "unknown"),
            place_id=data.get("place_id"),
            place_name=data.get("place_name"),
            place_url=data.get("place_url"),
            payload=data.get("payload") or {},
            source=data.get("source", "unknown"),
            native_id=data.get("native_id"),
            fetched_at=data.get("fetched_at") or utc_now_iso(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "place_id": self.place_id,
            "place_name": self.place_name,
            "place_url": self.place_url,
            "source": self.source,
            "native_id": self.native_id,
            "fetched_at": self.fetched_at,
            "payload": self.payload,
        }


@dataclass(slots=True)
class CollectionResult:
    """Outcome of collecting one target. Failures are recorded, not raised."""

    target: PlaceTarget
    reviews: list[RawReview] = field(default_factory=list)
    #: Verbatim API/network responses backing ``reviews``, kept for replay.
    raw_responses: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    duplicates_removed: int = 0
    resolved_place_id: str | None = None
    resolved_place_url: str | None = None
    resolved_place_name: str | None = None
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None

    def record_failure(self, message: str) -> None:
        """Log a failure and keep going -- one bad page must not kill the job."""
        log.warning("[%s] %s", self.target.name, message)
        self.failures.append(message)

    @property
    def ok(self) -> bool:
        return bool(self.reviews)


class BaseCollector(abc.ABC):
    """Base class for every platform collector."""

    #: Platform identifier written into every raw record.
    platform: str = "unknown"

    def __init__(
        self,
        *,
        max_reviews: int = 200,
        request_delay: float = 1.5,
        page_timeout_ms: int = 30_000,
        max_empty_rounds: int = 3,
    ) -> None:
        self.max_reviews = max_reviews
        self.request_delay = request_delay
        self.page_timeout_ms = page_timeout_ms
        self.max_empty_rounds = max_empty_rounds

    @abc.abstractmethod
    def collect(self, target: PlaceTarget) -> CollectionResult:
        """Collect up to ``max_reviews`` raw reviews for one target."""

    def collect_many(
        self, targets: Iterable[PlaceTarget]
    ) -> list[CollectionResult]:
        """Collect every target, isolating per-target failures."""
        results: list[CollectionResult] = []
        for target in targets:
            try:
                results.append(self.collect(target))
            except Exception as exc:  # noqa: BLE001 - one target must not kill the job
                log.exception("collector crashed for %s", target.label())
                result = CollectionResult(target=target)
                result.record_failure(f"collector crashed: {exc!r}")
                result.finished_at = utc_now_iso()
                results.append(result)
        return results
