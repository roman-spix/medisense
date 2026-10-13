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
