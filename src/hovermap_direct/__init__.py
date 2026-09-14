"""ROS-independent access to the Hovermap HTTP control API."""

from .http_client import (
    ControlInProgressError,
    DownloadInProgressError,
    DownloadResult,
    HovermapCancelledError,
    HovermapError,
    HovermapHttpClient,
    HovermapProtocolError,
    HovermapResponseError,
    HovermapStatusData,
    HovermapTransportError,
    ScanData,
    ScanListData,
    validate_scan_name,
    validate_scan_prefix,
)

__all__ = [
    "ControlInProgressError",
    "DownloadInProgressError",
    "DownloadResult",
    "HovermapCancelledError",
    "HovermapError",
    "HovermapHttpClient",
    "HovermapProtocolError",
    "HovermapResponseError",
    "HovermapStatusData",
    "HovermapTransportError",
    "ScanData",
    "ScanListData",
    "validate_scan_name",
    "validate_scan_prefix",
]
