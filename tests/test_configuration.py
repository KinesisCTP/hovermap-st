from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hovermap_mcp.configuration import ConfigurationError, parse_config


def test_defaults_use_wifi_profile_and_declared_limits(tmp_path: Path) -> None:
    config = parse_config(
        [],
        environ={"HOVERMAP_DOWNLOAD_DIRECTORY": str(tmp_path / "downloads")},
    )

    assert config.profile == "wifi"
    assert config.hovermap_url == "http://10.9.0.1"
    assert config.download_directory == (tmp_path / "downloads").resolve()
    assert config.download_directory.is_dir()
    assert config.request_timeout_seconds == 5
    assert config.download_timeout_seconds == 450
    assert config.max_json_bytes == 4_194_304
    assert config.max_download_bytes == 137_438_953_472
    assert config.log_level == "INFO"


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("wifi", "http://10.9.0.1"),
        ("fischer", "http://192.168.2.115"),
        ("usb", "http://192.168.3.115"),
    ],
)
def test_network_profiles(profile: str, expected: str, tmp_path: Path) -> None:
    config = parse_config(
        ["--profile", profile, "--download-directory", str(tmp_path)],
        environ={},
    )
    assert config.hovermap_url == expected


def test_cli_overrides_environment_and_explicit_url_overrides_profile(
    tmp_path: Path,
) -> None:
    config = parse_config(
        [
            "--profile",
            "usb",
            "--hovermap-url",
            "https://127.0.0.1:9443/",
            "--download-directory",
            str(tmp_path / "cli"),
            "--request-timeout-seconds",
            "12.5",
            "--log-level",
            "DEBUG",
        ],
        environ={
            "HOVERMAP_PROFILE": "fischer",
            "HOVERMAP_URL": "http://example.invalid",
            "HOVERMAP_DOWNLOAD_DIRECTORY": str(tmp_path / "env"),
            "HOVERMAP_REQUEST_TIMEOUT_SECONDS": "7",
            "HOVERMAP_LOG_LEVEL": "ERROR",
        },
    )

    assert config.profile == "usb"
    assert config.hovermap_url == "https://127.0.0.1:9443"
    assert config.download_directory == (tmp_path / "cli").resolve()
    assert config.request_timeout_seconds == 12.5
    assert config.log_level == "DEBUG"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://10.9.0.1",
        "http://",
        "http://user:password@10.9.0.1",
        "http://10.9.0.1/status",
        "http://10.9.0.1?x=1",
        "http://10.9.0.1?",
        "http://10.9.0.1#fragment",
        "http://10.9.0.1#",
        " http://10.9.0.1",
        "http://good.example\tevil",
        "http://good.example\\evil",
        "http://good.example\x7f",
        "http://10.9.0.1:0",
        "http://10.9.0.1:70000",
    ],
)
def test_hovermap_url_must_be_one_origin(url: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        parse_config(
            ["--hovermap-url", url, "--download-directory", str(tmp_path)],
            environ={},
        )


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--request-timeout-seconds", "0"),
        ("--request-timeout-seconds", "300.1"),
        ("--download-timeout-seconds", "3600.1"),
        ("--max-json-bytes", "0"),
        ("--max-json-bytes", "67108865"),
        ("--max-download-bytes", "1099511627777"),
        ("--max-download-bytes", "1.5"),
    ],
)
def test_numeric_bounds_are_enforced(option: str, value: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        parse_config(
            [option, value, "--download-directory", str(tmp_path)],
            environ={},
        )


def test_invalid_profile_and_log_level_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        parse_config(
            ["--profile", "ethernet", "--download-directory", str(tmp_path)],
            environ={},
        )
    with pytest.raises(ConfigurationError):
        parse_config(
            ["--log-level", "TRACE", "--download-directory", str(tmp_path)],
            environ={},
        )
    with pytest.raises(ConfigurationError):
        parse_config(
            ["--log-level", "debug", "--download-directory", str(tmp_path)],
            environ={},
        )


def test_cli_option_abbreviations_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        parse_config(
            ["--profi", "usb", "--download-directory", str(tmp_path)],
            environ={},
        )


def test_download_destination_must_be_a_directory(tmp_path: Path) -> None:
    destination = tmp_path / "archive"
    destination.write_bytes(b"not a directory")

    with pytest.raises(ConfigurationError):
        parse_config(["--download-directory", str(destination)], environ={})


def test_download_destination_must_be_writable(tmp_path: Path) -> None:
    with (
        patch(
            "hovermap_mcp.configuration.tempfile.TemporaryFile",
            side_effect=PermissionError("read only"),
        ),
        pytest.raises(ConfigurationError),
    ):
        parse_config(["--download-directory", str(tmp_path)], environ={})


def test_unresolvable_home_is_a_clean_configuration_error() -> None:
    with (
        patch("hovermap_mcp.configuration.Path.expanduser", side_effect=RuntimeError("no home")),
        pytest.raises(ConfigurationError),
    ):
        parse_config([], environ={})


def test_prepare_directory_can_be_disabled_for_read_only_validation(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "not-created"
    config = parse_config(
        ["--download-directory", str(destination)],
        environ={},
        prepare_directory=False,
    )
    assert config.download_directory == destination.resolve()
    assert not destination.exists()
