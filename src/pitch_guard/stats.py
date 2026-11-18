"""Selected-period statistics, live windows and improvement comparisons."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from enum import StrEnum
from typing import Final

from pitch_guard.aggregation import DailySummary, History, Totals
from pitch_guard.models import ClassifiedSample

MIN_COMPARISON_VOICED_S: Final[float] = 5.0
LIVE_CHART_SECONDS: Final[float] = 60.0
LIVE_WINDOW_SECONDS: Final[float] = 180.0


class Period(StrEnum):
    """Chart and statistics periods."""

    LIVE = "live"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    NINETY_DAYS = "90d"

    @property
    def shortcut(self) -> str:
        """Keyboard shortcut number for the period."""
        return {
            Period.LIVE: "1",
            Period.DAY: "2",
            Period.WEEK: "3",
            Period.MONTH: "4",
            Period.NINETY_DAYS: "5",
        }[self]

    @property
    def label(self) -> str:
        return {
            Period.LIVE: "Live",
            Period.DAY: "Day",
            Period.WEEK: "Week",
            Period.MONTH: "Month",
            Period.NINETY_DAYS: "90 Days",
        }[self]

    @property
    def detail(self) -> str:
        return {
            Period.LIVE: "last 60s",
            Period.DAY: "today",
            Period.WEEK: "last 7 days",
            Period.MONTH: "last 30 days",
            Period.NINETY_DAYS: "last 90 days",
        }[self]

    @classmethod
    def from_key(cls, key: str) -> Period | None:
        """Resolve a period name such as ``week``."""
        for period in cls:
            if period.value == key:
                return period
        return None


@dataclass(frozen=True, slots=True)
class PeriodWindow:
    """Wall-clock bounds for a selected period and its comparison window."""

    period: Period
    start: datetime
    end: datetime
    previous_start: datetime
    previous_end: datetime


def period_window(period: Period, now: datetime | None = None) -> PeriodWindow:
    """Resolve period boundaries using local calendar days for Day."""
    moment = (now or datetime.now(UTC)).astimezone()
    utc_moment = moment.astimezone(UTC)
    if period is Period.LIVE:
        start = utc_moment - timedelta(seconds=LIVE_CHART_SECONDS)
        return PeriodWindow(period, start, utc_moment, start - timedelta(seconds=60), start)
    if period is Period.DAY:
        local_start = datetime.combine(moment.date(), dt_time.min, tzinfo=moment.tzinfo)
        start = local_start.astimezone(UTC)
        previous_start = (local_start - timedelta(days=1)).astimezone(UTC)
        previous_end = (moment - timedelta(days=1)).astimezone(UTC)
        return PeriodWindow(period, start, utc_moment, previous_start, previous_end)

    durations = {
        Period.WEEK: timedelta(days=7),
        Period.MONTH: timedelta(days=30),
        Period.NINETY_DAYS: timedelta(days=90),
    }
    duration = durations[period]
    start = utc_moment - duration
    return PeriodWindow(period, start, utc_moment, start - duration, start)


class LiveWindow:
    """Bounded recent-sample buffer for the live chart and live statistics."""

    def __init__(self, window_s: float = LIVE_WINDOW_SECONDS) -> None:
        self.window_s = max(LIVE_CHART_SECONDS + 60.0, window_s)
        self._samples: deque[ClassifiedSample] = deque()

    def add(self, sample: ClassifiedSample) -> None:
        """Add a sample and discard measurements older than the window."""
        self._samples.append(sample)
        cutoff = sample.monotonic - self.window_s
        while self._samples and self._samples[0].monotonic < cutoff:
            self._samples.popleft()

    def clear(self) -> None:
        self._samples.clear()

    def query(self, start: float, end: float) -> Totals:
        """Aggregate samples whose frame end lies in ``[start, end)``."""
        totals = Totals()
        for sample in self._samples:
            if start <= sample.monotonic < end:
                totals.add_sample(sample)
        return totals

    def totals_for(self, seconds: float, now: float | None = None) -> Totals:
        """Aggregate the most recent *seconds*."""
        moment = now if now is not None else time.monotonic()
        return self.query(moment - seconds, moment)

    def sample_times(self) -> tuple[float, float] | None:
        """Return the oldest and newest monotonic timestamps."""
        if not self._samples:
            return None
        return self._samples[0].monotonic, self._samples[-1].monotonic

    def samples(self) -> list[ClassifiedSample]:
        """Return a snapshot of the live samples."""
        return list(self._samples)

    def current(self) -> ClassifiedSample | None:
        """Return the most recent sample, if any."""
        return self._samples[-1] if self._samples else None


@dataclass(frozen=True, slots=True)
class StatsSummary:
    """Everything the statistics panel needs for one selected period."""

    period: Period
    window: PeriodWindow
    avg_hz: float | None
    median_hz: float | None
    monitoring_s: float
    voiced_s: float
    in_range_s: float
    out_of_range_s: float
    in_range_pct: float | None
    alerts: int
    improvement_pp: float | None
    streak_days: int | None
    best_day: DailySummary | None

    @property
    def has_data(self) -> bool:
        return self.monitoring_s > 0 or self.voiced_s > 0


class StatsEngine:
    """Compute summaries from live samples and persisted history."""

    def __init__(self, history: History, live: LiveWindow) -> None:
        self.history = history
        self.live = live

    def summary(
        self,
        period: Period,
        now: datetime | None = None,
        monotonic_now: float | None = None,
        daily_summaries: list[DailySummary] | None = None,
    ) -> StatsSummary:
        """Build a summary for *period* and its previous equivalent period."""
        moment = now or datetime.now(UTC)
        window = period_window(period, moment)
        if period is Period.LIVE:
            live_now = monotonic_now if monotonic_now is not None else time.monotonic()
            current = self.live.query(live_now - 60.0, live_now)
            previous = self.live.query(live_now - 120.0, live_now - 60.0)
        else:
            current = self.history.query(window.start, window.end, moment)
            previous = self.history.query(window.previous_start, window.previous_end, moment)

        improvement = _improvement(current, previous)
        summaries = daily_summaries
        if summaries is None:
            summaries = self.history.daily_summaries(moment)
        streak, best = self.history.streak_and_best(summaries, moment.astimezone().date())
        return StatsSummary(
            period=period,
            window=window,
            avg_hz=current.avg_hz,
            median_hz=current.median_hz,
            monitoring_s=current.monitoring_s,
            voiced_s=current.voiced_s,
            in_range_s=current.in_range_s,
            out_of_range_s=current.out_of_range_s,
            in_range_pct=current.in_range_pct,
            alerts=round(current.alerts),
            improvement_pp=improvement,
            streak_days=streak if summaries else None,
            best_day=best,
        )


def _improvement(current: Totals, previous: Totals) -> float | None:
    if (
        current.voiced_s < MIN_COMPARISON_VOICED_S
        or previous.voiced_s < MIN_COMPARISON_VOICED_S
        or current.in_range_pct is None
        or previous.in_range_pct is None
    ):
        return None
    return current.in_range_pct - previous.in_range_pct


def format_duration(seconds: float | None, *, compact: bool = False) -> str:
    """Format a duration as ``H:MM:SS`` or ``M:SS``."""
    if seconds is None or seconds <= 0:
        return "N/A" if compact else "0:00"
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_hz(hz: float | None, *, decimals: int = 0) -> str:
    """Format a pitch value or N/A."""
    if hz is None:
        return "N/A"
    return f"{hz:.{decimals}f} Hz"


def format_pct(value: float | None, *, decimals: int = 0) -> str:
    """Format a percentage or N/A."""
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}%"


def format_improvement(value: float | None) -> str:
    """Format a percentage-point difference with an explicit sign."""
    if value is None:
        return "N/A"
    if abs(value) < 0.05:
        return "0.0 pp"
    return f"{value:+.1f} pp"


def format_date(day: date | None) -> str:
    """Short local date label for personal bests."""
    return day.strftime("%b %d") if day else "N/A"
