# Divoom Pixoo 64 Output Plugin

Shows your FiestaBoard pages on a Divoom Pixoo 64 LED matrix over your local network.

**→ [Setup Guide](./docs/SETUP.md)**

> **Beta.** This plugin needs FiestaBoard 10.0.0 or later with **Settings → Beta → Output
> Plugins** turned on. Its device behaviour was measured on a real Pixoo 64 on 2026-10-04 and
> 2026-10-05; two community reports are still unverified. See [Verify on your device](#verify-on-your-device).

## Overview

The Pixoo 64 is a 64×64 RGB LED matrix with a local HTTP API. This output plugin turns each
FiestaBoard page into pixels with FiestaBoard's own LED renderer and pushes them to the device.
Page changes animate: FiestaBoard's LED transitions (flip, slide, wipe, cascade, dissolve, fade)
are streamed to the panel as single frames at five a second, and fade dims the screen through
black with the brightness command (see [How it works](#how-it-works)).

## Device

| | |
| --- | --- |
| Device model | `divoom_pixoo64` (from [`output/device-models.json`](./output/device-models.json)) |
| Matrix | 64 × 64 pixels, 24-bit RGB |
| Character grid | **10 rows × 16 columns** (3×5 font with 1-pixel gaps) |
| Character set | `led_3x5`: A–Z, 0–9, board punctuation, colour tiles |
| Connection | `POST http://<host>/post` on your LAN; no account, no cloud, no password |
| Write spacing | At least 1 second between writes (enforced by FiestaBoard, not the plugin); 0.5 s verified safe |
| Animation | `stream` at 5 frames a second: LED transitions are sent as paced single frames; flip is the default |
| Read back | None: the device cannot report what it shows |

A 10 × 16 grid is larger than the 3 × 15 minimum every FiestaBoard board must reach, so pages
written for a Vestaboard Note fit. The smaller Pixoo 16 and Pixoo 32 cannot fit 3 × 15 and are not
supported.

## What it shows

FiestaBoard lays out each page on the 10 × 16 grid and the plugin draws it:

- Letters, digits and punctuation in the 3×5 font, in white on black
- Colour tiles `{63}`–`{68}` (red through violet) as solid cells in the board colours, `{69}` as
  a white cell, and `{70}`/`{71}` (black) as unlit cells
- Colour spans (`{red:HOT}`) as letters in that colour, block spans as lit backgrounds, and icons
  (`{icon:sun}`) as their 3×5 pictures
- Characters the 3×5 font has no glyph for as FiestaBoard's fallback for the `led_3x5` set

For example, this page:

```text
{63}{63} WEATHER {63}{63}
SUNNY 72
```

shows two red cells, `WEATHER` and two more red cells on the first row, and `SUNNY 72` on the
second.

FiestaBoard sends the Pixoo rich cells (each cell's glyph, colour and background, already fitted
to the `led_3x5` character set), so colours and icons in a page reach the screen.

## Configuration

| Setting | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `host` | string | Yes | — | The Pixoo's IP address or hostname, for example `192.168.1.50`. A port (`192.168.1.50:80`) is accepted; `http://` and any path are ignored. |
| `brightness` | integer, 0–100 | No | the device's own | Screen brightness, set after the first frame lands. Leave it out to keep what you set in the Divoom app. |
| `tile_gap` | `"gap"` or `"fill"` | No | `"gap"` | **Tile style** on the board screen. Gaps keeps the one-pixel line between neighbouring color tiles; Seamless lights it when both are the same color, so a row of tiles reads as one solid bar. A corner pixel lights only when all four cells around it match. |
| `block_padding` | `0` or `1` | No | `0` | **Block padding** on the board screen. 1 pixel grows the background of highlighted text (`{black/white:TEXT}`) one pixel on every side, so the letters never touch its edge. It never covers a neighbouring character, and a pixel between two different colors stays dark. |
| `fade_style` | `"black"` or `"blend"` | No | `"black"` | **Fade style**: how the Fade transition looks. Through black dims the screen to black, swaps the page and brightens it again (about a second, smooth). Blend cross-fades the two pages in a few frames. |
| `mac` | string, 12 hex digits | No | — | The Pixoo's MAC address. Filled in by the Divoom cloud lookup, which then uses it to find this Pixoo again after its IP address changes. |

No setting is secret: the Pixoo's local API has no authentication.

## Finding your Pixoo

**Find my Pixoo** (the search button next to the address field) looks for a Pixoo 64 on your
local network. It asks every address of a /24 network for the Pixoo's configuration
(`POST /post` `Channel/GetAllConf` on port 80), at most 64 at a time with 0.8 s per address and
10 s in all, and lists what answers like a Pixoo. Nothing outside your network is contacted.

It searches, in order:

1. a network you type, for example `192.168.1.0/24` (a /22 at most, private addresses only);
2. the network of the address you opened FiestaBoard at, when the settings screen passes it;
3. the network FiestaBoard itself is on. In Docker's default bridge mode that is a container
   network, so the first two matter.

If nothing is found, type the address yourself: it is in the Divoom app under the device's
settings, or in your router's list of connected devices.

**Ask Divoom's servers which Pixoos are on your network** is a separate, opt-in button. It sends
one request to Divoom's cloud (`app.divoom-gz.com`). Divoom sees your public IP address and
answers with the Pixoos registered from it; the plugin keeps only each one's local address,
name and MAC address. It runs only when you click it. When it finds exactly one Pixoo it fills in
the address and the MAC; with a MAC saved, it finds that Pixoo again at its current address (handy
when your router hands it a new one). The local network search cannot see MAC addresses, and the
Pixoo does not advertise itself over mDNS (checked).

Both go through FiestaBoard's device helper, so `FIESTABOARD_OUTPUTS_ALLOW_HOSTS` applies. It is
unset in production. Development setups set it to their mocks, which blocks a sweep: set it
empty (or add your Pixoo's address) to search a real network.

## Features

- Sends FiestaBoard pages to a Pixoo 64 on your local network, with no cloud service
- **Find my Pixoo**: searches your network for the device; manual entry always works
- Optional, clearly labelled lookup through Divoom's cloud when the search finds nothing
- Animated page changes: every FiestaBoard LED transition, streamed as single frames at five a
  second with no loading screen and no looping, always landing on the new page
- **Fade style**: a smooth fade through black using the panel's brightness, or a pixel blend
- Text and colours drawn into the image (the device's own text command is an overlay that every
  new image wipes)
- Keeps the device's picture ids increasing, so no update is silently ignored, and resets them
  every 32 pushes
- Gives up a write as soon as a newer page arrives, between any two requests
- Connection test that tells unreachable, timed-out, wrong-device and blocked-host cases apart
- Optional brightness setting
- **Tile style** (Gaps / Seamless) and **Block padding** (Off / 1 pixel) for crisper tile art and
  highlighted text; the board's preview draws the same pixels
- Honours `FIESTABOARD_OUTPUTS_ALLOW_HOSTS`: a host outside the list is never contacted

## How it works

**One still frame** (`write` / `write_cells`): the page is rendered to 64 × 64 RGB888 and sent as
`Draw/SendHttpGif` with `PicNum` 1, `PicWidth` 64, `PicOffset` 0, the next `PicID` and the
pixels base64-encoded in `PicData`.

**A page change** (`write_transition`): FiestaBoard resolves the board's LED transition for the
Pixoo model (flip by default; the board or page can pick another from the menu FiestaBoard offers
for a 5 fps stream). The plugin plans it with FiestaBoard's LED renderer at 5 frames a second and
**streams** it: each frame is its own single-frame push, sent one at a time about 200 ms apart, and
the last one is always the new page. A flip has no half-flaps at this rate (one frame per step).

**Fade** goes through black by default: the plugin dims the panel in five steps with
`Channel/SetBrightness`, pushes the new page while the screen is dark, and brightens it back, in
about 1.1 s. The level it returns to is the board's `brightness` setting, else the device's own
(read with `Channel/GetAllConf`; if the device does not say, the page changes without a fade). The
brightness is restored even when a newer page cuts the fade short. `fade_style: "blend"` streams
the pixel cross-fade instead.

Why stream rather than upload an animation, measured with a camera:

- a stream of single-frame pushes shows every frame once, with no loading screen and no loop;
  paced at 5 frames a second, 19 of 20 frames showed and the last always did. Each push takes
  about 0.19 s, whatever the frame size, so 6 a second cannot be sent;
- two requests in flight, or many frames in one `Draw/CommandList`, show only the last frame;
- an uploaded animation **loops forever**; there is no play-once option;
- an upload longer than about 1.2 s (more than about 3 frames) shows a **"LOADING…" screen**;
- pushing a still after an animation leaves the old loop running for **about 5 s**.

**Explicit sequences** (`write_sequence`): streamed the same way, each frame held its own
duration (never less than one 200 ms step), at most 40 frames (longer runs are thinned evenly,
first and last kept).

**`PicID`s.** The device shows an upload only if its `PicID` is higher than the last one it
accepted; a reused or lower id still answers `error_code` 0 but is silently ignored, so 0 means
"accepted", not "shown". The plugin therefore:

- starts a session with `Draw/ResetHttpGifId`, then seeds its counter from `Draw/GetHttpGifId`
  (which answers the next id, or 0 right after a reset; 0 means start at 1). A session starts with
  the first upload after FiestaBoard starts or the settings change, and after any upload that
  failed or was cut short;
- counts up by one per upload, never reusing or lowering an id;
- resets again, restarting at 1, before the push that would pass 32 since the last reset. A reset
  does not clear the screen and needs no wait.

**Timeouts and cancelling.** Every request goes through FiestaBoard's device helper
(`self.http`), which enforces `FIESTABOARD_OUTPUTS_ALLOW_HOSTS`, follows no redirects and refuses
requests once a write is cancelled; the reset, id and brightness commands are marked as setup
requests. Each request has a 3 s connect and 5 s read timeout. The plugin
checks whether a newer page has arrived before every request and between streamed frames, and
stops there (a fade still restores the brightness first). A device error is reported as a failed write, never raised; three failed writes in
a row make FiestaBoard pause this device for a few minutes.

**Rendering.** All drawing goes through FiestaBoard core's LED renderer (`src.led`, imported
via `src.plugins`, the same renderer FiestaUI previews use), for the device model and character
set FiestaBoard resolved for the board (`self.device_model`, `self.character_set`). The one entry
point is `DivoomPixoo.render()`: it takes 0–71 codes or rich cells.

## Verify on your device

**Verified** on a Pixoo 64 (hardware 92) on 2026-10-04 and 2026-10-05, with a camera on the panel. Details and
timings are in the model's `animation.notes` in
[`output/device-models.json`](./output/device-models.json).

| Fact | Measured | What the plugin does |
| --- | --- | --- |
| Single-frame push | Shows in about 0.5 s, no loading screen, plays once | Every frame is one single-frame push |
| Stream rate | Paced at 5 fps: 19 of 20 frames shown, the last always; 4 fps: 12 of 12; about 0.19 s per push for 64 and 32 px alike | Transitions planned and paced at 5 fps (`STREAM_MAX_FPS`) |
| Requests in flight | Two at once, or a `Draw/CommandList`, show only the last frame | One request at a time, in order |
| Transitions | Flip, slide, wipe, cascade, dissolve and fade, planned by FiestaBoard and streamed at 5 fps, all shown as intended | Streams every transition |
| Brightness ramp | `Channel/SetBrightness` answers in about 75 ms; down 6 steps, swap, up again took about 1.1 s and reads as a smooth fade | Fade through black (`fade_style`) |
| Push spacing | 20 pushes at 1.0 s and at 0.5 s, all shown in order | FiestaBoard spaces writes 1 s apart (`min_interval_ms` 1000, a conservative default) |
| Uploaded animations | Loop forever; no play-once; a loading screen beyond about 3 frames; a 5 s glitch landing on a still | Never uploads more than one frame |
| `PicID` | Shown only if higher than the last accepted; reused or lower ids are ignored with `error_code` 0 | Increasing counter, seeded from `Draw/GetHttpGifId` |
| `Draw/ResetHttpGifId` | Does not clear the screen; no wait needed afterwards | Reset per session and every 32 pushes |
| Colour and orientation | `PicData` is RGB, row-major from the top left, no mirroring | Sent as rendered |
| Brightness | `Channel/SetBrightness` 0–100 works | `brightness` setting |
| Discovery | A /24 search with `Channel/GetAllConf` took about 2 s; the cloud lookup answered in 0.3 s; no mDNS | Find my Pixoo; opt-in cloud lookup |
| Text command | `Draw/SendHttpText` is an overlay that a new image wipes | Text is drawn into the image |

**Still unverified.** If you own a Pixoo 64, please check these and report what you see in an issue.

| What to check | Reported | Plugin setting | How to check |
| --- | --- | --- | --- |
| Freeze after many pushes without a reset | about 300 pushes (the lab stayed under 25 between resets) | `RESET_AFTER_PUSHES` = 32 | Leave it running for a day of page changes; it must not freeze |
| Other firmware versions | Behaviour may differ; the lab did not read the firmware version | — | Note your firmware version (Divoom app) with any report |

## Device data

| File | Contents |
| --- | --- |
| [`output/device-models.json`](./output/device-models.json) | The `divoom_pixoo64` DeviceModel: 64×64 RGB pixels, `led_3x5` character set and 3×5 font (a 10-row × 16-column grid), the LED layout options a board may choose (`layoutOptions`), square-pixel appearance, and `stream` animation at 5 frames a second, with the hardware-lab evidence and sources |

The data is plain JSON validated against FiestaUI's DeviceModel JSON Schema. The plugin's manifest
points at it (`"device_models": {"$ref": "output/device-models.json"}`), so FiestaBoard, FiestaUI
and this plugin read one copy.

### Provenance

The model started as FiestaUI's `divoom_pixoo64` from the LED-matrix work (commit
`a70b7198f3ab6d7f96faf75f13260a4f2f5fceed`, FiestaUI PR #326). Its `animation` block was then
rewritten from the 2026-10-04 hardware lab: `stream` at `maxFps` 2, with the lab's evidence in
`notes` and `sources`. Version 0.4.0 raises `maxFps` to 5 from the 2026-10-05 lab, which measured
single-frame streaming and played every transition on the panel. Version 0.3.0 added `layoutOptions` from FiestaUI PR #338: both tile gaps
and both block paddings allowed, with today's look (`"gap"`, `0`) the default. FiestaUI mirrors
this model field for field in its built-in. Every other field is unchanged. The file validates against FiestaUI's DeviceModel JSON Schema as vendored in
FiestaBoard.

| File | sha256 |
| --- | --- |
| `output/device-models.json` | `81305c2a0104699b4b0052c4e3e01b1c6274bdaa79cf8cb5933596da699540f6` |
| `device-model.schema.json` | `903597ae530303b276995a0c01051e25a11bc5facd272d6e52931102bb1e2a61` |
| `character-set.schema.json` | `69efe686fc58060361d279be453b12f44395fa1a879dcac7c82ce559628437b3` |

Preview cosmetics (square pixels at 0.82 of the pitch, off-LED and substrate colours) live in the
model's `appearance` block; they never change what is sent to the device. A test pins the file's
sha256, so a change to it is always deliberate.

### Using the data from JavaScript

The repository is also a data-only npm package, so tools such as FiestaUI's Storybook can depend
on it:

```json
{
  "devDependencies": {
    "@fiestaboard/output-divoom-pixoo": "github:Fiestaboard/fiestaboard-output--divoom-pixoo#<commit or tag>"
  }
}
```

It ships only `output/*.json`; there is no JavaScript to import. `package.json` and
`manifest.json` carry the same version, and CI fails if they differ.

## Development

Tests run against a FiestaBoard core checkout that has the output-plugin API:

```bash
./run_tests.sh /path/to/FiestaBoard
```

They use a mock Pixoo on 127.0.0.1 (`tests/mock_pixoo.py`) that records every command and can
fail, hang or freeze on purpose; a network fence refuses any other host. The suite includes
FiestaBoard's output-plugin conformance suite (`tests/test_conformance.py`) and needs 80% coverage.

`tools/try_device.py` is a local, development-only check against a real Pixoo: it shows one
still frame, then makes one page change (the model's flip, or the transition `--transition` names),
and prints the timing and reply of every request and the PicIDs used. It runs once and stops. Pass the device address on the command line only:

```bash
PYTHONPATH=/path/to/FiestaBoard python3 tools/try_device.py --host 192.168.1.50
```

## Author

FiestaBoard Team
