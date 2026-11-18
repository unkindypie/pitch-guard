"""Pitch detection and classification tests."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from numpy.typing import NDArray

from pitch_guard.audio.pitch import PitchDetector
from pitch_guard.models import PitchKind, RangeStatus, classify_measurement

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def sine_frame(sample_rate: int, hz: float, amplitude: float = 0.05) -> NDArray[np.float64]:
    count = int(sample_rate * 0.064)
    time_axis = np.arange(count, dtype=np.float64) / sample_rate
    return amplitude * np.sin(2.0 * np.pi * hz * time_axis)


@pytest.mark.parametrize("sample_rate", [16_000, 44_100, 48_000])
@pytest.mark.parametrize("hz", [90.0, 100.0, 170.0, 200.0, 250.0, 300.0, 450.0])
def test_detects_sine_frequency(sample_rate: int, hz: float) -> None:
    detector = PitchDetector(sample_rate)
    result = detector.detect(sine_frame(sample_rate, hz), 1.0, NOW)
    assert result.kind is PitchKind.VOICED
    assert result.hz == pytest.approx(hz, rel=0.015)
    assert result.confidence > 0.9
    assert result.duration_s == pytest.approx(0.064, abs=0.002)


@pytest.mark.parametrize("sample_rate", [16_000, 48_000])
def test_silence_and_quiet_frames_are_ignored(sample_rate: int) -> None:
    detector = PitchDetector(sample_rate)
    silent = detector.detect(np.zeros(int(sample_rate * 0.064)), 0.0, NOW)
    quiet = detector.detect(sine_frame(sample_rate, 200.0, amplitude=0.0005), 0.0, NOW)
    assert silent.kind is PitchKind.SILENT
    assert silent.hz is None
    assert quiet.kind is PitchKind.SILENT
    assert quiet.hz is None


def test_white_noise_is_unvoiced() -> None:
    detector = PitchDetector(48_000)
    noise = np.random.default_rng(11).normal(0.0, 0.05, int(48_000 * 0.064))
    result = detector.detect(noise, 0.0, NOW)
    assert result.kind is PitchKind.UNVOICED
    assert result.hz is None


def test_out_of_detector_range_is_rejected() -> None:
    detector = PitchDetector(16_000)
    high = detector.detect(sine_frame(16_000, 800.0), 0.0, NOW)
    assert high.hz is None
    assert high.kind is PitchKind.UNVOICED


def test_frame_may_be_multidimensional() -> None:
    detector = PitchDetector(16_000)
    frame = sine_frame(16_000, 210.0).reshape(-1, 1)
    result = detector.detect(frame, 0.0, NOW)
    assert result.kind is PitchKind.VOICED
    assert result.hz == pytest.approx(210.0, rel=0.02)


@pytest.mark.parametrize(
    ("hz", "expected"),
    [
        (169.9, RangeStatus.BELOW),
        (170.0, RangeStatus.IN_RANGE),
        (250.0, RangeStatus.IN_RANGE),
        (250.1, RangeStatus.ABOVE),
    ],
)
def test_range_classification_is_inclusive(hz: float, expected: RangeStatus) -> None:
    measurement = _measurement(hz)
    assert classify_measurement(measurement, 170.0, 250.0).status is expected


def test_silence_and_unvoiced_classification() -> None:
    silent = _measurement(None, PitchKind.SILENT)
    unvoiced = _measurement(None, PitchKind.UNVOICED)
    assert classify_measurement(silent, 170.0, 250.0).status is RangeStatus.SILENT
    assert classify_measurement(unvoiced, 170.0, 250.0).status is RangeStatus.UNVOICED


def _measurement(hz: float | None, kind: PitchKind | None = None):
    from pitch_guard.models import PitchMeasurement

    return PitchMeasurement(
        monotonic=0.0,
        timestamp=NOW,
        hz=hz,
        rms=0.05,
        confidence=0.9,
        duration_s=0.1,
        kind=kind or (PitchKind.VOICED if hz is not None else PitchKind.SILENT),
    )
