"""Divoom Pixoo 64 output plugin for FiestaBoard.

Drives one Pixoo 64 over its local HTTP API (``POST http://<host>/post``,
one JSON command per request). Core owns the policy — the send floor
(``min_interval_ms``, 1 s between writes), dedupe, preemption, the write
budget and which LED transition a board runs — and this plugin moves pixels.

What the 2026-10-04 hardware lab (camera-timed, on a real Pixoo 64) settled:

- **The Pixoo snaps.** An uploaded animation loops forever (there is no
  play-once), an upload of more than ~3 frames shows a "LOADING" overlay
  (~6 s for 40 frames), and landing on a still after an animation glitches
  for ~5 s. A single-frame push shows in ~0.5 s with no overlay. So the
  device model streams at ``maxFps`` 2, under every LED transition's
  minimum: the resolved transition is ``none`` and preview and device both
  cut to the new page.
- **write / write_cells / write_transition** push one still frame:
  ``Draw/SendHttpGif`` with ``PicNum`` 1. Text is drawn into the image
  (``Draw/SendHttpText`` is an overlay that every new image wipes).
- **write_sequence** (only an explicit caller; core never asks this model)
  uploads up to :data:`SEQUENCE_MAX_FRAMES` frames back-to-back as one GIF,
  which LOOPS on the device; once it is ready and has played once, the
  target is pushed as a still — expect the ~5 s landing glitch.
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

logger = logging.getLogger(__name__)

__all__ = ["DivoomPixoo", "PixooError", "candidate_subnets", "is_pixoo_reply", "sweep"]

# --- device facts (each one: see README "Verify on your device") ----------------------------

#: Frame POSTs between two ``Draw/ResetHttpGifId``: the community convention
#: (SomethingWithComputers/pixoo resets every 32). The reported freeze after
#: ~300 pushes without a reset was NOT tested on hardware (the lab stayed
#: under 25 ids between resets); a reset needs no wait afterwards (verified).
RESET_AFTER_PUSHES = 32

#: The most frames one explicit sequence upload carries. The lab saw 59
#: frames accepted but playback wrap at ~55 (later frames silently dropped);
#: 40 played completely, and community reports crashes above ~40.
SEQUENCE_MAX_FRAMES = 40

#: The shortest ``PicSpeed`` an upload asks for (80 ms was honoured).
MIN_FRAME_MS = 80

#: After the last frame of a multi-frame upload, the LOADING overlay stays
#: ~0.5 s before the animation shows (lab-measured).
ANIMATION_READY_S = 0.5

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


class DivoomPixoo(OutputPluginBase):
    """One Divoom Pixoo 64 on the local network."""

    plugin_id = "divoom_pixoo"

    # Device pacing, as instance-overridable attributes (tests shorten them).
    RESET_AFTER_PUSHES = RESET_AFTER_PUSHES
    ANIMATION_READY_S = ANIMATION_READY_S
    CONNECT_TIMEOUT_S = CONNECT_TIMEOUT_S
    READ_TIMEOUT_S = READ_TIMEOUT_S

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

    # --- identity --------------------------------------------------------------------------

    def device_key(self) -> str:
        return self.host or super().device_key()

    @property
    def url(self) -> str:
        return f"http://{self.host}/post"

    # --- rendering ---------------------------------------------------------------------------

    def _render_setup(self) -> tuple[Any, LedLayoutOptions]:
        """The board's matrix spec and layout options, from what core resolved:
        its character set and its Tile style / Block padding settings
        (``tile_gap`` / ``block_padding``), checked against the model."""
        if self._renderer is None:
            self._renderer = (led_spec_for_model(self.device_model), self.led_layout_options())
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
                        "PicData": base64.b64encode(pixels).decode("ascii"),
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

    def _sequence(self, frames: list[bytes], speeds: list[int], target: bytes, cancel: CancelToken) -> WriteResult:
        """The explicit sequence path: one looping GIF, then *target* still.

        The device loops the GIF forever; once it is ready (~0.5 s after the
        last frame) and has played through once, *target* replaces it. Expect
        ~5 s in which the old loop keeps running with frame 0 replaced.
        """
        if len(frames) < 2:
            return self._still(target, cancel)
        if len(frames) > SEQUENCE_MAX_FRAMES:
            last = len(frames) - 1
            picks = sorted({round(i * last / (SEQUENCE_MAX_FRAMES - 1)) for i in range(SEQUENCE_MAX_FRAMES)})
            frames, speeds = [frames[i] for i in picks], [speeds[i] for i in picks]
        failed = self._deliver(frames, speeds, cancel)
        if failed is not None:
            return failed
        if self._wait(self.ANIMATION_READY_S + sum(speeds) / 1000.0, cancel):
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
        """Show *after*. For the Pixoo model core resolves ``none``: one still push.

        An explicit transition spec (a caller overriding the model) is planned
        with core's renderer and goes through the looping sequence path.
        """
        if transition.spec == "none":
            return self._still(self.render(after), cancel)
        planned = plan_transition(self.layout(before), self.layout(after), transition.spec)
        step_ms = MIN_FRAME_MS
        if planned.frame_count and planned.frame_count > 1:
            step_ms = max(step_ms, round(planned.duration_ms / (planned.frame_count - 1)))
        # Frame 0 is *before*, which the device already shows.
        frames = [f.pixels for f in transition_frames(planned, fps=1000 / step_ms)][1:]
        return self._sequence(frames, [step_ms] * len(frames), planned.to_frame.pixels, cancel)

    def write_sequence(self, frames: list[TimedFrame], *, cancel: CancelToken) -> WriteResult:
        """Frames as one looping GIF (at most :data:`SEQUENCE_MAX_FRAMES`,
        compressed evenly, first and last kept), each shown at least
        :data:`MIN_FRAME_MS`, then the last frame as a still."""
        if not frames:
            return WriteResult(True, False)
        pixels = [self.render(f.frame) for f in frames]
        speeds = [max(MIN_FRAME_MS, int(f.duration_ms)) for f in frames]
        return self._sequence(pixels, speeds, pixels[-1], cancel)

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
            response = self.http.post(DIVOOM_LAN_LOOKUP_URL, json={})
            body = response.json() if response.status_code == 200 else None
        except OutputHostBlocked:
            return ActionOutcome(
                status="error", message="Not contacted: Divoom's servers are not allowed.", guidance=tuple(_fence_guidance())
            )
        except (requests.RequestException, ValueError):
            return ActionOutcome(status="error", message="Could not reach Divoom's servers.")
        if not isinstance(body, dict) or body.get("ReturnCode") != 0:
            return ActionOutcome(status="error", message=failed)
        devices = []
        for entry in body.get("DeviceList") or []:
            if not isinstance(entry, dict) or not entry.get("DevicePrivateIP"):
                continue
            ip, name = str(entry["DevicePrivateIP"]), str(entry.get("DeviceName") or "Pixoo")
            devices.append({**_device(ip, 80, name, f"{name} ({ip})"), "mac": normalise_mac(entry.get("DeviceMac"))})
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

    @staticmethod
    def _fills(found: dict[str, Any]) -> dict[str, ActionField]:
        fields = {"host": ActionField(found["host"])}
        if found.get("mac"):
            fields["mac"] = ActionField(found["mac"])
        return fields

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
