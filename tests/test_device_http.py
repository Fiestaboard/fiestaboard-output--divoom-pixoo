"""Every device request goes through core's ``self.http`` helper."""

from __future__ import annotations

import threading

import requests

from src.outputs.plugin_base import CancelToken

from .conftest import build


def frame(code: int) -> list[list[int]]:
    return [[code] * 16 for _ in range(10)]


def recording(plugin):
    seen = []

    def transport(request):
        seen.append(request)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"error_code": 0}'
        return response

    plugin.http.use_transport(transport)
    return seen


def test_requests_go_through_the_helper_with_setup_marked_and_the_plugin_timeouts():
    plugin = build({"host": "192.0.2.10", "brightness": 30}, fast=False)
    seen = recording(plugin)
    assert plugin.write(frame(1), native=None, cancel=CancelToken()).success
    assert [(r.method, r.url, r.json["Command"], r.setup) for r in seen] == [
        ("POST", "http://192.0.2.10/post", "Draw/ResetHttpGifId", True),
        ("POST", "http://192.0.2.10/post", "Draw/SendHttpGif", False),
        ("POST", "http://192.0.2.10/post", "Channel/SetBrightness", True),
    ]
    assert {r.timeout for r in seen} == {(3.0, 5.0)}
    assert all(r.kwargs["allow_redirects"] is False for r in seen)


def test_a_request_refused_by_the_bound_cancel_scope_is_a_preempted_write():
    plugin = build({"host": "192.0.2.10"})
    seen = recording(plugin)
    event = threading.Event()
    event.set()
    with plugin.http.cancel_scope(CancelToken(event)):
        # The plugin's own token has not fired; core's bound one has.
        result = plugin.write(frame(1), native=None, cancel=CancelToken())
    assert (result.success, result.was_sent) == (True, False)
    assert seen == []
    # The device counter is unknown now: the next write resets first.
    plugin.write(frame(2), native=None, cancel=CancelToken())
    assert seen[0].json == {"Command": "Draw/ResetHttpGifId"}


def test_a_connection_check_refused_by_the_helper_is_reported_not_raised():
    plugin = build({"host": "192.0.2.10"})
    seen = recording(plugin)
    event = threading.Event()
    event.set()
    with plugin.http.cancel_scope(CancelToken(event)):
        check = plugin.check_connection()
    assert (check.success, check.failure) == (False, "unreachable")
    assert seen == []
