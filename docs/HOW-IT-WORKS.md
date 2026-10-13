# How MediSense works

A technical overview for whoever develops MediSense further.

## Data path

```
 router traffic ──► ESP32-S3 (RuView firmware 0.8.8, raw CSI mode)
                        │ UDP :5005, ~25–50 packets/s, ADR-018 frames
                        ▼
 Mac: medisense ── receiver ─► VitalsEngine ─► metrics (1 Hz), breath wave (20 Hz)
                        │                          │
                        ├─► SessionRecorder ◄──────┤  (~/MediSense/sessions/…)
                        └─► web server (127.0.0.1:8765) ─ WebSocket ─► browser dashboard
                                                                     ▲
                                        webcam ─► MediaPipe Pose (in browser) ─┘ posture angles
 USB ─► DeviceManager: detect, flash firmware + WiFi settings (NVS), read the board's log
```

## The board

- Firmware: RuView `firmware/esp32-csi-node` v0.8.8, built for plain S3 boards
  (see [firmware/README.md](../firmware/README.md)). Edge processing is off
  (`edge_tier=0`); the Mac does all the analysis.
- The firmware pings the router at up to 50 Hz, so the router's replies provide a
  steady source of CSI frames even in a quiet network.
- Settings are written to the NVS partition (`csi_cfg` namespace): `ssid`,
  `password`, `target_ip` (the Mac), `target_port` 5005, `node_id` 1,
  `edge_tier` 0 and, when known, `filter_mac` (the router's 2.4 GHz BSSID, found
  with macOS's `airport` tool). With the filter, the board ignores neighbours'
  WiFi. If no frames get through with the filter (e.g. a mesh unit with a
  different address), MediSense rewrites the settings without it.
- Packet format (`protocol.py`): 20-byte header (magic `0xC5110001`, node,
  antennas, subcarriers, frequency, sequence, RSSI, noise floor) + int8 I/Q
  pairs (imaginary first). Only the first 64 subcarriers (the LLTF block, present
  in every frame type) are used.

## Analysis (`vitals.py`)

1. **Normalise:** amplitude per subcarrier divided by the frame's mean amplitude
   (cancels automatic gain changes).
2. **Resample:** average into a uniform 20 Hz grid; fill gaps up to 1 s; restart
   after longer gaps.
3. **Movement:** mean absolute change of the normalised pattern between samples,
   smoothed, divided by the 25th percentile of the last 2 minutes. Thresholds:
   restless ≥ 2.2×, moving ≥ 5×. A *shift* is ≥ 5× for at least 0.5 s; it ends
   after 2 s below 2.5×.
4. **Breathing:** band-pass 0.07–0.75 Hz (4–45 /min, zero-phase), first
   principal component across subcarriers, spectral peak with parabolic
   interpolation. Cross-checked by peak counting; agreement and the number of
   cycles in the window set the confidence. Window 45 s (longer for slow
   breathing, up to 85 s); windows restart 3 s after a shift. Median of the last 5
   accepted estimates. Evenness = 1 − coefficient of variation of the
   breath-to-breath intervals.
5. **Live breath wave:** the same band-pass run causally per subcarrier,
   projected on the current PCA weights. Lag is well under a second.
6. **Heart rate:** band-pass 0.8–2.2 Hz, the 12 subcarriers with the most peaked
   spectrum, PCA, spectral peak excluding breathing harmonics, a mild preference
   for the previous estimate, median of 7. Shown only above a confidence floor,
   and always labelled rough.

Synthetic tests (`tests/test_core.py`) check that known breathing rates (5, 9 and
16 /min) are recovered within 0.6 /min, heart rate within 4 bpm on a clean signal,
and shifts are found within 2 s. **Real-world accuracy is not yet measured.**
Your pilot sessions with Polar entries and breath counts are the first
ground-truth data. `medisense reprocess <session>` re-runs the analysis over the
raw capture after algorithm changes.

## Posture (`web/posture.js`)

MediaPipe Pose Landmarker (lite model, ~8 fps, GPU with CPU fallback) runs in the
browser. Per frame: head height above the shoulder line divided by shoulder
width (drops when slumping), head tilt (ear line), shoulder tilt and, if the hips
are visible, torso lean. The first 8 s after the camera starts (or
**Recalibrate**) define *your* upright baseline. Score = 100 minus penalties for
head drop (20 % drop → −40) and for tilts/lean beyond small tolerances. A *slump*
event is ≥ 20 % head drop for 15 s. Only these numbers reach the app; video is
never stored or sent.

## Device lifecycle (`device.py`)

Poll USB serial ports every 2 s (Espressif native USB `303A`, CP210x, CH34x,
FTDI). For a new port, run `esptool flash_id` to get the chip, MAC and flash
size. Boards are remembered in `~/MediSense/devices.json` by MAC with the
firmware hash and settings:

- unknown board → the wizard asks for WiFi, then flashes bootloader, partition
  table, otadata, app and NVS in one `write_flash`, verifying `SHA256SUMS` first
- known board, current firmware, the Mac's IP changed → rewrite NVS only
- otherwise → reset the board and watch its log

The serial log (opened with DTR/RTS low so it doesn't reset the board) gives the
WiFi state, disconnect reasons (mapped to plain-language advice), the board's IP
and the CSI callback count. WiFi passwords are kept in the macOS Keychain
(service "MediSense WiFi").

## Security and privacy

- The web server binds to 127.0.0.1 only and rejects requests whose `Host`
  isn't localhost (DNS rebinding). State-changing requests need an
  `X-MediSense: 1` header, which other websites can't send cross-origin
  (CSRF).
- No telemetry, no cloud. Downloads: uv and Python at install time, and the
  MediaPipe files (hash-pinned) on first camera use.

## Ideas for the product

- A second board on the opposite side improves heart rate and allows motion
  localisation (RuView supports multi-node).
- Learn per-person movement thresholds and a breathing-depth estimate.
- Camera rPPG (pulse from skin colour) as a second heart-rate source in good light.
- Package as a signed macOS app (py2app/Briefcase) once the pilot settles; sign
  and notarise to avoid Gatekeeper prompts.
