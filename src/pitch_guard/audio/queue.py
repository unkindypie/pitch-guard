"""Bounded, thread-safe hand-off between audio processing and the UI."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable
from typing import Generic, TypeVar

T = TypeVar("T")


class MeasurementQueue(Generic[T]):
    """A drop-oldest bounded queue.

    The audio thread must never block on a busy UI.  If the consumer falls
    behind, the oldest measurements are discarded; timing decisions still use
    each retained measurement's monotonic timestamp.  The queue is deliberately
    small and never stores raw audio.
    """

    def __init__(self, maxsize: int = 64) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        self._maxsize = maxsize
        self._items: deque[T] = deque()
        self._lock = threading.Lock()

    def put(self, item: T) -> None:
        """Append an item, dropping the oldest item when full."""
        with self._lock:
            if len(self._items) >= self._maxsize:
                self._items.popleft()
            self._items.append(item)

    def extend(self, items: Iterable[T]) -> None:
        """Append several items under one lock."""
        with self._lock:
            for item in items:
                if len(self._items) >= self._maxsize:
                    self._items.popleft()
                self._items.append(item)

    def drain(self) -> list[T]:
        """Remove and return all queued items in order."""
        with self._lock:
            items = list(self._items)
            self._items.clear()
            return items

    def clear(self) -> None:
        """Discard every queued item."""
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def maxsize(self) -> int:
        """Maximum number of retained items."""
        return self._maxsize
