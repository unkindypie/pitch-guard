"""Pure helpers for chart series and axis bounds.

Keeping data shaping separate from the Textual widget makes gap handling and
time-scale aggregation straightforward to test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil, floor

from pitch_guard.aggregation import Bucket
from pitch_guard.models import ClassifiedSample

LIVE_GAP_SECONDS = 1.5


@dataclass(frozen=True, slots=True)
class ChartPoint:
    """One voiced chart observation."""

    timestamp: datetime
    hz: float


def build_live_series(
    samples: list[ClassifiedSample],
    start_monotonic: float,
    end_monotonic: float,
    *,
    gap_seconds: float = LIVE_GAP_SECONDS,
) -> list[list[ChartPoint]]:
    """Split live samples into connected voiced segments.

    Any silent/unvoiced frame or a gap longer than ``gap_seconds`` starts a new
    segment, so the chart never draws through silence or a processing stall.
    """
    series: list[list[ChartPoint]] = []
    current: list[ChartPoint] = []
    previous_voiced: ClassifiedSample | None = None
    for sample in samples:
        if not (start_monotonic <= sample.monotonic <= end_monotonic):
            continue
        if not sample.voiced or sample.hz is None:
            if current:
                series.append(current)
                current = []
            previous_voiced = None
            continue
        if (
            previous_voiced is not None
            and sample.monotonic - previous_voiced.monotonic > gap_seconds
        ):
            if current:
                series.append(current)
            current = []
        current.append(ChartPoint(timestamp=sample.timestamp, hz=sample.hz))
        previous_voiced = sample
    if current:
        series.append(current)
    return series


def build_history_series(
    buckets: list[Bucket],
    start: datetime,
    end: datetime,
) -> list[list[ChartPoint]]:
    """Convert time buckets into connected chart segments.

    Empty buckets and gaps larger than roughly two bucket resolutions break the
    line.  Silence is therefore shown as a gap rather than as 0 Hz.
    """
    selected = [bucket for bucket in buckets if bucket.end > start and bucket.start < end]
    selected.sort(key=lambda bucket: bucket.start)
    series: list[list[ChartPoint]] = []
    current: list[ChartPoint] = []
    previous: Bucket | None = None
    for bucket in selected:
        if bucket.voiced_s <= 0 or bucket.totals.avg_hz is None:
            if current:
                series.append(current)
                current = []
            previous = bucket
            continue
        if previous is not None:
            gap = bucket.start - previous.end
            allowed = timedelta(seconds=max(1, previous.resolution_s, bucket.resolution_s) * 1.8)
            if gap > allowed:
                if current:
                    series.append(current)
                current = []
        midpoint = bucket.start + (bucket.end - bucket.start) / 2
        bounded = min(max(midpoint, start + timedelta(seconds=1)), end - timedelta(seconds=1))
        current.append(ChartPoint(timestamp=bounded, hz=bucket.totals.avg_hz))
        previous = bucket
    if current:
        series.append(current)
    return series


def nice_bounds(values: list[float], target_min: float, target_max: float) -> tuple[float, float]:
    """Choose readable Y-axis bounds including the target thresholds."""
    candidates = [target_min, target_max, *values]
    low = max(40.0, floor((min(candidates) - 12.0) / 20.0) * 20.0)
    high = min(650.0, ceil((max(candidates) + 12.0) / 20.0) * 20.0)
    if high - low < 60.0:
        high = low + 60.0
    return float(low), float(high)


def nice_ticks(low: float, high: float, count: int = 4) -> list[float]:
    """Return evenly spaced, rounded Y-axis ticks."""
    if count < 2:
        count = 2
    span = high - low
    raw_step = span / (count - 1)
    magnitude = 10.0 ** max(0, floor(__import__("math").log10(raw_step)))
    choices = (1.0, 2.0, 2.5, 5.0, 10.0)
    step = next(
        (choice * magnitude for choice in choices if choice * magnitude >= raw_step), magnitude * 10
    )
    start = ceil(low / step) * step
    ticks: list[float] = []
    value = start
    while value <= high + 1e-6 and len(ticks) < count + 2:
        ticks.append(round(value, 1))
        value += step
    if not ticks:
        ticks = [low, high]
    return ticks


def format_axis_hz(value: float) -> str:
    """Format a Y-axis tick without a unit suffix."""
    return f"{value:.0f}"


def chart_start_label(period_label: str, start: datetime, _end: datetime) -> str:
    """Compact left-side X-axis label for a period."""
    local_start = start.astimezone()
    if period_label == "Live":
        return "60s ago"
    if period_label == "Day":
        return "00:00"
    if period_label == "Week":
        return local_start.strftime("%b %d")
    if period_label == "Month":
        return local_start.strftime("%b %d")
    return local_start.strftime("%b %d")


def chart_end_label(period_label: str, end: datetime) -> str:
    """Compact right-side X-axis label for a period."""
    if period_label == "Live":
        return "now"
    return end.astimezone().strftime("%H:%M")


def now_utc() -> datetime:
    """Small indirection for tests."""
    return datetime.now(UTC)
