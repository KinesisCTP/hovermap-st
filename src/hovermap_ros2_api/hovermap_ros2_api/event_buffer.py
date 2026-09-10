"""Thread-safe drop-oldest buffer bounded by item count and byte cost."""

from collections import deque
import queue
import threading
from typing import Callable


class DropOldestBuffer:
    """Keep recent work instead of rejecting new live sensor samples."""

    def __init__(
        self,
        *,
        max_items: int,
        max_bytes: int,
        size_of: Callable[[object], int],
        on_drop: Callable[[object], None],
    ) -> None:
        if max_items <= 0 or max_bytes <= 0:
            raise ValueError("buffer limits must be positive")
        self._max_items = max_items
        self._max_bytes = max_bytes
        self._size_of = size_of
        self._on_drop = on_drop
        self._items = deque()
        self._bytes = 0
        self._lock = threading.Lock()

    def put(self, item: object) -> bool:
        """Insert an item, evicting oldest entries; reject only oversize items."""

        size = self._size_of(item)
        if not isinstance(size, int) or size < 0:
            raise ValueError("item byte cost must be a non-negative integer")
        dropped = []
        accepted = True
        with self._lock:
            if size > self._max_bytes:
                dropped.append(item)
                accepted = False
            else:
                while self._items and (
                    len(self._items) >= self._max_items
                    or self._bytes + size > self._max_bytes
                ):
                    old_item, old_size = self._items.popleft()
                    self._bytes -= old_size
                    dropped.append(old_item)
                self._items.append((item, size))
                self._bytes += size
        for dropped_item in dropped:
            self._on_drop(dropped_item)
        return accepted

    def get_nowait(self):
        with self._lock:
            if not self._items:
                raise queue.Empty
            item, size = self._items.popleft()
            self._bytes -= size
            return item

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
