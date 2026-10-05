"""A stand-in Divoom Pixoo 64 on loopback, for tests.

It answers ``POST /post`` the way the device's local HTTP API does (a JSON
command in, ``{"error_code": 0, ...}`` out), records every command, and can
misbehave on purpose:

- ``mode = "ok"`` (default) answers every command;
- ``"http_500"`` / ``"http_404"`` answer with that status;
- ``"garbage"`` answers 200 with a body that is not JSON;
- ``"error_code"`` answers 200 with ``{"error_code": 1}``;
- ``"json_list"`` answers 200 with JSON that is not an object;
- ``"other_json"`` answers 200 ``{"error_code": 0}`` without the Pixoo's config keys
  (another device on the LAN that happens to speak JSON);
- ``"hang"`` accepts the request and never answers (until :meth:`stop`);
- ``freeze_after = N`` reproduces the community-reported freeze: after N
  ``Draw/SendHttpGif`` pushes with no ``Draw/ResetHttpGifId`` in between,
  the device stops answering every command.

PicIDs behave as the 2026-10-04 hardware lab measured: ``Draw/GetHttpGifId``
answers the next id (0 right after a reset); an upload displays only when its
PicID is above the last accepted one, and a reused or lower PicID still
answers ``error_code`` 0 (recorded in ``ignored``). ``ignore_reset`` makes
the device keep its last id across a reset.

``on_command`` (``fn(command_dict, index)``) runs before the answer, so a
test can fire a cancel token after the k-th push.

Only the standard library; binds 127.0.0.1 on a free port.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

SEND = "Draw/SendHttpGif"
RESET = "Draw/ResetHttpGifId"
GET_ID = "Draw/GetHttpGifId"
GET_CONF = "Channel/GetAllConf"
SET_BRIGHTNESS = "Channel/SetBrightness"

#: What ``Channel/GetAllConf`` answers (a trimmed, made-up device).
ALL_CONF = {
    "error_code": 0,
    "Brightness": 60,
    "RotationFlag": 0,
    "ClockTime": 60,
    "GalleryTime": 60,
    "SingleGalleyTime": -1,
    "PowerOnChannelId": 1,
    "GalleryShowTimeFlag": 0,
    "CurClockId": 182,
    "Time24Flag": 1,
    "TemperatureMode": 0,
    "GyrateAngle": 0,
    "MirrorFlag": 0,
    "LightSwitch": 1,
}


class MockPixoo:
    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []
        self.mode = "ok"
        self.freeze_after: int | None = None
        self.on_command: Callable[[dict[str, Any], int], None] | None = None
        self._pushes_since_reset = 0
        #: The device's PicID state (lab-verified semantics, see _answer).
        self.last_pic_id = 0
        self.ignore_reset = False
        self.displayed: list[dict[str, Any]] = []
        self.ignored: list[dict[str, Any]] = []
        self._frozen = False
        self._release = threading.Event()
        self._lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    command = json.loads(raw)
                except ValueError:
                    command = {"_unparsed": raw.decode("latin-1")}
                status, body = mock._answer(command, self.path)
                if status is None:
                    mock._release.wait(30)
                    return
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # --- lifecycle -----------------------------------------------------------------------

    def start(self) -> MockPixoo:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._release.set()
        self.server.shutdown()
        self.server.server_close()

    @property
    def host(self) -> str:
        host, port = self.server.server_address[:2]
        return f"{host}:{port}"

    # --- what the device saw ---------------------------------------------------------------

    def names(self) -> list[str]:
        return [c.get("Command", "?") for c in self.commands]

    def pushes(self) -> list[dict[str, Any]]:
        return [c for c in self.commands if c.get("Command") == SEND]

    def clear(self) -> None:
        with self._lock:
            self.commands = []

    # --- behaviour -------------------------------------------------------------------------

    def _answer(self, command: dict[str, Any], path: str) -> tuple[int | None, Any]:
        with self._lock:
            index = len(self.commands)
            self.commands.append(command)
        if self.on_command is not None:
            self.on_command(command, index)
        if path != "/post":
            return 404, {"error": "not found"}
        if self.mode == "hang" or self._frozen:
            return None, None
        if self.mode == "http_500":
            return 500, {"error": "internal"}
        if self.mode == "http_404":
            return 404, {"error": "not found"}
        if self.mode == "garbage":
            return 200, b"<html>not the pixoo</html>"
        if self.mode == "json_list":
            return 200, [0]
        if self.mode == "other_json":
            return 200, {"error_code": 0, "status": "some other device"}
        if self.mode == "error_code":
            return 200, {"error_code": 1}
        name = command.get("Command")
        if name == RESET:
            self._pushes_since_reset = 0
            if not self.ignore_reset:
                self.last_pic_id = 0
        elif name == GET_ID:
            # The next id, or 0 right after a reset (lab-verified).
            return 200, {"error_code": 0, "PicId": self.last_pic_id + 1 if self.last_pic_id else 0}
        elif name == SEND:
            self._pushes_since_reset += 1
            if self.freeze_after is not None and self._pushes_since_reset > self.freeze_after:
                self._frozen = True
                return None, None
            pic_id = command.get("PicID", 0)
            # Lab-verified: only a PicID above the last accepted one displays;
            # a reused or lower one still answers error_code 0.
            if pic_id > self.last_pic_id:
                self.displayed.append(command)
                if command.get("PicOffset", 0) == command.get("PicNum", 1) - 1:
                    self.last_pic_id = pic_id
            elif command.get("PicOffset", 0) == 0:
                self.ignored.append(command)
        if name == GET_CONF:
            return 200, ALL_CONF
        return 200, {"error_code": 0}
