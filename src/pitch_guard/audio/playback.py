"""Short cross-platform alert sound.

Playback uses PortAudio through ``sounddevice`` and is optional: when no output
device exists the application keeps monitoring and logs a warning instead of
crashing.  Waveforms are generated in memory; no audio is recorded or saved.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

BUZZ_SAMPLE_RATE = 22_050
BUZZ_DURATION_S = 0.34


class Buzzer(Protocol):
    """Playback interface used by the alert tracker."""

    def play(self) -> None:
        """Play one short buzz."""

    def close(self) -> None:
        """Release playback resources."""


class NullBuzzer:
    """Silent buzzer used by tests and headless runs."""

    def __init__(self) -> None:
        self.play_count = 0

    def play(self) -> None:
        self.play_count += 1

    def close(self) -> None:
        """No resources to release."""


def make_buzz_waveform(volume: int) -> NDArray[np.float32]:
    """Generate a short buzzy tone scaled to a 0-100 volume."""
    volume_fraction = min(100, max(0, volume)) / 100.0
    if volume_fraction <= 0:
        return np.zeros(1, dtype=np.float32)
    sample_count = int(BUZZ_SAMPLE_RATE * BUZZ_DURATION_S)
    time_axis = np.arange(sample_count, dtype=np.float64) / BUZZ_SAMPLE_RATE
    fundamental = 92.0
    buzzy = np.sign(np.sin(2.0 * np.pi * fundamental * time_axis))
    harmonic = 0.35 * np.sin(2.0 * np.pi * fundamental * 2.02 * time_axis)
    modulation = 0.72 + 0.28 * np.sin(2.0 * np.pi * 31.0 * time_axis)
    envelope = np.minimum(1.0, time_axis / 0.012) * np.exp(-time_axis * 4.2)
    wave = (buzzy + harmonic) * modulation * envelope
    peak = float(np.max(np.abs(wave))) or 1.0
    return (wave / peak * volume_fraction * 0.9).astype(np.float32)


class SoundDeviceBuzzer:
    """Play the generated buzz through the default output device."""

    def __init__(self, volume: int = 70, logger: logging.Logger | None = None) -> None:
        self._volume = volume
        self._log = logger or logging.getLogger("pitch_guard.audio")
        self._lock = threading.Lock()
        self._warned = False
        self._waveform = make_buzz_waveform(volume)

    def set_volume(self, volume: int) -> None:
        """Update volume for subsequent alerts."""
        with self._lock:
            self._volume = volume
            self._waveform = make_buzz_waveform(volume)

    def play(self) -> None:
        """Play the buzz without blocking the TUI event loop."""
        with self._lock:
            if self._volume <= 0:
                return
            waveform = self._waveform.copy()
        try:
            import sounddevice as sd

            sd.play(waveform, BUZZ_SAMPLE_RATE, blocking=False)
        except Exception as error:  # noqa: BLE001 - audio backend errors vary
            if not self._warned:
                self._log.warning("Alert sound unavailable: %s", error)
                self._warned = True
            else:
                self._log.debug("Alert sound failed", exc_info=True)

    def close(self) -> None:
        """Stop any sound still playing."""
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:  # noqa: BLE001 - audio backend errors vary
            self._log.debug("Could not stop audio playback", exc_info=True)
