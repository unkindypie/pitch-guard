"""Deterministic simulated pitch source for demos and UI tests."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime

from pitch_guard.audio.capture import ThreadedPitchSource
from pitch_guard.models import PitchKind, PitchMeasurement


class DemoPitchSource(ThreadedPitchSource):
    """Produce a repeatable speech-like pitch trace without a microphone.

    The 60-second scenario deliberately includes silence, in-range speech and
    sustained below/above-range episodes so every dashboard state can be
    demonstrated.  It never touches the audio backend.
    """

    def __init__(self, frame_duration_s: float = 0.064, seed: int = 7) -> None:
        super().__init__()
        self._frame_duration_s = frame_duration_s
        self._seed = seed
        self._description = "Demo source (simulated)"

    @property
    def description(self) -> str:
        return self._description

    def _run(self) -> None:
        started = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            elapsed = now - started
            kind, hz = self._scenario(elapsed)
            if kind is PitchKind.VOICED and hz is not None:
                measurement = PitchMeasurement(
                    monotonic=now,
                    timestamp=datetime.now(UTC),
                    hz=hz,
                    rms=0.055,
                    confidence=0.96,
                    duration_s=self._frame_duration_s,
                    kind=kind,
                )
            else:
                measurement = PitchMeasurement(
                    monotonic=now,
                    timestamp=datetime.now(UTC),
                    hz=None,
                    rms=0.001 if kind is PitchKind.SILENT else 0.02,
                    confidence=0.0,
                    duration_s=self._frame_duration_s,
                    kind=kind,
                )
            self._queue.put(measurement)
            self._stop_event.wait(self._frame_duration_s)

    def _scenario(self, elapsed: float) -> tuple[PitchKind, float | None]:
        """Return a pitch for a point in the repeating demo timeline."""
        cycle = elapsed % 60.0
        noise = ((elapsed * 1000) % 7 - 3) * 0.22
        if cycle < 2.0:
            return PitchKind.SILENT, None
        if cycle < 9.5:
            return PitchKind.VOICED, 205.0 + 18.0 * math.sin(cycle * 0.55) + noise
        if cycle < 10.5:
            return PitchKind.SILENT, None
        if cycle < 19.5:
            return PitchKind.VOICED, 200.0 + 22.0 * math.sin(cycle * 0.42) + noise
        if cycle < 23.5:
            # Sustained below-range episode: long enough to demonstrate a buzz.
            return PitchKind.VOICED, 138.0 + 8.0 * math.sin(cycle * 0.8) + noise
        if cycle < 24.5:
            return PitchKind.SILENT, None
        if cycle < 33.0:
            return PitchKind.VOICED, 212.0 + 16.0 * math.sin(cycle * 0.37) + noise
        if cycle < 40.0:
            # Sustained above-range episode.
            return PitchKind.VOICED, 294.0 + 12.0 * math.sin(cycle * 0.6) + noise
        if cycle < 41.5:
            return PitchKind.SILENT, None
        if cycle < 52.0:
            return PitchKind.VOICED, 198.0 + 20.0 * math.sin(cycle * 0.33) + noise
        if cycle < 54.0:
            return PitchKind.UNVOICED, None
        return PitchKind.VOICED, 208.0 + 15.0 * math.sin(cycle * 0.5) + noise
