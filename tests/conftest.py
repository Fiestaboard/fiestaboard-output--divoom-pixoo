"""Shared fixtures: a mock Pixoo on loopback and plugins pointed at it.

Every test talks to :class:`~tests.mock_pixoo.MockPixoo` on 127.0.0.1; the
network fence (pytest-socket, ``--allow-hosts=127.0.0.1``) refuses anything
else, so no test can reach a real device.

``make_plugin`` builds a :class:`DivoomPixoo` the way core does (the
manifest's ``output`` block bound, ``open()`` called) and shortens the
device-pacing constants so tests run fast. Tests that are *about* pacing
set them back explicitly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from plugins.divoom_pixoo import DivoomPixoo

from src.plugins.manifest import load_manifest

from .mock_pixoo import MockPixoo

PLUGIN_DIR = Path(__file__).resolve().parent.parent
MANIFEST, _ERRORS = load_manifest(PLUGIN_DIR / "manifest.json")


@pytest.fixture
def pixoo():
    device = MockPixoo().start()
    yield device
    device.stop()


def build(config: dict, *, fast: bool = True) -> DivoomPixoo:
    plugin = DivoomPixoo("board-test", config)
    if MANIFEST is not None and MANIFEST.output is not None:
        plugin.bind_manifest(MANIFEST.output)
    if fast:
        plugin.FRAME_GAP_S = 0.0
        plugin.LOADING_OVERLAY_S = 0.0
        plugin.CONNECT_TIMEOUT_S = 0.5
        plugin.READ_TIMEOUT_S = 0.5
    plugin.open()
    return plugin


@pytest.fixture
def make_plugin(pixoo):
    built: list[DivoomPixoo] = []

    def factory(fast: bool = True, **config) -> DivoomPixoo:
        config.setdefault("host", pixoo.host)
        plugin = build(config, fast=fast)
        built.append(plugin)
        return plugin

    yield factory
    for plugin in built:
        plugin.close()
