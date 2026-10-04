# Divoom Pixoo 64 Setup Guide

Connect a Divoom Pixoo 64 on your local network so FiestaBoard shows your pages on it.

## Overview

**What it does:** FiestaBoard draws each page on the Pixoo's 64 × 64 LED matrix as a 10-row ×
16-column grid of characters, and plays its flip transition when the page changes. Everything
stays on your network; no Divoom account or cloud service is involved.

**Prerequisites:**

- A **Divoom Pixoo 64** set up with the Divoom app and connected to your Wi-Fi. The smaller Pixoo
  16 and Pixoo 32 are not supported: they cannot fit FiestaBoard's minimum 3 × 15 grid.
- **FiestaBoard 10.0.0 or later**, on the same network as the Pixoo.
- The Pixoo's **IP address**. **Find my Pixoo** in the board settings searches your network for
  it. If that finds nothing, look in the Divoom app under the device's settings, or in your
  router's list of connected devices. A fixed (reserved) address in your router stops it from
  changing.

> Output plugins are a beta feature, and this plugin has not yet been verified on a real device.
> See [Verify on your device](../README.md#verify-on-your-device) for what is still to be
> confirmed.

## Quick Setup

1. **Enable** — In FiestaBoard, go to **Settings → Beta** and turn on **Output Plugins**. Then
   install this plugin from its repository:

   ```bash
   curl -X POST http://localhost:4420/api/plugins/install \
     -H "Content-Type: application/json" \
     -d '{"repository": "https://github.com/Fiestaboard/fiestaboard-output--divoom-pixoo"}'
   ```

2. **Configure** — Create a board driven by the Pixoo, with its IP address as `host`. In the
   board settings, **Find my Pixoo** fills it in; you can always type it instead. Through the
   API, the address below is an example; use your Pixoo's. `brightness` is optional.

   ```bash
   curl -X POST http://localhost:4420/api/outputs/divoom_pixoo/boards \
     -H "Content-Type: application/json" \
     -d '{
           "name": "Pixoo",
           "device_model": "divoom_pixoo64",
           "output_config": {"host": "192.168.1.50", "brightness": 60}
         }'
   ```

   FiestaBoard answers with the new board, sized 10 rows × 16 columns.

3. **Template** — Assign pages to the new board as you would for any board. Pages written for a
   Vestaboard Note (3 × 15) fit as they are; anything wider than 16 characters or taller than 10
   rows is cut off.

4. **View** — Send a page to the board. The Pixoo shows the page after a short "Loading.." overlay
   (part of how the device plays uploads). Later page changes play the flip transition and settle
   on the new page.

## What it shows

| Content | On the Pixoo |
| --- | --- |
| Grid | 10 rows × 16 columns |
| Letters, digits, punctuation | White 3×5 pixel characters on black |
| `{63}`–`{68}` | Solid red, orange, yellow, green, blue or violet cells |
| `{69}` | Solid white cell |
| `{70}`, `{71}` | Unlit (black) cells |
| Colour spans (`{red:HOT}`) | Letters in that colour |
| Icons (`{icon:sun}`) | The icon's 3×5 picture |
| Characters with no 3×5 glyph | FiestaBoard's fallback for the `led_3x5` set |

Page changes play the board's LED transition (by default FiestaBoard's flip: at most 32 frames,
80 ms each, exactly as the preview shows it), then the new page stays on screen. FiestaBoard sends at most one update per second to the Pixoo.

## Configuration Reference

| Setting | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `host` | string | Yes | — | The Pixoo's IP address or hostname. A port is accepted (`192.168.1.50:80`); `http://` and any path are ignored. |
| `brightness` | integer, 0–100 | No | the device's own | Screen brightness, applied after the first page lands. Leave it out to keep the brightness you set in the Divoom app. |

**Environment variables**

| Variable | Description |
| --- | --- |
| `FIESTABOARD_OUTPUTS_ALLOW_HOSTS` | Comma-separated hosts FiestaBoard may send board traffic to. When set, the Pixoo's address must be in the list, or the plugin never contacts it and the connection test reports it as blocked. Unset allows every host. |

The plugin itself needs no environment variables, API keys or passwords: the Pixoo's local API has
no authentication.

## Troubleshooting

**Find my Pixoo finds nothing**

- Make sure the Pixoo is on and on the same network as the device you opened FiestaBoard on.
- Enter your network, for example `192.168.1.0/24`, and search again. FiestaBoard in Docker
  sees a container network of its own, so it cannot always tell which network to search.
- If `FIESTABOARD_OUTPUTS_ALLOW_HOSTS` is set (development setups set it), only the hosts it
  lists are asked: set it empty to search.
- **Ask Divoom's servers which Pixoos are on your network** is the opt-in fallback. It sends
  one request to Divoom's cloud, which sees your public IP address.
- Or type the address from the Divoom app or your router.

**The connection test says it could not connect**

- Check the IP address in the Divoom app or your router; it may have changed. Reserve a fixed
  address for the Pixoo in your router.
- Make sure the Pixoo is powered on and on the same network (and VLAN) as FiestaBoard.

**The connection test says the device did not answer**

- The Pixoo accepted the connection but never replied. Unplug it for a few seconds, plug it back
  in, and test again.

**The connection test says the device answered, but not like a Pixoo 64**

- Another device has that address. Check the IP address again.

**The connection test says the host is not allowed**

- `FIESTABOARD_OUTPUTS_ALLOW_HOSTS` is set and does not include the Pixoo's address. Add it, or
  unset the variable.

**The Pixoo stopped updating after a while**

- Community reports say the device can freeze after many uploads. The plugin resets the device's
  upload counter regularly to prevent this; if it still happens, restart the Pixoo and
  [open an issue](https://github.com/Fiestaboard/fiestaboard-output--divoom-pixoo/issues) with
  roughly how long it ran and how often pages changed.

**Updates are refused for a few minutes after errors**

- After three failed writes in a row, FiestaBoard pauses that device for a few minutes so a
  dead device does not slow everything down. Fix the connection and it resumes on its own.

**Brightness does not change**

- Brightness is applied after the first page lands. If the device rejected it, the plugin tries
  again after the next page.
