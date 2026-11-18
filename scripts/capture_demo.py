"""Capture a reproducible demo GIF from the real Textual dashboard.

The script runs Pitch-Guard in demo mode inside Textual's headless test driver,
switches chart periods with the actual keyboard bindings, exports the real
rendered screen as SVG, and assembles the frames into ``docs/demo.gif``.

Requirements:
  * Pillow (installed in the uv dev group)
  * rsvg-convert (librsvg) or ImageMagick's ``magick`` command

Usage:
    uv run python scripts/capture_demo.py
    uv run python scripts/capture_demo.py --size 100x30 --width 960
"""

from __future__ import annotations

import argparse
import asyncio
import math
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image

from pitch_guard.audio.demo import DemoPitchSource
from pitch_guard.audio.demo_data import seed_demo_history
from pitch_guard.audio.playback import NullBuzzer
from pitch_guard.config import Config
from pitch_guard.models import PitchKind, PitchMeasurement, classify_measurement
from pitch_guard.persistence import PitchGuardStorage
from pitch_guard.service import PitchGuardService
from pitch_guard.storage import DataPaths
from pitch_guard.ui.app import PitchGuardApp

ROOT = Path(__file__).resolve().parents[1]


def parse_size(value: str) -> tuple[int, int]:
    try:
        columns, rows = value.lower().split("x", 1)
        return int(columns), int(rows)
    except ValueError as error:
        raise argparse.ArgumentTypeError("size must look like 100x30") from error


def svg_to_png(svg_path: Path, png_path: Path, width: int) -> None:
    rsvg = shutil.which("rsvg-convert")
    if rsvg:
        subprocess.run(
            [rsvg, "-w", str(width), str(svg_path), "-o", str(png_path)],
            check=True,
        )
        return
    magick = shutil.which("magick")
    if magick:
        subprocess.run(
            [magick, "-background", "none", "-resize", f"{width}x", str(svg_path), str(png_path)],
            check=True,
        )
        return
    raise SystemExit(
        "Neither rsvg-convert nor ImageMagick's magick command was found. "
        "Install librsvg2-bin or ImageMagick to build the demo GIF."
    )


def seed_live_window(service: PitchGuardService, seconds: float = 120.0) -> None:
    """Fill the live chart with recent simulated speech for the demo asset."""
    now_mono = time.monotonic()
    now_wall = datetime.now(UTC)
    step = 0.25
    count = int(seconds / step)
    for index in range(count):
        offset = now_mono - seconds + index * step
        wall = now_wall - timedelta(seconds=seconds) + timedelta(seconds=index * step)
        silent = index % 43 < 5
        hz = None
        if not silent:
            hz = 207.0 + 16.0 * math.sin(index * 0.11) + 5.0 * math.sin(index * 0.71)
            if 85 <= index <= 105:
                hz = 148.0 + 5.0 * math.sin(index * 0.3)
        measurement = PitchMeasurement(
            monotonic=offset,
            timestamp=wall,
            hz=hz,
            rms=0.05 if hz is not None else 0.001,
            confidence=0.95 if hz is not None else 0.0,
            duration_s=step,
            kind=PitchKind.VOICED if hz is not None else PitchKind.SILENT,
        )
        sample = classify_measurement(measurement, service.config.min_hz, service.config.max_hz)
        service.live.add(sample)
        service.history.add_sample(sample)


async def capture(size: tuple[int, int], frames_dir: Path) -> list[Path]:
    storage = PitchGuardStorage(DataPaths.from_root(ROOT / ".demo-data"), enabled=False)
    config = Config()
    history = seed_demo_history(config)
    service = PitchGuardService(
        DemoPitchSource(),
        config,
        storage,
        history,
        NullBuzzer(),
    )
    app = PitchGuardApp(service, demo=True)
    frame_paths: list[Path] = []

    async def save_frame(pilot) -> None:
        await pilot.pause(0.08)
        index = len(frame_paths) + 1
        path = frames_dir / f"frame-{index:02d}.svg"
        path.write_text(app.export_screenshot(title="Pitch-Guard"), encoding="utf-8")
        frame_paths.append(path)

    async with app.run_test(size=size) as pilot:
        seed_live_window(service)
        await pilot.pause(3.6)
        for _ in range(4):
            await save_frame(pilot)
            await pilot.pause(0.45)
        await pilot.press("3")
        await pilot.pause(0.5)
        await save_frame(pilot)
        await pilot.pause(0.4)
        await save_frame(pilot)
        await pilot.press("2")
        await pilot.pause(0.4)
        await save_frame(pilot)
        await pilot.pause(0.4)
        await save_frame(pilot)
        await pilot.press("4")
        await pilot.pause(0.4)
        await save_frame(pilot)
        await pilot.pause(0.4)
        await save_frame(pilot)
        await pilot.press("5")
        await pilot.pause(0.4)
        await save_frame(pilot)
        await pilot.pause(0.4)
        await save_frame(pilot)
        await pilot.press("1")
        await pilot.pause(0.5)
        await save_frame(pilot)
        await pilot.pause(0.4)
        await save_frame(pilot)

    return frame_paths


def build_gif(frame_paths: list[Path], output: Path, frames_dir: Path, width: int) -> None:
    png_paths: list[Path] = []
    for svg_path in frame_paths:
        png_path = svg_path.with_suffix(".png")
        svg_to_png(svg_path, png_path, width)
        png_paths.append(png_path)
    images = [Image.open(path).convert("RGB") for path in png_paths]
    quantized = [image.quantize(colors=96, method=Image.Quantize.MEDIANCUT) for image in images]
    output.parent.mkdir(parents=True, exist_ok=True)
    quantized[0].save(
        output,
        save_all=True,
        append_images=quantized[1:],
        duration=360,
        loop=0,
        optimize=True,
        disposal=2,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--size", type=parse_size, default=(100, 30), help="terminal size (cols x rows)"
    )
    parser.add_argument("--width", type=int, default=960, help="output pixel width")
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "demo.gif")
    parser.add_argument("--frames-dir", type=Path, default=ROOT / "docs" / "demo_frames")
    arguments = parser.parse_args()
    arguments.frames_dir.mkdir(parents=True, exist_ok=True)
    for old in arguments.frames_dir.glob("frame-*.svg"):
        old.unlink()
    started = datetime.now(UTC)
    frame_paths = asyncio.run(capture(arguments.size, arguments.frames_dir))
    build_gif(frame_paths, arguments.output, arguments.frames_dir, arguments.width)
    elapsed = (datetime.now(UTC) - started).total_seconds()
    size_kb = arguments.output.stat().st_size / 1024
    print(
        f"Wrote {arguments.output} ({size_kb:.0f} KiB, {len(frame_paths)} frames, {elapsed:.1f}s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
