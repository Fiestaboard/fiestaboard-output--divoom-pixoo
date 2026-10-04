"""Changes of page: the Pixoo snaps (single-frame push); an explicit sequence
is one looping GIF, capped at 40 frames, then the target as a still."""

from __future__ import annotations

import base64
import threading
import time

from plugins.divoom_pixoo import SEQUENCE_MAX_FRAMES

from src.led.transitions import LedTransitionSpec
from src.markup import message_to_grid
from src.outputs.cells import cells_from_codes
from src.outputs.plugin_base import CancelToken, TimedFrame
from src.plugins import ResolvedLedTransition, resolve_led_transition

from .conftest import MANIFEST
from .conftest import render as render_frame

RESET = "Draw/ResetHttpGifId"
SEND = "Draw/SendHttpGif"
MODEL = MANIFEST.output.model(0)


def frame(code: int) -> list[list[int]]:
    return [[code] * 16 for _ in range(10)]


def cells(code: int):
    return cells_from_codes(frame(code))


def data(code: int) -> str:
    return base64.b64encode(render_frame(frame(code))).decode("ascii")


def sequence(*codes: int, ms: int = 100) -> list[TimedFrame]:
    return [TimedFrame(frame(c), ms) for c in codes]


def animation(pixoo) -> list[dict]:
    return [c for c in pixoo.pushes() if c["PicNum"] > 1]


FLIP = ResolvedLedTransition("flip", LedTransitionSpec("flip", step_ms=80, half_flap=False, max_frames=32), "explicit")


# --- the snap path (what FiestaBoard resolves for the Pixoo) -------------------------------------


def test_the_pixoo_model_resolves_to_no_transition():
    assert resolve_led_transition(None, MODEL).spec == "none"
    assert resolve_led_transition("flip", MODEL).spec == "none"
    assert MANIFEST.output.capabilities.animation == "stream"


def test_a_none_transition_pushes_only_the_final_still(pixoo, make_plugin):
    plugin = make_plugin()
    result = plugin.write_transition(cells(1), cells(2), resolve_led_transition(None, MODEL), cancel=CancelToken())
    assert (result.success, result.was_sent) == (True, True)
    assert [(c["PicNum"], c["PicData"]) for c in pixoo.pushes()] == [(1, data(2))]


def test_rich_after_frames_keep_their_colours_when_snapping(pixoo, make_plugin):
    plugin = make_plugin()
    after = message_to_grid("{red:HOT}", 10, 16, extended_markup=True)
    plugin.write_transition(cells(0), after, resolve_led_transition(None, MODEL), cancel=CancelToken())
    assert pixoo.pushes()[-1]["PicData"] == base64.b64encode(render_frame(after)).decode("ascii")


# --- the explicit sequence path ------------------------------------------------------------------


def test_a_sequence_uploads_frames_back_to_back_as_one_gif_then_the_target(pixoo, make_plugin):
    plugin = make_plugin(fast=False)
    plugin.ANIMATION_READY_S = 0.0
    stamps: list[float] = []
    pixoo.on_command = lambda command, index: stamps.append(time.monotonic()) if command.get("PicNum", 1) > 1 else None
    result = plugin.write_sequence(sequence(1, 5, 2), cancel=CancelToken())

    assert (result.success, result.was_sent) == (True, True)
    frames = animation(pixoo)
    assert [c["PicData"] for c in frames] == [data(1), data(5), data(2)]
    assert {c["PicID"] for c in frames} == {1}
    assert [c["PicOffset"] for c in frames] == [0, 1, 2]
    assert all(c["PicNum"] == 3 and c["PicWidth"] == 64 for c in frames)
    # Back-to-back: no pacing gap between frame POSTs (lab: a shorter upload is a shorter overlay).
    assert stamps[-1] - stamps[0] < 0.5
    last = pixoo.pushes()[-1]
    assert (last["PicNum"], last["PicData"], last["PicID"]) == (1, data(2), 2)
    assert pixoo.ignored == []


def test_a_sequence_longer_than_40_frames_is_compressed_and_still_ends_on_the_target(pixoo, make_plugin):
    plugin = make_plugin()
    codes = [1 + (i % 60) for i in range(70)]
    plugin.write_sequence(sequence(*codes), cancel=CancelToken())
    frames = animation(pixoo)
    assert SEQUENCE_MAX_FRAMES == 40
    assert len(frames) == 40 and frames[0]["PicNum"] == 40
    assert frames[0]["PicData"] == data(codes[0])
    assert frames[-1]["PicData"] == data(codes[-1])


def test_sequence_frames_keep_their_durations_but_never_below_80_ms(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.write_sequence([TimedFrame(frame(1), 40), TimedFrame(frame(2), 250)], cancel=CancelToken())
    assert [c["PicSpeed"] for c in animation(pixoo)] == [80, 250]


def test_the_still_waits_for_the_upload_to_be_ready_and_one_play_through(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.ANIMATION_READY_S = 0.3
    stamps: list[tuple[int, float]] = []
    pixoo.on_command = lambda command, index: stamps.append((command.get("PicNum", 0), time.monotonic()))
    plugin.write_sequence(sequence(1, 2, ms=200), cancel=CancelToken())
    last_animation = max(t for num, t in stamps if num > 1)
    still = [t for num, t in stamps if num == 1][-1]
    assert still - last_animation >= 0.3 + 0.4 - 0.01


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


def test_an_explicit_transition_spec_is_planned_and_uploaded_as_a_sequence(pixoo, make_plugin):
    plugin = make_plugin()
    result = plugin.write_transition(cells(1), cells(2), FLIP, cancel=CancelToken())
    assert result.success
    frames = animation(pixoo)
    assert 2 <= len(frames) <= SEQUENCE_MAX_FRAMES
    assert all(c["PicSpeed"] == 80 for c in frames)
    assert frames[-1]["PicData"] == data(2)
    assert pixoo.pushes()[-1]["PicNum"] == 1


def test_cancel_mid_upload_stops_before_the_next_frame(pixoo, make_plugin):
    plugin = make_plugin()
    event = threading.Event()

    def cancel_on_third_push(command, index):
        if command.get("Command") == SEND and len(pixoo.pushes()) == 3:
            event.set()

    pixoo.on_command = cancel_on_third_push
    result = plugin.write_sequence(sequence(1, 2, 3, 4, 5), cancel=CancelToken(event))
    assert (result.success, result.was_sent) == (True, False)
    assert len(pixoo.pushes()) == 3

    # The device's counter is now unknown: the next write starts a new session.
    pixoo.on_command = None
    pixoo.clear()
    plugin.write(frame(7), native=None, cancel=CancelToken())
    assert pixoo.names() == [RESET, "Draw/GetHttpGifId", SEND]


def test_a_failure_mid_upload_is_a_failed_write(pixoo, make_plugin):
    plugin = make_plugin()

    def break_on_second_push(command, index):
        if command.get("Command") == SEND and len(pixoo.pushes()) == 2:
            pixoo.mode = "http_500"

    pixoo.on_command = break_on_second_push
    result = plugin.write_sequence(sequence(1, 2, 3), cancel=CancelToken())
    assert (result.success, result.was_sent) == (False, False)


def test_cancel_while_the_animation_plays_skips_the_still_frame(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.ANIMATION_READY_S = 5.0
    event = threading.Event()

    def cancel_after_last_frame(command, index):
        if command.get("Command") == SEND and command["PicOffset"] == command["PicNum"] - 1:
            event.set()

    pixoo.on_command = cancel_after_last_frame
    started = time.monotonic()
    result = plugin.write_sequence(sequence(1, 2), cancel=CancelToken(event))
    assert time.monotonic() - started < 2.0
    assert (result.success, result.was_sent) == (True, False)
    assert all(c["PicNum"] > 1 for c in pixoo.pushes())
