# Divoom Pixoo 64 Output Plugin

FiestaBoard output plugin that will show your FiestaBoard pages on a Divoom Pixoo 64 LED matrix.

> **Work in progress.** Today this repository publishes the Pixoo 64 **device data** only. The
> Python output plugin lands once FiestaBoard's output-plugin interface ships in the 10.0.0 beta.
> It is not installable yet.

## Overview

FiestaBoard is moving every display it drives (Vestaboard, TV panels, LED matrices) onto output
plugins, each owning its own device. This repository is the Pixoo 64's: its device model
(geometry, colour, character set, animation budget) lives here, and FiestaBoard and the FiestaUI
design system both read it from here rather than keeping their own copies.

## Device data

| File | Contents |
| --- | --- |
| [`output/device-models.json`](./output/device-models.json) | The `divoom_pixoo64` DeviceModel: 64×64 RGB pixels, `led_3x5` character set and 3×5 font (a 10-row × 16-column grid), square-pixel appearance, and `sequence` animation (up to 32 frames, 80 ms minimum per frame) with the device's known push limits and their sources |

The data is plain JSON validated against FiestaUI's DeviceModel JSON Schema. It carries no code.

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
model's `appearance` block; they never change what is sent to the device.

The first release (`v0.1.0`) will be tagged once that schema publishes in a FiestaUI release.
Until then, pin by commit.

### Using the data from JavaScript

The repository is also a data-only npm package, so tools such as FiestaUI's Storybook can depend on it:

```json
{
  "devDependencies": {
    "@fiestaboard/output-divoom-pixoo": "github:Fiestaboard/fiestaboard-output--divoom-pixoo#<commit or tag>"
  }
}
```

It ships only `output/*.json`; there is no JavaScript to import.

## Features

Planned for the plugin itself:

- Sends FiestaBoard pages to a Pixoo 64 on your local network
- FiestaBoard's flip transition, uploaded as one animation within the device's 32-frame budget
- Paced pushes that respect the device's limits

## Author

FiestaBoard maintainers
