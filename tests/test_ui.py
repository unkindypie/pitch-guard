"""Textual dashboard, settings and keyboard-navigation tests."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from textual.widgets import Input, Select, Static

from pitch_guard.aggregation import History
from pitch_guard.config import Config
from pitch_guard.persistence import PitchGuardStorage
from pitch_guard.service import PitchGuardService
from pitch_guard.storage import DataPaths
from pitch_guard.ui.app import PITCH_GUARD_THEME, PitchGuardApp
from pitch_guard.ui.chart import PitchChart
from pitch_guard.ui.settings import SettingsScreen
from tests.conftest import FakeBuzzer, FakeSource, make_measurement


def build_app(
    tmp_path: Path,
    *,
    ascii_only: bool = False,
    config: Config | None = None,
) -> tuple[PitchGuardApp, FakeSource, FakeBuzzer, PitchGuardService]:
    source = FakeSource()
    buzzer = FakeBuzzer()
    storage = PitchGuardStorage(DataPaths.from_root(tmp_path / ".pitch-guard"))
    service = PitchGuardService(
        source,
        config or Config(),
        storage,
        History(now=datetime.now(UTC)),
        buzzer,
    )
    app = PitchGuardApp(service, ascii_only=ascii_only)
    return app, source, buzzer, service


def push_samples(source: FakeSource, count: int = 20, hz: float = 200.0) -> None:
    now_mono = time.monotonic()
    now_wall = datetime.now(UTC)
    source.push(
        [
            make_measurement(
                now_mono - count * 0.1 + index * 0.1,
                hz,
                duration_s=0.1,
                timestamp=now_wall + timedelta(seconds=index * 0.1 - count * 0.1),
            )
            for index in range(count)
        ]
    )


@pytest.mark.asyncio
async def test_dashboard_shows_pitch_and_range(tmp_path: Path) -> None:
    app, source, _buzzer, service = build_app(tmp_path)
    async with app.run_test(size=(110, 34)) as pilot:
        assert source.started == 1
        assert app.ansi_color is True
        assert PITCH_GUARD_THEME.primary == "#b85c86"
        push_samples(source)
        await pilot.pause(1.2)
        current = app.query_one("#metric-0", Static).content
        status = app.query_one("#metric-2", Static).content
        assert "200 Hz" in str(current)
        assert "IN RANGE" in str(status)
        assert service.snapshot().status.value == "in_range"


@pytest.mark.asyncio
async def test_period_shortcuts_and_chart_header(tmp_path: Path) -> None:
    app, _source, _buzzer, _service = build_app(tmp_path)
    async with app.run_test(size=(110, 34)) as pilot:
        expected = {"1": "live", "2": "day", "3": "week", "4": "month", "5": "90d"}
        for key, period in expected.items():
            await pilot.press(key)
            await pilot.pause(0.1)
            assert app.period.value == period
            header = str(app.query_one("#chart-title", Static).content)
            assert app.period.label in header


@pytest.mark.asyncio
async def test_space_pauses_and_resumes(tmp_path: Path) -> None:
    app, source, _buzzer, service = build_app(tmp_path)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("space")
        await pilot.pause(0.2)
        assert service.paused is True
        assert source.running is False
        assert "PAUSED" in str(app.query_one("#metric-2", Static).content)
        await pilot.press("space")
        await pilot.pause(0.2)
        assert service.paused is False


@pytest.mark.asyncio
async def test_uppercase_shortcuts(tmp_path: Path) -> None:
    app, _source, _buzzer, _service = build_app(tmp_path)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("S")
        await pilot.pause(0.2)
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("escape")
        await pilot.pause(0.2)
        await pilot.press("Q")
        await pilot.pause(0.4)
        assert app._shutdown_done is True


@pytest.mark.asyncio
async def test_settings_opens_and_escape_returns(tmp_path: Path) -> None:
    app, _source, _buzzer, _service = build_app(tmp_path)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("s")
        await pilot.pause(0.2)
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, SettingsScreen)


@pytest.mark.asyncio
async def test_settings_edit_is_validated_and_saved(tmp_path: Path) -> None:
    app, _source, _buzzer, service = build_app(tmp_path)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("s")
        await pilot.pause(0.2)
        settings = app.screen
        assert isinstance(settings, SettingsScreen)
        min_input = settings.query_one("#min-hz", Input)
        min_input.focus()
        min_input.value = "180"
        await pilot.pause(0.2)
        assert service.config.min_hz == 180.0
        assert service.config.preset == "custom"
        assert service.storage.paths.config.exists()


@pytest.mark.asyncio
async def test_invalid_settings_are_not_saved(tmp_path: Path) -> None:
    app, _source, _buzzer, service = build_app(tmp_path)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("s")
        await pilot.pause(0.2)
        settings = app.screen
        assert isinstance(settings, SettingsScreen)
        min_input = settings.query_one("#min-hz", Input)
        min_input.focus()
        min_input.value = "300"
        await pilot.pause(0.2)
        # 300 is above the current max, so the last valid value stays active.
        assert service.config.min_hz == 170.0
        status = str(settings.query_one("#settings-status", Static).content)
        assert "lower" in status


@pytest.mark.asyncio
async def test_shortcuts_do_not_interfere_with_input_editing(tmp_path: Path) -> None:
    app, _source, _buzzer, _service = build_app(tmp_path)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("s")
        await pilot.pause(0.2)
        settings = app.screen
        assert isinstance(settings, SettingsScreen)
        volume = settings.query_one("#volume", Input)
        volume.focus()
        await pilot.press("1")
        await pilot.press("q")
        await pilot.pause(0.1)
        assert isinstance(app.screen, SettingsScreen)  # q did not quit
        assert app.period.value == "live"  # 1 did not switch charts


@pytest.mark.asyncio
async def test_tab_and_shift_tab_move_focus(tmp_path: Path) -> None:
    app, _source, _buzzer, _service = build_app(tmp_path)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("s")
        await pilot.pause(0.2)
        first = app.focused
        await pilot.press("tab")
        await pilot.pause(0.1)
        second = app.focused
        assert second is not None and second is not first
        await pilot.press("shift+tab")
        await pilot.pause(0.1)
        assert app.focused is first


@pytest.mark.asyncio
async def test_preset_selection_updates_range(tmp_path: Path) -> None:
    app, _source, _buzzer, service = build_app(tmp_path)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("s")
        await pilot.pause(0.2)
        settings = app.screen
        assert isinstance(settings, SettingsScreen)
        select = settings.query_one("#preset", Select)
        select.focus()
        select.value = "low"
        await pilot.pause(0.2)
        assert service.config.min_hz == 100.0
        assert service.config.max_hz == 180.0
        assert settings.query_one("#min-hz", Input).value == "100"


@pytest.mark.asyncio
async def test_arrow_keys_and_enter_navigate_settings(tmp_path: Path) -> None:
    app, _source, _buzzer, service = build_app(tmp_path)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.press("s")
        await pilot.pause(0.2)
        settings = app.screen
        assert isinstance(settings, SettingsScreen)
        select = settings.query_one("#preset", Select)
        select.focus()
        await pilot.press("down")  # open the menu
        await pilot.pause(0.1)
        await pilot.press("down")  # move to the next preset
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert select.value == "low"
        assert service.config.min_hz == 100.0
        assert service.config.max_hz == 180.0


@pytest.mark.asyncio
async def test_calibrate_to_current_voice_sets_custom_range(tmp_path: Path) -> None:
    app, source, _buzzer, service = build_app(tmp_path)
    async with app.run_test(size=(110, 34)) as pilot:
        push_samples(source, count=30, hz=128.0)
        await pilot.pause(0.4)
        config = app._calibrate_to_voice()
        assert config is not None
        assert config.preset == "custom"
        assert config.min_hz < 128.0 < config.max_hz
        assert service.config.min_hz == config.min_hz
        assert service.storage.paths.config.exists()


@pytest.mark.asyncio
async def test_calibration_requires_live_speech(tmp_path: Path) -> None:
    app, _source, _buzzer, _service = build_app(tmp_path)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause(0.2)
        assert app._calibrate_to_voice() is None


@pytest.mark.asyncio
async def test_settings_calibrate_and_test_buzz_buttons(tmp_path: Path) -> None:
    app, source, buzzer, service = build_app(tmp_path)
    async with app.run_test(size=(110, 34)) as pilot:
        push_samples(source, count=30, hz=140.0)
        await pilot.pause(0.4)
        await pilot.press("s")
        await pilot.pause(0.2)
        await pilot.click("#calibrate")
        await pilot.pause(0.2)
        assert service.config.preset == "custom"
        assert service.config.min_hz < 140.0 < service.config.max_hz
        before = buzzer.play_count
        await pilot.click("#test-buzz")
        await pilot.pause(0.1)
        assert buzzer.play_count == before + 1


@pytest.mark.asyncio
async def test_q_quits_cleanly_and_stops_service(tmp_path: Path) -> None:
    app, source, buzzer, service = build_app(tmp_path)
    async with app.run_test(size=(110, 34)) as pilot:
        push_samples(source, 10)
        await pilot.pause(0.3)
        await pilot.press("q")
        await pilot.pause(0.5)
        assert app._shutdown_done is True
        assert source.stopped >= 1
        assert buzzer.closed is True
        assert service.storage.paths.history.exists()


@pytest.mark.asyncio
async def test_idle_mode_pauses_analysis_and_shows_overlay(tmp_path: Path) -> None:
    app, source, _buzzer, service = build_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app._last_activity = time.monotonic() - 61.0
        app._check_idle()
        await pilot.pause(0.3)
        assert app._idle_mode is True
        assert service.paused is True
        assert source.running is False
        assert app.screen.has_class("idle")
        overlay = str(app.screen.query_one("#idle-overlay", Static).content)
        assert "IDLE" in overlay or "██" in overlay
        assert "Press any key" in overlay or "resume" in overlay
        assert "IDLE" in str(app.query_one("#status", Static).content)
        assert app.screen.query_one("#stats-panel").display is False
        await pilot.press("a")
        await pilot.pause(0.3)
        assert app._idle_mode is False
        assert service.paused is False
        assert source.running is True
        assert not app.screen.has_class("idle")


@pytest.mark.asyncio
async def test_idle_mode_space_resumes_without_repausing(tmp_path: Path) -> None:
    app, _source, _buzzer, service = build_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        app._last_activity = time.monotonic() - 61.0
        app._check_idle()
        await pilot.pause(0.2)
        assert app._idle_mode
        await pilot.press("space")
        await pilot.pause(0.2)
        assert app._idle_mode is False
        assert service.paused is False


@pytest.mark.asyncio
async def test_idle_mode_does_not_override_manual_pause(tmp_path: Path) -> None:
    app, _source, _buzzer, service = build_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        service.pause()
        app._last_activity = time.monotonic() - 61.0
        app._check_idle()
        await pilot.pause(0.1)
        assert app._idle_mode is False
        assert service.paused is True


@pytest.mark.asyncio
async def test_narrow_terminal_uses_fallback_and_narrow_class(tmp_path: Path) -> None:
    app, _source, _buzzer, _service = build_app(tmp_path)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause(0.5)
        assert app.screen.has_class("narrow")
        await pilot.pause(0.5)
        chart = app.query_one("#chart", PitchChart)
        rendered = chart.render().plain
        assert "Chart hidden at this size" in rendered


@pytest.mark.asyncio
async def test_ascii_mode_marks_screen_and_chart(tmp_path: Path) -> None:
    app, _source, _buzzer, _service = build_app(tmp_path, ascii_only=True)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.3)
        assert app.screen.has_class("ascii")
        chart = app.query_one("#chart", PitchChart)
        rendered = chart.render().plain
        assert "─" not in rendered
