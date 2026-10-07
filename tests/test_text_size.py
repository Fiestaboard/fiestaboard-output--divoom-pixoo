"""Text size: a Pixoo board draws in the Large (5x7) or Small (3x5) face.

The Pixoo 64 model offers both faces (``layoutOptions.font``) with Large as
the default for new boards, while the model's own ``font`` / ``charset``
stay 3x5: the face a board draws in when core binds no choice (an existing
board keeps 10 x 16).
Core resolves a board's face (``output_config`` key ``font``) and binds the
plugin to a copy of the model with ``font`` and ``charset`` swapped; the
plugin lays out from ``self.device_model``, so it needs no code of its own.

Grids on 64x64: 5x7 cells are 6 x 8 pixels -> 8 rows x 10 columns;
3x5 cells are 4 x 6 pixels -> 10 rows x 16 columns.
"""

from __future__ import annotations

import json

import pytest

from src.led import layout_message, led_spec_for_model
from src.markup import message_to_grid

from .conftest import MANIFEST, PLUGIN_DIR, build

FACES = {"5x7": ("led_5x7", (8, 10)), "3x5": ("led_3x5", (10, 16))}


def pixoo_model() -> dict:
    return json.loads((PLUGIN_DIR / "output" / "device-models.json").read_text())[0]


def model_in(face: str) -> dict:
    """The model core binds a board that chose *face* to (FiestaUI ``modelWithLedFont``)."""
    return {**pixoo_model(), "font": face, "charset": FACES[face][0]}


def bound(face: str):
    plugin = build({"host": "192.0.2.10", "font": face})
    plugin.bind_board(device_model=model_in(face))
    return plugin


def grid_of(plugin) -> tuple[int, int]:
    layout = plugin.layout([[0]])
    return layout.grid.rows, layout.grid.cols


# --- the setting -------------------------------------------------------------------------


def test_the_board_screen_offers_text_size_large_or_small_defaulting_to_large():
    prop = MANIFEST.output.settings_schema["properties"]["font"]
    assert (prop["title"], prop["type"], prop["enum"], prop["enumNames"], prop["default"]) == (
        "Text size",
        "string",
        ["5x7", "3x5"],
        ["Large", "Small"],
        "5x7",
    )
    assert "resize" in prop["description"]


def test_text_size_is_not_required():
    assert "font" not in MANIFEST.output.settings_schema["required"]


def test_text_size_sits_beside_tile_style():
    props = list(MANIFEST.output.settings_schema["properties"])
    assert props.index("font") == props.index("tile_gap") - 1


# --- the model -----------------------------------------------------------------------------


def test_the_pixoo_model_offers_both_faces_with_large_for_new_boards():
    assert pixoo_model()["layoutOptions"]["font"] == {"allowed": ["5x7", "3x5"], "default": "5x7"}


def test_the_model_itself_stays_small_so_a_board_with_no_choice_draws_10_by_16():
    model = pixoo_model()
    assert (model["font"], model["charset"]) == ("3x5", "led_3x5")
    assert grid_of(build({"host": "192.0.2.10"})) == (10, 16)


def test_the_bound_copy_is_the_one_core_makes():
    led = pytest.importorskip("src.led")
    if not hasattr(led, "model_with_led_font"):
        pytest.skip("this FiestaBoard predates per-board text size")
    for face in FACES:
        assert dict(led.model_with_led_font(pixoo_model(), face)) == model_in(face)


# --- what the device is sent ---------------------------------------------------------------


@pytest.mark.parametrize(("face", "grid"), [(f, g) for f, (_, g) in FACES.items()])
def test_a_board_bound_to_a_face_lays_out_on_that_faces_grid(face, grid):
    plugin = bound(face)
    assert plugin.character_set["id"] == FACES[face][0]
    assert grid_of(plugin) == grid
    assert len(plugin.render([[0] * grid[1]] * grid[0])) == 64 * 64 * 3


def test_large_draws_the_5x7_glyphs_core_draws():
    plugin = bound("5x7")
    frame = message_to_grid("HELLO", 8, 10, extended_markup=True, preserve_case=True)
    expected = layout_message(frame, led_spec_for_model(model_in("5x7")), plugin.led_layout_options())
    assert plugin.layout(frame) == expected
    assert plugin.render(frame) != bound("3x5").render(
        message_to_grid("HELLO", 10, 16, extended_markup=True, preserve_case=True)
    )
