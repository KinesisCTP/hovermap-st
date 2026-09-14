"""Mock HTTP coverage for the ROS-independent Hovermap client."""

from __future__ import annotations

import io
import json
import os
import threading
import time
import zipfile
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from hovermap_direct.http_client import (
    ControlInProgressError,
    DownloadInProgressError,
    HovermapCancelledError,
    HovermapHttpClient,
    HovermapProtocolError,
    HovermapResponseError,
    HovermapTransportError,
    normalize_base_url,
    parse_status_response,
    validate_scan_name,
    validate_scan_prefix,
)


def _zip_payload() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("metadata.txt", "hovermap fixture")
    return output.getvalue()


GOOD_ZIP = _zip_payload()


def _bad_crc_zip_payload() -> bytes:
    marker = b"crc fixture payload"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("payload.bin", marker)
    damaged = bytearray(output.getvalue())
    payload_offset = damaged.find(marker)
    assert payload_offset >= 0
    damaged[payload_offset] ^= 0x01
    return bytes(damaged)


BAD_CRC_ZIP = _bad_crc_zip_payload()


class StubResponse(io.BytesIO):
    def __init__(
        self,
        body: bytes,
        content_length: int | str | None = None,
        *,
        status: int | None = 200,
        reason: str = "fixture",
    ) -> None:
        super().__init__(body)
        self.headers: dict[str, str] = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.status = status
        self.reason = reason

    def __enter__(self) -> StubResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class BrokenReadResponse(StubResponse):
    def read(self, *_args: object, **_kwargs: object) -> bytes:
        raise TimeoutError("fixture timeout")


class InterruptibleSocket:
    def __init__(self) -> None:
        self.interrupted = threading.Event()
        self.shutdown_calls = 0
        self.timeout: float | None = None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def shutdown(self, _how: int) -> None:
        self.shutdown_calls += 1
        self.interrupted.set()


class BlockingReadResponse:
    def __init__(self) -> None:
        self.headers = {"Content-Length": str(len(GOOD_ZIP))}
        self.status = 200
        self.reason = "fixture"
        self.read_started = threading.Event()
        self.socket = InterruptibleSocket()
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=self.socket))
        self.closed = False

    def __enter__(self) -> BlockingReadResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def read(self, _maximum_bytes: int) -> bytes:
        self.read_started.set()
        if not self.socket.interrupted.wait(timeout=5.0):
            raise TimeoutError("fixture reader was not interrupted")
        raise OSError("fixture socket was shut down")

    def close(self) -> None:
        self.closed = True


class MockHovermapHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    requests: ClassVar[list[tuple[str, dict[str, list[str]]]]] = []
    fail_stop = False
    slow_status = False
    redirect_status_to: str | None = None
    files_scans: ClassVar[list[dict[str, Any]]] = []
    status_payload: ClassVar[Any] = {}
    status_raw_body: bytes | None = None

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        self.__class__.requests.append((parsed.path, parse_qs(parsed.query)))
        if parsed.path == "/status":
            if self.__class__.redirect_status_to is not None:
                self.send_response(302)
                self.send_header("Location", self.__class__.redirect_status_to)
                self.end_headers()
                return
            if self.__class__.slow_status:
                time.sleep(0.2)
            if self.__class__.status_raw_body is not None:
                self._bytes(self.__class__.status_raw_body, "application/json")
            else:
                self._json(self.__class__.status_payload)
        elif parsed.path == "/files":
            self._json({"scans": self.__class__.files_scans})
        elif parsed.path == "/stopsystem" and self.__class__.fail_stop:
            self.send_error(503, "busy")
        elif parsed.path in ("/startsystem", "/stopsystem", "/setprefix"):
            self._bytes(b"ok", "text/plain")
        elif parsed.path == "/downloadscan":
            name = parse_qs(parsed.query).get("scanname", [""])[0]
            if name == "CORRUPT_01":
                body = b"not a zip"
            elif name == "BADCRC_01":
                body = BAD_CRC_ZIP
            else:
                body = GOOD_ZIP
            self._bytes(body, "application/zip")
        else:
            self.send_error(404, "not found")

    def _json(self, value: Any) -> None:
        self._bytes(json.dumps(value).encode("utf-8"), "application/json")

    def _bytes(self, value: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(value)))
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            self.wfile.write(value)


@pytest.fixture(scope="module")
def mock_hovermap_url() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHovermapHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2.0)


@pytest.fixture(autouse=True)
def reset_mock_device() -> None:
    MockHovermapHandler.requests = []
    MockHovermapHandler.fail_stop = False
    MockHovermapHandler.slow_status = False
    MockHovermapHandler.redirect_status_to = None
    MockHovermapHandler.files_scans = [
        {"scan": "KINESIS_01", "size": 100, "time": 10.0},
        {"scan": "KINESIS_02", "size": 200, "time": 20.0},
    ]
    MockHovermapHandler.status_payload = {
        "scan_name": "KINESIS",
        "scan_dir": "KINESIS_12",
        "freeSpace": "295525027840B",
        "state": "Mapping",
    }
    MockHovermapHandler.status_raw_body = None


def test_status_parsing_and_endpoint(mock_hovermap_url: str) -> None:
    status = HovermapHttpClient(mock_hovermap_url).get_status()
    assert status.scan_prefix == "KINESIS"
    assert status.current_scan_name == "KINESIS_12"
    assert status.free_space_bytes == 295_525_027_840
    assert status.scan_running is True
    assert status.state == "Mapping"
    assert MockHovermapHandler.requests == [("/status", {})]


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("Mapping", True),
        ("Stopped", False),
        ("Disabled", False),
        ("Booting", False),
        ("USB Error", False),
        ("USB Mounted", False),
        ("Transferring Data", False),
        ("Processes Stopped", False),
        ("Future Firmware State", None),
        ("mapping", None),
    ],
)
def test_status_running_derivation_is_conservative(state: str, expected: bool | None) -> None:
    status = parse_status_response(
        {
            "scan_name": "KINESIS",
            "scan_dir": "KINESIS_1",
            "freeSpace": 42,
            "state": state,
        }
    )
    assert status.scan_running is expected
    assert status.state == state


@pytest.mark.parametrize("free_space", ["42B", "42", 42])
def test_status_accepts_documented_free_space_shapes(free_space: object) -> None:
    status = parse_status_response(
        {
            "scan_name": "K",
            "scan_dir": "K_1",
            "freeSpace": free_space,
            "state": "Stopped",
        }
    )
    assert status.free_space_bytes == 42


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"scan_name": 1, "scan_dir": "K_1", "freeSpace": 1, "state": "Stopped"},
        {"scan_name": "K", "scan_dir": "K_1", "freeSpace": True, "state": "Stopped"},
        {"scan_name": "K", "scan_dir": "K_1", "freeSpace": "1KB", "state": "Stopped"},
        {"scan_name": "K", "scan_dir": "K_1", "freeSpace": -1, "state": "Stopped"},
    ],
)
def test_malformed_status_is_protocol_error(payload: dict[str, object]) -> None:
    with pytest.raises(HovermapProtocolError):
        parse_status_response(payload)


def test_numeric_scan_listing_is_descending_without_epoch_claim(
    mock_hovermap_url: str,
) -> None:
    result = HovermapHttpClient(mock_hovermap_url).list_scans()
    assert result.ordering == "numeric_time_desc"
    assert result.warnings == ()
    assert [scan.name for scan in result.scans] == ["KINESIS_02", "KINESIS_01"]
    assert [scan.size_bytes for scan in result.scans] == [200, 100]
    assert [scan.timestamp for scan in result.scans] == [20.0, 10.0]
    assert all(scan.parsed_time_utc is None for scan in result.scans)


def test_arbitrarily_large_json_integers_remain_sortable_numeric_times(
    mock_hovermap_url: str,
) -> None:
    MockHovermapHandler.files_scans = [
        {"scan": "KINESIS_01", "size": 1, "time": 10**400},
        {"scan": "KINESIS_02", "size": 2, "time": 10**401},
    ]
    result = HovermapHttpClient(mock_hovermap_url).list_scans()
    assert result.ordering == "numeric_time_desc"
    assert [scan.name for scan in result.scans] == ["KINESIS_02", "KINESIS_01"]


def test_iso8601_scan_listing_is_descending_and_normalized(
    mock_hovermap_url: str,
) -> None:
    MockHovermapHandler.files_scans = [
        {"scan": "KINESIS_01", "size": 1, "time": "2024-01-01T00:00:00Z"},
        {
            "scan": "KINESIS_02",
            "size": 2,
            "time": "2024-01-03T03:00:00+03:00",
        },
        {"scan": "KINESIS_03", "size": 3, "time": "2024-01-02T00:00:00"},
    ]
    result = HovermapHttpClient(mock_hovermap_url).list_scans()
    assert result.ordering == "iso8601_time_desc"
    assert [scan.name for scan in result.scans] == [
        "KINESIS_02",
        "KINESIS_03",
        "KINESIS_01",
    ]
    assert result.scans[0].timestamp == "2024-01-03T03:00:00+03:00"
    assert result.scans[0].parsed_time_utc == "2024-01-03T00:00:00Z"


@pytest.mark.parametrize(
    "times",
    [
        [10, "2024-01-02T00:00:00Z"],
        [10, "firmware-local-value"],
        ["1704067200", "1704153600"],
        ["20240101T000000", "20240102T000000"],
        ["2024-W01-1T00:00:00", "2024-W01-2T00:00:00"],
        [" 2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z "],
        ["", " "],
    ],
)
def test_mixed_or_unparseable_times_preserve_device_order_and_warn(
    mock_hovermap_url: str, times: list[int | str]
) -> None:
    MockHovermapHandler.files_scans = [
        {"scan": "KINESIS_01", "size": 1, "time": times[0]},
        {"scan": "KINESIS_02", "size": 2, "time": times[1]},
    ]
    warnings_seen: list[str] = []
    result = HovermapHttpClient(mock_hovermap_url, warning_sink=warnings_seen.append).list_scans()
    assert result.ordering == "device_order"
    assert [scan.name for scan in result.scans] == ["KINESIS_01", "KINESIS_02"]
    assert len(result.warnings) == 1
    assert warnings_seen == list(result.warnings)


def test_empty_scan_listing_uses_device_order_without_warning(
    mock_hovermap_url: str,
) -> None:
    MockHovermapHandler.files_scans = []
    result = HovermapHttpClient(mock_hovermap_url).list_scans()
    assert result.scans == ()
    assert result.ordering == "device_order"
    assert result.warnings == ()


@pytest.mark.parametrize(
    "scan",
    [
        {"scan": "KINESIS_01", "size": 1, "time": {"bad": "shape"}},
        {"scan": "KINESIS_01", "size": -1, "time": 1},
        {"scan": "../KINESIS_01", "size": 1, "time": 1},
        {"scan": "KINESIS_01", "size": 1, "time": float("inf")},
        {"scan": "KINESIS_01", "size": 1},
    ],
)
def test_structurally_invalid_scan_is_rejected(
    mock_hovermap_url: str, scan: dict[str, object]
) -> None:
    MockHovermapHandler.files_scans = [scan]
    with pytest.raises(HovermapProtocolError):
        HovermapHttpClient(mock_hovermap_url).list_scans()


def test_control_endpoints_are_fixed_gets_with_exact_queries(
    mock_hovermap_url: str,
) -> None:
    client = HovermapHttpClient(mock_hovermap_url)
    client.start_scan()
    client.stop_scan()
    client.set_scan_prefix("KINESIS2")
    assert MockHovermapHandler.requests == [
        ("/startsystem", {"mission_type": ["1"]}),
        ("/stopsystem", {}),
        ("/setprefix", {"prefix": ["KINESIS2"]}),
    ]
    with pytest.raises(ValueError, match="fixed Hovermap endpoint"):
        client._make_url("/arbitrary")


def test_http_failure_is_typed(mock_hovermap_url: str) -> None:
    MockHovermapHandler.fail_stop = True
    with pytest.raises(HovermapResponseError) as caught:
        HovermapHttpClient(mock_hovermap_url).stop_scan()
    assert caught.value.status == 503


def test_custom_opener_non_2xx_is_also_rejected() -> None:
    response = StubResponse(b"busy", 4, status=409, reason="conflict")
    client = HovermapHttpClient("http://hovermap", opener=lambda _request, timeout: response)
    with pytest.raises(HovermapResponseError) as caught:
        client.stop_scan()
    assert caught.value.status == 409
    assert response.closed


def test_response_without_status_cannot_be_claimed_as_http_2xx() -> None:
    response = StubResponse(b"{}", 2, status=None)
    client = HovermapHttpClient("http://hovermap", opener=lambda _request, timeout: response)
    with pytest.raises(HovermapProtocolError, match="status code"):
        client.stop_scan()
    assert response.closed


def test_control_acknowledgement_does_not_read_an_irrelevant_body() -> None:
    class StalledBodyResponse(StubResponse):
        def read(self, *_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("control response body must not be read")

    response = StalledBodyResponse(b"body never completes", None)
    client = HovermapHttpClient("http://hovermap", opener=lambda _request, timeout: response)

    client.start_scan()

    assert response.closed


def test_redirect_is_rejected_before_second_origin(mock_hovermap_url: str) -> None:
    class RedirectSink(BaseHTTPRequestHandler):
        requests: ClassVar[int] = 0

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_GET(self) -> None:
            self.__class__.requests += 1
            self.send_response(200)
            self.end_headers()

    sink = ThreadingHTTPServer(("127.0.0.1", 0), RedirectSink)
    sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
    sink_thread.start()
    try:
        host, port = sink.server_address
        MockHovermapHandler.redirect_status_to = f"http://{host}:{port}/capture"
        with pytest.raises(HovermapResponseError) as caught:
            HovermapHttpClient(mock_hovermap_url).get_status()
        assert caught.value.status == 302
        assert RedirectSink.requests == 0
    finally:
        MockHovermapHandler.redirect_status_to = None
        sink.shutdown()
        sink.server_close()
        sink_thread.join(timeout=2.0)


def test_proxy_environment_is_ignored(mock_hovermap_url: str) -> None:
    proxy_environment = {
        "HTTP_PROXY": "http://127.0.0.1:1",
        "HTTPS_PROXY": "http://127.0.0.1:1",
        "NO_PROXY": "",
    }
    with patch.dict(os.environ, proxy_environment, clear=False):
        status = HovermapHttpClient(mock_hovermap_url).get_status()
    assert status.current_scan_name == "KINESIS_12"


def test_timeout_is_typed(mock_hovermap_url: str) -> None:
    MockHovermapHandler.slow_status = True
    with pytest.raises(HovermapTransportError):
        HovermapHttpClient(mock_hovermap_url, request_timeout=0.02).get_status()


def test_read_timeout_from_custom_response_is_typed() -> None:
    client = HovermapHttpClient(
        "http://hovermap",
        opener=lambda _request, timeout: BrokenReadResponse(b"", None),
    )
    with pytest.raises(HovermapTransportError):
        client.get_status()


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"state": NaN}', b'"not an object"'],
)
def test_malformed_json_is_rejected(body: bytes) -> None:
    client = HovermapHttpClient(
        "http://hovermap",
        opener=lambda _request, timeout: StubResponse(body, len(body)),
    )
    with pytest.raises(HovermapProtocolError):
        client.get_status()


def test_escaped_lone_surrogate_is_rejected_before_mcp_serialization() -> None:
    body = b'{"scan_name":"\\ud800","scan_dir":"KINESIS_1","freeSpace":1,"state":"Stopped"}'
    client = HovermapHttpClient(
        "http://hovermap",
        opener=lambda _request, timeout: StubResponse(body, len(body)),
    )

    with pytest.raises(HovermapProtocolError, match="invalid JSON"):
        client.get_status()


def test_oversized_json_is_rejected_from_header_and_stream() -> None:
    by_header = HovermapHttpClient(
        "http://hovermap",
        max_json_bytes=4,
        opener=lambda _request, timeout: StubResponse(b"{}", 5),
    )
    with pytest.raises(HovermapProtocolError, match="exceeds limit"):
        by_header.get_status()

    by_stream = HovermapHttpClient(
        "http://hovermap",
        max_json_bytes=4,
        opener=lambda _request, timeout: StubResponse(b"12345", None),
    )
    with pytest.raises(HovermapProtocolError, match="exceeds limit"):
        by_stream.get_status()


def test_invalid_content_length_is_protocol_error() -> None:
    client = HovermapHttpClient(
        "http://hovermap",
        opener=lambda _request, timeout: StubResponse(b"{}", "nonsense"),
    )
    with pytest.raises(HovermapProtocolError, match="Content-Length"):
        client.get_status()


def test_cancel_prevents_new_network_work(mock_hovermap_url: str) -> None:
    client = HovermapHttpClient(mock_hovermap_url)
    client.cancel()
    with pytest.raises(HovermapCancelledError):
        client.get_status()
    assert MockHovermapHandler.requests == []


def test_cancel_interrupts_opener_stalled_before_response_headers() -> None:
    started = threading.Event()
    release = threading.Event()
    response = StubResponse(b"{}", 2)

    def blocking_opener(_request: object, *, timeout: float) -> StubResponse:
        started.set()
        release.wait(timeout=5.0)
        return response

    client = HovermapHttpClient(
        "http://hovermap",
        request_timeout=10,
        opener=blocking_opener,
    )
    failures: list[BaseException] = []

    def get_status() -> None:
        try:
            client.get_status()
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=get_status)
    worker.start()
    assert started.wait(timeout=2.0)
    began_cancel = time.monotonic()
    client.cancel()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert time.monotonic() - began_cancel < 1.0
    assert len(failures) == 1
    assert isinstance(failures[0], HovermapCancelledError)

    release.set()
    for _ in range(100):
        if response.closed:
            break
        time.sleep(0.01)
    assert response.closed


def test_total_timeout_stops_opener_that_ignores_its_timeout() -> None:
    started = threading.Event()
    release = threading.Event()
    response = StubResponse(b"{}", 2)

    def blocking_opener(_request: object, *, timeout: float) -> StubResponse:
        started.set()
        release.wait(timeout=5.0)
        return response

    client = HovermapHttpClient(
        "http://hovermap",
        request_timeout=0.05,
        opener=blocking_opener,
    )
    began_request = time.monotonic()
    try:
        with pytest.raises(HovermapTransportError, match="total timeout"):
            client.get_status()
    finally:
        release.set()
    assert started.is_set()
    assert time.monotonic() - began_request < 1.0

    for _ in range(100):
        if response.closed:
            break
        time.sleep(0.01)
    assert response.closed


def test_timed_out_control_keeps_mutation_admission_until_opener_exits() -> None:
    started = threading.Event()
    release = threading.Event()
    response = StubResponse(b"", 0)
    opener_calls = 0

    def blocking_opener(_request: object, *, timeout: float) -> StubResponse:
        nonlocal opener_calls
        opener_calls += 1
        started.set()
        release.wait(timeout=5.0)
        return response

    client = HovermapHttpClient(
        "http://hovermap",
        request_timeout=0.05,
        opener=blocking_opener,
    )
    with pytest.raises(HovermapTransportError, match="total timeout"):
        client.start_scan()
    assert started.is_set()

    with pytest.raises(ControlInProgressError):
        client.stop_scan()
    assert opener_calls == 1

    release.set()
    for _ in range(100):
        if response.closed and not client._control_lock.locked():
            break
        time.sleep(0.01)
    assert response.closed
    assert not client._control_lock.locked()


def test_timed_out_download_stays_admitted_until_opener_exits(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    response = StubResponse(GOOD_ZIP, len(GOOD_ZIP))
    opener_calls = 0

    def blocking_opener(_request: object, *, timeout: float) -> StubResponse:
        nonlocal opener_calls
        opener_calls += 1
        started.set()
        release.wait(timeout=5.0)
        return response

    client = HovermapHttpClient(
        "http://hovermap",
        download_timeout=0.05,
        opener=blocking_opener,
    )
    with pytest.raises(HovermapTransportError, match="total timeout"):
        client.download_scan("KINESIS_01", tmp_path)
    assert started.is_set()

    with pytest.raises(DownloadInProgressError):
        client.download_scan("KINESIS_02", tmp_path)
    assert opener_calls == 1

    release.set()
    for _ in range(100):
        if response.closed and not client._download_lock.locked():
            break
        time.sleep(0.01)
    assert response.closed
    assert not client._download_lock.locked()


def test_cancel_interrupts_blocked_download_read_promptly(tmp_path: Path) -> None:
    response = BlockingReadResponse()
    client = HovermapHttpClient(
        "http://hovermap",
        download_timeout=10,
        opener=lambda _request, timeout: response,
    )
    failures: list[BaseException] = []

    def download() -> None:
        try:
            client.download_scan("KINESIS_02", tmp_path)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=download)
    worker.start()
    assert response.read_started.wait(timeout=2.0)

    started = time.monotonic()
    client.cancel()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert time.monotonic() - started < 1.0
    assert len(failures) == 1
    assert isinstance(failures[0], HovermapCancelledError)
    assert response.socket.shutdown_calls >= 1
    assert response.closed
    assert not (tmp_path / "KINESIS_02.zip").exists()
    assert (tmp_path / "KINESIS_02.zip.part").read_bytes() == b""


def test_download_is_zip_validated_and_atomically_replaces_completed_file(
    mock_hovermap_url: str, tmp_path: Path
) -> None:
    final_path = tmp_path / "KINESIS_02.zip"
    part_path = tmp_path / "KINESIS_02.zip.part"
    final_path.write_bytes(b"old completed scan")
    part_path.write_bytes(b"stale partial scan")
    progress: list[tuple[int, int | None]] = []

    result = HovermapHttpClient(mock_hovermap_url).download_scan(
        "KINESIS_02", tmp_path, progress=lambda done, total: progress.append((done, total))
    )

    assert result.scan_name == "KINESIS_02"
    assert result.path == final_path.resolve()
    assert result.size_bytes == len(GOOD_ZIP)
    assert final_path.read_bytes() == GOOD_ZIP
    assert not part_path.exists()
    assert progress[-1] == (len(GOOD_ZIP), len(GOOD_ZIP))
    with zipfile.ZipFile(final_path) as archive:
        assert archive.read("metadata.txt") == b"hovermap fixture"


def test_corrupt_download_preserves_completed_file_and_part(
    mock_hovermap_url: str, tmp_path: Path
) -> None:
    final_path = tmp_path / "CORRUPT_01.zip"
    final_path.write_bytes(b"known good previous file")
    with pytest.raises(HovermapProtocolError, match="valid ZIP"):
        HovermapHttpClient(mock_hovermap_url).download_scan("CORRUPT_01", tmp_path)
    assert final_path.read_bytes() == b"known good previous file"
    assert (tmp_path / "CORRUPT_01.zip.part").read_bytes() == b"not a zip"


def test_structurally_valid_zip_with_bad_crc_is_rejected(
    mock_hovermap_url: str, tmp_path: Path
) -> None:
    final_path = tmp_path / "BADCRC_01.zip"
    final_path.write_bytes(b"known good previous file")

    with pytest.raises(HovermapProtocolError, match="CRC"):
        HovermapHttpClient(mock_hovermap_url).download_scan("BADCRC_01", tmp_path)

    assert final_path.read_bytes() == b"known good previous file"
    assert (tmp_path / "BADCRC_01.zip.part").read_bytes() == BAD_CRC_ZIP


def test_zip_uncompressed_size_is_bounded_by_download_limit(tmp_path: Path) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("compressible.bin", b"A" * (1024 * 1024))
    compressed_zip = output.getvalue()
    assert len(compressed_zip) < 1024 * 1024
    client = HovermapHttpClient(
        "http://hovermap",
        max_download_bytes=len(compressed_zip),
        opener=lambda _request, timeout: StubResponse(compressed_zip, len(compressed_zip)),
    )

    with pytest.raises(HovermapProtocolError, match="uncompressed size exceeds"):
        client.download_scan("KINESIS_03", tmp_path)

    assert not (tmp_path / "KINESIS_03.zip").exists()
    assert (tmp_path / "KINESIS_03.zip.part").read_bytes() == compressed_zip


def test_zip_member_count_is_bounded(tmp_path: Path) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("one", b"1")
        archive.writestr("two", b"2")
    payload = output.getvalue()
    client = HovermapHttpClient(
        "http://hovermap",
        opener=lambda _request, timeout: StubResponse(payload, len(payload)),
    )

    with (
        patch("hovermap_direct.http_client.MAX_ZIP_MEMBERS", 1),
        pytest.raises(HovermapProtocolError, match="more than 1 members"),
    ):
        client.download_scan("KINESIS_04", tmp_path)

    assert not (tmp_path / "KINESIS_04.zip").exists()
    assert (tmp_path / "KINESIS_04.zip.part").read_bytes() == payload


def test_zip_directory_entry_with_data_is_not_skipped(tmp_path: Path) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("unexpected-directory/", b"hidden data")
    payload = output.getvalue()
    client = HovermapHttpClient(
        "http://hovermap",
        opener=lambda _request, timeout: StubResponse(payload, len(payload)),
    )

    with pytest.raises(HovermapProtocolError, match="directory member with data"):
        client.download_scan("KINESIS_05", tmp_path)

    assert not (tmp_path / "KINESIS_05.zip").exists()
    assert (tmp_path / "KINESIS_05.zip.part").read_bytes() == payload


def test_zip_validation_obeys_total_download_deadline(tmp_path: Path) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("slow.bin", b"A" * (2 * 1024 * 1024))
    payload = output.getvalue()
    client = HovermapHttpClient(
        "http://hovermap",
        download_timeout=0.05,
        opener=lambda _request, timeout: StubResponse(payload, len(payload)),
    )
    original_read = zipfile.ZipExtFile.read

    def delayed_read(member: zipfile.ZipExtFile, size: int = -1) -> bytes:
        chunk = original_read(member, size)
        time.sleep(0.06)
        return chunk

    with (
        patch.object(zipfile.ZipExtFile, "read", delayed_read),
        pytest.raises(HovermapTransportError, match="ZIP validation"),
    ):
        client.download_scan("KINESIS_06", tmp_path)

    assert not (tmp_path / "KINESIS_06.zip").exists()
    assert (tmp_path / "KINESIS_06.zip.part").read_bytes() == payload


def test_content_length_mismatch_keeps_part_file(tmp_path: Path) -> None:
    client = HovermapHttpClient(
        "http://hovermap",
        opener=lambda _request, timeout: StubResponse(GOOD_ZIP, len(GOOD_ZIP) + 1),
    )
    with pytest.raises(HovermapProtocolError, match="length mismatch"):
        client.download_scan("KINESIS_02", tmp_path)
    assert not (tmp_path / "KINESIS_02.zip").exists()
    assert (tmp_path / "KINESIS_02.zip.part").read_bytes() == GOOD_ZIP


def test_download_limits_apply_to_declared_and_streamed_size(tmp_path: Path) -> None:
    by_header = HovermapHttpClient(
        "http://hovermap",
        max_download_bytes=1,
        opener=lambda _request, timeout: StubResponse(GOOD_ZIP, len(GOOD_ZIP)),
    )
    with pytest.raises(HovermapProtocolError, match="Content-Length"):
        by_header.download_scan("KINESIS_01", tmp_path)
    assert not (tmp_path / "KINESIS_01.zip.part").exists()

    by_stream = HovermapHttpClient(
        "http://hovermap",
        max_download_bytes=1,
        opener=lambda _request, timeout: StubResponse(GOOD_ZIP, None),
    )
    with pytest.raises(HovermapProtocolError, match="exceeds limit"):
        by_stream.download_scan("KINESIS_02", tmp_path)
    assert (tmp_path / "KINESIS_02.zip.part").exists()


def test_concurrent_download_is_rejected_before_http_request(
    mock_hovermap_url: str, tmp_path: Path
) -> None:
    client = HovermapHttpClient(mock_hovermap_url)
    client._download_lock.acquire()
    try:
        with pytest.raises(DownloadInProgressError):
            client.download_scan("KINESIS_02", tmp_path)
    finally:
        client._download_lock.release()
    assert MockHovermapHandler.requests == []


@pytest.mark.parametrize("filename", ["KINESIS_01.zip", "KINESIS_01.zip.part"])
def test_download_rejects_existing_symlink_before_network(
    mock_hovermap_url: str, tmp_path: Path, filename: str
) -> None:
    target = tmp_path / "outside"
    target.write_bytes(b"must remain untouched")
    link = tmp_path / filename
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(OSError, match="symbolic link"):
        HovermapHttpClient(mock_hovermap_url).download_scan("KINESIS_01", tmp_path)
    assert target.read_bytes() == b"must remain untouched"
    assert MockHovermapHandler.requests == []


def test_download_rejects_configured_root_replaced_by_symlink_before_network(
    mock_hovermap_url: str, tmp_path: Path
) -> None:
    configured_root = tmp_path / "downloads"
    configured_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    configured_root.rmdir()
    try:
        configured_root.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        configured_root.mkdir()
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(OSError, match="symbolic links or reparse points"):
        HovermapHttpClient(mock_hovermap_url).download_scan("KINESIS_01", configured_root)

    assert list(outside.iterdir()) == []
    assert MockHovermapHandler.requests == []


def test_download_rejects_configured_ancestor_replaced_by_symlink_before_network(
    mock_hovermap_url: str, tmp_path: Path
) -> None:
    configured_parent = tmp_path / "configured-parent"
    configured_root = configured_parent / "downloads"
    configured_root.mkdir(parents=True)
    parked_parent = tmp_path / "parked-parent"
    outside_parent = tmp_path / "outside-parent"
    (outside_parent / "downloads").mkdir(parents=True)
    configured_parent.rename(parked_parent)
    try:
        configured_parent.symlink_to(outside_parent, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        parked_parent.rename(configured_parent)
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(OSError, match="symbolic links or reparse points"):
        HovermapHttpClient(mock_hovermap_url).download_scan("KINESIS_01", configured_root)

    assert list((outside_parent / "downloads").iterdir()) == []
    assert MockHovermapHandler.requests == []


def test_download_root_swap_during_read_never_writes_through_symlink(
    tmp_path: Path,
) -> None:
    probe_target = tmp_path / "probe-target"
    probe_target.mkdir()
    probe_link = tmp_path / "probe-link"
    try:
        probe_link.symlink_to(probe_target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    probe_link.unlink()

    configured_root = tmp_path / "downloads"
    configured_root.mkdir()
    parked_root = tmp_path / "parked-downloads"
    outside = tmp_path / "outside"
    outside.mkdir()

    class SwapRootOnReadResponse(StubResponse):
        swapped = False

        def read(self, maximum_bytes: int = -1) -> bytes:
            if not self.swapped:
                self.swapped = True
                configured_root.rename(parked_root)
                configured_root.symlink_to(outside, target_is_directory=True)
            return super().read(maximum_bytes)

    response = SwapRootOnReadResponse(GOOD_ZIP, len(GOOD_ZIP))
    client = HovermapHttpClient(
        "http://hovermap",
        opener=lambda _request, timeout: response,
    )

    with pytest.raises(OSError, match="download root"):
        client.download_scan("KINESIS_01", configured_root)

    assert not (outside / "KINESIS_01.zip").exists()
    assert not (outside / "KINESIS_01.zip.part").exists()
    assert not (parked_root / "KINESIS_01.zip").exists()
    assert (parked_root / "KINESIS_01.zip.part").read_bytes() == b""


@pytest.mark.parametrize("valid", ["A", "KINESIS2026", "abc123", "Z" * 20])
def test_prefix_validation_accepts_only_ascii_alphanumeric(valid: str) -> None:
    validate_scan_prefix(valid)


@pytest.mark.parametrize(
    "invalid",
    ["", "has space", "has_underscore", "x" * 21, "../escape", "café", None],
)
def test_prefix_validation_rejects_bad_values(invalid: object) -> None:
    with pytest.raises(ValueError):
        validate_scan_prefix(invalid)  # type: ignore[arg-type]


def test_scan_name_validation_and_total_length_limit() -> None:
    validate_scan_name("KINESIS_01")
    validate_scan_name(f"{'X' * 20}_{'1' * 43}")
    for invalid in (
        "KINESIS",
        "../KINESIS_01",
        "KINESIS_1.zip",
        "A_B_1",
        f"{'X' * 21}_01",
        f"{'X' * 20}_{'1' * 44}",
        "café_01",
    ):
        with pytest.raises(ValueError):
            validate_scan_name(invalid)


@pytest.mark.parametrize(
    "invalid",
    [
        "ftp://hovermap",
        "http://user:secret@hovermap",
        "hovermap",
        "http://hovermap/api",
        "http://hovermap?",
        "http://hovermap/?query=1",
        "http://hovermap#",
        "http://hovermap/#fragment",
        " http://hovermap",
        "http://good.example\tevil",
        "http://good.example\\evil",
        "http://hovermap:0",
        "http://hovermap:bad",
        "http://hovermap\x7f",
        "",
    ],
)
def test_base_url_rejects_non_origin_forms(invalid: str) -> None:
    with pytest.raises(ValueError):
        HovermapHttpClient(invalid)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("http://hovermap", "http://hovermap"),
        ("http://hovermap/", "http://hovermap"),
        ("HTTPS://hovermap:443/", "https://hovermap:443"),
        ("http://[::1]:8080", "http://[::1]:8080"),
    ],
)
def test_base_url_normalizes_valid_origins(given: str, expected: str) -> None:
    assert normalize_base_url(given) == expected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"request_timeout": 0},
        {"request_timeout": 301},
        {"download_timeout": float("nan")},
        {"download_timeout": 3601},
        {"max_json_bytes": 0},
        {"max_json_bytes": 64 * 1024 * 1024 + 1},
        {"max_download_bytes": 0},
        {"max_download_bytes": 1024**4 + 1},
    ],
)
def test_constructor_enforces_response_and_timeout_bounds(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        HovermapHttpClient("http://hovermap", **kwargs)  # type: ignore[arg-type]
