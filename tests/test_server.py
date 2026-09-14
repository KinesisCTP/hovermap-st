from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from mcp import Client

from hovermap_direct.http_client import HovermapResponseError
from hovermap_mcp.configuration import ServerConfig
from hovermap_mcp.errors import RuntimeBusyError, RuntimeClosingError
from hovermap_mcp.runtime import HovermapRuntime
from hovermap_mcp.server import (
    TOOL_NAMES,
    TOOLS,
    _ShutdownOnEndReceiveStream,
    _tools_only_initialization_options,
    create_server,
    dispatch_tool,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(tmp_path: Path) -> ServerConfig:
    return ServerConfig(
        profile="wifi",
        hovermap_url="http://10.9.0.1",
        download_directory=tmp_path,
        request_timeout_seconds=5,
        download_timeout_seconds=450,
        max_json_bytes=4_194_304,
        max_download_bytes=137_438_953_472,
        log_level="INFO",
    )


class FakeRuntime:
    async def get_status(self) -> dict[str, Any]:
        return {
            "scan_prefix": "KINESIS",
            "current_scan_name": "KINESIS_42",
            "free_space_bytes": 1234,
            "scan_running": True,
            "state": "Mapping",
            "retrieved_at_utc": "2026-09-14T07:00:00Z",
        }

    async def set_scan_prefix(self, prefix: str) -> dict[str, Any]:
        return {
            "prefix": prefix,
            "acknowledgement": "http_2xx",
            "state_confirmed": False,
        }

    async def start_scan(self) -> dict[str, Any]:
        return {
            "mission_type": 1,
            "acknowledgement": "http_2xx",
            "state_confirmed": False,
        }

    async def stop_scan(self) -> dict[str, Any]:
        return {"acknowledgement": "http_2xx", "state_confirmed": False}

    async def list_scans(self) -> dict[str, Any]:
        return {
            "count": 1,
            "ordering": "numeric_time_desc",
            "scans": [
                {
                    "name": "KINESIS_42",
                    "size_bytes": 12,
                    "timestamp": 42,
                    "parsed_time_utc": None,
                }
            ],
            "warnings": [],
        }

    async def download_scan(self, scan_name: str) -> dict[str, Any]:
        return {
            "scan_name": scan_name,
            "path": f"/tmp/{scan_name}.zip",
            "size_bytes": 12,
            "zip_validated": True,
            "published_atomically": True,
        }


def test_exact_six_tools_and_only_tools_capability(tmp_path: Path) -> None:
    assert TOOL_NAMES == (
        "get_status",
        "set_scan_prefix",
        "start_scan",
        "stop_scan",
        "list_scans",
        "download_scan",
    )
    assert len(TOOLS) == 6
    assert all(tool.input_schema["type"] == "object" for tool in TOOLS)
    assert all(tool.output_schema is not None for tool in TOOLS)

    server = create_server(_config(tmp_path))
    capabilities = _tools_only_initialization_options(server).capabilities.model_dump(
        by_alias=True, exclude_none=True
    )
    assert set(capabilities) == {"tools"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("get_status", {}),
        ("set_scan_prefix", {"prefix": "KINESIS"}),
        ("start_scan", {}),
        ("stop_scan", {}),
        ("list_scans", {}),
        ("download_scan", {"scan_name": "KINESIS_42"}),
    ],
)
async def test_each_tool_returns_structured_success(
    operation: str, arguments: dict[str, Any]
) -> None:
    result = await dispatch_tool(FakeRuntime(), operation, arguments)  # type: ignore[arg-type]

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["ok"] is True
    assert result.structured_content["operation"] == operation
    assert result.content[0].type == "text"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("get_status", {"surprise": True}),
        ("set_scan_prefix", {}),
        ("set_scan_prefix", {"prefix": "has space"}),
        ("set_scan_prefix", {"prefix": "é"}),
        ("start_scan", {"mission_type": 2}),
        ("download_scan", {"scan_name": "../escape"}),
        ("download_scan", {"scan_name": "A_1", "path": "/tmp"}),
        ("not_a_tool", {}),
    ],
)
async def test_invalid_calls_return_model_visible_errors(
    operation: str, arguments: dict[str, Any]
) -> None:
    result = await dispatch_tool(FakeRuntime(), operation, arguments)  # type: ignore[arg-type]

    assert result.is_error is True
    assert result.structured_content == {
        "ok": False,
        "operation": operation,
        "error": {
            "code": "invalid_argument",
            "message": result.structured_content["error"]["message"],
            "retryable": False,
        },
    }


class HttpFailureRuntime(FakeRuntime):
    async def get_status(self) -> dict[str, Any]:
        raise HovermapResponseError("GET", "http://10.9.0.1/status", 503, "x")


@pytest.mark.anyio
async def test_http_failure_is_sanitized_and_includes_status() -> None:
    result = await dispatch_tool(HttpFailureRuntime(), "get_status", {})  # type: ignore[arg-type]

    assert result.is_error is True
    assert result.structured_content == {
        "ok": False,
        "operation": "get_status",
        "error": {
            "code": "http_error",
            "message": "The Hovermap returned HTTP 503.",
            "retryable": True,
            "http_status": 503,
        },
    }


@dataclass(frozen=True)
class _Download:
    scan_name: str
    path: Path
    size_bytes: int


class BlockingClient:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.download_started = threading.Event()
        self.allow_download = threading.Event()
        self.cancelled = False
        self.mutations: list[str] = []

    def cancel(self) -> None:
        self.cancelled = True
        self.allow_download.set()

    def set_scan_prefix(self, prefix: str) -> None:
        self.mutations.append(f"prefix:{prefix}:start")
        time.sleep(0.02)
        self.mutations.append(f"prefix:{prefix}:end")

    def start_scan(self) -> None:
        self.mutations.append("start:start")
        time.sleep(0.01)
        self.mutations.append("start:end")

    def stop_scan(self) -> None:
        self.mutations.append("stop:start")
        self.mutations.append("stop:end")

    def download_scan(self, scan_name: str, _root: Path) -> _Download:
        self.download_started.set()
        if not self.allow_download.wait(timeout=2):
            raise TimeoutError("test download timed out")
        return _Download(scan_name, self.root / f"{scan_name}.zip", 9)


@pytest.mark.anyio
async def test_mutations_execute_in_one_ordered_lane(tmp_path: Path) -> None:
    client = BlockingClient(tmp_path)
    runtime = HovermapRuntime(client, tmp_path)  # type: ignore[arg-type]

    async with runtime.running(), anyio.create_task_group() as task_group:
        task_group.start_soon(runtime.set_scan_prefix, "ONE")
        await anyio.sleep(0)
        task_group.start_soon(runtime.start_scan)
        await anyio.sleep(0)
        task_group.start_soon(runtime.stop_scan)

    assert client.mutations == [
        "prefix:ONE:start",
        "prefix:ONE:end",
        "start:start",
        "start:end",
        "stop:start",
        "stop:end",
    ]
    assert client.cancelled is True


class GatedMutationClient(BlockingClient):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.first_started = threading.Event()
        self.release_first = threading.Event()

    def set_scan_prefix(self, prefix: str) -> None:
        self.mutations.append(prefix)
        if prefix == "FIRST":
            self.first_started.set()
            if not self.release_first.wait(timeout=2):
                raise TimeoutError("test mutation timed out")


@pytest.mark.anyio
async def test_mutation_lane_rejects_work_beyond_its_bounded_queue(tmp_path: Path) -> None:
    client = GatedMutationClient(tmp_path)
    runtime = HovermapRuntime(
        client,
        tmp_path,
        mutation_queue_capacity=1,  # type: ignore[arg-type]
    )

    async with runtime.running(), anyio.create_task_group() as task_group:
        task_group.start_soon(runtime.set_scan_prefix, "FIRST")
        started = await anyio.to_thread.run_sync(client.first_started.wait, 1)
        assert started is True
        task_group.start_soon(runtime.set_scan_prefix, "SECOND")
        await anyio.sleep(0)
        with pytest.raises(RuntimeBusyError, match="mutation queue"):
            await runtime.set_scan_prefix("THIRD")
        client.release_first.set()

    assert client.mutations == ["FIRST", "SECOND"]


@pytest.mark.anyio
async def test_cancelled_queued_mutation_is_not_executed_later(tmp_path: Path) -> None:
    client = GatedMutationClient(tmp_path)
    runtime = HovermapRuntime(
        client,
        tmp_path,
        mutation_queue_capacity=1,  # type: ignore[arg-type]
    )
    queued_scope: list[anyio.CancelScope] = []
    scope_ready = anyio.Event()

    async def queue_then_cancel() -> None:
        with anyio.CancelScope() as scope:
            queued_scope.append(scope)
            scope_ready.set()
            await runtime.set_scan_prefix("CANCELLED")

    async with runtime.running(), anyio.create_task_group() as task_group:
        task_group.start_soon(runtime.set_scan_prefix, "FIRST")
        started = await anyio.to_thread.run_sync(client.first_started.wait, 1)
        assert started is True
        task_group.start_soon(queue_then_cancel)
        await scope_ready.wait()
        await anyio.sleep(0)
        queued_scope[0].cancel()
        await anyio.sleep(0)
        client.release_first.set()

    assert client.mutations == ["FIRST"]


@pytest.mark.anyio
async def test_second_download_is_rejected_before_executor_submission(
    tmp_path: Path,
) -> None:
    client = BlockingClient(tmp_path)
    runtime = HovermapRuntime(client, tmp_path)  # type: ignore[arg-type]
    first_result: dict[str, Any] = {}

    async def first_download() -> None:
        first_result.update(await runtime.download_scan("KINESIS_1"))

    async with runtime.running(), anyio.create_task_group() as task_group:
        task_group.start_soon(first_download)
        started = await anyio.to_thread.run_sync(client.download_started.wait, 1)
        assert started is True
        with pytest.raises(RuntimeBusyError):
            await runtime.download_scan("KINESIS_2")
        client.allow_download.set()

    assert first_result["scan_name"] == "KINESIS_1"


class StatusClient(BlockingClient):
    def get_status(self) -> SimpleNamespace:
        return SimpleNamespace(
            scan_prefix="KINESIS",
            current_scan_name="KINESIS_1",
            free_space_bytes=50,
            scan_running=None,
            state="Future State",
        )


@pytest.mark.anyio
async def test_status_preserves_unknown_tri_state(tmp_path: Path) -> None:
    runtime = HovermapRuntime(StatusClient(tmp_path), tmp_path)  # type: ignore[arg-type]
    async with runtime.running():
        result = await runtime.get_status()
    assert result["state"] == "Future State"
    assert result["scan_running"] is None
    assert result["retrieved_at_utc"].endswith("Z")


@pytest.mark.anyio
async def test_shutdown_requests_client_cancellation_before_rejecting_work(
    tmp_path: Path,
) -> None:
    client = StatusClient(tmp_path)
    runtime = HovermapRuntime(client, tmp_path)  # type: ignore[arg-type]

    async with runtime.running():
        runtime.request_shutdown()
        assert client.cancelled is True
        assert runtime.closing is True
        with pytest.raises(RuntimeClosingError):
            await runtime.get_status()


@pytest.mark.anyio
async def test_input_eof_cancels_an_active_download_immediately(tmp_path: Path) -> None:
    client = BlockingClient(tmp_path)
    runtime = HovermapRuntime(client, tmp_path)  # type: ignore[arg-type]
    send_stream, receive_stream = anyio.create_memory_object_stream[Any](0)
    guarded_stream = _ShutdownOnEndReceiveStream(receive_stream, runtime)

    async with runtime.running(), anyio.create_task_group() as task_group:
        task_group.start_soon(runtime.download_scan, "KINESIS_1")
        started = await anyio.to_thread.run_sync(client.download_started.wait, 1)
        assert started is True

        await send_stream.aclose()
        with pytest.raises(anyio.EndOfStream):
            await guarded_stream.receive()

        assert client.cancelled is True


class CompleteClient:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def get_status(self) -> SimpleNamespace:
        return SimpleNamespace(
            scan_prefix="KINESIS",
            current_scan_name="KINESIS_42",
            free_space_bytes=1234,
            scan_running=True,
            state="Mapping",
        )

    def set_scan_prefix(self, _prefix: str) -> None:
        return None

    def start_scan(self) -> None:
        return None

    def stop_scan(self) -> None:
        return None

    def list_scans(self) -> SimpleNamespace:
        return SimpleNamespace(
            scans=(
                SimpleNamespace(
                    name="KINESIS_42",
                    size_bytes=12,
                    timestamp="2026-09-14T07:00:00Z",
                    parsed_time_utc="2026-09-14T07:00:00Z",
                ),
            ),
            ordering="iso8601_time_desc",
            warnings=(),
        )

    def download_scan(self, scan_name: str, _root: Path) -> _Download:
        return _Download(scan_name, self.root / f"{scan_name}.zip", 12)


@pytest.mark.anyio
async def test_real_mcp_client_discovers_and_calls_every_tool(tmp_path: Path) -> None:
    http_client = CompleteClient(tmp_path)
    server = create_server(
        _config(tmp_path),
        client=http_client,  # type: ignore[arg-type]
    )

    async with Client(server) as client:
        assert client.server_capabilities is not None
        assert client.server_capabilities.tools is not None
        assert client.server_capabilities.resources is None
        assert client.server_capabilities.prompts is None
        listed = await client.list_tools()
        assert tuple(tool.name for tool in listed.tools) == TOOL_NAMES

        calls = [
            ("get_status", {}),
            ("set_scan_prefix", {"prefix": "KINESIS"}),
            ("start_scan", {}),
            ("stop_scan", {}),
            ("list_scans", {}),
            ("download_scan", {"scan_name": "KINESIS_42"}),
        ]
        for name, arguments in calls:
            result = await client.call_tool(name, arguments)
            assert result.is_error is False
            assert result.structured_content is not None
            assert result.structured_content["operation"] == name

        invalid = await client.call_tool("set_scan_prefix", {"prefix": "not valid"})
        assert invalid.is_error is True
        assert invalid.structured_content is not None
        assert invalid.structured_content["error"]["code"] == "invalid_argument"

    assert http_client.cancelled is True
