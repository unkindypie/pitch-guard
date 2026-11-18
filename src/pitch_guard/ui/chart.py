"""Small, dependency-free dotted pitch chart for Textual.

Textual does not ship a line chart, and terminal plotting packages vary in how
they handle axes, reference lines and gaps.  This widget deliberately keeps the
rendering modest and calm: one dot per chart column (no moving line glyphs),
labelled Y ticks, dashed target-range lines and a numeric fallback for very
small terminals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from rich.style import Style
from rich.text import Text
from textual.widget import Widget

from pitch_guard.charts import ChartPoint, format_axis_hz, nice_bounds, nice_ticks
from pitch_guard.stats import format_hz, format_pct

UNICODE_CHARS = {
    "vertical": "│",
    "horizontal": "─",
    "corner": "└",
    "tick": "┤",
    "threshold": "┄",
    "average": "┈",
    "point": "•",
}
ASCII_CHARS = {
    "vertical": "|",
    "horizontal": "-",
    "corner": "+",
    "tick": "+",
    "threshold": "-",
    "average": ".",
    "point": "*",
}

MIN_WIDTH = 36
MIN_HEIGHT = 8


@dataclass(frozen=True, slots=True)
class ChartData:
    """Immutable chart payload handed to the widget."""

    period_label: str
    start: datetime
    end: datetime
    series: tuple[tuple[ChartPoint, ...], ...]
    target_min: float
    target_max: float
    avg_hz: float | None
    in_range_pct: float | None
    start_label: str
    end_label: str


class PitchChart(Widget):
    """Render pitch history as a calm dotted terminal chart."""

    DEFAULT_CSS = """
    PitchChart {
        height: 1fr;
        min-height: 6;
        padding: 0 1;
        background: transparent;
        color: $text;
    }
    PitchChart > .chart--axis { color: $text-muted; }
    PitchChart > .chart--threshold { color: $warning; }
    PitchChart > .chart--average { color: $accent 70%; }
    PitchChart > .chart--data { color: $accent; }
    PitchChart > .chart--label { color: $text-muted; }
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "chart--axis",
        "chart--threshold",
        "chart--average",
        "chart--data",
        "chart--label",
    }

    def __init__(
        self,
        *,
        ascii_only: bool = False,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)
        self._data: ChartData | None = None
        self._ascii = ascii_only
        self._chars = ASCII_CHARS if ascii_only else UNICODE_CHARS
        self._scale: tuple[float, float] | None = None
        self._scale_key: tuple[str, float, float] | None = None
        self._last_plain: str | None = None

    def set_ascii(self, ascii_only: bool) -> None:
        """Switch between Unicode and ASCII line characters."""
        if self._ascii == ascii_only:
            return
        self._ascii = ascii_only
        self._chars = ASCII_CHARS if ascii_only else UNICODE_CHARS
        self._last_plain = None
        self.refresh()

    def set_data(self, data: ChartData) -> None:
        """Replace chart data and repaint only when it changed."""
        if self._data == data:
            return
        self._data = data
        if self.size.width > 0 and self.size.height > 0:
            plain = self.render().plain
            if plain == self._last_plain:
                return
            self._last_plain = plain
        self.refresh()

    def render(self) -> Text:
        """Build the complete chart as one Rich Text object."""
        width = self.size.width
        height = self.size.height
        if width <= 0 or height <= 0:
            return Text("")
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            return self._fallback(width)
        data = self._data
        if data is None:
            return Text("Waiting for pitch data...", style="dim")

        styles = {
            "axis": self._style("chart--axis", "grey62"),
            "threshold": self._style("chart--threshold", "yellow"),
            "average": self._style("chart--average", "cyan"),
            "data": self._style("chart--data", "green"),
            "label": self._style("chart--label", "grey62"),
        }
        low, high = self._resolve_scale(data)
        margin = 6
        plot_width = max(1, width - margin)
        plot_height = max(2, height - 2)
        grid: list[list[tuple[str, str]]] = [
            [(" ", "") for _ in range(width)] for _ in range(plot_height)
        ]

        def put(row: int, column: int, char: str, style_name: str) -> None:
            if 0 <= row < plot_height and 0 <= column < width:
                grid[row][column] = (char, style_name)

        # Y axis and labelled ticks.  Configured thresholds get their own label
        # when they do not already coincide with a regular tick.
        labelled_rows: set[int] = set()
        for row in range(plot_height):
            put(row, margin - 1, self._chars["vertical"], "axis")
        for tick in nice_ticks(low, high, 4):
            if not low <= tick <= high:
                continue
            row = self._y_row(tick, low, high, plot_height)
            label = format_axis_hz(tick).rjust(margin - 2)
            for offset, character in enumerate(label):
                put(row, offset, character, "label")
            put(row, margin - 1, self._chars["tick"], "axis")
            labelled_rows.add(row)
        for threshold in (data.target_min, data.target_max):
            if not low <= threshold <= high:
                continue
            row = self._y_row(threshold, low, high, plot_height)
            if row in labelled_rows:
                continue
            label = format_axis_hz(threshold).rjust(margin - 2)
            for offset, character in enumerate(label):
                put(row, offset, character, "label")
            put(row, margin - 1, self._chars["tick"], "axis")
            labelled_rows.add(row)
        put(plot_height - 1, margin - 1, self._chars["corner"], "axis")
        for column in range(margin, width):
            put(plot_height - 1, column, self._chars["horizontal"], "axis")

        # Target thresholds and the period average.
        for threshold in (data.target_min, data.target_max):
            if low <= threshold <= high:
                row = self._y_row(threshold, low, high, plot_height)
                for column in range(margin, width):
                    if grid[row][column][0] == " ":
                        put(row, column, self._chars["threshold"], "threshold")
        if data.avg_hz is not None and low <= data.avg_hz <= high:
            row = self._y_row(data.avg_hz, low, high, plot_height)
            for column in range(margin, width):
                if grid[row][column][0] in (" ", self._chars["threshold"]):
                    put(row, column, self._chars["average"], "average")

        any_points = False
        for segment in data.series:
            compressed = self._compress(
                segment, data.start, data.end, margin, plot_width, low, high, plot_height
            )
            if not compressed:
                continue
            any_points = True
            for row, column in compressed:
                put(row, column, self._chars["point"], "data")

        if not any_points:
            message = "waiting for voiced pitch"
            row = max(0, plot_height // 2)
            start_column = max(margin + 1, margin + (plot_width - len(message)) // 2)
            for offset, character in enumerate(message):
                put(row, start_column + offset, character, "label")

        # X labels.
        lines = self._compose_lines(grid, styles)
        start_text = Text(data.start_label, style=styles["label"])
        end_text = Text(data.end_label, style=styles["label"])
        left = Text(" " * margin)
        left.append_text(start_text)
        right_space = max(1, width - margin - len(data.start_label) - len(data.end_label))
        left.append(" " * right_space)
        left.append_text(end_text)
        composed = Text("\n").join(lines)
        composed.append("\n")
        composed.append_text(left[:width])
        return composed

    def _resolve_scale(self, data: ChartData) -> tuple[float, float]:
        """Keep a stable Y scale so live movement does not make the chart blink.

        The scale resets when the period or target range changes.  While data
        stays inside it, small pitch changes never move the axis labels or the
        horizontal reference lines.  Extreme values expand the scale only when
        genuinely necessary.
        """
        key = (data.period_label, data.target_min, data.target_max)
        values = [point.hz for segment in data.series for point in segment]
        if self._scale is None or self._scale_key != key:
            self._scale = nice_bounds(values, data.target_min, data.target_max)
            self._scale_key = key
        else:
            low, high = self._scale
            if values and (min(values) < low or max(values) > high):
                self._scale = nice_bounds(values, data.target_min, data.target_max)
        return self._scale

    def _compose_lines(
        self, grid: list[list[tuple[str, str]]], styles: dict[str, Style]
    ) -> list[Text]:
        lines: list[Text] = []
        for row in grid:
            line = Text()
            for character, style_name in row:
                style = styles.get(style_name, Style())
                line.append(character, style=style)
            lines.append(line)
        return lines

    @staticmethod
    def _y_row(value: float, low: float, high: float, plot_height: int) -> int:
        if high <= low:
            return plot_height - 2
        return round((high - value) / (high - low) * (plot_height - 2))

    @staticmethod
    def _x_column(
        timestamp: datetime, start: datetime, end: datetime, margin: int, plot_width: int
    ) -> int:
        span = (end - start).total_seconds()
        if span <= 0:
            return margin
        fraction = (timestamp - start).total_seconds() / span
        return margin + round(max(0.0, min(1.0, fraction)) * (plot_width - 1))

    def _compress(
        self,
        segment: tuple[ChartPoint, ...],
        start: datetime,
        end: datetime,
        margin: int,
        plot_width: int,
        low: float,
        high: float,
        plot_height: int,
    ) -> list[tuple[int, int]]:
        columns: dict[int, list[float]] = {}
        for point in segment:
            column = self._x_column(point.timestamp, start, end, margin, plot_width)
            columns.setdefault(column, []).append(point.hz)
        result: list[tuple[int, int]] = []
        for column in sorted(columns):
            average = sum(columns[column]) / len(columns[column])
            result.append((self._y_row(average, low, high, plot_height), column))
        return result

    def _style(self, component: str, fallback: str) -> Style:
        try:
            color = self.get_component_styles(component).color
            return Style(color=color.rich_color)
        except Exception:  # noqa: BLE001 - optional component styling
            return Style(color=fallback)

    def _fallback(self, width: int) -> Text:
        data = self._data
        text = Text()
        text.append("Chart hidden at this size", style="bold")
        if data is None:
            return text
        text.append("\n")
        text.append(
            f"Target {format_axis_hz(data.target_min)}-{format_axis_hz(data.target_max)} Hz"
        )
        text.append("  AVG ")
        text.append(format_hz(data.avg_hz))
        text.append("  In range ")
        text.append(format_pct(data.in_range_pct))
        return text
