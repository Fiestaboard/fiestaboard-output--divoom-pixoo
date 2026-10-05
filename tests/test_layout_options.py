"""Tile style and Block padding: the two board settings that change the pixels sent.

``tile_gap`` (Gaps / Seamless) and ``block_padding`` (Off / 1 pixel) are
FiestaUI's LED layout options ``tileGap`` / ``blockPadding``. The Pixoo 64
model allows both values of both, with today's look as the default; core
resolves a board's choice against the model and the plugin lays out with it,
so the device shows exactly what the board's preview draws.

The 3x5 face on 64x64: cell (r, c)'s glyph box is x 4c..4c+2, y 2+6r..6+6r;
the column gutter after column c is x = 4c+3, the row gutter after row r is
y = 6r+7, and the margin is y = 0..1 above the grid.
"""

from __future__ import annotations

import base64
import json

from src.led import LedLayoutOptions, layout_message, led_spec_for_model, rasterize
from src.markup import message_to_grid
from src.outputs.plugin_base import CancelToken

from .conftest import MANIFEST, PLUGIN_DIR, build

ROWS, COLS = 10, 16
RED = (0xEB, 0x40, 0x34)
WHITE = (0xFF, 0xFF, 0xFF)
OFF = (0, 0, 0)
TILES = "{63}{63}{63}"
BLOCK = "{black/white:OK}"


def cells(message: str):
    return message_to_grid(message, ROWS, COLS, extended_markup=True, preserve_case=True)


def pixel(pixels: bytes, x: int, y: int) -> tuple[int, int, int]:
    i = (y * 64 + x) * 3
    return tuple(pixels[i : i + 3])


def render(message: str, **config) -> bytes:
    return build({"host": "192.0.2.10", **config}).render(cells(message))


# --- the settings ------------------------------------------------------------------------


def test_the_board_screen_offers_tile_style_gaps_or_seamless():
    prop = MANIFEST.output.settings_schema["properties"]["tile_gap"]
    assert (prop["title"], prop["enum"], prop["enumNames"], prop["default"]) == (
        "Tile style",
        ["gap", "fill"],
        ["Gaps", "Seamless"],
        "gap",
    )


def test_the_board_screen_offers_block_padding_off_or_one_pixel():
    prop = MANIFEST.output.settings_schema["properties"]["block_padding"]
    assert (prop["title"], prop["type"], prop["enum"], prop["enumNames"], prop["default"]) == (
        "Block padding",
        "integer",
        [0, 1],
        ["Off", "1 pixel"],
        0,
    )


def test_neither_setting_is_required():
    assert {"tile_gap", "block_padding"}.isdisjoint(MANIFEST.output.settings_schema["required"])


def test_the_pixoo_model_allows_both_values_of_both_with_todays_defaults():
    model = json.loads((PLUGIN_DIR / "output" / "device-models.json").read_text())[0]
    assert model["layoutOptions"] == {
        "tileGap": {"allowed": ["gap", "fill"], "default": "gap"},
        "blockPadding": {"allowed": [0, 1], "default": 0},
    }


# --- what the device is sent ------------------------------------------------------------------


def test_by_default_tiles_keep_their_gaps_and_blocks_their_edges():
    plain = render(TILES + "\n" + " " + BLOCK)
    assert pixel(plain, 3, 2) == OFF  # gutter between the first two red tiles
    assert pixel(plain, 3, 9) == OFF  # left of the block: no padding


def test_seamless_lights_the_gutter_between_same_colour_tiles():
    seamless = render(TILES, tile_gap="fill")
    assert [pixel(seamless, 3, y) for y in range(2, 7)] == [RED] * 5
    assert [pixel(seamless, 7, y) for y in range(2, 7)] == [RED] * 5
    assert pixel(seamless, 11, 2) == OFF  # after the last tile: a blank, not a tile
    assert pixel(seamless, 0, 1) == OFF  # the margin is never filled


def test_one_pixel_padding_borders_the_block_with_its_background():
    padded = render(" " + BLOCK, block_padding=1)
    assert pixel(padded, 3, 2) == WHITE  # left of the block, its row
    assert pixel(padded, 3, 1) == WHITE  # the corner, in the margin above
    assert pixel(padded, 11, 7) == WHITE  # bottom-right corner, in the row gutter
    assert pixel(padded, 12, 2) == OFF  # one pixel, not two


def test_the_device_gets_the_bytes_core_draws_for_those_options():
    message = TILES + "\n" + " " + BLOCK
    expected = rasterize(
        layout_message(
            cells(message),
            led_spec_for_model(build({"host": "192.0.2.10"}).device_model),
            LedLayoutOptions(charset=build({"host": "192.0.2.10"}).character_set, tile_gap="fill", block_padding=1),
        )
    ).pixels
    assert render(message, tile_gap="fill", block_padding=1) == expected
    assert expected != render(message)


def test_a_value_the_model_does_not_allow_draws_the_default():
    message = TILES + "\n" + " " + BLOCK
    assert render(message, tile_gap="seamless", block_padding=2) == render(message)


def test_a_seamless_board_pushes_its_seamless_frame(pixoo, make_plugin):
    plugin = make_plugin(tile_gap="fill", block_padding=1)
    result = plugin.write_cells(cells(TILES), native=None, cancel=CancelToken())
    assert result.success and result.was_sent
    pushed = base64.b64decode(pixoo.pushes()[-1]["PicData"])
    assert pixel(pushed, 3, 2) == RED
    assert pushed == render(TILES, tile_gap="fill", block_padding=1)
