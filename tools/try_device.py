"""Try the plugin on a real Pixoo 64 once, and time every request.

**Local / development use only.** It is not part of the plugin and FiestaBoard
never runs it. It does exactly two things, then stops:

1. shows ``HELLO`` / ``FIESTA`` as a still frame (``write_cells``);
2. waits (5 s by default), then changes to ``FIESTA`` / ``BOARD`` through
   ``write_transition`` with the transition FiestaBoard resolves for the
   Pixoo model (``none`` since the 2026-10-04 lab: one more still push).

It prints each HTTP request — the command, its PicID / PicNum / PicOffset,
how long it took, the HTTP status and the device's reply (never the pixel
data) — and the PicIDs used. It never loops or pushes rapidly: it is not a
stress or freeze test.

Run it from the repository root against a FiestaBoard core checkout that has
the output-plugin API, with the device address on the command line only::

    PYTHONPATH=/path/to/FiestaBoard python3 tools/try_device.py --host 192.168.1.50

or in Docker (Docker Desktop reaches LAN addresses)::

    docker run --rm -v /path/to/FiestaBoard:/core:ro -v "$PWD":/plugin -w /plugin \\
      -e PYTHONPATH=/core <image with requests> python tools/try_device.py --host 192.168.1.50

``FIESTABOARD_OUTPUTS_ALLOW_HOSTS``, when set, must include the address.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
FIRST = "HELLO\nFIESTA"
SECOND = "FIESTA\nBOARD"


def load_plugin_class() -> type:
    """The plugin class, loaded from this repository's ``__init__.py``."""
    spec = importlib.util.spec_from_file_location("divoom_pixoo_try", ROOT / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.DivoomPixoo


class Recorder:
    """A transport for the plugin's ``self.http``: sends for real, and times it."""

    def __init__(self, started: float) -> None:
        self.started = started
        self.calls: list[dict[str, Any]] = []
        self.session = requests.Session()

    def __call__(self, request: Any) -> requests.Response:
        body = request.json if isinstance(request.json, dict) else {}
        call: dict[str, Any] = {
            "at_s": round(time.monotonic() - self.started, 3),
            "command": body.get("Command", "?"),
            **{k: body[k] for k in ("PicID", "PicNum", "PicOffset", "PicSpeed", "Brightness") if k in body},
        }
        sent = time.monotonic()
        try:
            response = self.session.request(request.method, request.url, timeout=request.timeout, **request.kwargs)
        except requests.RequestException as exc:
            call.update(ms=round((time.monotonic() - sent) * 1000), error=f"{type(exc).__name__}: {exc}")
            self.calls.append(call)
            raise
        call.update(ms=round((time.monotonic() - sent) * 1000), status=response.status_code, reply=response.text[:300])
        self.calls.append(call)
        return response


def build(plugin_cls: type, host: str) -> Any:
    from src.plugins.manifest import load_manifest

    manifest, errors = load_manifest(ROOT / "manifest.json")
    if manifest is None:
        raise SystemExit(f"manifest.json does not load: {errors}")
    plugin = plugin_cls(None, {"host": host})
    plugin.bind_manifest(manifest.output)
    plugin.open()
    return plugin


def run(host: str, *, pause_s: float = 5.0, ready_s: float | None = None, out=sys.stdout) -> dict[str, Any]:
    """The two operations against *host*; the report as a dict (also printed)."""
    from src.plugins import CancelToken, cells_from_codes, resolve_led_transition, text_to_board_array

    plugin = build(load_plugin_class(), host)
    if ready_s is not None:
        plugin.ANIMATION_READY_S = ready_s
    started = time.monotonic()
    recorder = Recorder(started)
    plugin.http.use_transport(recorder)
    rows, cols = plugin.board_geometry
    first = cells_from_codes(text_to_board_array(FIRST, rows, cols))
    second = cells_from_codes(text_to_board_array(SECOND, rows, cols))

    steps = []
    t = time.monotonic()
    result = plugin.write_cells(first, native=None, cancel=CancelToken())
    steps.append({"step": "still HELLO/FIESTA (write_cells)", "s": round(time.monotonic() - t, 3), "result": result._asdict()})

    time.sleep(pause_s)

    transition = resolve_led_transition(None, plugin.device_model)
    t = time.monotonic()
    result = plugin.write_transition(first, second, transition, cancel=CancelToken())
    steps.append(
        {
            "step": f"change to FIESTA/BOARD (transition {transition.id}, write_transition)",
            "s": round(time.monotonic() - t, 3),
            "result": result._asdict(),
            "transition": {"id": transition.id, "source": transition.source, "reason": transition.reason},
        }
    )
    plugin.close()

    report = {
        "host": host,
        "steps": steps,
        "calls": recorder.calls,
        "pic_ids": [c["PicID"] for c in recorder.calls if "PicID" in c],
        "constants": {
            "ANIMATION_READY_S": plugin.ANIMATION_READY_S,
            "RESET_AFTER_PUSHES": plugin.RESET_AFTER_PUSHES,
        },
    }
    _print(report, out)
    return report


def _print(report: dict[str, Any], out) -> None:
    print(f"Pixoo at {report['host']}", file=out)
    for call in report["calls"]:
        pic = " ".join(f"{k}={call[k]}" for k in ("PicID", "PicNum", "PicOffset", "PicSpeed") if k in call)
        tail = call.get("error") or f"HTTP {call.get('status')} {call.get('reply')}"
        print(f"  t={call['at_s']:7.3f}s  {call['ms']:5d} ms  {call['command']:<22} {pic:<44} {tail}", file=out)
    for step in report["steps"]:
        print(f"{step['step']}: {step['s']} s -> {step['result']}", file=out)
    print(f"PicIDs: {report['pic_ids']}", file=out)
    print(json.dumps(report["constants"]), file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", required=True, help="the Pixoo's address, for example 192.168.1.50")
    parser.add_argument("--pause", type=float, default=5.0, help="seconds between the still frame and the transition")
    parser.add_argument("--json", type=Path, help="also write the report as JSON to this file")
    args = parser.parse_args(argv)
    report = run(args.host, pause_s=args.pause)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
    ok = all(step["result"]["success"] for step in report["steps"])
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover - the command line entry point
    raise SystemExit(main())
