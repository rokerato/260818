"""Orchestration: collect -> archive raw -> normalize -> validate -> summarize."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

from .collectors.base import BaseCollector, CollectionResult, PlaceTarget, RawReview
from .collectors.naver import NaverPlaceCollector
from .config import Config
from .normalize.naver_mapper import MAPPERS
from .normalize.schema import NormalizedReview
from .storage import (
    iter_raw_files,
    load_raw,
    run_stamp,
    slugify,
    write_normalized_csv,
    write_raw,
    write_summary_json,
)
from .validation import (
    RestaurantSummary,
    render_overall,
    summarize_restaurant,
    validate_dataset,
)

log = logging.getLogger(__name__)

#: Register a new platform here; nothing else in the pipeline changes.
COLLECTORS: dict[str, type[BaseCollector]] = {"naver": NaverPlaceCollector}


def build_collector(platform: str, config: Config) -> BaseCollector:
    try:
        collector_cls = COLLECTORS[platform]
    except KeyError as exc:
        raise ValueError(
            f"no collector registered for platform {platform!r}; "
            f"known platforms: {sorted(COLLECTORS)}"
        ) from exc
    settings = config.settings
    kwargs = {
        "max_reviews": settings.max_reviews_per_restaurant,
        "request_delay": settings.request_delay_seconds,
        "page_timeout_ms": settings.page_timeout_ms,
        "max_empty_rounds": settings.max_empty_rounds,
        "headless": settings.headless,
        "browser_executable": settings.browser_executable,
        "locale": settings.locale,
        "user_agent": settings.user_agent,
    }
    if settings.base_url:
        kwargs["base_url"] = settings.base_url
    if settings.debug_dir:
        kwargs["debug_dir"] = settings.debug_dir
    return collector_cls(**kwargs)


def normalize_result(
    result: CollectionResult, raw_stem: str | None
) -> list[NormalizedReview]:
    mapper = MAPPERS.get(result.target.platform)
    if mapper is None:
        log.error("no mapper for platform %r; skipping", result.target.platform)
        return []
    normalized: list[NormalizedReview] = []
    for index, raw in enumerate(result.reviews):
        try:
            normalized.append(
                mapper(raw, f"{raw_stem}#{index}" if raw_stem else None)
            )
        except Exception as exc:  # noqa: BLE001 - one bad payload must not stop the run
            result.record_failure(f"normalization failed for review {index}: {exc!r}")
    return normalized


def run(
    config: Config, *, csv_name: str | None = None
) -> tuple[list[RestaurantSummary], Path | None, list[str]]:
    """Collect every configured target and write the run's artifacts."""
    settings = config.settings
    stamp = run_stamp()
    summaries: list[RestaurantSummary] = []
    all_reviews: list[NormalizedReview] = []

    by_platform: dict[str, list[PlaceTarget]] = {}
    for target in config.targets:
        by_platform.setdefault(target.platform, []).append(target)

    for platform, targets in by_platform.items():
        try:
            collector = build_collector(platform, config)
        except ValueError as exc:
            log.error("%s", exc)
            for target in targets:
                failed = CollectionResult(target=target)
                failed.record_failure(str(exc))
                summaries.append(summarize_restaurant(failed, []))
            continue

        for result in collector.collect_many(targets):
            raw_path = write_raw(result, Path(settings.raw_dir), stamp)
            reviews = normalize_result(result, raw_path.stem)
            all_reviews.extend(reviews)
            summaries.append(summarize_restaurant(result, reviews))

    csv_path: Path | None = None
    if all_reviews:
        name = csv_name or f"reviews_{stamp}.csv"
        csv_path = write_normalized_csv(
            all_reviews, Path(settings.normalized_dir) / name, settings.csv_encoding
        )

    warnings = validate_dataset(all_reviews)
    write_summary_json(
        {
            "run": stamp,
            "restaurants": [s.to_dict() for s in summaries],
            "normalized_csv": str(csv_path) if csv_path else None,
            "validation_warnings": warnings,
        },
        Path(settings.normalized_dir) / f"summary_{stamp}.json",
    )
    return summaries, csv_path, warnings


def replay(config: Config, raw_dir: Path, csv_name: str | None = None) -> Path | None:
    """Re-normalize archived raw payloads without touching the network.

    This is the payoff of keeping the collector isolated from the schema: a
    schema change is re-applied to data already on disk.
    """
    all_reviews: list[NormalizedReview] = []
    for path in iter_raw_files(raw_dir):
        document = load_raw(path)
        platform = document.get("platform", "naver")
        mapper = MAPPERS.get(platform)
        if mapper is None:
            log.error("no mapper for platform %r in %s", platform, path)
            continue
        for index, entry in enumerate(document.get("reviews", [])):
            try:
                all_reviews.append(
                    mapper(RawReview.from_dict(entry), f"{path.stem}#{index}")
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("replay failed for %s#%d: %r", path.stem, index, exc)
    if not all_reviews:
        return None
    name = csv_name or f"reviews_replay_{run_stamp()}.csv"
    return write_normalized_csv(
        all_reviews,
        Path(config.settings.normalized_dir) / name,
        config.settings.csv_encoding,
    )


def print_report(summaries: Sequence[RestaurantSummary], warnings: Sequence[str]) -> None:
    for summary in summaries:
        print()
        print(summary.render())
    print()
    print("=" * 60)
    print(render_overall(summaries, warnings))
