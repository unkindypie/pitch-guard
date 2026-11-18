"""Shared deterministic fixtures and fakes for Pitch-Guard tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pitch_guard.config import Config
from pitch_guard.models import ClassifiedSample, PitchKind, PitchMeasurement, RangeStatus
from pitch_guard.storage import DataPaths

BASE_TIME = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def make_measurement(
    monotonic: float,
    hz: float | None,
    *,
    kind: PitchKind | None = None,
    duration_s: float = 0.1,
    timestamp: datetime | None = None,
) -> PitchMeasurement:
    """Build a physical pitch measurement for tests."""
    if kind is None:
        kind = PitchKind.VOICED if hz is not None else PitchKind.SILENT
    return PitchMeasurement(
        monotonic=monotonic,
        timestamp=timestamp or BASE_TIME + timedelta(seconds=monotonic),
        hz=hz,
        rms=0.05 if hz is not None else 0.001,
        confidence=0.95 if hz is not None else 0.0,
        duration_s=duration_s,
        kind=kind,
    )


def make_sample(
    monotonic: float,
    hz: float | None,
    *,
    status: RangeStatus | None = None,
    duration_s: float = 0.1,
    min_hz: float = 170.0,
    max_hz: float = 250.0,
    timestamp: datetime | None = None,
) -> ClassifiedSample:
    """Build a classified sample with sensible range defaults."""
    if status is None:
        if hz is None:
            status = RangeStatus.SILENT
        elif hz < min_hz:
            status = RangeStatus.BELOW
        elif hz > max_hz:
            status = RangeStatus.ABOVE
        else:
            status = RangeStatus.IN_RANGE
    return ClassifiedSample(
        monotonic=monotonic,
        timestamp=timestamp or BASE_TIME + timedelta(seconds=monotonic),
        hz=hz,
        rms=0.05 if hz is not None else 0.001,
        confidence=0.95 if hz is not None else 0.0,
        duration_s=duration_s,
        status=status,
        min_hz=min_hz,
        max_hz=max_hz,
    )


class FakeBuzzer:
    """Record alert playback instead of touching an audio device."""

    def __init__(self) -> None:
        self.play_count = 0
        self.closed = False
        self.volume = 70

    def play(self) -> None:
        self.play_count += 1

    def close(self) -> None:
        self.closed = True

    def set_volume(self, volume: int) -> None:
        self.volume = volume


class FakeSource:
    """A manually driven pitch source."""

    def __init__(self, description: str = "Test microphone") -> None:
        self._description = description
        self.error: str | None = None
        self.running = False
        self.started = 0
        self.stopped = 0
        self._measurements: list[PitchMeasurement] = []

    @property
    def description(self) -> str:
        return self._description

    def start(self) -> None:
        self.started += 1
        self.running = True

    def stop(self) -> None:
        self.stopped += 1
        self.running = False

    def drain(self) -> list[PitchMeasurement]:
        measurements = list(self._measurements)
        self._measurements.clear()
        return measurements

    def push(self, measurements: Sequence[PitchMeasurement]) -> None:
        self._measurements.extend(measurements)


@pytest.fixture
def config() -> Config:
    """Default configuration used by most tests."""
    return Config()


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    """Isolated data directory."""
    return DataPaths.from_root(tmp_path / ".pitch-guard")


@pytest.fixture
def buzzer() -> FakeBuzzer:
    """Recording buzzer."""
    return FakeBuzzer()


@pytest.fixture
def source() -> FakeSource:
    """Manual source."""
    return FakeSource()
