"""Application service tying audio, alert timing, statistics and storage together."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from pitch_guard.aggregation import DailySummary, History
from pitch_guard.alert import AlertTracker
from pitch_guard.audio.capture import PitchSource
from pitch_guard.audio.playback import Buzzer
from pitch_guard.config import Config
from pitch_guard.models import ClassifiedSample, RangeStatus, classify_measurement
from pitch_guard.persistence import PitchGuardStorage
from pitch_guard.stats import LiveWindow, Period, StatsEngine, StatsSummary

LOG = logging.getLogger("pitch_guard.service")
AUTOSAVE_INTERVAL_S = 30.0
ALERT_BRIDGE_S = 0.75


@dataclass(frozen=True, slots=True)
class GuardSnapshot:
    """Cheap state snapshot for the dashboard."""

    current: ClassifiedSample | None
    status: RangeStatus
    paused: bool
    out_of_range_s: float
    alert_progress: float
    alert_armed: bool
    source_description: str
    source_error: str | None
    config: Config
    session_samples: int


class PitchGuardService:
    """Drive one monitoring session without depending on the TUI."""

    def __init__(
        self,
        source: PitchSource,
        config: Config,
        storage: PitchGuardStorage,
        history: History,
        buzzer: Buzzer,
        *,
        alert_tracker: AlertTracker | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.source = source
        self.config = config.validate()
        self.storage = storage
        self.history = history
        self.live = LiveWindow()
        self.buzzer = buzzer
        self.alert = alert_tracker or AlertTracker(
            config.alert_duration_s,
            config.alert_cooldown_s,
            bridge_s=ALERT_BRIDGE_S,
        )
        self.alert.configure(self.config.alert_duration_s, self.config.alert_cooldown_s)
        self.stats = StatsEngine(history, self.live)
        self.log = logger or LOG
        self.paused = False
        self.alerts_session = 0
        self._last_save = time.monotonic()
        self._last_decision_progress = 0.0
        self._last_out_of_range_s = 0.0
        self._session_samples = 0

    def start(self) -> None:
        """Start microphone/demo capture."""
        self.paused = False
        self.alert.resume()
        self.source.start()
        self.log.info("Monitoring started with %s", self.source.description)

    def stop(self) -> None:
        """Stop capture, process remaining frames, persist and release audio."""
        self.source.stop()
        self.poll()
        self.save_history(force=True)
        self.buzzer.close()
        self.log.info("Monitoring stopped")

    def pause(self) -> None:
        """Pause capture and alert timing."""
        if self.paused:
            return
        self.source.stop()
        self.poll()
        self.paused = True
        self.alert.pause()
        self._last_out_of_range_s = 0.0
        self._last_decision_progress = 0.0
        self.log.info("Monitoring paused")

    def resume(self) -> None:
        """Resume capture with a fresh alert episode."""
        if not self.paused:
            return
        self.paused = False
        self.alert.resume()
        self.source.start()
        self.log.info("Monitoring resumed")

    def toggle_pause(self) -> None:
        """Toggle pause state."""
        if self.paused:
            self.resume()
        else:
            self.pause()

    def set_source(self, source: PitchSource, config: Config) -> None:
        """Replace the capture source after a microphone/settings change."""
        was_paused = self.paused
        self.source.stop()
        self.source = source
        self.config = config.validate()
        if not was_paused:
            self.source.start()
        self.log.info("Audio source changed to %s", source.description)

    def apply_config(self, config: Config) -> Config:
        """Validate, persist and apply a configuration update."""
        validated = config.validate()
        old = self.config
        self.config = validated
        self.alert.configure(validated.alert_duration_s, validated.alert_cooldown_s)
        set_volume = getattr(self.buzzer, "set_volume", None)
        if callable(set_volume):
            set_volume(validated.volume)
        self.storage.save_config(validated)
        if old.min_hz != validated.min_hz or old.max_hz != validated.max_hz:
            self.log.info(
                "Target range changed to %.0f-%.0f Hz", validated.min_hz, validated.max_hz
            )
        return validated

    def poll(self) -> list[ClassifiedSample]:
        """Drain and process available measurements; never touches raw audio."""
        processed: list[ClassifiedSample] = []
        for measurement in self.source.drain():
            sample = classify_measurement(measurement, self.config.min_hz, self.config.max_hz)
            processed.append(sample)
            self._session_samples += 1
            self.live.add(sample)
            self.history.add_sample(sample)
            decision = self.alert.feed(sample)
            self._last_out_of_range_s = decision.out_of_range_s
            self._last_decision_progress = decision.progress
            if decision.should_buzz:
                self.alerts_session += 1
                self.history.add_alert(sample.timestamp)
                try:
                    self.buzzer.play()
                except Exception:
                    self.log.exception("Alert playback failed")
                self.log.info(
                    "Alert buzz after %.1fs out of range at %.1f Hz",
                    decision.out_of_range_s,
                    sample.hz or 0.0,
                )
        self._autosave()
        return processed

    def _autosave(self) -> None:
        now = time.monotonic()
        if now - self._last_save < AUTOSAVE_INTERVAL_S:
            return
        self._last_save = now
        self.storage.save_history(self.history, datetime.now(UTC))

    def save_history(self, *, force: bool = False) -> None:
        """Persist history now (autosaves are throttled unless forced)."""
        now = time.monotonic()
        if not force and now - self._last_save < AUTOSAVE_INTERVAL_S:
            return
        self._last_save = now
        self.storage.save_history(self.history, datetime.now(UTC))

    def daily_summaries(self, now: datetime | None = None) -> list[DailySummary]:
        """Compute day rollups for streaks and personal bests."""
        return self.history.daily_summaries(now)

    def summary(
        self,
        period: Period,
        now: datetime | None = None,
        monotonic_now: float | None = None,
        daily_summaries: list[DailySummary] | None = None,
    ) -> StatsSummary:
        """Return statistics for a selected period."""
        return self.stats.summary(
            period,
            now,
            monotonic_now,
            daily_summaries,
        )

    def snapshot(self) -> GuardSnapshot:
        """Return dashboard state without blocking audio processing."""
        current = self.live.current()
        if self.paused:
            status = RangeStatus.PAUSED
        elif current is None:
            status = RangeStatus.SILENT
        else:
            status = current.status
        return GuardSnapshot(
            current=current,
            status=status,
            paused=self.paused,
            out_of_range_s=self._last_out_of_range_s if not self.paused else 0.0,
            alert_progress=self._last_decision_progress if not self.paused else 0.0,
            alert_armed=self.alert.armed,
            source_description=self.source.description,
            source_error=self.source.error,
            config=self.config,
            session_samples=self._session_samples,
        )
