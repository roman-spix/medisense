import { LineChart, WaveChart, BarChart, sparkline, fmtClock } from "./charts.js";
import { BrowserPoseCamera, drawSkeleton } from "./posture.js";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// " + MediSense 3": what the board itself reports at start-up, else what MediSense installed on it.
const msFirmware = (d) => {
  const v = d.board_medisense || ((d.firmware_label || "").match(/MediSense (\d+)/) || [])[1];
  return v ? ` + MediSense ${v}` : "";
};
const num = (v, d = 1) => (v == null || !isFinite(v) ? "--" : Number(v).toFixed(d));

const store = {
  get(k, d) { try { const v = localStorage.getItem("ms." + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("ms." + k, JSON.stringify(v)); } catch (e) {} },
};

async function api(path, method = "GET", body) {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-MediSense": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
}

// ------------------------------------------------------------------ theme

function applyTheme(t) {
  if (t) document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
  setTimeout(redrawAll, 0);
}
applyTheme(store.get("theme", null));
$("#theme").addEventListener("click", () => {
  const dark = getComputedStyle(document.documentElement).colorScheme.includes("dark");
  const next = dark ? "light" : "dark";
  store.set("theme", next);
  applyTheme(next);
});

// ------------------------------------------------------------------- tabs

function showTab(name) {
  $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  $$(".tab").forEach((s) => (s.hidden = s.id !== `tab-${name}`));
  if (name === "history") { loadHistory(); loadProfile(); }
  setTimeout(redrawAll, 0);
}
$$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------------------------------------------------------------- state

const S = {
  status: null,
  metrics: null,
  sessionId: null,
  sessionShifts: 0,
  trend: { breath: [], heart: [], motion: [], posture: [] },
  trendEvents: [],
  motionHist: [],
  pageStart: performance.now() / 1000,
  plannedBellRung: false,
  elapsedAt: null,
  stateRuns: [],
  heartExp: store.get("heartExp", false),
  posture: null,
};

const wave = new WaveChart($("#wave"), 60);
const pulseWave = new WaveChart($("#pulse-wave"), 8);
const CHART_OPTS = {
  breath: { floor: 0, minSpan: 6 },
  heart: { floor: 30, minSpan: 16 },
  motion: { yMin: 0, floor: 0, minSpan: 4 },
  posture: { yMin: 0, yMax: 100, floor: 0 },
};
const charts = {
  breath: new LineChart($("#t-breath"), CHART_OPTS.breath),
  heart: new LineChart($("#t-heart"), CHART_OPTS.heart),
  motion: new LineChart($("#t-motion"), CHART_OPTS.motion),
  posture: new LineChart($("#t-posture"), CHART_OPTS.posture),
};
const progress = {
  minutes: new BarChart($("#p-minutes"), { color: "--breath" }),
  steady: new BarChart($("#p-steady"), { color: "--motion", yMax: 100 }),
  settle: new BarChart($("#p-settle"), { color: "--text-secondary" }),
  breath: new BarChart($("#p-breath"), { color: "--breath" }),
};

function redrawAll() {
  wave.draw();
  pulseWave.draw();
  Object.values(charts).forEach((c) => c.draw());
  Object.values(progress).forEach((c) => c.draw());
  drawSpark();
  drawLiveStrip();
  detailCharts.forEach((c) => c.draw());
  if (detailStrip) detailStrip();
}

// ------------------------------------------------------- practice states

const PRACTICE_UI = {
  steady: ["✓", "Steady", "Still for over a minute. Rest here; nothing to fix."],
  settling: ["◌", "Settling", "Still and settling in. It counts as steady after a minute without moving."],
  restless: ["~", "Restless", "Moving. Notice the urge to move, then let the body be still."],
  drowsy: ["z", "Drowsy", "Your head is dropping or nodding: lift through the crown, raise the gaze a little, brighten your attention."],
  away: ["…", "Away", "Nobody seems to be sitting in front of the board."],
  "": ["○", "--", "Waiting for data from the board."],
};
const STATE_ORDER = ["steady", "settling", "restless", "drowsy", "away"];
const stateColor = (st) => getComputedStyle(document.documentElement).getPropertyValue(`--st-${st || "away"}`).trim();
$("#state-legend").innerHTML = STATE_ORDER.map((st) => `<span><i style="background:var(--st-${st})"></i>${PRACTICE_UI[st][1]}</span>`).join("");

function drawStateStrip(canvas, runs, xMin, xMax) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (!runs.length || !(xMax > xMin)) return;
  const x = (t) => ((t - xMin) / (xMax - xMin)) * w;
  for (const [a, b, st] of runs) {
    if (!st || b < xMin) continue;
    ctx.fillStyle = stateColor(st);
    const x0 = Math.max(0, x(a)), x1 = Math.min(w, x(b));
    ctx.fillRect(x0, 0, Math.max(1, x1 - x0 + 0.5), h);
  }
  canvas.onmousemove = (e) => {
    const r = canvas.getBoundingClientRect();
    const t = xMin + ((e.clientX - r.left) / r.width) * (xMax - xMin);
    const run = runs.find(([a, b]) => t >= a && t < b);
    canvas.title = `${fmtClock(Math.max(0, t))} · ${run ? PRACTICE_UI[run[2]][1] : "no data"}`;
  };
}
function pushRun(runs, t, st) {
  const last = runs[runs.length - 1];
  if (last && last[2] === st && t - last[1] <= 3) last[1] = t + 1;
  else runs.push([t, t + 1, st]);
}
function drawLiveStrip() {
  const runs = S.stateRuns;
  if (!runs.length) { drawStateStrip($("#t-state"), [], 0, 1); return; }
  const tEnd = runs[runs.length - 1][1];
  const xMin = S.sessionId ? 0 : Math.max(runs[0][0], tEnd - 600);
  drawStateStrip($("#t-state"), runs, xMin, Math.max(tEnd, xMin + 60));
}

// ----------------------------------------------------------- websocket

let ws = null;
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (e) => {
    let m;
    try { m = JSON.parse(e.data); } catch (err) { return; }
    if (m.type === "status") onStatus(m);
    else if (m.type === "metrics") onMetrics(m);
    else if (m.type === "wave") wave.push(m.samples);
    else if (m.type === "event") onEvent(m);
    else if (m.type === "posture") onPosture(m);
    else if (m.type === "ppg") { if (m.samples && m.samples.length) pulseWave.push(m.samples); }
    else if (m.type === "preview") { if (previewOn) $("#pose-img").src = `data:image/jpeg;base64,${m.jpg}`; }
  };
  ws.onopen = () => { if (previewOn) sendWs({ type: "preview", on: true }); };
  ws.onclose = () => {
    setPill("bad", "MediSense stopped. Start it again from the Desktop icon.");
    setTimeout(connect, 2000);
  };
}
connect();

function sendWs(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

// --------------------------------------------------------------- status

function setPill(kind, text) {
  const p = $("#pill");
  p.className = `pill ${kind}`;
  $("#pill-text").textContent = text;
  p.title = text;
}

const DEVICE_PILL = {
  no_board: ["warn", "No board"],
  identifying: ["warn", "Checking board…"],
  needs_setup: ["warn", "Board needs setup"],
  flashing: ["warn", "Setting up board…"],
  starting: ["warn", "Board connecting…"],
  wifi_failed: ["bad", "Board can't join WiFi"],
  unsupported: ["bad", "Unsupported board"],
  error: ["bad", "Board problem"],
  connected: ["warn", "No data yet"],
};

function onStatus(st) {
  S.status = st;
  const dev = st.device || {};
  if (st.session) {
    if (S.sessionId !== st.session.id) beginSessionUi(st.session);
    S.elapsedAt = { elapsed: st.session.elapsed, at: performance.now() };
  } else if (S.sessionId) {
    endSessionUi();
  }
  if (st.live) setPill(st.session ? "live rec" : "live", st.session ? "Recording · live" : st.demo ? "Live · demo" : "Live");
  else if (st.paused) setPill(st.session ? "warn rec" : "warn", "Signal paused: board reconnecting…");
  else if (st.demo) setPill("warn", "Demo starting…");
  else setPill(...(DEVICE_PILL[dev.state] || ["warn", dev.state || "…"]));
  renderSetup(st);
  renderBoard(st);
  const pro = st.pro;
  $("#card-rhythm").hidden = !pro;
  if (pro) {
    $("#radar-state").textContent = pro.radar.connected ? "connected" : "not found";
    $("#pulse-state").textContent = !pro.pulse.connected ? "not found" : pro.pulse.finger ? "finger on" : "no finger";
  }
  const evs = st.board_events || [];
  $("#board-events").innerHTML = evs.length
    ? evs.slice().reverse().map((e) => `<li><span class="t">${fmtClock(e.t)}</span><span>${esc(e.text)}</span></li>`).join("")
    : '<li class="caption">None so far.</li>';
}

// ------------------------------------------------------------- metrics

function onMetrics(m) {
  S.metrics = m;
  // Breathing
  const br = m.breath_rate;
  $("#breath-val").textContent = br == null ? "--" : br.toFixed(1);
  const c = m.breath_conf || 0;
  $("#breath-conf").textContent = br == null ? "--" : c >= 0.6 ? "High" : c >= 0.35 ? "Medium" : "Low";
  $("#breath-reg").textContent = m.breath_regularity == null ? "--" : `${Math.round(m.breath_regularity * 100)}%`;
  let note = "";
  if (m.signal === "no_signal") note = "Waiting for data from the board";
  else if (m.signal === "paused") note = "Signal paused: the board is reconnecting";
  else if (m.motion_state === "moving" || m.motion_state === "restless") note = "Paused while you move";
  else if (br == null) note = S.hadBreath ? "Measuring again after you moved (about 25 s)" : "Measuring: sit still (the first reading takes about 40 s)";
  if (br != null && m.breath_source && m.breath_source !== "wifi") note = `From the ${m.breath_source === "radar" ? "radar" : "pulse sensor"}`;
  if (br != null) S.hadBreath = true;
  $("#breath-note").textContent = note;

  // Heart: only a pulse sensor (or your watch) is trusted. WiFi's estimate is opt-in.
  const trusted = m.sensor_hr ?? m.heart_rate;
  const exp = m.heart_rate_experimental;
  $("#heart-val").textContent = trusted == null ? "--" : Math.round(trusted);
  $("#heart-tag").textContent = trusted != null ? (m.sensor_hr_source || "sensor") : "needs a sensor";
  $("#heart-exp").textContent = S.heartExp ? (exp == null ? "WiFi estimate: --" : `WiFi estimate ~${Math.round(exp)} (often wrong)`) : "";
  $("#heart-exp-btn").textContent = S.heartExp ? "Hide WiFi estimate" : "Show WiFi estimate";
  $("#heart-exp-btn").setAttribute("aria-pressed", String(S.heartExp));
  if (trusted != null) $("#heart-note").textContent = m.sensor_hr_source === "radar"
    ? "From the radar (put a finger on the pulse sensor for beat-to-beat accuracy)."
    : m.hrv_rmssd != null ? `From the pulse sensor. Heart rate variability (RMSSD): ${Math.round(m.hrv_rmssd)} ms.` : "From the pulse sensor.";
  if (S.status && S.status.pro) {
    $("#hrv-rmssd").textContent = m.hrv_rmssd == null ? "--" : `${Math.round(m.hrv_rmssd)} ms`;
    $("#hrv-sdnn").textContent = m.hrv_sdnn == null ? "--" : `${Math.round(m.hrv_sdnn)} ms`;
    $("#hrv-coh").textContent = m.coherence == null ? "--" : `${Math.round(m.coherence * 100)}%`;
    $("#breath-src").textContent = { radar: "radar", wifi: "WiFi", pulse: "pulse rhythm" }[m.breath_source] || "--";
    $("#pulse-note").textContent = m.finger_on
      ? (m.hrv_rmssd == null ? "Pulse found. Variability appears after about a minute of steady beats." : `Beats clean: ${Math.round((m.pulse_quality || 0) * 100)}% of the last minute.`)
      : "Rest a fingertip (or clip the sensor to an earlobe) lightly on the pulse sensor.";
  }

  // Practice state
  const st = m.state ?? "";
  const [icon, label, explain] = PRACTICE_UI[st] || PRACTICE_UI[""];
  $("#still-icon").textContent = icon;
  $("#still-val").textContent = label;
  $("#state-note").textContent = explain;
  $("#settled").textContent = m.signal === "no_signal" ? "--" : fmtClock(m.settled_s);
  $("#steady-so-far").textContent = m.steady_s ? `${fmtClock(m.steady_s)}${m.steady_pct != null ? ` · ${Math.round(m.steady_pct)}%` : ""}` : "--";
  $("#still-hint").textContent = m.settled_at != null && S.sessionId ? `Settled at ${fmtClock(m.settled_at)}` : "";
  S.motionHist.push(m.signal === "no_signal" ? null : m.motion);
  if (S.motionHist.length > 300) S.motionHist.shift();
  drawSpark();

  // Signal quality (icon + label, never colour alone)
  const fps = m.frames_per_s || 0;
  const q = m.signal === "paused" ? ["⏸", "Paused"]
    : fps <= 0 ? ["✕", "No data"] : fps >= 20 && m.loss_pct < 10 ? ["✓", "Good"] : fps >= 8 ? ["~", "Fair"] : ["!", "Weak"];
  $("#sig-icon").textContent = q[0];
  $("#sig-val").textContent = q[1];
  $("#sig-fps").textContent = fps > 0 ? num(fps, 0) : "--";
  $("#sig-loss").textContent = fps > 0 ? `${num(m.loss_pct, 0)}%` : "--";
  $("#sig-rssi").textContent = m.rssi == null || fps <= 0 ? "--" : `${num(m.rssi, 0)} dBm`;
  $("#sig-ch").textContent = m.channel ?? "--";
  const src = S.status && S.status.packet_source;
  $("#sig-note").textContent = q[1] === "Paused" ? `No data for ${Math.round(m.gap_s || 0)} s: the board is reconnecting or restarting. Readings continue when it's back.`
    : fps <= 0 ? "Waiting for data from the board."
    : q[1] === "Good" ? `Data arriving from ${src || "the board"}.`
    : "Few frames are arriving. Move the board closer to the router, or away from metal and other electronics.";

  // Trends: session time when recording, otherwise a rolling 10 minutes.
  const t = m.session_t != null ? m.session_t : performance.now() / 1000 - S.pageStart;
  const T = S.trend;
  T.breath.push([t, br]);
  T.heart.push([t, trusted ?? null]);
  T.heartExp = T.heartExp || [];
  T.heartExp.push([t, exp ?? null]);
  T.motion.push([t, m.signal === "no_signal" ? null : m.motion]);
  T.posture.push([t, m.posture_score ?? null]);
  if (m.session_t == null) for (const k in T) while (T[k].length && T[k][0][0] < t - 600) T[k].shift();
  pushRun(S.stateRuns, t, st);
  if (m.session_t == null) while (S.stateRuns.length && S.stateRuns[0][1] < t - 600) S.stateRuns.shift();
  drawTrends();
  drawLiveStrip();
  tickClock();
}
$("#heart-exp-btn").addEventListener("click", () => {
  S.heartExp = !S.heartExp; store.set("heartExp", S.heartExp);
  if (S.metrics) onMetrics(S.metrics);
});

function drawSpark() {
  sparkline($("#motion-spark"), S.motionHist, "--motion", { floor: 0, min: 6, band: 2.2 });
}

function drawTrends() {
  const T = S.trend, ev = S.trendEvents;
  const recording = !!S.sessionId;
  $("#trend-hint").textContent = recording ? "This session" : "Last 10 minutes";
  const xr = recording ? { xMin: 0 } : {};
  for (const c of Object.values(charts)) Object.assign(c.opts, { xMin: xr.xMin ?? null });
  charts.breath.set([{ label: "Breathing", color: "--breath", points: T.breath, unit: "/min", gap: 5 }], ev);
  const heartSeries = [{ label: "Heart", color: "--heart", points: T.heart, unit: "bpm", digits: 0, gap: 5 }];
  if (S.heartExp && T.heartExp) heartSeries.push({ label: "WiFi estimate (experimental)", color: "--text-muted", points: T.heartExp, unit: "bpm", digits: 0, gap: 5 });
  $("#t-heart-title").textContent = S.heartExp ? "Heart rate (sensor; grey: WiFi estimate)" : "Heart rate (per min)";
  charts.heart.set(heartSeries, ev);
  charts.motion.set([{ label: "Movement", color: "--motion", points: T.motion, unit: "×", gap: 5 }], ev);
  charts.posture.set([{ label: "Posture", color: "--posture", points: T.posture, digits: 0, gap: 5 }], ev);
}

const EVENT_LABEL = { shift: "Shifted position", mark: "Marked moment", polar_hr: "Watch heart rate", breath_count: "Breath count",
  slump: "Head dropped", nod: "Nodded", posture_alert: "Posture reminder", board: "Board", note: "Note" };

function onEvent(e) {
  if (!S.sessionId) return;
  if (e.kind === "shift") { S.sessionShifts++; $("#shifts").textContent = S.sessionShifts; }
  const label = (EVENT_LABEL[e.kind] || e.kind) + (e.text ? `: ${e.text}` : e.value != null ? `: ${e.value}` : "");
  S.trendEvents.push({ x: e.t, label });
  drawTrends();
}

// ------------------------------------------------------------- session

let selectedMin = 0;
$$("#durations .chip").forEach((b) => b.addEventListener("click", () => {
  $$("#durations .chip").forEach((x) => x.classList.toggle("on", x === b));
  selectedMin = +b.dataset.min;
}));
$("#practice").value = store.get("practice", "Breath focus");
$("#use-camera").checked = store.get("useCamera", false);

let audio = null;
function bell(strength = 1) {
  try {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    const now = audio.currentTime;
    const master = audio.createGain();
    master.gain.value = 0.22 * strength;
    master.connect(audio.destination);
    [[1, 1], [2.71, 0.5], [5.2, 0.25], [8.4, 0.12]].forEach(([ratio, amp], i) => {
      const o = audio.createOscillator();
      const g = audio.createGain();
      o.frequency.value = 262 * ratio;
      g.gain.setValueAtTime(0, now);
      g.gain.linearRampToValueAtTime(amp, now + 0.01);
      g.gain.exponentialRampToValueAtTime(0.0001, now + 7 / (1 + i * 0.6));
      o.connect(g); g.connect(master);
      o.start(now); o.stop(now + 8);
    });
  } catch (e) { /* no audio */ }
}

$("#btn-start").addEventListener("click", async () => {
  const practice = $("#practice").value;
  store.set("practice", practice);
  store.set("useCamera", $("#use-camera").checked);
  try {
    const r = await api("/api/session/start", "POST", {
      practice, intention: $("#intention").value.trim() || null,
      planned_min: selectedMin || null, posture_camera: $("#use-camera").checked,
    });
    bell();
    beginSessionUi({ id: r.id, elapsed: 0, practice, planned_min: selectedMin || null });
  } catch (e) { alert(e.message); }
});

function beginSessionUi(sess) {
  S.sessionId = sess.id;
  S.sessionShifts = 0;
  S.plannedBellRung = false;
  S.trend = { breath: [], heart: [], motion: [], posture: [] };
  S.trendEvents = [];
  S.stateRuns = [];
  S.hadBreath = false;
  S.elapsedAt = { elapsed: sess.elapsed || 0, at: performance.now() };
  S.planned = sess.planned_min || null;
  $("#shifts").textContent = "0";
  $("#session-idle").hidden = true;
  $("#session-running").hidden = false;
  $("#session-label").textContent = sess.practice || "Session";
  tickClock();
}

function endSessionUi() {
  S.sessionId = null;
  S.planned = null;
  $("#session-idle").hidden = false;
  $("#session-running").hidden = true;
  $("#inline-form").hidden = true;
  S.trend = { breath: [], heart: [], motion: [], posture: [] };
  S.trendEvents = [];
  S.stateRuns = [];
}

function tickClock() {
  if (!S.sessionId || !S.elapsedAt) return;
  const el = S.elapsedAt.elapsed + (performance.now() - S.elapsedAt.at) / 1000;
  $("#clock").textContent = fmtClock(el);
  if (S.planned) {
    const left = S.planned * 60 - el;
    $("#remaining").textContent = left > 0 ? `${fmtClock(left)} left of ${S.planned} min` : `${S.planned} min reached. End when you're ready.`;
    if (left <= 0 && !S.plannedBellRung) { S.plannedBellRung = true; bell(); setTimeout(() => bell(0.7), 2500); }
  } else {
    $("#remaining").textContent = "Open session";
  }
}
setInterval(tickClock, 1000);

function inlineForm(html, onSubmit) {
  const box = $("#inline-form");
  box.innerHTML = html + `<button class="primary small" data-act="ok">Save</button><button class="ghost small" data-act="cancel">Cancel</button>`;
  box.hidden = false;
  const input = box.querySelector("input");
  if (input) input.focus();
  const done = () => { box.hidden = true; box.innerHTML = ""; };
  box.querySelector('[data-act="cancel"]').onclick = done;
  const ok = async () => { try { await onSubmit(box); done(); } catch (e) { alert(e.message); } };
  box.querySelector('[data-act="ok"]').onclick = ok;
  if (input) input.addEventListener("keydown", (e) => { if (e.key === "Enter") ok(); if (e.key === "Escape") done(); });
}

function mark() {
  api("/api/session/event", "POST", { kind: "mark" }).catch((e) => alert(e.message));
}
function watchHr() {
  inlineForm(`<label class="field">Heart rate on your Polar watch now<input type="number" min="30" max="200" id="hr-in" inputmode="numeric"></label>`,
    async (box) => {
      const v = +box.querySelector("#hr-in").value;
      if (!(v >= 30 && v <= 200)) throw new Error("Enter the number shown on the watch (30–200).");
      await api("/api/session/event", "POST", { kind: "polar_hr", value: v });
    });
}
let countTimer = null;
function breathCount() {
  const box = $("#inline-form");
  box.hidden = false;
  let left = 60;
  box.innerHTML = `<span>Count your breaths silently. A chime ends the minute. <b id="count-left">60</b> s</span><button class="ghost small" id="count-cancel">Cancel</button>`;
  $("#count-cancel").onclick = () => { clearInterval(countTimer); box.hidden = true; };
  clearInterval(countTimer);
  countTimer = setInterval(() => {
    left--;
    const el = $("#count-left");
    if (el) el.textContent = left;
    if (left <= 0) {
      clearInterval(countTimer);
      bell(0.5);
      inlineForm(`<label class="field">How many breaths did you count?<input type="number" min="1" max="60" id="bc-in" inputmode="numeric"></label>`,
        async (b) => {
          const v = +b.querySelector("#bc-in").value;
          if (!(v >= 1 && v <= 60)) throw new Error("Enter a number between 1 and 60.");
          await api("/api/session/event", "POST", { kind: "breath_count", value: v });
        });
    }
  }, 1000);
}
$("#btn-mark").addEventListener("click", mark);
$("#btn-hr").addEventListener("click", watchHr);
$("#btn-count").addEventListener("click", breathCount);
document.addEventListener("keydown", (e) => {
  if (!S.sessionId || e.metaKey || e.ctrlKey || e.altKey) return;
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
  if (e.key === "m" || e.key === "M") mark();
  if (e.key === "h" || e.key === "H") { e.preventDefault(); watchHr(); }
});

let endedId = null, endRating = null, endMood = [];
$("#btn-stop").addEventListener("click", async () => {
  if (!confirm("End this session?")) return;
  try {
    const meta = await api("/api/session/stop", "POST", {});
    endedId = meta.id;
    endSessionUi();
    bell(0.6);
    const sm = meta.summary || {};
    $("#end-summary").textContent = `${fmtClock(meta.duration_s)} · ${sm.settle_s != null ? `settled in ${fmtClock(sm.settle_s)} · ` : ""}steady ${num(sm.steady_pct, 0)}% · breathing ${num(sm.breath_start)} → ${num(sm.breath_end)}/min`;
    endRating = null; endMood = [];
    $$("#rating .chip, #mood .chip").forEach((c) => c.classList.remove("on"));
    $("#end-notes").value = ""; $("#end-polar").value = "";
    $("#dlg-end").showModal();
  } catch (e) { alert(e.message); }
});
$$("#rating .chip").forEach((b) => b.addEventListener("click", () => {
  endRating = +b.dataset.v;
  $$("#rating .chip").forEach((x) => x.classList.toggle("on", x === b));
}));
$$("#mood .chip").forEach((b) => b.addEventListener("click", () => {
  b.classList.toggle("on");
  endMood = $$("#mood .chip.on").map((x) => x.textContent);
}));
$("#end-form").addEventListener("submit", async () => {
  if (!endedId) return;
  const polar = +$("#end-polar").value;
  await api(`/api/sessions/${encodeURIComponent(endedId)}/meta`, "POST", {
    rating: endRating, mood: endMood, notes: $("#end-notes").value.trim(),
    polar_avg_hr: polar >= 30 && polar <= 200 ? polar : null,
  }).catch((e) => alert(e.message));
  const id = endedId;
  endedId = null;
  showTab("history");
  openDetail(id);
});

// ---------------------------------------------------------------- camera
// The app watches posture with its background camera engine (works with this
// page hidden). Without it, this page's camera finds the body landmarks and
// sends them to the app, which scores them the same way.

let browserCam = null;
let previewOn = false;
const POINT_STATUS = {
  ok: ["✓", "Good"], minor: ["~", "Small adjustment"], major: ["✕", "Out of alignment"],
  not_visible: ["–", "Not in view"], not_checked: ["–", "Not visible to a camera"],
  needs_calibration: ["?", "Calibrate to check"], off: ["–", "Not checked"],
};
function renderVairocana(points) {
  $("#v-points").innerHTML = (points || []).map((p, i) => {
    const [icon, text] = POINT_STATUS[p.status] || POINT_STATUS.not_visible;
    const tip = p.advice ? `<div class="tip">${esc(p.advice)}</div>` : "";
    return `<li class="pt pt-${esc(p.status)}"><span class="pt-icon" aria-hidden="true">${icon}</span><div><b>${i + 1}. ${esc(p.name)}</b> <span class="pt-how">${esc(p.how)}</span><div class="pt-status">${esc(text)}</div>${tip}</div></li>`;
  }).join("");
}
const POSTURE_UI = {
  aligned: ["✓", "Aligned"], ok: ["✓", "Good"], drifting: ["!", "Drifting"], away: ["…", "Not in view"],
  waiting: ["…", "Starting the camera…"], calibrating: ["◎", "Calibrating: hold your best posture"],
};

function onPosture(p) {
  S.posture = p;
  const svc = p.service || {};
  const on = !!p.camera_on;
  $("#cam-toggle").textContent = on ? "Camera off" : "Camera on";
  $("#cam-video").hidden = !on;
  $("#cam-recal").hidden = !on;
  if (!on) {
    if (browserCam) { browserCam.stop(); browserCam = null; }
    $("#posture-val").textContent = "--";
    $("#posture-icon").textContent = "○";
    $("#posture-state").textContent = "Camera off";
    $("#posture-advice").textContent = "";
    $("#posture-note").textContent = "Camera is off.";
    drawSkeleton($("#pose"), null);
    return;
  }
  if (svc.state === "browser" && !browserCam) startBrowserCam();
  renderVairocana(p.points);
  $("#posture-val").textContent = p.score == null ? "--" : Math.round(p.score);
  const [icon, label] = POSTURE_UI[p.state] || POSTURE_UI.waiting;
  $("#posture-icon").textContent = icon;
  $("#posture-state").textContent = label;
  $("#posture-advice").textContent = (p.advice || [])[0] || (p.score != null ? "Upright and relaxed." : "");
  if (p.lm && !previewOn) drawSkeleton($("#pose"), p.lm);
  const note = svc.state === "error" ? (svc.error || "Camera problem.")
    : svc.state === "starting" ? "Starting the camera engine…"
    : !p.calibrated ? "Sit in your best posture and click Calibrate: MediSense then measures drift from it."
    : svc.source === "browser" ? "Using this page's camera: keep this page visible."
    : `Watching your posture${p.alerts ? ` · ${p.alerts} reminder${p.alerts > 1 ? "s" : ""}` : ""}.`;
  $("#posture-note").textContent = note;
  const set = svc.settings;
  if (set && !postureSettingsShown) {
    postureSettingsShown = true;
    $("#p-threshold").value = set.threshold; $("#v-threshold").textContent = Math.round(set.threshold);
    $("#p-hold").value = set.hold_s; $("#v-hold").textContent = `${Math.round(set.hold_s)} s`;
    $("#p-enabled").checked = set.enabled; $("#p-sound").checked = set.sound;
  }
}
let postureSettingsShown = false;

function startBrowserCam() {
  browserCam = new BrowserPoseCamera({
    canvas: $("#pose"),
    onLandmarks: (lm, world, aspect) => sendWs({ type: "pose", lm, world, aspect }),
    onStatus: (kind, msg) => { $("#posture-note").textContent = msg; },
  });
  browserCam.showVideo = previewOn;
  browserCam.start();
}

$("#cam-toggle").addEventListener("click", async () => {
  const on = !(S.posture && S.posture.camera_on);
  try { onPosture(await api("/api/posture/camera", "POST", { on })); } catch (e) { alert(e.message); }
});
$("#cam-recal").addEventListener("click", async () => {
  try { await api("/api/posture/calibrate", "POST", {}); $("#posture-note").textContent = "Calibrating for 5 seconds: hold your best posture…"; }
  catch (e) { alert(e.message); }
});
$("#cam-video").addEventListener("click", () => {
  previewOn = !previewOn;
  $("#cam-video").textContent = previewOn ? "Hide video" : "Show video";
  const native = S.posture && S.posture.service && S.posture.service.source === "native";
  $("#pose-img").hidden = !(previewOn && native);
  $("#pose").hidden = previewOn && native;
  if (browserCam) browserCam.showVideo = previewOn;
  sendWs({ type: "preview", on: previewOn && native });
});
async function savePostureSettings(patch) {
  try { await api("/api/posture/settings", "POST", patch); } catch (e) { alert(e.message); }
}
$("#p-threshold").addEventListener("input", (e) => { $("#v-threshold").textContent = e.target.value; });
$("#p-hold").addEventListener("input", (e) => { $("#v-hold").textContent = `${e.target.value} s`; });
$("#p-threshold").addEventListener("change", (e) => savePostureSettings({ threshold: +e.target.value }));
$("#p-hold").addEventListener("change", (e) => savePostureSettings({ hold_s: +e.target.value }));
$("#p-enabled").addEventListener("change", (e) => savePostureSettings({ enabled: e.target.checked }));
$("#p-sound").addEventListener("change", (e) => savePostureSettings({ sound: e.target.checked }));

// ----------------------------------------------------------- board setup

let setupKey = "";
function renderSetup(st) {
  const dev = st.device || {};
  const slotLive = $("#setup-slot"), slotBoard = $("#board-setup-slot");
  let html = "";
  const state = dev.state;
  const showWifiForm = state === "needs_setup" || state === "wifi_failed" || forceWifiForm;
  const key = `${state}|${showWifiForm}|${st.live}`;
  if (st.live && !forceWifiForm) {
    slotLive.innerHTML = ""; slotBoard.innerHTML = ""; setupKey = key;
    return;
  }
  if (key === setupKey) {
    // same state: only refresh message and progress, keep what the person typed
    $$(".setup .msg").forEach((el) => (el.textContent = dev.message || ""));
    $$(".setup .progress span").forEach((el) => (el.style.width = `${dev.progress || 0}%`));
    return;
  }
  setupKey = key;
  if (st.demo) html = "";
  else if (showWifiForm) html = wifiFormHtml(dev);
  else if (state === "no_board") html = `
    <h2>Connect your board</h2>
    <p class="msg">${esc(dev.message)}</p>
    <ol>
      <li>Plug the ESP32-S3 into this Mac with a USB <b>data</b> cable. On a DevKitC board, either port works; the one labelled “USB” needs no driver.</li>
      <li>MediSense notices it within a few seconds, so there's no need to click anything.</li>
    </ol>
    <p>No board yet? Close this window and run <b>MediSense Demo</b> to try everything with a simulated meditator.</p>`;
  else if (state === "identifying" || state === "starting") html = `<h2><span class="spinner"></span>${state === "identifying" ? "Board found" : "Starting up"}</h2><p class="msg">${esc(dev.message)}</p>`;
  else if (state === "flashing") html = `<h2><span class="spinner"></span>Setting up the board</h2><p class="msg">${esc(dev.message)}</p><div class="progress"><span style="width:${dev.progress || 0}%"></span></div><p class="caption">Don't unplug the board. This takes about a minute the first time, a few seconds after that.</p>`;
  else if (state === "connected") html = `<h2><span class="status-icon">!</span>Board connected, waiting for data</h2><p class="msg">${esc(dev.message)}</p>`;
  else if (state === "unsupported" || state === "error") html = `<h2><span class="status-icon error-text">✕</span>${state === "unsupported" ? "This board won't work" : "Something went wrong"}</h2><p class="msg">${esc(dev.message)}</p><p>Unplug the board and plug it back in to try again.</p>`;
  const wrap = html ? `<div class="card setup">${html}</div>` : "";
  slotLive.innerHTML = wrap;
  slotBoard.innerHTML = wrap;
  [slotLive, slotBoard].forEach((slot) => {
    const f = slot.querySelector("form");
    if (f) bindWifiForm(f, dev);
  });
}

let forceWifiForm = false;
function wifiFormHtml(dev) {
  const failed = dev.state === "wifi_failed";
  const fresh = dev.state === "needs_setup";
  return `
    <h2>${failed ? "The board couldn't join your WiFi" : fresh ? "Set up your board" : "Change WiFi"}</h2>
    <p class="msg">${esc(dev.message)}</p>
    ${fresh ? `<p>MediSense will install its firmware (RuView 0.8.8, adjusted for meditation: no flashing light) and your WiFi details. Anything else on the board is replaced. You only do this once per board.</p>` : ""}
    <form autocomplete="off">
      <label class="field">WiFi name (must be a 2.4 GHz network)<input name="ssid" required maxlength="32" value="${esc(dev.suggested_ssid || (dev.configured && dev.configured.ssid) || "")}"></label>
      <label class="field">WiFi password<input name="password" type="password" maxlength="63" ${dev.saved_password ? 'placeholder="Leave empty to use the saved one"' : ""}></label>
      <label class="check full"><input type="checkbox" name="show"> Show password</label>
      <label class="check full"><input type="checkbox" name="remember" checked> Remember it in this Mac's Keychain (needed to update the board automatically later)</label>
      <div class="actions full"><button class="primary" type="submit">${fresh ? "Set up board" : "Save to board"}</button>${forceWifiForm ? '<button type="button" class="ghost" data-act="cancel">Cancel</button>' : ""}<span class="caption err error-text"></span></div>
    </form>`;
}
function bindWifiForm(f, dev) {
  f.show.addEventListener("change", () => (f.password.type = f.show.checked ? "text" : "password"));
  const cancel = f.querySelector('[data-act="cancel"]');
  if (cancel) cancel.onclick = () => { forceWifiForm = false; setupKey = ""; renderSetup(S.status); };
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = f.querySelector('button[type="submit"]');
    btn.disabled = true;
    f.querySelector(".err").textContent = "";
    const useSaved = dev.saved_password && !f.password.value;
    try {
      await api("/api/device/setup", "POST", { ssid: f.ssid.value.trim(), password: f.password.value, use_saved: useSaved, remember: f.remember.checked });
      forceWifiForm = false;
      setupKey = "";
    } catch (err) {
      f.querySelector(".err").textContent = err.message;
      btn.disabled = false;
    }
  });
}
$("#btn-wifi").addEventListener("click", () => {
  const dev = (S.status && S.status.device) || {};
  if (!dev.mac) { alert("Plug the board into this Mac first."); return; }
  forceWifiForm = true; setupKey = ""; renderSetup(S.status);
});
$("#btn-reset").addEventListener("click", () => api("/api/device/reset", "POST", {}).catch((e) => alert(e.message)));

// What the board picks up, from its own 10-second report (MediSense firmware 3).
function boardHears(d) {
  const h = d.board_hears;
  if (!h) return d.board_wifi === "connected" && d.csi_callbacks ? `measuring (${d.csi_callbacks}+ frames so far)` : "--";
  if (h.heard === 0) return "nothing yet";
  if (h.heard_per_s == null) return `${h.heard} frames so far, ${h.used} measured`;
  return `${Math.round(h.heard_per_s)} frames/s, ${Math.round(h.used_per_s)} measured and sent`;
}
function boardSendsTo(d, st) {
  if (!d.board_target) return "--";
  const ip = d.board_target.split(":")[0];
  if (!st.mac_ip || st.demo) return d.board_target;
  return ip === st.mac_ip ? `${d.board_target} (this Mac)` : `${d.board_target} (not this Mac: ${st.mac_ip})`;
}

function renderBoard(st) {
  const d = st.device || {};
  const cfg = d.configured || {};
  const rows = [
    ["Status", d.message || d.state],
    ["Data", st.live ? `arriving${st.packet_source ? " from " + st.packet_source : ""}` : st.seconds_since_data != null ? `last seen ${Math.round(st.seconds_since_data)} s ago` : "none yet"],
    ["USB port", d.port ? `${d.port} (${d.port_label})` : "not connected"],
    ["Chip", d.chip ? `${d.chip}${d.flash_size ? ", " + d.flash_size + " flash" : ""}` : "--"],
    ["MAC address", d.mac || "--"],
    ["Firmware", d.board_firmware ? `RuView ${d.board_firmware}${msFirmware(d)}` : "--"],
    ["Board hears", boardHears(d)],
    ["Board sends to", boardSendsTo(d, st)],
    ["Board address", d.board_ip || "--"],
    ["This Mac", st.mac_ip || "--"],
    ["WiFi network", cfg.ssid || "--"],
    ["Router filter", d.board_hears && d.board_hears.any_sender ? "off (nothing came from the router's address, so every sender is used)"
      : cfg.filter_mac || (cfg.ssid ? "automatic (the router the board is connected to)" : "--")],
    ["App version", st.version],
  ];
  $("#board-facts").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  if (d.log_tail && d.log_tail.length) $("#board-log").textContent = d.log_tail.join("\n");
}

// --------------------------------------------------------------- history

let historyRows = [];
async function loadHistory() {
  try { historyRows = await api("/api/sessions"); } catch (e) { return; }
  const done = historyRows.filter((s) => s.ended_at);
  $("#history-empty").hidden = done.length > 0;
  $("#history-body").hidden = done.length === 0;
  const chrono = [...done].reverse();
  const label = (s) => s.started_at ? s.started_at.slice(5, 10).replace("-", "/") : "";
  const tip = (s, line) => `<div class="tip-x">${esc(s.started_at ? s.started_at.slice(0, 16).replace("T", " ") : "")}</div><div class="tip-row">${line}</div>`;
  const sm = (s) => s.summary || {};
  progress.minutes.set(chrono.map((s) => ({ label: label(s), value: s.duration_s ? s.duration_s / 60 : null, tip: tip(s, `<b>${num((s.duration_s || 0) / 60, 0)}</b> min`) })));
  progress.steady.set(chrono.map((s) => ({ label: label(s), value: sm(s).steady_pct ?? null, tip: tip(s, `<b>${num(sm(s).steady_pct, 0)}%</b> steady · longest still ${fmtClock(sm(s).longest_still_s || 0)}`) })));
  progress.settle.set(chrono.map((s) => ({ label: label(s), value: sm(s).settle_s != null ? sm(s).settle_s / 60 : null, tip: tip(s, sm(s).settle_s != null ? `settled after <b>${fmtClock(sm(s).settle_s)}</b>` : "didn't settle for 3 minutes in a row") })));
  progress.breath.set(chrono.map((s) => ({ label: label(s), value: sm(s).steady_breath ?? sm(s).breath_end ?? null, tip: tip(s, `<b>${num(sm(s).steady_breath ?? sm(s).breath_end)}</b> breaths/min while steady`) })));
  $("#sessions-rows").innerHTML = done.map((s) => {
    const x = sm(s);
    return `<tr data-id="${esc(s.id)}" tabindex="0">
      <td>${esc(s.started_at ? s.started_at.slice(0, 16).replace("T", " ") : s.id)}${s.demo ? ' <span class="tag">demo</span>' : ""}</td>
      <td>${esc(s.practice || "")}</td>
      <td class="num">${num((s.duration_s || 0) / 60, 0)}</td>
      <td class="num">${x.settle_s == null ? "--" : fmtClock(x.settle_s)}</td>
      <td class="num">${x.steady_pct == null ? "--" : num(x.steady_pct, 0) + "%"}</td>
      <td class="num">${num(x.steady_breath ?? x.breath_end)}</td>
      <td class="num">${x.restless_n ?? "--"} · ${x.drowsy_n ?? "--"}</td>
      <td class="num">${num(x.posture_avg, 0)}</td>
      <td class="num">${s.rating ?? "--"}</td></tr>`;
  }).join("");
  $$("#sessions-rows tr").forEach((tr) => {
    tr.addEventListener("click", () => openDetail(tr.dataset.id));
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") openDetail(tr.dataset.id); });
  });
}

// ------------------------------------------------------ where you are now

const LEAN_UI = {
  excitation: "Leaning towards restlessness (excitation)",
  laxity: "Leaning towards dullness (laxity)",
  balanced: "Restlessness and dullness in balance",
};
// betterUp: true / false, or null for numbers that are observed, not aimed at (breathing).
function trend(cur, before, betterUp, fmt = (v) => num(v, 0)) {
  if (cur == null || before == null) return "";
  const d = cur - before;
  if (Math.abs(d) < 1e-9 || Math.abs(d) / Math.max(1e-9, Math.abs(before)) < 0.05) return '<span class="trend-flat">→ same as before</span>';
  const cls = betterUp == null ? "trend-flat" : (d > 0) === betterUp ? "trend-up" : "trend-down";
  return `<span class="${cls}">${d > 0 ? "▲" : "▼"} ${fmt(Math.abs(d))} vs before</span>`;
}
async function loadProfile() {
  const p = await api("/api/profile").catch(() => null);
  if (!p) return;
  const tile = (l, v, sub = "") => `<div class="tile"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${sub}</div></div>`;
  $("#where-hint").textContent = p.sessions >= 2 ? `Your last ${p.recent_n} sessions${p.before_n ? `, compared with the ${p.before_n} before` : ""}` : "";
  const mins = (s) => (s == null ? "--" : fmtClock(s));
  const lean = p.leaning ? `${LEAN_UI[p.leaning]}${p.leaning !== "excitation" && !p.drowsiness_measured ? " · turn the camera on to see dullness" : ""}` : "";
  $("#profile").innerHTML = `
    <div class="profile-head"><span class="stage">${esc(p.stage)}</span><span class="lean">${esc(lean)}</span></div>
    ${p.sessions ? `<div class="tiles">
      ${tile("Practice", `${num(p.sessions_per_week, 1)}<small>/week</small>`, `${num(p.minutes_per_week, 0)} min a week · ${p.streak_days || 0}-day streak`)}
      ${tile("Steady", p.steady_pct == null ? "--" : `${num(p.steady_pct, 0)}%`, trend(p.steady_pct, p.steady_pct_before, true, (v) => `${num(v, 0)} pts`) || "of your time sitting")}
      ${tile("Time to settle", mins(p.settle_s), trend(p.settle_s, p.settle_s_before, false, (v) => fmtClock(v)) || "median")}
      ${tile("Longest still", mins(p.longest_still_s), "median per session")}
      ${tile("Breathing while steady", num(p.steady_breath), trend(p.steady_breath, p.steady_breath_before, null, (v) => num(v, 1)) || "breaths / min, breathe naturally")}
      ${tile("Restless · drowsy", `${num(p.restless_per10, 1)} · ${p.drowsiness_measured ? num(p.drowsy_per10, 1) : "--"}`, "times per 10 min")}
    </div>` : ""}
    <ul class="profile-advice">${(p.advice || []).map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
    <details class="howto"><summary>How this works</summary>
      <p>Every second of a session gets a state: <b>steady</b> (still for over a minute), <b>settling</b> (still, but you moved less than a minute ago), <b>restless</b> (moving or shifting), <b>drowsy</b> (your head dropping or nodding, seen by the camera) or <b>away</b>.</p>
      <p><b>Time to settle</b> is when your first stretch of three still minutes began. <b>Steady</b> is the share of your sitting time spent steady. Fidgeting is the body's side of restlessness (excitation); a dropping, nodding head the body's side of dullness (laxity). These are the two classic obstacles to a calm, clear mind, and the profile says which one shows more.</p>
      <p>The stage comes from your last sessions: <i>Building the habit</i> (fewer than 3 a week), <i>Settling the body</i> (steady under 40%), <i>Steadying</i> (40 to 70%), <i>Stable sitting</i> (over 70%).</p>
      <p>The sensors see the body, not the mind: steadiness means the conditions for meditation (stillness, even breathing, an upright posture), not proof of it. Your own notes and ratings stay the real record.</p>
    </details>`;
}

let detailCharts = [];
let detailStrip = null;
async function openDetail(id) {
  let s;
  try { s = await api(`/api/sessions/${encodeURIComponent(id)}`); } catch (e) { alert(e.message); return; }
  const meta = s.meta, sm = meta.summary || {}, m = s.metrics || {};
  $("#history-body").hidden = true;
  $("#history-empty").hidden = true;
  const det = $("#detail");
  det.hidden = false;
  const tile = (l, v, sub = "") => `<div class="tile"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${sub}</div></div>`;
  const evs = s.events || [];
  det.innerHTML = `
    <div class="row gap between" style="margin-bottom:12px"><button class="ghost small" id="back">← All sessions</button>
      <span class="row gap-s"><a class="ghost small" id="export" href="/api/sessions/${encodeURIComponent(id)}/export.csv" download>Export CSV</a>
      <button class="ghost small" id="open-folder">Show files</button><button class="ghost small" id="trash">Delete</button></span></div>
    <article class="card">
      <header class="card-h"><h2>${esc(meta.practice || "Session")} · ${esc(meta.started_at ? meta.started_at.slice(0, 16).replace("T", " ") : "")}</h2><span class="hint">${fmtClock(meta.duration_s)}${sm.coverage_pct != null && sm.coverage_pct < 99.5 ? ` · WiFi data ${num(sm.coverage_pct, 0)}% of the time` : ""}${meta.demo ? " · demo data" : ""}</span></header>
      ${meta.intention ? `<p class="caption">Intention: ${esc(meta.intention)}</p>` : ""}
      <div class="tiles">
        ${tile("Settled in", sm.settle_s == null ? "--" : fmtClock(sm.settle_s), sm.settle_s == null ? "no 3 still minutes in a row" : "first 3 still minutes began")}
        ${tile("Steady", sm.steady_min == null ? "--" : `${num(sm.steady_min, 0)} min`, sm.steady_pct == null ? "" : `${num(sm.steady_pct, 0)}% of your sitting${usual(id, "steady_pct", (v) => ` · usual ${num(v, 0)}%`)}`)}
        ${tile("Longest still", sm.longest_still_s == null ? "--" : fmtClock(sm.longest_still_s), `${sm.shifts ?? 0} shift${sm.shifts === 1 ? "" : "s"}`)}
        ${tile("Restless · drowsy", `${sm.restless_n ?? "--"} · ${sm.drowsy_n ?? "--"}`, `${num(sm.restless_min, 1)} · ${num(sm.drowsy_min, 1)} min`)}
        ${tile("Breathing", num(sm.steady_breath ?? sm.breath_avg), `while steady · ${num(sm.breath_start)} → ${num(sm.breath_end)} start to end`)}
        ${tile("Posture", num(sm.posture_avg, 0), sm.posture_aligned_pct == null ? (sm.posture_avg == null ? "camera was off" : "") : `${num(sm.posture_aligned_pct, 0)}% aligned${sm.nods ? ` · ${sm.nods} nods` : ""}`)}
        ${tile("Heart", sm.sensor_hr_avg != null ? num(sm.sensor_hr_avg, 0) : sm.polar_hr_avg != null ? num(sm.polar_hr_avg, 0) : meta.polar_avg_hr ?? "--", sm.sensor_hr_avg != null ? "pulse sensor" : (sm.polar_hr_avg != null || meta.polar_avg_hr) ? "your watch" : "no sensor")}
      </div>
      <div class="strip-wrap"><h3>Practice through the session</h3><canvas id="d-state" class="strip" aria-label="Practice state through the session"></canvas>
        <div class="legend">${STATE_ORDER.map((st) => `<span><i style="background:var(--st-${st})"></i>${PRACTICE_UI[st][1]}</span>`).join("")}</div></div>
    </article>
    <article class="card">
      <header class="card-h"><h2>During the session</h2><span class="hint">Dots on the time axis are events; hover for details</span></header>
      <div class="multiples">
        <div><h3><span class="key" style="background:var(--breath)"></span>Breathing (per min)</h3><canvas id="d-breath"></canvas></div>
        <div><h3><span class="key" style="background:var(--heart)"></span>Heart rate (per min)</h3><canvas id="d-heart"></canvas></div>
        <div><h3><span class="key" style="background:var(--motion)"></span>Movement (× quiet level)</h3><canvas id="d-motion"></canvas></div>
        <div><h3><span class="key" style="background:var(--posture)"></span>Posture score</h3><canvas id="d-posture"></canvas></div>
      </div>
    </article>
    <div class="grid" style="grid-template-columns:repeat(2,minmax(0,1fr))">
      <article class="card"><header class="card-h"><h2>Events</h2></header>
        <ul class="events">${evs.length ? evs.map((e) => `<li><span class="t">${fmtClock(e.t)}</span><span>${esc(EVENT_LABEL[e.kind] || e.kind)}${e.text ? ": " + esc(e.text) : e.value != null ? ": " + esc(e.value) : ""}${e.app_sensor_hr ? ` (sensor ${Math.round(e.app_sensor_hr)})` : e.app_heart_rate_experimental ? ` (WiFi estimate ~${Math.round(e.app_heart_rate_experimental)})` : ""}${e.app_breath_rate ? ` (app ${num(e.app_breath_rate)})` : ""}${e.advice && e.advice.length ? ` (${esc(e.advice[0])})` : ""}</span></li>`).join("") : '<li class="caption">No events.</li>'}</ul>
      </article>
      <article class="card"><header class="card-h"><h2>Reflection</h2></header>
        <form id="meta-form" class="col">
          <div class="field">Depth (1–5)<div class="chips" id="d-rating">${[1, 2, 3, 4, 5].map((v) => `<button type="button" class="chip ${meta.rating === v ? "on" : ""}" data-v="${v}">${v}</button>`).join("")}</div></div>
          ${meta.mood && meta.mood.length ? `<p class="caption">Felt: ${esc(meta.mood.join(", "))}</p>` : ""}
          <label class="field">Notes<textarea id="d-notes" rows="4">${esc(meta.notes || "")}</textarea></label>
          <div class="row gap"><button class="primary small" type="submit">Save notes</button><span class="caption" id="d-saved"></span></div>
        </form>
      </article>
    </div>`;
  const t = m.t || [];
  const pts = (k) => t.map((x, i) => [x, m[k] ? m[k][i] : null]);
  const evMarks = evs.filter((e) => ["shift", "mark", "slump", "nod", "posture_alert", "board"].includes(e.kind)).map((e) => ({ x: e.t, label: EVENT_LABEL[e.kind] + (e.text ? `: ${e.text}` : "") }));
  const runs = s.timeline || [];
  const tMax = Math.max(meta.duration_s || 0, runs.length ? runs[runs.length - 1][1] : 0, 60);
  detailStrip = () => drawStateStrip($("#d-state"), runs, 0, tMax);
  detailStrip();
  const polar = evs.filter((e) => e.kind === "polar_hr").map((e) => [e.t, e.value]);
  const counts = evs.filter((e) => e.kind === "breath_count").map((e) => [e.t, e.value]);
  const breathSeries = [{ label: "Breathing", color: "--breath", points: pts("breath_rate"), unit: "/min", gap: 5 }];
  if (counts.length) breathSeries.push({ label: "Your count", color: "--text-secondary", points: counts, dots: true, digits: 0, unit: "/min" });
  const sensor = m.sensor_hr && m.sensor_hr.some((v) => v != null);
  // WiFi's own heart estimate stays out of sight here too: it's too often wrong.
  const heartSeries = sensor ? [{ label: "Pulse sensor / radar", color: "--heart", points: pts("sensor_hr"), digits: 0, unit: "bpm", gap: 5 }] : [];
  if (polar.length) heartSeries.push({ label: "Polar watch", color: "--heart", points: polar, dots: true, digits: 0, unit: "bpm" });
  const chart = (sel, opts, series) => { const c = new LineChart($(sel), { ...opts, xMin: 0 }); c.set(series, evMarks); return c; };
  detailCharts = [
    chart("#d-breath", CHART_OPTS.breath, breathSeries),
    chart("#d-motion", CHART_OPTS.motion, [{ label: "Movement", color: "--motion", points: pts("motion"), unit: "×", gap: 5 }]),
    chart("#d-posture", CHART_OPTS.posture, [{ label: "Posture", color: "--posture", points: pts("posture_score"), digits: 0, gap: 5 }]),
  ];
  if (heartSeries.length) detailCharts.push(chart("#d-heart", CHART_OPTS.heart, heartSeries));
  else $("#d-heart").replaceWith(Object.assign(document.createElement("p"), { className: "caption",
    textContent: "No heart data for this session. WiFi can't measure heart rate reliably: press H during a session to enter your watch's reading, or add a pulse sensor (MediSense Pro)." }));

  let rating = meta.rating ?? null;
  $$("#d-rating .chip").forEach((b) => b.addEventListener("click", () => {
    rating = +b.dataset.v;
    $$("#d-rating .chip").forEach((x) => x.classList.toggle("on", x === b));
  }));
  $("#meta-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await api(`/api/sessions/${encodeURIComponent(id)}/meta`, "POST", { notes: $("#d-notes").value, rating }).catch((err) => alert(err.message));
    $("#d-saved").textContent = "Saved.";
  });
  $("#back").onclick = closeDetail;
  $("#open-folder").onclick = () => api(`/api/sessions/${encodeURIComponent(id)}/open`, "POST", {}).catch((e) => alert(e.message));
  $("#trash").onclick = async () => {
    if (!confirm("Move this session to the trash folder (~/MediSense/trash)?")) return;
    await api(`/api/sessions/${encodeURIComponent(id)}`, "DELETE").catch((e) => alert(e.message));
    closeDetail();
  };
  window.scrollTo(0, 0);
}
function closeDetail() {
  $("#detail").hidden = true;
  $("#detail").innerHTML = "";
  detailCharts = [];
  detailStrip = null;
  loadHistory();
  loadProfile();
}
// This session's number next to the median of the 7 sessions before it.
function usual(id, key, fmt) {
  const done = historyRows.filter((s) => s.ended_at && !s.demo);
  const i = done.findIndex((s) => s.id === id);
  if (i < 0) return "";
  const prev = done.slice(i + 1, i + 8).map((s) => (s.summary || {})[key]).filter((v) => v != null).sort((a, b) => a - b);
  if (prev.length < 3) return "";
  return fmt(prev[Math.floor(prev.length / 2)]);
}

window.addEventListener("resize", () => setTimeout(redrawAll, 50));
