"""Tests for the fixed Hovermap network profiles."""

import pytest

from hovermap_direct.profiles import (
    PROFILE_URLS,
    SUPPORTED_PROFILES,
    get_profile_url,
)


def test_profile_addresses_are_exact_and_stable() -> None:
    assert dict(PROFILE_URLS) == {
        "wifi": "http://10.9.0.1",
        "fischer": "http://192.168.2.115",
        "usb": "http://192.168.3.115",
    }
    assert SUPPORTED_PROFILES == ("wifi", "fischer", "usb")
    assert get_profile_url("wifi") == "http://10.9.0.1"


@pytest.mark.parametrize("value", ["", "WiFi", "ethernet", None, 1])
def test_unknown_profile_is_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="profile must be one of"):
        get_profile_url(value)  # type: ignore[arg-type]


def test_profile_mapping_cannot_be_mutated() -> None:
    with pytest.raises(TypeError):
        PROFILE_URLS["wifi"] = "http://127.0.0.1"  # type: ignore[index]
