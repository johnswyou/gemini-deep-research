"""Tests for `gdr.core.client`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gdr.core import client as client_module
from gdr.core.client import GdrClient, api_key_fingerprint, sdk_version
from gdr.errors import ConfigError


def test_sdk_version_returns_string() -> None:
    # google-genai is a hard dependency so this should always resolve.
    result = sdk_version()
    assert isinstance(result, str)
    assert result != "unknown"


def test_api_key_fingerprint_masks_middle() -> None:
    fp = api_key_fingerprint("AIzaSyA1234567890abcdefXYZW")
    # First 4 + last 4 with an ellipsis separator.
    assert fp.startswith("AIza")
    assert fp.endswith("XYZW")
    assert "…" in fp


def test_api_key_fingerprint_rejects_short_keys() -> None:
    assert api_key_fingerprint("short") == "invalid"
    assert api_key_fingerprint("") == "invalid"


def test_gdr_client_rejects_missing_key() -> None:
    with pytest.raises(ConfigError) as excinfo:
        GdrClient(api_key=None)
    assert "No Gemini API key found" in str(excinfo.value)


def test_gdr_client_rejects_empty_key() -> None:
    with pytest.raises(ConfigError):
        GdrClient(api_key="")


def test_gdr_client_constructs_with_valid_key(mocker: MagicMock) -> None:
    fake_client = MagicMock()
    fake_client.interactions = MagicMock()
    fake_genai = MagicMock()
    fake_genai.Client.return_value = fake_client
    mocker.patch.object(client_module, "genai", fake_genai, create=True)
    # genai is imported lazily inside __init__, so patch the "google.genai" module too:
    mocker.patch("google.genai.Client", return_value=fake_client)

    gdr_client = GdrClient(api_key="AIzaSy-test-key-1234567890")
    assert gdr_client.interactions is fake_client.interactions
    assert gdr_client.raw is fake_client
    assert gdr_client.fingerprint().startswith("AIza")


def test_gdr_client_errors_when_sdk_lacks_interactions(mocker: MagicMock) -> None:
    fake_client = MagicMock(spec=["models"])  # note: no `interactions` attribute
    mocker.patch("google.genai.Client", return_value=fake_client)

    with pytest.raises(ConfigError) as excinfo:
        GdrClient(api_key="AIzaSy-test-key-1234567890")
    assert "Interactions API" in str(excinfo.value)


def test_repr_does_not_contain_api_key(mocker: MagicMock) -> None:
    fake_client = MagicMock()
    fake_client.interactions = MagicMock()
    mocker.patch("google.genai.Client", return_value=fake_client)

    gdr_client = GdrClient(api_key="AIzaSy-VERY-SECRET-abcdefg1234")
    rendered = repr(gdr_client)
    assert "VERY-SECRET" not in rendered
    assert "GdrClient" in rendered
