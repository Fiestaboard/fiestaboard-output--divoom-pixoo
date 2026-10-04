"""Divoom Pixoo 64 output plugin for FiestaBoard.

Drives one Pixoo 64 over its local HTTP API (``POST http://<host>/post``,
one JSON command per request). Core owns the policy — the send floor
(``min_interval_ms``, 1 s between writes and sequences), dedupe, preemption
and the write budget — and this plugin moves pixels:

- **Render.** Every frame is laid out with core's LED renderer
  (:mod:`src.led`) in the model's 3x5 face — a 10-row x 16-column grid on
  64x64 pixels — and rasterised to RGB888. Today core hands an output the
  0-71 character grid; :func:`frame_tokens` is the one seam that turns a
  frame into board tokens, and it already passes rich cells
  (:class:`src.markup.BoardToken`) through untouched, so per-cell colour
  and icons arrive without touching the write path.
- **write** pushes one still frame: ``Draw/SendHttpGif`` with ``PicNum`` 1.
- **write_sequence** runs FiestaBoard's LED flip (coarse: one frame per
  step, no half-flaps, at most the model's 32 frames, at least 80 ms each)
  from what the device shows to the target, uploads it as ONE multi-frame
  GIF (same ``PicID``, ``PicOffset`` 0..n-1), then — once it has played —
  pushes the target as a still, so the device never loops the animation.
- **PicID policy.** ``Draw/ResetHttpGifId`` before the first upload of an
  instance, before every animation, after any failed or cancelled upload,
  and once :data:`RESET_AFTER_PUSHES` frames went up since the last reset.
  PicIDs count up from 1 after each reset.

Every request is bounded by ``(connect, read)`` timeouts and checks the run's
cancel token first; waits (pacing, the play-out) wait on the token. Device
failures come back as a failed :class:`WriteResult`, never as an exception.
Only the standard library, ``requests`` and FiestaBoard core are used.

Several device facts below are community-reported and **unverified** on
current firmware; each is a named constant, listed in the README's
"Verify on your device" section.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any

import requests

from src.board_chars import characters_to_message
from src.led import (
    BUILTIN_CHARACTER_SETS,
    LedLayout,
    LedLayoutOptions,
    layout_message,
    led_spec_for_model,
    plan_transition,
    rasterize,
    resolve_led_transition,
    transition_frames,
)
from src.markup import BoardToken
from src.output_allowlist import OutputHostBlocked, check_output_url
from src.plugins import (
    CancelToken,
    CellFrame,
    ConnectionCheck,
    OutputPluginBase,
    TimedFrame,
    WriteResult,
)

logger = logging.getLogger(__name__)

__all__ = ["DivoomPixoo", "PixooError", "frame_tokens", "layout_frame", "render_frame"]

# --- device facts (each one: see README "Verify on your device") ----------------------------

#: Frame POSTs between two ``Draw/ResetHttpGifId``. Community reports say the
#: device stops answering after ~300 pushes without a reset; 32 matches the
#: SomethingWithComputers/pixoo library's default refresh and keeps an order
#: of magnitude of headroom. UNVERIFIED on current firmware.
RESET_AFTER_PUSHES = 32

#: Pause between two POSTs of one upload. Reports put the safe push spacing
#: anywhere from ~150 ms to 1 s; core's floor already spaces whole writes by
#: 1 s, so this only paces the frames of one animation. UNVERIFIED.
FRAME_GAP_S = 0.15

#: How long the device shows its "Loading.." overlay before it plays an
#: uploaded animation (reported ~5 s). The still-frame push waits this out,
#: plus the play time, so the animation is seen before the still replaces it.
#: UNVERIFIED, including whether single-frame pushes show it too.
LOADING_OVERLAY_S = 5.0

#: ``requests`` timeouts, ``(connect, read)``, for one command.
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 5.0

#: The Pixoo 64's pixel width; ``PicWidth`` must be exactly this.
PIC_WIDTH = 64

#: ``PicSpeed`` for a still frame (there is nothing to play; any value works).
STILL_SPEED_MS = 1000

SEND_GIF = "Draw/SendHttpGif"
RESET_GIF_ID = "Draw/ResetHttpGifId"
GET_ALL_CONF = "Channel/GetAllConf"
SET_BRIGHTNESS = "Channel/SetBrightness"

_DEVICE_MODELS = Path(__file__).resolve().parent / "output" / "device-models.json"


class PixooError(OSError):
    """The device answered, but not with success."""


class PixooStatusError(PixooError):
    """The device answered with an HTTP status other than 200."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class PixooBadResponse(PixooError):
    """A 200 whose body is not a Pixoo reply (not JSON, or ``error_code`` != 0)."""


# --- rendering -------------------------------------------------------------------------------


@cache
def device_model() -> dict[str, Any]:
    """The plugin's own ``divoom_pixoo64`` model (the file the manifest ``$ref`` s)."""
    return json.loads(_DEVICE_MODELS.read_text(encoding="utf-8"))[0]


@cache
def _layout_options() -> LedLayoutOptions:
    return LedLayoutOptions(charset=BUILTIN_CHARACTER_SETS[device_model()["charset"]])


@cache
def _code_token(code: int) -> BoardToken:
    """The board token a 0-71 character code draws as."""
    if 63 <= code <= 71:
        return BoardToken("color", code=str(code))
    text = characters_to_message([[code]]) if 0 <= code <= 62 else " "
    return BoardToken("char", value=text if len(text) == 1 else " ")


def frame_tokens(frame: Sequence[Sequence[Any]]) -> list[list[BoardToken]]:
    """A frame as rows of board tokens: the seam between core's frame and the renderer.

    A cell is a 0-71 character code (today's ``CellFrame``) or already a
    rich :class:`BoardToken` (per-cell glyph, colour, background, icon),
    which passes through as is.
    """
    return [[cell if isinstance(cell, BoardToken) else _code_token(int(cell)) for cell in row] for row in frame]


def layout_frame(frame: Sequence[Sequence[Any]]) -> LedLayout:
    """Lay *frame* out on the Pixoo's 64x64 matrix (3x5 face, 10x16 cells)."""
    spec = led_spec_for_model(device_model())
    assert spec is not None  # a pixels model
    return layout_message(frame_tokens(frame), spec, _layout_options())


def render_frame(frame: Sequence[Sequence[Any]]) -> bytes:
    """*frame* as the device's RGB888 pixels, row-major from the top left."""
    return rasterize(layout_frame(frame)).pixels


# --- the plugin ------------------------------------------------------------------------------

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def normalise_host(raw: Any) -> str:
    """``host`` or ``host:port``, lowercased, with any scheme or path dropped."""
    text = str(raw or "").strip()
    text = _SCHEME.sub("", text)
    return text.split("/", 1)[0].strip().lower()


class DivoomPixoo(OutputPluginBase):
    """One Divoom Pixoo 64 on the local network."""

    plugin_id = "divoom_pixoo"

    # Device pacing, as instance-overridable attributes (tests shorten them).
    RESET_AFTER_PUSHES = RESET_AFTER_PUSHES
    FRAME_GAP_S = FRAME_GAP_S
    LOADING_OVERLAY_S = LOADING_OVERLAY_S
    CONNECT_TIMEOUT_S = CONNECT_TIMEOUT_S
    READ_TIMEOUT_S = READ_TIMEOUT_S

    def __init__(self, board_id: str | None, config: dict[str, Any]) -> None:
        super().__init__(board_id, config)
        self.host = normalise_host(self.config.get("host"))
        #: The PicID the next upload uses; ``None`` = the device's counter is
        #: unknown (new instance, or an upload failed or was cut short).
        self._next_pic_id: int | None = None
        self._pushes_since_reset = 0
        #: What the device shows, as laid out (the flip starts from it).
        self._shown: LedLayout | None = None
        brightness = self.config.get("brightness")
        self._brightness: int | None = None if brightness is None else max(0, min(100, int(brightness)))
        self._brightness_pending = self._brightness is not None

    # --- identity --------------------------------------------------------------------------

    def device_key(self) -> str:
        return self.host or super().device_key()

    @property
    def url(self) -> str:
        return f"http://{self.host}/post"

    # --- the device seam ----------------------------------------------------------------------

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one command; the device's JSON reply, or raise.

        Raises:
            OutputHostBlocked: the host is outside FIESTABOARD_OUTPUTS_ALLOW_HOSTS.
            requests.RequestException: no answer (unreachable, timed out).
            PixooStatusError / PixooBadResponse: an answer that is not success.
        """
        if not self.host:
            raise requests.exceptions.InvalidURL("No device address configured")
        check_output_url(self.url)
        response = requests.post(self.url, json=payload, timeout=(self.CONNECT_TIMEOUT_S, self.READ_TIMEOUT_S))
        if response.status_code != 200:
            raise PixooStatusError(response.status_code)
        try:
            body = response.json()
        except ValueError as exc:
            raise PixooBadResponse("the reply is not JSON") from exc
        if not isinstance(body, dict):
            raise PixooBadResponse("the reply is not a JSON object")
        if body.get("error_code", 0) != 0:
            raise PixooBadResponse(f"error_code {body.get('error_code')!r}")
        return body

    # --- uploads -------------------------------------------------------------------------------

    def _wait(self, seconds: float, cancel: CancelToken) -> bool:
        """Pause; True when the run was cancelled meanwhile."""
        if seconds > 0:
            return cancel.wait(seconds)
        return cancel.cancelled

    def _upload(self, frames: list[bytes], speed_ms: int, cancel: CancelToken) -> bool | None:
        """Upload *frames* as one GIF.

        Returns True when every frame landed, None when cancelled first, and
        raises when the device failed. Either of the last two leaves the
        device's PicID counter unknown, so the next upload resets.
        """
        count = len(frames)
        posted = False

        def gap() -> bool:
            return self._wait(self.FRAME_GAP_S, cancel) if posted else cancel.cancelled

        try:
            if (
                self._next_pic_id is None
                or count > 1
                or self._pushes_since_reset + count > self.RESET_AFTER_PUSHES
            ):
                if cancel.cancelled:
                    return None
                self._next_pic_id = None
                self._post({"Command": RESET_GIF_ID})
                posted = True
                self._next_pic_id, self._pushes_since_reset = 1, 0
            pic_id = self._next_pic_id
            for offset, pixels in enumerate(frames):
                if gap():
                    self._next_pic_id = None
                    return None
                self._next_pic_id = None  # unknown until this frame lands
                self._post(
                    {
                        "Command": SEND_GIF,
                        "PicNum": count,
                        "PicWidth": PIC_WIDTH,
                        "PicOffset": offset,
                        "PicID": pic_id,
                        "PicSpeed": speed_ms,
                        "PicData": base64.b64encode(pixels).decode("ascii"),
                    }
                )
                posted = True
                self._pushes_since_reset += 1
            self._next_pic_id = pic_id + 1
            return True
        except Exception:
            self._next_pic_id = None
            raise

    def _apply_brightness(self, cancel: CancelToken) -> None:
        """Set the configured brightness once a frame has landed. A failure
        is logged and retried after the next frame; it never fails the write."""
        if not self._brightness_pending or cancel.cancelled:
            return
        try:
            self._post({"Command": SET_BRIGHTNESS, "Brightness": self._brightness})
        except Exception as exc:
            logger.warning("Pixoo %s: setting brightness failed: %s", self.host, exc)
            return
        self._brightness_pending = False

    def _deliver(self, frames: list[bytes], speed_ms: int, cancel: CancelToken) -> WriteResult | None:
        """One upload as a write verdict; ``None`` when it landed."""
        try:
            landed = self._upload(frames, speed_ms, cancel)
        except Exception as exc:
            logger.warning("Pixoo %s: upload failed: %s", self.host or "(no address)", exc)
            return WriteResult(False, False)
        if landed is None:
            # Preempted by a newer frame: not a device failure.
            return WriteResult(True, False)
        return None

    def write(self, frame: CellFrame, *, native: Any, cancel: CancelToken) -> WriteResult:
        layout = layout_frame(frame)
        failed = self._deliver([rasterize(layout).pixels], STILL_SPEED_MS, cancel)
        if failed is not None:
            return failed
        self._shown = layout
        self._apply_brightness(cancel)
        return WriteResult(True, True)

    def write_sequence(self, frames: list[TimedFrame], *, cancel: CancelToken) -> WriteResult:
        if not frames:
            return WriteResult(True, False)
        model = device_model()
        target = layout_frame(frames[-1].frame)
        before = self._shown if self._shown is not None else layout_frame(frames[0].frame)
        # The model's default: the coarse flip (one frame per step, no
        # half-flaps, compressed to maxFrames, each step >= minFrameMs).
        spec = resolve_led_transition(None, model).spec
        transition = plan_transition(before, target, spec)
        pixels = [f.pixels for f in transition_frames(transition)]
        if self._shown is not None and len(pixels) > 1:
            pixels = pixels[1:]  # frame 0 is already on the device
        if len(pixels) <= 1:
            # Nothing changes (or nothing left to animate): one still frame.
            return self.write(frames[-1].frame, native=None, cancel=cancel)

        step_ms = int(spec.step_ms)
        failed = self._deliver(pixels, step_ms, cancel)
        if failed is not None:
            return failed
        # The device loops an uploaded GIF; once it has shown (after its
        # loading overlay) and played through, replace it with the still target.
        if self._wait(self.LOADING_OVERLAY_S + len(pixels) * step_ms / 1000.0, cancel):
            self._next_pic_id = None
            return WriteResult(True, False)
        return self.write(frames[-1].frame, native=None, cancel=cancel)

    # --- probes -----------------------------------------------------------------------------

    def check_connection(self) -> ConnectionCheck:
        where = self.host or "the Pixoo"
        try:
            body = self._post({"Command": GET_ALL_CONF})
        except OutputHostBlocked:
            return ConnectionCheck.blocked(self.host)
        except requests.exceptions.InvalidURL:
            return self._failed("unreachable", "No device address is set.", "No address", "Enter the Pixoo's IP address.")
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError) as exc:
            return self._failed(
                "unreachable",
                f"Could not connect to {where}.",
                str(exc),
                "Check the IP address in the Divoom app (Device Settings).",
                "Make sure the Pixoo is powered on and on the same network as FiestaBoard.",
            )
        except requests.exceptions.Timeout as exc:
            return self._failed(
                "timeout",
                f"{where} accepted the connection but did not answer.",
                str(exc),
                "Restart the Pixoo (unplug it for a few seconds) and try again.",
            )
        except PixooStatusError as exc:
            failure = "server_error" if exc.status >= 500 else "unexpected_status"
            return self._failed(
                failure, f"{where} answered with {exc}.", str(exc), "Check that this address is a Pixoo 64."
            )
        except PixooBadResponse as exc:
            return self._failed(
                "bad_response",
                f"{where} answered, but not like a Pixoo 64.",
                str(exc),
                "Check that this address is a Pixoo 64, not another device.",
            )
        except Exception as exc:
            return self._failed("unreachable", f"Could not reach {where}.", str(exc), "Check the IP address.")
        details = {"brightness": body["Brightness"]} if "Brightness" in body else {}
        return ConnectionCheck(success=True, message=f"Connected to the Pixoo 64 at {where}.", details=details)

    @staticmethod
    def _failed(failure: Any, message: str, error: str, *steps: str) -> ConnectionCheck:
        return ConnectionCheck(success=False, message=message, failure=failure, error=error, troubleshooting=steps)
