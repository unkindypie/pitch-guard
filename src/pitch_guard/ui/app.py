"""Textual dashboard application for Pitch-Guard."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import ClassVar, TypeVar

from rich.markup import escape
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import Static

from pitch_guard.aggregation import DailySummary
from pitch_guard.audio.capture import MicrophonePitchSource, query_input_devices
from pitch_guard.charts import (
    build_history_series,
    build_live_series,
    chart_end_label,
    chart_start_label,
)
from pitch_guard.config import Config
from pitch_guard.models import RangeStatus
from pitch_guard.service import GuardSnapshot, PitchGuardService
from pitch_guard.stats import (
    Period,
    StatsSummary,
    format_date,
    format_duration,
    format_hz,
    format_improvement,
    format_pct,
    period_window,
)
from pitch_guard.ui.chart import ChartData, PitchChart
from pitch_guard.ui.settings import SettingsScreen

LOG = logging.getLogger("pitch_guard.ui")
TWidget = TypeVar("TWidget", bound=Widget)

COLOR_SUCCESS = "#9ece6a"
COLOR_WARNING = "#e0af68"
COLOR_ERROR = "#f7768e"
COLOR_ACCENT = "#d17aa4"
COLOR_MUTED = "#7f8c98"
COLOR_TEXT = "#c0caf5"
LIVE_CHART_REFRESH_S = 0.1

PITCH_GUARD_THEME = Theme(
    name="pitch-guard",
    primary="#b85c86",
    secondary="#9f4a70",
    warning=COLOR_WARNING,
    error=COLOR_ERROR,
    success=COLOR_SUCCESS,
    accent=COLOR_ACCENT,
    foreground=COLOR_TEXT,
    dark=True,
)


class PitchGuardApp(App[None]):
    """The dashboard, keyboard bindings and lifecycle for one session."""

    CSS_PATH = "styles.tcss"
    TITLE = "Pitch-Guard"
    SUB_TITLE = "voice pitch monitor"

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("1", "show_period('live')", "Live", show=False),
        Binding("2", "show_period('day')", "Day", show=False),
        Binding("3", "show_period('week')", "Week", show=False),
        Binding("4", "show_period('month')", "Month", show=False),
        Binding("5", "show_period('90d')", "90 Days", show=False),
        Binding("s", "settings", "Settings", show=False),
        Binding("S", "settings", "Settings", show=False),
        Binding("space", "toggle_pause", "Pause", show=False),
        Binding("q", "quit_app", "Quit", show=False),
        Binding("Q", "quit_app", "Quit", show=False),
        Binding("ctrl+c", "quit_app", "Quit", show=False, priority=True),
    ]

    def __init__(
        self,
        service: PitchGuardService,
        *,
        demo: bool = False,
        ascii_only: bool = False,
    ) -> None:
        super().__init__(ansi_color=True)
        self.service = service
        self.demo = demo
        self.ascii_only = ascii_only
        self.period = Period.LIVE
        self._shutdown_done = False
        self._last_chart_refresh = 0.0
        self._last_stats: StatsSummary | None = None
        self._daily_cache: list[DailySummary] = []
        self._daily_cache_at = 0.0
        self._last_meta = ""
        self._last_status = ""
        self._last_title = ""
        self._last_footer = ""
        self._last_chart_title = ""
        self._last_chart_meta = ""
        self._footer_width = 0
        self._source_error_seen: str | None = None
        self._dashboard: Screen | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("", id="title")
            yield Static("", id="status")
        yield Static("", id="meta")
        with Vertical(id="metrics"):
            for index in range(4):
                yield Static("", id=f"metric-{index}", classes="metric")
        with Vertical(id="chart-panel"):
            with Horizontal(id="chart-header"):
                yield Static("", id="chart-title")
                yield Static("", id="chart-meta")
            yield PitchChart(ascii_only=self.ascii_only, id="chart")
        with Vertical(id="stats-panel"):
            with Vertical(id="stats-grid"):
                for index in range(8):
                    yield Static("", id=f"stat-{index}", classes="stat")
            yield Static("", id="stats-note")
        yield Static("", id="footer")

    def on_mount(self) -> None:
        self._dashboard = self.screen
        self.register_theme(PITCH_GUARD_THEME)
        self.theme = PITCH_GUARD_THEME.name
        self._apply_capabilities()
        self.service.start()
        self._refresh_daily_cache(force=True)
        self._refresh_all(force=True)
        self._update_footer()
        self.set_interval(0.1, self._tick)
        self.set_interval(1.0, self._stats_tick)

    def on_resize(self, event: events.Resize) -> None:
        width = event.size.width
        height = event.size.height
        dashboard = self._dashboard or self.screen
        for screen in {dashboard, self.screen}:
            screen.set_class(width < 72, "narrow")
            screen.set_class(height < 27, "short")
        self._update_footer(width)
        self._base_query("#chart", PitchChart).set_ascii(self.ascii_only or width < 40)

    def on_unmount(self) -> None:
        self._shutdown_service()

    def check_action(self, action: str, parameters: object) -> bool | None:
        if isinstance(self.screen, SettingsScreen):
            return action == "quit_app"
        focused = self.focused
        if focused is not None and focused.__class__.__name__ == "Input":
            return action == "quit_app"
        return True

    def _base_query(self, selector: str, expect_type: type[TWidget]) -> TWidget:
        base = self._dashboard if self._dashboard is not None else self.screen
        return base.query_one(selector, expect_type)

    def _apply_capabilities(self) -> None:
        self.screen.set_class(self.ascii_only, "ascii")
        self._base_query("#chart", PitchChart).set_ascii(self.ascii_only)

    def _tick(self) -> None:
        try:
            self.service.poll()
        except Exception:
            LOG.exception("Failed to process audio measurements")
        snapshot = self.service.snapshot()
        self._render_snapshot(snapshot, time.monotonic())
        if (
            self.period is Period.LIVE
            and time.monotonic() - self._last_chart_refresh >= LIVE_CHART_REFRESH_S
        ):
            self._refresh_chart(snapshot, render_stats=False)

    def _stats_tick(self) -> None:
        if self.size.width != self._footer_width:
            self._update_footer(self.size.width)
        mon = time.monotonic()
        if mon - self._daily_cache_at >= 60.0:
            self._refresh_daily_cache()
        summary = self.service.summary(
            self.period,
            datetime.now(UTC),
            mon,
            self._daily_cache,
        )
        self._last_stats = summary
        self._render_stats(summary)
        if self.period is not Period.LIVE:
            self._refresh_chart(self.service.snapshot(), summary=summary, render_stats=False)

    def _refresh_all(self, *, force: bool = False) -> None:
        snapshot = self.service.snapshot()
        self._render_snapshot(snapshot, time.monotonic())
        self._refresh_chart(snapshot, force=force)

    def _refresh_daily_cache(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._daily_cache_at < 60.0:
            return
        self._daily_cache_at = now
        try:
            self._daily_cache = self.service.daily_summaries(datetime.now(UTC))
        except Exception:
            LOG.exception("Could not compute daily summaries")
            self._daily_cache = []

    def _render_snapshot(self, snapshot: GuardSnapshot, monotonic_now: float) -> None:
        status_text, status_color = self._status_text(snapshot)
        title = "[bold]Pitch-Guard[/]  [dim]voice pitch monitor[/]"
        if self.demo:
            title += f"  [bold {COLOR_ACCENT}]DEMO[/]"
        self._set_static("title", title, self._last_title)
        self._last_title = title

        status = f"[bold {status_color}]{status_text}[/]"
        self._set_static("status", status, self._last_status)
        self._last_status = status

        meta = self._meta_line(snapshot)
        self._set_static("meta", meta, self._last_meta)
        self._last_meta = meta

        current = snapshot.current
        current_hz = current.hz if current is not None and current.voiced else None
        current_value = format_hz(current_hz) if current_hz is not None else self._dash()
        current_note = "paused" if snapshot.paused else snapshot.status.label.lower()
        current_color = self._status_color(snapshot.status)

        avg = self._last_stats.avg_hz if self._last_stats else None
        avg_value = format_hz(avg)
        avg_note = f"{self.period.label.lower()} average"

        status_value = snapshot.status.label
        status_note = self._alert_note(snapshot)
        alert_value = str(self.service.alerts_session)
        alert_note = "this session"

        metrics = [
            ("CURRENT", current_value, current_note, current_color),
            ("AVG", avg_value, avg_note, None),
            ("STATUS", status_value, status_note, status_color),
            ("ALERTS", alert_value, alert_note, None),
        ]
        for index, (label, value, note, color) in enumerate(metrics):
            self._base_query(f"#metric-{index}", Static).update(
                self._metric_markup(label, value, note, color)
            )

        if snapshot.source_error and snapshot.source_error != self._source_error_seen:
            LOG.warning("Audio source error: %s", snapshot.source_error)
            self._source_error_seen = snapshot.source_error

    def _refresh_chart(
        self,
        snapshot: GuardSnapshot,
        *,
        force: bool = False,
        summary: StatsSummary | None = None,
        render_stats: bool = True,
    ) -> None:
        now = datetime.now(UTC)
        mono_now = time.monotonic()
        window = period_window(self.period, now)
        if summary is None:
            summary = self.service.summary(self.period, now, mono_now, self._daily_cache)
        if self.period is Period.LIVE:
            series = build_live_series(
                self.service.live.samples(),
                mono_now - 60.0,
                mono_now,
            )
        else:
            buckets = self.service.history.snapshot(now)
            series = build_history_series(buckets, window.start, window.end)
        data = ChartData(
            period_label=self.period.label,
            start=window.start,
            end=window.end,
            series=tuple(tuple(segment) for segment in series),
            target_min=snapshot.config.min_hz,
            target_max=snapshot.config.max_hz,
            avg_hz=summary.avg_hz,
            in_range_pct=summary.in_range_pct,
            start_label=chart_start_label(self.period.label, window.start, window.end),
            end_label=chart_end_label(self.period.label, window.end),
        )
        self._base_query("#chart", PitchChart).set_data(data)
        title = f"[bold]Pitch History[/]  [dim]{self.period.label} · {self.period.detail}[/]"
        self._set_static("chart-title", title, self._last_chart_title)
        self._last_chart_title = title
        chart_meta = (
            f"[dim]target  [/]{snapshot.config.range_label()}   "
            f"[dim]avg  [/]{format_hz(summary.avg_hz)}"
        )
        self._set_static("chart-meta", chart_meta, self._last_chart_meta)
        self._last_chart_meta = chart_meta
        self._last_chart_refresh = mono_now
        self._last_stats = summary
        if render_stats:
            self._render_stats(summary)

    def _render_stats(self, summary: StatsSummary) -> None:
        values: list[tuple[str, str, str, str | None]] = [
            ("AVG", format_hz(summary.avg_hz), "voiced pitch", None),
            ("MEDIAN", format_hz(summary.median_hz), "voiced pitch", None),
            ("VOICED", format_duration(summary.voiced_s), "speaking time", None),
            ("IN RANGE", format_pct(summary.in_range_pct), "of voiced time", None),
            ("MONITORING", format_duration(summary.monitoring_s), summary.period.detail, None),
            ("OUT OF RANGE", format_duration(summary.out_of_range_s), "voiced time", None),
            ("ALERTS", str(summary.alerts), summary.period.label.lower(), None),
            (
                "IMPROVEMENT",
                format_improvement(summary.improvement_pp),
                "vs previous period",
                self._improvement_color(summary.improvement_pp),
            ),
        ]
        for index, (label, value, note, color) in enumerate(values):
            self._base_query(f"#stat-{index}", Static).update(
                self._metric_markup(label, value, note, color)
            )
        streak = summary.streak_days
        streak_text = f"{streak}d" if streak is not None else "N/A"
        best = summary.best_day
        best_text = (
            f"{format_pct(best.in_range_pct)} on {format_date(best.day)}"
            if best is not None
            else "N/A"
        )
        note_parts = [
            f"Streak {streak_text}",
            f"Best {best_text}",
            f"Improved metric: in-range % vs previous {summary.period.detail}",
        ]
        if summary.voiced_s <= 0:
            note_parts.append("Waiting for voiced speech")
        separator = " | " if self.ascii_only else "  ·  "
        self._base_query("#stats-note", Static).update("[dim]" + separator.join(note_parts) + "[/]")

    def _status_text(self, snapshot: GuardSnapshot) -> tuple[str, str]:
        if snapshot.source_error:
            return "AUDIO ERROR", COLOR_ERROR
        if snapshot.paused:
            return "PAUSED", COLOR_ACCENT
        return "MONITORING", COLOR_SUCCESS

    def _meta_line(self, snapshot: GuardSnapshot) -> str:
        config = snapshot.config
        microphone = escape(snapshot.source_description)
        volume = f"{config.volume}%"
        separator = " | " if self.ascii_only else "  ·  "
        if self.size.width < 76:
            return f"[dim]MIC  [/]{microphone}{separator}[dim]RANGE  [/]{config.range_label()}"
        parts = [
            f"[dim]MIC  [/]{microphone}",
            f"[dim]RANGE  [/]{config.range_label()}",
            f"[dim]ALERT  [/]{config.alert_duration_s:.1f}s / {config.alert_cooldown_s:.1f}s",
            f"[dim]VOL  [/]{volume}",
        ]
        return separator.join(parts)

    def _alert_note(self, snapshot: GuardSnapshot) -> str:
        if snapshot.paused:
            return "monitoring paused"
        if snapshot.source_error:
            return "check audio"
        if snapshot.status.is_out_of_range:
            if snapshot.alert_progress >= 1.0:
                return "buzz played" if not snapshot.alert_armed else "episode alert"
            remaining = max(0.0, snapshot.config.alert_duration_s - snapshot.out_of_range_s)
            return f"alert in {remaining:.1f}s"
        if snapshot.status is RangeStatus.IN_RANGE:
            return "armed" if snapshot.alert_armed else "cooldown"
        return "listening"

    def _metric_markup(self, label: str, value: str, note: str, color: str | None = None) -> str:
        value_style = f"bold {color}" if color else "bold"
        return f"[dim]{escape(label)}[/]\n[{value_style}]{escape(value)}[/]  [dim]{escape(note)}[/]"

    @staticmethod
    def _status_color(status: RangeStatus) -> str:
        if status is RangeStatus.IN_RANGE:
            return COLOR_SUCCESS
        if status.is_out_of_range:
            return COLOR_WARNING
        if status is RangeStatus.PAUSED:
            return COLOR_ACCENT
        return COLOR_MUTED

    @staticmethod
    def _improvement_color(value: float | None) -> str | None:
        if value is None:
            return None
        if value > 0.05:
            return COLOR_SUCCESS
        if value < -0.05:
            return COLOR_ERROR
        return COLOR_MUTED

    def _dash(self) -> str:
        return "--" if self.ascii_only else "—"

    def _set_static(self, widget_id: str, markup: str, previous: str) -> None:
        if markup != previous:
            self._base_query(f"#{widget_id}", Static).update(markup)

    def _update_footer(self, width: int | None = None) -> None:
        terminal_width = width or self.size.width
        if terminal_width < 44:
            footer = "1-5 Chart  S  Space  Q"
        elif terminal_width < 82:
            footer = "1-5 Chart   S Settings   Space Pause   Q Quit"
        else:
            footer = "1 Live   2 Day   3 Week   4 Month   5 90d   S Settings   Space Pause   Q Quit"
        if footer != self._last_footer:
            self._base_query("#footer", Static).update(f"[dim]{footer}[/]")
            self._last_footer = footer
        self._footer_width = terminal_width

    def action_show_period(self, period_key: str) -> None:
        period = Period.from_key(period_key)
        if period is None:
            return
        self.period = period
        self._last_stats = None
        self._refresh_daily_cache(force=True)
        self._refresh_all(force=True)

    def action_toggle_pause(self) -> None:
        self.service.toggle_pause()
        self._last_stats = None
        self._refresh_all(force=True)

    def action_settings(self) -> None:
        devices = [] if self.demo else query_input_devices()
        screen = SettingsScreen(
            self.service.config,
            devices,
            demo=self.demo,
            on_change=self._auto_apply_config,
        )
        screen.set_class(self.ascii_only, "ascii")
        screen.set_class(self.size.width < 72, "narrow")
        screen.set_class(self.size.height < 27, "short")
        self.push_screen(screen)

    def _auto_apply_config(self, config: Config) -> None:
        previous_microphone = self.service.config.microphone
        validated = self.service.apply_config(config)
        if not self.demo and validated.microphone != previous_microphone:
            source = MicrophonePitchSource(validated.microphone)
            self.service.set_source(source, validated)

    def action_quit_app(self) -> None:
        self._shutdown_service()
        self.exit()

    def _shutdown_service(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        try:
            self.service.stop()
        except Exception:
            LOG.exception("Error while shutting down monitoring")
