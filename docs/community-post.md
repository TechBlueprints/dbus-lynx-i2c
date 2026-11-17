# Draft: Victron Community post

Draft reply for the Victron Community thread on reading Lynx Distributor fuse
states without a Lynx Smart BMS. Written as a how-to someone can repeat, not a
narrative of how this project got there. Not yet posted.

---

**How-to: Lynx Distributor fuse monitoring on a Cerbo GX — without a Lynx Smart BMS (~$25)**

This gets blown-fuse alarms from your Lynx Distributors onto the GX's native
Fuses pages, GX notifications (with your own fuse names, so the alarm says
"Fuse blown — Battery feed", not "fuse 3"), the buzzer, and VRM — using a USB-I2C
adapter instead of a Lynx Smart BMS. Software is open source:
**https://github.com/TechBlueprints/dbus-lynx-i2c**

**You'll need**

- Waveshare **"USB TO UART/I2C/SPI/JTAG"** adapter (WCH CH347 chip, ~$24)
- The **Victron RJ10 cable that ships with each distributor** — use this one, not
  a generic phone cord; its conductor gauge matters (see troubleshooting)
- A way to join four wires: I used an RJ45 crimp + RJ45 keystone jack for a clean
  wall-plate install (RJ45 pins 3/4/5/6 → RJ10 pins 1/2/3/4), but an RJ11/RJ9
  punch-down jack or an RJ9-to-screw-terminal adapter works the same
- A **multimeter** — not optional. Victron's manual (§3.3) states the RJ10
  connection "is not protected against reverse polarity" and that a wrong
  connection "can irreversibly damage the Lynx Distributor's electronic circuits."
- Distributors with serial **HQ1909 or later**

**1. Configure the adapter (while unplugged)**

Mode DIP to **M2** (S1 ON, S2 OFF — it only latches at power-on), level slide to
**5V**. In M2 the adapter is a driverless USB-HID device on stock Venus OS and
survives firmware updates.

**2. Wire the adapter's I2C header to the RJ10 cable**

Cut the plug off *one* end of the Victron cable; the surviving plug goes into the
distributor. Identify wires **by position, not colour** — but for reference, the
Victron cable is:

| Adapter pin (printed on case) | RJ10 pin | Victron wire |
|---|---|---|
| VCC | 1 | yellow |
| SDA | 2 | green |
| SCL | 3 | red |
| GND | 4 | black |

**3. Verify with the multimeter — before the plug ever touches a distributor**

1. *Continuity:* beep each conductor from your joint through to the plug's gold
   contacts. (Contacts toward you, clip behind, cable down → pin 1 is leftmost.)
2. *Polarity, powered:* adapter on USB, black probe on pin 4, red on pin 1:
   **+5.0 V, positive sign**. Negative or zero → stop and fix.
3. *Data lines:* pins 2 and 3 to pin 4 should each read **~5 V** (the adapter's
   pull-ups idling high).

**4. Set addresses and connect**

Inside each distributor, the 2-way DIP sets its identity: **A = both off,
B = switch 1 on** (C/D continue the pattern). Plug your cable into distributor A;
daisy-chain A→B with the second, uncut Victron cable. On power-up each unit
flashes all LEDs for a second, then blinks one fuse LED — fuse 1 = address A,
fuse 2 = address B. That blink confirms your DIP settings.

**5. Verify at the distributor, under load**

With everything connected and LEDs live, measure 5 V-to-GND again at the *far*
distributor (its free jack, two outermost springs): **4.5 V minimum**.
Open-circuit voltage means nothing here — thin conductors can read a perfect 5 V
unloaded and still collapse under load.

**6. Install the software**

```
ssh root@<cerbo-ip> "curl -fsSL https://raw.githubusercontent.com/TechBlueprints/dbus-lynx-i2c/main/install.sh | bash"
```

Then name your circuits in `/data/apps/dbus-lynx-i2c/config.ini` and restart:

```ini
[DEFAULT]
distributors = A, B
fuse_names_a = Main feed, Solar, DC panel, Charger
fuse_names_b = Inverter, Inverter 2, Battery 1, Battery 2
```

Sanity-check the bus anytime with `python3 ch347.py --scan` from the install
directory — you should see `0x08` (and `0x09` with a second distributor). The
Fuses pages appear under the device in the GX menu, and a blown fuse alarms by
name.

**Troubleshooting — if you see this, do this**

- **All LEDs stuck on (power LED amber, every fuse LED red), no I2C response:**
  your 5 V is collapsing under load — the conductors in your RJ10 run are too thin
  (typical of generic RJ9/handset cords) or a crimp isn't biting. The
  distributor's microcontroller sits stuck in boot and will never answer. Use the
  heavier-gauge Victron cable and re-check step 5. This one cost me the most time:
  the cable read a perfect 5 V open-circuit and still failed.
- **Distributors boot normally (you see the address blink) but nothing ACKs:**
  SDA and SCL are swapped somewhere in your run — easy to do through a keystone or
  crimp, and harmless to get wrong. Swap the two data wires at your joint and
  rescan.
- **Two distributors, only one answers:** revisit the DIP switches — both units
  answer `0x08` if B was never changed.
- **Occasional wrong readings / phantom blown fuses:** the distributor aborts its
  transmission roughly 60 µs into the data byte, so at a flat 20 kHz the byte gets
  truncated and the pull-ups fill the rest with 1s (values like `0x7F`, `0x3F`,
  `0x1F` — always 2ⁿ−1). Two things help, both implemented in the linked project:
  reject any byte with bits outside the documented encoding (a truncated byte
  always sets bit 0, which is not a valid protocol bit, so this catches the whole
  failure mode), and clock just the *data byte* fast. The I2C speed opcode is part
  of the CH347 command stream, so you can keep START/address at 20 kHz, clock the
  status byte at 750 kHz (~11 µs, beating the abort), then drop back to 20 kHz for
  the closing NACK — a NACK delivered at high speed is missed by the slave, which
  then holds SDA and blocks the next transaction. That took truncated reads from
  roughly one every 20–30 minutes to two in 75 days.

Not affiliated with Victron. Protocol knowledge comes from this community's
reverse-engineering plus twam's dbus-lynx-distributor and others — all credited in
the repo. Mind the manual's live-busbar safety warnings when working inside the
Lynx.
