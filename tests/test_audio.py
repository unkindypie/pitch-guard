"""Audio queue, playback, device resolution, logging and source lifecycle tests."""

from __future__ import annotations

import logging
import sys
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from pitch_guard.audio.capture import (
    MicrophonePitchSource,
    _portaudio_message,
    query_input_devices,
    resolve_device,
)
from pitch_guard.audio.playback import (
    BUZZ_SAMPLE_RATE,
    NullBuzzer,
    SoundDeviceBuzzer,
    make_buzz_waveform,
)
from pitch_guard.audio.queue import MeasurementQueue
from pitch_guard.logging_setup import configure_logging


def test_bounded_queue_drops_oldest() -> None:
    queue: MeasurementQueue[int] = MeasurementQueue(maxsize=3)
    queue.extend([1, 2, 3, 4])
    assert queue.drain() == [2, 3, 4]
    queue.put(5)
    queue.put(6)
    assert len(queue) == 2
    queue.clear()
    assert len(queue) == 0
    with pytest.raises(ValueError):
        MeasurementQueue(maxsize=0)


class FakeSoundDevice(SimpleNamespace):
    """Minimal sounddevice stand-in for playback and device tests."""

    def __init__(self) -> None:
        super().__init__()
        self.played: list[np.ndarray] = []
        self.stopped = 0
        self.default = SimpleNamespace(device=(1, 1))

    def play(self, waveform: np.ndarray, sample_rate: int, blocking: bool = False) -> None:
        self.played.append(waveform)
        assert sample_rate == BUZZ_SAMPLE_RATE
        assert blocking is False

    def stop(self) -> None:
        self.stopped += 1

    def query_devices(self, device: int | None = None) -> Any:
        devices = [
            {"name": "Output", "max_input_channels": 0, "default_samplerate": 48000.0},
            {"name": "Built-in Mic", "max_input_channels": 1, "default_samplerate": 48000.0},
            {"name": "USB Mic", "max_input_channels": 2, "default_samplerate": 16000.0},
        ]
        if device is None:
            return devices
        return devices[device]


def test_buzz_waveform_volume_and_shape() -> None:
    loud = make_buzz_waveform(100)
    quiet = make_buzz_waveform(30)
    muted = make_buzz_waveform(0)
    assert loud.dtype == np.float32
    assert len(loud) == int(BUZZ_SAMPLE_RATE * 0.34)
    assert np.max(np.abs(loud)) <= 0.9
    assert np.max(np.abs(quiet)) < np.max(np.abs(loud))
    assert np.max(np.abs(muted)) == 0.0


def test_sounddevice_buzzer_plays_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSoundDevice()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    buzzer = SoundDeviceBuzzer(80)
    buzzer.play()
    assert fake.played
    buzzer.set_volume(0)
    buzzer.play()
    assert len(fake.played) == 1
    buzzer.close()
    assert fake.stopped == 1


def test_sounddevice_buzzer_handles_missing_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenModule:
        @staticmethod
        def play(*args: object, **kwargs: object) -> None:
            raise OSError("no output")

    monkeypatch.setitem(sys.modules, "sounddevice", BrokenModule())
    buzzer = SoundDeviceBuzzer(70)
    buzzer.play()  # logs a warning, must not raise
    buzzer.close()


def test_null_buzzer_records_play() -> None:
    buzzer = NullBuzzer()
    buzzer.play()
    assert buzzer.play_count == 1
    buzzer.close()


def test_device_enumeration_and_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSoundDevice()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    devices = query_input_devices()
    assert [device.name for device in devices] == ["Built-in Mic", "USB Mic"]
    assert devices[0].default is True
    assert resolve_device(fake, None) == (1, "Built-in Mic")
    assert resolve_device(fake, "USB Mic") == (2, "USB Mic")
    assert resolve_device(fake, "Missing") == (1, "Built-in Mic")


def test_device_enumeration_failure_is_graceful(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", object())
    assert query_input_devices() == []


def test_portaudio_error_message() -> None:
    assert "PortAudio" in _portaudio_message(OSError("PortAudio library not found"))
    assert _portaudio_message(OSError("other")) == "other"


class FakeInputStream:
    def __init__(self, **kwargs: object) -> None:
        self.closed = False
        self.aborted = False
        raw_block_size = kwargs.get("blocksize", 1024)
        self.block_size = raw_block_size if isinstance(raw_block_size, int) else 1024

    def __enter__(self) -> FakeInputStream:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True

    def read(self, frames: int) -> tuple[np.ndarray, bool]:
        time.sleep(0.002)
        return np.zeros((frames, 1), dtype=np.float32), False

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        self.closed = True


class CaptureDevice(FakeSoundDevice):
    def __init__(self) -> None:
        super().__init__()
        self.streams: list[FakeInputStream] = []

    def InputStream(self, **kwargs: object) -> FakeInputStream:
        stream = FakeInputStream(**kwargs)
        self.streams.append(stream)
        return stream


def test_microphone_source_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = CaptureDevice()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    source = MicrophonePitchSource(frame_duration_s=0.02)
    source.start()
    deadline = time.monotonic() + 2.0
    measurements = []
    while time.monotonic() < deadline and not measurements:
        time.sleep(0.01)
        measurements = source.drain()
    source.stop()
    assert measurements
    assert source.description == "Built-in Mic"
    assert source.error is None
    assert source.running is False
    assert fake.streams and fake.streams[0].closed


def test_logging_rotates_to_a_file(tmp_path: Any) -> None:
    log_path = tmp_path / "pitch-guard.log"
    logger = configure_logging(log_path, "DEBUG")
    logger.info("hello")
    for handler in logger.handlers:
        handler.flush()
    assert log_path.exists()
    assert "hello" in log_path.read_text(encoding="utf-8")
    logging.getLogger("pitch_guard").handlers.clear()
