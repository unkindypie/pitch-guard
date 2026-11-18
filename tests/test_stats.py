"""Live-window, period-statistics and improvement tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pitch_guard.aggregation import Bucket, History, add_histogram, local_day_bounds, local_timezone
from pitch_guard.stats import (
    LiveWindow,
    Period,
    StatsEngine,
    format_duration,
    format_hz,
    format_improvement,
    format_pct,
    period_window,
)
from tests.conftest import make_sample

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def make_bucket(
    start: datetime,
    *,
    voiced: float = 120.0,
    in_range: float = 90.0,
    monitoring: float = 300.0,
    hz: float = 200.0,
) -> Bucket:
    item = Bucket(
        start=start,
        monitoring_s=monitoring,
        voiced_s=voiced,
        in_range_s=in_range,
        out_of_range_s=voiced - in_range,
        alerts=1,
    )
    add_histogram(item.bins, hz, voiced)
    return item


def test_period_keys_and_labels() -> None:
    assert Period.from_key("week") is Period.WEEK
    assert Period.from_key("nope") is None
    assert [period.shortcut for period in Period] == ["1", "2", "3", "4", "5"]


def test_period_window_rolling_periods() -> None:
    week = period_window(Period.WEEK, NOW)
    assert week.end == NOW
    assert week.start == NOW - timedelta(days=7)
    assert week.previous_end == week.start
    assert week.previous_start == week.start - timedelta(days=7)
    live = period_window(Period.LIVE, NOW)
    assert live.end - live.start == timedelta(seconds=60)
    ninety = period_window(Period.NINETY_DAYS, NOW)
    assert ninety.end - ninety.start == timedelta(days=90)


def test_period_window_day_uses_local_midnight() -> None:
    day = period_window(Period.DAY, NOW)
    expected_start, expected_end = local_day_bounds(
        NOW.astimezone(local_timezone()).date(), local_timezone()
    )
    assert day.start == expected_start
    assert day.end == NOW
    assert day.previous_start == (expected_start - timedelta(days=1))
    assert expected_start <= day.start < expected_end


def test_live_window_query_and_expiry() -> None:
    window = LiveWindow(window_s=150.0)
    for index in range(200):
        window.add(make_sample(float(index), 200.0, duration_s=1.0))
    # Samples older than 150 seconds behind the newest sample are dropped.
    assert window.sample_times() == (49.0, 199.0)
    totals = window.totals_for(60.0, now=200.0)
    assert totals.voiced_s == pytest.approx(60.0)
    assert totals.in_range_pct == pytest.approx(100.0)
    assert window.current() is not None


def test_live_window_ignores_silence_in_averages() -> None:
    window = LiveWindow()
    window.add(make_sample(0.0, 200.0))
    window.add(make_sample(0.1, 220.0))
    window.add(make_sample(0.2, None))
    totals = window.totals_for(10.0, now=1.0)
    assert totals.monitoring_s == pytest.approx(0.3)
    assert totals.voiced_s == pytest.approx(0.2)
    assert totals.avg_hz == pytest.approx(210.0)
    assert totals.in_range_pct == pytest.approx(100.0)


def test_improvement_compares_percentage_points() -> None:
    history = History(
        [
            make_bucket(NOW - timedelta(days=1), voiced=120.0, in_range=90.0),
            make_bucket(NOW - timedelta(days=8), voiced=120.0, in_range=60.0),
        ],
        now=NOW,
    )
    engine = StatsEngine(history, LiveWindow())
    summary = engine.summary(Period.WEEK, NOW, daily_summaries=[])
    assert summary.in_range_pct == pytest.approx(75.0)
    assert summary.improvement_pp == pytest.approx(25.0)
    assert summary.avg_hz == pytest.approx(200.0)
    assert summary.median_hz == pytest.approx(200.0)
    assert summary.alerts == 1


def test_improvement_is_na_with_insufficient_previous_data() -> None:
    history = History([make_bucket(NOW - timedelta(days=1))], now=NOW)
    summary = StatsEngine(history, LiveWindow()).summary(Period.WEEK, NOW, daily_summaries=[])
    assert summary.improvement_pp is None


def test_empty_statistics_are_na_not_zero() -> None:
    summary = StatsEngine(History(now=NOW), LiveWindow()).summary(
        Period.DAY, NOW, daily_summaries=[]
    )
    assert summary.avg_hz is None
    assert summary.median_hz is None
    assert summary.in_range_pct is None
    assert summary.improvement_pp is None
    assert summary.alerts == 0
    assert format_hz(summary.avg_hz) == "N/A"
    assert format_pct(summary.in_range_pct) == "N/A"
    assert format_improvement(summary.improvement_pp) == "N/A"


def test_live_summary_uses_recent_window() -> None:
    live = LiveWindow()
    for index in range(150):
        moment = 100.0 + index * 0.5
        live.add(make_sample(moment, 200.0 if index < 100 else 260.0, duration_s=0.5))
    engine = StatsEngine(History(now=NOW), live)
    summary = engine.summary(Period.LIVE, NOW, monotonic_now=200.0, daily_summaries=[])
    assert summary.voiced_s > 0
    assert summary.median_hz is not None
    assert summary.period is Period.LIVE


def test_format_helpers() -> None:
    assert format_duration(65) == "1:05"
    assert format_duration(3661) == "1:01:01"
    assert format_duration(None) == "0:00"
    assert format_duration(None, compact=True) == "N/A"
    assert format_hz(218.4) == "218 Hz"
    assert format_hz(None) == "N/A"
    assert format_pct(78.44, decimals=1) == "78.4%"
    assert format_improvement(6.24) == "+6.2 pp"
    assert format_improvement(-3.1) == "-3.1 pp"
    assert format_improvement(0.0) == "0.0 pp"
