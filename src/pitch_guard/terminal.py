"""Terminal capability detection and rendering fallbacks."""

from __future__ import annotations

import os
import sys
from typing import Protocol

BOX_SAMPLE = "╭─│┤└┄┈•╱╲"


class EncodedStream(Protocol):
    """Minimal stream interface needed for capability detection."""

    @property
    def encoding(self) -> str | None:
        """Text encoding advertised by the stream."""
        ...


def supports_unicode(stream: EncodedStream | None = None) -> bool:
    """Return whether the current terminal encoding can render box drawing.

    The check is intentionally conservative.  ``--ascii`` always overrides it,
    and Textual still handles the actual terminal negotiation.
    """
    if os.environ.get("PITCH_GUARD_ASCII", "").strip() == "1":
        return False
    target = stream or sys.stdout
    encoding = getattr(target, "encoding", None) or "ascii"
    try:
        BOX_SAMPLE.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    term = os.environ.get("TERM", "")
    if term in {"dumb", "unknown"}:
        return False
    io_encoding = os.environ.get("PYTHONIOENCODING", "").lower()
    return not io_encoding.startswith("ascii")
