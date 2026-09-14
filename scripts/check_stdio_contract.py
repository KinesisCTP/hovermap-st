#!/usr/bin/env python3
"""Exercise an installed Hovermap MCP server through a real stdio subprocess."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import tempfile
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import files as distribution_files
from importlib.metadata import requires, version
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

from jsonschema import Draft202012Validator
from mcp import Client, StdioServerParameters

import hovermap_direct
import hovermap_mcp

EXPECTED_TOOL_NAMES = (
    "get_status",
    "set_scan_prefix",
    "start_scan",
    "stop_scan",
    "list_scans",
    "download_scan",
)

NO_ARGUMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

EXPECTED_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "get_status": NO_ARGUMENTS_SCHEMA,
    "set_scan_prefix": {
        "type": "object",
        "properties": {
            "prefix": {
                "type": "string",
                "minLength": 1,
                "maxLength": 20,
                "pattern": "^[A-Za-z0-9]+$",
            }
        },
        "required": ["prefix"],
        "additionalProperties": False,
    },
    "start_scan": NO_ARGUMENTS_SCHEMA,
    "stop_scan": NO_ARGUMENTS_SCHEMA,
    "list_scans": NO_ARGUMENTS_SCHEMA,
    "download_scan": {
        "type": "object",
        "properties": {
            "scan_name": {
                "type": "string",
                "maxLength": 64,
                "pattern": "^[A-Za-z0-9]{1,20}_[0-9]+$",
            }
        },
        "required": ["scan_name"],
        "additionalProperties": False,
    },
}

ERROR_CODES = [
    "invalid_argument",
    "transport_error",
    "http_error",
    "protocol_error",
    "busy",
    "filesystem_error",
    "cancelled",
    "internal_error",
]


def _expected_output_schema(
    operation: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "ok": {"const": True},
                    "operation": {"const": operation},
                    **properties,
                },
                "required": ["ok", "operation", *required],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "ok": {"const": False},
                    "operation": {"const": operation},
                    "error": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "enum": ERROR_CODES},
                            "message": {"type": "string"},
                            "retryable": {"type": "boolean"},
                            "http_status": {
                                "type": "integer",
                                "minimum": 100,
                                "maximum": 599,
                            },
                        },
                        "required": ["code", "message", "retryable"],
                        "additionalProperties": False,
                    },
                },
                "required": ["ok", "operation", "error"],
                "additionalProperties": False,
            },
        ],
    }


EXPECTED_OUTPUT_SCHEMAS = {
    "get_status": _expected_output_schema(
        "get_status",
        {
            "scan_prefix": {"type": "string"},
            "current_scan_name": {"type": "string"},
            "free_space_bytes": {"type": "integer", "minimum": 0},
            "scan_running": {"type": ["boolean", "null"]},
            "state": {"type": "string"},
            "retrieved_at_utc": {"type": "string", "format": "date-time"},
        },
        [
            "scan_prefix",
            "current_scan_name",
            "free_space_bytes",
            "scan_running",
            "state",
            "retrieved_at_utc",
        ],
    ),
    "set_scan_prefix": _expected_output_schema(
        "set_scan_prefix",
        {
            "prefix": {"type": "string"},
            "acknowledgement": {"const": "http_2xx"},
            "state_confirmed": {"const": False},
        },
        ["prefix", "acknowledgement", "state_confirmed"],
    ),
    "start_scan": _expected_output_schema(
        "start_scan",
        {
            "mission_type": {"const": 1},
            "acknowledgement": {"const": "http_2xx"},
            "state_confirmed": {"const": False},
        },
        ["mission_type", "acknowledgement", "state_confirmed"],
    ),
    "stop_scan": _expected_output_schema(
        "stop_scan",
        {
            "acknowledgement": {"const": "http_2xx"},
            "state_confirmed": {"const": False},
        },
        ["acknowledgement", "state_confirmed"],
    ),
    "list_scans": _expected_output_schema(
        "list_scans",
        {
            "count": {"type": "integer", "minimum": 0},
            "ordering": {
                "type": "string",
                "enum": ["numeric_time_desc", "iso8601_time_desc", "device_order"],
            },
            "scans": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "size_bytes": {"type": "integer", "minimum": 0},
                        "timestamp": {"type": ["number", "string"]},
                        "parsed_time_utc": {"type": ["string", "null"]},
                    },
                    "required": ["name", "size_bytes", "timestamp", "parsed_time_utc"],
                    "additionalProperties": False,
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        ["count", "ordering", "scans", "warnings"],
    ),
    "download_scan": _expected_output_schema(
        "download_scan",
        {
            "scan_name": {"type": "string"},
            "path": {"type": "string"},
            "size_bytes": {"type": "integer", "minimum": 0},
            "zip_validated": {"const": True},
            "published_atomically": {"const": True},
        },
        ["scan_name", "path", "size_bytes", "zip_validated", "published_atomically"],
    ),
}

EXPECTED_ANNOTATIONS = {
    "get_status": (True, None, None, True),
    "set_scan_prefix": (False, True, True, True),
    "start_scan": (False, False, False, True),
    "stop_scan": (False, True, True, True),
    "list_scans": (True, None, None, True),
    "download_scan": (False, True, True, True),
}


def _zip_payload() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("metadata.txt", "hovermap stdio acceptance fixture")
    return output.getvalue()


SCAN_ARCHIVE = _zip_payload()


class _MockHovermapHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    requests_seen: ClassVar[list[tuple[str, dict[str, list[str]]]]] = []
    status_surrogate = False
    files_surrogate = False

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        self.__class__.requests_seen.append((parsed.path, parse_qs(parsed.query)))
        if parsed.path == "/status":
            if self.__class__.status_surrogate:
                self._bytes(
                    b'{"scan_name":"\\ud800","scan_dir":"KINESIS_1",'
                    b'"freeSpace":987654,"state":"Stopped"}',
                    "application/json",
                )
            else:
                self._json(
                    {
                        "scan_name": "KINESIS",
                        "scan_dir": "KINESIS_1",
                        "freeSpace": "987654B",
                        "state": "Stopped",
                    }
                )
        elif parsed.path in ("/setprefix", "/startsystem", "/stopsystem"):
            self._bytes(b"ok", "text/plain")
        elif parsed.path == "/files":
            if self.__class__.files_surrogate:
                self._bytes(
                    b'{"scans":[{"scan":"KINESIS_1","size":1,"time":"\\ud800"}]}',
                    "application/json",
                )
            else:
                self._json(
                    {
                        "scans": [
                            {"scan": "KINESIS_1", "size": len(SCAN_ARCHIVE), "time": 1},
                            {"scan": "KINESIS_2", "size": 42, "time": 2},
                        ]
                    }
                )
        elif parsed.path == "/downloadscan":
            self._bytes(SCAN_ARCHIVE, "application/zip")
        else:
            self.send_error(404, "not found")

    def _json(self, value: object) -> None:
        self._bytes(json.dumps(value).encode("utf-8"), "application/json")

    def _bytes(self, value: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(value)))
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            self.wfile.write(value)


@contextmanager
def mock_hovermap() -> Iterator[str]:
    _MockHovermapHandler.requests_seen = []
    _MockHovermapHandler.status_surrogate = False
    _MockHovermapHandler.files_surrogate = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockHovermapHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _check_tool_contract(tool: Any) -> None:
    if not tool.description:
        raise AssertionError(f"{tool.name} has no description")
    if tool.input_schema != EXPECTED_INPUT_SCHEMAS[tool.name]:
        raise AssertionError(f"{tool.name} has an unexpected input schema")
    if tool.output_schema != EXPECTED_OUTPUT_SCHEMAS[tool.name]:
        raise AssertionError(f"{tool.name} has an unexpected output schema")
    if tool.annotations is None:
        raise AssertionError(f"{tool.name} has no annotations")
    annotations = (
        tool.annotations.read_only_hint,
        tool.annotations.destructive_hint,
        tool.annotations.idempotent_hint,
        tool.annotations.open_world_hint,
    )
    if annotations != EXPECTED_ANNOTATIONS[tool.name]:
        raise AssertionError(f"{tool.name} has unexpected annotations: {annotations}")


def _check_result(tool: Any, result: Any, *, is_error: bool = False) -> dict[str, Any]:
    if result.is_error is not is_error:
        raise AssertionError(f"{tool.name} returned is_error={result.is_error!r}")
    if not result.content or getattr(result.content[0], "text", "") == "":
        raise AssertionError(f"{tool.name} returned no explanatory text")
    structured = result.structured_content
    if not isinstance(structured, dict):
        raise AssertionError(f"{tool.name} returned no structured content")
    Draft202012Validator(tool.output_schema).validate(structured)
    if structured.get("operation") != tool.name:
        raise AssertionError(f"{tool.name} returned the wrong operation marker")
    return structured


def _check_installed_wheel(checkout_root: Path) -> None:
    for module in (hovermap_direct, hovermap_mcp):
        module_path = Path(module.__file__).resolve()
        with suppress(ValueError):
            module_path.relative_to(checkout_root)
            raise AssertionError(f"{module.__name__} imported from the checkout, not the wheel")

    active_dependencies: set[str] = set()
    for raw_requirement in requires("kinesis-hovermap-mcp") or []:
        requirement_text, _, marker = raw_requirement.partition(";")
        if "extra" in marker:
            continue
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement_text)
        if match is None:
            raise AssertionError(f"could not parse runtime requirement: {raw_requirement}")
        active_dependencies.add(match.group(1).lower().replace("_", "-"))
    if active_dependencies != {"anyio", "mcp"}:
        raise AssertionError(f"unexpected runtime dependencies: {sorted(active_dependencies)}")
    if version("mcp") != "2.2.0":
        raise AssertionError(f"wheel environment did not use locked MCP 2.2.0: {version('mcp')}")

    packaged_paths = [
        str(path).lower() for path in distribution_files("kinesis-hovermap-mcp") or []
    ]
    forbidden = [path for path in packaged_paths if "hovermap_ros" in path or "mule" in path]
    if forbidden:
        raise AssertionError(f"wheel contains ROS or Mule paths: {forbidden}")


async def _check_quiet_eof(command: str, server_args: list[str]) -> None:
    process = await asyncio.create_subprocess_exec(
        command,
        *server_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "HOVERMAP_LOG_LEVEL": "ERROR"},
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise AssertionError("stdio subprocess did not expose all protocol pipes")
    stdout_task = asyncio.create_task(process.stdout.read())
    stderr_task = asyncio.create_task(process.stderr.read())
    process.stdin.close()
    with suppress(ConnectionError):
        await process.stdin.wait_closed()
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise AssertionError("server did not exit promptly on stdin EOF") from None
    stdout = await stdout_task
    stderr = await stderr_task
    if process.returncode != 0:
        raise AssertionError(
            f"server exited {process.returncode} on clean EOF: {stderr.decode(errors='replace')}"
        )
    if stdout:
        raise AssertionError(f"server wrote non-protocol output before initialization: {stdout!r}")


async def _read_protocol_frame(process: asyncio.subprocess.Process, label: str) -> dict[str, Any]:
    if process.stdout is None:
        raise AssertionError("stdio subprocess has no stdout pipe")
    try:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
    except TimeoutError:
        raise AssertionError(f"server did not return the {label} frame") from None
    if not line:
        raise AssertionError(f"server closed stdout before the {label} frame")
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssertionError(f"server wrote non-protocol stdout for {label}: {line!r}") from exc
    if not isinstance(value, dict):
        raise AssertionError(f"server returned a non-object {label} frame: {value!r}")
    return value


async def _check_raw_initialized_session(command: str, server_args: list[str]) -> None:
    """Verify every stdout byte in an initialized raw session is an expected frame."""

    process = await asyncio.create_subprocess_exec(
        command,
        *server_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "HOVERMAP_LOG_LEVEL": "ERROR"},
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise AssertionError("stdio subprocess did not expose all protocol pipes")

    async def send(value: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
        await process.stdin.drain()

    try:
        await send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "hovermap-raw-acceptance", "version": "1"},
                },
            }
        )
        initialized = await _read_protocol_frame(process, "initialize response")
        if initialized.get("id") != 1 or "result" not in initialized:
            raise AssertionError(f"unexpected initialize response: {initialized}")
        capabilities = initialized["result"].get("capabilities")
        if not isinstance(capabilities, dict) or set(capabilities) != {"tools"}:
            raise AssertionError(f"raw session advertised non-tool capabilities: {capabilities}")

        await send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = await _read_protocol_frame(process, "tools/list response")
        if listed.get("id") != 2 or "result" not in listed:
            raise AssertionError(f"unexpected tools/list response: {listed}")
        names = tuple(tool.get("name") for tool in listed["result"].get("tools", []))
        if names != EXPECTED_TOOL_NAMES:
            raise AssertionError(f"raw session returned unexpected tools: {names}")

        process.stdin.close()
        with suppress(ConnectionError):
            await process.stdin.wait_closed()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            raise AssertionError("initialized server did not exit promptly on stdin EOF") from None
        remainder = await process.stdout.read()
        stderr = await process.stderr.read()
        if process.returncode != 0:
            raise AssertionError(
                f"initialized server exited {process.returncode}: {stderr.decode(errors='replace')}"
            )
        if remainder:
            raise AssertionError(f"server wrote unexpected stdout after tools/list: {remainder!r}")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def check(command: str, *, require_installed_wheel: bool) -> None:
    checkout_root = Path(__file__).resolve().parents[1]
    if require_installed_wheel:
        _check_installed_wheel(checkout_root)

    with tempfile.TemporaryDirectory(prefix="hovermap-mcp-contract-") as temporary:
        download_directory = Path(temporary) / "downloads"
        with mock_hovermap() as hovermap_url:
            server_args = [
                "--hovermap-url",
                hovermap_url,
                "--download-directory",
                str(download_directory),
                "--log-level",
                "ERROR",
            ]
            await _check_quiet_eof(command, server_args)
            await _check_raw_initialized_session(command, server_args)

            parameters = StdioServerParameters(command=command, args=server_args)
            async with Client(parameters) as client:
                capabilities = client.server_capabilities
                if capabilities is None:
                    raise AssertionError("server did not return capabilities")
                advertised = capabilities.model_dump(exclude_none=True)
                if set(advertised) != {"tools"}:
                    raise AssertionError(f"server advertised non-tool capabilities: {advertised}")

                result = await client.list_tools()
                names = tuple(tool.name for tool in result.tools)
                if names != EXPECTED_TOOL_NAMES:
                    raise AssertionError(f"unexpected tool sequence: {names}")
                tools = {tool.name: tool for tool in result.tools}
                for tool in result.tools:
                    _check_tool_contract(tool)

                status = _check_result(tools["get_status"], await client.call_tool("get_status"))
                if status["scan_running"] is not False or status["free_space_bytes"] != 987654:
                    raise AssertionError(f"unexpected status result: {status}")

                _MockHovermapHandler.status_surrogate = True
                invalid_status = _check_result(
                    tools["get_status"], await client.call_tool("get_status"), is_error=True
                )
                _MockHovermapHandler.status_surrogate = False
                if invalid_status["error"]["code"] != "protocol_error":
                    raise AssertionError(f"unexpected surrogate status error: {invalid_status}")

                prefix = _check_result(
                    tools["set_scan_prefix"],
                    await client.call_tool("set_scan_prefix", {"prefix": "KINESIS"}),
                )
                if prefix["state_confirmed"] is not False:
                    raise AssertionError("prefix acknowledgement overclaimed device state")

                start = _check_result(tools["start_scan"], await client.call_tool("start_scan"))
                if start["mission_type"] != 1 or start["state_confirmed"] is not False:
                    raise AssertionError("start acknowledgement contract changed")

                stop = _check_result(tools["stop_scan"], await client.call_tool("stop_scan"))
                if stop["state_confirmed"] is not False:
                    raise AssertionError("stop acknowledgement overclaimed device state")

                scans = _check_result(tools["list_scans"], await client.call_tool("list_scans"))
                if scans["ordering"] != "numeric_time_desc":
                    raise AssertionError(f"unexpected scan ordering: {scans}")
                if [scan["name"] for scan in scans["scans"]] != ["KINESIS_2", "KINESIS_1"]:
                    raise AssertionError(f"scans were not sorted newest-first: {scans}")

                _MockHovermapHandler.files_surrogate = True
                invalid_scans = _check_result(
                    tools["list_scans"], await client.call_tool("list_scans"), is_error=True
                )
                _MockHovermapHandler.files_surrogate = False
                if invalid_scans["error"]["code"] != "protocol_error":
                    raise AssertionError(f"unexpected surrogate scan error: {invalid_scans}")

                downloaded = _check_result(
                    tools["download_scan"],
                    await client.call_tool("download_scan", {"scan_name": "KINESIS_1"}),
                )
                archive_path = Path(downloaded["path"])
                if archive_path != (download_directory / "KINESIS_1.zip").resolve():
                    raise AssertionError(f"download escaped its configured root: {downloaded}")
                if downloaded["size_bytes"] != len(SCAN_ARCHIVE) or not archive_path.is_file():
                    raise AssertionError(f"download result did not match the archive: {downloaded}")
                with zipfile.ZipFile(archive_path) as archive:
                    if archive.testzip() is not None:
                        raise AssertionError("downloaded acceptance archive failed CRC validation")

                invalid = _check_result(
                    tools["set_scan_prefix"],
                    await client.call_tool("set_scan_prefix", {"prefix": "not-valid"}),
                    is_error=True,
                )
                if invalid["error"]["code"] != "invalid_argument":
                    raise AssertionError(f"unexpected structured validation error: {invalid}")

        expected_requests = [
            ("/status", {}),
            ("/status", {}),
            ("/setprefix", {"prefix": ["KINESIS"]}),
            ("/startsystem", {"mission_type": ["1"]}),
            ("/stopsystem", {}),
            ("/files", {}),
            ("/files", {}),
            ("/downloadscan", {"scanname": ["KINESIS_1"]}),
        ]
        if _MockHovermapHandler.requests_seen != expected_requests:
            raise AssertionError(
                f"server used unexpected HTTP endpoints: {_MockHovermapHandler.requests_seen}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", default="hovermap-mcp")
    parser.add_argument("--require-installed-wheel", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(
        check(
            arguments.command,
            require_installed_wheel=arguments.require_installed_wheel,
        )
    )


if __name__ == "__main__":
    main()
