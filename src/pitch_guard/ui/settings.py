"""Interactive settings modal with automatic persistence."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from pitch_guard.audio.capture import DeviceInfo
from pitch_guard.config import (
    PITCH_PRESETS,
    PRESET_CUSTOM,
    PRESET_LABELS,
    Config,
    ConfigError,
)


class SettingsScreen(ModalScreen[Config | None]):
    """Modal settings editor returning the latest valid configuration."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss_settings", "Back", show=False),
        Binding("q", "quit_app", "Quit", show=False),
        Binding("Q", "quit_app", "Quit", show=False),
        Binding("ctrl+s", "done", "Save", show=False, priority=True),
        Binding("tab", "focus_next_field", "Next", show=False, priority=True),
        Binding("shift+tab", "focus_previous_field", "Previous", show=False, priority=True),
    ]

    def __init__(
        self,
        config: Config,
        devices: list[DeviceInfo],
        *,
        demo: bool,
        on_change: Callable[[Config], None],
        on_test_buzz: Callable[[], None],
        on_calibrate: Callable[[], Config | None],
    ) -> None:
        super().__init__()
        self._config = config
        self._valid_config = config
        self._devices = devices
        self._demo = demo
        self._on_change = on_change
        self._on_test_buzz = on_test_buzz
        self._on_calibrate = on_calibrate

    def compose(self) -> ComposeResult:
        preset_options = [
            (PRESET_LABELS[preset], preset) for preset in (*PITCH_PRESETS.keys(), PRESET_CUSTOM)
        ]
        microphone_options: list[tuple[str, str]]
        if self._demo:
            microphone_options = [("Demo source (simulated)", "__demo__")]
        else:
            microphone_options = [("System default", "__default__")]
            names = {device.name for device in self._devices}
            microphone_options.extend((device.label, device.name) for device in self._devices)
            if self._config.microphone and self._config.microphone not in names:
                microphone_options.append(
                    (f"{self._config.microphone} (unavailable)", self._config.microphone)
                )
        microphone_value = "__demo__" if self._demo else (self._config.microphone or "__default__")

        with Vertical(id="settings-dialog"):
            yield Static("Settings", id="settings-title")
            yield Static("Changes are validated and saved automatically.", id="settings-subtitle")
            with Vertical(id="settings-form"):
                yield _row(
                    "Preset",
                    Select(
                        preset_options,
                        value=self._config.preset,
                        id="preset",
                        allow_blank=False,
                        compact=True,
                    ),
                )
                yield _row(
                    "Min pitch (Hz)",
                    Input(
                        _format_number(self._config.min_hz),
                        id="min-hz",
                        type="number",
                        select_on_focus=True,
                        compact=True,
                    ),
                )
                yield _row(
                    "Max pitch (Hz)",
                    Input(
                        _format_number(self._config.max_hz),
                        id="max-hz",
                        type="number",
                        select_on_focus=True,
                        compact=True,
                    ),
                )
                yield _row(
                    "Alert after (s)",
                    Input(
                        _format_number(self._config.alert_duration_s),
                        id="alert-duration",
                        type="number",
                        select_on_focus=True,
                        compact=True,
                    ),
                )
                yield _row(
                    "Cooldown (s)",
                    Input(
                        _format_number(self._config.alert_cooldown_s),
                        id="alert-cooldown",
                        type="number",
                        select_on_focus=True,
                        compact=True,
                    ),
                )
                yield _row(
                    "Idle after (s)",
                    Input(
                        _format_number(self._config.idle_timeout_s),
                        id="idle-timeout",
                        type="number",
                        select_on_focus=True,
                        compact=True,
                    ),
                )
                yield _row(
                    "Microphone",
                    Select(
                        microphone_options,
                        value=microphone_value,
                        id="microphone",
                        allow_blank=False,
                        disabled=self._demo,
                        compact=True,
                    ),
                )
                yield _row(
                    "Volume (0-100)",
                    Input(
                        str(self._config.volume),
                        id="volume",
                        type="integer",
                        select_on_focus=True,
                        compact=True,
                    ),
                )
            yield Static("", id="settings-status")
            with Horizontal(id="settings-actions"):
                yield Button("Use voice", id="calibrate")
                yield Button("Test buzz", id="test-buzz")
                yield Button("Done", id="done", variant="primary")
            yield Static(
                "Tab/Shift+Tab move / Arrows / Enter / Esc / Q quit",
                id="settings-footer",
            )

    def on_mount(self) -> None:
        self.call_after_refresh(self._focus_first)

    def _focus_first(self) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#preset", Select).focus()

    def _set_input(self, widget_id: str, value: str) -> None:
        widget = self.query_one(f"#{widget_id}", Input)
        if widget.value != value:
            widget.value = value

    def _set_select(self, widget_id: str, value: str) -> None:
        widget = self.query_one(f"#{widget_id}", Select)
        if widget.value != value:
            widget.value = value

    def on_select_changed(self, event: Select.Changed) -> None:
        if not event.select.has_focus:
            return
        select_id = event.select.id
        if select_id == "preset":
            preset = str(event.value)
            if preset in PITCH_PRESETS:
                min_hz, max_hz = PITCH_PRESETS[preset]
                self._set_input("min-hz", _format_number(min_hz))
                self._set_input("max-hz", _format_number(max_hz))
            self._apply_draft()
        elif select_id == "microphone":
            self._apply_draft()

    def on_input_changed(self, event: Input.Changed) -> None:
        if not event.input.has_focus:
            return
        input_id = event.input.id
        if input_id in {"min-hz", "max-hz"}:
            preset_select = self.query_one("#preset", Select)
            if preset_select.value != PRESET_CUSTOM:
                self._set_select("preset", PRESET_CUSTOM)
        self._apply_draft()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "done":
            self.action_done()
        elif event.button.id == "test-buzz":
            self._test_buzz()
        elif event.button.id == "calibrate":
            self._calibrate()

    def _test_buzz(self) -> None:
        status = self.query_one("#settings-status", Static)
        try:
            self._on_test_buzz()
        except Exception as error:  # noqa: BLE001 - surface output errors in the UI
            status.update(f"Could not play buzz: {error}")
            status.add_class("error")
            status.remove_class("saved")
            return
        status.update("Test buzz played")
        status.remove_class("error")
        status.add_class("saved")

    def _calibrate(self) -> None:
        status = self.query_one("#settings-status", Static)
        config = self._on_calibrate()
        if config is None:
            status.update("Speak continuously for a few seconds, then try again")
            status.add_class("error")
            status.remove_class("saved")
            return
        self._config = config
        self._valid_config = config
        self._set_select("preset", config.preset)
        self._set_input("min-hz", _format_number(config.min_hz))
        self._set_input("max-hz", _format_number(config.max_hz))
        status.update(f"Range set to {config.range_label()} (saved)")
        status.remove_class("error")
        status.add_class("saved")

    def _draft(self) -> Config:
        preset_value = self.query_one("#preset", Select).value
        preset = str(preset_value) if preset_value is not Select.NULL else self._config.preset
        microphone_value = self.query_one("#microphone", Select).value
        microphone: str | None
        if self._demo or microphone_value in (Select.NULL, "__default__"):
            microphone = None
        else:
            microphone = str(microphone_value)
        return Config(
            preset=preset,
            min_hz=_number(self.query_one("#min-hz", Input).value, "Minimum pitch"),
            max_hz=_number(self.query_one("#max-hz", Input).value, "Maximum pitch"),
            alert_duration_s=_number(
                self.query_one("#alert-duration", Input).value, "Alert duration"
            ),
            alert_cooldown_s=_number(
                self.query_one("#alert-cooldown", Input).value, "Alert cooldown"
            ),
            microphone=microphone,
            volume=_integer(self.query_one("#volume", Input).value, "Alert volume"),
            idle_timeout_s=_number(self.query_one("#idle-timeout", Input).value, "Idle timeout"),
        )

    def _apply_draft(self) -> None:
        status = self.query_one("#settings-status", Static)
        try:
            config = self._draft().validate()
        except ConfigError as error:
            status.update(str(error))
            status.add_class("error")
            status.remove_class("saved")
            return
        if config == self._config:
            return
        self._valid_config = config
        self._config = config
        try:
            self._on_change(config)
        except Exception as error:  # noqa: BLE001 - surface persistence errors in the UI
            status.update(f"Could not save: {error}")
            status.add_class("error")
            status.remove_class("saved")
            return
        status.update("Saved")
        status.remove_class("error")
        status.add_class("saved")

    def action_done(self) -> None:
        status = self.query_one("#settings-status", Static)
        try:
            config = self._draft().validate()
        except ConfigError as error:
            status.update(str(error))
            status.add_class("error")
            status.remove_class("saved")
            return
        self._on_change(config)
        self.dismiss(config)

    def action_dismiss_settings(self) -> None:
        self.dismiss(self._valid_config)

    def action_focus_next_field(self) -> None:
        self.focus_next()

    def action_focus_previous_field(self) -> None:
        self.focus_previous()

    def action_quit_app(self) -> None:
        self.app.exit()


def _row(label: str, control: Input | Select) -> Horizontal:
    control.add_class("field-control")
    return Horizontal(
        Label(label, classes="field-label"),
        control,
        classes="row",
    )


def _format_number(value: float | int) -> str:
    numeric = float(value)
    if numeric == int(numeric):
        return str(int(numeric))
    return f"{numeric:g}"


def _number(value: str, field: str) -> float:
    try:
        return float(value.strip())
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{field} must be a number") from error


def _integer(value: str, field: str) -> int:
    number = _number(value, field)
    if number != int(number):
        raise ConfigError(f"{field} must be a whole number")
    return int(number)
