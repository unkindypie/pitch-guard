"""Time-weighted pitch aggregation and historical bucket management.

Measurements are stored as small time buckets rather than individual audio
frames.  Each bucket keeps duration totals plus a sparse pitch histogram, which
is enough for accurate averages, medians and range compliance without retaining
raw audio.  Buckets older than a day are compacted into hourly buckets and data
older than the retention window is discarded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from pitch_guard.models import ClassifiedSample, RangeStatus
from pitch_guard.storage import iso_timestamp, parse_timestamp

HIST_BIN_WIDTH_HZ: Final[float] = 2.0
BUCKET_RESOLUTION_S: Final[int] = 300
COMPACT_RESOLUTION_S: Final[int] = 3600
COMPACT_AFTER_S: Final[float] = 24 * 3600
RETENTION_DAYS: Final[int] = 180
DAY_STREAK_MIN_VOICED_S: Final[float] = 60.0
PERSONAL_BEST_MIN_VOICED_S: Final[float] = 60.0


def histogram_bin(hz: float) -> float:
    """Return the nearest histogram bin for *hz*.

    Two-Hz bins keep the JSON compact while limiting historical pitch error to
    at most 1 Hz.  Live values and medians still come from the same weighted
    bins, so comparisons remain internally consistent.
    """
    return round(hz / HIST_BIN_WIDTH_HZ) * HIST_BIN_WIDTH_HZ


def add_histogram(histogram: dict[float, float], hz: float, seconds: float) -> None:
    """Add voiced *seconds* at *hz* to a sparse histogram."""
    if seconds <= 0 or hz <= 0:
        return
    key = histogram_bin(hz)
    histogram[key] = histogram.get(key, 0.0) + seconds


def histogram_mean(histogram: dict[float, float]) -> float | None:
    """Time-weighted mean of a pitch histogram."""
    total = sum(histogram.values())
    if total <= 0:
        return None
    return sum(hz * seconds for hz, seconds in histogram.items()) / total


def histogram_median(histogram: dict[float, float]) -> float | None:
    """Time-weighted median of a pitch histogram."""
    total = sum(histogram.values())
    if total <= 0:
        return None
    halfway = total / 2.0
    running = 0.0
    for hz in sorted(histogram):
        running += histogram[hz]
        if running >= halfway:
            return hz
    return max(histogram)


def merge_histograms(target: dict[float, float], source: dict[float, float]) -> None:
    """Merge *source* into *target* in place."""
    for hz, seconds in source.items():
        if seconds > 0:
            target[hz] = target.get(hz, 0.0) + seconds


def floor_to_resolution(moment: datetime, resolution_s: int) -> datetime:
    """Floor a timezone-aware timestamp to a UTC bucket boundary."""
    timestamp = int(moment.timestamp())
    floored = timestamp - (timestamp % resolution_s)
    return datetime.fromtimestamp(floored, tz=UTC)


def local_timezone():
    """Best-effort local timezone for daily grouping."""
    from datetime import datetime as _datetime

    return _datetime.now().astimezone().tzinfo or UTC


def local_day_bounds(day: date, timezone) -> tuple[datetime, datetime]:
    """Return UTC bounds for a local calendar day."""
    start_local = datetime.combine(day, time.min, tzinfo=timezone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


@dataclass(slots=True)
class Totals:
    """Aggregated durations and pitch distribution."""

    monitoring_s: float = 0.0
    voiced_s: float = 0.0
    in_range_s: float = 0.0
    out_of_range_s: float = 0.0
    alerts: float = 0.0
    bins: dict[float, float] = field(default_factory=dict)

    @property
    def avg_hz(self) -> float | None:
        return histogram_mean(self.bins)

    @property
    def median_hz(self) -> float | None:
        return histogram_median(self.bins)

    @property
    def in_range_pct(self) -> float | None:
        if self.voiced_s <= 0:
            return None
        return self.in_range_s / self.voiced_s * 100.0

    def add_sample(self, sample: ClassifiedSample) -> None:
        """Add one frame, weighting all statistics by its duration."""
        seconds = max(0.0, sample.duration_s)
        self.monitoring_s += seconds
        if not sample.voiced or sample.hz is None:
            return
        self.voiced_s += seconds
        add_histogram(self.bins, sample.hz, seconds)
        if sample.status is RangeStatus.IN_RANGE:
            self.in_range_s += seconds
        elif sample.status.is_out_of_range:
            self.out_of_range_s += seconds

    def merge(self, other: Totals) -> None:
        """Merge another aggregate into this one."""
        self.monitoring_s += other.monitoring_s
        self.voiced_s += other.voiced_s
        self.in_range_s += other.in_range_s
        self.out_of_range_s += other.out_of_range_s
        self.alerts += other.alerts
        merge_histograms(self.bins, other.bins)

    def scaled(self, factor: float) -> Totals:
        """Return a copy with duration-based values multiplied by *factor*."""
        factor = max(0.0, min(1.0, factor))
        return Totals(
            monitoring_s=self.monitoring_s * factor,
            voiced_s=self.voiced_s * factor,
            in_range_s=self.in_range_s * factor,
            out_of_range_s=self.out_of_range_s * factor,
            alerts=self.alerts * factor,
            bins={hz: seconds * factor for hz, seconds in self.bins.items()},
        )


@dataclass(slots=True)
class Bucket:
    """One persisted, time-aligned aggregation bucket."""

    start: datetime
    resolution_s: int = BUCKET_RESOLUTION_S
    monitoring_s: float = 0.0
    voiced_s: float = 0.0
    in_range_s: float = 0.0
    out_of_range_s: float = 0.0
    alerts: int = 0
    bins: dict[float, float] = field(default_factory=dict)
    min_hz: float | None = None
    max_hz: float | None = None

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.resolution_s)

    @property
    def totals(self) -> Totals:
        return Totals(
            monitoring_s=self.monitoring_s,
            voiced_s=self.voiced_s,
            in_range_s=self.in_range_s,
            out_of_range_s=self.out_of_range_s,
            alerts=float(self.alerts),
            bins=dict(self.bins),
        )

    def add_sample(self, sample: ClassifiedSample) -> None:
        """Accumulate one classified sample into this bucket."""
        seconds = max(0.0, sample.duration_s)
        self.monitoring_s += seconds
        if sample.min_hz is not None:
            self.min_hz = sample.min_hz
        if sample.max_hz is not None:
            self.max_hz = sample.max_hz
        if not sample.voiced or sample.hz is None:
            return
        self.voiced_s += seconds
        add_histogram(self.bins, sample.hz, seconds)
        if sample.status is RangeStatus.IN_RANGE:
            self.in_range_s += seconds
        elif sample.status.is_out_of_range:
            self.out_of_range_s += seconds

    def add_alert(self) -> None:
        """Count one alert in this bucket."""
        self.alerts += 1

    def merge(self, other: Bucket) -> None:
        """Merge *other* into this bucket, preserving weighted durations."""
        self.monitoring_s += other.monitoring_s
        self.voiced_s += other.voiced_s
        self.in_range_s += other.in_range_s
        self.out_of_range_s += other.out_of_range_s
        self.alerts += other.alerts
        merge_histograms(self.bins, other.bins)
        if self.min_hz != other.min_hz or self.max_hz != other.max_hz:
            # The historical in-range totals already preserve the thresholds
            # that were active at capture time.  Mixed ranges are represented
            # by the totals rather than a single misleading threshold pair.
            self.min_hz = None
            self.max_hz = None

    def to_json(self) -> dict[str, object]:
        """Serialise the bucket for ``history.json``."""
        return {
            "start": iso_timestamp(self.start),
            "resolution": self.resolution_s,
            "monitoring": round(self.monitoring_s, 3),
            "voiced": round(self.voiced_s, 3),
            "in_range": round(self.in_range_s, 3),
            "out_of_range": round(self.out_of_range_s, 3),
            "alerts": self.alerts,
            "min_hz": self.min_hz,
            "max_hz": self.max_hz,
            "bins": {str(int(hz)): round(seconds, 3) for hz, seconds in self.bins.items()},
        }

    @classmethod
    def from_json(cls, payload: object) -> Bucket | None:
        """Decode a persisted bucket, rejecting malformed entries."""
        if not isinstance(payload, dict):
            return None
        start = parse_timestamp(payload.get("start"))
        if start is None:
            return None
        resolution = _positive_int(payload.get("resolution"), BUCKET_RESOLUTION_S)
        bins: dict[float, float] = {}
        raw_bins = payload.get("bins")
        if isinstance(raw_bins, dict):
            for key, value in raw_bins.items():
                try:
                    hz = float(key)
                    seconds = float(value)
                except (TypeError, ValueError):
                    continue
                if hz > 0 and seconds > 0:
                    bins[hz] = seconds
        return cls(
            start=start,
            resolution_s=resolution,
            monitoring_s=_non_negative_float(payload.get("monitoring")),
            voiced_s=_non_negative_float(payload.get("voiced")),
            in_range_s=_non_negative_float(payload.get("in_range")),
            out_of_range_s=_non_negative_float(payload.get("out_of_range")),
            alerts=max(0, _non_negative_int(payload.get("alerts"))),
            bins=bins,
            min_hz=_optional_float(payload.get("min_hz")),
            max_hz=_optional_float(payload.get("max_hz")),
        )


@dataclass(frozen=True, slots=True)
class DailySummary:
    """Derived statistics for one local calendar day."""

    day: date
    monitoring_s: float
    voiced_s: float
    in_range_s: float
    out_of_range_s: float
    alerts: int
    avg_hz: float | None
    median_hz: float | None
    in_range_pct: float | None

    def to_json(self) -> dict[str, object]:
        return {
            "date": self.day.isoformat(),
            "monitoring": round(self.monitoring_s, 3),
            "voiced": round(self.voiced_s, 3),
            "in_range": round(self.in_range_s, 3),
            "out_of_range": round(self.out_of_range_s, 3),
            "alerts": self.alerts,
            "avg_hz": None if self.avg_hz is None else round(self.avg_hz, 2),
            "median_hz": None if self.median_hz is None else round(self.median_hz, 2),
            "in_range_pct": None if self.in_range_pct is None else round(self.in_range_pct, 2),
        }

    @classmethod
    def from_json(cls, payload: object) -> DailySummary | None:
        if not isinstance(payload, dict):
            return None
        raw_day = payload.get("date")
        if not isinstance(raw_day, str):
            return None
        try:
            day = date.fromisoformat(raw_day)
        except ValueError:
            return None
        return cls(
            day=day,
            monitoring_s=_non_negative_float(payload.get("monitoring")),
            voiced_s=_non_negative_float(payload.get("voiced")),
            in_range_s=_non_negative_float(payload.get("in_range")),
            out_of_range_s=_non_negative_float(payload.get("out_of_range")),
            alerts=max(0, _non_negative_int(payload.get("alerts"))),
            avg_hz=_optional_float(payload.get("avg_hz")),
            median_hz=_optional_float(payload.get("median_hz")),
            in_range_pct=_optional_float(payload.get("in_range_pct")),
        )


class History:
    """In-memory historical buckets plus the currently open bucket."""

    def __init__(
        self,
        buckets: list[Bucket] | None = None,
        *,
        now: datetime | None = None,
        resolution_s: int = BUCKET_RESOLUTION_S,
        retention_days: int = RETENTION_DAYS,
    ) -> None:
        self.resolution_s = resolution_s
        self.retention_days = retention_days
        self._buckets: list[Bucket] = sorted(buckets or [], key=lambda bucket: bucket.start)
        self._open: Bucket | None = None
        self._current_time = now or datetime.now(UTC)
        self._adopt_current_open_bucket(self._current_time)

    @property
    def open_bucket(self) -> Bucket | None:
        return self._open

    def _adopt_current_open_bucket(self, now: datetime) -> None:
        if not self._buckets:
            return
        latest = self._buckets[-1]
        if latest.resolution_s == self.resolution_s and latest.start == floor_to_resolution(
            now, self.resolution_s
        ):
            self._open = self._buckets.pop()
            if self._open.min_hz is not None and self._open.max_hz is not None:
                pass

    def add_sample(self, sample: ClassifiedSample) -> None:
        """Accumulate one sample into the current time bucket."""
        bucket_start = floor_to_resolution(sample.timestamp, self.resolution_s)
        if self._open is None:
            self._open = Bucket(start=bucket_start, resolution_s=self.resolution_s)
        elif self._open.start != bucket_start:
            self._finalize_open()
            self._open = Bucket(start=bucket_start, resolution_s=self.resolution_s)
        self._open.add_sample(sample)

    def add_alert(self, at: datetime) -> None:
        """Count an alert at wall-clock time *at*."""
        bucket_start = floor_to_resolution(at, self.resolution_s)
        if self._open is None:
            self._open = Bucket(start=bucket_start, resolution_s=self.resolution_s)
        elif self._open.start != bucket_start:
            self._finalize_open()
            self._open = Bucket(start=bucket_start, resolution_s=self.resolution_s)
        self._open.add_alert()

    def _finalize_open(self) -> None:
        if self._open is not None:
            self._insert_bucket(self._open)
            self._open = None

    def _insert_bucket(self, bucket: Bucket) -> None:
        for existing in self._buckets:
            if existing.start == bucket.start and existing.resolution_s == bucket.resolution_s:
                existing.merge(bucket)
                return
        self._buckets.append(bucket)
        self._buckets.sort(key=lambda item: item.start)

    def snapshot(self, now: datetime | None = None) -> list[Bucket]:
        """Return completed buckets plus a copy of the open bucket."""
        moment = now or datetime.now(UTC)
        self._prune(moment)
        result = list(self._buckets)
        if self._open is not None and (self._open.monitoring_s > 0 or self._open.alerts > 0):
            result.append(_copy_bucket(self._open))
        return result

    def query(self, start: datetime, end: datetime, now: datetime | None = None) -> Totals:
        """Aggregate time-weighted totals intersecting ``[start, end)``."""
        if end <= start:
            return Totals()
        moment = now or datetime.now(UTC)
        self._prune(moment)
        totals = Totals()
        for bucket in self._iter_with_open():
            overlap = _overlap_seconds(bucket, start, end)
            if overlap <= 0:
                continue
            fraction = min(1.0, overlap / max(1, bucket.resolution_s))
            totals.merge(bucket.totals.scaled(fraction))
        return totals

    def _iter_with_open(self) -> list[Bucket]:
        buckets = list(self._buckets)
        if self._open is not None:
            buckets.append(self._open)
        return buckets

    def daily_summaries(
        self, now: datetime | None = None, *, days: int = RETENTION_DAYS
    ) -> list[DailySummary]:
        """Compute local-day summaries for streaks and personal bests."""
        current = now or datetime.now(UTC)
        moment = current.astimezone()
        timezone = local_timezone()
        today = moment.date()
        summaries: list[DailySummary] = []
        for offset in range(days + 1):
            day = today - timedelta(days=offset)
            start, end = local_day_bounds(day, timezone)
            totals = self.query(start, min(end, current), current)
            if totals.monitoring_s <= 0 and totals.voiced_s <= 0:
                continue
            summaries.append(
                DailySummary(
                    day=day,
                    monitoring_s=totals.monitoring_s,
                    voiced_s=totals.voiced_s,
                    in_range_s=totals.in_range_s,
                    out_of_range_s=totals.out_of_range_s,
                    alerts=round(totals.alerts),
                    avg_hz=totals.avg_hz,
                    median_hz=totals.median_hz,
                    in_range_pct=totals.in_range_pct,
                )
            )
        return summaries

    def compact(self, now: datetime | None = None) -> None:
        """Drop expired data and merge old five-minute buckets into hourly ones."""
        moment = now or datetime.now(UTC)
        self._prune(moment)
        cutoff = moment - timedelta(seconds=COMPACT_AFTER_S)
        merged: dict[datetime, Bucket] = {}
        retained: list[Bucket] = []
        for bucket in self._buckets:
            if bucket.start < cutoff and bucket.resolution_s < COMPACT_RESOLUTION_S:
                hour_start = floor_to_resolution(bucket.start, COMPACT_RESOLUTION_S)
                target = merged.get(hour_start)
                if target is None:
                    target = Bucket(
                        start=hour_start,
                        resolution_s=COMPACT_RESOLUTION_S,
                        min_hz=bucket.min_hz,
                        max_hz=bucket.max_hz,
                    )
                    merged[hour_start] = target
                target.merge(bucket)
            else:
                retained.append(bucket)
        self._buckets = sorted([*retained, *merged.values()], key=lambda bucket: bucket.start)

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=self.retention_days)
        self._buckets = [bucket for bucket in self._buckets if bucket.start >= cutoff]
        if self._open is not None and self._open.start < cutoff:
            self._open = None

    def streak_and_best(
        self, summaries: list[DailySummary], today: date | None = None
    ) -> tuple[int, DailySummary | None]:
        """Return (current daily streak, personal-best day)."""
        by_day = {summary.day: summary for summary in summaries}
        cursor = today or datetime.now(UTC).astimezone().date()
        today_summary = by_day.get(cursor)
        if today_summary is None or today_summary.voiced_s < DAY_STREAK_MIN_VOICED_S:
            cursor -= timedelta(days=1)
        streak = 0
        while True:
            summary = by_day.get(cursor)
            if summary is None or summary.voiced_s < DAY_STREAK_MIN_VOICED_S:
                break
            streak += 1
            cursor -= timedelta(days=1)

        eligible = [
            summary
            for summary in summaries
            if summary.voiced_s >= PERSONAL_BEST_MIN_VOICED_S and summary.in_range_pct is not None
        ]
        best = (
            max(eligible, key=lambda summary: (summary.in_range_pct or 0.0, summary.day))
            if eligible
            else None
        )
        return streak, best


def _copy_bucket(bucket: Bucket) -> Bucket:
    return Bucket(
        start=bucket.start,
        resolution_s=bucket.resolution_s,
        monitoring_s=bucket.monitoring_s,
        voiced_s=bucket.voiced_s,
        in_range_s=bucket.in_range_s,
        out_of_range_s=bucket.out_of_range_s,
        alerts=bucket.alerts,
        bins=dict(bucket.bins),
        min_hz=bucket.min_hz,
        max_hz=bucket.max_hz,
    )


def _overlap_seconds(bucket: Bucket, start: datetime, end: datetime) -> float:
    overlap_start = max(bucket.start, start)
    overlap_end = min(bucket.end, end)
    if overlap_end <= overlap_start:
        return 0.0
    return (overlap_end - overlap_start).total_seconds()


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value > 0:
        return value
    return default


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and math.isfinite(value):
        return max(0, int(value))
    return 0


def _non_negative_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return max(0.0, float(value))
    return 0.0


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None
