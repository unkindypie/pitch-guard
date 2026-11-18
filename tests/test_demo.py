"""Demo input and seeded-history tests."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from pitch_guard.audio.demo import DemoPitchSource
from pitch_guard.audio.demo_data import seed_demo_history
from pitch_guard.config import Config
from pitch_guard.models import PitchKind


def test_demo_scenario_covers_all_ranges() -> None:
    source = DemoPitchSource()
    silent_kind, silent_hz = source._scenario(0.5)
    in_range_kind, in_range_hz = source._scenario(5.0)
    low_kind, low_hz = source._scenario(21.0)
    high_kind, high_hz = source._scenario(37.0)
    assert silent_kind is PitchKind.SILENT and silent_hz is None
    assert in_range_kind is PitchKind.VOICED and 170.0 <= (in_range_hz or 0) <= 250.0
    assert low_kind is PitchKind.VOICED and (low_hz or 0) < 170.0
    assert high_kind is PitchKind.VOICED and (high_hz or 0) > 250.0


def test_demo_source_produces_measurements() -> None:
    source = DemoPitchSource(frame_duration_s=0.002)
    source.start()
    time.sleep(0.08)
    source.stop()
    measurements = source.drain()
    assert measurements
    assert all(item.duration_s == 0.002 for item in measurements)
    assert all(item.timestamp.tzinfo is not None for item in measurements)


def test_demo_history_covers_90_days_and_is_bounded() -> None:
    now = datetime.now(UTC)
    history = seed_demo_history(Config(), now)
    buckets = history.snapshot(now)
    assert len(buckets) > 500
    assert all((now - bucket.start).days <= 91 for bucket in buckets)
    summaries = history.daily_summaries(now, days=90)
    assert len(summaries) > 60
    assert any(summary.in_range_pct and summary.in_range_pct > 50 for summary in summaries)


def test_demo_history_is_in_memory_only() -> None:
    history = seed_demo_history(Config(), datetime.now(UTC))
    assert history.open_bucket is not None or history.snapshot(datetime.now(UTC))
