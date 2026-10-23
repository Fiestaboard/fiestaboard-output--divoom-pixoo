"""Changes of page: every transition and explicit sequence is streamed as
paced single-frame pushes ending on the target; a fade goes through black
with dimmed pixel frames unless the board asks for a blend."""

from __future__ import annotations

import base64
import copy
import threading
import time

import pytest
from plugins.divoom_pixoo import SEQUENCE_MAX_FRAMES, STREAM_MAX_FPS

from src.led.transitions import LedTransitionSpec
from src.markup import message_to_grid
from src.outputs.cells import cells_from_codes
from src.outputs.plugin_base import CancelToken, TimedFrame
from src.plugins import ResolvedLedTransition, resolve_led_transition

from .conftest import MANIFEST
from .conftest import render as render_frame

RESET = "Draw/ResetHttpGifId"
GET_ID = "Draw/GetHttpGifId"
SEND = "Draw/SendHttpGif"
BRIGHTNESS = "Channel/SetBrightness"
GET_CONF = "Channel/GetAllConf"
MODEL = MANIFEST.output.model(0)


def frame(code: int) -> list[list[int]]:
    return [[code] * 16 for _ in range(10)]


def cells(code: int):
    return cells_from_codes(frame(code))


def data(code: int) -> str:
    return base64.b64encode(render_frame(frame(code))).decode("ascii")


def sequence(*codes: int, ms: int = 100) -> list[TimedFrame]:
    return [TimedFrame(frame(c), ms) for c in codes]


def explicit(kind: str) -> ResolvedLedTransition:
    return ResolvedLedTransition(kind, LedTransitionSpec(kind), "explicit")


def levels(pixoo) -> list[int]:
    return [c["Brightness"] for c in pixoo.commands if c.get("Command") == BRIGHTNESS]


def push_times(pixoo) -> list[float]:
    stamps: list[float] = []
    pixoo.on_command = lambda command, index: (
        stamps.append(time.monotonic()) if command.get("Command") == SEND else None
    )
    return stamps


# --- what FiestaBoard resolves for the Pixoo -----------------------------------------------------


def test_the_pixoo_model_streams_at_five_frames_a_second():
    assert MANIFEST.output.capabilities.animation == "stream"
    assert MODEL["animation"]["maxFps"] == STREAM_MAX_FPS == 5


def test_the_pixoo_model_defaults_to_a_coarse_flip():
    resolved = resolve_led_transition(None, MODEL)
    assert (resolved.id, resolved.source) == ("flip", "default")
    assert (resolved.spec.step_ms, resolved.spec.half_flap) == (200, False)


def test_a_none_transition_pushes_only_the_final_still(pixoo, make_plugin):
    plugin = make_plugin()
    result = plugin.write_transition(cells(1), cells(2), resolve_led_transition("none", MODEL), cancel=CancelToken())
    assert (result.success, result.was_sent) == (True, True)
    assert [(c["PicNum"], c["PicData"]) for c in pixoo.pushes()] == [(1, data(2))]


# --- streamed transitions ------------------------------------------------------------------------


def test_the_default_flip_is_streamed_as_single_frame_pushes_ending_on_the_target(pixoo, make_plugin):
    plugin = make_plugin()
    result = plugin.write_transition(cells(1), cells(2), resolve_led_transition(None, MODEL), cancel=CancelToken())

    assert (result.success, result.was_sent) == (True, True)
    pushes = pixoo.pushes()
    assert len(pushes) > 3
    assert all(c["PicNum"] == 1 and c["PicOffset"] == 0 and c["PicWidth"] == 64 for c in pushes)
    # Every push a fresh, higher PicID, so the device shows every one.
    ids = [c["PicID"] for c in pushes]
    assert ids == sorted(set(ids))
    assert pixoo.ignored == []
    assert pushes[-1]["PicData"] == data(2)
    # The old board is on screen already: the stream never re-sends it.
    assert pushes[0]["PicData"] != data(1)


@pytest.mark.parametrize("kind", ["flip", "cascade", "slide", "wipe", "dissolve"])
def test_every_pixel_transition_streams_and_lands_on_the_target(pixoo, make_plugin, kind):
    plugin = make_plugin()
    result = plugin.write_transition(cells(1), cells(2), explicit(kind), cancel=CancelToken())
    assert (result.success, result.was_sent) == (True, True)
    pushes = pixoo.pushes()
    assert len(pushes) >= 2 and len({c["PicData"] for c in pushes}) >= 2
    assert pushes[-1]["PicData"] == data(2)
    assert BRIGHTNESS not in pixoo.names()


def test_a_continuous_transition_with_no_duration_runs_at_least_a_second(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.write_transition(cells(1), cells(2), explicit("slide"), cancel=CancelToken())
    # 1000 ms at 5 fps: five frames after the one already on screen.
    assert len(pixoo.pushes()) == 5


def test_an_explicit_duration_is_kept(pixoo, make_plugin):
    plugin = make_plugin()
    spec = ResolvedLedTransition("slide", LedTransitionSpec("slide", duration_ms=400), "explicit")
    plugin.write_transition(cells(1), cells(2), spec, cancel=CancelToken())
    assert len(pixoo.pushes()) == 2


def test_a_slower_model_plans_fewer_frames_and_paces_them_wider(pixoo, make_plugin, monkeypatch):
    plugin = make_plugin()
    plugin.write_transition(cells(1), cells(2), explicit("wipe"), cancel=CancelToken())
    at_five = len(pixoo.pushes())
    pixoo.clear()
    slow = copy.deepcopy(MODEL)
    slow["animation"]["maxFps"] = 2
    monkeypatch.setattr(type(plugin), "device_model", property(lambda self: slow))
    plugin.write_transition(cells(1), cells(2), explicit("wipe"), cancel=CancelToken())
    assert 1 <= len(pixoo.pushes()) < at_five
    plugin.STREAM_STEP_S = None
    assert plugin._step_s() == pytest.approx(0.5)


def test_frames_are_paced_one_stream_step_apart(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.STREAM_STEP_S = 0.1
    stamps = push_times(pixoo)
    plugin.write_transition(cells(1), cells(2), explicit("wipe"), cancel=CancelToken())
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert gaps and min(gaps) >= 0.1 - 0.01


def test_by_default_the_step_is_one_frame_at_the_models_rate(make_plugin):
    plugin = make_plugin(fast=False)
    assert plugin._step_s() == pytest.approx(1 / 5)


def test_rich_after_frames_keep_their_colours(pixoo, make_plugin):
    plugin = make_plugin()
    after = message_to_grid("{red:HOT}", 10, 16, extended_markup=True)
    plugin.write_transition(cells(0), after, resolve_led_transition(None, MODEL), cancel=CancelToken())
    assert pixoo.pushes()[-1]["PicData"] == base64.b64encode(render_frame(after)).decode("ascii")


def test_the_configured_brightness_is_set_once_after_the_stream_lands(pixoo, make_plugin):
    plugin = make_plugin(brightness=40)
    plugin.write_transition(cells(1), cells(2), explicit("slide"), cancel=CancelToken())
    names = pixoo.names()
    assert names.count(BRIGHTNESS) == 1 and names[-1] == BRIGHTNESS
    assert levels(pixoo) == [40]


def test_a_long_stream_resets_the_id_counter_and_every_frame_still_shows(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.write_sequence(sequence(*range(1, 39)), cancel=CancelToken())
    assert pixoo.names().count(RESET) == 2  # the session's, then one after 32 pushes
    assert len(pixoo.displayed) == 38 and pixoo.ignored == []


def test_cancel_mid_stream_stops_before_the_next_frame(pixoo, make_plugin):
    plugin = make_plugin()
    event = threading.Event()

    def cancel_on_third_push(command, index):
        if command.get("Command") == SEND and len(pixoo.pushes()) == 3:
            event.set()

    pixoo.on_command = cancel_on_third_push
    result = plugin.write_sequence(sequence(1, 2, 3, 4, 5), cancel=CancelToken(event))
    assert (result.success, result.was_sent) == (True, False)
    assert len(pixoo.pushes()) == 3

    # Every frame sent was accepted whole, so the id session carries on.
    last_id = pixoo.pushes()[-1]["PicID"]
    pixoo.on_command = None
    pixoo.clear()
    plugin.write(frame(7), native=None, cancel=CancelToken())
    assert pixoo.names() == [SEND]
    assert pixoo.pushes()[0]["PicID"] == last_id + 1


def test_cancel_during_a_hold_returns_at_once(pixoo, make_plugin):
    plugin = make_plugin()
    event = threading.Event()
    pixoo.on_command = lambda command, index: event.set() if command.get("Command") == SEND else None
    started = time.monotonic()
    result = plugin.write_sequence(sequence(1, 2, ms=5000), cancel=CancelToken(event))
    assert time.monotonic() - started < 1.0
    assert (result.success, result.was_sent) == (True, False)
    assert len(pixoo.pushes()) == 1


def test_a_failure_mid_stream_is_a_failed_write(pixoo, make_plugin):
    plugin = make_plugin()

    def break_on_second_push(command, index):
        if command.get("Command") == SEND and len(pixoo.pushes()) == 2:
            pixoo.mode = "http_500"

    pixoo.on_command = break_on_second_push
    result = plugin.write_sequence(sequence(1, 2, 3), cancel=CancelToken())
    assert (result.success, result.was_sent) == (False, False)
    assert len(pixoo.pushes()) == 2


# --- fade -----------------------------------------------------------------------------------------


def test_a_fade_streams_dimmed_old_pixels_black_then_brightening_target(pixoo, make_plugin):
    plugin = make_plugin()
    result = plugin.write_transition(cells(1), cells(2), explicit("fade"), cancel=CancelToken())
    assert (result.success, result.was_sent) == (True, True)
    before, target = render_frame(frame(1)), render_frame(frame(2))
    expected = [bytes(v * level // 3 for v in before) for level in (2, 1, 0)]
    expected += [bytes(v * level // 3 for v in target) for level in (1, 2, 3)]
    assert [base64.b64decode(push["PicData"]) for push in pixoo.pushes()] == expected
    assert all(push["PicNum"] == 1 for push in pixoo.pushes())
    assert BRIGHTNESS not in pixoo.names() and GET_CONF not in pixoo.names()


def test_fade_steps_respect_the_models_stream_pacing(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.STREAM_STEP_S = 0.05
    stamps = push_times(pixoo)
    plugin.write_transition(cells(1), cells(2), explicit("fade"), cancel=CancelToken())
    assert len(stamps) == 6
    assert all(stamps[index] - stamps[index - 1] >= 0.05 - 0.01 for index in range(1, len(stamps)))


def test_a_fade_applies_the_boards_brightness_once_after_the_target(pixoo, make_plugin):
    plugin = make_plugin(brightness=80)
    plugin.write_transition(cells(1), cells(2), explicit("fade"), cancel=CancelToken())
    assert GET_CONF not in pixoo.names()
    assert levels(pixoo) == [80]
    assert pixoo.names()[-1] == BRIGHTNESS
    assert pixoo.pushes()[-1]["PicData"] == data(2)


def test_cancel_while_dimming_stops_before_the_next_pixel_frame(pixoo, make_plugin):
    plugin = make_plugin()
    event = threading.Event()

    def cancel_on_second_push(command, index):
        if command.get("Command") == SEND and len(pixoo.pushes()) == 2:
            event.set()

    pixoo.on_command = cancel_on_second_push
    token = CancelToken(event)
    with plugin.http.cancel_scope(token):
        result = plugin.write_transition(cells(1), cells(2), explicit("fade"), cancel=token)
    assert (result.success, result.was_sent) == (True, False)
    assert len(pixoo.pushes()) == 2
    assert levels(pixoo) == []


def test_cancel_after_the_target_lands_reports_sent_without_cleanup_requests(pixoo, make_plugin):
    plugin = make_plugin(brightness=40)
    event = threading.Event()

    def cancel_on_target(command, index):
        if command.get("Command") == SEND and len(pixoo.pushes()) == 6:
            event.set()

    pixoo.on_command = cancel_on_target
    token = CancelToken(event)
    with plugin.http.cancel_scope(token):
        result = plugin.write_transition(cells(1), cells(2), explicit("fade"), cancel=token)
    assert (result.success, result.was_sent) == (True, True)
    assert len(pixoo.pushes()) == 6 and pixoo.pushes()[-1]["PicData"] == data(2)
    assert levels(pixoo) == []
    pixoo.on_command = None
    plugin.write(frame(3), native=None, cancel=CancelToken())
    assert levels(pixoo) == [40]


def test_a_fade_cancelled_before_it_starts_leaves_the_device_alone(pixoo, make_plugin):
    plugin = make_plugin()
    event = threading.Event()
    event.set()
    token = CancelToken(event)
    with plugin.http.cancel_scope(token):
        result = plugin.write_transition(cells(1), cells(2), explicit("fade"), cancel=token)
    assert (result.success, result.was_sent) == (True, False)
    assert pixoo.commands == []


def test_a_failed_fade_is_a_failed_write_without_a_persistent_brightness_change(pixoo, make_plugin):
    plugin = make_plugin()

    def fail_second_push(command, index):
        if command.get("Command") == SEND and len(pixoo.pushes()) == 2:
            pixoo.mode = "http_500"

    pixoo.on_command = fail_second_push
    result = plugin.write_transition(cells(1), cells(2), explicit("fade"), cancel=CancelToken())
    assert (result.success, result.was_sent) == (False, False)
    assert levels(pixoo) == []
    assert len(pixoo.pushes()) == 2


def test_a_failed_restore_is_retried_after_the_next_frame(pixoo, make_plugin):
    plugin = make_plugin(brightness=50)

    def fail_the_restore(command, index):
        pixoo.mode = "http_500" if command.get("Command") == BRIGHTNESS and command["Brightness"] == 50 else "ok"

    pixoo.on_command = fail_the_restore
    plugin.write_transition(cells(1), cells(2), explicit("fade"), cancel=CancelToken())
    pixoo.on_command = None
    pixoo.mode = "ok"
    pixoo.clear()
    plugin.write(frame(3), native=None, cancel=CancelToken())
    assert levels(pixoo) == [50]


def test_fade_style_blend_cross_fades_the_pixels_instead(pixoo, make_plugin):
    plugin = make_plugin(fade_style="blend")
    result = plugin.write_transition(cells(1), cells(2), explicit("fade"), cancel=CancelToken())
    assert result.success
    assert BRIGHTNESS not in pixoo.names()
    assert len(pixoo.pushes()) >= 2 and pixoo.pushes()[-1]["PicData"] == data(2)


# --- explicit sequences ----------------------------------------------------------------------------


def test_a_sequence_streams_its_frames_in_order_as_single_frame_pushes(pixoo, make_plugin):
    plugin = make_plugin()
    result = plugin.write_sequence(sequence(1, 5, 2), cancel=CancelToken())
    assert (result.success, result.was_sent) == (True, True)
    assert [(c["PicNum"], c["PicData"]) for c in pixoo.pushes()] == [(1, data(1)), (1, data(5)), (1, data(2))]
    assert pixoo.ignored == []


def test_each_sequence_frame_is_held_its_own_duration(pixoo, make_plugin):
    plugin = make_plugin()
    stamps = push_times(pixoo)
    plugin.write_sequence(
        [TimedFrame(frame(1), 300), TimedFrame(frame(2), 0), TimedFrame(frame(3), 0)], cancel=CancelToken()
    )
    assert stamps[1] - stamps[0] >= 0.3 - 0.01
    assert stamps[2] - stamps[1] < 0.2


def test_no_sequence_frame_is_held_shorter_than_one_stream_step(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.STREAM_STEP_S = 0.15
    stamps = push_times(pixoo)
    plugin.write_sequence(sequence(1, 2, ms=10), cancel=CancelToken())
    assert stamps[1] - stamps[0] >= 0.15 - 0.01


def test_a_sequence_longer_than_40_frames_is_compressed_and_keeps_its_ends(pixoo, make_plugin):
    plugin = make_plugin()
    codes = [1 + (i % 60) for i in range(70)]
    plugin.write_sequence(sequence(*codes), cancel=CancelToken())
    pushes = pixoo.pushes()
    assert SEQUENCE_MAX_FRAMES == 40
    assert len(pushes) == 40
    assert pushes[0]["PicData"] == data(codes[0])
    assert pushes[-1]["PicData"] == data(codes[-1])


def test_a_one_frame_sequence_is_one_still_frame(pixoo, make_plugin):
    plugin = make_plugin()
    result = plugin.write_sequence(sequence(4), cancel=CancelToken())
    assert result.success
    assert [(c["PicNum"], c["PicData"]) for c in pixoo.pushes()] == [(1, data(4))]


def test_an_empty_sequence_sends_nothing(pixoo, make_plugin):
    plugin = make_plugin()
    result = plugin.write_sequence([], cancel=CancelToken())
    assert (result.success, result.was_sent) == (True, False)
    assert pixoo.commands == []
