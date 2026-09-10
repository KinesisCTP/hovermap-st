"""Mock HTTP endpoint tests for control, status, listing, and downloads."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile

from hovermap_ros2_api.http_client import (
    DownloadInProgressError,
    HovermapHttpClient,
    HovermapProtocolError,
    HovermapResponseError,
    HovermapTransportError,
    validate_scan_name,
    validate_scan_prefix,
)


def _zip_payload() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("metadata.txt", "hovermap fixture")
    return output.getvalue()


GOOD_ZIP = _zip_payload()


class StubResponse(io.BytesIO):
    def __init__(self, body: bytes, content_length: int):
        super().__init__(body)
        self.headers = {"Content-Length": str(content_length)}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class MockHovermapHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    requests = []
    fail_stop = False
    slow_status = False
    redirect_status_to = None
    files_scans = []

    def log_message(self, _format, *_args):
        return

    def do_GET(self):
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
            self._json(
                {
                    "scan_name": "KINESIS",
                    "scan_dir": "KINESIS_12",
                    "freeSpace": "295525027840B",
                    "state": "Mapping",
                }
            )
        elif parsed.path == "/files":
            self._json({"scans": self.__class__.files_scans})
        elif parsed.path == "/stopsystem" and self.__class__.fail_stop:
            self.send_error(503, "busy")
        elif parsed.path in ("/startsystem", "/stopsystem", "/setprefix"):
            self._bytes(b"ok", "text/plain")
        elif parsed.path == "/downloadscan":
            name = parse_qs(parsed.query).get("scanname", [""])[0]
            body = b"not a zip" if name == "CORRUPT_01" else GOOD_ZIP
            self._bytes(body, "application/zip")
        else:
            self.send_error(404, "not found")

    def _json(self, value):
        self._bytes(json.dumps(value).encode("utf-8"), "application/json")

    def _bytes(self, value: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(value)))
        self.end_headers()
        try:
            self.wfile.write(value)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass


class HttpClientEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockHovermapHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2.0)

    def setUp(self):
        MockHovermapHandler.requests = []
        MockHovermapHandler.fail_stop = False
        MockHovermapHandler.slow_status = False
        MockHovermapHandler.redirect_status_to = None
        MockHovermapHandler.files_scans = [
            {"scan": "KINESIS_01", "size": 100, "time": 10.0},
            {"scan": "KINESIS_02", "size": 200, "time": 20.0},
        ]
        self.client = HovermapHttpClient(self.base_url)

    def test_status_parsing(self):
        status = self.client.get_status()
        self.assertEqual(status.scan_prefix, "KINESIS")
        self.assertEqual(status.current_scan_name, "KINESIS_12")
        self.assertEqual(status.free_space, 295525027840)
        self.assertTrue(status.scan_running)

    def test_scan_listing_is_newest_first(self):
        scans = self.client.list_scans()
        self.assertEqual([scan.name for scan in scans], ["KINESIS_02", "KINESIS_01"])
        self.assertEqual([scan.size for scan in scans], [200, 100])

    def test_scan_time_normalizes_numbers_numeric_strings_and_iso8601(self):
        MockHovermapHandler.files_scans = [
            {"scan": "KINESIS_01", "size": 1, "time": 1704067200},
            {"scan": "KINESIS_02", "size": 2, "time": "1704153600"},
            {
                "scan": "KINESIS_03",
                "size": 3,
                "time": "2024-01-03T00:00:00Z",
            },
        ]
        scans = self.client.list_scans()
        self.assertEqual(
            [scan.name for scan in scans],
            ["KINESIS_03", "KINESIS_02", "KINESIS_01"],
        )

    def test_unknown_safe_scan_time_warns_and_preserves_device_order(self):
        warnings_seen = []
        MockHovermapHandler.files_scans = [
            {"scan": "KINESIS_01", "size": 1, "time": "firmware-local-value"},
            {"scan": "KINESIS_02", "size": 2, "time": 20},
        ]
        client = HovermapHttpClient(self.base_url, warning_sink=warnings_seen.append)
        scans = client.list_scans()
        self.assertEqual([scan.name for scan in scans], ["KINESIS_01", "KINESIS_02"])
        self.assertEqual(len(warnings_seen), 1)

    def test_structurally_unsafe_scan_time_is_rejected(self):
        MockHovermapHandler.files_scans = [
            {"scan": "KINESIS_01", "size": 1, "time": {"unexpected": "object"}}
        ]
        with self.assertRaises(HovermapProtocolError):
            self.client.list_scans()

    def test_control_endpoints_and_url_encoding(self):
        self.client.start_scan()
        self.client.stop_scan()
        self.client.set_scan_prefix("KINESIS2")
        self.assertIn(("/startsystem", {"mission_type": ["1"]}), MockHovermapHandler.requests)
        self.assertIn(("/stopsystem", {}), MockHovermapHandler.requests)
        self.assertIn(("/setprefix", {"prefix": ["KINESIS2"]}), MockHovermapHandler.requests)

    def test_http_failure_is_typed(self):
        MockHovermapHandler.fail_stop = True
        with self.assertRaises(HovermapResponseError) as caught:
            self.client.stop_scan()
        self.assertEqual(caught.exception.status, 503)

    def test_redirect_is_rejected_before_contacting_second_origin(self):
        class RedirectSink(BaseHTTPRequestHandler):
            requests = 0

            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                self.__class__.requests += 1
                self.send_response(200)
                self.end_headers()

        sink = ThreadingHTTPServer(("127.0.0.1", 0), RedirectSink)
        sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
        sink_thread.start()
        try:
            host, port = sink.server_address
            MockHovermapHandler.redirect_status_to = f"http://{host}:{port}/capture"
            with self.assertRaises(HovermapResponseError) as caught:
                self.client.get_status()
            self.assertEqual(caught.exception.status, 302)
            self.assertEqual(RedirectSink.requests, 0)
        finally:
            MockHovermapHandler.redirect_status_to = None
            sink.shutdown()
            sink.server_close()
            sink_thread.join(timeout=2.0)

    def test_proxy_environment_is_ignored_for_device_traffic(self):
        proxy_environment = {
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "",
        }
        with patch.dict(os.environ, proxy_environment, clear=False):
            status = HovermapHttpClient(self.base_url).get_status()
        self.assertEqual(status.current_scan_name, "KINESIS_12")

    def test_timeout_is_typed(self):
        MockHovermapHandler.slow_status = True
        client = HovermapHttpClient(self.base_url, request_timeout=0.02)
        with self.assertRaises(HovermapTransportError):
            client.get_status()

    def test_download_is_validated_and_atomically_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            final_path = destination / "KINESIS_02.zip"
            final_path.write_bytes(b"old completed scan")
            result = self.client.download_scan("KINESIS_02", destination)
            self.assertEqual(result.path, final_path)
            self.assertEqual(result.size, len(GOOD_ZIP))
            self.assertEqual(final_path.read_bytes(), GOOD_ZIP)
            self.assertFalse((destination / "KINESIS_02.zip.part").exists())
            with zipfile.ZipFile(final_path) as archive:
                self.assertEqual(archive.read("metadata.txt"), b"hovermap fixture")

    def test_corrupt_download_never_replaces_completed_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            final_path = destination / "CORRUPT_01.zip"
            final_path.write_bytes(b"known good previous file")
            with self.assertRaises(HovermapProtocolError):
                self.client.download_scan("CORRUPT_01", destination)
            self.assertEqual(final_path.read_bytes(), b"known good previous file")
            self.assertTrue((destination / "CORRUPT_01.zip.part").exists())

    def test_concurrent_download_is_rejected_without_http_request(self):
        self.client._download_lock.acquire()
        try:
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(DownloadInProgressError):
                    self.client.download_scan("KINESIS_02", Path(directory))
        finally:
            self.client._download_lock.release()

    def test_content_length_mismatch_keeps_part_file(self):
        client = HovermapHttpClient(
            self.base_url,
            opener=lambda _request, timeout: StubResponse(GOOD_ZIP, len(GOOD_ZIP) + 1),
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            with self.assertRaises(HovermapProtocolError):
                client.download_scan("KINESIS_02", destination)
            self.assertFalse((destination / "KINESIS_02.zip").exists())
            self.assertEqual(
                (destination / "KINESIS_02.zip.part").read_bytes(), GOOD_ZIP
            )


class ValidationTests(unittest.TestCase):
    def test_prefix_validation(self):
        for valid in ("A", "KINESIS2026", "abc123"):
            validate_scan_prefix(valid)
        for invalid in ("", "has space", "has_underscore", "x" * 21, "../escape"):
            with self.assertRaises(ValueError):
                validate_scan_prefix(invalid)

    def test_scan_name_blocks_path_traversal(self):
        validate_scan_name("KINESIS_01")
        for invalid in (
            "KINESIS",
            "../KINESIS_01",
            "KINESIS_1.zip",
            "A_B_1",
            f"{'X' * 21}_01",
        ):
            with self.assertRaises(ValueError):
                validate_scan_name(invalid)

    def test_base_url_rejects_credentials_and_non_http_scheme(self):
        for invalid in ("ftp://hovermap", "http://user:secret@hovermap", "hovermap"):
            with self.assertRaises(ValueError):
                HovermapHttpClient(invalid)


if __name__ == "__main__":
    unittest.main()
