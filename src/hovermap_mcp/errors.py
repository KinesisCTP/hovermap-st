"""Stable, model-visible error mapping for Hovermap MCP tools."""

from __future__ import annotations

from dataclasses import dataclass

from hovermap_direct.http_client import (
    ControlInProgressError,
    DownloadInProgressError,
    HovermapCancelledError,
    HovermapProtocolError,
    HovermapResponseError,
    HovermapTransportError,
)

ERROR_CODES = frozenset(
    {
        "invalid_argument",
        "transport_error",
        "http_error",
        "protocol_error",
        "busy",
        "filesystem_error",
        "cancelled",
        "internal_error",
    }
)


@dataclass(frozen=True)
class ToolFailure(Exception):
    """A sanitized error safe to return as structured tool output."""

    code: str
    message: str
    retryable: bool
    http_status: int | None = None

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"unsupported tool error code: {self.code}")


class RuntimeBusyError(RuntimeError):
    """The server cannot admit another bounded operation."""


class RuntimeClosingError(RuntimeError):
    """The runtime is shutting down and accepts no new work."""


def map_exception(exc: BaseException) -> ToolFailure:
    """Map expected implementation exceptions without exposing internal details."""

    if isinstance(exc, ToolFailure):
        return exc
    if isinstance(exc, ValueError):
        return ToolFailure("invalid_argument", str(exc), False)
    if isinstance(exc, (ControlInProgressError, DownloadInProgressError, RuntimeBusyError)):
        return ToolFailure("busy", str(exc), True)
    if isinstance(exc, RuntimeClosingError):
        return ToolFailure("cancelled", "The Hovermap MCP server is shutting down.", True)
    if isinstance(exc, HovermapCancelledError):
        return ToolFailure("cancelled", "The Hovermap operation was cancelled.", True)
    if isinstance(exc, HovermapResponseError):
        status = int(exc.status)
        return ToolFailure(
            "http_error",
            f"The Hovermap returned HTTP {status}.",
            status in {408, 425, 429} or 500 <= status <= 599,
            status,
        )
    if isinstance(exc, HovermapTransportError):
        return ToolFailure(
            "transport_error",
            "The Hovermap could not be reached before the request completed.",
            True,
        )
    if isinstance(exc, HovermapProtocolError):
        return ToolFailure(
            "protocol_error",
            "The Hovermap returned an unexpected or invalid response.",
            False,
        )
    if isinstance(exc, OSError):
        return ToolFailure(
            "filesystem_error",
            "The local download filesystem operation failed.",
            False,
        )
    return ToolFailure(
        "internal_error",
        "The Hovermap MCP server encountered an unexpected internal error.",
        False,
    )
