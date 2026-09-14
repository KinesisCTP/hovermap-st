"""Async orchestration around the synchronous Hovermap HTTP client."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

import anyio

from hovermap_direct.http_client import HovermapHttpClient

from .configuration import ServerConfig
from .errors import RuntimeBusyError, RuntimeClosingError

MUTATION_QUEUE_CAPACITY = 16


@dataclass
class _MutationJob:
    callback: Callable[[], object]
    done: anyio.Event = field(default_factory=anyio.Event)
    result: object | None = None
    error: BaseException | None = None
    cancelled: bool = False


class HovermapRuntime:
    """Own concurrency and lifecycle rules for one MCP server process."""

    def __init__(
        self,
        client: HovermapHttpClient,
        download_directory: Path,
        *,
        mutation_queue_capacity: int = MUTATION_QUEUE_CAPACITY,
    ) -> None:
        if mutation_queue_capacity < 1:
            raise ValueError("mutation queue capacity must be positive")
        self.client = client
        self.download_directory = Path(download_directory).resolve(strict=False)
        self._mutation_send, self._mutation_receive = anyio.create_memory_object_stream(
            mutation_queue_capacity
        )
        self._download_guard = threading.Lock()
        self._closing = threading.Event()
        self._running = False

    @property
    def closing(self) -> bool:
        return self._closing.is_set()

    def request_shutdown(self) -> None:
        """Reject new work and ask an active synchronous operation to cancel."""

        self._closing.set()
        self.client.cancel()

    @asynccontextmanager
    async def running(self) -> AsyncIterator[HovermapRuntime]:
        """Run the ordered mutation worker for the duration of a server connection."""

        if self._running:
            raise RuntimeError("Hovermap runtime is already running")
        self._running = True
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(self._run_mutations)
            try:
                yield self
            finally:
                self.request_shutdown()
                await self._mutation_send.aclose()
                task_group.cancel_scope.cancel()
        self._running = False

    async def get_status(self) -> dict[str, Any]:
        self._ensure_open()
        status = await self._run_sync(self.client.get_status)
        return {
            "scan_prefix": status.scan_prefix,
            "current_scan_name": status.current_scan_name,
            "free_space_bytes": status.free_space_bytes,
            "scan_running": status.scan_running,
            "state": status.state,
            "retrieved_at_utc": _utc_now(),
        }

    async def set_scan_prefix(self, prefix: str) -> dict[str, Any]:
        await self._submit_mutation(partial(self.client.set_scan_prefix, prefix))
        return {
            "prefix": prefix,
            "acknowledgement": "http_2xx",
            "state_confirmed": False,
        }

    async def start_scan(self) -> dict[str, Any]:
        await self._submit_mutation(self.client.start_scan)
        return {
            "mission_type": 1,
            "acknowledgement": "http_2xx",
            "state_confirmed": False,
        }

    async def stop_scan(self) -> dict[str, Any]:
        await self._submit_mutation(self.client.stop_scan)
        return {
            "acknowledgement": "http_2xx",
            "state_confirmed": False,
        }

    async def list_scans(self) -> dict[str, Any]:
        self._ensure_open()
        result = await self._run_sync(self.client.list_scans)
        structured_scans = []
        for scan in result.scans:
            structured_scans.append(
                {
                    "name": scan.name,
                    "size_bytes": scan.size_bytes,
                    "timestamp": scan.timestamp,
                    "parsed_time_utc": scan.parsed_time_utc,
                }
            )
        return {
            "count": len(structured_scans),
            "ordering": result.ordering,
            "scans": structured_scans,
            "warnings": list(result.warnings),
        }

    async def download_scan(self, scan_name: str) -> dict[str, Any]:
        self._ensure_open()
        if not self._download_guard.acquire(blocking=False):
            raise RuntimeBusyError("Another scan download is already in progress.")
        try:
            result = await self._run_sync(
                partial(
                    self.client.download_scan,
                    scan_name,
                    self.download_directory,
                )
            )
        finally:
            self._download_guard.release()
        path = Path(result.path).resolve(strict=False)
        try:
            path.relative_to(self.download_directory)
        except ValueError as exc:
            raise OSError("download result escaped the configured root") from exc
        return {
            "scan_name": result.scan_name,
            "path": str(path),
            "size_bytes": result.size_bytes,
            "zip_validated": True,
            "published_atomically": True,
        }

    async def _submit_mutation(self, callback: Callable[[], object]) -> object:
        self._ensure_open()
        if not self._running:
            raise RuntimeClosingError("mutation worker is not running")
        job = _MutationJob(callback=callback)
        try:
            self._mutation_send.send_nowait(job)
        except anyio.WouldBlock as exc:
            raise RuntimeBusyError("The mutation queue is full; try again later.") from exc
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            raise RuntimeClosingError("mutation lane is closed") from exc
        try:
            await job.done.wait()
        except BaseException:
            job.cancelled = True
            raise
        if job.error is not None:
            raise job.error
        return job.result

    async def _run_mutations(self) -> None:
        async with self._mutation_receive:
            async for job in self._mutation_receive:
                if job.cancelled:
                    job.error = RuntimeClosingError("mutation request was cancelled")
                    job.done.set()
                    continue
                if self.closing:
                    job.error = RuntimeClosingError("mutation lane is closing")
                    job.done.set()
                    continue
                try:
                    job.result = await self._run_sync(job.callback)
                except Exception as exc:
                    job.error = exc
                finally:
                    job.done.set()

    async def _run_sync(self, callback: Callable[[], object]) -> Any:
        self._ensure_open()
        # Keep cancellation deferred until the worker returns. In particular,
        # download admission must remain held for the entire synchronous call.
        return await anyio.to_thread.run_sync(callback, abandon_on_cancel=False)

    def _ensure_open(self) -> None:
        if self.closing:
            raise RuntimeClosingError("Hovermap runtime is shutting down")


def create_runtime(
    config: ServerConfig,
    *,
    client: HovermapHttpClient | None = None,
) -> HovermapRuntime:
    """Create a runtime, optionally injecting a fake client for tests."""

    selected_client = client or HovermapHttpClient(
        config.hovermap_url,
        request_timeout=config.request_timeout_seconds,
        download_timeout=config.download_timeout_seconds,
        max_json_bytes=config.max_json_bytes,
        max_download_bytes=config.max_download_bytes,
    )
    return HovermapRuntime(selected_client, config.download_directory)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
