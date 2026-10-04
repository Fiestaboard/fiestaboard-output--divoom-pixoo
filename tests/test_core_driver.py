"""Core's driver adapter in front of the plugin, against the mock device.

The plugin never spaces writes itself; core's floor (``min_interval_ms``
1000, keyed by ``device_key()``) does. This drives the plugin the way a
board does and checks the device sees exactly what core lets through.
"""

from __future__ import annotations

from src.outputs.plugin_base import TimedFrame
from src.outputs.plugin_driver import OutputPluginDriver


def frame(code: int) -> list[list[int]]:
    return [[code] * 16 for _ in range(10)]


def test_core_keeps_the_one_second_floor_between_writes(pixoo, make_plugin):
    now = [10_000.0]
    driver = OutputPluginDriver(make_plugin(), clock=lambda: now[0])

    first = driver.send_characters(frame(1), with_outcome=True)
    assert (first.success, first.was_sent) == (True, True)

    now[0] += 0.5
    second = driver.send_characters(frame(2), with_outcome=True)
    assert second.throttled and not second.was_sent
    assert len(pixoo.pushes()) == 1

    now[0] += 0.6
    third = driver.write_sequence([TimedFrame(frame(1), 100), TimedFrame(frame(3), 100)])
    assert (third.success, third.was_sent) == (True, True)
    assert pixoo.pushes()[-1]["PicNum"] == 1  # the still target after the animation


def test_a_dead_device_is_a_failed_write_core_can_report(pixoo, make_plugin):
    pixoo.mode = "http_500"
    driver = OutputPluginDriver(make_plugin())
    result = driver.send_characters(frame(1), with_outcome=True)
    assert (result.success, result.was_sent) == (False, False)
    assert driver.last_write_error


def test_core_snaps_a_page_change_to_one_still_push(pixoo, make_plugin):
    now = [20_000.0]
    driver = OutputPluginDriver(make_plugin(), clock=lambda: now[0])
    assert driver.takes_transitions
    driver.send_characters(frame(1))
    now[0] += 2.0
    pixoo.clear()

    result = driver.render(frame(2), with_outcome=True)

    assert (result.success, result.was_sent) == (True, True)
    # The Pixoo model resolves no LED transition: one single-frame push, no GIF.
    assert [c["PicNum"] for c in pixoo.pushes()] == [1]
