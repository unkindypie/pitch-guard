"""Application-service integration tests with mocked audio and playback."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pitch_guard.aggregation import History
from pitch_guard.config import Config
from pitch_guard.models import RangeStatus
from pitch_guard.persistence import PitchGuardStorage
from pitch_guard.service import PitchGuardService
from pitch_guard.storage import DataPaths
from tests.conftest import FakeSource, make_measurement


def make_service(
    tmp_path: Path,
    source: FakeSource,
    buzzer,
    config: Config | None = None,
    *,
    enabled: bool = True,
) -> PitchGuardService:
    storage = PitchGuardStorage(DataPaths.from_root(tmp_path / ".pitch-guard"), enabled=enabled)
    return PitchGuardService(
        source,
        config or Config(),
        storage,
        History(now=datetime.now(UTC)),
        buzzer,
    )


def push_episode(
    source: FakeSource, start: float, end: float, hz: float, step: float = 0.1
) -> None:
    wall = datetime.now(UTC)
    moment = start
    while moment <= end + 1e-9:
        source.push(
            [
                make_measurement(
                    moment,
                    hz,
                    timestamp=wall + timedelta(seconds=moment),
                )
            ]
        )
        moment = round(moment + step, 10)


def test_service_start_poll_and_single_buzz(tmp_path: Path, source: FakeSource, buzzer) -> None:
    service = make_service(tmp_path, source, buzzer)
    service.start()
    assert source.started == 1
    push_episode(source, 0.0, 3.2, 140.0)
    samples = service.poll()
    assert len(samples) == 33
    assert buzzer.play_count == 1
    assert service.alerts_session == 1
    alerts = service.recent_alerts(
        datetime.now(UTC) - timedelta(minutes=1), datetime.now(UTC) + timedelta(minutes=1)
    )
    assert len(alerts) == 1
    assert alerts[0].hz == pytest.approx(140.0)
    assert service.snapshot().status is RangeStatus.BELOW
    push_episode(source, 3.3, 6.0, 140.0)
    service.poll()
    assert buzzer.play_count == 1


def test_service_bridges_brief_speech_gaps(tmp_path: Path, source: FakeSource, buzzer) -> None:
    service = make_service(tmp_path, source, buzzer)
    service.start()
    wall = datetime.now(UTC)
    measurements = []
    for index in range(11):
        measurements.append(
            make_measurement(index * 0.1, 140.0, timestamp=wall + timedelta(seconds=index * 0.1))
        )
    for index in range(11, 16):
        measurements.append(
            make_measurement(index * 0.1, None, timestamp=wall + timedelta(seconds=index * 0.1))
        )
    for index in range(16, 36):
        measurements.append(
            make_measurement(index * 0.1, 140.0, timestamp=wall + timedelta(seconds=index * 0.1))
        )
    source.push(measurements)
    service.poll()
    assert buzzer.play_count == 1


def test_service_ignores_silence_for_alert(tmp_path: Path, source: FakeSource, buzzer) -> None:
    service = make_service(tmp_path, source, buzzer)
    service.start()
    wall = datetime.now(UTC)
    for index in range(40):
        source.push(
            [make_measurement(index * 0.1, None, timestamp=wall + timedelta(seconds=index * 0.1))]
        )
    service.poll()
    assert buzzer.play_count == 0
    assert service.snapshot().status is RangeStatus.SILENT


def test_pause_and_resume(tmp_path: Path, source: FakeSource, buzzer) -> None:
    service = make_service(tmp_path, source, buzzer)
    service.start()
    service.pause()
    assert service.paused is True
    assert source.stopped >= 1
    assert service.snapshot().status is RangeStatus.PAUSED
    service.resume()
    assert service.paused is False
    assert source.started == 2


def test_toggle_pause(tmp_path: Path, source: FakeSource, buzzer) -> None:
    service = make_service(tmp_path, source, buzzer)
    service.start()
    service.toggle_pause()
    assert service.paused
    service.toggle_pause()
    assert not service.paused


def test_apply_config_updates_alert_volume_and_persists(
    tmp_path: Path, source: FakeSource, buzzer
) -> None:
    service = make_service(tmp_path, source, buzzer)
    new = Config(min_hz=180.0, max_hz=260.0, alert_duration_s=2.0, volume=33)
    applied = service.apply_config(new)
    assert applied.alert_duration_s == 2.0
    assert service.alert.duration_s == 2.0
    assert buzzer.volume == 33
    assert service.storage.paths.config.exists()


def test_stop_saves_history_and_closes_audio(tmp_path: Path, source: FakeSource, buzzer) -> None:
    service = make_service(tmp_path, source, buzzer)
    service.start()
    push_episode(source, 0.0, 1.0, 200.0)
    service.stop()
    assert source.stopped >= 1
    assert buzzer.closed is True
    assert service.storage.paths.history.exists()
    assert service.storage.paths.daily_stats.exists()


def test_set_source_switches_capture(tmp_path: Path, source: FakeSource, buzzer) -> None:
    service = make_service(tmp_path, source, buzzer)
    service.start()
    replacement = FakeSource("USB microphone")
    service.set_source(replacement, Config())
    assert source.stopped >= 1
    assert replacement.started == 1
    assert service.snapshot().source_description == "USB microphone"


def test_source_error_is_exposed(tmp_path: Path, source: FakeSource, buzzer) -> None:
    service = make_service(tmp_path, source, buzzer)
    source.error = "No microphone"
    service.start()
    snapshot = service.snapshot()
    assert snapshot.source_error == "No microphone"


def test_stop_can_be_called_twice(tmp_path: Path, source: FakeSource, buzzer) -> None:
    service = make_service(tmp_path, source, buzzer)
    service.start()
    service.stop()
    service.stop()
    assert service.storage.paths.history.exists()


def test_playback_failure_does_not_stop_monitoring(tmp_path: Path, source: FakeSource) -> None:
    class BrokenBuzzer:
        def play(self) -> None:
            raise RuntimeError("output unavailable")

        def close(self) -> None:
            pass

    service = make_service(tmp_path, source, BrokenBuzzer())
    service.start()
    push_episode(source, 0.0, 3.2, 140.0)
    samples = service.poll()
    assert len(samples) == 33
    assert service.alerts_session == 1
