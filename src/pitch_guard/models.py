"""Shared data types for Pitch-Guard.

The audio layer produces :class:`PitchMeasurement` objects.  The application
classifies each measurement against the active target range before feeding it
to the alert tracker and statistics engine.  Keeping the audio measurement
separate from its range classification makes pitch detection independently
testable and lets historical data preserve the thresholds active at capture
time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PitchKind(StrEnum):
    """Physical interpretation of one analysed audio frame."""

    VOICED = "voiced"
    SILENT = "silent"
    UNVOICED = "unvoiced"


class RangeStatus(StrEnum):
    """A voiced pitch measurement classified against a target range."""

    IN_RANGE = "in_range"
    BELOW = "below"
    ABOVE = "above"
    SILENT = "silent"
    UNVOICED = "unvoiced"
    PAUSED = "paused"

    @property
    def is_voiced(self) -> bool:
        """Whether the measurement contains a usable voice pitch."""
        return self in (RangeStatus.IN_RANGE, RangeStatus.BELOW, RangeStatus.ABOVE)

    @property
    def is_out_of_range(self) -> bool:
        """Whether the measurement is voiced but outside the target range."""
        return self in (RangeStatus.BELOW, RangeStatus.ABOVE)

    @property
    def label(self) -> str:
        """Human-readable dashboard label."""
        return {
            RangeStatus.IN_RANGE: "IN RANGE",
            RangeStatus.BELOW: "BELOW RANGE",
            RangeStatus.ABOVE: "ABOVE RANGE",
            RangeStatus.SILENT: "SILENCE",
            RangeStatus.UNVOICED: "UNVOICED",
            RangeStatus.PAUSED: "PAUSED",
        }[self]


@dataclass(frozen=True, slots=True)
class PitchMeasurement:
    """One pitch estimate from an audio frame.

    ``monotonic`` is used for all timing decisions.  ``timestamp`` is the
    wall-clock time used only for historical aggregation, so it is timezone
    aware and stored in UTC by the persistence layer.

    No sample data or raw audio is retained in this object.
    """

    monotonic: float
    timestamp: datetime
    hz: float | None
    rms: float
    confidence: float
    duration_s: float
    kind: PitchKind

    @property
    def voiced(self) -> bool:
        """Whether a reliable fundamental frequency was found."""
        return self.kind is PitchKind.VOICED and self.hz is not None


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """A wall-clock alert marker for chart rendering."""

    timestamp: datetime
    hz: float


@dataclass(frozen=True, slots=True)
class ClassifiedSample:
    """A pitch measurement with its target-range classification."""

    monotonic: float
    timestamp: datetime
    hz: float | None
    rms: float
    confidence: float
    duration_s: float
    status: RangeStatus
    min_hz: float
    max_hz: float

    @property
    def voiced(self) -> bool:
        """Whether the sample contains a usable voiced pitch."""
        return self.status.is_voiced and self.hz is not None

    @property
    def out_of_range(self) -> bool:
        """Whether the sample is voiced outside the target range."""
        return self.status.is_out_of_range


def classify_measurement(
    measurement: PitchMeasurement, min_hz: float, max_hz: float
) -> ClassifiedSample:
    """Attach a range classification to a physical pitch measurement."""
    if measurement.kind is PitchKind.SILENT:
        status = RangeStatus.SILENT
    elif measurement.kind is PitchKind.UNVOICED or measurement.hz is None:
        status = RangeStatus.UNVOICED
    elif measurement.hz < min_hz:
        status = RangeStatus.BELOW
    elif measurement.hz > max_hz:
        status = RangeStatus.ABOVE
    else:
        status = RangeStatus.IN_RANGE
    return ClassifiedSample(
        monotonic=measurement.monotonic,
        timestamp=measurement.timestamp,
        hz=measurement.hz,
        rms=measurement.rms,
        confidence=measurement.confidence,
        duration_s=measurement.duration_s,
        status=status,
        min_hz=min_hz,
        max_hz=max_hz,
    )
