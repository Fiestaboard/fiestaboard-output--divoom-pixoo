"""check_connection's failure classes, the host fence, and brightness."""

from __future__ import annotations

import socket

import pytest

from src.outputs.plugin_base import CancelToken

from .conftest import build

ALLOW = "FIESTABOARD_OUTPUTS_ALLOW_HOSTS"


def frame(code: int) -> list[list[int]]:
    return [[code] * 16 for _ in range(10)]


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_a_healthy_device_reports_success_with_its_brightness(pixoo, make_plugin):
    check = make_plugin().check_connection()
    assert check.success and check.failure is None
    assert check.details == {"brightness": 60}
    assert pixoo.names() == ["Channel/GetAllConf"]


def test_nothing_listening_is_unreachable():
    check = build({"host": f"127.0.0.1:{closed_port()}"}).check_connection()
    assert (check.success, check.failure) == (False, "unreachable")
    assert check.troubleshooting


def test_a_device_that_never_answers_is_a_timeout(pixoo, make_plugin):
    pixoo.mode = "hang"
    check = make_plugin().check_connection()
    assert (check.success, check.failure) == (False, "timeout")


@pytest.mark.parametrize(("mode", "failure"), [("http_500", "server_error"), ("http_404", "unexpected_status")])
def test_http_errors_are_classified_by_status(pixoo, make_plugin, mode, failure):
    pixoo.mode = mode
    check = make_plugin().check_connection()
    assert (check.success, check.failure) == (False, failure)


@pytest.mark.parametrize("mode", ["garbage", "error_code", "json_list"])
def test_a_reply_that_is_not_the_pixoo_config_is_a_bad_response(pixoo, make_plugin, mode):
    pixoo.mode = mode
    check = make_plugin().check_connection()
    assert (check.success, check.failure) == (False, "bad_response")


def test_a_missing_host_is_reported_not_raised():
    check = build({}).check_connection()
    assert (check.success, check.failure) == (False, "unreachable")
    result = build({}).write(frame(1), native=None, cancel=CancelToken())
    assert (result.success, result.was_sent) == (False, False)


def test_a_host_outside_the_fence_is_blocked_and_never_contacted(pixoo, make_plugin, monkeypatch):
    monkeypatch.setenv(ALLOW, "fiestaboard-mock-pixoo")
    plugin = make_plugin()
    check = plugin.check_connection()
    assert (check.success, check.failure) == (False, "blocked")
    result = plugin.write(frame(1), native=None, cancel=CancelToken())
    assert (result.success, result.was_sent) == (False, False)
    assert pixoo.commands == []


def test_a_fenced_host_on_the_allow_list_is_contacted(pixoo, make_plugin, monkeypatch):
    monkeypatch.setenv(ALLOW, "127.0.0.1")
    assert make_plugin().check_connection().success


@pytest.mark.parametrize(
    ("host", "key"),
    [
        ("192.0.2.10", "192.0.2.10"),
        (" HTTP://Pixoo.Local/ ", "pixoo.local"),
        ("192.0.2.10:8080", "192.0.2.10:8080"),
        ("http://192.0.2.10/post", "192.0.2.10"),
    ],
)
def test_the_device_key_is_the_normalised_host(host, key):
    assert build({"host": host}).device_key() == key


def test_brightness_is_set_once_after_the_first_frame_lands(pixoo, make_plugin):
    plugin = make_plugin(brightness=40)
    plugin.write(frame(1), native=None, cancel=CancelToken())
    plugin.write(frame(2), native=None, cancel=CancelToken())
    assert pixoo.names() == [
        "Draw/ResetHttpGifId",
        "Draw/GetHttpGifId",
        "Draw/SendHttpGif",
        "Channel/SetBrightness",
        "Draw/SendHttpGif",
    ]
    assert pixoo.commands[3] == {"Command": "Channel/SetBrightness", "Brightness": 40}


def test_without_a_brightness_setting_the_device_keeps_its_own(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.write(frame(1), native=None, cancel=CancelToken())
    assert "Channel/SetBrightness" not in pixoo.names()


def test_a_failed_brightness_call_keeps_the_frame_and_retries_next_write(pixoo, make_plugin):
    plugin = make_plugin(brightness=250)

    def fail_brightness(command, index):
        pixoo.mode = "http_500" if command.get("Command") == "Channel/SetBrightness" else "ok"

    pixoo.on_command = fail_brightness
    assert plugin.write(frame(1), native=None, cancel=CancelToken()).success
    pixoo.on_command = None
    pixoo.mode = "ok"
    plugin.write(frame(2), native=None, cancel=CancelToken())
    sets = [c for c in pixoo.commands if c["Command"] == "Channel/SetBrightness"]
    assert sets == [{"Command": "Channel/SetBrightness", "Brightness": 100}] * 2


def test_brightness_also_follows_a_sequence(pixoo, make_plugin):
    plugin = make_plugin(brightness=10)
    from src.outputs.plugin_base import TimedFrame

    plugin.write_sequence([TimedFrame(frame(1), 100), TimedFrame(frame(2), 100)], cancel=CancelToken())
    assert pixoo.names()[-1] == "Channel/SetBrightness"
