"""Local JSON persistence helpers.

Pitch-Guard stores only configuration values and aggregated statistics.  Raw
microphone audio is never written anywhere.  Every write is atomic: a temporary
file in the destination directory is flushed, synced and then replaced with
``os.replace`` so a crash cannot leave a half-written JSON document.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

APP_DIR_NAME = ".pitch-guard"
CONFIG_SCHEMA_VERSION = 1
HISTORY_SCHEMA_VERSION = 1
DAILY_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class DataPaths:
    """Resolved locations of Pitch-Guard's local data files."""

    root: Path
    config: Path
    history: Path
    daily_stats: Path
    log: Path

    @classmethod
    def from_root(cls, root: Path) -> DataPaths:
        """Build all file paths below *root*."""
        return cls(
            root=root,
            config=root / "config.json",
            history=root / "history.json",
            daily_stats=root / "daily-stats.json",
            log=root / "pitch-guard.log",
        )

    def ensure_directories(self) -> None:
        """Create the data directory with user-only permissions where possible."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)


def resolve_data_home(explicit: Path | None = None) -> Path:
    """Resolve the data directory.

    ``--data-dir`` wins, then the ``PITCH_GUARD_HOME`` environment variable,
    then the documented ``~/.pitch-guard`` location.  An environment override
    keeps tests and demo runs entirely off the user's real history.
    """
    if explicit is not None:
        return explicit.expanduser().resolve()
    env_path = os.environ.get("PITCH_GUARD_HOME")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return (Path.home() / APP_DIR_NAME).resolve()


def utc_now() -> datetime:
    """Return the current timezone-aware UTC time."""
    return datetime.now(UTC)


def ensure_utc(moment: datetime) -> datetime:
    """Normalise a datetime to an aware UTC value."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def iso_timestamp(moment: datetime) -> str:
    """Serialise a datetime as a timezone-safe ISO-8601 UTC string."""
    return ensure_utc(moment).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp, returning ``None`` when invalid."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ensure_utc(parsed)


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory sync so a rename survives power loss."""
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_json(path: Path, payload: object) -> None:
    """Write *payload* to *path* atomically as UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            temporary_path.chmod(0o600)
        temporary_path.replace(path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def read_json_file(path: Path, logger: logging.Logger | None = None) -> Any | None:
    """Read a JSON document, tolerating missing or malformed files.

    A malformed file is copied to ``<name>.corrupt`` before returning ``None``
    so the next save can repair the working file without silently destroying
    useful debugging information.
    """
    log = logger or logging.getLogger("pitch_guard.storage")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        log.warning("Could not read %s: %s", path, error)
        if path.exists():
            backup = path.with_suffix(path.suffix + ".corrupt")
            try:
                backup.write_bytes(path.read_bytes())
            except OSError:
                log.debug("Could not preserve corrupt file %s", path, exc_info=True)
        return None


def schema_version(payload: object) -> int | None:
    """Read a schema version from a decoded JSON object."""
    if not isinstance(payload, dict):
        return None
    value = payload.get("schema_version")
    if isinstance(value, int):
        return value
    return None
