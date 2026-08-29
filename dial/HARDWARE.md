# The dial that turns itself — build guide

A physical governance-weight knob on the podium, turned live by the controller
reading real GPU thermometers. In FIXED mode you set it by hand; flip to AUTO
and the knob turns **by itself**. ~$30 of parts, one weekend.

## Parts (~$30)

| Part | Example | ~Price |
|---|---|---|
| ESP32 devkit (any WROOM board) | HiLetgo / DOIT ESP32 | $8 |
| Micro servo, metal gear | MG90S | $6 |
| 0.96" SSD1306 OLED (I2C) — optional but great | generic | $6 |
| Aluminum knob, ~30–40 mm | any machined knob w/ set screw* | $8 |
| Small project box / 3D-printed panel + USB cable | — | $5 |

\* The simplest coupling: press/screw the knob onto the **servo horn shaft**
directly — the servo *is* the spindle. Mount the servo behind the panel with
its shaft through a hole; the knob faces the audience.

## Wiring

```
ESP32            MG90S servo          SSD1306 OLED
-----            -----------          ------------
GPIO 13  ------> signal (orange)
VIN (5V) ------> V+     (red)
GND      ------> GND    (brown)
GPIO 21  --------------------------->  SDA
GPIO 22  --------------------------->  SCL
3V3      --------------------------->  VCC
GND      --------------------------->  GND
```

Power the whole thing from a USB power bank on the podium (no wall cable).

## Panel

Label the arc around the knob with the spectrum:
`SPREAD · resilience  ←  λ  →  PACK · energy` (teal → amber gradient — print a
strip, or a paint pen). OLED window above the knob shows the live value + mode.

## Firmware

`lambda_dial.ino` — Arduino IDE, board "ESP32 Dev Module". Install libraries:
**ESP32Servo, ArduinoJson**, and (optional) **Adafruit SSD1306 + GFX**.
Edit the three lines at the top: hotspot SSID/password and your pod's
`/metrics` URL. It polls at 1 Hz and slews the servo smoothly (no snapping —
deliberate, thermostat-like motion reads better on camera).

## Stage notes

- Connect the ESP32 to your **phone hotspot** (same network story as the
  laptop — venue wifi is never in the loop).
- Rehearse the Act 1 → Act 2 beat: hold the knob at 0.85 during FIXED (the
  servo holds position, so it genuinely resists small nudges — fun), then
  *let go*, tap AUTO on the dashboard, and step back from the podium while
  it turns itself.
- Failure mode is graceful: network drop → knob freezes at last position.
  Nothing to explain; the dashboard recording carries the demo.
- TSA: it's a bare hobby board + servo in a project box. Put it in carry-on
  with cables; it reads as exactly what it is.
