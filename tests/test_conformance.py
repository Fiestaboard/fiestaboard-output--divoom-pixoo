"""FiestaBoard core's output-plugin conformance suite, run against this plugin.

The suite plays core and device both: it routes the plugin's device helper
(``self.http``) to its fake transport, so the plugin's real request code runs. ``decode`` turns a
``Draw/SendHttpGif`` payload back into the grid it shows, by matching its
RGB against every uniform grid the suite writes, so the suite can check
that a sequence upload ends on its target frame.

The pacing constants stay at their shipped values here: conformance is
about the plugin as it ships.
"""

from __future__ import annotations

import base64
from functools import cache

from plugins.divoom_pixoo import DivoomPixoo

from src.outputs.conformance import OutputConformanceSuite

from .conftest import PLUGIN_DIR
from .conftest import render as render_frame

ROWS, COLS = 10, 16


@cache
def _uniform_grids() -> dict[bytes, tuple[tuple[int, ...], ...]]:
    grids = {}
    for code in range(72):
        grid = tuple(tuple([code] * COLS) for _ in range(ROWS))
        grids.setdefault(render_frame([list(r) for r in grid]), grid)
    return grids


def decode(payload):
    if not isinstance(payload, dict) or payload.get("Command") != "Draw/SendHttpGif":
        return None
    grid = _uniform_grids().get(base64.b64decode(payload["PicData"]))
    return None if grid is None else [list(row) for row in grid]


def make_plugin(board_id, config, transport):
    # The suite routes self.http to its fake device itself.
    return DivoomPixoo(board_id, config)


def test_the_plugin_is_conformant():
    report = OutputConformanceSuite(
        plugin_dir=PLUGIN_DIR,
        factory=make_plugin,
        config={"host": "192.0.2.10", "brightness": 50},
        decode=decode,
    ).assert_conformant()
    # Every rule ran (device_traffic included) except the secret check.
    assert all(s.startswith("device_key: no secret") for s in report.skipped), report.skipped
