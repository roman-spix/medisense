# Firmware for the ESP32-S3

MediSense installs RuView's CSI firmware on your board for you when you plug
it in for the first time. You never need to build or flash anything by hand.
This folder holds the ready-made images and how they were made.

| Folder | For boards with | Flash layout |
|---|---|---|
| `esp32s3-8mb/` | 8 MB or more flash (ESP32-S3-DevKitC-1 N8R8, N16R8, XIAO ESP32-S3, ...) | RuView `partitions_display.csv` |
| `esp32s3-4mb/` | 4 MB flash | RuView `partitions_4mb.csv` |

The app reads the chip's flash size and picks the right folder.

## What it is

RuView `firmware/esp32-csi-node` at tag `v0.8.8-esp32` (commit `e04f269f`),
built with ESP-IDF 5.4 in GitHub Actions (`.github/workflows/firmware.yml`,
script `build/build.sh`). Version 0.8.8 is the newest RuView release with a
recorded physical test on an ESP32-S3 in raw-CSI mode, which is exactly how
MediSense uses the board.

Changes from RuView's own release images, and why:

1. **Display support compiled out** (`CONFIG_DISPLAY_ENABLE=n`). RuView's
   8 MB release image includes display support, and on plain boards without a
   screen its panel check can misfire and drop the CSI rate to zero. RuView's
   `sdkconfig.defaults.devkitc` documents this; MediSense applies the same fix.
2. **The 40 Hz flickering LED is off** (`CONFIG_LED_GAMMA_VIZ=n`). RuView
   drives the board's RGB LED as a 40 Hz flicker by default. Not something to
   have next to you while meditating.
3. **WASM modules off** (`CONFIG_WASM_ENABLE=n`): not used, and RuView's
   qualification build didn't have them either.
4. **The 4 MB image keeps RuView's network-buffer fixes.** RuView's own 4 MB
   release replaces its base settings instead of layering on them and so
   loses the buffer sizes that stop frames being dropped; MediSense layers.
5. **`led_strip` pinned to 3.0.x** (`build/patches/0001-led-strip-idf54.patch`):
   newer versions need ESP-IDF 5.5, so the 0.8.8 tag no longer builds without
   it. RuView made the same fix later upstream.
6. **MediSense 2** (`build/patches/0002-medisense-csi-sensors.patch`), after
   the first real test (signal only "fair", ~8 s dropouts):
   - *Cleaner frames.* RuView limited the CSI rate to 50 frames/s **before**
     filtering by sender, so on a busy network most of the 50 slots went to
     frames that were then thrown away. Now the board filters first (frames
     from your router only: the configured filter, else the router it's
     connected to) and prefers frames the router sends **to the board**
     (self-ping replies and the app's pacer packets: one transmit chain,
     directly comparable). Other frames only fill gaps over 0.5 s.
   - *Flags and timestamps.* Byte 19 marks frames addressed to the board
     (bit 5) and the radio's invalid-first-word case (bit 6); bit 7 means a
     4-byte receive timestamp (µs, the radio's own clock) follows the CSI.
     Older readers ignore all three.
   - *No 1-second holes.* RuView's gateway pinger waited 1 s for each lost
     reply; the timeout is now twice the ping interval.
   - *(MediSense 2 also kept capture to management frames only, to save
     radio load. That was a mistake: as RuView#521/#893 describe, on a board
     without a display it starves the CSI engine, and the board joined the
     WiFi but sent nothing. MediSense 3 keeps RuView's setting again.)*
   - *Never gives up on WiFi.* RuView stopped reconnecting after 10 failures
     in a row; now it retries every 3 s for as long as it takes, refreshes
     the router address on reconnection and restarts the pinger.
   - *Radar.* The mmWave driver also probes the HLK-LD6002 at 1,382,400 baud
     (same TinyFrame protocol as the Seeed MR60BHA2) and forwards the radar's
     raw bytes to the app (magic `0xC511D0A1`), so MediSense Pro sees every
     frame type, not only the parsed rates.
   - *Pulse sensor.* New `medisense_ppg.c`: a MAX30102/MAX30101/MAX30105 on
     I2C (SDA GPIO8, SCL GPIO9) is detected at boot and streams red + IR at
     100 Hz in small batches (magic `0xC511D0B1`). Without a sensor nothing
     runs.

7. **MediSense 3** (same patch file, after the first test of MediSense 2):
   - *Data frames are captured again* (see above), so the board always has
     frames to measure.
   - *Never silent because of the sender filter.* If frames keep arriving but
     none from the router for 5 s (a wrong router address, mesh units), the
     board uses frames from every sender until it reconnects.
   - *It says what it hears.* Every 10 s the board logs how many frames it
     heard, used, and why it set others aside (`medisense stats: ...`), and at
     start-up `MediSense firmware 3`. The app shows this on the Board tab, so
     "the board hears nothing" and "the board sends but nothing arrives" look
     different.

   Boards set up with an earlier image are updated automatically the next
   time they're plugged in (when the WiFi password is in the Keychain;
   otherwise the app asks for it once).

Each folder holds only what the app flashes: the four images,
`flasher_args.json` (flash offsets), `BUILDINFO.json` (exact inputs) and
`SHA256SUMS` (checked before every flash). `build/dependencies.lock.*` pins
the ESP-IDF component versions used, so rebuilds are repeatable.

The build log, resulting `sdkconfig` and `image_info` for the current images
are on the `firmware-build` branch (run 41903308358); those of the first
images are in commit `bfb3173`. ESP-IDF builds aren't byte-identical, so a
later rebuild produces different (equivalent) files.

## How it was checked

- The build script verifies the final configuration (display, LED and WASM
  off; CSI on; flash size; partition table; buffer sizes).
- The first images (before MediSense 2) were booted in Espressif's QEMU
  (esp32s3) with a settings partition generated by MediSense; the firmware
  printed every setting back. The MediSense 2 images compiled cleanly (one
  pre-existing RuView warning) and their image checksums and validation
  hashes verify; they were not booted in QEMU (the WiFi-dependent parts
  can't run there anyway). The patch only touches code that runs after the
  settings are read.
- Not yet checked on a physical board: that happens at your first setup,
  where the app confirms the board joined your WiFi and that CSI frames
  arrive on your Mac.

## Settings the app writes (NVS partition at 0x9000)

`ssid`, `password`, `target_ip` (your Mac), `target_port` (5005), `node_id`
(1), `edge_tier` (0 = raw CSI only, the mode RuView qualified on the S3), and,
once the board has seen your router, `filter_mac` (the router's address, so
the board ignores neighbours' WiFi).

## Rebuilding

Push a change under `firmware/build/` or run the "Firmware (ESP32-S3)"
workflow by hand on GitHub. Results land on the `firmware-build` branch
together with the build log and configuration. After checking `STATUS.md` and
the `ok:` lines in `build.log`, copy the `.bin` files, `flasher_args.json`,
`BUILDINFO.json` and `SHA256SUMS` of each variant into this folder.
