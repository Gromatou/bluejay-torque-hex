# Bluejay Torque Mod - Pre-compiled HEX files

Modified Bluejay ESC firmware with boosted low-RPM torque for 2S low-KV (8500KV) motors.

**Based on:** Bluejay v0.21.1-RC1  
**Changes:** Higher startup power, faster transition to full power, increased stall boost, raised power limits.

## Quick flash

Go to [ESC Configurator Extended Sliders](https://gromatou.github.io/esc-configurator-extended-sliders/) and select your layout.

## Layout reference

| Layout | MCU | Common ESCs |
|--------|-----|-------------|
| Z_H | BB21 | Happymodel, generic |
| H_H | BB21 | BetaFPV |
| S_H | BB21 | JHEMCU, DarwinFPV |
| G_H | BB21 | Flywoo, GEPRC |
| O_H | BB21 | iFlight, HGLRC |
| F_H | BB21 | Some Happymodel |
| A_X | BB51 | Newer AIO boards |

## File naming

`{LAYOUT}_{MCU}_{DEADTIME}_{PWM}_torque.hex`

- **LAYOUT**: A-W, Z, OA (ESC pinout layout)
- **MCU**: H (BB21) or X (BB51)
- **DEADTIME**: 5, 10, 15, 20, 25, 30, 40, 50, 70, 90, 120 (most common: **15**)
- **PWM**: 24, 48, 96 kHz (most common: **48**)

If unsure, try `Z_H_15_48_torque.hex` first.

## Rebuild

```bash
python tools/patch_official_hex.py
```

Downloads official Bluejay v0.21.1-RC1 hex files from GitHub and applies the torque mod patches.
