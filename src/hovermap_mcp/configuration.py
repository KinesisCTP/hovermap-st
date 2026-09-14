"""Startup configuration for the local Hovermap MCP server."""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from hovermap_direct.profiles import PROFILE_URLS

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigurationError(ValueError):
    """A startup value is invalid or cannot be prepared safely."""


@dataclass(frozen=True)
class ServerConfig:
    """Fully validated, resolved configuration used by the server runtime."""

    profile: str
    hovermap_url: str
    download_directory: Path
    request_timeout_seconds: float
    download_timeout_seconds: float
    max_json_bytes: int
    max_download_bytes: int
    log_level: str

    @property
    def numeric_log_level(self) -> int:
        return int(getattr(logging, self.log_level))


def parse_config(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    prepare_directory: bool = True,
) -> ServerConfig:
    """Parse CLI and environment values with CLI > environment > default precedence."""

    env = os.environ if environ is None else environ
    parser = _argument_parser()
    namespace = parser.parse_args(argv)

    profile = _select(namespace.profile, env, "HOVERMAP_PROFILE", "wifi")
    if profile not in PROFILE_URLS:
        raise ConfigurationError("profile must be one of: wifi, fischer, usb")

    raw_url = _select(namespace.hovermap_url, env, "HOVERMAP_URL", None)
    hovermap_url = _validate_origin_url(PROFILE_URLS[profile] if raw_url is None else raw_url)

    raw_directory = _select(
        namespace.download_directory,
        env,
        "HOVERMAP_DOWNLOAD_DIRECTORY",
        "~/hovermap_downloads",
    )
    download_directory = _resolve_download_directory(raw_directory, prepare=prepare_directory)

    request_timeout = _bounded_float(
        _select(
            namespace.request_timeout_seconds,
            env,
            "HOVERMAP_REQUEST_TIMEOUT_SECONDS",
            "5",
        ),
        "request timeout",
        maximum=300.0,
    )
    download_timeout = _bounded_float(
        _select(
            namespace.download_timeout_seconds,
            env,
            "HOVERMAP_DOWNLOAD_TIMEOUT_SECONDS",
            "450",
        ),
        "download timeout",
        maximum=3600.0,
    )
    max_json_bytes = _bounded_int(
        _select(
            namespace.max_json_bytes,
            env,
            "HOVERMAP_MAX_JSON_BYTES",
            "4194304",
        ),
        "maximum JSON bytes",
        maximum=67_108_864,
    )
    max_download_bytes = _bounded_int(
        _select(
            namespace.max_download_bytes,
            env,
            "HOVERMAP_MAX_DOWNLOAD_BYTES",
            "137438953472",
        ),
        "maximum download bytes",
        maximum=1_099_511_627_776,
    )

    log_level = _select(namespace.log_level, env, "HOVERMAP_LOG_LEVEL", "INFO")
    if log_level not in LOG_LEVELS:
        raise ConfigurationError("log level must be one of: DEBUG, INFO, WARNING, ERROR")

    return ServerConfig(
        profile=profile,
        hovermap_url=hovermap_url,
        download_directory=download_directory,
        request_timeout_seconds=request_timeout,
        download_timeout_seconds=download_timeout,
        max_json_bytes=max_json_bytes,
        max_download_bytes=max_download_bytes,
        log_level=log_level,
    )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hovermap-mcp",
        description="Run the ROS-free Hovermap MCP server over stdio.",
        allow_abbrev=False,
    )
    parser.add_argument("--profile")
    parser.add_argument("--hovermap-url")
    parser.add_argument("--download-directory")
    parser.add_argument("--request-timeout-seconds")
    parser.add_argument("--download-timeout-seconds")
    parser.add_argument("--max-json-bytes")
    parser.add_argument("--max-download-bytes")
    parser.add_argument("--log-level")
    return parser


def _select(
    cli_value: str | None,
    environ: Mapping[str, str],
    variable: str,
    default: str | None,
) -> str | None:
    if cli_value is not None:
        return cli_value
    if variable in environ:
        return environ[variable]
    return default


def _validate_origin_url(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConfigurationError("Hovermap URL must be a non-empty HTTP(S) origin")
    if "\\" in value or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ConfigurationError(
            "Hovermap URL must not contain whitespace, controls, or backslashes"
        )
    # urlsplit() erases the distinction between an absent delimiter and an
    # explicitly empty query/fragment, but neither is valid in an origin-only URL.
    if "?" in value or "#" in value:
        raise ConfigurationError("Hovermap URL must not contain a query or fragment")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("Hovermap URL contains an invalid port") from exc
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ConfigurationError("Hovermap URL scheme must be http or https")
    if not parsed.hostname:
        raise ConfigurationError("Hovermap URL must contain a host")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("Hovermap URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("Hovermap URL must not contain a query or fragment")
    if parsed.path not in ("", "/"):
        raise ConfigurationError("Hovermap URL must be an origin without an API path")
    if port is not None and not 1 <= port <= 65_535:
        raise ConfigurationError("Hovermap URL port must be between 1 and 65535")
    return urlunsplit((scheme, parsed.netloc, "", "", ""))


def _resolve_download_directory(value: object, *, prepare: bool) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ConfigurationError("download directory must be a non-empty local path")
    try:
        resolved = Path(value).expanduser().resolve(strict=False)
        if prepare:
            resolved.mkdir(parents=True, exist_ok=True)
            if not resolved.is_dir():
                raise ConfigurationError("configured download destination is not a directory")
            with tempfile.TemporaryFile(dir=resolved):
                pass
    except ConfigurationError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ConfigurationError(
            "configured download directory could not be created or resolved"
        ) from exc
    return resolved


def _bounded_float(value: object, label: str, *, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label} must be a number") from exc
    if not 0.0 < result <= maximum:
        raise ConfigurationError(f"{label} must be greater than 0 and at most {maximum:g}")
    return result


def _bounded_int(value: object, label: str, *, maximum: int) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{label} must be an integer")
    try:
        text = str(value)
        if not text or text.strip() != text or any(ch not in "0123456789" for ch in text):
            raise ValueError
        result = int(text, 10)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label} must be an integer") from exc
    if not 1 <= result <= maximum:
        raise ConfigurationError(f"{label} must be between 1 and {maximum}")
    return result
