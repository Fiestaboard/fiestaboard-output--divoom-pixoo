"""Animations: write_transition (core's resolved LED transition) and
write_sequence (a transition plugin's frames), each one GIF upload then
the target as a still frame."""

from __future__ import annotations

import base64
import threading
import time

from src.markup import message_to_grid
from src.outputs.board_profile import model_character_set
from src.outputs.cells import cells_from_codes
from src.outputs.plugin_base import CancelToken, TimedFrame
from src.plugins import (
    LedLayoutOptions,
    layout_message,
    led_spec_for_model,
    plan_transition,
    resolve_led_transition,
    transition_frames,
)

from .conftest import MANIFEST
from .conftest import render as render_frame

RESET = "Draw/ResetHttpGifId"
SEND = "Draw/SendHttpGif"
MODEL = MANIFEST.output.model(0)


def frame(code: int) -> list[list[int]]:
    return [[code] * 16 for _ in range(10)]


def cells(code: int):
    return cells_from_codes(frame(code))


def b64(pixels: bytes) -> str:
    return base64.b64encode(pixels).decode("ascii")


def data(code: int) -> str:
    return b64(render_frame(frame(code)))


def sequence(*codes: int, ms: int = 100) -> list[TimedFrame]:
    return [TimedFrame(frame(c), ms) for c in codes]


def animation(pixoo) -> list[dict]:
    return [c for c in pixoo.pushes() if c["PicNum"] > 1]


def expected_frames(before, after, transition) -> list[str]:
    """What FiestaUI previews for this change (core's planner), as PicData."""
    spec = led_spec_for_model(MODEL)
    options = LedLayoutOptions(charset=model_character_set(MODEL))
    planned = plan_transition(layout_message(before, spec, options), layout_message(after, spec, options), transition.spec)
    return [b64(f.pixels) for f in transition_frames(planned)]


# --- write_transition ------------------------------------------------------------------------


def test_the_default_flip_uploads_exactly_core_planned_frames_after_the_first(pixoo, make_plugin):
    plugin = make_plugin()
    transition = resolve_led_transition(None, MODEL)
    result = plugin.write_transition(cells(1), cells(2), transition, cancel=CancelToken())

    assert (result.success, result.was_sent) == (True, True)
    frames = animation(pixoo)
    # Frame 0 is what the board already shows: not re-sent.
    assert [c["PicData"] for c in frames] == expected_frames(cells(1), cells(2), transition)[1:]
    assert frames[-1]["PicData"] == data(2)
    assert pixoo.names()[0] == RESET
    n = len(frames)
    assert 2 <= n <= 32
    assert {c["PicID"] for c in frames} == {1}
    assert [c["PicOffset"] for c in frames] == list(range(n))
    assert all(c["PicNum"] == n and c["PicWidth"] == 64 and c["PicSpeed"] == 80 for c in frames)


def test_an_explicit_transition_the_device_can_run_is_rendered_as_resolved(pixoo, make_plugin):
    plugin = make_plugin()
    transition = resolve_led_transition("dissolve", MODEL)
    assert transition.id == "dissolve"
    plugin.write_transition(cells(1), cells(2), transition, cancel=CancelToken())
    expected = expected_frames(cells(1), cells(2), transition)[1:]
    assert [c["PicData"] for c in animation(pixoo)] == expected
    assert len(expected) <= 32


def test_rich_before_and_after_frames_keep_their_colours(pixoo, make_plugin):
    plugin = make_plugin()
    after = message_to_grid("{red:HOT}", 10, 16, extended_markup=True)
    plugin.write_transition(cells(0), after, resolve_led_transition(None, MODEL), cancel=CancelToken())
    assert pixoo.pushes()[-1]["PicData"] == b64(render_frame(after))


def test_after_the_animation_the_target_is_pushed_as_a_still_frame(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.write_transition(cells(1), cells(2), resolve_led_transition(None, MODEL), cancel=CancelToken())
    last = pixoo.pushes()[-1]
    assert last["PicNum"] == 1
    assert last["PicData"] == data(2)
    assert last["PicID"] == animation(pixoo)[0]["PicID"] + 1


def test_cancel_mid_upload_stops_before_the_next_frame(pixoo, make_plugin):
    plugin = make_plugin()
    event = threading.Event()

    def cancel_on_third_push(command, index):
        if command.get("Command") == SEND and len(pixoo.pushes()) == 3:
            event.set()

    pixoo.on_command = cancel_on_third_push
    result = plugin.write_transition(cells(1), cells(2), resolve_led_transition(None, MODEL), cancel=CancelToken(event))
    assert (result.success, result.was_sent) == (True, False)
    assert len(pixoo.pushes()) == 3

    # The device holds a half-uploaded GIF: the next write starts on a fresh id.
    pixoo.on_command = None
    pixoo.clear()
    plugin.write(frame(7), native=None, cancel=CancelToken())
    assert pixoo.names() == [RESET, SEND]


def test_a_failure_mid_upload_is_a_failed_write(pixoo, make_plugin):
    plugin = make_plugin()

    def break_on_second_push(command, index):
        if command.get("Command") == SEND and len(pixoo.pushes()) == 2:
            pixoo.mode = "http_500"

    pixoo.on_command = break_on_second_push
    result = plugin.write_transition(cells(1), cells(2), resolve_led_transition(None, MODEL), cancel=CancelToken())
    assert (result.success, result.was_sent) == (False, False)


def test_frames_are_paced_and_the_pacing_wait_honours_cancel(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.FRAME_GAP_S = 0.05
    transition = resolve_led_transition(None, MODEL)
    started = time.monotonic()
    plugin.write_transition(cells(1), cells(2), transition, cancel=CancelToken())
    n = len(animation(pixoo))
    assert time.monotonic() - started >= (n - 1) * 0.05

    plugin.FRAME_GAP_S = 5.0
    event = threading.Event()
    pixoo.on_command = lambda command, index: event.set()  # cancelled while the next POST waits
    started = time.monotonic()
    result = plugin.write_transition(cells(3), cells(4), transition, cancel=CancelToken(event))
    assert time.monotonic() - started < 1.0
    assert (result.success, result.was_sent) == (True, False)


def test_the_still_frame_waits_out_the_animation_and_the_loading_overlay(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.LOADING_OVERLAY_S = 0.3
    stamps: list[tuple[int, float]] = []
    pixoo.on_command = lambda command, index: stamps.append((command.get("PicNum", 0), time.monotonic()))
    plugin.write_transition(cells(1), cells(2), resolve_led_transition(None, MODEL), cancel=CancelToken())
    last_animation = max(t for num, t in stamps if num > 1)
    still = [t for num, t in stamps if num == 1][-1]
    n = len(animation(pixoo))
    assert still - last_animation >= 0.3 + n * 0.08 - 0.01


def test_cancel_while_the_animation_plays_skips_the_still_frame(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.LOADING_OVERLAY_S = 5.0
    event = threading.Event()

    def cancel_after_last_frame(command, index):
        if command.get("Command") == SEND and command["PicOffset"] == command["PicNum"] - 1:
            event.set()

    pixoo.on_command = cancel_after_last_frame
    started = time.monotonic()
    result = plugin.write_transition(cells(1), cells(2), resolve_led_transition(None, MODEL), cancel=CancelToken(event))
    assert time.monotonic() - started < 2.0
    assert (result.success, result.was_sent) == (True, False)
    assert all(c["PicNum"] > 1 for c in pixoo.pushes())

    pixoo.on_command = None
    pixoo.clear()
    plugin.write(frame(7), native=None, cancel=CancelToken())
    assert pixoo.names() == [RESET, SEND]


# --- write_sequence (a transition plugin's frames) ----------------------------------------------


def test_a_sequence_uploads_core_frames_as_given_then_the_target(pixoo, make_plugin):
    plugin = make_plugin()
    result = plugin.write_sequence(sequence(1, 5, 2), cancel=CancelToken())
    assert (result.success, result.was_sent) == (True, True)
    assert [c["PicData"] for c in animation(pixoo)] == [data(1), data(5), data(2)]
    assert [(c["PicNum"], c["PicData"]) for c in pixoo.pushes()][-1] == (1, data(2))


def test_sequence_frames_keep_their_durations_but_never_below_80_ms(pixoo, make_plugin):
    plugin = make_plugin()
    frames = [TimedFrame(frame(1), 40), TimedFrame(frame(2), 250)]
    plugin.write_sequence(frames, cancel=CancelToken())
    assert [c["PicSpeed"] for c in animation(pixoo)] == [80, 250]


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
