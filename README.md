# Divoom Pixoo 64 Output Plugin

Shows your FiestaBoard pages on a Divoom Pixoo 64 LED matrix over your local network.

**→ [Setup Guide](./docs/SETUP.md)**

> **Beta.** This plugin needs FiestaBoard 10.0.0 or later with **Settings → Beta → Output
> Plugins** turned on. Its device behaviour was measured on a real Pixoo 64 on 2026-10-04; two
> community reports are still unverified. See [Verify on your device](#verify-on-your-device).

## Overview

The Pixoo 64 is a 64×64 RGB LED matrix with a local HTTP API. This output plugin turns each
FiestaBoard page into pixels with FiestaBoard's own LED renderer and pushes them to the device.
Page changes cut straight to the new page, in about half a second: the Pixoo cannot play an
animation once without looping it or showing a loading screen (see [How it works](#how-it-works)).

## Device

| | |
| --- | --- |
| Device model | `divoom_pixoo64` (from [`output/device-models.json`](./output/device-models.json)) |
| Matrix | 64 × 64 pixels, 24-bit RGB |
| Character grid | **10 rows × 16 columns** (3×5 font with 1-pixel gaps) |
| Character set | `led_3x5`: A–Z, 0–9, board punctuation, colour tiles |
| Connection | `POST http://<host>/post` on your LAN; no account, no cloud, no password |
| Write spacing | At least 1 second between writes (enforced by FiestaBoard, not the plugin); 0.5 s verified safe |
| Animation | None: page changes snap (`stream` at 2 frames a second, below every transition) |
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
- Fast, clean page changes: one single-frame push, shown in about half a second, no loading screen
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
Pixoo model. The model streams at 2 frames a second, below every transition's minimum, so the
answer is `none`: the plugin pushes the new page as one still frame, and FiestaBoard's preview cuts
the same way. This is deliberate. On the device, measured with a camera:

- an uploaded animation **loops forever**; there is no play-once option;
- an upload longer than about 1.2 s (more than about 3 frames) shows a **"LOADING…" screen** from
  about 1.5 s after the first frame until about 0.5 s after the last: 1.3 s for 8 frames, 4.8 s
  for 32, 6.3 s for 40;
- pushing a still after an animation leaves the old loop running, with its first frame replaced,
  for **about 5 s** before the still settles;
- a single-frame push shows in about 0.5 s with no loading screen.

**Explicit sequences** (`write_sequence`, which FiestaBoard does not use for this model): the
frames go up back-to-back as one GIF, at most 40 (longer runs are thinned evenly, first and last
kept), each shown at least 80 ms. The device loops it; once it is ready and has played once, the
plugin pushes the last frame as a still, with the 5 s glitch above.

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
checks whether a newer page has arrived before every request and while it waits for an
explicit sequence to play, and stops there. A device error is reported as a failed write, never raised; three failed writes in
a row make FiestaBoard pause this device for a few minutes.

**Rendering.** All drawing goes through FiestaBoard core's LED renderer (`src.led`, imported
via `src.plugins`, the same renderer FiestaUI previews use), for the device model and character
set FiestaBoard resolved for the board (`self.device_model`, `self.character_set`). The one entry
point is `DivoomPixoo.render()`: it takes 0–71 codes or rich cells.

## Verify on your device

**Verified** on a Pixoo 64 (hardware 92) on 2026-10-04, with a camera on the panel. Details and
timings are in the model's `animation.notes` in
[`output/device-models.json`](./output/device-models.json).

| Fact | Measured | What the plugin does |
| --- | --- | --- |
| Single-frame push | Shows in about 0.5 s, no loading screen | Every page change is one single-frame push |
| Push spacing | 20 pushes at 1.0 s and at 0.5 s, all shown in order | FiestaBoard spaces writes 1 s apart (`min_interval_ms` 1000, a conservative default) |
| Animations | Loop forever; no play-once | Snaps instead of animating |
| Loading screen | From about 1.5 s into an upload until about 0.5 s after the last frame; none under about 1.2 s | Snaps; an explicit sequence waits it out (`ANIMATION_READY_S`) |
| Landing on a still after an animation | Old loop keeps running for about 5 s | Only on the explicit sequence path, documented |
| Frames per upload | 59 accepted, but playback wrapped at about 55; 40 played completely | `SEQUENCE_MAX_FRAMES` = 40 |
| `PicSpeed` | Milliseconds per frame; 80 ms and 250 ms honoured | Never below 80 ms (`MIN_FRAME_MS`) |
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
| [`output/device-models.json`](./output/device-models.json) | The `divoom_pixoo64` DeviceModel: 64×64 RGB pixels, `led_3x5` character set and 3×5 font (a 10-row × 16-column grid), the LED layout options a board may choose (`layoutOptions`), square-pixel appearance, and `stream` animation at 2 frames a second (so FiestaBoard snaps), with the hardware-lab evidence and sources |

The data is plain JSON validated against FiestaUI's DeviceModel JSON Schema. The plugin's manifest
points at it (`"device_models": {"$ref": "output/device-models.json"}`), so FiestaBoard, FiestaUI
and this plugin read one copy.

### Provenance

The model started as FiestaUI's `divoom_pixoo64` from the LED-matrix work (commit
`a70b7198f3ab6d7f96faf75f13260a4f2f5fceed`, FiestaUI PR #326). Its `animation` block was then
rewritten from the 2026-10-04 hardware lab: `stream` at `maxFps` 2, with the lab's evidence in
`notes` and `sources`. Version 0.3.0 adds `layoutOptions` from FiestaUI PR #338: both tile gaps
and both block paddings allowed, with today's look (`"gap"`, `0`) the default. FiestaUI mirrors
this model field for field in its built-in. Every other field is unchanged. The file validates against FiestaUI's DeviceModel JSON Schema as vendored in
FiestaBoard.

| File | sha256 |
| --- | --- |
| `output/device-models.json` | `84eff14306316a1f19713d42c1046d5d0cca7d459309ee1724d8fb5b0b5ecad0` |
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
still frame, then makes one page change (a snap, as FiestaBoard resolves it), and prints the timing
and reply of every request and the PicIDs used. It never loops. Pass the device address on the command line only:

```bash
PYTHONPATH=/path/to/FiestaBoard python3 tools/try_device.py --host 192.168.1.50
```

## Author

FiestaBoard Team
