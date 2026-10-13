import { LineChart, WaveChart, BarChart, sparkline, fmtClock } from "/static/charts.js";
import { PostureTracker } from "/static/posture.js";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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
  if (name === "history") loadHistory();
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
};

const wave = new WaveChart($("#wave"), 60);
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
  breath: new BarChart($("#p-breath"), { color: "--breath" }),
  still: new BarChart($("#p-still"), { color: "--motion", yMax: 100 }),
  posture: new BarChart($("#p-posture"), { color: "--posture", yMax: 100 }),
};

function redrawAll() {
  wave.draw();
  Object.values(charts).forEach((c) => c.draw());
  Object.values(progress).forEach((c) => c.draw());
  drawSpark();
  detailCharts.forEach((c) => c.draw());
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
  };
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
  else if (st.demo) setPill("warn", "Demo starting…");
  else setPill(...(DEVICE_PILL[dev.state] || ["warn", dev.state || "…"]));
  renderSetup(st);
  renderBoard(st);
}

// ------------------------------------------------------------- metrics

const STATE_UI = {
  still: ["✓", "Still"],
  restless: ["~", "Restless"],
  moving: ["↯", "Moving"],
  unknown: ["…", "Learning"],
};

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
  else if (m.motion_state === "moving") note = "Paused while you move";
  else if (br == null) note = "Calibrating, please sit still (about 20 s)";
  $("#breath-note").textContent = note;

  // Heart
  const hr = m.heart_rate;
  $("#heart-val").textContent = hr == null ? "--" : `~${Math.round(hr)}`;
  $("#heart-meter").style.width = `${Math.min(1, (m.heart_conf || 0) / 0.6) * 100}%`;
  $("#heart-note").textContent = hr == null && m.signal === "ok"
    ? "Heartbeat too faint in the WiFi signal right now. Sit still; it may appear."
    : "From tiny chest movements in the WiFi signal. Compare with your Polar watch using “Watch heart rate”.";

  // Stillness
  const [icon, label] = STATE_UI[m.motion_state] || STATE_UI.unknown;
  $("#still-icon").textContent = icon;
  $("#still-val").textContent = m.signal === "no_signal" ? "--" : label;
  $("#settled").textContent = m.signal === "no_signal" ? "--" : fmtClock(m.settled_s);
  S.motionHist.push(m.signal === "no_signal" ? null : m.motion);
  if (S.motionHist.length > 300) S.motionHist.shift();
  drawSpark();

  // Posture (from the app's view of the camera)
  if (m.posture_score != null) {
    $("#posture-val").textContent = Math.round(m.posture_score);
    $("#posture-slump").textContent = m.slump == null ? "--" : `${Math.round(m.slump * 100)}%`;
    $("#posture-tilt").textContent = m.head_tilt == null ? "--" : `${m.head_tilt > 0 ? "+" : ""}${Math.round(m.head_tilt)}°`;
  } else if (!tracker.running) {
    $("#posture-val").textContent = "--";
  }

  // Signal quality (icon + label, never colour alone)
  const fps = m.frames_per_s || 0;
  const q = fps <= 0 ? ["✕", "No data"] : fps >= 20 && m.loss_pct < 10 ? ["✓", "Good"] : fps >= 8 ? ["~", "Fair"] : ["!", "Weak"];
  $("#sig-icon").textContent = q[0];
  $("#sig-val").textContent = q[1];
  $("#sig-fps").textContent = fps > 0 ? num(fps, 0) : "--";
  $("#sig-loss").textContent = fps > 0 ? `${num(m.loss_pct, 0)}%` : "--";
  $("#sig-rssi").textContent = m.rssi == null || fps <= 0 ? "--" : `${num(m.rssi, 0)} dBm`;
  $("#sig-ch").textContent = m.channel ?? "--";
  const src = S.status && S.status.packet_source;
  $("#sig-note").textContent = fps <= 0 ? "Waiting for data from the board."
    : q[1] === "Good" ? `Data arriving from ${src || "the board"}.`
    : "Few frames are arriving. Move the board closer to the router, or away from metal and other electronics.";

  // Trends: session time when recording, otherwise a rolling 10 minutes.
  const t = m.session_t != null ? m.session_t : performance.now() / 1000 - S.pageStart;
  const T = S.trend;
  T.breath.push([t, br]);
  T.heart.push([t, hr]);
  T.motion.push([t, m.signal === "no_signal" ? null : m.motion]);
  T.posture.push([t, m.posture_score ?? null]);
  if (m.session_t == null) for (const k in T) while (T[k].length && T[k][0][0] < t - 600) T[k].shift();
  drawTrends();
  tickClock();
}

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
  charts.heart.set([{ label: "Heart", color: "--heart", points: T.heart, unit: "bpm", digits: 0, gap: 5 }], ev);
  charts.motion.set([{ label: "Movement", color: "--motion", points: T.motion, unit: "×", gap: 5 }], ev);
  charts.posture.set([{ label: "Posture", color: "--posture", points: T.posture, digits: 0, gap: 5 }], ev);
}

const EVENT_LABEL = { shift: "Shifted position", mark: "Marked moment", polar_hr: "Watch heart rate", breath_count: "Breath count", slump: "Slumped", note: "Note" };

function onEvent(e) {
  if (!S.sessionId) return;
  if (e.kind === "shift") { S.sessionShifts++; $("#shifts").textContent = S.sessionShifts; }
  const label = EVENT_LABEL[e.kind] + (e.value != null ? `: ${e.value}` : "");
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
    if ($("#use-camera").checked && !tracker.running) startCamera();
    beginSessionUi({ id: r.id, elapsed: 0, practice, planned_min: selectedMin || null });
  } catch (e) { alert(e.message); }
});

function beginSessionUi(sess) {
  S.sessionId = sess.id;
  S.sessionShifts = 0;
  S.plannedBellRung = false;
  S.trend = { breath: [], heart: [], motion: [], posture: [] };
  S.trendEvents = [];
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
    $("#end-summary").textContent = `${fmtClock(meta.duration_s)} · breathing ${num(sm.breath_start)} → ${num(sm.breath_end)}/min · still ${num(sm.stillness_pct, 0)}%`;
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

const tracker = new PostureTracker({
  canvas: $("#pose"),
  onMetrics: (p) => {
    sendWs(p);
    if (p.visible && p.score != null) {
      $("#posture-val").textContent = Math.round(p.score);
      $("#posture-slump").textContent = `${Math.round(p.slump * 100)}%`;
      $("#posture-tilt").textContent = `${p.head_tilt > 0 ? "+" : ""}${Math.round(p.head_tilt)}°`;
    }
  },
  onStatus: (kind, msg) => {
    $("#posture-note").textContent = msg;
    if (kind === "error") camUi(false);
  },
});
function camUi(on) {
  $("#cam-toggle").textContent = on ? "Camera off" : "Camera on";
  $("#cam-video").hidden = !on;
  $("#cam-recal").hidden = !on;
  if (!on) { $("#posture-val").textContent = "--"; $("#posture-slump").textContent = "--"; $("#posture-tilt").textContent = "--"; }
}
function startCamera() { camUi(true); tracker.start(); }
$("#cam-toggle").addEventListener("click", () => {
  if (tracker.running) { tracker.stop(); camUi(false); $("#posture-note").textContent = "Camera is off."; }
  else startCamera();
});
$("#cam-recal").addEventListener("click", () => tracker.recalibrate());
$("#cam-video").addEventListener("click", () => {
  tracker.showVideo = !tracker.showVideo;
  $("#cam-video").textContent = tracker.showVideo ? "Hide video" : "Show video";
});

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

function renderBoard(st) {
  const d = st.device || {};
  const cfg = d.configured || {};
  const rows = [
    ["Status", d.message || d.state],
    ["Data", st.live ? `arriving${st.packet_source ? " from " + st.packet_source : ""}` : st.seconds_since_data != null ? `last seen ${Math.round(st.seconds_since_data)} s ago` : "none yet"],
    ["USB port", d.port ? `${d.port} (${d.port_label})` : "not connected"],
    ["Chip", d.chip ? `${d.chip}${d.flash_size ? ", " + d.flash_size + " flash" : ""}` : "--"],
    ["MAC address", d.mac || "--"],
    ["Firmware", d.board_firmware ? `RuView ${d.board_firmware}` : "--"],
    ["Board address", d.board_ip || "--"],
    ["This Mac", st.mac_ip || "--"],
    ["WiFi network", cfg.ssid || "--"],
    ["Router filter", cfg.filter_mac || (cfg.ssid ? "off (listening to all traffic)" : "--")],
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
  progress.minutes.set(chrono.map((s) => ({ label: label(s), value: s.duration_s ? s.duration_s / 60 : null, tip: tip(s, `<b>${num((s.duration_s || 0) / 60, 0)}</b> min`) })));
  progress.breath.set(chrono.map((s) => ({ label: label(s), value: s.summary?.breath_end ?? null, tip: tip(s, `<b>${num(s.summary?.breath_end)}</b> breaths/min at the end`) })));
  progress.still.set(chrono.map((s) => ({ label: label(s), value: s.summary?.stillness_pct ?? null, tip: tip(s, `<b>${num(s.summary?.stillness_pct, 0)}%</b> still`) })));
  progress.posture.set(chrono.map((s) => ({ label: label(s), value: s.summary?.posture_avg ?? null, tip: tip(s, `<b>${num(s.summary?.posture_avg, 0)}</b> posture score`) })));
  $("#sessions-rows").innerHTML = done.map((s) => {
    const sm = s.summary || {};
    return `<tr data-id="${esc(s.id)}" tabindex="0">
      <td>${esc(s.started_at ? s.started_at.slice(0, 16).replace("T", " ") : s.id)}${s.demo ? ' <span class="tag">demo</span>' : ""}</td>
      <td>${esc(s.practice || "")}</td>
      <td class="num">${num((s.duration_s || 0) / 60, 0)}</td>
      <td class="num">${num(sm.breath_start)} → ${num(sm.breath_end)}</td>
      <td class="num">${sm.stillness_pct == null ? "--" : num(sm.stillness_pct, 0) + "%"}</td>
      <td class="num">${sm.shifts ?? "--"}</td>
      <td class="num">${num(sm.posture_avg, 0)}</td>
      <td class="num">${s.rating ?? "--"}</td></tr>`;
  }).join("");
  $$("#sessions-rows tr").forEach((tr) => {
    tr.addEventListener("click", () => openDetail(tr.dataset.id));
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") openDetail(tr.dataset.id); });
  });
}

let detailCharts = [];
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
      <header class="card-h"><h2>${esc(meta.practice || "Session")} · ${esc(meta.started_at ? meta.started_at.slice(0, 16).replace("T", " ") : "")}</h2><span class="hint">${fmtClock(meta.duration_s)}${meta.demo ? " · demo data" : ""}</span></header>
      ${meta.intention ? `<p class="caption">Intention: ${esc(meta.intention)}</p>` : ""}
      <div class="tiles">
        ${tile("Breathing", `${num(sm.breath_start)} → ${num(sm.breath_end)}`, "per min, start vs end")}
        ${tile("Slowest minute", num(sm.breath_lowest_minute), "breaths per min")}
        ${tile("Breath evenness", sm.breath_regularity == null ? "--" : Math.round(sm.breath_regularity * 100) + "%", "steadier is higher")}
        ${tile("Stillness", sm.stillness_pct == null ? "--" : num(sm.stillness_pct, 0) + "%", `${sm.shifts ?? 0} shifts`)}
        ${tile("Heart (WiFi)", sm.heart_avg == null ? "--" : "~" + num(sm.heart_avg, 0), sm.polar_hr_avg ? `watch: ${num(sm.polar_hr_avg, 0)}` : (meta.polar_avg_hr ? `watch avg: ${meta.polar_avg_hr}` : "rough"))}
        ${tile("Posture", num(sm.posture_avg, 0), sm.posture_upright_pct == null ? "camera was off" : `${num(sm.posture_upright_pct, 0)}% upright`)}
      </div>
    </article>
    <article class="card">
      <header class="card-h"><h2>During the session</h2><span class="hint">Dots on the time axis are events; hover for details</span></header>
      <div class="multiples">
        <div><h3><span class="key" style="background:var(--breath)"></span>Breathing (per min)</h3><canvas id="d-breath"></canvas></div>
        <div><h3><span class="key" style="background:var(--heart)"></span>Heart rate (per min, rough)</h3><canvas id="d-heart"></canvas></div>
        <div><h3><span class="key" style="background:var(--motion)"></span>Movement (× quiet level)</h3><canvas id="d-motion"></canvas></div>
        <div><h3><span class="key" style="background:var(--posture)"></span>Posture score</h3><canvas id="d-posture"></canvas></div>
      </div>
    </article>
    <div class="grid" style="grid-template-columns:repeat(2,minmax(0,1fr))">
      <article class="card"><header class="card-h"><h2>Events</h2></header>
        <ul class="events">${evs.length ? evs.map((e) => `<li><span class="t">${fmtClock(e.t)}</span><span>${esc(EVENT_LABEL[e.kind] || e.kind)}${e.value != null ? ": " + esc(e.value) : ""}${e.app_heart_rate ? ` (app ~${Math.round(e.app_heart_rate)})` : ""}${e.app_breath_rate ? ` (app ${num(e.app_breath_rate)})` : ""}</span></li>`).join("") : '<li class="caption">No events.</li>'}</ul>
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
  const evMarks = evs.filter((e) => ["shift", "mark", "slump"].includes(e.kind)).map((e) => ({ x: e.t, label: EVENT_LABEL[e.kind] }));
  const polar = evs.filter((e) => e.kind === "polar_hr").map((e) => [e.t, e.value]);
  const counts = evs.filter((e) => e.kind === "breath_count").map((e) => [e.t, e.value]);
  detailCharts = [
    new LineChart($("#d-breath"), { ...CHART_OPTS.breath, xMin: 0 }),
    new LineChart($("#d-heart"), { ...CHART_OPTS.heart, xMin: 0 }),
    new LineChart($("#d-motion"), { ...CHART_OPTS.motion, xMin: 0 }),
    new LineChart($("#d-posture"), { ...CHART_OPTS.posture, xMin: 0 }),
  ];
  const breathSeries = [{ label: "Breathing", color: "--breath", points: pts("breath_rate"), unit: "/min", gap: 5 }];
  if (counts.length) breathSeries.push({ label: "Your count", color: "--text-secondary", points: counts, dots: true, digits: 0, unit: "/min" });
  const heartSeries = [{ label: "Heart (WiFi)", color: "--heart", points: pts("heart_rate"), digits: 0, unit: "bpm", gap: 5 }];
  if (polar.length) heartSeries.push({ label: "Polar watch", color: "--text-secondary", points: polar, dots: true, digits: 0, unit: "bpm" });
  detailCharts[0].set(breathSeries, evMarks);
  detailCharts[1].set(heartSeries, evMarks);
  detailCharts[2].set([{ label: "Movement", color: "--motion", points: pts("motion"), unit: "×", gap: 5 }], evMarks);
  detailCharts[3].set([{ label: "Posture", color: "--posture", points: pts("posture_score"), digits: 0, gap: 5 }], evMarks);

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
  loadHistory();
}

window.addEventListener("resize", () => setTimeout(redrawAll, 50));
