"""What goes on the wire for one frame: the exact JSON and the RGB it carries.

The expected pixels are painted here by hand (glyph bitmap, grid origin,
colours written out as literals), not by core's renderer, so a change in
how the plugin renders shows up as a byte difference.
"""

from __future__ import annotations

import base64

from src.markup import message_to_grid
from src.outputs.plugin_base import CancelToken

from .conftest import render as render_frame

ROWS, COLS = 10, 16
#: The 3x5 face on 64x64: 4 px per column (3 + 1 gap) from x=0, 6 px per row
#: (5 + 1 gap) from y=2 (59 px of rows centred in 64, remainder split down).
CELL_W, CELL_H, ORIGIN_X, ORIGIN_Y = 4, 6, 0, 2
GLYPH_A = (".#.", "#.#", "###", "#.#", "#.#")
WHITE = (0xFF, 0xFF, 0xFF)
RED = (0xEB, 0x40, 0x34)  # FiestaUI's board red, code 63


def blank() -> list[list[int]]:
    return [[0] * COLS for _ in range(ROWS)]


def paint(pixels: bytearray, x: int, y: int, rgb: tuple[int, int, int]) -> None:
    i = (y * 64 + x) * 3
    pixels[i : i + 3] = bytes(rgb)


def expected_with_a(row: int, col: int) -> bytes:
    pixels = bytearray(64 * 64 * 3)
    for dy, line in enumerate(GLYPH_A):
        for dx, ch in enumerate(line):
            if ch == "#":
                paint(pixels, ORIGIN_X + col * CELL_W + dx, ORIGIN_Y + row * CELL_H + dy, WHITE)
    return bytes(pixels)


def test_a_blank_frame_renders_all_pixels_off():
    assert render_frame(blank()) == bytes(64 * 64 * 3)


def test_a_letter_renders_as_its_3x5_glyph_at_its_cell():
    frame = blank()
    frame[1][2] = 1  # "A"
    assert render_frame(frame) == expected_with_a(1, 2)


def test_a_colour_tile_renders_as_a_solid_cell_in_its_colour():
    frame = blank()
    frame[0][0] = 63  # red tile
    pixels = render_frame(frame)
    lit = {
        (i // 3 % 64, i // 3 // 64): tuple(pixels[i : i + 3])
        for i in range(0, len(pixels), 3)
        if pixels[i : i + 3] != b"\x00\x00\x00"
    }
    assert lit == {(x, y): RED for x in range(3) for y in range(ORIGIN_Y, ORIGIN_Y + 5)}


def test_a_short_or_ragged_frame_is_padded_not_rejected():
    assert render_frame([[1]]) == expected_with_a(0, 0)


def test_the_first_write_resets_seeds_the_gif_id_then_sends_one_frame_exactly(pixoo, make_plugin):
    plugin = make_plugin()
    frame = blank()
    frame[1][2] = 1

    result = plugin.write(frame, native=None, cancel=CancelToken())

    assert (result.success, result.was_sent, result.partial) == (True, True, False)
    assert pixoo.commands == [
        {"Command": "Draw/ResetHttpGifId"},
        {"Command": "Draw/GetHttpGifId"},
        {
            "Command": "Draw/SendHttpGif",
            "PicNum": 1,
            "PicWidth": 64,
            "PicOffset": 0,
            "PicID": 1,
            "PicSpeed": 1000,
            "PicData": base64.b64encode(expected_with_a(1, 2)).decode("ascii"),
        },
    ]


def rich(message: str):
    return message_to_grid(message, ROWS, COLS, extended_markup=True)


def lit(pixels: bytes) -> dict[tuple[int, int], tuple[int, int, int]]:
    return {
        (i // 3 % 64, i // 3 // 64): tuple(pixels[i : i + 3])
        for i in range(0, len(pixels), 3)
        if pixels[i : i + 3] != b"\x00\x00\x00"
    }


def test_rich_cells_without_markup_render_like_their_codes():
    assert render_frame(rich("A")) == expected_with_a(0, 0)


def test_a_colour_span_draws_its_letters_in_that_colour():
    pixels = lit(render_frame(rich("{red:A}")))
    a = lit(expected_with_a(0, 0))
    assert set(pixels) == set(a)
    assert set(pixels.values()) == {RED}


def test_an_icon_draws_lit_pixels_in_its_cell():
    pixels = lit(render_frame(rich("{icon:sun}")))
    assert pixels
    assert all(0 <= x < CELL_W and ORIGIN_Y <= y < ORIGIN_Y + CELL_H for x, y in pixels)


def test_write_cells_sends_the_rich_frame(pixoo, make_plugin):
    plugin = make_plugin()
    cells = rich("{red:A}")
    result = plugin.write_cells(cells, native=None, cancel=CancelToken())
    assert (result.success, result.was_sent) == (True, True)
    assert pixoo.pushes()[-1]["PicData"] == base64.b64encode(render_frame(cells)).decode("ascii")
    assert render_frame(cells) != expected_with_a(0, 0)
