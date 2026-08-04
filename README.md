# dbus-lynx-i2c

Venus OS D-Bus service for **Victron Lynx Distributor fuse monitoring** on a Cerbo GX — without a Lynx Smart BMS. A USB-I2C adapter (Waveshare CH347) plugged into the Cerbo reads each distributor's fuse-status byte over the RJ10 I2C bus and publishes it to D-Bus so blown fuses surface on the GX display and VRM.

> **Status:** hardware validated on paper, adapter ordered; software written but **not yet validated on hardware**. The CH347 HID-I2C driver, status-byte decoder, poller and D-Bus service are implemented with unit tests; the fuse-bit encoding and the CH347 framing still need empirical verification the day the adapter and distributors are in hand (see [Bring-up](#bring-up-first-day-with-hardware)).

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
                                   └── I2C @ 5V, 20 kHz (see Software), one bus for up to 4 distributors
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

## Software

The service is three pure-Python-stdlib modules (no pip on Venus OS):

| Module | Role |
|--------|------|
| [ch347.py](ch347.py) | CH347 HID-I2C driver: sysfs auto-detect (VID:PID `1a86:55dc`, HID interface 1), HID report framing, CH341-compatible I2C command stream, plus a bring-up CLI (`--list/--scan/--read/--watch`) |
| [lynx_distributor.py](lynx_distributor.py) | Status-byte decode (fuse bits, no-supply bit, unpopulated-position masking) plus the fuse-pull verification CLI (`--decode/--watch`) |
| [dbus-lynx-i2c.py](dbus-lynx-i2c.py) | Poller + D-Bus services via `velib_python`; adapter hot-plug recovery and per-distributor disconnect handling |

Protocol sources: WCH's *CH347 Application Development Manual* (in the [Waveshare demo package](https://files.waveshare.com/wiki/USB-TO-UART-I2C-SPI-JTAG/USB-TO-UART-I2C-SPI-JTAG-Demo.zip)) cross-checked against two open-source implementations: [i2cy/CH347-HIDAPI](https://github.com/i2cy/CH347-HIDAPI) (Python) and [serfreeman1337/go-ch347](https://github.com/serfreeman1337/go-ch347) (Go, built from USB captures).

One deviation from the original plan: the CH347's I2C clock is limited to exactly 20/100/400/750 kHz, so the community-proven ~50 kHz is not available. The default is **20 kHz** (closest rate at or below); `i2c_speed_hz = 100000` is a config option once the bus is proven.

### D-Bus services

One service per distributor, digital-input style (following the
`dbus-digitalinputs` conventions — ProductId `0xA166`, `/State` 8=OK/9=Alarm,
`/Alarm` 0/2 — so the GX device list and notifications render it natively):

```
com.victronenergy.digitalinput.lynx_distributor_a   (DeviceInstance 100 + index)
```

| Path | Meaning |
|------|---------|
| `/State` | 8 = OK, 9 = Alarm (any monitored fuse blown, or busbar unpowered) |
| `/Alarm` | 0 = ok, 2 = alarm — drives GX/VRM notifications; gated by `/Settings/AlarmSetting` |
| `/InputState` | 0/1 mirror of the alarm condition |
| `/Connected` | 0 when the distributor stops ACKing (3 consecutive polls) or the adapter is unplugged |
| `/Distributor/StatusByte` | Raw status byte (for the empirical phase and debugging) |
| `/Distributor/NoBusSupply` | 0/1 — the 0x02 bit |
| `/Fuses/1..4/Blown` | 0/1 per populated position, invalid for unpopulated ones |
| `/Count` | OK→Alarm transitions since service start |
| `/CustomName`, `/Settings/AlarmSetting` | Writable, persisted via `com.victronenergy.settings` |

A dead bus deliberately does **not** clear an active alarm — `/Connected`
drops but `/State`/`/Alarm` hold their last value.

> The digitalinput service class is provisional (the "digital-input-style
> vs. generic" question): it is the one class where a custom ok/alarm
> device renders natively in the GX UI, but what VRM makes of it still
> needs to be seen on real hardware.

### Bring-up (first day with hardware)

After the [pre-flight wiring checks](#adapter-configuration-and-wiring), from the repo directory on the Cerbo:

```bash
# 1. Adapter enumerates? (M2 mode → two hidraw nodes, we want interface 1)
python3 ch347.py --list

# 2. Distributors answer? (expect 0x08, plus 0x09... if chained)
python3 ch347.py --scan

# 3. Raw read
python3 ch347.py --read 0x08

# 4. THE empirical step: pull each fuse in turn, watch the bits.
#    Community sources conflict slightly on bit order — verify before trusting.
python3 lynx_distributor.py --watch A
```

If the bit order differs from the documented encoding (`0x10/0x20/0x40/0x80` = fuse 1-4, `0x02` = no supply), fix `FUSE_BITS`/`BIT_NO_SUPPLY` in [lynx_distributor.py](lynx_distributor.py) and the unit tests, and note the finding here.

### Tests

```bash
python3 -m pytest
```

Runs on any dev machine — Venus-only libraries (`dbus`, `gi`, `velib_python`) are stubbed, the CH347 driver is tested against a fake HID transport with wire-format assertions, and the decoder/config parsing are covered directly.

## Installation

> ⚠️ The software has not been validated on hardware yet — until the
> [bring-up steps](#bring-up-first-day-with-hardware) confirm the CH347
> framing and the fuse-bit order, treat what this service publishes as
> unverified.

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
| `dbus-lynx-i2c.py` | Main service: config, poller, D-Bus registration |
| `ch347.py` | CH347 USB-HID I2C driver + bring-up CLI |
| `lynx_distributor.py` | Status-byte decoder + fuse-pull verification CLI |
| `service/run`, `service/log/run` | daemontools service + multilog logging |
| `install.sh` | Remote installer (clone/update, submodules, enable, start) |
| `enable.sh` / `disable.sh` | Hook/unhook the service and `/data/rc.local` entry |
| `config.default.ini` | Configuration template (copy to `config.ini`) |
| `ext/velib_python/` | Victron D-Bus helper library (git submodule) |
| `tests/` | pytest suite: driver wire format, decoder, config parsing |

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
