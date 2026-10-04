"""write_sequence: one multi-frame upload of the LED flip, then the target."""

from __future__ import annotations

import base64
import threading
import time

from plugins.divoom_pixoo import render_frame

from src.outputs.plugin_base import CancelToken, TimedFrame

RESET = "Draw/ResetHttpGifId"
SEND = "Draw/SendHttpGif"


def frame(code: int) -> list[list[int]]:
    return [[code] * 16 for _ in range(10)]


def data(code: int) -> str:
    return base64.b64encode(render_frame(frame(code))).decode("ascii")


def sequence(*codes: int) -> list[TimedFrame]:
    return [TimedFrame(frame(c), 100) for c in codes]


def animation(pixoo) -> list[dict]:
    return [c for c in pixoo.pushes() if c["PicNum"] > 1]


def test_a_sequence_uploads_one_gif_with_one_pic_id_and_ordered_offsets(pixoo, make_plugin):
    plugin = make_plugin()
    result = plugin.write_sequence(sequence(1, 5, 2), cancel=CancelToken())

    assert (result.success, result.was_sent) == (True, True)
    frames = animation(pixoo)
    n = len(frames)
    assert 2 <= n <= 32
    assert pixoo.names()[0] == RESET
    assert {c["PicID"] for c in frames} == {1}
    assert [c["PicOffset"] for c in frames] == list(range(n))
    assert all(c["PicNum"] == n and c["PicWidth"] == 64 for c in frames)
    assert all(c["PicSpeed"] >= 80 for c in frames)


def test_the_animation_starts_on_the_first_frame_and_lands_on_the_target(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.write_sequence(sequence(1, 5, 2), cancel=CancelToken())
    frames = animation(pixoo)
    assert frames[0]["PicData"] == data(1)
    assert frames[-1]["PicData"] == data(2)
    # The core-side intermediate grid is not rasterised: the plugin runs
    # FiestaBoard's LED flip between the two ends instead.
    assert all(c["PicData"] != data(5) for c in frames)


def test_after_the_animation_the_target_is_pushed_as_a_still_frame(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.write_sequence(sequence(1, 2), cancel=CancelToken())
    last = pixoo.pushes()[-1]
    assert last["PicNum"] == 1
    assert last["PicData"] == data(2)
    assert last["PicID"] == animation(pixoo)[0]["PicID"] + 1


def test_the_flip_starts_from_what_the_device_already_shows(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.write(frame(3), native=None, cancel=CancelToken())
    pixoo.clear()
    plugin.write_sequence(sequence(1, 2), cancel=CancelToken())
    frames = animation(pixoo)
    # Frame 0 of the flip is the frame already on the device: not re-sent.
    assert all(c["PicData"] != data(1) for c in frames)
    assert all(c["PicData"] != data(3) for c in frames)
    assert frames[-1]["PicData"] == data(2)
    assert pixoo.names()[0] == RESET  # every animation starts on a fresh id


def test_a_whole_board_change_stays_inside_the_32_frame_budget(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.write_sequence(sequence(0, 36), cancel=CancelToken())
    assert len(animation(pixoo)) <= 32


def test_an_unchanged_target_is_one_still_frame(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.write(frame(4), native=None, cancel=CancelToken())
    pixoo.clear()
    result = plugin.write_sequence(sequence(9, 4), cancel=CancelToken())
    assert result.success
    assert [(c["PicNum"], c["PicData"]) for c in pixoo.pushes()] == [(1, data(4))]


def test_an_empty_sequence_sends_nothing(pixoo, make_plugin):
    plugin = make_plugin()
    result = plugin.write_sequence([], cancel=CancelToken())
    assert (result.success, result.was_sent) == (True, False)
    assert pixoo.commands == []


def test_cancel_mid_upload_stops_before_the_next_frame(pixoo, make_plugin):
    plugin = make_plugin()
    event = threading.Event()

    def cancel_on_third_push(command, index):
        if command.get("Command") == SEND and len(pixoo.pushes()) == 3:
            event.set()

    pixoo.on_command = cancel_on_third_push
    result = plugin.write_sequence(sequence(1, 2), cancel=CancelToken(event))
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
    result = plugin.write_sequence(sequence(1, 2), cancel=CancelToken())
    assert (result.success, result.was_sent) == (False, False)


def test_frames_are_paced_and_the_pacing_wait_honours_cancel(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.FRAME_GAP_S = 0.05
    started = time.monotonic()
    plugin.write_sequence(sequence(1, 2), cancel=CancelToken())
    n = len(animation(pixoo))
    assert time.monotonic() - started >= (n - 1) * 0.05

    plugin.FRAME_GAP_S = 5.0
    event = threading.Event()
    pixoo.on_command = lambda command, index: event.set()  # cancelled while the next POST waits
    started = time.monotonic()
    result = plugin.write_sequence(sequence(3, 4), cancel=CancelToken(event))
    assert time.monotonic() - started < 1.0
    assert (result.success, result.was_sent) == (True, False)


def test_the_still_frame_waits_out_the_animation_and_the_loading_overlay(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.LOADING_OVERLAY_S = 0.3
    stamps: list[tuple[int, float]] = []
    pixoo.on_command = lambda command, index: stamps.append((command.get("PicNum", 0), time.monotonic()))
    plugin.write_sequence(sequence(1, 2), cancel=CancelToken())
    last_animation = max(t for num, t in stamps if num > 1)
    still = [t for num, t in stamps if num == 1][-1]
    n = len(animation(pixoo))
    assert still - last_animation >= 0.3 + n * 0.08 - 0.01


def test_cancel_while_the_animation_plays_skips_the_still_frame(pixoo, make_plugin):
    plugin = make_plugin()
    plugin.LOADING_OVERLAY_S = 5.0
    event = threading.Event()
    pushes = []

    def cancel_after_last_frame(command, index):
        if command.get("Command") == SEND:
            pushes.append(command)
            if command["PicOffset"] == command["PicNum"] - 1:
                event.set()

    pixoo.on_command = cancel_after_last_frame
    started = time.monotonic()
    result = plugin.write_sequence(sequence(1, 2), cancel=CancelToken(event))
    assert time.monotonic() - started < 2.0
    assert (result.success, result.was_sent) == (True, False)
    assert all(c["PicNum"] > 1 for c in pixoo.pushes())

    # The device is still looping that GIF: the next write starts on a fresh id.
    pixoo.on_command = None
    pixoo.clear()
    plugin.write(frame(7), native=None, cancel=CancelToken())
    assert pixoo.names() == [RESET, SEND]
