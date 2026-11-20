"""Pure helpers for chart series and axis bounds.

Keeping data shaping separate from the Textual widget makes gap handling and
time-scale aggregation straightforward to test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil, floor, log10

from pitch_guard.aggregation import Bucket
from pitch_guard.models import ClassifiedSample

LIVE_GAP_SECONDS = 1.5
LIVE_BUCKET_SECONDS = 0.25


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
    bucket_seconds: float = 0.0,
) -> list[list[ChartPoint]]:
    """Split live samples into calm, non-interpolated chart segments.

    With ``bucket_seconds <= 0`` every voiced sample becomes a point, breaking on
    silence or a gap longer than ``gap_seconds``.  The live dashboard uses
    ``bucket_seconds=1.0``: one average point per completed one-second bucket,
    aligned to stable monotonic boundaries.  The incomplete newest bucket is
    deliberately omitted so the rightmost dot does not jitter every frame.
    """
    if bucket_seconds > 0:
        return _build_bucketed_live_series(
            samples, start_monotonic, end_monotonic, gap_seconds, bucket_seconds
        )
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


def _build_bucketed_live_series(
    samples: list[ClassifiedSample],
    start_monotonic: float,
    end_monotonic: float,
    gap_seconds: float,
    bucket_seconds: float,
) -> list[list[ChartPoint]]:
    """Aggregate live samples into completed, globally aligned time buckets."""
    bucket_count = int((end_monotonic - start_monotonic) / bucket_seconds) + 1
    first_bucket = int(start_monotonic // bucket_seconds)
    buckets: dict[int, list[tuple[float, datetime]]] = {}
    for sample in samples:
        if not (start_monotonic <= sample.monotonic <= end_monotonic):
            continue
        index = int(sample.monotonic // bucket_seconds)
        bucket_end = (index + 1) * bucket_seconds
        if bucket_end > end_monotonic:
            continue
        bucket = buckets.setdefault(index, [])
        if sample.voiced and sample.hz is not None:
            bucket.append((sample.hz, sample.timestamp))
    series: list[list[ChartPoint]] = []
    current: list[ChartPoint] = []
    previous_index: int | None = None
    for index in range(first_bucket, first_bucket + bucket_count):
        bucket = buckets.get(index)
        if not bucket:
            if current:
                series.append(current)
                current = []
            previous_index = None
            continue
        if previous_index is not None and (index - previous_index) * bucket_seconds > gap_seconds:
            if current:
                series.append(current)
            current = []
        hz = sum(value for value, _ in bucket) / len(bucket)
        timestamp = sum((moment.timestamp() for _, moment in bucket)) / len(bucket)
        current.append(ChartPoint(timestamp=datetime.fromtimestamp(timestamp, tz=UTC), hz=hz))
        previous_index = index
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
    magnitude = 10.0 ** max(0, floor(log10(raw_step)))
    choices = (1.0, 2.0, 2.5, 5.0, 10.0)
    step = min(
        (choice * magnitude for choice in choices),
        key=lambda candidate: abs(candidate - raw_step),
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
