"""Hardened, ROS-independent client for the Hovermap HTTP control API."""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import socket
import stat
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SCAN_PREFIX_MAX_LEN = 20
SCAN_NAME_MAX_LEN = 64
MAX_REQUEST_TIMEOUT_SECONDS = 300.0
MAX_DOWNLOAD_TIMEOUT_SECONDS = 3600.0
MAX_JSON_BYTES_LIMIT = 64 * 1024 * 1024
MAX_DOWNLOAD_BYTES_LIMIT = 1024 * 1024 * 1024 * 1024

SCAN_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9]{1,20}$", re.ASCII)
SCAN_NAME_PATTERN = re.compile(r"^[A-Za-z0-9]{1,20}_[0-9]+$", re.ASCII)
ISO_8601_SCAN_TIME_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})?$",
    re.ASCII,
)

RUNNING_STATES = frozenset({"Mapping"})
NON_RUNNING_STATES = frozenset(
    {
        "Stopped",
        "Disabled",
        "Booting",
        "USB Error",
        "USB Mounted",
        "Transferring Data",
        "Processes Stopped",
    }
)

_ALLOWED_ENDPOINTS = frozenset(
    {
        "/status",
        "/setprefix",
        "/startsystem",
        "/stopsystem",
        "/files",
        "/downloadscan",
    }
)
_DEVICE_ORDER_WARNING = (
    "/files timestamps are mixed or unparseable; preserving device order because "
    "the device's numeric time unit is undocumented"
)
_OPEN_POLL_SECONDS = 0.05
MAX_ZIP_MEMBERS = 100_000


class HovermapError(RuntimeError):
    """Base class for Hovermap HTTP failures."""


class HovermapTransportError(HovermapError):
    """Network-level timeout or connection failure."""


class HovermapResponseError(HovermapError):
    """Non-success HTTP status."""

    def __init__(self, method: str, url: str, status: int, reason: str) -> None:
        super().__init__(f"{method} {url} failed: HTTP {status} {reason}")
        self.method = method
        self.url = url
        self.status = status
        self.reason = reason


class HovermapProtocolError(HovermapError):
    """A successful response did not match the documented API shape."""


class DownloadInProgressError(HovermapError):
    """A second download was requested while one was in progress."""


class ControlInProgressError(HovermapError):
    """A prior state-changing HTTP request has not actually finished."""


class HovermapCancelledError(HovermapError):
    """A request was cancelled because the direct API client is shutting down."""


class _OpenStillRunningError(HovermapTransportError):
    """The caller timed out while its daemonized urllib open is still running."""

    def __init__(self, message: str, completion: threading.Event) -> None:
        super().__init__(message)
        self.completion = completion


class _RejectRedirects(HTTPRedirectHandler):
    """Turn every redirect into an HTTPError before following its Location."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _build_device_opener() -> Callable[..., Any]:
    # An explicitly empty ProxyHandler bypasses HTTP_PROXY/HTTPS_PROXY and the
    # redirect handler prevents a response from sending credentials-free device
    # traffic to an unexpected origin.
    return build_opener(ProxyHandler({}), _RejectRedirects()).open


_NO_PROXY_NO_REDIRECT_OPENER = _build_device_opener()


@dataclass(frozen=True)
class HovermapStatusData:
    scan_prefix: str
    current_scan_name: str
    free_space_bytes: int
    scan_running: bool | None
    state: str


@dataclass(frozen=True)
class ScanData:
    name: str
    size_bytes: int
    timestamp: int | float | str
    parsed_time_utc: str | None


@dataclass(frozen=True)
class ScanListData:
    scans: tuple[ScanData, ...]
    ordering: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class DownloadResult:
    scan_name: str
    path: Path
    size_bytes: int


class HovermapHttpClient:
    """Synchronous client intended for execution outside an async event loop."""

    def __init__(
        self,
        base_url: str,
        *,
        request_timeout: float = 5.0,
        download_timeout: float = 450.0,
        max_json_bytes: int = 4 * 1024 * 1024,
        max_download_bytes: int = 128 * 1024 * 1024 * 1024,
        opener: Callable[..., Any] | None = None,
        warning_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.request_timeout = _bounded_float(
            "request_timeout", request_timeout, MAX_REQUEST_TIMEOUT_SECONDS
        )
        self.download_timeout = _bounded_float(
            "download_timeout", download_timeout, MAX_DOWNLOAD_TIMEOUT_SECONDS
        )
        self.max_json_bytes = _bounded_integer(
            "max_json_bytes", max_json_bytes, MAX_JSON_BYTES_LIMIT
        )
        self.max_download_bytes = _bounded_integer(
            "max_download_bytes", max_download_bytes, MAX_DOWNLOAD_BYTES_LIMIT
        )
        self._opener = _NO_PROXY_NO_REDIRECT_OPENER if opener is None else opener
        self._warning_sink = warning_sink
        # The MCP layer performs admission before executor submission. This
        # second guard keeps the reusable synchronous client safe for direct use.
        self._download_lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._active_responses_lock = threading.Lock()
        self._active_responses: dict[int, Any] = {}

    def cancel(self) -> None:
        """Reject new work and interrupt an active response read when possible."""

        self._cancel_event.set()
        with self._active_responses_lock:
            active = tuple(self._active_responses.values())
        for response in active:
            _interrupt_response(response)

    def start_scan(self) -> None:
        self._request_control_ack(
            "/startsystem",
            query={"mission_type": "1"},
        )

    def stop_scan(self) -> None:
        self._request_control_ack("/stopsystem")

    def set_scan_prefix(self, prefix: str) -> None:
        validate_scan_prefix(prefix)
        self._request_control_ack(
            "/setprefix",
            query={"prefix": prefix},
        )

    def get_status(self) -> HovermapStatusData:
        return parse_status_response(self._request_json("/status"))

    def list_scans(self) -> ScanListData:
        data = self._request_json("/files")
        raw_scans = data.get("scans")
        if not isinstance(raw_scans, list):
            raise HovermapProtocolError("/files field 'scans' must be an array")

        parsed = [_parse_scan(item, index) for index, item in enumerate(raw_scans)]
        if not parsed:
            return ScanListData(scans=(), ordering="device_order", warnings=())

        kinds = {item.kind for item in parsed}
        warnings_seen: tuple[str, ...] = ()
        if kinds == {"numeric"}:
            parsed.sort(key=lambda item: item.sort_value, reverse=True)
            ordering = "numeric_time_desc"
        elif kinds == {"iso8601"}:
            parsed.sort(key=lambda item: item.sort_value, reverse=True)
            ordering = "iso8601_time_desc"
        else:
            ordering = "device_order"
            warnings_seen = (_DEVICE_ORDER_WARNING,)
            self._warn(_DEVICE_ORDER_WARNING)

        return ScanListData(
            scans=tuple(item.scan for item in parsed),
            ordering=ordering,
            warnings=warnings_seen,
        )

    def download_scan(
        self,
        scan_name: str,
        output_directory: Path,
        *,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> DownloadResult:
        """Stream, length-check, ZIP-check, fsync, and atomically publish a scan."""

        validate_scan_name(scan_name)
        self._raise_if_cancelled()
        if not self._download_lock.acquire(blocking=False):
            raise DownloadInProgressError("another scan download is already in progress")
        release_guard = True
        try:
            return self._download_scan_locked(scan_name, output_directory, progress)
        except _OpenStillRunningError as exc:
            release_guard = False
            _release_lock_after(self._download_lock, exc.completion, "hovermap-download-release")
            raise
        finally:
            if release_guard:
                self._download_lock.release()

    def _download_scan_locked(
        self,
        scan_name: str,
        output_directory: Path,
        progress: Callable[[int, int | None], None] | None,
    ) -> DownloadResult:
        self._raise_if_cancelled()
        with _open_download_root(output_directory) as root:
            output_name = f"{scan_name}.zip"
            part_name = f"{output_name}.part"
            output_path = root.path / output_name
            root.reject_unsafe_existing(output_name, "completed archive")
            self._raise_if_cancelled()
            root.remove_safe_stale_part(part_name)
            self._raise_if_cancelled()

            url = self._make_url("/downloadscan", {"scanname": scan_name})
            request = Request(
                url,
                headers={
                    "Accept": "application/zip, application/octet-stream",
                    "User-Agent": "kinesis-hovermap-direct/0.1",
                },
                method="GET",
            )

            self._raise_if_cancelled()
            root.assert_attached()
            deadline = time.monotonic() + self.download_timeout
            response = self._open(request, self.download_timeout)
            downloaded = 0
            declared_size: int | None = None
            opened_stat: os.stat_result | None = None
            with self._tracked_response(response) as response:
                declared_size = _parse_content_length(
                    response.headers.get("Content-Length"), context="download"
                )
                if declared_size is not None and declared_size > self.max_download_bytes:
                    raise HovermapProtocolError(
                        f"download Content-Length {declared_size} exceeds limit "
                        f"{self.max_download_bytes}"
                    )

                descriptor, opened_stat = root.open_new_part(part_name)
                with os.fdopen(descriptor, "w+b") as output:
                    while True:
                        self._raise_if_cancelled()
                        root.assert_attached()
                        chunk = _read_with_deadline(
                            response,
                            1024 * 1024,
                            deadline,
                            url,
                            cancel_check=self._raise_if_cancelled,
                        )
                        self._raise_if_cancelled()
                        root.assert_attached()
                        if not chunk:
                            break
                        if not isinstance(chunk, (bytes, bytearray)):
                            raise HovermapProtocolError(
                                "download response produced a non-byte chunk"
                            )
                        downloaded += len(chunk)
                        if downloaded > self.max_download_bytes:
                            raise HovermapProtocolError(
                                f"download exceeds limit {self.max_download_bytes}"
                            )
                        output.write(chunk)
                        if progress is not None:
                            progress(downloaded, declared_size)
                    output.flush()
                    os.fsync(output.fileno())

                    if declared_size is not None and downloaded != declared_size:
                        raise HovermapProtocolError(
                            f"download length mismatch: expected {declared_size}, got {downloaded}"
                        )
                    root.assert_entry(part_name, opened_stat)
                    self._raise_if_cancelled()
                    root.assert_attached()
                    output.seek(0)
                    _validate_zip(
                        output,
                        max_uncompressed_bytes=self.max_download_bytes,
                        max_members=MAX_ZIP_MEMBERS,
                        cancel_check=lambda: _check_download_still_safe(self, root, deadline),
                    )
                    self._raise_if_cancelled()
                    root.assert_attached()
                    root.assert_entry(part_name, opened_stat)

            if opened_stat is None:  # Defensive: response processing always opens it.
                raise HovermapProtocolError("download did not create a partial archive")
            root.publish_part(part_name, output_name, opened_stat)
            return DownloadResult(
                scan_name=scan_name,
                path=output_path,
                size_bytes=downloaded,
            )

    def _request_json(self, path: str) -> Mapping[str, Any]:
        body = self._request_bytes(path, timeout=self.request_timeout)
        try:
            decoded = body.decode("utf-8", "strict")
            value = json.loads(decoded, parse_constant=_reject_json_constant)
            _reject_json_surrogates(value)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise HovermapProtocolError(f"{path} returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise HovermapProtocolError(f"{path} JSON root must be an object")
        return value

    def _request_control_ack(
        self,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
    ) -> None:
        """Serialize controls and succeed as soon as an HTTP 2xx is observed."""

        self._raise_if_cancelled()
        if not self._control_lock.acquire(blocking=False):
            raise ControlInProgressError("a previous device-control request is still in progress")
        release_guard = True
        try:
            self._request_ack(path, query=query, timeout=self.request_timeout)
        except _OpenStillRunningError as exc:
            release_guard = False
            _release_lock_after(self._control_lock, exc.completion, "hovermap-control-release")
            raise
        finally:
            if release_guard:
                self._control_lock.release()

    def _request_ack(
        self,
        path: str,
        *,
        query: Mapping[str, str] | None,
        timeout: float,
    ) -> None:
        self._raise_if_cancelled()
        url = self._make_url(path, query)
        request = Request(
            url,
            headers={
                "Accept": "*/*",
                "User-Agent": "kinesis-hovermap-direct/0.1",
            },
            method="GET",
        )
        response = self._open(request, timeout)
        # _open_blocking has already validated an explicit 2xx status. The
        # control response body carries no correlated state acknowledgement and
        # must not turn an observed acknowledgement into an ambiguous timeout.
        _interrupt_response(response)

    def _request_bytes(
        self,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        timeout: float,
    ) -> bytes:
        self._raise_if_cancelled()
        url = self._make_url(path, query)
        request = Request(
            url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "kinesis-hovermap-direct/0.1",
            },
            method="GET",
        )
        deadline = time.monotonic() + timeout
        response = self._open(request, timeout)
        try:
            with self._tracked_response(response) as response:
                declared_size = _parse_content_length(
                    response.headers.get("Content-Length"), context=path
                )
                if declared_size is not None and declared_size > self.max_json_bytes:
                    raise HovermapProtocolError(
                        f"response from {path} exceeds limit {self.max_json_bytes}"
                    )
                chunks: list[bytes] = []
                size = 0
                while size <= self.max_json_bytes:
                    self._raise_if_cancelled()
                    chunk = _read_with_deadline(
                        response,
                        min(64 * 1024, self.max_json_bytes + 1 - size),
                        deadline,
                        url,
                        cancel_check=self._raise_if_cancelled,
                    )
                    self._raise_if_cancelled()
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                body = b"".join(chunks)
        except HovermapError:
            raise
        if not isinstance(body, bytes):
            raise HovermapProtocolError(f"response from {path} was not bytes")
        if len(body) > self.max_json_bytes:
            raise HovermapProtocolError(f"response from {path} exceeds limit {self.max_json_bytes}")
        return body

    def _raise_if_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise HovermapCancelledError("Hovermap HTTP client is shutting down")

    @contextmanager
    def _tracked_response(self, response: Any) -> Iterator[Any]:
        self._register_response(response)
        try:
            with response as opened:
                yield opened
        finally:
            with self._active_responses_lock:
                self._active_responses.pop(id(response), None)

    def _register_response(self, response: Any) -> None:
        with self._active_responses_lock:
            cancelled = self._cancel_event.is_set()
            if not cancelled:
                self._active_responses[id(response)] = response
        if cancelled:
            _interrupt_response(response)
            self._raise_if_cancelled()

    def _open(self, request: Request, timeout: float) -> Any:
        """Run urllib opening behind a daemon so shutdown never waits on headers."""

        completed = threading.Event()
        abandoned = threading.Event()
        outcome_lock = threading.Lock()
        responses: list[Any] = []
        failures: list[BaseException] = []

        def abandon_open() -> None:
            with outcome_lock:
                abandoned.set()
                ready_responses = tuple(responses)
                responses.clear()
            for response in ready_responses:
                _interrupt_response(response)

        def perform_open() -> None:
            try:
                response = self._open_blocking(request, timeout)
                with outcome_lock:
                    close_response = abandoned.is_set() or self._cancel_event.is_set()
                    if not close_response:
                        responses.append(response)
                if close_response:
                    _interrupt_response(response)
            except BaseException as exc:
                with outcome_lock:
                    failures.append(exc)
            finally:
                completed.set()

        threading.Thread(
            target=perform_open,
            name="hovermap-http-open",
            daemon=True,
        ).start()
        deadline = time.monotonic() + timeout
        while not completed.is_set():
            if self._cancel_event.is_set():
                abandon_open()
                self._raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                abandon_open()
                raise _OpenStillRunningError(
                    f"{request.method} {request.full_url} exceeded its total timeout",
                    completed,
                )
            completed.wait(min(_OPEN_POLL_SECONDS, remaining))

        if self._cancel_event.is_set():
            abandon_open()
            self._raise_if_cancelled()
        if time.monotonic() > deadline:
            abandon_open()
            raise HovermapTransportError(
                f"{request.method} {request.full_url} exceeded its total timeout"
            )
        with outcome_lock:
            failure = failures[0] if failures else None
            response = responses[0] if responses else None
        if failure is not None:
            raise failure
        if response is None:
            raise HovermapTransportError(
                f"{request.method} {request.full_url} failed without a response"
            )
        return response

    def _open_blocking(self, request: Request, timeout: float) -> Any:
        try:
            response = self._opener(request, timeout=timeout)
        except HTTPError as exc:
            try:
                status = _parse_http_status(exc.code)
            except HovermapProtocolError:
                exc.close()
                raise
            error = HovermapResponseError(request.method, request.full_url, status, str(exc.reason))
            exc.close()
            raise error from exc
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise HovermapTransportError(
                f"{request.method} {request.full_url} failed: {reason}"
            ) from exc

        status = getattr(response, "status", None)
        if status is None:
            getcode = getattr(response, "getcode", None)
            status = getcode() if callable(getcode) else None
        if status is None:
            _interrupt_response(response)
            raise HovermapProtocolError("HTTP response did not contain a status code")
        try:
            numeric_status = _parse_http_status(status)
        except HovermapProtocolError:
            _interrupt_response(response)
            raise
        if not 200 <= numeric_status <= 299:
            reason = str(getattr(response, "reason", "unexpected response"))
            _interrupt_response(response)
            raise HovermapResponseError(request.method, request.full_url, numeric_status, reason)
        return response

    def _make_url(self, path: str, query: Mapping[str, str] | None = None) -> str:
        if path not in _ALLOWED_ENDPOINTS:
            raise ValueError("API path is not in the fixed Hovermap endpoint set")
        result = f"{self.base_url}{path}"
        if query:
            result = f"{result}?{urlencode(query)}"
        return result

    def _warn(self, message: str) -> None:
        if self._warning_sink is not None:
            self._warning_sink(message)


def normalize_base_url(value: str) -> str:
    """Validate and normalize one origin-only HTTP(S) base URL."""

    if not isinstance(value, str) or not value:
        raise ValueError("base_url must be a non-empty string")
    if value != value.strip():
        raise ValueError("base_url must not contain surrounding whitespace")
    if "\\" in value or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("base_url must not contain whitespace, controls, or backslashes")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("base_url scheme must be http or https")
    if not parsed.netloc or not parsed.hostname:
        raise ValueError("base_url must contain a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain credentials")
    if "?" in value or "#" in value:
        raise ValueError("base_url must not contain a query or fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("base_url must be an origin without an API path")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base_url contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("base_url port must be between 1 and 65535")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, "", "", ""))


# Retain the inherited spelling for callers that imported this private helper.
_normalise_base_url = normalize_base_url


def validate_scan_prefix(prefix: str) -> None:
    if not isinstance(prefix, str):
        raise ValueError("scan prefix must be text")
    if SCAN_PREFIX_PATTERN.fullmatch(prefix) is None:
        raise ValueError(
            f"scan prefix must contain 1-{SCAN_PREFIX_MAX_LEN} ASCII alphanumeric characters"
        )


def validate_scan_name(scan_name: str) -> None:
    if (
        not isinstance(scan_name, str)
        or len(scan_name) > SCAN_NAME_MAX_LEN
        or SCAN_NAME_PATTERN.fullmatch(scan_name) is None
    ):
        raise ValueError(
            "scan name must match '<1-20 ASCII alphanumeric characters>_<digits>' "
            f"and be no longer than {SCAN_NAME_MAX_LEN} characters"
        )


def parse_status_response(data: Mapping[str, Any]) -> HovermapStatusData:
    try:
        scan_prefix = data["scan_name"]
        current_scan_name = data["scan_dir"]
        state = data["state"]
        free_space_value = data["freeSpace"]
    except KeyError as exc:
        raise HovermapProtocolError(f"/status missing field {exc.args[0]!r}") from exc
    if not all(isinstance(value, str) for value in (scan_prefix, current_scan_name, state)):
        raise HovermapProtocolError("/status scan_name, scan_dir, and state must be strings")
    if state in RUNNING_STATES:
        scan_running: bool | None = True
    elif state in NON_RUNNING_STATES:
        scan_running = False
    else:
        scan_running = None
    return HovermapStatusData(
        scan_prefix=scan_prefix,
        current_scan_name=current_scan_name,
        free_space_bytes=_parse_free_space(free_space_value),
        scan_running=scan_running,
        state=state,
    )


def _parse_free_space(value: Any) -> int:
    if isinstance(value, bool):
        raise HovermapProtocolError("/status freeSpace must be a byte count")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        text = value[:-1] if value.endswith("B") else value
        if not text.isascii() or not text.isdigit():
            raise HovermapProtocolError("/status freeSpace must be decimal bytes")
        significant = text.lstrip("0") or "0"
        if len(significant) > 19:
            raise HovermapProtocolError("/status freeSpace is outside int64 range")
        try:
            result = int(significant, 10)
        except ValueError as exc:
            raise HovermapProtocolError("/status freeSpace must be decimal bytes") from exc
    else:
        raise HovermapProtocolError("/status freeSpace must be a string or integer")
    if result < 0 or result > (1 << 63) - 1:
        raise HovermapProtocolError("/status freeSpace is outside int64 range")
    return result


@dataclass(frozen=True)
class _ParsedScan:
    scan: ScanData
    kind: str
    sort_value: int | float


def _parse_scan(value: Any, index: int) -> _ParsedScan:
    if not isinstance(value, dict):
        raise HovermapProtocolError(f"/files scans[{index}] must be an object")
    try:
        name = value["scan"]
        size = value["size"]
        timestamp = value["time"]
    except KeyError as exc:
        raise HovermapProtocolError(f"/files scans[{index}] missing field {exc.args[0]!r}") from exc
    try:
        validate_scan_name(name)
    except ValueError as exc:
        raise HovermapProtocolError(f"/files scans[{index}] has invalid name") from exc
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise HovermapProtocolError(f"/files scans[{index}].size must be a non-negative integer")
    if size > (1 << 63) - 1:
        raise HovermapProtocolError(f"/files scans[{index}].size exceeds int64")

    kind, sort_value, parsed_time_utc = _classify_scan_time(timestamp, index)
    return _ParsedScan(
        scan=ScanData(
            name=name,
            size_bytes=size,
            timestamp=timestamp,
            parsed_time_utc=parsed_time_utc,
        ),
        kind=kind,
        sort_value=sort_value,
    )


def _classify_scan_time(value: Any, index: int) -> tuple[str, int | float, str | None]:
    if isinstance(value, bool):
        raise HovermapProtocolError(
            f"/files scans[{index}].time must be a finite number or non-empty string"
        )
    if isinstance(value, int):
        return "numeric", value, None
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HovermapProtocolError(f"/files scans[{index}].time must be finite")
        return "numeric", value, None
    if not isinstance(value, str):
        raise HovermapProtocolError(f"/files scans[{index}].time must be a finite number or string")

    if not value.strip():
        return "unparseable", 0.0, None
    if ISO_8601_SCAN_TIME_PATTERN.fullmatch(value) is None:
        return "unparseable", 0.0, None
    text = value
    iso_text = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(iso_text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        utc_time = parsed.astimezone(timezone.utc)
        sort_value = utc_time.timestamp()
    except (ValueError, OSError, OverflowError):
        return "unparseable", 0.0, None
    if not math.isfinite(sort_value):
        return "unparseable", 0.0, None
    parsed_time_utc = utc_time.isoformat().replace("+00:00", "Z")
    return "iso8601", sort_value, parsed_time_utc


@dataclass
class _DownloadRoot(AbstractContextManager["_DownloadRoot"]):
    """Pin and operate relative to one canonical download directory.

    On POSIX, entry operations use an open directory descriptor. A rename or
    symlink swap can therefore detach the configured path, but it cannot redirect
    a write or replacement into the attacker's directory. The attachment checks
    then turn that detachment into an explicit failure.
    """

    path: Path
    identity: tuple[int, int]
    descriptor: int | None

    def __enter__(self) -> _DownloadRoot:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: object,
    ) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None

    def assert_attached(self) -> None:
        """Require the configured path to still name the pinned directory."""

        try:
            _assert_no_linklike_components(self.path, allow_missing=False)
            resolved = self.path.resolve(strict=True)
            current = self.path.lstat()
        except OSError as exc:
            raise OSError(f"configured download root was removed or replaced: {self.path}") from exc
        if resolved != self.path:
            raise OSError(
                "configured download root or an ancestor is now a symbolic link "
                f"or reparse point: {self.path}"
            )
        if not stat.S_ISDIR(current.st_mode) or _is_linklike(current):
            raise OSError(f"configured download root is no longer a directory: {self.path}")
        if _file_identity(current) != self.identity:
            raise OSError(f"configured download root was replaced: {self.path}")
        if self.descriptor is not None:
            opened = os.fstat(self.descriptor)
            if _file_identity(opened) != self.identity:
                raise OSError("open download-root descriptor changed identity")

    def reject_unsafe_existing(self, name: str, description: str) -> None:
        try:
            details = self._lstat_entry(name)
        except FileNotFoundError:
            return
        if _is_linklike(details):
            raise OSError(
                f"{description} path must not be a symbolic link or reparse point: "
                f"{self.path / name}"
            )
        if not stat.S_ISREG(details.st_mode):
            raise OSError(f"{description} path must be a regular file: {self.path / name}")

    def remove_safe_stale_part(self, name: str) -> None:
        try:
            details = self._lstat_entry(name)
        except FileNotFoundError:
            return
        if _is_linklike(details):
            raise OSError(
                "partial archive path must not be a symbolic link or reparse point: "
                f"{self.path / name}"
            )
        if not stat.S_ISREG(details.st_mode):
            raise OSError(f"partial archive path must be a regular file: {self.path / name}")
        self.assert_attached()
        if self.descriptor is not None:
            os.unlink(name, dir_fd=self.descriptor)
        else:
            (self.path / name).unlink()

    def open_new_part(self, name: str) -> tuple[int, os.stat_result]:
        self.assert_attached()
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if self.descriptor is not None:
            descriptor = os.open(name, flags, 0o600, dir_fd=self.descriptor)
        else:
            descriptor = os.open(self.path / name, flags, 0o600)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError("partial archive is not a regular file")
            self.assert_attached()
            self.assert_entry(name, opened)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor, opened

    def assert_entry(self, name: str, opened_stat: os.stat_result) -> None:
        try:
            current = self._lstat_entry(name)
        except FileNotFoundError as exc:
            raise OSError("partial archive disappeared before validation") from exc
        if not stat.S_ISREG(current.st_mode) or _is_linklike(current):
            raise OSError("partial archive was replaced before validation")
        if _file_identity(current) != _file_identity(opened_stat):
            raise OSError("partial archive was replaced before validation")

    def publish_part(
        self,
        part_name: str,
        output_name: str,
        opened_stat: os.stat_result,
    ) -> None:
        self.assert_attached()
        self.assert_entry(part_name, opened_stat)
        self.reject_unsafe_existing(output_name, "completed archive")
        self.assert_attached()
        if self.descriptor is not None:
            os.replace(
                part_name,
                output_name,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )
        else:
            os.replace(self.path / part_name, self.path / output_name)
        self.assert_attached()
        try:
            published = self._lstat_entry(output_name)
        except FileNotFoundError as exc:
            raise OSError("completed archive disappeared after publication") from exc
        if not stat.S_ISREG(published.st_mode) or _is_linklike(published):
            raise OSError("completed archive was replaced during publication")
        if _file_identity(published) != _file_identity(opened_stat):
            raise OSError("completed archive was replaced during publication")

    def _lstat_entry(self, name: str) -> os.stat_result:
        if not name or Path(name).name != name:
            raise OSError("download entry name escaped the configured directory")
        self.assert_attached()
        if self.descriptor is not None:
            return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        return (self.path / name).lstat()


def _open_download_root(output_directory: Path) -> _DownloadRoot:
    directory = Path(os.path.abspath(os.fspath(Path(output_directory).expanduser())))
    # Reject an already-swapped ancestor before mkdir() could follow it and
    # create the requested directory in an attacker-controlled location.
    _assert_no_linklike_components(directory, allow_missing=True)
    directory.mkdir(parents=True, exist_ok=True)
    _assert_no_linklike_components(directory, allow_missing=False)
    resolved = directory.resolve(strict=True)
    if resolved != directory:
        raise OSError(
            "configured download root or an ancestor is a symbolic link or "
            f"reparse point: {directory}"
        )
    details = directory.lstat()
    if not stat.S_ISDIR(details.st_mode) or _is_linklike(details):
        raise OSError(f"download destination is not a directory: {directory}")

    descriptor: int | None = None
    try:
        if os.name == "posix":
            flags = os.O_RDONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(directory, flags)
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode):
                raise OSError(f"download destination is not a directory: {directory}")
            if _file_identity(opened) != _file_identity(details):
                raise OSError("configured download root changed while it was opened")
        root = _DownloadRoot(
            path=directory,
            identity=_file_identity(details),
            descriptor=descriptor,
        )
        root.assert_attached()
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    return root


def _assert_no_linklike_components(path: Path, *, allow_missing: bool) -> None:
    parts = path.parts
    if not path.is_absolute() or not parts:
        raise OSError("download destination must resolve to an absolute local path")
    current = Path(path.anchor)
    for component in parts[1:]:
        current /= component
        try:
            details = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise
        if _is_linklike(details):
            raise OSError(
                f"configured download root must not use symbolic links or reparse points: {current}"
            )


def _is_linklike(details: os.stat_result) -> bool:
    if stat.S_ISLNK(details.st_mode):
        return True
    attributes = getattr(details, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _file_identity(details: os.stat_result) -> tuple[int, int]:
    return details.st_dev, details.st_ino


def _check_download_still_safe(
    client: HovermapHttpClient,
    root: _DownloadRoot,
    deadline: float,
) -> None:
    client._raise_if_cancelled()
    if time.monotonic() > deadline:
        raise HovermapTransportError(
            "GET /downloadscan exceeded its total timeout during ZIP validation"
        )
    root.assert_attached()


def _read_with_deadline(
    response: Any,
    maximum_bytes: int,
    deadline: float,
    context: str,
    *,
    cancel_check: Callable[[], None] | None = None,
) -> bytes:
    if cancel_check is not None:
        cancel_check()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HovermapTransportError(f"GET {context} exceeded its total timeout")
    try:
        _set_response_timeout(response, remaining)
        read_once = (
            getattr(response, "read1", None)
            if isinstance(response, http.client.HTTPResponse)
            else None
        )
        reader = read_once if callable(read_once) else response.read
        chunk = reader(maximum_bytes)
    except (TimeoutError, http.client.HTTPException, OSError) as exc:
        if cancel_check is not None:
            cancel_check()
        raise HovermapTransportError(f"GET {context} failed while reading the response") from exc
    if cancel_check is not None:
        cancel_check()
    if time.monotonic() > deadline:
        raise HovermapTransportError(f"GET {context} exceeded its total timeout")
    if not isinstance(chunk, (bytes, bytearray)):
        raise HovermapProtocolError(f"GET {context} produced a non-byte response chunk")
    return bytes(chunk)


def _release_lock_after(lock: threading.Lock, completed: threading.Event, name: str) -> None:
    """Keep admission closed until a timed-out daemon opener truly exits."""

    def release() -> None:
        completed.wait()
        lock.release()

    threading.Thread(target=release, name=name, daemon=True).start()


def _interrupt_response(response: Any) -> None:
    """Best-effort shutdown of a response's socket to wake a blocked reader."""

    pending = [response]
    visited: set[int] = set()
    socket_found = False
    while pending:
        candidate = pending.pop()
        if candidate is None or id(candidate) in visited:
            continue
        visited.add(id(candidate))
        shutdown = getattr(candidate, "shutdown", None)
        if callable(shutdown):
            socket_found = True
            with suppress(OSError, TypeError, ValueError):
                shutdown(socket.SHUT_RDWR)
        for attribute in ("fp", "raw", "_sock"):
            nested = getattr(candidate, attribute, None)
            if nested is not None:
                pending.append(nested)
    closer = getattr(response, "close", None)
    if not socket_found and callable(closer):
        with suppress(OSError, TypeError, ValueError):
            closer()


def _set_response_timeout(response: Any, timeout: float) -> None:
    pending = [response]
    visited: set[int] = set()
    while pending:
        candidate = pending.pop()
        if candidate is None or id(candidate) in visited:
            continue
        visited.add(id(candidate))
        setter = getattr(candidate, "settimeout", None)
        if callable(setter):
            setter(timeout)
            return
        for attribute in ("fp", "raw", "_sock"):
            nested = getattr(candidate, attribute, None)
            if nested is not None:
                pending.append(nested)


def _parse_http_status(value: Any) -> int:
    if isinstance(value, bool):
        raise HovermapProtocolError("HTTP response contained an invalid status code")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        result = int(value, 10)
    else:
        raise HovermapProtocolError("HTTP response contained an invalid status code")
    if not 100 <= result <= 599:
        raise HovermapProtocolError("HTTP response contained an invalid status code")
    return result


def _parse_content_length(value: str | None, *, context: str) -> int | None:
    if value is None:
        return None
    if not value.isascii() or not value.isdigit():
        raise HovermapProtocolError(f"invalid Content-Length in {context} response")
    significant = value.lstrip("0") or "0"
    if len(significant) > 19:
        raise HovermapProtocolError(f"Content-Length in {context} response is too large")
    try:
        result = int(significant, 10)
    except ValueError as exc:
        raise HovermapProtocolError(f"invalid Content-Length in {context} response") from exc
    if result < 0:
        raise HovermapProtocolError(f"negative Content-Length in {context} response")
    return result


def _validate_zip(
    source: Any,
    *,
    max_uncompressed_bytes: int,
    max_members: int,
    cancel_check: Callable[[], None] | None = None,
) -> None:
    try:
        if cancel_check is not None:
            cancel_check()
        with zipfile.ZipFile(source, "r") as archive:
            members = archive.infolist()
            if len(members) > max_members:
                raise HovermapProtocolError(
                    f"downloaded ZIP contains more than {max_members} members"
                )
            declared_uncompressed = 0
            for member in members:
                if cancel_check is not None:
                    cancel_check()
                if member.file_size < 0:
                    raise HovermapProtocolError("downloaded ZIP contains an invalid member size")
                if member.is_dir() and (member.file_size != 0 or member.CRC != 0):
                    raise HovermapProtocolError(
                        "downloaded ZIP contains a directory member with data"
                    )
                declared_uncompressed += member.file_size
                if declared_uncompressed > max_uncompressed_bytes:
                    raise HovermapProtocolError(
                        f"downloaded ZIP uncompressed size exceeds limit {max_uncompressed_bytes}"
                    )

            validated_uncompressed = 0
            for member in members:
                with archive.open(member) as member_source:
                    while True:
                        if cancel_check is not None:
                            cancel_check()
                        chunk = member_source.read(1024 * 1024)
                        if not chunk:
                            break
                        validated_uncompressed += len(chunk)
                        if validated_uncompressed > max_uncompressed_bytes:
                            raise HovermapProtocolError(
                                "downloaded ZIP uncompressed size exceeds limit "
                                f"{max_uncompressed_bytes}"
                            )
    except HovermapError:
        raise
    except zipfile.BadZipFile as exc:
        if "crc" in str(exc).lower():
            raise HovermapProtocolError("downloaded ZIP failed CRC validation") from exc
        raise HovermapProtocolError("downloaded payload is not a valid ZIP archive") from exc
    except (EOFError, RuntimeError) as exc:
        raise HovermapProtocolError("downloaded payload is not a valid ZIP archive") from exc


def _bounded_float(name: str, value: Any, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result) or result <= 0 or result > maximum:
        raise ValueError(f"{name} must be in (0, {maximum:g}]")
    return result


def _bounded_integer(name: str, value: Any, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 1 or value > maximum:
        raise ValueError(f"{name} must be in [1, {maximum}]")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


def _reject_json_surrogates(value: Any) -> None:
    pending = [value]
    while pending:
        candidate = pending.pop()
        if isinstance(candidate, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in candidate):
                raise ValueError("JSON strings must not contain Unicode surrogates")
        elif isinstance(candidate, list):
            pending.extend(candidate)
        elif isinstance(candidate, dict):
            pending.extend(candidate.keys())
            pending.extend(candidate.values())
