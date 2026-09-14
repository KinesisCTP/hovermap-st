"""Low-level, tools-only MCP server for direct Hovermap operation."""

from __future__ import annotations

import logging
import os
import re
import signal
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import anyio
from anyio.abc import ObjectReceiveStream
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)

from hovermap_direct.http_client import HovermapHttpClient

from . import __version__
from .configuration import ConfigurationError, ServerConfig, parse_config
from .errors import ToolFailure, map_exception
from .runtime import HovermapRuntime, create_runtime

LOGGER = logging.getLogger(__name__)
SCAN_PREFIX_RE = re.compile(r"^[A-Za-z0-9]{1,20}$", re.ASCII)
SCAN_NAME_RE = re.compile(r"^[A-Za-z0-9]{1,20}_[0-9]+$", re.ASCII)


class _ShutdownOnEndReceiveStream(ObjectReceiveStream[Any]):
    """Notify the runtime as soon as the MCP input side disappears."""

    def __init__(self, inner: ObjectReceiveStream[Any], runtime: HovermapRuntime) -> None:
        self._inner = inner
        self._runtime = runtime

    async def receive(self) -> Any:
        try:
            return await self._inner.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError):
            self._runtime.request_shutdown()
            raise

    async def aclose(self) -> None:
        self._runtime.request_shutdown()
        await self._inner.aclose()


NO_ARGUMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

PREFIX_INPUT_SCHEMA: dict[str, Any] = {
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
}

DOWNLOAD_INPUT_SCHEMA: dict[str, Any] = {
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
}


def _success_output_schema(
    operation: str,
    properties: Mapping[str, Any],
    required: Sequence[str],
) -> dict[str, Any]:
    success_properties = {
        "ok": {"const": True},
        "operation": {"const": operation},
        **properties,
    }
    error_properties = {
        "ok": {"const": False},
        "operation": {"const": operation},
        "error": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "enum": [
                        "invalid_argument",
                        "transport_error",
                        "http_error",
                        "protocol_error",
                        "busy",
                        "filesystem_error",
                        "cancelled",
                        "internal_error",
                    ],
                },
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
    }
    return {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "properties": success_properties,
                "required": ["ok", "operation", *required],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": error_properties,
                "required": ["ok", "operation", "error"],
                "additionalProperties": False,
            },
        ],
    }


GET_STATUS_OUTPUT_SCHEMA = _success_output_schema(
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
)

SET_PREFIX_OUTPUT_SCHEMA = _success_output_schema(
    "set_scan_prefix",
    {
        "prefix": {"type": "string"},
        "acknowledgement": {"const": "http_2xx"},
        "state_confirmed": {"const": False},
    },
    ["prefix", "acknowledgement", "state_confirmed"],
)

START_SCAN_OUTPUT_SCHEMA = _success_output_schema(
    "start_scan",
    {
        "mission_type": {"const": 1},
        "acknowledgement": {"const": "http_2xx"},
        "state_confirmed": {"const": False},
    },
    ["mission_type", "acknowledgement", "state_confirmed"],
)

STOP_SCAN_OUTPUT_SCHEMA = _success_output_schema(
    "stop_scan",
    {
        "acknowledgement": {"const": "http_2xx"},
        "state_confirmed": {"const": False},
    },
    ["acknowledgement", "state_confirmed"],
)

LIST_SCANS_OUTPUT_SCHEMA = _success_output_schema(
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
                "required": [
                    "name",
                    "size_bytes",
                    "timestamp",
                    "parsed_time_utc",
                ],
                "additionalProperties": False,
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    ["count", "ordering", "scans", "warnings"],
)

DOWNLOAD_OUTPUT_SCHEMA = _success_output_schema(
    "download_scan",
    {
        "scan_name": {"type": "string"},
        "path": {"type": "string"},
        "size_bytes": {"type": "integer", "minimum": 0},
        "zip_validated": {"const": True},
        "published_atomically": {"const": True},
    },
    [
        "scan_name",
        "path",
        "size_bytes",
        "zip_validated",
        "published_atomically",
    ],
)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="get_status",
        description=(
            "Read the Hovermap status and free storage. scan_running is derived "
            "conservatively from the unmodified device state and may be null."
        ),
        input_schema=NO_ARGUMENTS_SCHEMA,
        output_schema=GET_STATUS_OUTPUT_SCHEMA,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    ),
    Tool(
        name="set_scan_prefix",
        description=(
            "Request a 1-20 character ASCII alphanumeric scan prefix. HTTP 2xx "
            "acknowledges only the request; use get_status to observe device state."
        ),
        input_schema=PREFIX_INPUT_SCHEMA,
        output_schema=SET_PREFIX_OUTPUT_SCHEMA,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    Tool(
        name="start_scan",
        description=(
            "Request a Hovermap Mapping mission (mission type 1). HTTP 2xx does "
            "not confirm the transition; use get_status to observe it."
        ),
        input_schema=NO_ARGUMENTS_SCHEMA,
        output_schema=START_SCAN_OUTPUT_SCHEMA,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    ),
    Tool(
        name="stop_scan",
        description=(
            "Request that the Hovermap stop its current mission. HTTP 2xx does "
            "not confirm the transition; use get_status to observe it."
        ),
        input_schema=NO_ARGUMENTS_SCHEMA,
        output_schema=STOP_SCAN_OUTPUT_SCHEMA,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    Tool(
        name="list_scans",
        description=(
            "List completed scans reported by the Hovermap, preserving original "
            "timestamps and declaring whether sorting was safe."
        ),
        input_schema=NO_ARGUMENTS_SCHEMA,
        output_schema=LIST_SCANS_OUTPUT_SCHEMA,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    ),
    Tool(
        name="download_scan",
        description=(
            "Download one named scan ZIP beneath the configured local directory, "
            "validate its CRC, and atomically publish or replace the archive."
        ),
        input_schema=DOWNLOAD_INPUT_SCHEMA,
        output_schema=DOWNLOAD_OUTPUT_SCHEMA,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
)

TOOL_NAMES = tuple(tool.name for tool in TOOLS)


async def dispatch_tool(
    runtime: HovermapRuntime,
    operation: str,
    arguments: Mapping[str, Any] | None,
) -> CallToolResult:
    """Validate and execute one advertised tool without leaking expected exceptions."""

    try:
        args = _validate_arguments(operation, arguments)
        if operation == "get_status":
            fields = await runtime.get_status()
            text = (
                f"Hovermap state is {fields['state']!r}; scan_running is "
                f"{fields['scan_running']!r}, with {fields['free_space_bytes']} bytes free."
            )
        elif operation == "set_scan_prefix":
            fields = await runtime.set_scan_prefix(args["prefix"])
            text = (
                f"Hovermap returned HTTP 2xx for prefix {args['prefix']!r}; "
                "the resulting state is not confirmed."
            )
        elif operation == "start_scan":
            fields = await runtime.start_scan()
            text = (
                "Hovermap returned HTTP 2xx for Mapping mission type 1; "
                "the resulting state is not confirmed."
            )
        elif operation == "stop_scan":
            fields = await runtime.stop_scan()
            text = (
                "Hovermap returned HTTP 2xx for the stop request; "
                "the resulting state is not confirmed."
            )
        elif operation == "list_scans":
            fields = await runtime.list_scans()
            text = f"Hovermap reported {fields['count']} completed scans ({fields['ordering']})."
        elif operation == "download_scan":
            fields = await runtime.download_scan(args["scan_name"])
            text = (
                f"Downloaded {fields['scan_name']!r} to {fields['path']} "
                f"({fields['size_bytes']} bytes); ZIP validated and atomically published."
            )
        else:  # _validate_arguments normally catches this first.
            raise ToolFailure("invalid_argument", "Unknown Hovermap tool.", False)
        structured = {"ok": True, "operation": operation, **fields}
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=structured,
        )
    except Exception as exc:
        failure = map_exception(exc)
        if failure.code == "internal_error":
            LOGGER.exception("Unexpected error while executing %s", operation)
        error: dict[str, Any] = {
            "code": failure.code,
            "message": failure.message,
            "retryable": failure.retryable,
        }
        if failure.http_status is not None:
            error["http_status"] = failure.http_status
        structured = {
            "ok": False,
            "operation": operation,
            "error": error,
        }
        return CallToolResult(
            content=[TextContent(type="text", text=failure.message)],
            structured_content=structured,
            is_error=True,
        )


def create_server(
    config: ServerConfig | None = None,
    *,
    client: HovermapHttpClient | None = None,
    runtime: HovermapRuntime | None = None,
) -> Server[HovermapRuntime]:
    """Build an exact-capability low-level MCP server."""

    if runtime is not None and client is not None:
        raise ValueError("inject either runtime or client, not both")
    if runtime is None and config is None:
        raise ValueError("config is required when runtime is not injected")

    @asynccontextmanager
    async def lifespan(_server: Server[HovermapRuntime]) -> AsyncIterator[HovermapRuntime]:
        selected = runtime or create_runtime(config, client=client)  # type: ignore[arg-type]
        async with selected.running():
            yield selected

    async def list_tools(
        _ctx: ServerRequestContext[HovermapRuntime],
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        return ListToolsResult(tools=list(TOOLS))

    async def call_tool(
        ctx: ServerRequestContext[HovermapRuntime],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        return await dispatch_tool(ctx.lifespan_context, params.name, params.arguments)

    return Server(
        "KINESIS Hovermap",
        version=__version__,
        lifespan=lifespan,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def _tools_only_initialization_options(server: Server[HovermapRuntime]) -> Any:
    """Remove empty optional capability maps from the wire advertisement."""

    options = server.create_initialization_options()
    capabilities = options.capabilities.model_copy(
        update={"experimental": None, "extensions": None}
    )
    return options.model_copy(update={"capabilities": capabilities})


def _validate_arguments(
    operation: str,
    arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if operation not in TOOL_NAMES:
        raise ToolFailure("invalid_argument", "Unknown Hovermap tool.", False)
    if arguments is None:
        args: dict[str, Any] = {}
    elif isinstance(arguments, Mapping):
        args = dict(arguments)
    else:
        raise ToolFailure("invalid_argument", "Tool arguments must be an object.", False)

    if operation in {"get_status", "start_scan", "stop_scan", "list_scans"}:
        if args:
            raise ToolFailure(
                "invalid_argument",
                f"{operation} accepts no arguments.",
                False,
            )
        return args

    argument_name = "prefix" if operation == "set_scan_prefix" else "scan_name"
    if set(args) != {argument_name}:
        raise ToolFailure(
            "invalid_argument",
            f"{operation} requires exactly the {argument_name!r} argument.",
            False,
        )
    value = args[argument_name]
    if not isinstance(value, str):
        raise ToolFailure("invalid_argument", f"{argument_name} must be text.", False)
    if operation == "set_scan_prefix":
        if SCAN_PREFIX_RE.fullmatch(value) is None:
            raise ToolFailure(
                "invalid_argument",
                "prefix must be 1-20 ASCII alphanumeric characters.",
                False,
            )
    elif len(value) > 64 or SCAN_NAME_RE.fullmatch(value) is None:
        raise ToolFailure(
            "invalid_argument",
            "scan_name must match '<1-20 ASCII alphanumeric characters>_<digits>' "
            "and be no longer than 64 characters.",
            False,
        )
    return args


async def run_stdio(config: ServerConfig) -> None:
    """Run one local stdio MCP connection until EOF, disconnect, or a signal."""

    runtime = create_runtime(config)
    server = create_server(runtime=runtime)

    async def run_connection() -> None:
        async with stdio_server() as (read_stream, write_stream):
            guarded_read_stream = _ShutdownOnEndReceiveStream(read_stream, runtime)
            await server.run(
                guarded_read_stream,
                write_stream,
                _tools_only_initialization_options(server),
            )

    if os.name == "nt":
        try:
            await run_connection()
        finally:
            runtime.request_shutdown()
        return

    with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
        async with anyio.create_task_group() as task_group:

            async def serve() -> None:
                try:
                    await run_connection()
                finally:
                    runtime.request_shutdown()
                    task_group.cancel_scope.cancel()

            async def stop_on_signal() -> None:
                async for signum in signals:
                    LOGGER.info("Received signal %s; shutting down", signum)
                    runtime.request_shutdown()
                    task_group.cancel_scope.cancel()
                    return

            task_group.start_soon(serve)
            task_group.start_soon(stop_on_signal)


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point; reserve stdout exclusively for MCP frames."""

    try:
        config = parse_config(argv)
    except ConfigurationError as exc:
        sys.stderr.write(f"hovermap-mcp: configuration error: {exc}\n")
        return 2

    logging.basicConfig(
        level=config.numeric_log_level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    try:
        anyio.run(run_stdio, config)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
