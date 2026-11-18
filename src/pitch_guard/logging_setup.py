"""Bounded file logging for live troubleshooting.

Logs contain device names, configuration changes and alert events, never raw
audio or decoded samples.  Rotation prevents unbounded growth.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(log_path: Path, level: str = "INFO") -> logging.Logger:
    """Configure the application logger with a rotating file handler."""
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    logger = logging.getLogger("pitch_guard")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(
        log_path,
        maxBytes=1_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger
