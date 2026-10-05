"""The PicID counter: reset per session and every <=32 pushes, seeded from
Draw/GetHttpGifId, never reused or lowered (lab-verified device rules)."""

from __future__ import annotations

import threading
import time

from plugins.divoom_pixoo import RESET_AFTER_PUSHES

from src.outputs.plugin_base import CancelToken

RESET = "Draw/ResetHttpGifId"
GET_ID = "Draw/GetHttpGifId"
SEND = "Draw/SendHttpGif"


def frame(code: int) -> list[list[int]]:
    return [[code] * 16 for _ in range(10)]


def write(plugin, code: int):
    return plugin.write(frame(code), native=None, cancel=CancelToken())


def test_a_session_starts_with_a_reset_and_a_seed_then_counts_up(pixoo, make_plugin):
    plugin = make_plugin()
    for code in (1, 2, 3):
        assert write(plugin, code).success
    assert pixoo.names() == [RESET, GET_ID, SEND, SEND, SEND]
    assert [c["PicID"] for c in pixoo.pushes()] == [1, 2, 3]
    assert len(pixoo.displayed) == 3 and pixoo.ignored == []


def test_the_seed_is_the_device_s_next_id_when_a_reset_did_not_take(pixoo, make_plugin):
    pixoo.ignore_reset = True
    pixoo.last_pic_id = 6
    plugin = make_plugin()
    assert write(plugin, 1).success
    assert pixoo.pushes()[0]["PicID"] == 7
    assert pixoo.ignored == []


def test_the_counter_resets_after_the_push_threshold_without_a_new_seed(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.RESET_AFTER_PUSHES = 3
    for code in range(1, 6):
        assert write(plugin, code).success
    assert pixoo.names() == [RESET, GET_ID, SEND, SEND, SEND, RESET, SEND, SEND]
    assert [c["PicID"] for c in pixoo.pushes()] == [1, 2, 3, 1, 2]
    assert pixoo.ignored == []


def test_the_threshold_is_at_most_32_pushes():
    assert 1 <= RESET_AFTER_PUSHES <= 32


def test_a_failed_write_reports_failure_and_the_next_write_starts_a_new_session(pixoo, make_plugin):
    plugin = make_plugin()
    assert write(plugin, 1).success
    pixoo.mode = "http_500"
    result = write(plugin, 2)
    assert (result.success, result.was_sent, result.partial) == (False, False, False)
    pixoo.mode = "ok"
    pixoo.clear()
    assert write(plugin, 3).success
    assert pixoo.names() == [RESET, GET_ID, SEND]
    assert pixoo.pushes()[0]["PicID"] == 1


def test_ids_never_repeat_or_go_down_without_a_reset(pixoo, make_plugin):
    plugin = make_plugin()
    for code in range(1, 30):
        assert write(plugin, code % 60 + 1).success
    ids = [c["PicID"] for c in pixoo.pushes()]
    resets = [i for i, name in enumerate(pixoo.names()) if name == RESET]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert len(resets) == 1
    assert pixoo.ignored == []


def test_a_device_error_code_is_a_failed_write(pixoo, make_plugin):
    plugin = make_plugin()
    pixoo.mode = "error_code"
    result = write(plugin, 1)
    assert (result.success, result.was_sent) == (False, False)


def test_a_failed_reset_sends_no_frame(pixoo, make_plugin):
    plugin = make_plugin()
    pixoo.mode = "http_500"
    result = write(plugin, 1)
    assert (result.success, result.was_sent) == (False, False)
    assert pixoo.names() == [RESET]


def test_the_reset_policy_keeps_a_freezing_device_alive(pixoo, make_plugin):
    pixoo.freeze_after = 40  # the mock's stand-in for the reported "~300 pushes"
    plugin = make_plugin()
    results = [write(plugin, code % 60 + 1) for code in range(100)]
    assert all(r.success for r in results)
    assert pixoo.names().count(RESET) >= 100 // RESET_AFTER_PUSHES


def test_without_resets_the_freeze_is_a_bounded_failed_write(pixoo, make_plugin):
    pixoo.freeze_after = 5
    plugin = make_plugin()
    plugin.RESET_AFTER_PUSHES = 10_000
    assert all(write(plugin, code).success for code in range(1, 6))
    started = time.monotonic()
    result = write(plugin, 6)
    assert (result.success, result.was_sent) == (False, False)
    assert time.monotonic() - started < plugin.CONNECT_TIMEOUT_S + plugin.READ_TIMEOUT_S + 1.0


def test_a_write_cancelled_before_it_starts_sends_nothing(pixoo, make_plugin):
    plugin = make_plugin()
    event = threading.Event()
    event.set()
    result = plugin.write(frame(1), native=None, cancel=CancelToken(event))
    assert (result.success, result.was_sent) == (True, False)
    assert pixoo.commands == []


def test_a_write_cancelled_after_the_reset_sends_no_frame(pixoo, make_plugin):
    plugin = make_plugin()
    event = threading.Event()
    pixoo.on_command = lambda command, index: event.set()
    result = plugin.write(frame(1), native=None, cancel=CancelToken(event))
    assert (result.success, result.was_sent) == (True, False)
    assert SEND not in pixoo.names()
