"""Microphone capture and the source abstraction used by the application."""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from pitch_guard.audio.pitch import PitchDetector, PitchDetectorConfig
from pitch_guard.audio.queue import MeasurementQueue
from pitch_guard.models import PitchMeasurement

LOG = logging.getLogger("pitch_guard.audio")


class PitchSource(Protocol):
    """A source of pitch measurements consumed by the TUI."""

    @property
    def description(self) -> str:
        """Human-readable source name shown on the dashboard."""

    @property
    def error(self) -> str | None:
        """Latest user-facing error, if the source is unavailable."""

    @property
    def running(self) -> bool:
        """Whether the source worker is active."""

    def start(self) -> None:
        """Begin producing measurements."""

    def stop(self) -> None:
        """Stop producing measurements and release audio resources."""

    def drain(self) -> list[PitchMeasurement]:
        """Return measurements produced since the previous drain."""


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """An input device reported by the audio backend."""

    index: int
    name: str
    channels: int
    default: bool

    @property
    def label(self) -> str:
        """Display label for the settings screen."""
        suffix = " (default)" if self.default else ""
        return f"{self.name}{suffix}"


class ThreadedPitchSource(ABC):
    """Common bounded-queue and lifecycle handling for pitch sources."""

    def __init__(self, max_queue: int = 64) -> None:
        self._queue: MeasurementQueue[PitchMeasurement] = MeasurementQueue(max_queue)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = threading.Event()
        self._error: str | None = None
        self._error_lock = threading.Lock()

    @property
    def error(self) -> str | None:
        with self._error_lock:
            return self._error

    def _set_error(self, message: str | None) -> None:
        with self._error_lock:
            self._error = message

    @property
    def running(self) -> bool:
        return self._running.is_set()

    def start(self) -> None:
        """Start the worker thread once."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._set_error(None)
        self._thread = threading.Thread(
            target=self._run_guarded,
            name=f"pitch-guard-{type(self).__name__}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the worker and wait briefly for a clean exit.

        The worker normally notices the stop event after the current audio block
        and closes its own stream.  Forcing the stream closed from this thread
        while a read is active can make PortAudio's ALSA backend print errors
        during shutdown, so that is only a fallback for a wedged device.
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
            if thread.is_alive():
                self._release()
                thread.join(timeout=1.0)
        self._thread = None
        self._running.clear()

    def _run_guarded(self) -> None:
        self._running.set()
        try:
            self._run()
        except Exception as error:
            LOG.exception("Pitch source stopped unexpectedly")
            self._set_error(str(error) or type(error).__name__)
        finally:
            self._running.clear()
            self._release()

    @abstractmethod
    def _run(self) -> None:
        """Worker loop implementation."""

    def _release(self) -> None:
        """Release backend resources; safe to call repeatedly."""
        return

    def drain(self) -> list[PitchMeasurement]:
        """Drain queued measurements."""
        return self._queue.drain()


class MicrophonePitchSource(ThreadedPitchSource):
    """Capture mono microphone audio and estimate pitch in a worker thread."""

    def __init__(
        self,
        device: str | None = None,
        *,
        frame_duration_s: float = 0.064,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__()
        self._device_setting = device
        self._frame_duration_s = frame_duration_s
        self._log = logger or LOG
        self._description = "System default microphone"
        self._stream: object | None = None
        self._stream_lock = threading.Lock()

    @property
    def description(self) -> str:
        return self._description

    def _release(self) -> None:
        with self._stream_lock:
            stream = self._stream
            self._stream = None
        if stream is None:
            return
        for method_name in ("abort", "close"):
            method = getattr(stream, method_name, None)
            if method is None:
                continue
            try:
                method()
            except Exception:  # noqa: BLE001 - audio backend errors vary
                self._log.debug("Could not close audio stream", exc_info=True)
            break

    def _run(self) -> None:
        try:
            import sounddevice as sd
        except Exception as error:  # noqa: BLE001 - audio backend errors vary
            self._set_error(_portaudio_message(error))
            self._log.error("Microphone unavailable: %s", error)
            return

        try:
            index, label = resolve_device(sd, self._device_setting)
            info = sd.query_devices(index)
            sample_rate = float(info["default_samplerate"])
            frame_samples = max(256, int(sample_rate * self._frame_duration_s))
            detector = PitchDetector(sample_rate, PitchDetectorConfig())
            self._description = label
            self._log.info("Capturing from %s at %.0f Hz", label, sample_rate)
            stream = sd.InputStream(
                device=index,
                channels=1,
                samplerate=sample_rate,
                dtype="float32",
                blocksize=frame_samples,
            )
            with self._stream_lock:
                self._stream = stream
            try:
                with stream:
                    while not self._stop_event.is_set():
                        data, overflowed = stream.read(frame_samples)
                        if overflowed:
                            self._log.debug("Audio input overflow; dropping frame")
                        if self._stop_event.is_set():
                            break
                        frame = data[:, 0] if data.ndim > 1 else data
                        measurement = detector.detect(
                            frame,
                            monotonic=time.monotonic(),
                            timestamp=datetime.now(UTC),
                        )
                        self._queue.put(measurement)
            finally:
                with self._stream_lock:
                    if self._stream is stream:
                        self._stream = None
        except Exception as error:  # noqa: BLE001 - audio backend errors vary
            if not self._stop_event.is_set():
                self._set_error(_portaudio_message(error))
                self._log.exception("Microphone capture failed")
        finally:
            self._release()


def query_input_devices() -> list[DeviceInfo]:
    """List input devices without raising when audio support is unavailable."""
    try:
        import sounddevice as sd
    except Exception:  # noqa: BLE001 - audio backend errors vary
        return []
    try:
        raw_devices = sd.query_devices()
        default_index = _default_input_index(sd, raw_devices)
        devices: list[DeviceInfo] = []
        for index, device in enumerate(raw_devices):
            channels = int(device["max_input_channels"])
            if channels <= 0:
                continue
            devices.append(
                DeviceInfo(
                    index=index,
                    name=str(device["name"]),
                    channels=channels,
                    default=index == default_index,
                )
            )
        return devices
    except Exception:
        LOG.debug("Could not enumerate audio devices", exc_info=True)
        return []


def resolve_device(sd: Any, setting: str | None) -> tuple[int, str]:
    """Resolve a stored device name to a current PortAudio index.

    Device indices are not stable between launches, so the configuration stores
    the device name.  Missing or disconnected devices fall back to the system
    default with a log message rather than preventing the app from starting.
    """
    devices = sd.query_devices()
    default_index = _default_input_index(sd, devices)
    if setting:
        for index, device in enumerate(devices):
            if int(device["max_input_channels"]) > 0 and str(device["name"]) == setting:
                return index, str(device["name"])
        LOG.warning("Configured microphone %r was not found; using the default", setting)
    if default_index is not None:
        return default_index, str(devices[default_index]["name"])
    for index, device in enumerate(devices):
        if int(device["max_input_channels"]) > 0:
            return index, str(device["name"])
    raise RuntimeError("No microphone input device is available")


def _default_input_index(sd: Any, devices: Any) -> int | None:
    try:
        default = sd.default.device
        candidate = default[0] if isinstance(default, (list, tuple)) else default
        index = int(candidate)
        if index >= 0 and int(devices[index]["max_input_channels"]) > 0:
            return index
    except (TypeError, ValueError, IndexError, KeyError, AttributeError):
        pass
    try:
        for index, device in enumerate(devices):
            if int(device["max_input_channels"]) > 0:
                return index
    except (TypeError, KeyError):
        return None
    return None


def _portaudio_message(error: BaseException) -> str:
    text = str(error)
    if "PortAudio" in text:
        return (
            "PortAudio is not available. Install libportaudio2 (Debian/Ubuntu), "
            "portaudio (Fedora), or use a Python wheel that bundles it."
        )
    return text or type(error).__name__
