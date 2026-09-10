"""Hardened, ROS-independent client for the Hovermap HTTP control API."""

from dataclasses import dataclass
from datetime import datetime, timezone
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import threading
from typing import Callable, List, Mapping, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zipfile
import warnings


SCAN_PREFIX_MAX_LEN = 20
SCAN_NAME_MAX_LEN = 64
SCAN_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9]+$")
SCAN_NAME_PATTERN = re.compile(r"^[A-Za-z0-9]{1,20}_[0-9]+$")

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


class HovermapCancelledError(HovermapError):
    """A request was cancelled because the ROS node is shutting down."""


class _RejectRedirects(HTTPRedirectHandler):
    """Turn every redirect into an HTTPError before another origin is contacted."""

    def redirect_request(self, *_args, **_kwargs):
        return None


_NO_REDIRECT_OPENER = build_opener(ProxyHandler({}), _RejectRedirects()).open


@dataclass(frozen=True)
class HovermapStatusData:
    scan_prefix: str
    current_scan_name: str
    free_space: int
    scan_running: bool
    state: str


@dataclass(frozen=True)
class ScanData:
    name: str
    size: int
    timestamp: Union[int, float, str]
    normalized_time: Optional[float]


@dataclass(frozen=True)
class DownloadResult:
    scan_name: str
    path: Path
    size: int


class HovermapHttpClient:
    """Synchronous client suitable for execution in a bounded worker pool."""

    def __init__(
        self,
        base_url: str,
        *,
        request_timeout: float = 5.0,
        download_timeout: float = 450.0,
        max_json_bytes: int = 4 * 1024 * 1024,
        max_download_bytes: int = 128 * 1024 * 1024 * 1024,
        opener: Optional[Callable] = None,
        warning_sink: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.base_url = _normalise_base_url(base_url)
        if request_timeout <= 0 or download_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if max_json_bytes <= 0 or max_download_bytes <= 0:
            raise ValueError("response limits must be positive")
        self.request_timeout = float(request_timeout)
        self.download_timeout = float(download_timeout)
        self.max_json_bytes = int(max_json_bytes)
        self.max_download_bytes = int(max_download_bytes)
        self._opener = _NO_REDIRECT_OPENER if opener is None else opener
        self._warning_sink = warning_sink
        self._download_lock = threading.Lock()
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Prevent new work and ask streaming downloads to stop at a chunk boundary."""

        self._cancel_event.set()

    def start_scan(self) -> None:
        self._request_bytes(
            "/startsystem", query={"mission_type": "1"}, timeout=self.request_timeout
        )

    def stop_scan(self) -> None:
        self._request_bytes("/stopsystem", timeout=self.request_timeout)

    def set_scan_prefix(self, prefix: str) -> None:
        validate_scan_prefix(prefix)
        self._request_bytes(
            "/setprefix", query={"prefix": prefix}, timeout=self.request_timeout
        )

    def get_status(self) -> HovermapStatusData:
        data = self._request_json("/status")
        return parse_status_response(data)

    def list_scans(self) -> List[ScanData]:
        data = self._request_json("/files")
        scans = data.get("scans")
        if not isinstance(scans, list):
            raise HovermapProtocolError("/files field 'scans' must be an array")
        parsed = [_parse_scan(item, index) for index, item in enumerate(scans)]
        if all(scan.normalized_time is not None for scan in parsed):
            return sorted(parsed, key=lambda scan: scan.normalized_time, reverse=True)
        self._warn(
            "/files contains an undocumented time value; preserving stable device "
            "order instead of guessing newest-first order"
        )
        return parsed

    def download_scan(
        self,
        scan_name: str,
        output_directory: Path,
        *,
        progress: Optional[Callable[[int, Optional[int]], None]] = None,
    ) -> DownloadResult:
        """Download, length-check, ZIP-check, fsync, then atomically publish a scan."""

        validate_scan_name(scan_name)
        if not self._download_lock.acquire(blocking=False):
            raise DownloadInProgressError("another scan download is already in progress")
        try:
            return self._download_scan_locked(scan_name, output_directory, progress)
        finally:
            self._download_lock.release()

    def _download_scan_locked(
        self,
        scan_name: str,
        output_directory: Path,
        progress: Optional[Callable[[int, Optional[int]], None]],
    ) -> DownloadResult:
        directory = Path(output_directory)
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise HovermapError(f"download destination is not a directory: {directory}")

        output_path = directory / f"{scan_name}.zip"
        part_path = directory / f"{scan_name}.zip.part"
        url = self._make_url("/downloadscan", {"scanname": scan_name})
        request = Request(
            url,
            headers={
                "Accept": "application/zip, application/octet-stream",
                "User-Agent": "kinesis-hovermap-ros2/0.1",
            },
            method="GET",
        )

        try:
            self._raise_if_cancelled()
            response = self._open(request, self.download_timeout)
            with response:
                declared_size = _parse_content_length(response.headers.get("Content-Length"))
                if declared_size is not None and declared_size > self.max_download_bytes:
                    raise HovermapProtocolError(
                        f"download Content-Length {declared_size} exceeds limit "
                        f"{self.max_download_bytes}"
                    )
                downloaded = 0
                with part_path.open("wb") as output:
                    while True:
                        self._raise_if_cancelled()
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
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
        except HovermapError:
            # Keep a partial file explicitly marked .part for diagnosis/recovery.
            raise
        except (socket.timeout, TimeoutError, http.client.HTTPException) as exc:
            raise HovermapTransportError(f"GET {url} failed while reading: {exc}") from exc
        except OSError:
            # Filesystem errors retain their native type so callers can distinguish
            # destination failures from Hovermap transport failures.
            raise

        if declared_size is not None and downloaded != declared_size:
            raise HovermapProtocolError(
                f"download length mismatch: expected {declared_size}, got {downloaded}"
            )
        _validate_zip(part_path)
        os.replace(part_path, output_path)
        return DownloadResult(scan_name=scan_name, path=output_path, size=downloaded)

    def _request_json(self, path: str) -> Mapping:
        body = self._request_bytes(path, timeout=self.request_timeout)
        try:
            decoded = body.decode("utf-8", "strict")
            value = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HovermapProtocolError(f"{path} returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise HovermapProtocolError(f"{path} JSON root must be an object")
        return value

    def _request_bytes(
        self,
        path: str,
        *,
        query: Optional[Mapping[str, str]] = None,
        timeout: float,
    ) -> bytes:
        self._raise_if_cancelled()
        url = self._make_url(path, query)
        request = Request(
            url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "kinesis-hovermap-ros2/0.1",
            },
            method="GET",
        )
        response = self._open(request, timeout)
        try:
            with response:
                body = response.read(self.max_json_bytes + 1)
        except (socket.timeout, TimeoutError, http.client.HTTPException, OSError) as exc:
            raise HovermapTransportError(
                f"GET {request.full_url} failed while reading: {exc}"
            ) from exc
        if len(body) > self.max_json_bytes:
            raise HovermapProtocolError(
                f"response from {path} exceeds limit {self.max_json_bytes}"
            )
        return body

    def _raise_if_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise HovermapCancelledError("Hovermap HTTP client is shutting down")

    def _open(self, request: Request, timeout: float):
        try:
            return self._opener(request, timeout=timeout)
        except HTTPError as exc:
            error = HovermapResponseError(
                request.method, request.full_url, exc.code, str(exc.reason)
            )
            exc.close()
            raise error from exc
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise HovermapTransportError(
                f"{request.method} {request.full_url} failed: {reason}"
            ) from exc

    def _make_url(
        self, path: str, query: Optional[Mapping[str, str]] = None
    ) -> str:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("API path must be a single-host absolute path")
        result = f"{self.base_url}{path}"
        if query:
            result = f"{result}?{urlencode(query)}"
        return result

    def _warn(self, message: str) -> None:
        if self._warning_sink is not None:
            self._warning_sink(message)
        else:
            warnings.warn(message, RuntimeWarning, stacklevel=2)


def _normalise_base_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("base_url must be a non-empty string")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("base_url scheme must be http or https")
    if not parsed.hostname:
        raise ValueError("base_url must contain a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def validate_scan_prefix(prefix: str) -> None:
    if not isinstance(prefix, str):
        raise ValueError("scan prefix must be text")
    if len(prefix) > SCAN_PREFIX_MAX_LEN or SCAN_PREFIX_PATTERN.fullmatch(prefix) is None:
        raise ValueError(
            f"scan prefix must be alphanumeric and at most {SCAN_PREFIX_MAX_LEN} characters"
        )


def validate_scan_name(scan_name: str) -> None:
    if (
        not isinstance(scan_name, str)
        or len(scan_name) > SCAN_NAME_MAX_LEN
        or SCAN_NAME_PATTERN.fullmatch(scan_name) is None
    ):
        raise ValueError("scan name must match '<alphanumeric-prefix>_<digits>'")


def parse_status_response(data: Mapping) -> HovermapStatusData:
    try:
        scan_prefix = data["scan_name"]
        current_scan_name = data["scan_dir"]
        state = data["state"]
        free_space_value = data["freeSpace"]
    except KeyError as exc:
        raise HovermapProtocolError(f"/status missing field {exc.args[0]!r}") from exc
    if not all(isinstance(value, str) for value in (scan_prefix, current_scan_name, state)):
        raise HovermapProtocolError("/status scan_name, scan_dir, and state must be strings")
    free_space = _parse_free_space(free_space_value)
    return HovermapStatusData(
        scan_prefix=scan_prefix,
        current_scan_name=current_scan_name,
        free_space=free_space,
        scan_running=state not in NON_RUNNING_STATES,
        state=state,
    )


def _parse_free_space(value) -> int:
    if isinstance(value, bool):
        raise HovermapProtocolError("/status freeSpace must be a byte count")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        text = value[:-1] if value.endswith("B") else value
        if not text.isdigit():
            raise HovermapProtocolError("/status freeSpace must be decimal bytes")
        result = int(text)
    else:
        raise HovermapProtocolError("/status freeSpace must be a string or integer")
    if result < 0 or result > (1 << 63) - 1:
        raise HovermapProtocolError("/status freeSpace is outside int64 range")
    return result


def _parse_scan(value, index: int) -> ScanData:
    if not isinstance(value, dict):
        raise HovermapProtocolError(f"/files scans[{index}] must be an object")
    try:
        name = value["scan"]
        size = value["size"]
        timestamp = value["time"]
    except KeyError as exc:
        raise HovermapProtocolError(
            f"/files scans[{index}] missing field {exc.args[0]!r}"
        ) from exc
    try:
        validate_scan_name(name)
    except ValueError as exc:
        raise HovermapProtocolError(f"/files scans[{index}] has invalid name") from exc
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise HovermapProtocolError(f"/files scans[{index}].size must be non-negative int")
    if size > (1 << 63) - 1:
        raise HovermapProtocolError(f"/files scans[{index}].size exceeds int64")
    normalized_time = _normalise_scan_time(timestamp, index)
    return ScanData(
        name=name,
        size=size,
        timestamp=timestamp,
        normalized_time=normalized_time,
    )


def _normalise_scan_time(value, index: int) -> Optional[float]:
    if isinstance(value, bool):
        raise HovermapProtocolError(
            f"/files scans[{index}].time must be a finite number or non-empty string"
        )
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise HovermapProtocolError(f"/files scans[{index}].time must be finite")
        return float(value)
    if not isinstance(value, str) or not value.strip():
        raise HovermapProtocolError(
            f"/files scans[{index}].time must be a finite number or non-empty string"
        )
    text = value.strip()
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        if not math.isfinite(numeric):
            raise HovermapProtocolError(f"/files scans[{index}].time must be finite")
        return numeric
    iso_text = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return None


def _parse_content_length(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        result = int(value, 10)
    except ValueError as exc:
        raise HovermapProtocolError("invalid Content-Length in download response") from exc
    if result < 0:
        raise HovermapProtocolError("negative Content-Length in download response")
    return result


def _validate_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            corrupt_member = archive.testzip()
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise HovermapProtocolError("downloaded payload is not a valid ZIP archive") from exc
    if corrupt_member is not None:
        raise HovermapProtocolError(
            f"downloaded ZIP failed CRC validation at member {corrupt_member!r}"
        )
