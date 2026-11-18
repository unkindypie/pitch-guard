"""Fundamental-frequency estimation for microphone frames.

The detector uses the YIN cumulative-mean-normalised difference function.  It
is deliberately conservative: quiet frames are labelled silent and frames
without a convincing periodic dip are labelled unvoiced, so neither silence nor
steady background noise can drive the range alert timer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
from numpy.typing import NDArray

from pitch_guard.models import PitchKind, PitchMeasurement


@dataclass(frozen=True, slots=True)
class PitchDetectorConfig:
    """Tuning parameters for :class:`PitchDetector`."""

    min_hz: float = 50.0
    max_hz: float = 700.0
    silence_rms: float = 0.006
    yin_threshold: float = 0.16
    fallback_threshold: float = 0.32
    min_confidence: float = 0.65
    max_zero_crossing_rate: float = 0.22


class PitchDetector:
    """Estimate F0 in Hz from a single mono audio frame."""

    def __init__(self, sample_rate: float, config: PitchDetectorConfig | None = None) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self.sample_rate = float(sample_rate)
        self.config = config or PitchDetectorConfig()
        if self.config.min_hz >= self.config.max_hz:
            raise ValueError("min_hz must be lower than max_hz")
        self._decimation = max(1, int(self.sample_rate // 16_000))
        self._effective_rate = self.sample_rate / self._decimation

    def detect(
        self, frame: NDArray[np.floating], monotonic: float, timestamp: datetime
    ) -> PitchMeasurement:
        """Analyse *frame* and return a measurement.

        ``frame`` may be any floating NumPy array; it is converted to a
        one-dimensional ``float64`` array.  The returned duration reflects the
        native frame length divided by the native sample rate.
        """
        samples = np.asarray(frame, dtype=np.float64).reshape(-1)
        native_duration = samples.size / self.sample_rate
        if samples.size == 0:
            return self._result(
                PitchKind.SILENT, None, 0.0, 0.0, monotonic, timestamp, native_duration
            )

        samples = samples - float(np.mean(samples))
        rms = float(np.sqrt(np.mean(samples * samples)))
        if not np.isfinite(rms) or rms < self.config.silence_rms:
            return self._result(
                PitchKind.SILENT, None, rms, 0.0, monotonic, timestamp, native_duration
            )

        zero_crossings = float(np.mean(np.abs(np.diff(np.signbit(samples)))))
        if zero_crossings > self.config.max_zero_crossing_rate:
            return self._result(
                PitchKind.UNVOICED, None, rms, 0.0, monotonic, timestamp, native_duration
            )

        working = self._decimate(samples)
        if working.size < 64:
            return self._result(
                PitchKind.UNVOICED, None, rms, 0.0, monotonic, timestamp, native_duration
            )

        analysis = self._difference(working)
        if analysis is None:
            return self._result(
                PitchKind.UNVOICED, None, rms, 0.0, monotonic, timestamp, native_duration
            )

        tau, confidence = analysis
        hz = self._effective_rate / tau
        if not self.config.min_hz <= hz <= self.config.max_hz:
            return self._result(
                PitchKind.UNVOICED,
                None,
                rms,
                confidence,
                monotonic,
                timestamp,
                native_duration,
            )
        if confidence < self.config.min_confidence:
            return self._result(
                PitchKind.UNVOICED,
                None,
                rms,
                confidence,
                monotonic,
                timestamp,
                native_duration,
            )
        return self._result(
            PitchKind.VOICED, float(hz), rms, confidence, monotonic, timestamp, native_duration
        )

    def _decimate(self, samples: NDArray[np.float64]) -> NDArray[np.float64]:
        factor = self._decimation
        if factor <= 1:
            return samples
        usable = samples.size - (samples.size % factor)
        if usable <= 0:
            return samples
        return samples[:usable].reshape(-1, factor).mean(axis=1)

    def _difference(self, samples: NDArray[np.float64]) -> tuple[float, float] | None:
        min_tau = 2
        max_tau = min(samples.size // 2, int(self._effective_rate / self.config.min_hz))
        if max_tau <= min_tau + 1:
            return None

        window_size = samples.size - max_tau
        windows = np.lib.stride_tricks.sliding_window_view(samples, window_size)
        differences = windows[1 : max_tau + 1] - windows[0]
        squared = np.einsum("ij,ij->i", differences, differences) / window_size
        cumulative = np.cumsum(squared)
        taus = np.arange(1, max_tau + 1, dtype=np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            normalised = np.ones(max_tau + 1, dtype=np.float64)
            normalised[1:] = squared * taus / cumulative
        normalised = np.nan_to_num(normalised, nan=1.0, posinf=1.0, neginf=1.0)

        search = normalised[min_tau : max_tau + 1]
        if search.size == 0:
            return None

        below = np.flatnonzero(search < self.config.yin_threshold)
        if below.size:
            index = int(below[0]) + min_tau
            while index < max_tau and normalised[index + 1] < normalised[index]:
                index += 1
        else:
            local = int(np.argmin(search))
            index = local + min_tau
            if normalised[index] > self.config.fallback_threshold:
                return None

        confidence = float(np.clip(1.0 - normalised[index], 0.0, 1.0))
        refined = float(self._parabolic(normalised, index))
        if refined <= 0:
            return None
        return refined, confidence

    @staticmethod
    def _parabolic(values: NDArray[np.float64], index: int) -> float:
        if index <= 0 or index >= values.size - 1:
            return float(index)
        left, centre, right = values[index - 1], values[index], values[index + 1]
        denominator = left - 2.0 * centre + right
        if abs(denominator) < 1e-12:
            return float(index)
        offset = 0.5 * (left - right) / denominator
        return float(index + float(np.clip(offset, -0.5, 0.5)))

    @staticmethod
    def _result(
        kind: PitchKind,
        hz: float | None,
        rms: float,
        confidence: float,
        monotonic: float,
        timestamp: datetime,
        duration: float,
    ) -> PitchMeasurement:
        return PitchMeasurement(
            monotonic=monotonic,
            timestamp=timestamp,
            hz=hz,
            rms=rms,
            confidence=confidence,
            duration_s=duration,
            kind=kind,
        )
