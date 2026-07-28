# RK-X40F family identification and controls

## Identification

Original packaging identifies the tested hardware as a Koolertron `RK-X40F`,
variant `X40F 3rd` / `RK-X40F Ultra 3rd`. A cross-brand Fliq Pro manual names the
same `RK-X40F` family and matches the tested unit's unusually specific combination
of features:

- 4-inch, 1136x640 display;
- 3.7 V, 2300 mAh internal battery;
- separate USB-C charging and wired-data ports;
- wired and wireless phone mirroring;
- Bluetooth HID/volume-key camera controls;
- an `X-40F` family receiver name.

Packaging and manual materials are provenance evidence, not redistributed
repository assets. Private originals and metadata-bearing photographs remain
outside the public boundary.

## Packaging claims

The packaging states:

```text
model family: RK-X40F
display: 4 inch
resolution: 1136x640
battery: 3.7 V, 2300 mAh
input: 5 V / 2 A
separate charging and wired-projection USB-C ports
```

Those are manufacturer claims. The wired HCCAST path was independently
demonstrated on the tested unit; other packaging claims retain their original
status.

## Power-on UI observations

The first visible UI identifies the device as an `X-40F` family screen and
provides Bluetooth, local Wi-Fi, and screen-projection instructions. The public
record intentionally omits the tested unit's unique receiver suffix, displayed
local-network credential, and packaging serial/lot fields.

The displayed version marker is retained privately as unit evidence but is not
treated as a decoded firmware version.

## Documented controls

The family manual describes:

| Control | Short press | Long press |
|---|---|---|
| Power button | Toggle the LCD while the receiver host continues running | Power on/off |
| Flip/language key | Reduce brightness; alternate select function in Apple mode | Switch language |
| Upper shooting/volume key | Increase volume and camera shutter | Switch screen ratio |
| Lower shooting/volume key | Reduce volume and camera shutter | Rotate/mirror |

The manual associates a deep-blue status light with the LCD being off while the
receiver host remains active.

## Physical-unit observations

- `OBSERVED`: a short press did not power the tested unit on.
- `OBSERVED`: power-on generally required a deliberate hold of roughly five
  seconds.
- `OBSERVED`: while on, short presses affected display/HID behavior rather than
  acting as ordinary power-on actions.
- `OBSERVED`: a flashing-blue/black-LCD state could retain a USB personality even
  though the panel showed no image.
- `OBSERVED`: a normal visible boot showed a steady light and setup UI while the
  transient direct-host USB identity disappeared after about one second.
- `MANUAL-DOCUMENTED`: long press controls receiver power; short press can affect
  only the LCD or an alternate control mode.

The exact short-press UI varies between firmware or retail variants. The manual's
Apple select-mode and HID volume-key behavior plausibly explain the observed
keyboard-mode changes, but that mapping remains `INFERRED`.

## Test consequence

Controlled experiments should distinguish:

```text
receiver powered off
receiver powered with LCD off
receiver powered with setup UI visible
```

For this unit, a deliberate long hold is the observed boot action. A black LCD
alone is not evidence that the receiver host is off. Every future run should
record panel state, indicator state, external power, data connection, and button
action independently.

## Public provenance

The family manual was published by Fliq Gear under the title “Fliq Pro Manual
Selfie Screen” and names model `Fliq Pro - RK-X40F`. Public documentation uses
only narrow factual findings in original prose. The vendor PDF, page renderings,
manual text, packaging originals, and metadata-bearing media are excluded from
this repository.
