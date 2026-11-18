"""Chart series, axis and widget fallback tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from textual.app import App, ComposeResult

from pitch_guard.aggregation import Bucket, add_histogram
from pitch_guard.charts import (
    ChartPoint,
    build_history_series,
    build_live_series,
    chart_end_label,
    chart_start_label,
    nice_bounds,
    nice_ticks,
)
from pitch_guard.stats import Period
from pitch_guard.ui.chart import ChartData, PitchChart
from tests.conftest import make_sample

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def test_live_series_breaks_on_silence_and_gaps() -> None:
    samples = [
        make_sample(0.0, 200.0),
        make_sample(0.1, 210.0),
        make_sample(0.2, None),
        make_sample(0.3, 220.0),
    ]
    series = build_live_series(samples, 0.0, 10.0, gap_seconds=1.0)
    assert [len(segment) for segment in series] == [2, 1]
    gap_series = build_live_series(
        [make_sample(0.0, 200.0), make_sample(4.0, 210.0)], 0.0, 10.0, gap_seconds=1.0
    )
    assert [len(segment) for segment in gap_series] == [1, 1]


def test_live_series_filters_outside_window() -> None:
    samples = [make_sample(0.0, 200.0), make_sample(50.0, 210.0)]
    series = build_live_series(samples, 40.0, 60.0)
    assert len(series) == 1
    assert len(series[0]) == 1


def test_live_bucketed_series_omits_partial_bucket_and_keeps_gaps() -> None:
    samples = []
    values = {
        0: 200.0,
        1: 210.0,
        2: None,
        3: 220.0,
        4: 230.0,
        5: 240.0,
    }
    for bucket, hz in values.items():
        for parts in (0.25, 0.5, 0.75):
            samples.append(make_sample(bucket + parts, hz))
    series = build_live_series(samples, 0.0, 5.5, bucket_seconds=1.0)
    assert [len(segment) for segment in series] == [2, 2]
    assert series[0][0].hz == pytest.approx(200.0)
    assert series[0][1].hz == pytest.approx(210.0)
    assert series[1][0].hz == pytest.approx(220.0)
    assert all(point.hz != 240.0 for segment in series for point in segment)


def make_bucket(start: datetime, hz: float | None, voiced: float = 60.0) -> Bucket:
    item = Bucket(start=start, resolution_s=300, monitoring_s=300.0, voiced_s=voiced)
    if hz is not None and voiced > 0:
        add_histogram(item.bins, hz, voiced)
        item.in_range_s = voiced
    return item


def test_history_series_breaks_on_empty_bucket_and_gap() -> None:
    start = NOW - timedelta(hours=2)
    buckets = [
        make_bucket(start, 200.0),
        make_bucket(start + timedelta(minutes=5), 210.0),
        make_bucket(start + timedelta(minutes=10), None, voiced=0.0),
        make_bucket(start + timedelta(minutes=15), 220.0),
        make_bucket(start + timedelta(hours=1), 230.0),
    ]
    series = build_history_series(buckets, start, NOW)
    assert [len(segment) for segment in series] == [2, 1, 1]


def test_nice_bounds_include_targets_and_ticks() -> None:
    low, high = nice_bounds([190.0, 210.0], 170.0, 250.0)
    assert low <= 170.0
    assert high >= 250.0
    ticks = nice_ticks(low, high, 4)
    assert len(ticks) >= 3
    assert all(low <= tick <= high for tick in ticks)


def test_chart_labels() -> None:
    start = NOW - timedelta(days=7)
    assert chart_start_label(Period.LIVE.label, NOW - timedelta(seconds=60), NOW) == "60s ago"
    assert chart_start_label(Period.DAY.label, start, NOW) == "00:00"
    assert chart_end_label(Period.WEEK.label, NOW) != ""


class ChartHost(App[None]):
    def compose(self) -> ComposeResult:
        yield PitchChart(id="chart")


def chart_data(width_series: bool = True) -> ChartData:
    points = tuple(
        ChartPoint(
            NOW - timedelta(seconds=59 - index * 2),
            185.0 if index % 2 == 0 else 230.0,
        )
        for index in range(30)
    )
    series = (points,) if width_series else ()
    return ChartData(
        period_label="Live",
        start=NOW - timedelta(seconds=60),
        end=NOW,
        series=series,
        target_min=170.0,
        target_max=250.0,
        avg_hz=210.0,
        in_range_pct=80.0,
        start_label="60s ago",
        end_label="now",
    )


@pytest.mark.asyncio
async def test_chart_renders_axis_targets_and_data() -> None:
    app = ChartHost()
    async with app.run_test(size=(80, 18)) as pilot:
        chart = app.query_one("#chart", PitchChart)
        chart.set_data(chart_data())
        await pilot.pause()
        rendered = chart.render().plain
        assert "250" in rendered
        assert "170" in rendered
        assert "60s ago" in rendered
        assert "now" in rendered
        assert rendered.count("•") >= 10
        assert "╱" not in rendered
        assert "╲" not in rendered


@pytest.mark.asyncio
async def test_chart_scale_stays_stable_while_live_data_moves() -> None:
    app = ChartHost()
    async with app.run_test(size=(80, 18)) as pilot:
        chart = app.query_one("#chart", PitchChart)
        chart.set_data(chart_data())
        await pilot.pause()
        original = chart._scale
        moved = chart_data()
        moved_points = tuple(
            ChartPoint(point.timestamp, point.hz + 4.0) for point in moved.series[0]
        )
        chart.set_data(
            ChartData(
                period_label=moved.period_label,
                start=moved.start,
                end=moved.end,
                series=(moved_points,),
                target_min=moved.target_min,
                target_max=moved.target_max,
                avg_hz=moved.avg_hz,
                in_range_pct=moved.in_range_pct,
                start_label=moved.start_label,
                end_label=moved.end_label,
            )
        )
        await pilot.pause()
        assert chart._scale == original


@pytest.mark.asyncio
async def test_chart_scale_expands_for_extreme_pitch() -> None:
    app = ChartHost()
    async with app.run_test(size=(80, 18)) as pilot:
        chart = app.query_one("#chart", PitchChart)
        chart.set_data(chart_data())
        await pilot.pause()
        original_high = chart._scale[1] if chart._scale else 0.0
        extreme = chart_data()
        chart.set_data(
            ChartData(
                period_label=extreme.period_label,
                start=extreme.start,
                end=extreme.end,
                series=((ChartPoint(extreme.start, 480.0),),),
                target_min=extreme.target_min,
                target_max=extreme.target_max,
                avg_hz=480.0,
                in_range_pct=0.0,
                start_label=extreme.start_label,
                end_label=extreme.end_label,
            )
        )
        await pilot.pause()
        assert chart._scale is not None
        assert chart._scale[1] > original_high


@pytest.mark.asyncio
async def test_chart_numeric_fallback_when_narrow() -> None:
    app = ChartHost()
    async with app.run_test(size=(30, 10)) as pilot:
        chart = app.query_one("#chart", PitchChart)
        chart.set_data(chart_data())
        await pilot.pause()
        rendered = chart.render().plain
        assert "Chart hidden at this size" in rendered
        assert "Target 170-250 Hz" in rendered
        assert "AVG 210 Hz" in rendered


@pytest.mark.asyncio
async def test_chart_ascii_mode_avoids_unicode_line_chars() -> None:
    app = ChartHost()
    async with app.run_test(size=(80, 18)) as pilot:
        chart = app.query_one("#chart", PitchChart)
        chart.set_ascii(True)
        chart.set_data(chart_data())
        await pilot.pause()
        rendered = chart.render().plain
        assert "╭" not in rendered
        assert "─" not in rendered
        assert "•" not in rendered
