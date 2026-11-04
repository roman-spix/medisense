import { LineChart } from "./charts.js";
import { BrowserPoseCamera, drawSkeleton } from "./posture.js";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pad2 = (n) => String(n).padStart(2, "0");
// Days are named by this Mac's clock (the app names its files the same way), not UTC.
const localDay = (d = new Date()) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
const hhmm = (epoch) => { const d = new Date(epoch * 1000); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };
const minutes = (m) => m == null ? "--" : m < 1 ? `${Math.round(m * 60)} s` : m < 60 ? `${Math.round(m)} min` : `${Math.floor(m / 60)} h ${pad2(Math.round(m % 60))}`;

const store = {
  get(k, d) { try { const v = localStorage.getItem("msp." + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("msp." + k, JSON.stringify(v)); } catch (e) {} },
};

async function api(path, method = "GET", body) {
  const r = await fetch(path, { method, headers: { "Content-Type": "application/json", "X-MediSense": "1" },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
}

// ---------------------------------------------------------------- theme
function applyTheme(t) {
  if (t) document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme");
  setTimeout(() => dayChart.draw(), 0);
}
$("#theme").addEventListener("click", () => {
  const dark = getComputedStyle(document.documentElement).colorScheme.includes("dark");
  store.set("theme", dark ? "light" : "dark"); applyTheme(dark ? "light" : "dark");
});

// Short labels: each point keeps the same size whatever its state, so the
// cards below never jump while you're adjusting the reminders.
const STATUS = {
  ok: ["✓", "Good"], minor: ["~", "Adjust a little"], major: ["✕", "Out of line"],
  not_visible: ["–", "Not in view"], not_checked: ["–", "Not checked"],
  needs_calibration: ["?", "Calibrate first"], off: ["–", "Off"],
};
const STATE_UI = {
  aligned: ["✓", "Aligned"], ok: ["✓", "Good"], drifting: ["!", "Drifting"], away: ["…", "Away from the desk"],
  waiting: ["…", "Waiting for the camera"], calibrating: ["◎", "Calibrating: hold your best posture"],
  stopped: ["■", "Not tracking"],
};

let settings = null;
let paused = false;
let tracking = true;
let trackingSince = null;
let browserCam = null;
let ws = null;
let pointsSig = "";

const dayChart = new LineChart($("#day-chart"), { yMin: 0, yMax: 100, floor: 0, xFormat: (v) => {
  const h = Math.floor(v / 3600), m = Math.floor((v % 3600) / 60); return `${h}:${pad2(m)}`;
} });
applyTheme(store.get("theme", null));

function setPill(kind, text) { $("#pill").className = `pill ${kind}`; $("#pill-text").textContent = text; }

function renderPoints(points) {
  const sig = (points || []).map((p) => `${p.key}:${p.status}`).join("|");
  if (sig === pointsSig) return;  // nothing changed: leave the list alone
  pointsSig = sig;
  $("#points").innerHTML = (points || []).map((p) => {
    const [icon, text] = STATUS[p.status] || STATUS.not_visible;
    return `<li class="pt pt-${esc(p.status)}"><span class="pt-icon" aria-hidden="true">${icon}</span><div class="pt-body">`
      + `<div class="pt-head"><b>${esc(p.name)}</b><span class="pt-badge">${esc(text)}</span></div>`
      + `<div class="pt-how">${esc(p.how)}</div></div></li>`;
  }).join("");
}

function renderTracking() {
  const b = $("#tracking");
  b.textContent = tracking ? "Stop tracking" : "Start tracking";
  b.className = tracking ? "ghost small" : "primary small";
  $("#track-note").textContent = tracking
    ? `Tracking${trackingSince ? " since " + hhmm(trackingSince) : ""}. The numbers update every 10 seconds.`
    : "Not tracking: the camera is off and nothing is recorded. Click Start tracking to go on.";
}

function onPosture(m) {
  const svc = m.service || {};
  paused = !!m.paused;
  tracking = m.tracking !== false;
  trackingSince = m.tracking_since || null;
  renderTracking();
  $("#pause").textContent = paused ? "Resume reminders" : "Pause reminders";
  if (svc.settings && !settings) applySettings(svc.settings);
  const sc = tracking ? m.score : null;
  $("#score").textContent = sc == null ? "--" : Math.round(sc);
  const [icon, label] = (tracking ? STATE_UI[m.state] : STATE_UI.stopped) || STATE_UI.waiting;
  $("#state-icon").textContent = icon;
  $("#state").textContent = label;
  // At most two pieces of advice, in a box of fixed height.
  const advice = (tracking ? m.advice || [] : []).slice(0, 2);
  $("#advice").innerHTML = advice.length ? advice.map((a) => `<li>${esc(a)}</li>`).join("")
    : tracking && sc != null && m.calibrated ? '<li class="calm">Nothing to adjust right now.</li>' : "";
  renderPoints(m.points);
  $("#calib-note").textContent = m.calibrated
    ? "Calibrated. Recalibrate whenever you move the laptop or the chair."
    : "Sit well (tall, shoulders relaxed, head over your shoulders, hands on the keyboard), then click Calibrate.";
  if (m.lm && tracking) drawSkeleton($("#pose"), m.lm);
  // the camera follows the tracking switch
  const note = $("#cam-note");
  if (!tracking) {
    if (browserCam) { browserCam.stop(); browserCam = null; }
    note.textContent = "The camera is off while tracking is stopped.";
    note.dataset.kind = "off";
  } else if (svc.state === "running") note.textContent = "Background camera engine running: works with this page closed.";
  else if (svc.state === "starting") note.textContent = "Starting the camera engine…";
  else if (svc.state === "error") note.textContent = svc.error || "Camera problem.";
  else if (svc.state === "browser" && !browserCam) startBrowserCam();
  else if (svc.state === "browser" && browserCam && note.dataset.kind === "tracking")
    note.textContent = "Watching with this page's camera. Keep this page open (it can sit behind other windows; don't close it).";
  const showRestart = tracking && (svc.state === "browser" || svc.state === "error") && !(browserCam && browserCam.running);
  $("#browser-cam").classList.toggle("invisible", !showRestart);
  if (!tracking) setPill("warn", "Not tracking");
  else if (paused) setPill("warn", "Reminders paused");
  else if (m.state === "drifting") setPill("bad", `Drifting · ${Math.round(sc)}`);
  else if (sc != null) setPill("live", `Aligned · ${Math.round(sc)}`);
  else if (svc.state === "error") setPill("bad", "Camera problem");
  else setPill("warn", m.state === "away" ? "Away" : "Waiting");
}

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (e) => {
    let m; try { m = JSON.parse(e.data); } catch (err) { return; }
    if (m.type === "posture") onPosture(m);
    else if (m.type === "day") { if ($("#day").value === m.day) loadDay(m.day); else if (m.day === localDay()) loadDays(); }
    else if (m.type === "preview") { $("#preview-img").src = `data:image/jpeg;base64,${m.jpg}`; }
  };
  ws.onopen = () => { if (previewOn) ws.send(JSON.stringify({ type: "preview", on: true })); };
  ws.onclose = () => { setPill("bad", "Posture app stopped"); setTimeout(connect, 2000); };
}
connect();

// ------------------------------------------------------------- controls
$("#calibrate").addEventListener("click", async () => {
  if (!tracking) { alert("Start tracking first: calibrating needs the camera."); return; }
  await api("/api/calibrate", "POST", {});
  $("#calib-note").textContent = "Calibrating for 5 seconds: hold your best posture…";
});
$("#pause").addEventListener("click", () => saveSettings({ paused: !paused }));
$("#tracking").addEventListener("click", async () => {
  const b = $("#tracking");
  b.disabled = true;
  try {
    const r = await api("/api/tracking", "POST", { on: !tracking });
    onPosture({ ...r.posture, service: r.service, paused: r.paused, tracking: r.tracking, tracking_since: r.tracking_since });
  } catch (e) {
    alert(e.message);
  }
  b.disabled = false;
  loadDay($("#day").value);
});
$("#test").addEventListener("click", async () => {
  const r = await api("/api/test-notification", "POST", {}).catch(() => ({ ok: false }));
  $("#test-note").textContent = r.ok ? "Sent." : "Couldn't send (see the note below).";
});

let previewOn = false;
$("#preview").addEventListener("click", () => {
  previewOn = !previewOn;
  $("#preview").textContent = previewOn ? "Hide camera" : "Show camera";
  if (browserCam) {  // the page's camera: video under the skeleton, same picture
    browserCam.showVideo = previewOn;
    return;
  }
  $("#preview-img").hidden = !previewOn;
  $("#pose").hidden = previewOn;
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "preview", on: previewOn }));
});
function startBrowserCam() {
  browserCam = new BrowserPoseCamera({
    canvas: $("#pose"),
    fps: 3,  // posture changes slowly: 3 checks a second spare an older Mac all day
    onLandmarks: (lm, world, aspect) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "pose", lm, world, aspect })); },
    onStatus: (kind, msg) => { const n = $("#cam-note"); n.textContent = msg; n.dataset.kind = kind; },
  });
  browserCam.showVideo = previewOn;
  browserCam.start();
  $("#browser-cam").classList.add("invisible");
}
$("#browser-cam").addEventListener("click", () => {
  if (browserCam) browserCam.stop();
  browserCam = null;
  startBrowserCam();
});

function applySettings(s) {
  settings = s;
  $("#threshold").value = s.threshold; $("#v-threshold").textContent = Math.round(s.threshold);
  $("#hold").value = s.hold_s; $("#v-hold").textContent = `${Math.round(s.hold_s)} s`;
  $("#cooldown").value = Math.round(s.cooldown_s / 60); $("#v-cooldown").textContent = `${Math.round(s.cooldown_s / 60)} min`;
  $("#enabled").checked = s.enabled; $("#sound").checked = s.sound;
}
async function saveSettings(patch) {
  const r = await api("/api/settings", "POST", patch).catch((e) => { alert(e.message); return null; });
  if (r && r.service) {
    settings = null; applySettings(r.service.settings);
    onPosture({ ...r.posture, service: r.service, paused: r.paused, tracking: r.tracking, tracking_since: r.tracking_since });
    if ("threshold" in patch) loadDay($("#day").value);  // "upright" is measured against it
  }
}
$("#threshold").addEventListener("input", (e) => { $("#v-threshold").textContent = e.target.value; });
$("#hold").addEventListener("input", (e) => { $("#v-hold").textContent = `${e.target.value} s`; });
$("#cooldown").addEventListener("input", (e) => { $("#v-cooldown").textContent = `${e.target.value} min`; });
$("#threshold").addEventListener("change", (e) => saveSettings({ threshold: +e.target.value }));
$("#hold").addEventListener("change", (e) => saveSettings({ hold_s: +e.target.value }));
$("#cooldown").addEventListener("change", (e) => saveSettings({ cooldown_s: +e.target.value * 60 }));
$("#enabled").addEventListener("change", (e) => saveSettings({ enabled: e.target.checked }));
$("#sound").addEventListener("change", (e) => saveSettings({ sound: e.target.checked }));

// --------------------------------------------------------------- the day
async function loadDays() {
  const days = await api("/api/days").catch(() => []);
  const today = localDay();
  const keep = $("#day").value;
  const list = days.length && days[0].day === today ? days : [{ day: today }, ...days];
  $("#day").innerHTML = list.map((d) => `<option value="${d.day}">${d.day === today ? "Today" : d.day}</option>`).join("");
  if (keep && list.some((d) => d.day === keep)) $("#day").value = keep;
  loadDay($("#day").value);
}
async function loadDay(day) {
  const h = await api(`/api/history?day=${encodeURIComponent(day)}`).catch(() => null);
  if (!h) return;
  const today = day === localDay();
  $("#h-today").textContent = today ? "Today" : day;
  $("#t-min").textContent = minutes(h.minutes_tracked || null);
  $("#t-span").textContent = h.first ? `${h.first} to ${h.last}` : "in view of the camera";
  $("#t-aligned").textContent = h.aligned_pct == null ? "--" : `${Math.round(h.aligned_pct)}%`;
  $("#t-aligned-s").textContent = `of the time at ${Math.round(h.threshold)} or more`;
  $("#t-longest").textContent = minutes(h.longest_upright_min);
  $("#t-avg").textContent = h.average == null ? "--" : Math.round(h.average);
  $("#t-alerts").textContent = h.alerts ?? 0;
  const issues = h.issues || [];
  const top = Math.max(1, ...issues.map((i) => i.minutes));
  $("#issues").innerHTML = issues.length
    ? issues.map((i) => `<li><span>${esc(i.name)}</span><span class="min">${minutes(i.minutes)}</span>`
      + `<span class="bar"><span style="width:${Math.max(4, Math.round(100 * i.minutes / top))}%"></span></span></li>`).join("")
    : `<li class="empty">${h.minutes_tracked ? "Nothing: you stayed upright." : "Nothing yet. Time below your reminder level shows up here, by cause."}</li>`;
  const pts = h.time.map((t, i) => { const [a, b, c] = t.split(":").map(Number); return [a * 3600 + b * 60 + c, h.score[i]]; });
  dayChart.set([{ label: "Posture", color: "--posture", points: pts, digits: 0, gap: 120 }]);
}
$("#day").addEventListener("change", (e) => loadDay(e.target.value));
loadDays();
// After midnight "Today" is a new day.
setInterval(() => { if (!$("#day").querySelector(`option[value="${localDay()}"]`)) loadDays(); }, 60000);
window.addEventListener("resize", () => dayChart.draw());
