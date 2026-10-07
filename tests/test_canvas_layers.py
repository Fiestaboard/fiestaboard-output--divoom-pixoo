"""Pixel canvases: a page's canvases reach the Pixoo as bitmap layers.

Core (FiestaBoard with canvas pages) hands a rich frame that is a list
subclass carrying ``.layers`` — a tuple of ``CanvasLayer(x, y, w, h, rgba)``,
``()`` when the page has none. ``layout_message(..., layers=...)`` draws them
as bitmap ops after the cells, and core's transition planner treats layouts
with layers itself (per-cell kinds switch layers at the half-way frame).

Older cores have neither the attribute nor the keyword: the plugin reads
``getattr(frame, "layers", ())`` and only passes ``layers=`` when there are
some and the ``layout_message`` it got accepts them.
"""

from __future__ import annotations

import base64
import inspect
from dataclasses import dataclass

import pytest
import plugins.divoom_pixoo as pixoo_module

from src.led import layout_message, led_spec_for_model, rasterize
from src.led.transitions import LedTransitionSpec
from src.markup import message_to_grid
from src.outputs.plugin_base import CancelToken, TimedFrame
from src.plugins import ResolvedLedTransition

from .conftest import build

SEND = "Draw/SendHttpGif"
COLOR = (12, 34, 56)

canvases = pytest.mark.skipif(
    "layers" not in inspect.signature(layout_message).parameters,
    reason="this FiestaBoard predates pixel canvases",
)


try:
    from src.canvas.layer import CanvasLayer
except ImportError:  # the canvas engine needs ``regex``, which a bare test image may lack

    @dataclass(frozen=True)
    class CanvasLayer:  # type: ignore[no-redef]
        """Shaped like ``src.canvas.CanvasLayer``: core's layout reads these fields."""

        x: int
        y: int
        w: int
        h: int
        rgba: bytes


def rows(text: str):
    return message_to_grid(text, 10, 16, extended_markup=True, preserve_case=True)


def canvas_frame(text: str = "HI", *, x: int = 0, y: int = 0, w: int = 4, h: int = 4):
    """*text* as a rich frame with one opaque :data:`COLOR` canvas at (x, y)."""
    from src.outputs.cells import RichCells

    layer = CanvasLayer(x=x, y=y, w=w, h=h, rgba=bytes([*COLOR, 255]) * (w * h))
    return RichCells(rows(text), (layer,))


def pixel(pixels: bytes, x: int, y: int) -> tuple[int, int, int]:
    i = (y * 64 + x) * 3
    return tuple(pixels[i : i + 3])


def pushed(pixoo) -> list[bytes]:
    return [base64.b64decode(c["PicData"]) for c in pixoo.commands if c.get("Command") == SEND]


def expected(plugin, frame) -> bytes:
    """What core itself draws for *frame*, layers included."""
    spec, options = led_spec_for_model(plugin.device_model), plugin.led_layout_options()
    return rasterize(layout_message(frame, spec, options, layers=frame.layers)).pixels


def explicit(kind: str, **spec) -> ResolvedLedTransition:
    return ResolvedLedTransition(kind, LedTransitionSpec(kind, **spec), "explicit")


# --- one still ---------------------------------------------------------------------------------


@canvases
def test_write_cells_draws_the_frames_canvas_into_the_pushed_image(pixoo, make_plugin):
    plugin = make_plugin()
    frame = canvas_frame(x=60, y=60)
    result = plugin.write_cells(frame, native=None, cancel=CancelToken())
    assert (result.success, result.was_sent) == (True, True)
    image = pushed(pixoo)[-1]
    assert image == expected(plugin, frame)
    assert {pixel(image, x, y) for x in range(60, 64) for y in range(60, 64)} == {COLOR}


@canvases
def test_a_canvas_paints_over_the_text_under_it(make_plugin):
    plugin = make_plugin()
    covered = canvas_frame("HHHH", w=64, h=8)
    assert plugin.render(covered) != plugin.render(rows("HHHH"))
    assert {pixel(plugin.render(covered), x, y) for x in range(64) for y in range(8)} == {COLOR}


@canvases
def test_a_frame_with_no_canvases_draws_byte_for_byte_what_it_did_before(make_plugin):
    from src.outputs.cells import RichCells

    plugin = make_plugin()
    plain = rows("HELLO")
    assert plugin.render(RichCells(plain, ())) == plugin.render(plain)
    spec, options = led_spec_for_model(plugin.device_model), plugin.led_layout_options()
    assert plugin.layout(plain) == layout_message(plain, spec, options)


# --- changes of page ---------------------------------------------------------------------------


@canvases
@pytest.mark.parametrize("kind", ["flip", "slide", "wipe", "cascade", "dissolve"])
def test_a_transition_lands_on_the_new_pages_canvas(pixoo, make_plugin, kind):
    plugin = make_plugin()
    after = canvas_frame("NEW", x=40, y=40)
    result = plugin.write_transition(rows("OLD"), after, explicit(kind), cancel=CancelToken())
    assert (result.success, result.was_sent) == (True, True)
    frames = pushed(pixoo)
    assert frames[-1] == expected(plugin, after)
    assert pixel(frames[-1], 41, 41) == COLOR


@canvases
def test_a_transition_away_from_a_canvas_starts_from_it_and_drops_it(pixoo, make_plugin):
    plugin = make_plugin()
    before = canvas_frame("OLD", x=40, y=40)
    plugin.write_transition(before, rows("NEW"), explicit("flip"), cancel=CancelToken())
    frames = pushed(pixoo)
    # Core's half-switch rule: the first half-turned flaps still carry the old canvas.
    assert pixel(frames[0], 41, 41) == COLOR
    assert pixel(frames[-1], 41, 41) != COLOR
    assert frames[-1] == plugin.render(rows("NEW"))


@canvases
def test_a_fade_through_black_swaps_in_the_new_pages_canvas(pixoo, make_plugin):
    plugin = make_plugin(brightness=80)
    after = canvas_frame("NEW", x=8, y=8)
    result = plugin.write_transition(rows("OLD"), after, explicit("fade"), cancel=CancelToken())
    assert (result.success, result.was_sent) == (True, True)
    assert pushed(pixoo) == [expected(plugin, after)]


@canvases
def test_a_snap_pushes_the_new_pages_canvas(pixoo, make_plugin):
    plugin = make_plugin()
    after = canvas_frame(x=8, y=8)
    plugin.write_transition(rows("OLD"), after, ResolvedLedTransition("none", "none", "explicit"), cancel=CancelToken())
    assert pushed(pixoo) == [expected(plugin, after)]


@canvases
def test_a_sequence_frame_that_carries_canvases_draws_them(pixoo, make_plugin):
    plugin = make_plugin()
    last = canvas_frame(x=8, y=8)
    plugin.write_sequence([TimedFrame(rows("A"), 0), TimedFrame(last, 0)], cancel=CancelToken())
    assert pushed(pixoo)[-1] == expected(plugin, last)


# --- older cores -------------------------------------------------------------------------------


def _old_layout_message(message, spec, options=None):
    """``layout_message`` as a FiestaBoard before pixel canvases had it: no ``layers``."""
    return layout_message(message, spec, options)


def test_on_a_core_without_canvases_a_plain_frame_renders_unchanged(monkeypatch):
    plugin = build({"host": "192.0.2.10"})
    plain = rows("HELLO")
    before = plugin.render(plain)
    monkeypatch.setattr(pixoo_module, "layout_message", _old_layout_message)
    assert plugin.render(plain) == before


def test_on_a_core_without_canvases_a_frame_with_layers_still_renders_its_text(monkeypatch):
    class Framed(list):
        layers = ({"x": 0, "y": 0, "width": 1, "height": 1, "rgba": "AAAA/w=="},)

    plugin = build({"host": "192.0.2.10"})
    monkeypatch.setattr(pixoo_module, "layout_message", _old_layout_message)
    assert plugin.render(Framed(rows("HELLO"))) == plugin.render(rows("HELLO"))
