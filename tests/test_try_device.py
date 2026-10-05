"""tools/try_device.py, run against the mock Pixoo (never a real device here)."""

from __future__ import annotations

import importlib.util
import io
import json

import pytest

from .conftest import PLUGIN_DIR


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("try_device_under_test", PLUGIN_DIR / "tools" / "try_device.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_shows_a_still_then_one_transition_and_times_every_request(tool, pixoo):
    out = io.StringIO()
    report = tool.run(pixoo.host, pause_s=0, ready_s=0, out=out)

    commands = [c["command"] for c in report["calls"]]
    # Exactly: reset + seed + still, then the change, which the Pixoo snaps: one more still.
    assert commands == ["Draw/ResetHttpGifId", "Draw/GetHttpGifId", "Draw/SendHttpGif", "Draw/SendHttpGif"]
    assert commands == [c.get("Command") for c in pixoo.commands]
    assert all(c["status"] == 200 and c["ms"] >= 0 and "PicData" not in c for c in report["calls"])
    assert report["pic_ids"] == [1, 2]
    assert [s["result"]["success"] for s in report["steps"]] == [True, True]
    assert report["steps"][1]["transition"]["id"] == "none"

    text = out.getvalue()
    assert "Draw/SendHttpGif" in text and "PicIDs:" in text and "PicData" not in text


def test_the_first_message_is_hello_fiesta(tool, pixoo):
    from src.plugins import cells_from_codes, text_to_board_array

    from .conftest import build, render

    tool.run(pixoo.host, pause_s=0, ready_s=0, out=io.StringIO())
    rows, cols = build({"host": "192.0.2.10"}).board_geometry
    expected = render(cells_from_codes(text_to_board_array("HELLO\nFIESTA", rows, cols)))
    import base64

    assert pixoo.commands[2]["PicData"] == base64.b64encode(expected).decode("ascii")


def test_a_failing_device_is_reported_with_its_errors(tool, pixoo):
    pixoo.mode = "http_500"
    report = tool.run(pixoo.host, pause_s=0, ready_s=0, out=io.StringIO())
    assert [s["result"]["success"] for s in report["steps"]] == [False, False]
    assert {c["status"] for c in report["calls"]} == {500}


def test_an_unreachable_device_records_the_error(tool):
    report = tool.run("127.0.0.1:9", pause_s=0, ready_s=0, out=io.StringIO())
    assert report["calls"] and all("error" in c for c in report["calls"])
    assert "error" in json.dumps(report)


def test_main_writes_json_and_exits_by_outcome(tool, pixoo, tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    monkeypatch.setattr(tool, "run", lambda host, pause_s: {"steps": [{"result": {"success": True}}], "host": host})
    assert tool.main(["--host", pixoo.host, "--pause", "0", "--json", str(target)]) == 0
    assert json.loads(target.read_text())["host"] == pixoo.host
    monkeypatch.setattr(tool, "run", lambda host, pause_s: {"steps": [{"result": {"success": False}}]})
    assert tool.main(["--host", pixoo.host]) == 1


def test_a_manifest_that_does_not_load_stops_the_tool(tool, monkeypatch, tmp_path):
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    (tmp_path / "manifest.json").write_text("{}")
    with pytest.raises(SystemExit):
        tool.build(object, "192.0.2.10")
