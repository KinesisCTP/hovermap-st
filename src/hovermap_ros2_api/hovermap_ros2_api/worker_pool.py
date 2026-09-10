"""Small bounded daemon worker pool for blocking device HTTP calls."""

from concurrent.futures import Future
import queue
import threading


class WorkQueueFull(RuntimeError):
    """Raised instead of allowing an unbounded command backlog."""


class BoundedDaemonWorkerPool:
    """Execute blocking calls without non-daemon shutdown hangs."""

    _STOP = object()

    def __init__(self, workers: int, max_pending: int, name: str) -> None:
        if workers <= 0 or max_pending <= 0:
            raise ValueError("workers and max_pending must be positive")
        if max_pending < workers:
            raise ValueError("max_pending must be at least the worker count")
        self._queue = queue.Queue(maxsize=max_pending)
        self._lock = threading.Lock()
        self._shutdown = False
        self._threads = [
            threading.Thread(
                target=self._run,
                name=f"{name}-{index}",
                daemon=True,
            )
            for index in range(workers)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, function, *args, **kwargs) -> Future:
        """Submit without blocking, or raise when the pending queue is full."""

        future = Future()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("worker pool is shut down")
            try:
                self._queue.put_nowait((future, function, args, kwargs))
            except queue.Full as exc:
                raise WorkQueueFull("worker queue is full") from exc
        return future

    def shutdown(self, *, cancel_pending: bool = True, wait: bool = False) -> None:
        """Cancel queued work; running daemon calls may finish in the background."""

        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            if cancel_pending:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    item[0].cancel()
                    self._queue.task_done()
            for _thread in self._threads:
                self._queue.put_nowait(self._STOP)
        if wait:
            for thread in self._threads:
                thread.join()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                future, function, args, kwargs = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = function(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                self._queue.task_done()
