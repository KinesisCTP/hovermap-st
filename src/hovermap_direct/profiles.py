"""Built-in network profiles for direct Hovermap connections."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

PROFILE_URLS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "wifi": "http://10.9.0.1",
        "fischer": "http://192.168.2.115",
        "usb": "http://192.168.3.115",
    }
)
SUPPORTED_PROFILES: Final[tuple[str, ...]] = tuple(PROFILE_URLS)


def get_profile_url(profile: str) -> str:
    """Return the fixed device origin for *profile*."""

    if not isinstance(profile, str) or profile not in PROFILE_URLS:
        choices = ", ".join(SUPPORTED_PROFILES)
        raise ValueError(f"profile must be one of: {choices}")
    return PROFILE_URLS[profile]
