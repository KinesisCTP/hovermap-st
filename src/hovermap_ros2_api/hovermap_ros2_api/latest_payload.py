"""Thread-safe latest-value mailbox for idempotent Mule configuration sends."""

import threading
from typing import Callable, Optional


class LatestPayloadMailbox:
    """Keep the newest payload and allow at most one scheduled drain."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: Optional[bytes] = None
        self._scheduled = False

    def offer(self, payload: bytes, is_ready: Callable[[], bool]) -> bool:
        """Replace the pending value and report whether a drain must be queued."""

        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        with self._lock:
            self._payload = payload
            if is_ready() and not self._scheduled:
                self._scheduled = True
                return True
            return False

    def arm(self) -> bool:
        """Schedule a drain after transport readiness if a value is pending."""

        with self._lock:
            if self._payload is not None and not self._scheduled:
                self._scheduled = True
                return True
            return False

    def take_or_disarm(self) -> Optional[bytes]:
        """Take the latest value, or atomically mark an empty drain complete."""

        with self._lock:
            payload = self._payload
            self._payload = None
            if payload is None:
                self._scheduled = False
            return payload

    def restore_after_failure(
        self, payload: bytes, *, schedule_retry: bool = False
    ) -> bool:
        """Retain a failed value and atomically reserve an optional retry."""

        with self._lock:
            if self._payload is None:
                self._payload = payload
            self._scheduled = bool(schedule_retry and self._payload is not None)
            return self._scheduled

    def cancel_schedule(self) -> None:
        """Allow a future readiness transition to reschedule pending work."""

        with self._lock:
            self._scheduled = False

    def has_pending(self) -> bool:
        with self._lock:
            return self._payload is not None
