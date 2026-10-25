"""Divoom Pixoo 64 output plugin for FiestaBoard.

Drives one Pixoo 64 over its local HTTP API, one JSON command per request:
``POST http://<host>/post`` by default, or the explicit HTTP(S) endpoint
(port and path) the board's ``host`` setting names. Core owns the policy — the send floor
(``min_interval_ms``, 1 s between writes), dedupe, preemption, the write
budget and which LED transition a board runs — and this plugin moves pixels.

What the hardware labs (camera-timed, on a real Pixoo 64) settled:

- **An uploaded animation is the wrong tool.** It loops forever (no
  play-once), an upload of more than ~3 frames shows a "LOADING" overlay, and
  landing on a still after one glitches for ~5 s (lab 1, 2026-10-04).
- **A stream of single frames is the right one** (lab 2, 2026-10-05). Each
  ``Draw/SendHttpGif`` with ``PicNum`` 1 shows once, with no overlay and no
  loop, and the last frame lands cleanly. The device takes about five a
  second (each push ~0.19 s whatever the frame size): paced at 5 fps, 19 of
  20 frames showed and the final frame always did. So the device model
  streams at ``maxFps`` 5 and every LED transition core plans is played as
  paced stills at up to :data:`STREAM_MAX_FPS`: flip, slide, wipe, cascade,
  dissolve and pixel blend were verified on the panel in those labs.
- **Fade goes through black** by default as six streamed pixel frames,
  about one second at 5 fps. This is a stepped fade; it never uses persistent
  hardware brightness as temporary state, so cancellation or replacement
  cannot strand that setting at zero. ``fade_style: blend`` cross-fades pixels.
  The revised through-black sequence has not yet been camera-verified.
- **write / write_cells** push one still frame. Text is drawn into the image
  (``Draw/SendHttpText`` is an overlay that every new image wipes).
- **Pixel canvases** (a page's bitmap layers, ``frame.layers`` on cores that
  have them) are drawn by core's ``layout_message`` over the text, so stills,
  every streamed transition (core switches layers half-way through a per-cell
  one) and the fade's target all carry them. An older core has no layers and
  no ``layers=`` keyword; the plugin then lays out exactly as before.
- **write_sequence** streams its frames the same way, each held its own
  duration (never shorter than one stream step), at most
  :data:`SEQUENCE_MAX_FRAMES` (compressed evenly, first and last kept).
- Two requests in flight, or many frames in one ``Draw/CommandList``, show
  only the last frame: frames are always sent one at a time, in order.
- **PicID policy.** A session (the first upload of an instance, or after a
  failed or cancelled one) starts with ``Draw/ResetHttpGifId`` and is
  seeded from ``Draw/GetHttpGifId`` (0 means 1); after
  :data:`RESET_AFTER_PUSHES` frame pushes it resets again and restarts at 1.
  IDs only ever go up within a session: the device silently ignores a
  reused or lower PicID while still answering ``error_code`` 0, so 0 means
  "accepted", not "shown".

All device traffic goes through ``self.http`` (core's helper: the
``FIESTABOARD_OUTPUTS_ALLOW_HOSTS`` fence, no redirects, the run's cancel
token) with ``(connect, read)`` timeouts; the reset, id and brightness
commands are marked ``setup=True``. Device failures come back as a failed
:class:`WriteResult`, never as an exception.
"""

from __future__ import annotations

import base64
import inspect
import ipaddress
import logging
import os
import re
import socket
import time
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from src.plugins import (
    ActionField,
    ActionOutcome,
    BoardToken,
    CancelToken,
    CellFrame,
    ConnectionCheck,
    LedLayoutOptions,
    OutputHostBlocked,
    OutputHttp,
    OutputPluginBase,
    RequestCancelled,
    ResolvedLedTransition,
    RichCellFrame,
    TimedFrame,
    WriteResult,
    cells_from_codes,
    layout_message,
    led_spec_for_model,
    local_ipv4,
    plan_transition,
    rasterize,
    transition_frames,
)

try:
    from src.plugins import DevicePoll
except ImportError:  # a core before the poll_device hook never polls: nothing to report
    DevicePoll = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

__all__ = ["DivoomPixoo", "PixooError", "candidate_subnets", "is_pixoo_reply", "sweep"]

# --- device facts (each one: see README "Verify on your device") ----------------------------

#: Frame POSTs between two ``Draw/ResetHttpGifId``: the community convention
#: (SomethingWithComputers/pixoo resets every 32). The reported freeze after
#: ~300 pushes without a reset was NOT tested on hardware (the lab stayed
#: under 25 ids between resets); a reset needs no wait afterwards (verified).
RESET_AFTER_PUSHES = 32

#: The most frames one explicit sequence streams (compressed evenly beyond
#: it): at the stream rate that is already eight seconds of animation.
SEQUENCE_MAX_FRAMES = 40

#: The fastest the device shows a stream of single frames (lab 2: paced at
#: 5 fps, 19 of 20 frames showed and the last always did; 6 fps cannot be
#: sent, each push taking ~0.19 s).
STREAM_MAX_FPS = 5

#: The shortest a continuous transition (slide, wipe, cascade, dissolve,
#: blend) runs when nothing asked for a duration: core's default (480 ms) is
#: three frames at the stream rate, which reads as a jump; a second is five
#: (lab 2 played them at this length).
STREAM_TRANSITION_MS = 1000

#: Three pixel steps to black and three back to the target. At the model's
#: five-frame/second stream rate, the six pushes take about one second.
#: Never use persistent device brightness as temporary transition state.
FADE_STEPS = 3

#: The least time between two Divoom cloud lookups for a Pixoo that stopped
#: answering at its saved address (core polls every ~30 s; the cloud need not
#: hear from us that often while the device is simply unplugged).
REFIND_INTERVAL_S = 120.0

#: ``(connect, read)`` timeouts for one command.
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 5.0

#: The Pixoo 64's pixel width; ``PicWidth`` must be exactly this.
PIC_WIDTH = 64

#: ``PicSpeed`` for a still frame (there is nothing to play; any value works).
STILL_SPEED_MS = 1000

SEND_GIF = "Draw/SendHttpGif"
RESET_GIF_ID = "Draw/ResetHttpGifId"
GET_GIF_ID = "Draw/GetHttpGifId"
GET_ALL_CONF = "Channel/GetAllConf"
SET_BRIGHTNESS = "Channel/SetBrightness"


class PixooError(OSError):
    """The device answered, but not with success."""


class PixooStatusError(PixooError):
    """The device answered with an HTTP status other than 200."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class PixooBadResponse(PixooError):
    """A 200 whose body is not a Pixoo reply (not JSON, or ``error_code`` != 0)."""


class _NoHost(requests.exceptions.InvalidURL):
    """No device address is configured."""


_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def normalise_host(raw: Any) -> str:
    """``host`` or ``host:port``, lowercased, with any scheme or path dropped."""
    text = _SCHEME.sub("", str(raw or "").strip())
    return text.split("/", 1)[0].strip().lower()


# --- finding a Pixoo -------------------------------------------------------------------------

#: Port, per-host timeout, total budget and concurrency of a LAN sweep.
DISCOVERY_PORT = 80
DISCOVERY_PER_HOST_S = 0.8
DISCOVERY_TOTAL_S = 10.0
DISCOVERY_CONCURRENCY = 64
#: The largest network a sweep accepts (a /22 is 1022 hosts).
DISCOVERY_MAX_PREFIX = 22
#: Divoom's LAN-lookup endpoint (opt-in: the request goes to Divoom's cloud).
DIVOOM_LAN_LOOKUP_URL = "https://app.divoom-gz.com/Device/ReturnSameLANDevice"
#: Keys of the Pixoo's Channel/GetAllConf reply; any one marks the device.
_PIXOO_KEYS = frozenset({"Brightness", "LightSwitch", "RotationFlag", "CurClockId"})
_ALLOW_ENV = "FIESTABOARD_OUTPUTS_ALLOW_HOSTS"


def is_pixoo_reply(body: Any) -> bool:
    """Whether *body* is a Pixoo's ``Channel/GetAllConf`` answer."""
    return isinstance(body, dict) and body.get("error_code") == 0 and bool(_PIXOO_KEYS & body.keys())


def _resolve_ipv4(name: str) -> str | None:
    try:
        return socket.gethostbyname(name)
    except (OSError, UnicodeError):
        return None


def _lan_address(value: str | None) -> ipaddress.IPv4Address | None:
    """*value* (an address or hostname) as a private, non-loopback LAN IPv4."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        resolved = _resolve_ipv4(text)
        if resolved is None:
            return None
        address = ipaddress.ip_address(resolved)
    if not isinstance(address, ipaddress.IPv4Address):
        return None
    if not address.is_private or address.is_loopback or address.is_link_local:
        return None
    return address


def candidate_subnets(
    *, subnet: str | None = None, hint_host: str | None = None, local_ip: str | None = None
) -> list[ipaddress.IPv4Network]:
    """The networks a sweep searches, in order: the one asked for, the /24 of
    the browser's address (*hint_host*: in Docker bridge mode FiestaBoard's
    own address is a container network), then FiestaBoard's own /24.

    FiestaBoard's own address is skipped when it is in 172.16.0.0/12 (where
    Docker puts its bridge networks) and the user already named a network:
    a Pixoo is never on a container network, so sweeping it only costs time.
    With nothing else to go on it is still searched.

    Raises:
        ValueError: *subnet* is malformed, not private, or larger than a /22.
    """
    found: list[ipaddress.IPv4Network] = []
    if subnet:
        network = ipaddress.IPv4Network(subnet.strip(), strict=False)
        if not network.is_private:
            raise ValueError(f"{network} is not a private network")
        if network.prefixlen < DISCOVERY_MAX_PREFIX:
            raise ValueError(f"{network} is too large to search; use a /{DISCOVERY_MAX_PREFIX} or smaller")
        found.append(network)
    for value in (hint_host, local_ip):
        address = _lan_address(value)
        if address is None:
            continue
        if value is local_ip and found and address in _DOCKER_BRIDGES:
            continue
        network = ipaddress.IPv4Network(f"{address}/24", strict=False)
        if not any(network.subnet_of(n) for n in found):
            found.append(network)
    return found


#: Where Docker allocates its bridge networks (and nothing a Pixoo joins).
_DOCKER_BRIDGES = ipaddress.IPv4Network("172.16.0.0/12")


def _device(ip: str, port: int, name: str = "Pixoo 64", label: str | None = None) -> dict[str, Any]:
    return {
        "ip": ip,
        "port": port,
        "host": ip if port == 80 else f"{ip}:{port}",
        "label": label or f"{name} at {ip}",
        "hostname": name,
    }


def normalise_mac(raw: Any) -> str | None:
    """A MAC as 12 lowercase hex digits (separators dropped), or ``None``."""
    text = re.sub(r"[^0-9a-fA-F]", "", str(raw or "")).lower()
    return text if len(text) == 12 else None


def _probe(http: OutputHttp, ip: str, port: int, per_host_s: float) -> bool:
    try:
        response = http.post(
            f"http://{ip}:{port}/post", json={"Command": "Channel/GetAllConf"}, timeout=(per_host_s, per_host_s)
        )
        return response.status_code == 200 and is_pixoo_reply(response.json())
    except Exception:  # unreachable, fenced, not JSON: not a Pixoo
        return False


def sweep(
    http: OutputHttp,
    targets: list[tuple[str, int]],
    *,
    per_host_s: float = DISCOVERY_PER_HOST_S,
    total_s: float = DISCOVERY_TOTAL_S,
    concurrency: int = DISCOVERY_CONCURRENCY,
) -> list[dict[str, Any]]:
    """Ask every ``(ip, port)`` in *targets* for its config, at most
    *concurrency* at once; the Pixoos that answered by *total_s*, in target order.

    Goes through *http*, so ``FIESTABOARD_OUTPUTS_ALLOW_HOSTS`` applies: with
    it set, only listed hosts are asked.
    """
    deadline = time.monotonic() + total_s
    pool = ThreadPoolExecutor(max_workers=max(1, concurrency), thread_name_prefix="pixoo-sweep")
    futures = {pool.submit(_probe, http, ip, port, per_host_s): i for i, (ip, port) in enumerate(targets)}
    hits: dict[int, dict[str, Any]] = {}
    pending = set(futures)
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
        for future in done:
            if future.result():
                index = futures[future]
                hits[index] = _device(*targets[index])
    # Probes still on the wire end within their own timeout; nobody waits.
    pool.shutdown(wait=False, cancel_futures=True)
    return [hits[i] for i in sorted(hits)]


def _targets(networks: list[ipaddress.IPv4Network], port: int) -> list[tuple[str, int]]:
    seen: set[str] = set()
    out = []
    for network in networks:
        for address in network.hosts():
            ip = str(address)
            if ip not in seen:
                seen.add(ip)
                out.append((ip, port))
    return out


def _fence_guidance() -> list[str]:
    if os.environ.get(_ALLOW_ENV, "").strip():
        return [
            f"{_ALLOW_ENV} is set, so only the hosts it lists are contacted. "
            "Development setups set it; unset it (or set it empty) to search your network."
        ]
    return []


def _token(cell: Any) -> BoardToken:
    """One cell as a board token: a rich cell as is, a 0-71 code projected."""
    return cell if isinstance(cell, BoardToken) else cells_from_codes([[int(cell)]])[0][0]


@lru_cache(maxsize=4)
def _takes_layers(layout: Any) -> bool:
    """Whether *layout* (core's ``layout_message``) draws bitmap layers:
    FiestaBoard before pixel canvases has no ``layers`` keyword."""
    return "layers" in inspect.signature(layout).parameters


class DivoomPixoo(OutputPluginBase):
    """One Divoom Pixoo 64 on the local network."""

    plugin_id = "divoom_pixoo"

    # Device pacing, as instance-overridable attributes (tests shorten them).
    RESET_AFTER_PUSHES = RESET_AFTER_PUSHES
    #: Seconds between streamed frames; ``None`` = one frame at the model's
    #: rate (capped at :data:`STREAM_MAX_FPS`). Tests set 0.
    STREAM_STEP_S: float | None = None
    CONNECT_TIMEOUT_S = CONNECT_TIMEOUT_S
    READ_TIMEOUT_S = READ_TIMEOUT_S
    REFIND_INTERVAL_S = REFIND_INTERVAL_S

    def __init__(self, board_id: str | None, config: dict[str, Any]) -> None:
        super().__init__(board_id, config)
        self.host = normalise_host(self.config.get("host"))
        #: The PicID the next upload uses; ``None`` = no session yet (new
        #: instance, or an upload failed or was cut short): reset and re-seed.
        self._next_pic_id: int | None = None
        self._pushes_since_reset = 0
        brightness = self.config.get("brightness")
        self._brightness: int | None = None if brightness is None else max(0, min(100, int(brightness)))
        self._brightness_pending = self._brightness is not None
        self._renderer: tuple[Any, LedLayoutOptions] | None = None
        #: An upload failed and nothing has landed since: the next poll that
        #: hears the device report it back (see :meth:`poll_device`).
        self._upload_failed = False
        #: ``time.monotonic()`` before which no cloud lookup is made again.
        self._next_refind_at = 0.0

    # --- identity --------------------------------------------------------------------------

    def device_key(self) -> str:
        return self.host or super().device_key()

    @property
    def url(self) -> str:
        """The explicit HTTP endpoint, or the standard Pixoo ``/post`` path."""
        raw = str(self.config.get("host") or "").strip()
        try:
            endpoint = urlsplit(raw if _SCHEME.match(raw) else f"http://{raw}")
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.query
                or endpoint.fragment
                or endpoint.port == 0
                # "http:/192.168.1.50" (one slash) parses as host "http" with an
                # empty port; any empty port is a typo, not the default.
                or endpoint.netloc.endswith(":")
            ):
                raise ValueError("unsupported endpoint")
        except ValueError as exc:
            raise requests.exceptions.InvalidURL(
                "Use an HTTP(S) device address with an optional port and path, without credentials, query or fragment."
            ) from exc
        path = endpoint.path if endpoint.path not in {"", "/"} else "/post"
        return urlunsplit((endpoint.scheme, self.host, path, "", ""))

    # --- rendering ---------------------------------------------------------------------------

    def _render_setup(self) -> tuple[Any, LedLayoutOptions]:
        """The board's matrix spec and layout options, from what core resolved:
        its character set and its Tile style / Block padding settings
        (``tile_gap`` / ``block_padding``), checked against the model."""
        if self._renderer is None:
            self._renderer = (led_spec_for_model(self.device_model), self.led_layout_options())
        return self._renderer

    def layout(self, frame: Sequence[Sequence[Any]]) -> Any:
        """*frame* (0-71 codes or rich cells) laid out on the board's matrix,
        with the pixel canvases it carries (``frame.layers``) drawn over it.

        ``layers=`` is passed only when there are some and core's
        ``layout_message`` takes them, so a frame without canvases (and any
        frame on an older core) lays out exactly as it always did.
        """
        spec, options = self._render_setup()
        tokens = [[_token(cell) for cell in row] for row in frame]
        layers = tuple(getattr(frame, "layers", None) or ())
        if layers and _takes_layers(layout_message):
            return layout_message(tokens, spec, options, layers=layers)
        return layout_message(tokens, spec, options)

    def render(self, frame: Sequence[Sequence[Any]]) -> bytes:
        """*frame* as the device's RGB888 pixels, row-major from the top left."""
        return rasterize(self.layout(frame)).pixels

    # --- the device seam ----------------------------------------------------------------------

    def _post(self, payload: dict[str, Any], *, setup: bool = False, cancel: Any = None) -> dict[str, Any]:
        """Send one command through ``self.http``; the device's JSON reply, or raise.

        *cancel* explicitly binds a live write token; requests stop when it is cancelled.

        Raises:
            requests.RequestException: no answer, a fenced host
                (``OutputHostBlocked``), or a cancelled run (``RequestCancelled``).
            PixooStatusError / PixooBadResponse: an answer that is not success.
        """
        if not self.host:
            raise _NoHost("No device address configured")
        extra = {} if cancel is None else {"cancel": cancel}
        response = self.http.post(
            self.url, json=payload, setup=setup, timeout=(self.CONNECT_TIMEOUT_S, self.READ_TIMEOUT_S), **extra
        )
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
        return cancel.wait(seconds)

    def _start_session(self, cancel: CancelToken) -> bool:
        """Reset the device's id counter and seed ours from it (0 means 1).
        False when the run was cancelled between the two requests."""
        self._post({"Command": RESET_GIF_ID}, setup=True)
        if cancel.cancelled:
            return False
        reply = self._post({"Command": GET_GIF_ID}, setup=True)
        seed = reply.get("PicId")
        self._next_pic_id = seed if isinstance(seed, int) and not isinstance(seed, bool) and seed > 0 else 1
        self._pushes_since_reset = 0
        return True

    def _orient_pixels(self, pixels: bytes) -> bytes:
        """Correct mounting clockwise at upload; logical layouts stay upright."""
        angle = self.config.get("orientation", 0)
        if angle == 0:
            return pixels
        oriented = bytearray(len(pixels))
        edge = PIC_WIDTH - 1
        for y in range(PIC_WIDTH):
            for x in range(PIC_WIDTH):
                if angle == 90:
                    target_x, target_y = edge - y, x
                elif angle == 180:
                    target_x, target_y = edge - x, edge - y
                else:  # 270; the board settings schema restricts the choices.
                    target_x, target_y = y, edge - x
                source = (y * PIC_WIDTH + x) * 3
                target = (target_y * PIC_WIDTH + target_x) * 3
                oriented[target : target + 3] = pixels[source : source + 3]
        return bytes(oriented)

    def _upload(self, frames: list[bytes], speeds: list[int], cancel: CancelToken) -> bool | None:
        """Upload *frames* as one GIF, back-to-back, frame *i* shown ``speeds[i]`` ms.

        Returns True when every frame was accepted, None when cancelled first,
        and raises when the device failed. Either of the last two ends the
        session, so the next upload resets and re-seeds.
        """
        count = len(frames)
        try:
            if cancel.cancelled:
                return None
            if self._next_pic_id is None:
                if not self._start_session(cancel):
                    return None
            elif self._pushes_since_reset + count > self.RESET_AFTER_PUSHES:
                self._next_pic_id = None
                self._post({"Command": RESET_GIF_ID}, setup=True)
                self._next_pic_id, self._pushes_since_reset = 1, 0
            pic_id = self._next_pic_id
            assert pic_id is not None
            for offset, (pixels, speed) in enumerate(zip(frames, speeds, strict=True)):
                if cancel.cancelled:
                    self._next_pic_id = None
                    return None
                self._next_pic_id = None  # unknown until this frame is accepted
                self._post(
                    {
                        "Command": SEND_GIF,
                        "PicNum": count,
                        "PicWidth": PIC_WIDTH,
                        "PicOffset": offset,
                        "PicID": pic_id,
                        "PicSpeed": speed,
                        "PicData": base64.b64encode(self._orient_pixels(pixels)).decode("ascii"),
                    }
                )
                self._pushes_since_reset += 1
            # Never reuse or lower an id: the device would ignore it silently.
            self._next_pic_id = pic_id + 1
            return True
        except RequestCancelled:
            # Core's cancel scope refused the request: preempted, not failed.
            self._next_pic_id = None
            return None
        except Exception:
            self._next_pic_id = None
            raise

    def _apply_brightness(self, cancel: CancelToken) -> None:
        """Set the configured brightness once a frame has landed. A failure
        is logged and retried after the next frame; it never fails the write."""
        if not self._brightness_pending or cancel.cancelled:
            return
        try:
            self._post({"Command": SET_BRIGHTNESS, "Brightness": self._brightness}, setup=True)
        except Exception as exc:
            logger.warning("Pixoo %s: setting brightness failed: %s", self.host, exc)
            return
        self._brightness_pending = False

    def _deliver(self, frames: list[bytes], speeds: list[int], cancel: CancelToken) -> WriteResult | None:
        """One upload as a write verdict; ``None`` when it landed."""
        try:
            landed = self._upload(frames, speeds, cancel)
        except Exception as exc:
            logger.warning("Pixoo %s: upload failed: %s", self.host or "(no address)", exc)
            self._upload_failed = True
            return WriteResult(False, False)
        if landed is None:
            # Preempted by a newer frame: not a device failure.
            return WriteResult(True, False)
        self._upload_failed = False
        return None

    def _still(self, pixels: bytes, cancel: CancelToken) -> WriteResult:
        failed = self._deliver([pixels], [STILL_SPEED_MS], cancel)
        if failed is not None:
            return failed
        self._apply_brightness(cancel)
        return WriteResult(True, True)

    def _stream(self, frames: list[bytes], holds_s: list[float], cancel: CancelToken) -> WriteResult:
        """Show *frames* one at a time, frame *i* held ``holds_s[i]`` seconds
        (never less than one stream step); the last one stays.

        Each frame is its own ``PicNum`` 1 push, so it plays once and the last
        lands cleanly. A cancel between frames stops at once: a newer page is
        on its way, and it is not a device failure.
        """
        if not frames:
            return WriteResult(True, False)
        last = len(frames) - 1
        step = self._step_s()
        for index, pixels in enumerate(frames):
            started = time.monotonic()
            failed = self._deliver([pixels], [STILL_SPEED_MS], cancel)
            if failed is not None:
                return failed
            if index < last:
                hold = max(holds_s[index], step) - (time.monotonic() - started)
                if hold > 0 and self._wait(hold, cancel):
                    return WriteResult(True, False)
        self._apply_brightness(cancel)
        return WriteResult(True, True)

    def _fade_through_black(self, before: bytes, target: bytes, cancel: CancelToken) -> WriteResult:
        """Stream a dimmed old image, black, then the brightening target.

        Brightness is a persistent device setting. Using it for a transient
        fade would require requests after cancellation, or leave the device
        dim when a runtime is replaced or stopped. Pixel frames need no such
        cleanup: any successor frame replaces the interrupted transition.
        """
        if cancel.cancelled:
            return WriteResult(True, False)

        def dim(pixels: bytes, numerator: int) -> bytes:
            return bytes(value * numerator // FADE_STEPS for value in pixels)

        frames = [dim(before, level) for level in range(FADE_STEPS - 1, -1, -1)]
        frames.extend(dim(target, level) for level in range(1, FADE_STEPS + 1))
        return self._stream(frames, [0.0] * len(frames), cancel)

    def _step_s(self) -> float:
        """The shortest gap between streamed frames."""
        return 1.0 / self._stream_fps() if self.STREAM_STEP_S is None else self.STREAM_STEP_S

    def _stream_fps(self) -> float:
        """Frames a second to plan a transition at: the model's rate, capped by the device's."""
        animation = (self.device_model or {}).get("animation") if isinstance(self.device_model, dict) else None
        rate = animation.get("maxFps") if isinstance(animation, dict) else None
        return float(min(rate, STREAM_MAX_FPS)) if isinstance(rate, (int, float)) and rate > 0 else STREAM_MAX_FPS

    # --- writes --------------------------------------------------------------------------------

    def write(self, frame: CellFrame, *, native: Any, cancel: CancelToken) -> WriteResult:
        return self._still(self.render(frame), cancel)

    def write_cells(self, cells: RichCellFrame, *, native: Any, cancel: CancelToken) -> WriteResult:
        return self._still(self.render(cells), cancel)

    def write_transition(
        self,
        before: RichCellFrame,
        after: RichCellFrame,
        transition: ResolvedLedTransition,
        *,
        cancel: CancelToken,
    ) -> WriteResult:
        """Show *after* through the transition core resolved for this board.

        ``none`` is one still push. A fade goes through black unless the
        board's ``fade_style`` is ``blend``. Every other kind is planned with
        core's renderer at the stream rate and played as paced stills.
        """
        if transition.spec == "none":
            return self._still(self.render(after), cancel)
        spec = transition.spec
        if getattr(spec, "kind", None) == "fade" and self.config.get("fade_style", "black") != "blend":
            return self._fade_through_black(self.render(before), self.render(after), cancel)
        if spec.kind != "flip" and spec.duration_ms is None:
            spec = replace(spec, duration_ms=STREAM_TRANSITION_MS)
        planned = plan_transition(self.layout(before), self.layout(after), spec)
        fps = self._stream_fps()
        # Frame 0 is *before*, which the device already shows.
        frames = [f.pixels for f in transition_frames(planned, fps=fps)][1:]
        target = planned.to_frame.pixels
        if not frames or frames[-1] != target:
            frames.append(target)
        return self._stream(frames, [0.0] * len(frames), cancel)

    def write_sequence(self, frames: list[TimedFrame], *, cancel: CancelToken) -> WriteResult:
        """*frames* as paced stills, each held its own duration (never shorter
        than one stream step), at most :data:`SEQUENCE_MAX_FRAMES`
        (compressed evenly, first and last kept); the last one stays."""
        if not frames:
            return WriteResult(True, False)
        if len(frames) > SEQUENCE_MAX_FRAMES:
            last = len(frames) - 1
            picks = sorted({round(i * last / (SEQUENCE_MAX_FRAMES - 1)) for i in range(SEQUENCE_MAX_FRAMES)})
            frames = [frames[i] for i in picks]
        pixels = [self.render(f.frame) for f in frames]
        return self._stream(pixels, [max(0, int(f.duration_ms)) / 1000.0 for f in frames], cancel)

    # --- finding the device ------------------------------------------------------------------

    DISCOVERY_PORT = DISCOVERY_PORT
    DISCOVERY_PER_HOST_S = DISCOVERY_PER_HOST_S

    @classmethod
    def discover(cls, timeout: float, hint: str | None = None) -> list[dict]:
        """Core's discover hook: sweep the browser's network (*hint*), then
        FiestaBoard's own, for Pixoos. Bounded by *timeout* (at most 10 s)."""
        networks = candidate_subnets(hint_host=hint, local_ip=local_ipv4())
        return sweep(
            OutputHttp(),
            _targets(networks, DISCOVERY_PORT),
            per_host_s=DISCOVERY_PER_HOST_S,
            total_s=min(float(timeout), DISCOVERY_TOTAL_S),
        )

    def action_find_pixoo(self, inputs: dict[str, Any]) -> ActionOutcome:
        """"Find my Pixoo": sweep the given network, the browser's, then FiestaBoard's own."""
        try:
            networks = candidate_subnets(
                subnet=inputs.get("subnet") or None, hint_host=inputs.get("hint_host"), local_ip=local_ipv4()
            )
        except ValueError as exc:
            return ActionOutcome(status="error", message=f"Cannot search that network: {exc}.")
        manual = [
            "Type the Pixoo's address instead: in the Divoom app, open the device's settings, "
            "or look for it in your router's list of connected devices.",
            "Or enter your network, for example 192.168.1.0/24, and search again.",
        ]
        if not networks:
            return ActionOutcome(
                status="warning", message="FiestaBoard could not tell which network to search.", guidance=tuple(manual), devices=()
            )
        found = sweep(
            self.http, _targets(networks, self.DISCOVERY_PORT), per_host_s=self.DISCOVERY_PER_HOST_S
        )
        searched = ", ".join(str(n) for n in networks)
        if not found:
            return ActionOutcome(
                status="warning",
                message=f"No Pixoo found on {searched}.",
                guidance=tuple(_fence_guidance() + ["Check that the Pixoo is on and on the same network."] + manual),
                devices=(),
            )
        return ActionOutcome(message=f"Found {len(found)} Pixoo(s) on {searched}.", devices=tuple(found))

    def action_cloud_lookup(self, inputs: dict[str, Any]) -> ActionOutcome:
        """Opt-in: ask Divoom's cloud which Pixoos share this network's public address.

        Reply (lab-verified): ``{"ReturnCode": 0, "ReturnMessage": "", "DeviceList":
        [{"DeviceName", "DeviceId", "DevicePrivateIP", "DeviceMac", "Hardware"}]}``
        for an empty request body. Only the local address, name and MAC are kept.
        With a saved ``mac`` it re-finds that device (its address may have changed
        under DHCP); a single device found fills in ``host`` and ``mac``.
        """
        failed = "Divoom's servers did not return any Pixoo."
        try:
            devices = self._cloud_devices()
        except OutputHostBlocked:
            return ActionOutcome(
                status="error", message="Not contacted: Divoom's servers are not allowed.", guidance=tuple(_fence_guidance())
            )
        except (requests.RequestException, ValueError):
            return ActionOutcome(status="error", message="Could not reach Divoom's servers.")
        if devices is None:
            return ActionOutcome(status="error", message=failed)
        if not devices:
            return ActionOutcome(status="warning", message=failed, devices=())

        saved = normalise_mac(self.config.get("mac"))
        if saved is not None:
            match = next((d for d in devices if d["mac"] == saved), None)
            if match is None:
                return ActionOutcome(
                    status="warning",
                    message="Divoom's servers did not list the Pixoo saved for this board; these are the ones they did.",
                    devices=tuple(devices),
                )
            return ActionOutcome(
                message=f"Found this board's Pixoo at {match['host']}.",
                fields=self._fills(match),
                devices=(match,),
            )
        fields = self._fills(devices[0]) if len(devices) == 1 else {}
        return ActionOutcome(
            message=f"Divoom's servers listed {len(devices)} Pixoo(s).", fields=fields, devices=tuple(devices)
        )

    def _cloud_devices(self) -> list[dict[str, Any]] | None:
        """The Pixoos Divoom's cloud lists on this network's public address, each
        with ``host``, ``name`` and ``mac``; ``None`` when the reply is not a list.

        Raises:
            OutputHostBlocked: Divoom's servers are not allowed.
            requests.RequestException / ValueError: no answer, or not JSON.
        """
        response = self.http.post(DIVOOM_LAN_LOOKUP_URL, json={})
        body = response.json() if response.status_code == 200 else None
        if not isinstance(body, dict) or body.get("ReturnCode") != 0:
            return None
        devices = []
        for entry in body.get("DeviceList") or []:
            if not isinstance(entry, dict) or not entry.get("DevicePrivateIP"):
                continue
            ip, name = str(entry["DevicePrivateIP"]), str(entry.get("DeviceName") or "Pixoo")
            devices.append({**_device(ip, 80, name, f"{name} ({ip})"), "mac": normalise_mac(entry.get("DeviceMac"))})
        return devices

    @staticmethod
    def _fills(found: dict[str, Any]) -> dict[str, ActionField]:
        fields = {"host": ActionField(found["host"])}
        if found.get("mac"):
            fields["mac"] = ActionField(found["mac"])
        return fields

    # --- probes -----------------------------------------------------------------------------

    def poll_device(self) -> Any:
        """Core's between-writes look at the device (core's ``poll_device`` hook).

        The Pixoo cannot say what it shows, but it can say it restarted: a
        reboot wipes FiestaBoard's image back to the device's own clock face
        AND resets its picture-id counter (``Draw/GetHttpGifId`` answered 1 on
        the bench straight after a power-cycle). Reported as a restart, so
        core re-sends the board's content even though the page has not
        changed, when:

        - the device's next id is below the one this instance would upload
          next: it forgot every id since our last upload;
        - it answers again after an upload failed: whatever it shows now,
          core's last frame never reached it.

        When the saved address does not answer and a ``mac`` is saved (the
        Divoom cloud lookup fills it in), the Pixoo may have come back on a
        new DHCP address: Divoom's cloud is asked where that MAC is (at most
        every :data:`REFIND_INTERVAL_S`), and a Pixoo answering there becomes
        this board's ``host`` — reported for core to save.
        """
        if DevicePoll is None or not self.host:
            return None
        try:
            reply = self._post({"Command": GET_GIF_ID}, setup=True)
        except OutputHostBlocked:
            return None
        except requests.RequestException:
            return self._refind()
        except Exception:
            return None
        device_next = reply.get("PicId")
        forgot = (
            isinstance(device_next, int)
            and not isinstance(device_next, bool)
            and self._next_pic_id is not None
            and device_next < self._next_pic_id
        )
        if not (forgot or self._upload_failed):
            return None
        logger.info(
            "Pixoo %s: %s; asking for the board's content again",
            self.host,
            "restarted (its picture ids were reset)" if forgot else "answering again after a failed upload",
        )
        self._restart_session()
        return DevicePoll(restarted=True)

    def _refind(self) -> Any:
        """Find this board's Pixoo by its saved MAC after its address stopped answering."""
        # Cloud entries name the native port-80 API, not a custom endpoint.
        # Do not silently replace its port/path or persist a different endpoint.
        try:
            endpoint = urlsplit(self.url)
        except requests.exceptions.InvalidURL:
            return None
        if endpoint.scheme != "http" or endpoint.port not in {None, 80} or endpoint.path != "/post":
            return None
        mac = normalise_mac(self.config.get("mac"))
        if mac is None or time.monotonic() < self._next_refind_at:
            return None
        self._next_refind_at = time.monotonic() + self.REFIND_INTERVAL_S
        try:
            devices = self._cloud_devices() or []
        except (requests.RequestException, ValueError):
            return None
        match = next((d for d in devices if d["mac"] == mac), None)
        if match is None or match["host"] == self.host:
            return None
        old, self.host = self.host, match["host"]
        try:
            self._post({"Command": GET_GIF_ID}, setup=True)
        except Exception:
            self.host = old
            return None
        logger.info("Pixoo %s: not answering; found it by its MAC at %s", old, self.host)
        self.config["host"] = self.host
        self._restart_session()
        return DevicePoll(restarted=True, config={"host": self.host})

    def _restart_session(self) -> None:
        """Start over as if new: re-seed picture ids, re-apply brightness."""
        self._next_pic_id = None
        self._upload_failed = False
        self._brightness_pending = self._brightness is not None

    def check_connection(self) -> ConnectionCheck:
        where = self.host or "the Pixoo"
        try:
            body = self._post({"Command": GET_ALL_CONF})
        except OutputHostBlocked:
            return ConnectionCheck.blocked(self.host)
        except _NoHost:
            return self._failed("unreachable", "No device address is set.", "No address", "Enter the Pixoo's IP address.")
        except requests.exceptions.InvalidURL as exc:
            # Not contacted: the address is refused before any request. The
            # raw address is not echoed back, since it may carry credentials.
            return self._failed(
                "blocked",
                "Not contacted: this is not a device address the Pixoo plugin can use.",
                str(exc),
                "Enter the Pixoo's IP address or hostname, or an HTTP or HTTPS address with an optional"
                " port and path (for example 192.168.1.50 or http://192.168.1.50:9000/divoom_api).",
                "Leave out any user name or password, ?query and #fragment; a port must be 1-65535.",
            )
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
