"""Divoom Pixoo 64 output plugin for FiestaBoard.

Drives one Pixoo 64 over its local HTTP API (``POST http://<host>/post``,
one JSON command per request). Core owns the policy — the send floor
(``min_interval_ms``, 1 s between writes), dedupe, preemption, the write
budget and which LED transition a board runs — and this plugin moves pixels:

- **Render.** Frames are laid out with core's LED renderer (``src.led``,
  through ``src.plugins``) for the board's device model and character set
  (``self.device_model``, ``self.character_set``: 3x5 face, 10 x 16 cells
  on 64 x 64) and rasterised to RGB888. A frame is 0-71 codes
  (:meth:`write`) or rich cells (:meth:`write_cells`: colour spans, blocks,
  icons); both go through :meth:`DivoomPixoo.render`.
- **write / write_cells** push one still frame: ``Draw/SendHttpGif`` with
  ``PicNum`` 1.
- **write_transition** renders exactly the LED transition core resolved for
  the board (``resolve_led_transition`` for its model: the flip FiestaUI
  previews, already fitted to 32 frames of >= 80 ms), uploads it as ONE
  multi-frame GIF (same ``PicID``, ``PicOffset`` 0..n-1), then — once it has
  played — pushes the target as a still, so the device never loops it.
- **write_sequence** uploads a transition plugin's frames the same way.
- **PicID policy.** ``Draw/ResetHttpGifId`` before the first upload of an
  instance, before every animation, after any failed or cancelled upload,
  and once :data:`RESET_AFTER_PUSHES` frames went up since the last reset.

All device traffic goes through ``self.http`` (core's helper: the
``FIESTABOARD_OUTPUTS_ALLOW_HOSTS`` fence, no redirects, the run's cancel
token) with ``(connect, read)`` timeouts; the reset and brightness commands
are marked ``setup=True``. Waits (pacing, the play-out) wait on the cancel
token. Device failures come back as a failed :class:`WriteResult`, never as
an exception.

Several device facts below are community-reported and **unverified** on
current firmware; each is a named constant, listed in the README's
"Verify on your device" section.
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import os
import re
import socket
import time
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

import requests

from src.plugins import (
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

logger = logging.getLogger(__name__)

__all__ = ["DivoomPixoo", "PixooError", "candidate_subnets", "is_pixoo_reply", "sweep"]

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

#: ``(connect, read)`` timeouts for one command.
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
        network = ipaddress.IPv4Network(f"{address}/24", strict=False)
        if not any(network.subnet_of(n) for n in found):
            found.append(network)
    return found


def _device(ip: str, port: int, name: str = "Pixoo 64", label: str | None = None) -> dict[str, Any]:
    return {
        "ip": ip,
        "port": port,
        "host": ip if port == 80 else f"{ip}:{port}",
        "label": label or f"{name} at {ip}",
        "hostname": name,
    }


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
        brightness = self.config.get("brightness")
        self._brightness: int | None = None if brightness is None else max(0, min(100, int(brightness)))
        self._brightness_pending = self._brightness is not None
        self._renderer: tuple[Any, LedLayoutOptions] | None = None

    # --- identity --------------------------------------------------------------------------

    def device_key(self) -> str:
        return self.host or super().device_key()

    @property
    def url(self) -> str:
        return f"http://{self.host}/post"

    # --- rendering ---------------------------------------------------------------------------

    def _render_setup(self) -> tuple[Any, LedLayoutOptions]:
        """The board's matrix spec and layout options, from what core resolved."""
        if self._renderer is None:
            self._renderer = (
                led_spec_for_model(self.device_model),
                LedLayoutOptions(charset=self.character_set),
            )
        return self._renderer

    def layout(self, frame: Sequence[Sequence[Any]]) -> Any:
        """*frame* (0-71 codes or rich cells) laid out on the board's matrix."""
        spec, options = self._render_setup()
        return layout_message([[_token(cell) for cell in row] for row in frame], spec, options)

    def render(self, frame: Sequence[Sequence[Any]]) -> bytes:
        """*frame* as the device's RGB888 pixels, row-major from the top left."""
        return rasterize(self.layout(frame)).pixels

    # --- the device seam ----------------------------------------------------------------------

    def _post(self, payload: dict[str, Any], *, setup: bool = False) -> dict[str, Any]:
        """Send one command through ``self.http``; the device's JSON reply, or raise.

        Raises:
            requests.RequestException: no answer, a fenced host
                (``OutputHostBlocked``), or a cancelled run (``RequestCancelled``).
            PixooStatusError / PixooBadResponse: an answer that is not success.
        """
        if not self.host:
            raise _NoHost("No device address configured")
        response = self.http.post(
            self.url, json=payload, setup=setup, timeout=(self.CONNECT_TIMEOUT_S, self.READ_TIMEOUT_S)
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
        if seconds > 0:
            return cancel.wait(seconds)
        return cancel.cancelled

    def _upload(self, frames: list[bytes], speeds: list[int], cancel: CancelToken) -> bool | None:
        """Upload *frames* as one GIF, frame *i* shown ``speeds[i]`` ms.

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
                self._post({"Command": RESET_GIF_ID}, setup=True)
                posted = True
                self._next_pic_id, self._pushes_since_reset = 1, 0
            pic_id = self._next_pic_id
            for offset, (pixels, speed) in enumerate(zip(frames, speeds, strict=True)):
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
                        "PicSpeed": speed,
                        "PicData": base64.b64encode(pixels).decode("ascii"),
                    }
                )
                posted = True
                self._pushes_since_reset += 1
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
            return WriteResult(False, False)
        if landed is None:
            # Preempted by a newer frame: not a device failure.
            return WriteResult(True, False)
        return None

    def _still(self, pixels: bytes, cancel: CancelToken) -> WriteResult:
        failed = self._deliver([pixels], [STILL_SPEED_MS], cancel)
        if failed is not None:
            return failed
        self._apply_brightness(cancel)
        return WriteResult(True, True)

    def _animate(self, frames: list[bytes], speeds: list[int], target: bytes, cancel: CancelToken) -> WriteResult:
        """Upload an animation as one GIF, let it play, then show *target* still."""
        if len(frames) < 2:
            return self._still(target, cancel)
        failed = self._deliver(frames, speeds, cancel)
        if failed is not None:
            return failed
        # The device loops an uploaded GIF; once it has shown (after its
        # loading overlay) and played through, replace it with the still target.
        if self._wait(self.LOADING_OVERLAY_S + sum(speeds) / 1000.0, cancel):
            self._next_pic_id = None
            return WriteResult(True, False)
        return self._still(target, cancel)

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
        """Play exactly the transition core resolved for the board, then rest on *after*."""
        planned = plan_transition(self.layout(before), self.layout(after), transition.spec)
        # Core fits the spec to the device (frame budget, minFrameMs), so the
        # plan is sequenced: one frame per step of duration / (frames - 1).
        step_ms = self._min_frame_ms()
        if planned.frame_count and planned.frame_count > 1:
            step_ms = max(step_ms, round(planned.duration_ms / (planned.frame_count - 1)))
        # Frame 0 is *before*, which the device already shows.
        frames = [f.pixels for f in transition_frames(planned, fps=1000 / max(1, step_ms))][1:]
        return self._animate(frames, [step_ms] * len(frames), planned.to_frame.pixels, cancel)

    def write_sequence(self, frames: list[TimedFrame], *, cancel: CancelToken) -> WriteResult:
        """A transition plugin's frames, as given (core already fitted them to
        ``maxFrames``), each shown at least the model's ``minFrameMs``."""
        if not frames:
            return WriteResult(True, False)
        floor = self._min_frame_ms()
        pixels = [self.render(f.frame) for f in frames]
        speeds = [max(floor, int(f.duration_ms)) for f in frames]
        return self._animate(pixels, speeds, pixels[-1], cancel)

    def _min_frame_ms(self) -> int:
        model = self.device_model or {}
        return int((model.get("animation") or {}).get("minFrameMs") or 0)

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
        """Opt-in: ask Divoom's cloud which Pixoos share this network's public address."""
        failed = "Divoom's servers did not return any Pixoo."
        try:
            response = self.http.post(DIVOOM_LAN_LOOKUP_URL, json={})
            body = response.json() if response.status_code == 200 else None
        except OutputHostBlocked:
            return ActionOutcome(status="error", message="Not contacted: Divoom's servers are not allowed.", guidance=tuple(_fence_guidance()))
        except (requests.RequestException, ValueError):
            return ActionOutcome(status="error", message="Could not reach Divoom's servers.")
        if not isinstance(body, dict) or body.get("ReturnCode") != 0:
            return ActionOutcome(status="error", message=failed)
        devices = [
            _device(str(d["DevicePrivateIP"]), 80, str(d.get("DeviceName") or "Pixoo"),
                    f"{d.get('DeviceName') or 'Pixoo'} ({d['DevicePrivateIP']})")
            for d in body.get("DeviceList") or []
            if isinstance(d, dict) and d.get("DevicePrivateIP")
        ]
        if not devices:
            return ActionOutcome(status="warning", message=failed, devices=())
        return ActionOutcome(message=f"Divoom's servers listed {len(devices)} Pixoo(s).", devices=tuple(devices))

    # --- probes -----------------------------------------------------------------------------

    def check_connection(self) -> ConnectionCheck:
        where = self.host or "the Pixoo"
        try:
            body = self._post({"Command": GET_ALL_CONF})
        except OutputHostBlocked:
            return ConnectionCheck.blocked(self.host)
        except _NoHost:
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
