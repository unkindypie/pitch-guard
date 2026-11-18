"""Configuration validation and JSON persistence tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pitch_guard.aggregation import Bucket, History, add_histogram
from pitch_guard.config import (
    DEFAULT_MAX_HZ,
    DEFAULT_MIN_HZ,
    PRESET_CUSTOM,
    PRESET_DEFAULT,
    PRESET_LOW,
    Config,
    ConfigError,
    ConfigStore,
)
from pitch_guard.persistence import PitchGuardStorage
from pitch_guard.storage import (
    CONFIG_SCHEMA_VERSION,
    HISTORY_SCHEMA_VERSION,
    DataPaths,
    atomic_write_json,
    read_json_file,
    schema_version,
)
from tests.conftest import BASE_TIME, make_sample


def test_config_defaults_match_requirements() -> None:
    config = Config()
    assert config.min_hz == DEFAULT_MIN_HZ == 170.0
    assert config.max_hz == DEFAULT_MAX_HZ == 250.0
    assert config.alert_duration_s == 3.0
    assert config.idle_timeout_s == 60.0
    assert config.preset == PRESET_DEFAULT


def test_config_validation_and_custom_preset() -> None:
    config = Config(min_hz=180.0, max_hz=260.0).validate()
    assert config.preset == PRESET_CUSTOM
    with pytest.raises(ConfigError):
        Config(min_hz=250.0, max_hz=170.0).validate()
    with pytest.raises(ConfigError):
        Config(min_hz=20.0, max_hz=250.0).validate()
    with pytest.raises(ConfigError):
        Config(alert_duration_s=0.1).validate()
    with pytest.raises(ConfigError):
        Config(alert_cooldown_s=-1.0).validate()
    with pytest.raises(ConfigError):
        Config(volume=101).validate()
    with pytest.raises(ConfigError):
        Config(idle_timeout_s=-1.0).validate()
    with pytest.raises(ConfigError):
        Config(idle_timeout_s=3601.0).validate()
    assert Config(idle_timeout_s=0.0).validate().idle_timeout_s == 0.0


def test_named_presets_and_microphone_normalisation() -> None:
    config = Config().with_preset(PRESET_LOW)
    assert (config.min_hz, config.max_hz) == (100.0, 180.0)
    assert config.validate().preset == PRESET_LOW
    whitespace = Config(microphone="   ").validate()
    assert whitespace.microphone is None


def test_config_roundtrip_atomically(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    config = Config(
        preset="custom",
        min_hz=175.0,
        max_hz=260.0,
        volume=55,
        idle_timeout_s=45.0,
    )
    store.save(config)
    assert not list(tmp_path.glob("*.tmp"))
    loaded = store.load()
    assert loaded == config
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == CONFIG_SCHEMA_VERSION


def test_missing_config_uses_defaults(tmp_path: Path) -> None:
    assert ConfigStore(tmp_path / "missing.json").load() == Config()


def test_malformed_config_is_preserved_and_defaults_are_used(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    loaded = ConfigStore(path).load()
    assert loaded == Config()
    assert path.with_suffix(".json.corrupt").exists()


def test_outdated_config_schema_uses_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    atomic_write_json(
        path,
        {"schema_version": CONFIG_SCHEMA_VERSION + 1, "min_hz": 100.0, "max_hz": 200.0},
    )
    assert ConfigStore(path).load() == Config()


def test_config_with_invalid_values_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    atomic_write_json(
        path,
        {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "preset": "default",
            "min_hz": 300.0,
            "max_hz": 200.0,
            "alert_duration_s": 3.0,
            "alert_cooldown_s": 10.0,
            "volume": 70,
        },
    )
    assert ConfigStore(path).load() == Config()


def test_atomic_json_write_and_read(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "data.json"
    atomic_write_json(path, {"a": 1, "unicode": "·"})
    assert read_json_file(path) == {"a": 1, "unicode": "·"}
    assert schema_version({"schema_version": 3}) == 3
    assert schema_version([]) is None


def test_read_missing_json_returns_none(tmp_path: Path) -> None:
    assert read_json_file(tmp_path / "nope.json") is None


def make_bucket(start: datetime) -> Bucket:
    item = Bucket(
        start=start,
        monitoring_s=120.0,
        voiced_s=60.0,
        in_range_s=45.0,
        out_of_range_s=15.0,
        alerts=1,
        min_hz=170.0,
        max_hz=250.0,
    )
    add_histogram(item.bins, 200.0, 60.0)
    return item


def test_history_roundtrip_including_open_bucket(tmp_path: Path) -> None:
    paths = DataPaths.from_root(tmp_path / ".pitch-guard")
    storage = PitchGuardStorage(paths)
    now = BASE_TIME
    history = History([make_bucket(BASE_TIME - timedelta(hours=2))], now=now)
    history.add_sample(make_sample(0.0, 200.0, timestamp=BASE_TIME))
    storage.save_history(history, now)
    assert paths.history.exists()
    assert paths.daily_stats.exists()
    loaded = storage.load_history(now)
    totals = loaded.query(BASE_TIME - timedelta(hours=3), BASE_TIME + timedelta(minutes=5), now)
    assert totals.voiced_s >= 60.0
    assert loaded.open_bucket is not None


def test_history_schema_and_malformed_recovery(tmp_path: Path) -> None:
    paths = DataPaths.from_root(tmp_path / ".pitch-guard")
    storage = PitchGuardStorage(paths)
    atomic_write_json(paths.history, {"schema_version": HISTORY_SCHEMA_VERSION + 1, "buckets": []})
    assert storage.load_history(BASE_TIME).snapshot(BASE_TIME) == []
    paths.history.write_text("broken", encoding="utf-8")
    assert storage.load_history(BASE_TIME).snapshot(BASE_TIME) == []


def test_history_skips_invalid_bucket_entries(tmp_path: Path) -> None:
    paths = DataPaths.from_root(tmp_path / ".pitch-guard")
    storage = PitchGuardStorage(paths)
    atomic_write_json(
        paths.history,
        {
            "schema_version": HISTORY_SCHEMA_VERSION,
            "buckets": [
                {"start": "not-a-date"},
                {"start": BASE_TIME.isoformat(), "monitoring": -4, "bins": {"bad": 3}},
                make_bucket(BASE_TIME).to_json(),
            ],
        },
    )
    loaded = storage.load_history(BASE_TIME)
    assert len(loaded.snapshot(BASE_TIME)) == 1


def test_daily_stats_roundtrip(tmp_path: Path) -> None:
    paths = DataPaths.from_root(tmp_path / ".pitch-guard")
    storage = PitchGuardStorage(paths)
    history = History([make_bucket(BASE_TIME - timedelta(hours=1))], now=BASE_TIME)
    storage.save_history(history, BASE_TIME + timedelta(minutes=1))
    summaries = storage.load_daily_summaries()
    assert summaries
    assert summaries[0].voiced_s > 0


def test_disabled_storage_writes_nothing(tmp_path: Path) -> None:
    paths = DataPaths.from_root(tmp_path / ".pitch-guard")
    storage = PitchGuardStorage(paths, enabled=False)
    storage.save_config(Config())
    storage.save_history(History(now=BASE_TIME), BASE_TIME)
    assert not paths.config.exists()
    assert not paths.history.exists()


def test_retention_is_bounded(tmp_path: Path) -> None:
    paths = DataPaths.from_root(tmp_path / ".pitch-guard")
    storage = PitchGuardStorage(paths)
    now = datetime.now(UTC)
    history = History(
        [make_bucket(now - timedelta(days=400)), make_bucket(now - timedelta(hours=1))],
        now=now,
    )
    storage.save_history(history, now)
    loaded = storage.load_history(now)
    assert len(loaded.snapshot(now)) == 1
