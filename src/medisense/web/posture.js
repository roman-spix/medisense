// Webcam posture, computed entirely in this browser tab with MediaPipe Pose.
// Video frames never leave the page; only a handful of angles are sent to the app.

const BASE = "/vendor/mediapipe";
const L = { nose: 0, lEye: 2, rEye: 5, lEar: 7, rEar: 8, lSh: 11, rSh: 12, lHip: 23, rHip: 24 };
const EDGES = [[11, 12], [11, 23], [12, 24], [23, 24], [11, 13], [13, 15], [12, 14], [14, 16], [7, 2], [2, 0], [0, 5], [5, 8]];
const CALIBRATE_S = 8;
const FPS = 8;

const median = (a) => { const s = a.filter((x) => x != null && isFinite(x)).sort((x, y) => x - y); return s.length ? s[Math.floor(s.length / 2)] : null; };
const deg = (r) => r * 180 / Math.PI;

export class PostureTracker {
  constructor({ onMetrics, onStatus, canvas }) {
    this.onMetrics = onMetrics;
    this.onStatus = onStatus;
    this.canvas = canvas;
    this.showVideo = false;
    this.running = false;
    this.baseline = null;
    this.calib = [];
    this.recent = [];
    this.prev = null;
  }

  async start() {
    if (this.running) return;
    this.running = true;
    try {
      this.onStatus("loading", "Loading the posture model (first time downloads about 25 MB)…");
      const vision = await import(`${BASE}/vision_bundle.mjs`);
      const files = await vision.FilesetResolver.forVisionTasks(`${BASE}/wasm`);
      const make = (delegate) => vision.PoseLandmarker.createFromOptions(files, {
        baseOptions: { modelAssetPath: `${BASE}/pose_landmarker_lite.task`, delegate },
        runningMode: "VIDEO", numPoses: 1,
      });
      try { this.pose = await make("GPU"); } catch (e) { this.pose = await make("CPU"); }
      this.onStatus("camera", "Waiting for camera permission…");
      this.stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 640 }, height: { ideal: 480 }, frameRate: { ideal: 15 } }, audio: false });
      this.video = document.createElement("video");
      this.video.playsInline = true; this.video.muted = true;
      this.video.srcObject = this.stream;
      await this.video.play();
      this.recalibrate();
      this._loop();
    } catch (e) {
      this.running = false;
      const msg = e && e.name === "NotAllowedError"
        ? "Camera permission was denied. Allow camera access for this page in the browser settings, then try again."
        : `Camera posture isn't available: ${e && e.message ? e.message : e}`;
      this.onStatus("error", msg);
      this.stop();
    }
  }

  stop() {
    this.running = false;
    if (this._timer) clearTimeout(this._timer);
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    this.stream = null;
    if (this.pose) { try { this.pose.close(); } catch (e) {} }
    this.pose = null;
    const ctx = this.canvas.getContext("2d");
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
  }

  recalibrate() {
    this.baseline = null;
    this.calib = [];
    this.calibStart = performance.now();
    this.onStatus("calibrating", `Sit the way you want to sit, upright and relaxed. Calibrating for ${CALIBRATE_S} s…`);
  }

  _loop() {
    if (!this.running) return;
    const t0 = performance.now();
    try { this._frame(t0); } catch (e) { /* skip a bad frame */ }
    const spent = performance.now() - t0;
    this._timer = setTimeout(() => this._loop(), Math.max(10, 1000 / FPS - spent));
  }

  _frame(now) {
    if (!this.video || this.video.readyState < 2) return;
    const res = this.pose.detectForVideo(this.video, now);
    const lm = res && res.landmarks && res.landmarks[0];
    this._draw(lm);
    const m = lm ? this._measure(lm) : null;
    if (!m || !m.visible) {
      this.onStatus("lost", "Can't see you clearly. Head and both shoulders need to be in view.");
      this._send({ visible: 0 });
      return;
    }
    if (!this.baseline) {
      this.calib.push(m);
      if ((now - this.calibStart) / 1000 >= CALIBRATE_S && this.calib.length >= CALIBRATE_S * FPS * 0.6) {
        this.baseline = {
          head_drop: median(this.calib.map((c) => c.head_drop)),
          head_tilt: median(this.calib.map((c) => c.head_tilt)),
          shoulder_tilt: median(this.calib.map((c) => c.shoulder_tilt)),
          lean: median(this.calib.map((c) => c.lean)),
        };
        this.onStatus("tracking", "Tracking posture.");
      }
      return;
    }
    this.onStatus("tracking", "Tracking posture.");
    this.recent.push(m);
    if (!this._lastSend || now - this._lastSend >= 500) {
      this._lastSend = now;
      const pick = (k) => median(this.recent.map((r) => r[k]));
      const cur = { head_drop: pick("head_drop"), head_tilt: pick("head_tilt"), shoulder_tilt: pick("shoulder_tilt"), lean: pick("lean"), camera_motion: pick("motion") };
      this.recent = [];
      const b = this.baseline;
      const slump = b.head_drop ? Math.max(0, 1 - cur.head_drop / b.head_drop) : 0;
      const dTilt = Math.abs(cur.head_tilt - b.head_tilt);
      const dSh = Math.abs(cur.shoulder_tilt - b.shoulder_tilt);
      const dLean = cur.lean != null && b.lean != null ? Math.abs(cur.lean - b.lean) : 0;
      let score = 100 - slump * 200 - Math.max(0, dTilt - 5) * 1.5 - Math.max(0, dSh - 3) * 2 - Math.max(0, dLean - 5) * 1.5;
      score = Math.max(0, Math.min(100, score));
      this._send({
        visible: 1, score, slump,
        head_drop: cur.head_drop,
        head_tilt: cur.head_tilt - b.head_tilt,
        shoulder_tilt: cur.shoulder_tilt - b.shoulder_tilt,
        lean: cur.lean != null && b.lean != null ? cur.lean - b.lean : null,
        camera_motion: cur.camera_motion,
      });
    }
  }

  _send(p) { this.onMetrics({ type: "posture", ...p }); }

  _measure(lm) {
    const ar = this.video.videoWidth / Math.max(1, this.video.videoHeight);
    const P = (i) => ({ x: lm[i].x * ar, y: lm[i].y, v: lm[i].visibility ?? 1 });
    const ls = P(L.lSh), rs = P(L.rSh), nose = P(L.nose);
    const visible = ls.v > 0.5 && rs.v > 0.5 && nose.v > 0.5;
    const sw = Math.hypot(ls.x - rs.x, ls.y - rs.y) || 1e-3;
    const sm = { x: (ls.x + rs.x) / 2, y: (ls.y + rs.y) / 2 };
    let le = P(L.lEar), re = P(L.rEar);
    if (le.v < 0.5 || re.v < 0.5) { le = P(L.lEye); re = P(L.rEye); }
    const head_tilt = deg(Math.atan2(le.y - re.y, le.x - re.x));
    const shoulder_tilt = deg(Math.atan2(ls.y - rs.y, ls.x - rs.x));
    const lh = P(L.lHip), rh = P(L.rHip);
    let lean = null;
    if (lh.v > 0.5 && rh.v > 0.5) {
      const hm = { x: (lh.x + rh.x) / 2, y: (lh.y + rh.y) / 2 };
      lean = deg(Math.atan2(sm.x - hm.x, hm.y - sm.y));
    }
    let motion = 0;
    if (this.prev) motion = (Math.hypot(nose.x - this.prev.x, nose.y - this.prev.y) / sw) * FPS;
    this.prev = nose;
    return { visible, head_drop: (sm.y - nose.y) / sw, head_tilt: wrap(head_tilt), shoulder_tilt: wrap(shoulder_tilt), lean, motion };
  }

  _draw(lm) {
    const c = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth, h = c.clientHeight;
    if (c.width !== w * dpr) { c.width = w * dpr; c.height = h * dpr; }
    const ctx = c.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    // mirror, like a mirror
    ctx.save(); ctx.translate(w, 0); ctx.scale(-1, 1);
    if (this.showVideo && this.video) { ctx.globalAlpha = 0.85; ctx.drawImage(this.video, 0, 0, w, h); ctx.globalAlpha = 1; }
    if (lm) {
      const st = getComputedStyle(document.documentElement);
      ctx.strokeStyle = st.getPropertyValue("--posture").trim();
      ctx.lineWidth = 2; ctx.lineCap = "round";
      for (const [a, b] of EDGES) {
        if ((lm[a].visibility ?? 1) < 0.5 || (lm[b].visibility ?? 1) < 0.5) continue;
        ctx.beginPath(); ctx.moveTo(lm[a].x * w, lm[a].y * h); ctx.lineTo(lm[b].x * w, lm[b].y * h); ctx.stroke();
      }
      ctx.fillStyle = st.getPropertyValue("--posture").trim();
      ctx.beginPath(); ctx.arc(lm[0].x * w, lm[0].y * h, 4, 0, Math.PI * 2); ctx.fill();
    }
    ctx.restore();
  }
}

function wrap(a) {
  // angles of a left->right line come out near +-180 when mirrored; fold to [-90, 90]
  if (a > 90) return a - 180;
  if (a < -90) return a + 180;
  return a;
}
