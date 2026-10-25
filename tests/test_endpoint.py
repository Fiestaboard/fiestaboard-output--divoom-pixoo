"""Explicit endpoints route real loopback requests without changing discovery."""

from __future__ import annotations

import pytest
import requests
from src.outputs.plugin_base import CancelToken

from .conftest import build
from .test_connection import closed_port, frame
from .test_device_poll import moved


@pytest.mark.parametrize("path", ["/divoom_api", "/Api%2Fv1"])
@pytest.mark.parametrize("scheme", ["", "http://"])
def test_manual_connection_uses_the_explicit_port_and_path(pixoo, path, scheme):
    pixoo.path = path
    plugin = build({"host": f"{scheme}{pixoo.host}{path}"})
    check = plugin.check_connection()
    assert check.success, check
    assert check.details == {"brightness": 60}


def test_upload_uses_the_same_explicit_endpoint_as_connection_testing(pixoo):
    pixoo.path = "/divoom_api"
    plugin = build({"host": f"http://{pixoo.host}/divoom_api"})
    result = plugin.write(frame(1), native=None, cancel=CancelToken())
    assert result.success and result.was_sent
    assert len(pixoo.displayed) == 1


@pytest.mark.parametrize("mode", ["garbage", "error_code", "json_list"])
def test_explicit_endpoint_does_not_turn_protocol_failures_into_success(pixoo, mode):
    pixoo.path = "/divoom_api"
    pixoo.mode = mode
    check = build({"host": f"{pixoo.host}/divoom_api"}).check_connection()
    assert (check.success, check.failure) == (False, "bad_response")


def test_wrong_explicit_path_is_reported_as_http_failure(pixoo):
    check = build({"host": f"{pixoo.host}/wrong"}).check_connection()
    assert (check.success, check.failure) == (False, "unexpected_status")


@pytest.mark.parametrize("suffix", ["", "/", "/post"])
def test_default_endpoint_forms_still_connect(pixoo, suffix):
    assert build({"host": f"http://{pixoo.host}{suffix}"}).check_connection().success


@pytest.mark.parametrize("suffix", ["/divoom_api", "/post"])
def test_cloud_recovery_does_not_overwrite_a_custom_port_or_path(monkeypatch, suffix):
    address = f"127.0.0.1:{closed_port()}{suffix}"
    plugin = build({"host": address, "mac": "001122334455"})
    lookups = []
    monkeypatch.setattr(plugin, "_cloud_devices", lambda: lookups.append(True) or [])
    assert plugin.poll_device() is None
    assert lookups == []
    assert plugin.config["host"] == address


@pytest.mark.parametrize(
    "address",
    [
        "ftp://127.0.0.1",
        "http://user:secret@127.0.0.1",
        "http://127.0.0.1/post?token=x",
        "http://127.0.0.1/post#fragment",
    ],
)
def test_unsupported_endpoint_parts_fail_before_any_request(pixoo, address):
    address = address.replace("127.0.0.1", pixoo.host)
    check = build({"host": address}).check_connection()
    assert (check.success, check.failure) == (False, "blocked")
    assert check.message == "Not contacted: this is not a device address the Pixoo plugin can use."
    assert any("HTTP or HTTPS" in step and "port and path" in step for step in check.troubleshooting)
    assert any("query" in step and "fragment" in step for step in check.troubleshooting)
    assert "secret" not in check.message + check.error + " ".join(check.troubleshooting)
    assert pixoo.commands == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1:0",
        "127.0.0.1:abc",
        "127.0.0.1:99999",
        "http://127.0.0.1:0/post",
        "http:/127.0.0.1",
        "127.0.0.1:/post",
    ],
)
def test_bad_ports_and_single_slash_typos_are_invalid_endpoints(address):
    plugin = build({"host": address})
    with pytest.raises(requests.exceptions.InvalidURL):
        plugin.url
    check = plugin.check_connection()
    assert (check.success, check.failure) == (False, "blocked")
    assert check.message == "Not contacted: this is not a device address the Pixoo plugin can use."


def test_missing_address_is_still_reported_as_no_address():
    check = build({"host": ""}).check_connection()
    assert (check.success, check.failure, check.error) == (False, "unreachable", "No address")


def test_device_poll_does_not_refind_an_invalid_endpoint(monkeypatch):
    plugin = build({"host": "127.0.0.1:abc", "mac": "001122334455"})
    lookups = []
    monkeypatch.setattr(plugin, "_cloud_devices", lambda: lookups.append(True) or [])
    assert plugin._refind() is None
    assert plugin.poll_device() is None
    assert lookups == []
    assert plugin.config["host"] == "127.0.0.1:abc"


def test_ipv6_literal_endpoint_keeps_its_brackets_port_and_path():
    plugin = build({"host": "http://[fe80::1]:9000/x"})
    assert plugin.url == "http://[fe80::1]:9000/x"


def test_surrounding_whitespace_is_stripped_from_the_endpoint():
    plugin = build({"host": "  http://192.0.2.24:9000/divoom_api \t"})
    assert plugin.url == "http://192.0.2.24:9000/divoom_api"


def test_https_endpoint_keeps_its_scheme_and_case_sensitive_path():
    plugin = build({"host": "https://PIxoo.Example:9443/Api%2Fv1"})
    assert plugin.url == "https://pixoo.example:9443/Api%2Fv1"
    assert plugin.device_key() == "pixoo.example:9443"


@pytest.mark.parametrize("address", ["192.0.2.24/divoom_api", "https://192.0.2.24/post"])
def test_cloud_recovery_does_not_replace_custom_path_or_https_on_default_port(address):
    plugin, network = moved([], set(), host=address)
    assert plugin.poll_device() is None
    assert network.cloud_calls() == 0
    assert plugin.config["host"] == address
