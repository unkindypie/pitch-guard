"""Continuous-deviation alert timing.

The tracker never counts in-range, silent or unvoiced time toward the alert
threshold.  A brief in-range frame, silence, or a processing gap longer than
``max_gap_s`` ends the current episode.  After a buzz the tracker is disarmed
until voiced pitch has returned inside the range *and* the cooldown has elapsed.
"""

from __future__ import annotations

from dataclasses import dataclass

from pitch_guard.models import ClassifiedSample, RangeStatus


@dataclass(frozen=True, slots=True)
class AlertDecision:
    """Outcome of feeding one classified sample to the tracker."""

    should_buzz: bool
    status: RangeStatus
    out_of_range_s: float
    progress: float
    armed: bool
    episode_active: bool


class AlertTracker:
    """State machine implementing the continuous out-of-range buzz rule."""

    def __init__(
        self,
        duration_s: float = 3.0,
        cooldown_s: float = 10.0,
        *,
        max_gap_s: float = 0.35,
    ) -> None:
        self._duration_s = max(0.0, duration_s)
        self._cooldown_s = max(0.0, cooldown_s)
        self._max_gap_s = max(0.0, max_gap_s)
        self._episode_start: float | None = None
        self._episode_first_duration = 0.0
        self._episode_alerted = False
        self._last_out_of_range_at: float | None = None
        self._last_alert_at: float | None = None
        self._armed = True

    @property
    def duration_s(self) -> float:
        return self._duration_s

    @property
    def cooldown_s(self) -> float:
        return self._cooldown_s

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def last_alert_at(self) -> float | None:
        return self._last_alert_at

    @property
    def episode_active(self) -> bool:
        return self._episode_start is not None

    def configure(self, duration_s: float, cooldown_s: float) -> None:
        """Apply new alert timings without discarding the current episode."""
        self._duration_s = max(0.0, duration_s)
        self._cooldown_s = max(0.0, cooldown_s)

    def reset_episode(self) -> None:
        """End the current episode but keep cooldown/re-arm state."""
        self._episode_start = None
        self._episode_first_duration = 0.0
        self._episode_alerted = False
        self._last_out_of_range_at = None

    def pause(self) -> None:
        """Pause timing; resumed audio starts with a fresh episode."""
        self.reset_episode()

    def resume(self) -> None:
        """Resume with a fresh episode while preserving alert cooldown."""
        self.reset_episode()

    def feed(self, sample: ClassifiedSample) -> AlertDecision:
        """Process one classified sample and decide whether to buzz now."""
        now = sample.monotonic
        status = sample.status

        if status is RangeStatus.IN_RANGE:
            self._episode_start = None
            self._episode_first_duration = 0.0
            self._episode_alerted = False
            self._last_out_of_range_at = None
            if not self._armed and self._cooldown_elapsed(now):
                self._armed = True
            return self._decision(False, status, 0.0)

        if not status.is_out_of_range:
            # Silence and unvoiced input break the continuous episode.  They do
            # not re-arm an alerted tracker; only reliable in-range pitch does.
            self._episode_start = None
            self._episode_first_duration = 0.0
            self._episode_alerted = False
            self._last_out_of_range_at = None
            return self._decision(False, status, 0.0)

        previous_out = self._last_out_of_range_at
        gap = None if previous_out is None else now - previous_out
        if self._episode_start is None or gap is None or gap < 0 or gap > self._max_gap_s:
            self._episode_start = now
            self._episode_first_duration = max(0.0, sample.duration_s)
            self._episode_alerted = False

        self._last_out_of_range_at = now
        elapsed = max(0.0, now - self._episode_start + self._episode_first_duration)
        should_buzz = False
        if not self._episode_alerted and elapsed + 1e-9 >= self._duration_s:
            if self._armed and self._cooldown_elapsed(now):
                should_buzz = True
                self._last_alert_at = now
                self._armed = False
            # Suppress the remainder of this episode even when the cooldown or
            # re-arm condition prevented the buzz.
            self._episode_alerted = True
        progress = 1.0 if self._duration_s <= 0 else min(1.0, elapsed / self._duration_s)
        return self._decision(should_buzz, status, elapsed, progress)

    def _cooldown_elapsed(self, now: float) -> bool:
        return self._last_alert_at is None or now - self._last_alert_at >= self._cooldown_s

    def _decision(
        self,
        should_buzz: bool,
        status: RangeStatus,
        elapsed: float,
        progress: float = 0.0,
    ) -> AlertDecision:
        return AlertDecision(
            should_buzz=should_buzz,
            status=status,
            out_of_range_s=elapsed,
            progress=progress,
            armed=self._armed,
            episode_active=self._episode_start is not None,
        )
