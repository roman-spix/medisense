# How MediSense works

A technical overview for whoever develops MediSense further. Version 2,
rebuilt after the first real-device test (router 2 m behind the meditator,
board 1 m in front, Polar FT1 as reference): heart rate was unstable and about
20 bpm off, breathing showed 6.7 and spikes of 12–16 against a true 5 /min,
movement read 0.0 all session (even when getting up), and the data dropped out
for ~8 s at a time. Each of those has a fix below.

## Data path

```
 router ──frames to the board──► ESP32-S3 (RuView 0.8.8 + MediSense 3 patch)
   ▲  self-ping replies + the Mac's 50 Hz pacer │ UDP :5005
   │                                            │  CSI (0xC5110001), radar bytes (0xC511D0A1),
 Mac ── UDP pacer to the board's port 9 ────────┘  pulse samples (0xC511D0B1)
                                                ▼
 medisense ─ VitalsEngine ─► metrics 1 Hz + breath wave 20 Hz ─┐
           ─ PracticeState (insights) ◄─────────────────────────┤
           ─ PostureService ◄─ posture_engine.py (own venv, camera, MediaPipe Pose)
           │                  or landmarks from the page's camera (fallback)
           ─ ProApp (radar + pulse, `medisense pro`)
           ─ SessionRecorder (~/MediSense/sessions/…) and web server 127.0.0.1:8765 ─► browser
 USB ─► DeviceManager: detect, flash firmware + WiFi settings (NVS), read the board's log
 medisense posture ─► the same posture engine and scorer, all day, port 8766
```

## The board (firmware: see [firmware/README.md](../firmware/README.md))

- RuView `esp32-csi-node` 0.8.8, edge processing off; display, 40 Hz LED and
  WASM compiled out.
- **MediSense patch, version 3** (`firmware/build/patches/0002-…`):
  frames are filtered by sender *before* RuView's 50 Hz rate gate (upstream
  gated first, so other devices' frames used up the slots); frames the router
  addresses to the board are preferred (one transmit chain, comparable frame
  to frame; others fill gaps > 0.5 s); byte 19 flags: bit 5 addressed to the
  board, bit 6 invalid first word, bit 7 a 4-byte receive timestamp (µs)
  follows the CSI; the gateway pinger's timeout is twice its interval (a
  lost reply used to stall the stream for 1 s); WiFi reconnects forever
  (upstream gave up after 10 failures); radar bytes and pulse samples are
  forwarded raw. Version 3: RuView's management + data promiscuous capture
  is kept (version 2 kept management frames only, which on a board without a
  display starves the CSI engine: the board joined the WiFi and sent
  nothing); the sender filter gives way after 5 s without frames from the
  router; and every 10 s the board logs what it heard (`medisense stats:
  heard= used= to_board= other= foreign= rate_drop= any_sender=`).
- **Where the data goes:** the board's `target_ip` is this Mac's address on
  the board's own subnet (read from the network interfaces), not the route to
  the internet, which leads into the tunnel when a VPN is on. The board logs
  its target at start-up; if it isn't this Mac, the app rewrites the settings
  once by itself. The Board tab shows *Board hears* and *Board sends to*.
- **Pacer:** the app sends a 2-byte UDP packet to the board's port 9 fifty
  times a second while data flows. The router forwards each to the board,
  which makes one more CSI frame, independent of how the router treats pings.
- Settings live in the NVS partition (`csi_cfg`): `ssid`, `password`,
  `target_ip`, `target_port` 5005, `node_id`, `edge_tier` 0, and `filter_mac`
  (the router's 2.4 GHz BSSID) when known; without it the v2 firmware uses
  the BSSID it's connected to.

## Analysis (`vitals.py`)

**Frames.** Only the dominant frame type (by CSI length: L-LTF only, +HT-LTF,
+STBC) is used, and only frames addressed to the board when the firmware
marks them. With the v2 timestamp, frames are placed on the board's radio
clock (mapped to the Mac's clock by the minimum offset over 30 s), so WiFi
jitter doesn't smear the waveform. Each frame becomes a vector of
log-amplitudes per subcarrier minus the frame mean (cancels gain jumps).
Phase is not used: after removing the radio's random phase and timing slope
it's mostly noise, and in simulation frames meant for other devices
(beamformed, different in phase) made breathing lock onto slow drifts.
Amplitude-only removed every wrong reading in that test (178 → 0 wrong
seconds) at equal accuracy on the new firmware. Frames whose amplitude
pattern doesn't correlate (> 0.6) with the recent median are set aside; a
flood of them means the person is moving.

**Gaps.** Up to 10 s are bridged (the analysis continues); the page says
"Signal paused" after 2.5 s and "no signal" after 10 s; after 60 s everything
starts fresh.

**Movement.** Every 0.25 s: the robust spread (MAD) of each subcarrier's
amplitude over the last second (at least 8–12 real frames, never padded),
divided by that subcarrier's own quiet level (its 25th percentile over 30 s).
The statistic is the 80th percentile across subcarriers (movement changes the
channel broadly), or the RSSI wobble ratio if larger. Restless ≥ 1.8×,
moving ≥ 3.5×. (v1 padded a sparse stream with copies, so its quiet level
became 0 and movement read 0.0.)

**Breathing.** Window: 6 breaths at the last rate, 45–150 s (120 s and at
least 40 s of quiet for the first reading). Band-pass 0.05–0.8 Hz,
standardise, blank the gap filler. Then the autocorrelation of every
subcarrier, minus the autocorrelation the band-pass filter itself puts into
noise (otherwise an empty room shows a ~1.5 s "rhythm"), averaged with
weights for subcarriers that repeat in the breathing range. Each candidate
period scores its peak *minus the dip at half that period*: at the true period
the breathing subcarriers line up and at half of it they're out of step, so a
harmonic (slow breathing has strong 2nd–4th harmonics) or a subcarrier near a
multipath null that "doubles" can't win; the earliest strong candidate must
repeat at twice its period. Periodic component analysis gives the composite
waveform (the live wave and breath-to-breath evenness); the period is refined
on it and checked against its spectrum. A majority vote over recent estimates
(6 of 10 within 15 %) decides what's shown, and nothing is shown that was
estimated before your last movement.

Simulation (six breathing rates 3–20 /min × six rooms, a shift at 90 s; see
`tests/test_core.py` for the fixed cases): with the v2 firmware, a reading
99 % of the time when one is due, median error 0.2 %, no reading off by more
than 15 %. With the original firmware (1 s stalls, frames for other devices)
a reading 66 % of the time, median error 0.4 %, and 2 seconds out of about
2,000 off by more than 15 % (260 before phase was dropped).

**Heart rate.** Estimated the same way in 45–150 bpm and logged as
`heart_rate_experimental`, never shown as a trusted number: one board's WiFi
can't do it reliably, and v1 showed 55–100 against 74–80. A pulse sensor or
radar provides it (MediSense Pro).

## Practice insights (`insights.py`)

Every second gets a state: **steady** (still ≥ 60 s), **settling**, **restless**
(movement), **drowsy** (head dropped ≥ 10 s or a nod in the last 60 s; needs the
camera) or **away** (no breathing, movement or camera person for 20 s). Short
data gaps keep the state for up to 10 s. The same code runs live and over
recorded sessions, so old sessions get the new numbers too
(`SUMMARY_VERSION`).

Per session: time to settle (start of the first 3-minute still stretch),
steady minutes and share of the sitting, longest still stretch, restless and
drowsy episodes (merged within 10 s / 30 s), breathing while steady (median),
posture aligned share, data coverage. Across sessions (`profile`): last 7 vs
the 7 before, sessions per week (over 4 weeks or since the first session),
streak, trends, which obstacle shows more (restless ≥ 1 per 10 min and 1.5×
drowsy → excitation; drowsy ≥ 0.5 per 10 min and more than restless →
laxity), a stage and tailored advice. Advice draws on traditional remedies:
for agitation relax the body, lower the gaze, lengthen the out-breath; for
dullness sit up, raise the gaze, brighten attention, shorter sessions.

## Posture (`posture.py`, `posture_engine.py`, `posture_service.py`)

**Camera.** By default the page itself runs MediaPipe Pose (lite, on the
graphics chip via WebGL when possible) on the camera: 6 checks a second in
MediSense, 3 in MediSense Posture, and about one a second when the page is
in the background (an active camera keeps Chrome from throttling it
further). Only the 33 landmark positions go to the local app, which does all
the scoring. Page code is served under a versioned path (`/v/<version>/`)
with `no-cache`, so a browser can never mix cached old code with a new page.

**Optional background engine.** Off by default, because on a 2014 MacBook it
loaded the machine too much. A separate process in its own environment
(`.venv-posture`) reads the camera with OpenCV (AVFoundation) and prints
landmarks as JSON lines in MediaPipe's 33-point layout; it works with the
page closed. On a
Mac the body is found by Apple's **Vision** framework
(`VNDetectHumanBodyPoseRequest`, built into macOS 11+) through PyObjC: every
MediaPipe build still on PyPI imports C++ library functions that macOS only
has from 12.3 (e.g. `std::stringbuf` move-assignment, libc++ 12), so it can't
load on Big Sur. Every compiled file of the Mac engine (PyObjC 10.3.1, NumPy
1.26.4, OpenCV 4.8.1.78) was checked to need macOS 11.0 or older, and CI
compares its landmarks on a test photo with MediaPipe's. Vision gives 2-D
points: distance from the camera is judged by the size of the face and the
shoulders in the picture. On Linux the engine uses MediaPipe Pose Landmarker
(full). If the engine can't run, the page's own camera (MediaPipe in the
browser) takes over automatically; scoring is identical. Video never leaves
the engine except as an optional small preview while you look at it; nothing
is stored.

**Two scorings.** *Meditation* (MediSense) follows the seven-point posture
of Vairocana as Lama Yonten Gyaltso teaches it in *Nectar of Liberation*, in
his order: 1 legs (vajra posture or cross-legged; information only, when in
view), 2 hands (palms four finger-widths below the navel, right on left,
thumbs joined: wrists together, low, centred), 3 back (fully straight: no
slouch; and his four errors, leaning right, left, forward or back, each with
the affliction he says it stirs), 4 shoulders (open and level: level, not
hunched; a shrug raises the shoulders while the nose stays, a head drop lowers
the nose while the shoulders stay), 5 head (chin lowered, lightly drawn in:
pitch vs calibration; not sinking, not pushed forward, level), 6 tongue (not
visible to a camera), 7 gaze (down along the nose, eyes half-closed, steady:
the eyes aren't checked, but the head turning away is). *Office* (MediSense
Posture) checks only a healthy working posture with the hands and legs free:
back upright (slumping: torso height, or with the hips out of view both the
shoulders and the head sinking; leaning in towards the screen), shoulders
relaxed and level, head over the shoulders (not pushed towards the screen,
not hanging; looser limits for glancing at the keyboard), sitting centred. Level checks compare with your calibration
(bodies and cameras are rarely level); penalties under one point are ignored.
Score = 100 − penalties, smoothed over 2 s. Reminders when the score stays
below the threshold for the hold time, at most once per cooldown (MediSense
during sessions: below 80 for 30 s, every 5 min; MediSense Posture: below 80
for 20 s, every 3 min). Nods (the chin dropping fast and coming back) and
slumps (head 15 % lower for 15 s) become session events and feed "drowsy".
Calibrations are kept per scoring in `~/MediSense/posture.json`, settings
per app. Notifications use `osascript` (shown as Script
Editor).

## MediSense Pro (`pro.py`)

**Radar** (HLK-LD6002 at 1,382,400 baud, or MR60BHA2 at 115,200): TinyFrame
frames (SOF 0x01, big-endian ID/length/type, ~XOR checksums; floats
little-endian): 0x0A14 breathing, 0x0A15 heart rate, 0x0A16 distance, 0x0A13
phases, 0x0F09 presence. Frames with an unexpected header checksum are still
accepted when type, exact length and data checksum match.

**Pulse sensor** (MAX30102, red + IR at 100 Hz, timed by the board's sample
clock): the channels are summed, band-passed 0.6–4 Hz and inverted; a beat
is the steepest point of the upstroke (the slope's local maximum above 45 %
of the recent maximum), refined between samples, at most 180 /min. Intervals
are accepted within 0.33–1.6 s and 30 % of the recent median. Heart rate =
median of the last 8; RMSSD and SDNN over 60 s; coherence = the share of the
4 Hz-resampled interval series' power (64 s) within ±0.015 Hz of its
strongest peak in 0.04–0.26 Hz, whose frequency is the resonance (usually
breathing) rate.

**Best source:** heart rate from the pulse sensor (≥ 60 % clean beats), else
the radar; breathing from the radar, else WiFi, else the heart's breathing
rhythm (coherence ≥ 0.3). Breathing numbers are observations, never targets:
the practice instruction is to breathe naturally.

## Device lifecycle (`device.py`)

Poll USB serial ports every 2 s. For a new port, `esptool flash_id` gives the
chip, MAC and flash size. Boards are remembered in `~/MediSense/devices.json`
with the firmware hash and settings:

- unknown board → the wizard asks for WiFi, then flashes everything in one
  `write_flash`, checking `SHA256SUMS` first
- known board with older firmware → updated in place with the same WiFi
  settings when the password is in the Keychain (else the wizard asks once)
- known board, current firmware, the Mac's IP changed → rewrite settings only
- otherwise → reset the board and watch its log

The serial log gives the WiFi state, disconnect reasons (in plain language),
the board's IP, and interruptions (brownouts, crashes, restarts, WiFi drops,
network-buffer pauses), which the Board tab lists and sessions record in
`board.log`.

## Security and privacy

- Servers bind to 127.0.0.1 only and reject requests whose `Host` isn't
  localhost (DNS rebinding). State-changing requests need an `X-MediSense: 1`
  header, which other websites can't send cross-origin (CSRF).
- No telemetry, no cloud. Downloads: uv, Python and the posture engine's
  hash-pinned wheels at install time; the MediaPipe models (hash-pinned) on
  first camera use.

## Ideas

- A second ESP32-S3 as a dedicated transmitter for a steadier signal and
  motion localisation (RuView supports multi-node).
- A load-cell cushion (HX711) for stillness and breathing through the seat.
- HRV and coherence trends in "Where you are now" once Pro sessions exist.
- Package as a signed macOS app (py2app/Briefcase) once the pilot settles.
