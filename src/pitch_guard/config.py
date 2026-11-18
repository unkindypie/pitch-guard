"""Configuration loading, validation and automatic persistence."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Final

from pitch_guard.storage import (
    CONFIG_SCHEMA_VERSION,
    atomic_write_json,
    read_json_file,
    schema_version,
)

DEFAULT_MIN_HZ: Final[float] = 170.0
DEFAULT_MAX_HZ: Final[float] = 250.0
DEFAULT_ALERT_DURATION_S: Final[float] = 3.0
DEFAULT_ALERT_COOLDOWN_S: Final[float] = 10.0
DEFAULT_VOLUME: Final[int] = 70

PITCH_FLOOR_HZ: Final[float] = 50.0
PITCH_CEILING_HZ: Final[float] = 600.0
MIN_ALERT_DURATION_S: Final[float] = 0.5
MAX_ALERT_DURATION_S: Final[float] = 60.0
MIN_ALERT_COOLDOWN_S: Final[float] = 0.0
MAX_ALERT_COOLDOWN_S: Final[float] = 600.0

PRESET_DEFAULT: Final[str] = "default"
PRESET_LOW: Final[str] = "low"
PRESET_HIGH: Final[str] = "high"
PRESET_CUSTOM: Final[str] = "custom"

PITCH_PRESETS: Final[dict[str, tuple[float, float]]] = {
    PRESET_DEFAULT: (DEFAULT_MIN_HZ, DEFAULT_MAX_HZ),
    PRESET_LOW: (100.0, 180.0),
    PRESET_HIGH: (220.0, 330.0),
}

PRESET_LABELS: Final[dict[str, str]] = {
    PRESET_DEFAULT: "Default 170-250 Hz",
    PRESET_LOW: "Lower 100-180 Hz",
    PRESET_HIGH: "Higher 220-330 Hz",
    PRESET_CUSTOM: "Custom range",
}

LOG = logging.getLogger("pitch_guard.config")


class ConfigError(ValueError):
    """Raised when a settings value is outside its supported range."""


@dataclass(slots=True)
class Config:
    """User-visible settings, persisted automatically to ``config.json``."""

    preset: str = PRESET_DEFAULT
    min_hz: float = DEFAULT_MIN_HZ
    max_hz: float = DEFAULT_MAX_HZ
    alert_duration_s: float = DEFAULT_ALERT_DURATION_S
    alert_cooldown_s: float = DEFAULT_ALERT_COOLDOWN_S
    microphone: str | None = None
    volume: int = DEFAULT_VOLUME

    def range_label(self) -> str:
        """Compact target-range label used by the dashboard."""
        return f"{self.min_hz:.0f}-{self.max_hz:.0f} Hz"

    def validate(self) -> Config:
        """Return a normalised copy or raise :class:`ConfigError`."""
        min_hz = _finite_float(self.min_hz, "Minimum pitch")
        max_hz = _finite_float(self.max_hz, "Maximum pitch")
        duration = _finite_float(self.alert_duration_s, "Alert duration")
        cooldown = _finite_float(self.alert_cooldown_s, "Alert cooldown")
        volume = _finite_float(float(self.volume), "Alert volume")

        if not PITCH_FLOOR_HZ <= min_hz <= PITCH_CEILING_HZ:
            raise ConfigError(
                f"Minimum pitch must be between {PITCH_FLOOR_HZ:.0f} and {PITCH_CEILING_HZ:.0f} Hz"
            )
        if not PITCH_FLOOR_HZ <= max_hz <= PITCH_CEILING_HZ:
            raise ConfigError(
                f"Maximum pitch must be between {PITCH_FLOOR_HZ:.0f} and {PITCH_CEILING_HZ:.0f} Hz"
            )
        if min_hz >= max_hz:
            raise ConfigError("Minimum pitch must be lower than maximum pitch")
        if not MIN_ALERT_DURATION_S <= duration <= MAX_ALERT_DURATION_S:
            raise ConfigError(
                f"Alert duration must be between {MIN_ALERT_DURATION_S:g} and "
                f"{MAX_ALERT_DURATION_S:g} seconds"
            )
        if not MIN_ALERT_COOLDOWN_S <= cooldown <= MAX_ALERT_COOLDOWN_S:
            raise ConfigError(
                f"Alert cooldown must be between {MIN_ALERT_COOLDOWN_S:g} and "
                f"{MAX_ALERT_COOLDOWN_S:g} seconds"
            )
        if not 0 <= volume <= 100:
            raise ConfigError("Alert volume must be between 0 and 100")

        preset = self.preset if self.preset in PITCH_PRESETS else PRESET_CUSTOM
        if preset in PITCH_PRESETS:
            preset_min, preset_max = PITCH_PRESETS[preset]
            if min_hz != preset_min or max_hz != preset_max:
                preset = PRESET_CUSTOM
        if preset == PRESET_CUSTOM and (min_hz, max_hz) in PITCH_PRESETS.values():
            preset = next(name for name, pair in PITCH_PRESETS.items() if pair == (min_hz, max_hz))

        microphone = self.microphone.strip() if isinstance(self.microphone, str) else None
        if not microphone:
            microphone = None

        return Config(
            preset=preset,
            min_hz=round(min_hz, 1),
            max_hz=round(max_hz, 1),
            alert_duration_s=round(duration, 1),
            alert_cooldown_s=round(cooldown, 1),
            microphone=microphone,
            volume=round(volume),
        )

    def with_preset(self, preset: str) -> Config:
        """Return a copy using a named preset (or custom without changing Hz)."""
        if preset not in PITCH_PRESETS:
            return replace(self, preset=PRESET_CUSTOM)
        min_hz, max_hz = PITCH_PRESETS[preset]
        return replace(self, preset=preset, min_hz=min_hz, max_hz=max_hz)

    def to_dict(self) -> dict[str, object]:
        """Serialise the configuration for JSON storage."""
        values = asdict(self)
        values["schema_version"] = CONFIG_SCHEMA_VERSION
        return values

    @classmethod
    def from_dict(cls, payload: object) -> Config:
        """Build a configuration from decoded JSON, using per-field defaults."""
        if not isinstance(payload, dict):
            return cls()
        return cls(
            preset=_string(payload.get("preset"), PRESET_DEFAULT),
            min_hz=_number(payload.get("min_hz"), DEFAULT_MIN_HZ),
            max_hz=_number(payload.get("max_hz"), DEFAULT_MAX_HZ),
            alert_duration_s=_number(payload.get("alert_duration_s"), DEFAULT_ALERT_DURATION_S),
            alert_cooldown_s=_number(payload.get("alert_cooldown_s"), DEFAULT_ALERT_COOLDOWN_S),
            microphone=_optional_string(payload.get("microphone")),
            volume=int(_number(payload.get("volume"), DEFAULT_VOLUME)),
        )


class ConfigStore:
    """Load and atomically save configuration."""

    def __init__(self, path: Path, logger: logging.Logger | None = None) -> None:
        self.path = path
        self.log = logger or LOG

    def load(self) -> Config:
        """Load configuration, repairing malformed or outdated files gracefully."""
        payload = read_json_file(self.path, self.log)
        if payload is None:
            return Config()
        version = schema_version(payload)
        if version != CONFIG_SCHEMA_VERSION:
            self.log.warning(
                "Unsupported config schema %s in %s; using defaults", version, self.path
            )
            return Config()
        config = Config.from_dict(payload)
        try:
            return config.validate()
        except ConfigError as error:
            self.log.warning("Invalid configuration in %s (%s); using defaults", self.path, error)
            return Config()

    def save(self, config: Config) -> None:
        """Persist a validated configuration immediately."""
        atomic_write_json(self.path, config.validate().to_dict())


def _finite_float(value: float, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or value != value
        or value in (float("inf"), float("-inf"))
    ):
        raise ConfigError(f"{field} must be a finite number")
    return float(value)


def _number(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _string(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None
