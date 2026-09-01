"""Map raw Naver Place payloads onto :class:`NormalizedReview`.

This is the only module that knows both a platform's field names and the
normalized schema. The collector stays ignorant of the schema, and the schema
stays ignorant of Naver -- so a second platform is a new mapper, not a rewrite.

The alias tables below list field names *observed on Naver visitor-review
payloads*. A key that is absent yields ``None``: no defaulting, no inference.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from ..collectors.base import RawReview
from .schema import NormalizedReview

# --- alias tables -----------------------------------------------------------
_REVIEW_ID = ("id", "reviewId", "seq")
_AUTHOR_CONTAINER = ("author", "user", "writer", "reviewer")
_AUTHOR_NAME = ("nickname", "name", "displayName", "userName")
_AUTHOR_ID = ("id", "objectId", "userIdno", "userId", "encodedId")
_AUTHOR_URL = ("url", "profileUrl", "href", "link")
_RATING = ("rating", "score", "starRating", "starScore")
_TEXT = ("body", "content", "reviewText", "text", "reviewBody")
_CREATED = ("created", "createdAt", "createdDateTime", "reviewCreatedDate", "date")
_VISITED = (
    "visited",
    "visitedAt",
    "visitDate",
    "representativeVisitDateTime",
    "visitDateTime",
)
_VISIT_COUNT = ("visitCount", "visitCountText", "visitTimes")
_MEDIA = ("media", "thumbnails", "images", "photos")
_MEDIA_URL = ("url", "imageUrl", "thumbnail", "src", "originalUrl")
_HELPFUL_CONTAINER = ("reactionStat", "reaction", "reactionStats")
_HELPFUL = ("totalCount", "count", "helpCount", "helpfulCount", "likeCount", "thumbsUp")
_KEYWORDS = ("votedKeywords", "keywords", "tags", "reviewKeywords")
_KEYWORD_NAME = ("displayName", "name", "keyword", "text", "code")
_MENUS = ("menus", "menu", "purchasedItems", "items", "usedMenus")
_MENU_NAME = ("name", "menuName", "displayName", "title")

#: Values of ``originType``/``visitConfirmType`` that Naver surfaces as a
#: visit-verified badge (receipt, card, or booking confirmation).
_VERIFIED_TOKENS = frozenset(
    {"receipt", "card", "booking", "reservation", "pay", "payment", "visit"}
)
_VERIFIED_KEYS = (
    "originType",
    "visitConfirmType",
    "receiptChecked",
    "isReceipt",
    "visited",
    "visitConfirmed",
)

_ISO_DATE = re.compile(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})")
_SHORT_DATE = re.compile(r"^(\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})")
_DIGITS = re.compile(r"\d+")


# --- small helpers ----------------------------------------------------------
def _first(payload: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, "", [], {}):
            return payload[key]
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value)
        if match:
            return float(match.group())
    return None


def _as_int(value: Any) -> int | None:
    """Extract a count. Handles Naver's ``"3번째 방문"`` style strings."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        match = _DIGITS.search(value)
        if match:
            return int(match.group())
    return None


def normalize_date(value: Any) -> str | None:
    """Return ``YYYY-MM-DD`` when parseable, else the original text.

    Naver mixes ISO datetimes (``2024-05-03T10:11:12``), plain dates and the
    compact Korean list format (``24.5.3.금``). Anything unrecognised is kept
    verbatim rather than dropped or guessed at.
    """
    text = _as_str(value)
    if not text:
        return None
    match = _ISO_DATE.search(text)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    match = _SHORT_DATE.match(text)
    if match:
        year, month, day = match.groups()
        return f"20{int(year):02d}-{int(month):02d}-{int(day):02d}"
    return text


def _names_from(container: Any, keys: Iterable[str]) -> list[str]:
    """Pull display names out of a list of strings or of objects."""
    out: list[str] = []
    if isinstance(container, dict):
        container = container.get("items", container.get("list", []))
    if not isinstance(container, list):
        return out
    for entry in container:
        if isinstance(entry, str):
            name = entry.strip()
        elif isinstance(entry, dict):
            name = _as_str(_first(entry, tuple(keys)))
        else:
            name = None
        if name and name not in out:
            out.append(name)
    return out


def _image_urls(payload: dict[str, Any]) -> list[str]:
    container = _first(payload, _MEDIA)
    urls: list[str] = []
    if isinstance(container, dict):
        container = container.get("items", [])
    if isinstance(container, list):
        for entry in container:
            if isinstance(entry, str):
                url = entry.strip()
            elif isinstance(entry, dict):
                url = _as_str(_first(entry, _MEDIA_URL))
            else:
                url = None
            if url and url not in urls:
                urls.append(url)
    return urls


def _verified_visit(payload: dict[str, Any]) -> bool | None:
    """``True``/``False`` only when Naver actually exposes a visit signal."""
    found_signal = False
    for key in _VERIFIED_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool):
            found_signal = True
            if value:
                return True
        elif isinstance(value, str) and value.strip():
            found_signal = True
            lowered = value.lower()
            if any(token in lowered for token in _VERIFIED_TOKENS):
                return True
    return False if found_signal else None


def _helpful_count(payload: dict[str, Any]) -> int | None:
    container = _first(payload, _HELPFUL_CONTAINER)
    if isinstance(container, dict):
        value = _as_int(_first(container, _HELPFUL))
        if value is not None:
            return value
    return _as_int(_first(payload, _HELPFUL))


# --- public API -------------------------------------------------------------
def map_review(raw: RawReview, raw_ref: str | None = None) -> NormalizedReview:
    """Translate one raw Naver review into the normalized schema."""
    payload = raw.payload or {}
    author = _first(payload, _AUTHOR_CONTAINER)
    author = author if isinstance(author, dict) else {}

    images = _image_urls(payload)
    # Prefer an explicit count from the platform; otherwise count what we saw.
    image_count = _as_int(payload.get("mediaCount"))
    if image_count is None:
        image_count = len(images) if images else (0 if _first(payload, _MEDIA) is not None else None)

    return NormalizedReview(
        platform=raw.platform,
        place_name=raw.place_name,
        place_id=raw.place_id,
        place_url=raw.place_url,
        review_id=_as_str(_first(payload, _REVIEW_ID)) or raw.native_id,
        reviewer_name=_as_str(_first(author, _AUTHOR_NAME))
        or _as_str(payload.get("authorName")),
        reviewer_profile_id=_as_str(_first(author, _AUTHOR_ID))
        or _as_str(payload.get("userIdno")),
        reviewer_profile_url=_as_str(_first(author, _AUTHOR_URL)),
        rating=_as_float(_first(payload, _RATING)),
        review_text=_as_str(_first(payload, _TEXT)),
        review_date=normalize_date(_first(payload, _CREATED)),
        visit_date=normalize_date(_first(payload, _VISITED)),
        visit_count=_as_int(_first(payload, _VISIT_COUNT)),
        review_image_count=image_count,
        review_image_urls=images,
        helpful_count=_helpful_count(payload),
        keywords=_names_from(_first(payload, _KEYWORDS), _KEYWORD_NAME),
        menus=_names_from(_first(payload, _MENUS), _MENU_NAME),
        verified_visit=_verified_visit(payload),
        collected_at=raw.fetched_at,
        raw_ref=raw_ref,
    )


def map_reviews(
    raws: Iterable[RawReview], raw_file_stem: str | None = None
) -> list[NormalizedReview]:
    return [
        map_review(raw, f"{raw_file_stem}#{index}" if raw_file_stem else None)
        for index, raw in enumerate(raws)
    ]


#: Registry consulted by the pipeline; a new platform registers its mapper here.
MAPPERS = {"naver": map_review}
