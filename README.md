# dbus-lynx-i2c

Venus OS D-Bus service for **Victron Lynx Distributor fuse monitoring** on a Cerbo GX — without a Lynx Smart BMS. A USB-I2C adapter (Waveshare CH347) plugged into the Cerbo reads each distributor's fuse-status byte over the RJ10 I2C bus and publishes it to D-Bus so blown fuses surface on the GX display and VRM.

> **Status:** hardware validated on paper, adapter ordered; driver not yet written. This README documents the complete hardware design; repo scaffolding (license, service files, install/enable/disable scripts, `velib_python` submodule) is in place. Software (CH347-HID I2C driver, poller, D-Bus service) lands next.

## Why

The Lynx Distributor's fuse monitoring is normally only readable by a Lynx Smart BMS (~$700, and it's a full lithium BMS with contactor — wrong tool if your packs have their own BMS). But the RJ10 "power/data" jack is just an I2C slave with a published-enough protocol, and the Cerbo GX is a Linux box with USB ports. This project bridges the two.

## Architecture

```
Cerbo GX (Venus OS)                      Lynx Distributor "A"        Lynx Distributor "B"
┌─────────────────────┐                  ┌──────────────────┐        ┌──────────────────┐
│ dbus-lynx-i2c       │   USB            │ addr 0x08        │  RJ10  │ addr 0x09        │
│  └ /dev/hidraw*  ───┼──────► Waveshare │ RJ10 ◄───────────┼────────┼─► RJ10           │
│                     │        CH347     │  (jack L)  (jack R)       │                  │
└─────────────────────┘        (M2, 5V)  └──────────────────┘        └──────────────────┘
                                   └── I2C @ 5V, ~50 kHz, one bus for up to 4 distributors
```

One adapter serves all distributors: each Lynx Distributor has **two RJ10 jacks** (left and right) and daisy-chains (Lynx Distributor manual §6.2.1).

## Hardware

### The Lynx Distributor RJ10 port

Source: [Victron Lynx Distributor M8/M10 manual](https://www.victronenergy.com/upload/documents/Lynx_Distributor/24531-Lynx_Distributor_Manual-pdf-en.pdf) §3.3 + community reverse-engineering ([Victron Community: reading fuse states with ESPHome](https://community.victronenergy.com/t/how-to-power-the-lynx-distributor-and-read-the-fuse-states-with-esphome/3534)).

| RJ10 pin | Victron wire color | Function |
|---|---|---|
| 1 | yellow | **5V in** (4.5–5.5V) — powers the fuse-detection PCB; the distributor has no internal supply |
| 2 | green | **SDA** |
| 3 | red | **SCL** |
| 4 | black | **GND** |

Pin numbering depends on viewing side: contact (gold-pin) side facing you, cable down → pin 1 on the left; clip side reverses the order. The Victron manual's Figure 1 shows both views.

⚠️ **The RJ10 port has no reverse-polarity protection.** Swapping pins 1/4 permanently damages the distributor's monitoring electronics (the busbars don't care, but fuse detection dies). Meter-verify 5V/GND at the RJ10 plug before ever connecting a distributor.

Requirements/limits:

- Fuse-status communication needs distributor **serial HQ1909 or later**.
- I2C slave address is set by the 2-way DIP switch on the distributor PCB (under the cover, top center): **A = 0x08** (both off, factory default), **B = 0x09** (sw1 on), **C = 0x0A** (sw2 on), **D = 0x0B** (both on). Label/address distributors left-to-right A, B, … per Victron convention so alarms name the right unit.
- Bus speed: ~**50 kHz** is the community-proven rate.

### Status byte protocol

Read **one byte** from the distributor's address. Community-documented encoding (verify bit order empirically before trusting — pull a fuse and watch):

| Bit | Meaning (1 = fault) |
|---|---|
| 0x10 | Fuse 1 blown/missing |
| 0x20 | Fuse 2 blown/missing |
| 0x40 | Fuse 3 blown/missing |
| 0x80 | Fuse 4 blown/missing |
| 0x02 | Busbar has no supply |

`0x00` = all four fuses present and intact. Caveat from the Victron manual: with batteries on multiple circuits, a blown battery-side fuse may not read as blown until the battery is under charge/discharge (not enough voltage across the fuse to trigger detection).

### The USB-I2C adapter: Waveshare "USB TO UART/I2C/SPI/JTAG"

[Product wiki](https://www.waveshare.com/wiki/USB_TO_UART/I2C/SPI/JTAG) · WCH **CH347** chip · aluminum case, ~$24 on Amazon (sold by waveshare). Chosen because:

- **Mode 2 (M2) is USB-HID — driverless.** Venus OS ships `usbhid`/`hidraw` (verified on kernel `6.12.23-venus-9`); the adapter appears as `/dev/hidraw*`. No kernel module to build, survives Venus firmware updates.
- **5V native.** A slide switch sets all I/O and the VCC pin to 5V — matches the Lynx bus and powers the distributors' pin 1 from USB. Onboard I2C pull-ups; no external components.
- Industrial temp range, fused/ESD-protected USB, mounting flanges.

Rejected alternatives, for the record:

| Adapter | Verdict on Cerbo |
|---|---|
| CP2112 boards (CJMCU clone) | ✅ works via in-kernel `hid-cp2112` → native `/dev/i2c-N`, but every Amazon board ships with unsoldered headers; the no-solder option is the Silabs **CP2112EK** eval kit (Digi-Key 336-2010-ND, ~$42). This is the fallback if M2/HID disappoints. |
| CH341A/CH341T sticks | ❌ I2C mode needs the out-of-tree `i2c-ch341-usb` module; Venus only ships the CH341 *serial* driver |
| CP2102 "USB to TTL" cables | ❌ UART only — no I2C hardware at all, despite the similar name |
| Victron VE.Direct(-USB) cables | ❌ UART, not I2C |
| "JI2C" EEPROM-debugger dongles | ❌ proprietary Windows-only protocol, undocumented chip |
| CH347 in **M1** mode | ❌ needs the vendor `ch34x_pis` kernel module (cross-compile per Venus kernel, breaks on updates) and still doesn't yield a real `/dev/i2c-N` — same command protocol as M2 with extra fragility |

### Adapter configuration and wiring

**Set both switches while unpowered** (mode only latches at power-on; unplug USB → flip → replug):

- Red DIP: **M2** (switch 1 ON, switch 2 OFF) → UART1 + I2C + SPI as HID
- Level slide: **5V**

Wire the adapter's keyed 4-pin **I2C** plug (pins: VCC, SCL / GND, SDA — labels printed on the case; do **not** trust the pigtail wire colors, identify by plug position) to the RJ10 cable:

| Waveshare I2C pin | RJ10 pin | Victron wire color |
|---|---|---|
| VCC | 1 | yellow |
| SDA | 2 | green |
| SCL | 3 | red |
| GND | 4 | black |

Pre-flight: with the adapter on USB and nothing else connected, verify ~5V across the VCC/GND wires; verify the splice lands 5V on RJ10 pin 1 and GND on pin 4; then connect the distributor chain. Chain additional distributors with a standard RJ10 cord jack-to-jack (each distributor ships with a 40 cm cable).

## Cerbo GX / Venus OS facts (verified on the target)

- Kernel `6.12.23-venus-9`, Python **3.12**, no `pip`, no `smbus2`.
- `CONFIG_HIDRAW=y`, `usbhid.ko` + `hid-generic.ko` present → M2 HID works stock.
- `hid-cp2112.ko` present (the CP2112 fallback would enumerate as `/dev/i2c-5`; internal buses 0–3 are SoC `mv64xxx`, bus 4 is HDMI DDC).
- **No** `i2c-tiny-usb`, **no** CH341 I2C, **no** MCP2221 HID-I2C, **no** `CONFIG_I2C_MUX`; `/lib/modules/.../drivers/i2c/busses/` is empty.
- `cdc-acm.ko` and `ch341.ko` (serial-only) present.
- Persistence pattern (like sibling projects [dbus-power-watchdog](https://github.com/TechBlueprints/dbus-power-watchdog), [dbus-aggregate-smartshunts](https://github.com/TechBlueprints/dbus-aggregate-smartshunts)): install under `/data` (survives firmware updates), autostart via `/data/rc.local`, D-Bus via the `velib_python` bundled in Venus.

## Software plan

1. **CH347 HID-I2C driver** (pure Python, stdlib only — no pip on Venus): open `/dev/hidraw*`, speak the CH347 I2C command protocol (public: WCH's *CH347 Application Development Manual* in the [Waveshare demo package](https://files.waveshare.com/wiki/USB-TO-UART-I2C-SPI-JTAG/USB-TO-UART-I2C-SPI-JTAG-Demo.zip); open-source reference: [ch347-hidapi](https://github.com/MeimeiZ/ch347-hidapi)). Configure 50 kHz (falls in CH347 standard-mode class), read 1 byte from 0x08/0x09.
2. **Empirical verification**: pull each fuse in turn, record the byte, pin down the bit order (community sources conflict slightly).
3. **Poller + D-Bus service** via `velib_python`: one service per distributor, fuse states + alarm paths so VRM raises notifications. Exact D-Bus service class (digital-input-style vs. generic) to be settled against what systemcalc/VRM will display.
4. ~~**Packaging**: `/data/apps/dbus-lynx-i2c/`, `rc.local` hook, install script.~~ Done — see below.

## Installation

> ⚠️ The driver is not yet implemented — installing today registers a service
> that idles with a "not yet implemented" log line. The plumbing below is in
> place so the software drops straight in once the hardware arrives.

### One-Line Remote Install

```bash
ssh root@<cerbo-ip> "curl -fsSL https://raw.githubusercontent.com/TechBlueprints/dbus-lynx-i2c/main/install.sh | bash"
```

### Manual Installation

```bash
ssh root@<cerbo-ip>
cd /data/apps
git clone --recurse-submodules https://github.com/TechBlueprints/dbus-lynx-i2c.git
cd dbus-lynx-i2c
bash enable.sh
```

If you cloned without `--recurse-submodules`, initialize them manually:

```bash
git submodule update --init --recursive
```

The install lives under `/data` so it survives Venus OS firmware updates;
`enable.sh` hooks itself into `/data/rc.local` so the daemontools service
symlink is recreated on every boot. `disable.sh` reverses everything.

## Configuration

Optional: copy `config.default.ini` to `config.ini` (git-ignored, survives
updates) to customize:

```ini
[DEFAULT]
distributors = A,B
poll_interval = 5
```

See [config.default.ini](config.default.ini) for all settings (distributor
letters/addresses, populated fuse counts, poll interval, hidraw device
override, I2C speed).

## Service Management

```bash
svc -u /service/dbus-lynx-i2c  # Start
svc -d /service/dbus-lynx-i2c  # Stop
svc -t /service/dbus-lynx-i2c  # Restart
svstat /service/dbus-lynx-i2c  # Status
tail -f /var/log/dbus-lynx-i2c/current | tai64nlocal  # Logs
```

## Repository Layout

| Path | Purpose |
|------|---------|
| `dbus-lynx-i2c.py` | Main service (placeholder until the driver lands) |
| `service/run`, `service/log/run` | daemontools service + multilog logging |
| `install.sh` | Remote installer (clone/update, submodules, enable, start) |
| `enable.sh` / `disable.sh` | Hook/unhook the service and `/data/rc.local` entry |
| `config.default.ini` | Configuration template (copy to `config.ini`) |
| `ext/velib_python/` | Victron D-Bus helper library (git submodule) |
| `tests/` | pytest suite (protocol decode tests land with the driver) |

## Third-Party Software

This project includes [velib_python](https://github.com/victronenergy/velib_python)
by Victron Energy BV as a git submodule at `ext/velib_python/`, licensed under
the MIT License — see [`ext/velib_python/LICENSE`](ext/velib_python/LICENSE).

## License

Apache License 2.0 - see [LICENSE](LICENSE)

## References

- [Victron Lynx Distributor M8/M10 manual (PDF)](https://www.victronenergy.com/upload/documents/Lynx_Distributor/24531-Lynx_Distributor_Manual-pdf-en.pdf) — §3.3 RJ10 pinout/warning, §5 system design, §6.1.3 DIP addressing, §6.2.1 RJ10 chaining
- [Victron Community — How to power the Lynx distributor and read the fuse states with esphome](https://community.victronenergy.com/t/how-to-power-the-lynx-distributor-and-read-the-fuse-states-with-esphome/3534) — I2C protocol reverse-engineering, addresses, status-byte encoding
- [Waveshare USB TO UART/I2C/SPI/JTAG wiki](https://www.waveshare.com/wiki/USB_TO_UART/I2C/SPI/JTAG) — modes, Linux device nodes (M2 → `hidraw*`), demos
- [WCH CH347 datasheet/dev manual](https://www.wch-ic.com/products/CH347.html) — USB command protocol
- [Silabs CP2112 datasheet](https://www.silabs.com/documents/public/data-sheets/cp2112-datasheet.pdf) — fallback adapter; SDA/SCL abs-max 5.8V (5V-bus safe)
