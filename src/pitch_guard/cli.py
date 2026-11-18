"""Command-line entry point for ``pitch-guard``."""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pitch_guard.audio.capture import MicrophonePitchSource
from pitch_guard.audio.demo import DemoPitchSource
from pitch_guard.audio.demo_data import seed_demo_history
from pitch_guard.audio.playback import NullBuzzer, SoundDeviceBuzzer
from pitch_guard.config import Config
from pitch_guard.logging_setup import configure_logging
from pitch_guard.persistence import PitchGuardStorage
from pitch_guard.service import PitchGuardService
from pitch_guard.storage import DataPaths, resolve_data_home
from pitch_guard.terminal import supports_unicode
from pitch_guard.ui.app import PitchGuardApp

LOG = logging.getLogger("pitch_guard.cli")


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="pitch-guard",
        description=(
            "Live voice pitch monitor with a terminal dashboard, continuous "
            "out-of-range alerts and local progress statistics."
        ),
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="use simulated pitch input and seeded demo history instead of a microphone",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="store config/history below PATH instead of ~/.pitch-guard",
    )
    parser.add_argument(
        "--mic",
        default=None,
        metavar="NAME",
        help="microphone device name override for this run",
    )
    parser.add_argument(
        "--mute",
        action="store_true",
        help="disable the alert buzz without changing the saved volume",
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="force ASCII borders and chart characters",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="file log verbosity (default: INFO)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_package_version()}",
    )
    return parser


def _set_process_name() -> None:
    """Give the running process a stable OS-level name for tmux/ps/top."""
    try:
        import setproctitle
    except ImportError:
        return
    try:
        setproctitle.setproctitle("pitch-guard")
    except Exception:  # noqa: BLE001 - best-effort title change per platform
        return


def main(argv: Sequence[str] | None = None) -> int:
    """Run Pitch-Guard and return a process exit code."""
    _set_process_name()
    arguments = build_parser().parse_args(argv)
    root = resolve_data_home(arguments.data_dir)
    if arguments.demo and arguments.data_dir is None:
        root = Path(tempfile.gettempdir()) / "pitch-guard-demo"
    paths = DataPaths.from_root(root)
    try:
        configure_logging(paths.log, arguments.log_level)
    except OSError as error:
        logging.basicConfig(level=getattr(logging, arguments.log_level))
        logging.getLogger("pitch_guard").warning("Could not open log file: %s", error)

    LOG.info(
        "Starting Pitch-Guard demo=%s ascii=%s data=%s",
        arguments.demo,
        arguments.ascii,
        paths.root,
    )
    try:
        storage = PitchGuardStorage(paths, enabled=not arguments.demo)
        if arguments.demo:
            config = Config()
            history = seed_demo_history(config)
            source = DemoPitchSource()
        else:
            config = storage.load_config()
            history = storage.load_history()
            if arguments.mic:
                config.microphone = arguments.mic
                config = config.validate()
                storage.save_config(config)
            source = MicrophonePitchSource(config.microphone)
        buzzer = NullBuzzer() if arguments.mute else SoundDeviceBuzzer(config.volume)
        service = PitchGuardService(source, config, storage, history, buzzer)
        app = PitchGuardApp(
            service,
            demo=arguments.demo,
            ascii_only=arguments.ascii or not supports_unicode(),
        )
        app.run()
        return 0
    except KeyboardInterrupt:
        LOG.info("Interrupted")
        return 130
    except Exception as error:
        LOG.exception("Fatal error")
        sys.stderr.write(f"pitch-guard: {error}\n")
        return 1


def _package_version() -> str:
    try:
        return version("pitch-guard")
    except PackageNotFoundError:
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
