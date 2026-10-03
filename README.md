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
| [`output/device-models.json`](./output/device-models.json) | The `divoom_pixoo64` DeviceModel: 64×64 RGB pixels, `led_3x5` character set and 3×5 font (a 10-row × 16-column grid), square pixels, and `sequence` animation (up to 32 frames, 80 ms minimum per frame) with the device's known push limits and their sources |

The data is plain JSON validated against FiestaUI's DeviceModel JSON Schema. It carries no code.

### Provenance

The current data was handed over by the FiestaUI LED-matrix work and validates against the
**draft** schema from FiestaUI branch `feat/led-matrix-display`, revision 7, base commit `81ef225`
plus uncommitted changes:

| File | sha256 |
| --- | --- |
| `output/device-models.json` | `ae3d62d471e22e1f8070bba2ced4e21213170465cde2959db302909b21893559` |
| draft `device-model.schema.json` | `b8c9241e410cfb85634859adb21e2c8e92d1a370d5566194f2202b7f20436426` |
| draft `character-set.schema.json` | `480451113d4cf4f2ce77af4c455f229eb30c1fbf7e8e1ae288ececa1c40a4665` |

The first release (`v0.1.0`) will be tagged once the schema publishes in a FiestaUI release and
this data validates against it. Until then, pin by commit.

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
