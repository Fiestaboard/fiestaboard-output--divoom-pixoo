"""A transient fade cannot alter persistent device brightness."""
import base64
import threading

import pytest
from src.outputs.plugin_base import CancelToken

from .test_sequence import BRIGHTNESS, SEND, cells, data, explicit, frame


@pytest.mark.parametrize("brightness", [None, 0, 35])
@pytest.mark.parametrize("replace_instance", [False, True])
def test_cancelled_fade_preserves_brightness_and_stops_requests(pixoo, make_plugin, brightness, replace_instance):
    config = {} if brightness is None else {"brightness": brightness}
    plugin = make_plugin(**config)
    device_level = [60]
    event = threading.Event()
    counts_at_cancel = []

    def track(command, index):
        if command.get("Command") == BRIGHTNESS:
            device_level[0] = command["Brightness"]

    pixoo.on_command = track
    assert plugin.write(frame(1), native=None, cancel=CancelToken()).was_sent
    original_level = device_level[0]

    def cancel_first_change(command, index):
        track(command, index)
        if command.get("Command") in (BRIGHTNESS, SEND) and not event.is_set():
            counts_at_cancel.append(len(pixoo.commands))
            event.set()

    pixoo.on_command = cancel_first_change
    token = CancelToken(event)
    with plugin.http.cancel_scope(token):
        plugin.write_transition(cells(1), cells(2), explicit("fade"), cancel=token)
    assert len(pixoo.commands) == counts_at_cancel[0], "cancelled fade started another request"
    assert device_level[0] == original_level, "transient fade changed persistent brightness"
    pixoo.on_command = track
    if replace_instance:
        plugin.http.close()
        plugin.close()
        plugin = make_plugin(**config)
    assert plugin.write(frame(3), native=None, cancel=CancelToken()).was_sent
    assert pixoo.pushes()[-1]["PicData"] == data(3)
    assert device_level[0] == original_level


def test_through_black_fade_never_changes_persistent_brightness(pixoo, make_plugin):
    plugin = make_plugin()
    result = plugin.write_transition(cells(1), cells(2), explicit("fade"), cancel=CancelToken())
    assert result.success and result.was_sent
    assert BRIGHTNESS not in pixoo.names(), "fade must not leave a persistent dim level after shutdown or replacement"
    pixels = [base64.b64decode(push["PicData"]) for push in pixoo.pushes()]
    assert bytes(64 * 64 * 3) in pixels
    assert pixoo.pushes()[-1]["PicData"] == data(2)
