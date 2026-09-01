"""Load the restaurant target list and collection settings from YAML.

Adding an Everland restaurant is a config edit -- never a code edit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .collectors.base import PlaceTarget

DEFAULT_CONFIG_PATH = Path("config/restaurants.yaml")

_KNOWN_TARGET_KEYS = {
    "name", "url", "place_id", "platform", "category", "max_reviews", "enabled",
}


@dataclass(slots=True)
class Settings:
    """Global collection knobs, all overridable from the CLI."""

    max_reviews_per_restaurant: int = 200
    request_delay_seconds: float = 1.5
    page_timeout_ms: int = 30_000
    max_empty_rounds: int = 3
    headless: bool = True
    browser_executable: str | None = None
    locale: str = "ko-KR"
    user_agent: str | None = None
    base_url: str | None = None
    raw_dir: Path = Path("data/raw")
    normalized_dir: Path = Path("data/normalized")
    csv_encoding: str = "utf-8"


@dataclass(slots=True)
class Config:
    settings: Settings = field(default_factory=Settings)
    targets: list[PlaceTarget] = field(default_factory=list)


class ConfigError(ValueError):
    """Raised for a malformed or unusable configuration file."""


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if not isinstance(document, dict):
        raise ConfigError(f"{config_path}: top level must be a mapping")

    raw_settings = document.get("settings") or {}
    if not isinstance(raw_settings, dict):
        raise ConfigError(f"{config_path}: 'settings' must be a mapping")

    settings = Settings()
    for key, value in raw_settings.items():
        if not hasattr(settings, key):
            raise ConfigError(f"{config_path}: unknown setting {key!r}")
        if key in ("raw_dir", "normalized_dir"):
            value = Path(value)
        setattr(settings, key, value)

    entries = document.get("restaurants")
    if entries is None:
        raise ConfigError(f"{config_path}: missing 'restaurants' list")
    if not isinstance(entries, list):
        raise ConfigError(f"{config_path}: 'restaurants' must be a list")

    targets: list[PlaceTarget] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"{config_path}: restaurants[{index}] must be a mapping"
            )
        name = entry.get("name")
        if not name:
            raise ConfigError(f"{config_path}: restaurants[{index}] needs a 'name'")
        if entry.get("enabled") is False:
            continue
        if not entry.get("url") and not entry.get("place_id"):
            raise ConfigError(
                f"{config_path}: restaurants[{index}] ({name}) needs a 'url' "
                "or a 'place_id'"
            )
        extra: dict[str, Any] = {
            k: v for k, v in entry.items() if k not in _KNOWN_TARGET_KEYS
        }
        targets.append(
            PlaceTarget(
                name=str(name),
                url=entry.get("url"),
                place_id=(
                    str(entry["place_id"]) if entry.get("place_id") is not None else None
                ),
                platform=str(entry.get("platform", "naver")),
                category=str(entry.get("category", "restaurant")),
                max_reviews=entry.get("max_reviews"),
                extra=extra,
            )
        )

    return Config(settings=settings, targets=targets)
