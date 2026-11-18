"""Historical aggregation, compaction and daily-statistics tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from pitch_guard.aggregation import (
    BUCKET_RESOLUTION_S,
    Bucket,
    DailySummary,
    History,
    add_histogram,
    floor_to_resolution,
    histogram_mean,
    histogram_median,
    local_day_bounds,
    local_timezone,
    merge_histograms,
)
from tests.conftest import BASE_TIME, make_sample


def bucket(
    start: datetime,
    *,
    resolution: int = 300,
    monitoring: float = 300.0,
    voiced: float = 120.0,
    in_range: float = 90.0,
    out_of_range: float = 30.0,
    alerts: int = 1,
    hz: float = 200.0,
    min_hz: float = 170.0,
    max_hz: float = 250.0,
) -> Bucket:
    item = Bucket(
        start=start,
        resolution_s=resolution,
        monitoring_s=monitoring,
        voiced_s=voiced,
        in_range_s=in_range,
        out_of_range_s=out_of_range,
        alerts=alerts,
        min_hz=min_hz,
        max_hz=max_hz,
    )
    add_histogram(item.bins, hz, voiced)
    return item


def test_histogram_mean_and_median_are_time_weighted() -> None:
    histogram: dict[float, float] = {}
    add_histogram(histogram, 190.0, 1.0)
    add_histogram(histogram, 210.0, 3.0)
    assert histogram_mean(histogram) == pytest.approx(205.0)
    assert histogram_median(histogram) == pytest.approx(210.0)
    assert histogram_mean({}) is None
    assert histogram_median({}) is None


def test_merge_histograms_and_bucket_totals() -> None:
    first = bucket(BASE_TIME, hz=190.0)
    second = bucket(BASE_TIME, hz=230.0, min_hz=180.0, max_hz=260.0)
    first.merge(second)
    assert first.voiced_s == pytest.approx(240.0)
    assert first.totals.avg_hz == pytest.approx(210.0)
    assert first.totals.in_range_pct == pytest.approx(75.0)
    assert first.min_hz is None  # mixed ranges are represented by totals only


def test_bucket_adds_samples_with_status_weighting() -> None:
    item = Bucket(start=BASE_TIME)
    item.add_sample(make_sample(0.0, 200.0, duration_s=0.1))
    item.add_sample(make_sample(0.1, 150.0, duration_s=0.1))
    item.add_sample(make_sample(0.2, None, duration_s=0.1))
    assert item.monitoring_s == pytest.approx(0.3)
    assert item.voiced_s == pytest.approx(0.2)
    assert item.in_range_s == pytest.approx(0.1)
    assert item.out_of_range_s == pytest.approx(0.1)


def test_history_rolls_over_buckets_and_keeps_open_data() -> None:
    history = History(now=BASE_TIME)
    history.add_sample(make_sample(0.0, 200.0, timestamp=BASE_TIME))
    history.add_sample(make_sample(301.0, 200.0, timestamp=BASE_TIME + timedelta(seconds=301)))
    buckets = history.snapshot(BASE_TIME + timedelta(seconds=302))
    assert len(buckets) == 2
    assert buckets[0].resolution_s == BUCKET_RESOLUTION_S
    assert history.open_bucket is not None


def test_query_prorates_partial_buckets() -> None:
    history = History([bucket(BASE_TIME)], now=BASE_TIME)
    totals = history.query(BASE_TIME, BASE_TIME + timedelta(seconds=150), BASE_TIME)
    assert totals.monitoring_s == pytest.approx(150.0)
    assert totals.voiced_s == pytest.approx(60.0)
    assert totals.in_range_s == pytest.approx(45.0)
    assert totals.alerts == pytest.approx(0.5)


def test_query_combines_buckets_and_empty_period_is_na() -> None:
    history = History(
        [bucket(BASE_TIME), bucket(BASE_TIME + timedelta(seconds=300))],
        now=BASE_TIME,
    )
    totals = history.query(BASE_TIME, BASE_TIME + timedelta(seconds=600), BASE_TIME)
    assert totals.monitoring_s == pytest.approx(600.0)
    assert totals.voiced_s == pytest.approx(240.0)
    assert totals.in_range_pct == pytest.approx(75.0)
    empty = history.query(BASE_TIME + timedelta(days=2), BASE_TIME + timedelta(days=3), BASE_TIME)
    assert empty.avg_hz is None
    assert empty.in_range_pct is None


def test_old_buckets_are_compacted_to_hourly() -> None:
    now = BASE_TIME + timedelta(days=3)
    first = bucket(now - timedelta(days=2, minutes=50))
    second = bucket(now - timedelta(days=2, minutes=45))
    history = History([first, second], now=now)
    history.compact(now)
    buckets = history.snapshot(now)
    old = [item for item in buckets if item.start < now - timedelta(days=1)]
    assert old
    assert all(item.resolution_s == 3600 for item in old)
    assert sum(item.voiced_s for item in old) == pytest.approx(240.0)


def test_retention_drops_expired_buckets() -> None:
    history = History([bucket(BASE_TIME - timedelta(days=200))], now=BASE_TIME)
    assert history.snapshot(BASE_TIME) == []


def test_loaded_current_bucket_is_reused_as_open() -> None:
    current_start = floor_to_resolution(BASE_TIME, BUCKET_RESOLUTION_S)
    loaded = bucket(current_start, monitoring=100.0)
    history = History([loaded], now=BASE_TIME)
    assert history.open_bucket is not None
    assert history.open_bucket.monitoring_s == pytest.approx(100.0)
    history.add_sample(make_sample(0.0, 200.0, timestamp=BASE_TIME))
    assert history.open_bucket.monitoring_s == pytest.approx(100.1)


def test_daily_summaries_use_local_day_boundaries() -> None:
    timezone = local_timezone()
    today = (BASE_TIME + timedelta(days=1)).date()
    start, _end = local_day_bounds(today, timezone)
    history = History([bucket(start + timedelta(hours=2))], now=start + timedelta(hours=3))
    summaries = history.daily_summaries(start + timedelta(hours=3), days=2)
    assert any(summary.day == today for summary in summaries)


def test_streak_and_personal_best() -> None:
    today = date(2026, 1, 15)
    summaries = [
        DailySummary(today - timedelta(days=2), 100.0, 120.0, 90.0, 30.0, 1, 200, 200, 75.0),
        DailySummary(today - timedelta(days=1), 100.0, 120.0, 96.0, 24.0, 0, 200, 200, 80.0),
        DailySummary(today, 100.0, 120.0, 90.0, 30.0, 1, 200, 200, 75.0),
        DailySummary(today - timedelta(days=4), 10.0, 5.0, 5.0, 0.0, 0, 200, 200, 100.0),
    ]
    history = History(now=BASE_TIME)
    streak, best = history.streak_and_best(summaries, today)
    assert streak == 3
    assert best is not None
    assert best.day == today - timedelta(days=1)
    assert best.in_range_pct == pytest.approx(80.0)


def test_streak_starts_yesterday_when_today_has_too_little_speech() -> None:
    today = date(2026, 1, 15)
    summaries = [
        DailySummary(today - timedelta(days=1), 100.0, 120.0, 90.0, 30.0, 0, 200, 200, 75.0),
        DailySummary(today - timedelta(days=2), 100.0, 120.0, 90.0, 30.0, 0, 200, 200, 75.0),
        DailySummary(today, 100.0, 10.0, 5.0, 5.0, 0, 200, 200, 50.0),
    ]
    streak, _ = History(now=BASE_TIME).streak_and_best(summaries, today)
    assert streak == 2


def test_merge_histograms_ignores_non_positive_values() -> None:
    target = {200.0: 1.0}
    merge_histograms(target, {200.0: 2.0, 220.0: 0.0})
    assert target == {200.0: 3.0}
