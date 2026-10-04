# Divoom Pixoo 64 Output Plugin

Shows your FiestaBoard pages on a Divoom Pixoo 64 LED matrix over your local network.

**→ [Setup Guide](./docs/SETUP.md)**

> **Beta, not yet verified on hardware.** This plugin needs FiestaBoard 10.0.0 or later with
> **Settings → Beta → Output Plugins** turned on. Several device limits it relies on are
> community reports that have not been confirmed on current firmware. See
> [Verify on your device](#verify-on-your-device).

## Overview

The Pixoo 64 is a 64×64 RGB LED matrix with a local HTTP API. This output plugin turns each
FiestaBoard page into pixels with FiestaBoard's own LED renderer and pushes them to the device.
Page changes play FiestaBoard's flip transition, uploaded as one short animation that always ends
on the new page.

## Device

| | |
| --- | --- |
| Device model | `divoom_pixoo64` (from [`output/device-models.json`](./output/device-models.json)) |
| Matrix | 64 × 64 pixels, 24-bit RGB |
| Character grid | **10 rows × 16 columns** (3×5 font with 1-pixel gaps) |
| Character set | `led_3x5`: A–Z, 0–9, board punctuation, colour tiles |
| Connection | `POST http://<host>/post` on your LAN; no account, no cloud, no password |
| Write spacing | At least 1 second between writes (enforced by FiestaBoard, not the plugin) |
| Animation | One upload per page change: up to 32 frames, at least 80 ms each |
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
answers with the Pixoos registered from it; the plugin keeps only each one's local address and
name. It runs only when you click it.

Both go through FiestaBoard's device helper, so `FIESTABOARD_OUTPUTS_ALLOW_HOSTS` applies. It is
unset in production. Development setups set it to their mocks, which blocks a sweep: set it
empty (or add your Pixoo's address) to search a real network.

## Features

- Sends FiestaBoard pages to a Pixoo 64 on your local network, with no cloud service
- **Find my Pixoo**: searches your network for the device; manual entry always works
- Optional, clearly labelled lookup through Divoom's cloud when the search finds nothing
- FiestaBoard's flip transition, uploaded as one animation within the device's 32-frame budget
- Always lands on the new page: after the animation plays, the target is pushed as a still frame
- Guards against the reported upload freeze by resetting the device's GIF counter regularly
- Gives up a write as soon as a newer page arrives, between any two requests
- Connection test that tells unreachable, timed-out, wrong-device and blocked-host cases apart
- Optional brightness setting
- Honours `FIESTABOARD_OUTPUTS_ALLOW_HOSTS`: a host outside the list is never contacted

## How it works

**One still frame** (`write` / `write_cells`): the page is rendered to 64 × 64 RGB888 and sent as
`Draw/SendHttpGif` with `PicNum` 1, `PicWidth` 64, `PicOffset` 0, the next `PicID` and the
pixels base64-encoded in `PicData`.

**A page change** (`write_transition`): FiestaBoard resolves the board's LED transition for the
Pixoo model (by default the flip: one frame per 80 ms step, no half-flaps, at most 32 frames) and
hands the plugin the before and after pages. The plugin plans exactly that transition with core's
LED renderer, the same frames FiestaUI previews, and uploads them as **one** GIF: one POST per
frame, all with the same `PicID`, `PicNum` = frame count and `PicOffset` 0, 1, 2… The first
frame (the page already on screen) is not re-sent. The device loops an uploaded GIF, so once it
has loaded and played through, the plugin pushes the new page as a still frame.

**Transition plugins** (`write_sequence`): when a board uses one of FiestaBoard's transition
plugins instead, its frames are uploaded the same way, each shown for its own duration but never
less than 80 ms.

**`PicID` and resets.** The plugin sends `Draw/ResetHttpGifId` and restarts `PicID` at 1:

- before the first upload after FiestaBoard starts or the settings change;
- before every animation;
- after an upload that failed or was cut short (the device's counter is then unknown);
- when the next upload would take it past 32 frame pushes since the last reset.

**Timeouts and cancelling.** Every request goes through FiestaBoard's device helper
(`self.http`), which enforces `FIESTABOARD_OUTPUTS_ALLOW_HOSTS`, follows no redirects and refuses
requests once a write is cancelled; the reset and brightness commands are marked as setup
requests. Each request has a 3 s connect and 5 s read timeout. The plugin
checks whether a newer page has arrived before every request and while it waits between frames,
and stops there. A device error is reported as a failed write, never raised; three failed writes in
a row make FiestaBoard pause this device for a few minutes.

**Rendering.** All drawing goes through FiestaBoard core's LED renderer (`src.led`, imported
via `src.plugins`, the same renderer FiestaUI previews use), for the device model and character
set FiestaBoard resolved for the board (`self.device_model`, `self.character_set`). The one entry
point is `DivoomPixoo.render()`: it takes 0–71 codes or rich cells.

## Verify on your device

These device facts come from community libraries and notes (listed under `sources` in
[`output/device-models.json`](./output/device-models.json)) and have **not** been confirmed on
current firmware. Each is a named constant at the top of `__init__.py`. If you own a Pixoo 64,
please check them and report what you see in an issue.

| What to check | Reported | Plugin setting | How to check |
| --- | --- | --- | --- |
| Safe push rate | about 1 push per second | FiestaBoard's floor, `min_interval_ms` 1000 | Change pages quickly for several minutes; the device must keep answering |
| Spacing between frames of one upload | 150 ms to 1 s | `FRAME_GAP_S` = 0.15 | Watch transitions; look for dropped or garbled frames |
| Freeze after many pushes without a reset | about 300 pushes | `RESET_AFTER_PUSHES` = 32 | Leave it running for a day of page changes; it must not freeze |
| "Loading.." overlay before an animation plays | about 5 s | `LOADING_OVERLAY_S` = 5.0 | Change pages and time the overlay |
| Does a **single-frame** push show "Loading.." too? | unknown | — | Push one still page. If it does, static updates need another command |
| Maximum frames in one upload | 32–40 frames; the official doc says 60 | the model's `maxFrames` 32 | Uploads over 32 should never be sent; confirm 32 plays cleanly |
| Does an uploaded GIF loop? | yes | the still-frame push after each animation | If it plays once and stops on its last frame, the still push can go |
| Brightness command | `Channel/SetBrightness` 0–100 | `brightness` setting | Set it and watch the screen |

## Device data

| File | Contents |
| --- | --- |
| [`output/device-models.json`](./output/device-models.json) | The `divoom_pixoo64` DeviceModel: 64×64 RGB pixels, `led_3x5` character set and 3×5 font (a 10-row × 16-column grid), square-pixel appearance, and `sequence` animation (up to 32 frames, 80 ms minimum per frame) with the device's known push limits and their sources |

The data is plain JSON validated against FiestaUI's DeviceModel JSON Schema. The plugin's manifest
points at it (`"device_models": {"$ref": "output/device-models.json"}`), so FiestaBoard, FiestaUI
and this plugin read one copy.

### Provenance

The data comes from the FiestaUI LED-matrix work and validates against FiestaUI's DeviceModel
JSON Schema at commit `a70b7198f3ab6d7f96faf75f13260a4f2f5fceed` (FiestaUI PR #326, the LED data
layer; part of a stack that has not been released yet):

| File | sha256 |
| --- | --- |
| `output/device-models.json` | `e7cbfff32e91afd3d7b3ce39fa27cd7335cba76a326698221fc14100d8607d6f` |
| `device-model.schema.json` | `ef3129dac12f01f9a515b9d1798376881475472fa79dfd168ff5ee5c6f349ffb` |
| `character-set.schema.json` | `69efe686fc58060361d279be453b12f44395fa1a879dcac7c82ce559628437b3` |

Preview cosmetics (square pixels at 0.82 of the pitch, off-LED and substrate colours) live in the
model's `appearance` block; they never change what is sent to the device. A test pins the file's
sha256, so changing it is a deliberate re-vendor.

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
still frame, then plays one transition, and prints the timing and reply of every request. It
never loops. Pass the device address on the command line only:

```bash
PYTHONPATH=/path/to/FiestaBoard python3 tools/try_device.py --host 192.168.1.50
```

## Author

FiestaBoard Team
