"""High-level loading and saving of configuration and historical statistics."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pitch_guard.aggregation import Bucket, DailySummary, History
from pitch_guard.config import Config, ConfigError
from pitch_guard.storage import (
    DAILY_SCHEMA_VERSION,
    HISTORY_SCHEMA_VERSION,
    DataPaths,
    atomic_write_json,
    read_json_file,
    schema_version,
)

LOG = logging.getLogger("pitch_guard.persistence")


class PitchGuardStorage:
    """Own the JSON files under ``~/.pitch-guard``."""

    def __init__(
        self, paths: DataPaths, logger: logging.Logger | None = None, *, enabled: bool = True
    ) -> None:
        self.paths = paths
        self.log = logger or LOG
        self.enabled = enabled
        if enabled:
            self.paths.ensure_directories()

    def load_config(self) -> Config:
        """Load configuration with safe defaults for missing/broken files."""
        from pitch_guard.config import ConfigStore

        return ConfigStore(self.paths.config, self.log).load()

    def save_config(self, config: Config) -> None:
        """Persist configuration when storage is enabled."""
        if not self.enabled:
            return
        try:
            from pitch_guard.config import ConfigStore

            ConfigStore(self.paths.config, self.log).save(config)
        except (OSError, ConfigError) as error:
            self.log.warning("Could not save configuration: %s", error)

    def load_history(self, now: datetime | None = None) -> History:
        """Load historical buckets, skipping malformed entries."""
        moment = now or datetime.now(UTC)
        if not self.enabled:
            return History(now=moment)
        payload = read_json_file(self.paths.history, self.log)
        if payload is None:
            return History(now=moment)
        version = schema_version(payload)
        if version is None or version > HISTORY_SCHEMA_VERSION:
            self.log.warning("Unsupported history schema %s; starting fresh", version)
            return History(now=moment)
        raw_buckets = payload.get("buckets") if isinstance(payload, dict) else None
        buckets: list[Bucket] = []
        if isinstance(raw_buckets, list):
            for entry in raw_buckets:
                bucket = Bucket.from_json(entry)
                if bucket is None:
                    continue
                if (
                    bucket.monitoring_s <= 0
                    and bucket.voiced_s <= 0
                    and bucket.alerts <= 0
                    and not bucket.bins
                ):
                    continue
                buckets.append(bucket)
        return History(buckets, now=moment)

    def save_history(self, history: History, now: datetime | None = None) -> None:
        """Compact, serialise and atomically write history and daily rollups."""
        if not self.enabled:
            return
        moment = now or datetime.now(UTC)
        try:
            history.compact(moment)
            buckets = history.snapshot(moment)
            atomic_write_json(
                self.paths.history,
                {
                    "schema_version": HISTORY_SCHEMA_VERSION,
                    "buckets": [bucket.to_json() for bucket in buckets],
                },
            )
            summaries = history.daily_summaries(moment)
            self.save_daily_summaries(summaries)
        except OSError as error:
            self.log.warning("Could not save history: %s", error)

    def save_daily_summaries(self, summaries: list[DailySummary]) -> None:
        """Persist daily rollups used for streaks and personal bests."""
        if not self.enabled:
            return
        try:
            atomic_write_json(
                self.paths.daily_stats,
                {
                    "schema_version": DAILY_SCHEMA_VERSION,
                    "days": [summary.to_json() for summary in summaries],
                },
            )
        except OSError as error:
            self.log.warning("Could not save daily statistics: %s", error)

    def load_daily_summaries(self) -> list[DailySummary]:
        """Load cached daily rollups; callers can recompute when empty."""
        if not self.enabled:
            return []
        payload = read_json_file(self.paths.daily_stats, self.log)
        if not isinstance(payload, dict) or schema_version(payload) != DAILY_SCHEMA_VERSION:
            return []
        raw_days = payload.get("days")
        if not isinstance(raw_days, list):
            return []
        summaries: list[DailySummary] = []
        for entry in raw_days:
            summary = DailySummary.from_json(entry)
            if summary is not None:
                summaries.append(summary)
        return summaries
