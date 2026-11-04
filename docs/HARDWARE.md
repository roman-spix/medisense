# Hardware guide

## The board

**Buy: Espressif ESP32-S3-DevKitC-1, variant N16R8 (or N8R8)**, about $12–18.
"Espressif" is the chip maker's own board; good clones work too.

| Look for | Why |
|---|---|
| **ESP32-S3** in the name | The firmware is built for the S3. A plain "ESP32", "ESP32-C3" or "ESP32-C6" will not work. |
| Module **WROOM-1** (not WROOM-1**U**) | WROOM-1 has an antenna on the board. The "U" version needs an external antenna. |
| Flash **8 MB or 16 MB** (the "N8"/"N16" part) | 4 MB boards also work (MediSense has an image for them). |
| Two USB ports, one labelled **USB**, one **UART** | Either works. The **USB** port needs no driver on macOS. |

The "R8"/"R2" part (PSRAM) doesn't matter; MediSense doesn't use it.

Also fine: Seeed Studio **XIAO ESP32-S3** (attach the antenna that comes in the
box). Avoid boards with a built-in display: they work, but the display isn't
used and only adds cost.

## The cable

The board has USB-C (older DevKitC revisions: micro-USB). A MacBook Pro 2014 has
only USB-A ports, so get a **USB-A to USB-C cable that carries data**. Cables
sold for phone charging sometimes carry power only. If the board's light comes
on but MediSense never sees it, try another cable.

## Where to put it

WiFi sensing works best when your body is in the path of the signal:

```
   router ))))   ->   you (sitting)   ->   board
   (same or next room)       1–2 m
```

- Board 1–2 m from you, roughly at chest height (a shelf, a chair, a stack of books).
- The router should be in the same room or the next one. You don't need to move
  it; just place the board so you sit roughly between them.
- Keep the board still. Don't put it next to a fan, a fridge, a microwave or a
  metal surface.
- Other people or pets moving in the room disturb the readings.

Once set up, the board doesn't need the Mac. Any USB phone charger can power
it, and the data reaches the Mac over WiFi. It only needs to be plugged into
the Mac for the first setup, or when the Mac's network address changes
(MediSense then updates the board automatically when you plug it in).

## Polar FT1

The FT1's chest strap talks to the watch over an old 5 kHz radio link, not
Bluetooth, so the Mac can't read it directly. During a session press **H** and
type the number the watch shows. At the end, enter the session average from the
watch. MediSense charts your entries next to its own estimate.

## MediSense Pro: a radar and a pulse sensor

WiFi alone sees stillness well and breathing reasonably, but not your heart:
in the first real test it was off by up to 20 beats per minute against a
Polar strap. Two small modules wired to the same ESP32-S3 fix that. Run
`medisense pro` (or double-click a launcher that runs it) instead of
`medisense start`; everything else stays the same.

| Module | What it adds | Cost |
|---|---|---|
| **HLK-LD6002** 60 GHz vital-signs radar | Breathing rate and waveform straight from the chest's movement (millimetre-precise), a second heart-rate source, presence | ~$8–15 |
| **MAX30102** pulse sensor breakout (or MAX30101 / MAX30105) | Every heartbeat: an exact heart rate, heart-rate variability (RMSSD, SDNN) and coherence | ~$3–8 |

MediSense picks the best source for each number: heart rate from the pulse
sensor, then the radar (never WiFi); breathing from the radar, then WiFi,
then the breathing rhythm hidden in your heartbeat.

### Radar: HLK-LD6002

A 60 GHz radar on a 2 × 2 cm board, 3.3 V, serial (UART) at 1,382,400 baud.
It detects breathing and heartbeat from about 0.4 to 1.5 m
([Electropeak](https://electropeak.com/hi-link-ld6002-60g-heart-rate-and-breathing-monitoring-module),
[LCSC](https://www.lcsc.com/product-detail/C37897213.html)). Its frames
(breathing 0x0A14, heart 0x0A15, distance 0x0A16, phases 0x0A13) follow the
TinyFrame layout, as in the open-source
[hlk_ld6002 driver](https://github.com/icewind1991/hlk_ld6002).

| LD6002 pin | ESP32-S3 pin |
|---|---|
| 3V3 | 3V3 |
| GND | GND |
| TX | GPIO **18** (the board's RX for the radar) |
| RX | GPIO **17** (the board's TX for the radar) |

Place it 0.5–1 m in front of you at chest height, aimed at your breastbone,
with nothing in between and no fan or curtain in its view. The MediSense 2
firmware finds it by itself at power-up (it tries 115,200 and 1,382,400
baud) and forwards everything it says to the app.

*Alternative:* the Seeed **MR60BHA2** radar speaks the same protocol at
115,200 baud and is detected the same way. Seeed mostly sells it as a kit
with its own small ESP32-C6 board
([Seeed wiki](https://wiki.seeedstudio.com/getting_started_with_mr60bha2_mmwave_kit/),
[ESPHome](https://esphome.io/components/seeed_mr60bha2/)); the bare LD6002
is simpler to wire to your S3.

Honest limits: Seeed says its radar's heart and breathing readings are meant
mainly for someone lying still, and can be well off for someone sitting at a
desk ([Seeed wiki](https://wiki.seeedstudio.com/getting_started_with_mr60bha2_mmwave_kit/)).
Sitting in meditation is much stiller than desk work, but treat the radar's
heart rate as the backup; the pulse sensor is the reference.

### Pulse sensor: MAX30102

The red/infrared sensor found in fitness bands, on a small I2C breakout.

| Sensor pin | ESP32-S3 pin |
|---|---|
| VIN (or 3V3) | 3V3 |
| GND | GND |
| SDA | GPIO **8** |
| SCL | GPIO **9** |
| INT | not needed |

Use a 30–50 cm four-wire cable so the board can stay on its shelf. Rest a
fingertip on the sensor with a soft strap or a finger clip (light pressure:
pressing hard flattens the pulse), or use an earlobe clip; the hands can
then stay in the meditation mudra. The firmware samples red and infrared at
100 per second and the app finds each beat; it needs about a minute of
steady beats before heart-rate variability and coherence appear.

### Power

Both modules run from the board's 3V3 pin. Power the board from the Mac or a
good USB charger (1 A or more): the radar draws noticeably more than the
board alone, and a weak supply shows up as restarts ("power dip") in the
Interruptions list on the Board tab.

### Other ideas (not built in yet)

- A **second ESP32-S3** as a dedicated transmitter on the far side of you
  would give a steadier WiFi signal than the household router.
- A **load-cell cushion** (four bathroom-scale cells and an HX711) would
  measure stillness and breathing through the seat.
