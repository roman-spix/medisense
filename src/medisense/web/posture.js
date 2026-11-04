// Browser camera fallback for posture: MediaPipe Pose in this tab.
// Used only when the background camera engine isn't installed. The page must
// stay visible (browsers pause hidden tabs). Video never leaves the page;
// only the 33 landmark positions are sent to the local app, which scores
// them against the Vairocana posture (the same scoring as the background engine).

const BASE = "/vendor/mediapipe";
const FPS = 6;

export const EDGES = [[11, 12], [11, 23], [12, 24], [23, 24], [11, 13], [13, 15], [12, 14], [14, 16],
  [7, 2], [2, 0], [0, 5], [5, 8], [23, 25], [24, 26]];

export function drawSkeleton(canvas, lm, { video = null, colorVar = "--posture", mirror = true } = {}) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.save();
  if (mirror) { ctx.translate(w, 0); ctx.scale(-1, 1); }
  if (video) { ctx.globalAlpha = 0.85; ctx.drawImage(video, 0, 0, w, h); ctx.globalAlpha = 1; }
  if (lm) {
    const color = getComputedStyle(document.documentElement).getPropertyValue(colorVar).trim();
    ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 2; ctx.lineCap = "round";
    const vis = (i) => (lm[i][3] ?? 1) >= 0.5;
    for (const [a, b] of EDGES) {
      if (!vis(a) || !vis(b)) continue;
      ctx.beginPath(); ctx.moveTo(lm[a][0] * w, lm[a][1] * h); ctx.lineTo(lm[b][0] * w, lm[b][1] * h); ctx.stroke();
    }
    if (vis(0)) { ctx.beginPath(); ctx.arc(lm[0][0] * w, lm[0][1] * h, 4, 0, Math.PI * 2); ctx.fill(); }
  }
  ctx.restore();
}

export class BrowserPoseCamera {
  constructor({ canvas, onLandmarks, onStatus }) {
    this.canvas = canvas;
    this.onLandmarks = onLandmarks;
    this.onStatus = onStatus;
    this.showVideo = false;
    this.running = false;
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
      this.stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 640 }, height: { ideal: 480 } }, audio: false });
      this.video = document.createElement("video");
      this.video.playsInline = true; this.video.muted = true;
      this.video.srcObject = this.stream;
      await this.video.play();
      this.onStatus("tracking", "Tracking (browser camera: keep this page visible).");
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
    if (this.canvas) drawSkeleton(this.canvas, null);
  }

  _loop() {
    if (!this.running) return;
    const t0 = performance.now();
    try {
      if (this.video && this.video.readyState >= 2) {
        const res = this.pose.detectForVideo(this.video, t0);
        const p = res && res.landmarks && res.landmarks[0];
        const w = res && res.worldLandmarks && res.worldLandmarks[0];
        const lm = p ? p.map((q) => [q.x, q.y, q.z, q.visibility ?? 1]) : null;
        const world = w ? w.map((q) => [q.x, q.y, q.z]) : null;
        if (this.canvas) drawSkeleton(this.canvas, lm, { video: this.showVideo ? this.video : null });
        this.onLandmarks(lm, world, this.video.videoWidth / Math.max(1, this.video.videoHeight));
      }
    } catch (e) { /* skip a bad frame */ }
    this._timer = setTimeout(() => this._loop(), Math.max(10, 1000 / FPS - (performance.now() - t0)));
  }
}
