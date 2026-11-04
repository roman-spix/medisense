import { LineChart } from "/static/charts.js";
import { BrowserPoseCamera, drawSkeleton } from "/static/posture.js";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

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

const STATUS = {
  ok: ["✓", "Good"], minor: ["~", "Small adjustment"], major: ["✕", "Out of alignment"],
  not_visible: ["–", "Not in view"], not_checked: ["–", "Not visible to a camera"],
  needs_calibration: ["?", "Calibrate to check"], off: ["–", "Not checked"],
};
const STATE_UI = {
  aligned: ["✓", "Aligned"], ok: ["✓", "Good"], drifting: ["!", "Drifting"], away: ["…", "Away"],
  waiting: ["…", "Waiting for the camera"], calibrating: ["◎", "Calibrating: hold your best posture"],
};

let settings = null;
let paused = false;
let browserCam = null;
let ws = null;

const dayChart = new LineChart($("#day-chart"), { yMin: 0, yMax: 100, floor: 0, xFormat: (v) => {
  const h = Math.floor(v / 3600), m = Math.floor((v % 3600) / 60); return `${h}:${String(m).padStart(2, "0")}`;
} });
applyTheme(store.get("theme", null));

function setPill(kind, text) { $("#pill").className = `pill ${kind}`; $("#pill-text").textContent = text; }

function renderPoints(points) {
  $("#points").innerHTML = (points || []).map((p) => {
    const [icon, text] = STATUS[p.status] || STATUS.not_visible;
    const tip = p.advice ? `<div class="tip">${esc(p.advice)}</div>` : "";
    return `<li class="pt pt-${esc(p.status)}"><span class="pt-icon" aria-hidden="true">${icon}</span><div><b>${esc(p.name)}</b><div class="pt-status">${esc(text)} · <span class="pt-how">${esc(p.how)}</span></div>${tip}</div></li>`;
  }).join("");
}

function onPosture(m) {
  const svc = m.service || {};
  paused = !!m.paused;
  $("#pause").textContent = paused ? "Resume reminders" : "Pause reminders";
  if (svc.settings && !settings) applySettings(svc.settings);
  const sc = m.score;
  $("#score").textContent = sc == null ? "--" : Math.round(sc);
  const [icon, label] = STATE_UI[m.state] || STATE_UI.waiting;
  $("#state-icon").textContent = icon;
  $("#state").textContent = label;
  $("#advice").innerHTML = (m.advice || []).map((a) => `<li>${esc(a)}</li>`).join("");
  renderPoints(m.points);
  $("#calib-note").textContent = m.calibrated
    ? "Calibrated. Recalibrate whenever you move the laptop or the chair."
    : "Sit well at your desk (tall, shoulders relaxed, head over your shoulders, hands on the keyboard), then click Calibrate. Until then only the level checks run.";
  if (m.lm) drawSkeleton($("#pose"), m.lm);
  // camera / engine state
  const note = $("#cam-note");
  if (svc.state === "running") note.textContent = "Background camera engine running: works with this page closed.";
  else if (svc.state === "starting") note.textContent = "Starting the camera engine…";
  else if (svc.state === "error") note.textContent = svc.error || "Camera problem.";
  else if (svc.state === "browser") note.textContent = browserCam && browserCam.running
    ? "Using this page's camera: keep this page visible (the background engine isn't installed)."
    : "The background camera engine isn't installed. Click below to use this page's camera instead.";
  $("#browser-cam").hidden = !(svc.state === "browser" || svc.state === "error") || (browserCam && browserCam.running);
  if (paused) setPill("warn", "Reminders paused");
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
    else if (m.type === "preview") { $("#preview-img").src = `data:image/jpeg;base64,${m.jpg}`; }
  };
  ws.onopen = () => { if (previewOn) ws.send(JSON.stringify({ type: "preview", on: true })); };
  ws.onclose = () => { setPill("bad", "Posture app stopped"); setTimeout(connect, 2000); };
}
connect();

// ------------------------------------------------------------- controls
$("#calibrate").addEventListener("click", async () => {
  await api("/api/calibrate", "POST", {});
  $("#calib-note").textContent = "Calibrating for 5 seconds: hold your best posture…";
});
$("#pause").addEventListener("click", () => saveSettings({ paused: !paused }));
$("#test").addEventListener("click", async () => {
  const r = await api("/api/test-notification", "POST", {}).catch(() => ({ ok: false }));
  $("#test-note").textContent = r.ok ? "Sent." : "Couldn't send (see the note below).";
});

let previewOn = false;
$("#preview").addEventListener("click", () => {
  previewOn = !previewOn;
  $("#preview").textContent = previewOn ? "Hide camera" : "Show camera";
  $("#preview-img").hidden = !previewOn;
  $("#pose").hidden = previewOn;
  if (browserCam) browserCam.showVideo = previewOn;
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "preview", on: previewOn }));
});
$("#browser-cam").addEventListener("click", () => {
  browserCam = new BrowserPoseCamera({
    canvas: $("#pose"),
    onLandmarks: (lm, world, aspect) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "pose", lm, world, aspect })); },
    onStatus: (kind, msg) => { $("#cam-note").textContent = msg; },
  });
  browserCam.start();
  $("#browser-cam").hidden = true;
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
  if (r && r.service) { settings = null; applySettings(r.service.settings); onPosture({ ...r.posture, service: r.service, paused: r.paused }); }
}
$("#threshold").addEventListener("input", (e) => { $("#v-threshold").textContent = e.target.value; });
$("#hold").addEventListener("input", (e) => { $("#v-hold").textContent = `${e.target.value} s`; });
$("#cooldown").addEventListener("input", (e) => { $("#v-cooldown").textContent = `${e.target.value} min`; });
$("#threshold").addEventListener("change", (e) => saveSettings({ threshold: +e.target.value }));
$("#hold").addEventListener("change", (e) => saveSettings({ hold_s: +e.target.value }));
$("#cooldown").addEventListener("change", (e) => saveSettings({ cooldown_s: +e.target.value * 60 }));
$("#enabled").addEventListener("change", (e) => saveSettings({ enabled: e.target.checked }));
$("#sound").addEventListener("change", (e) => saveSettings({ sound: e.target.checked }));

// --------------------------------------------------------------- history
async function loadDays() {
  const days = await api("/api/days").catch(() => []);
  const today = new Date().toISOString().slice(0, 10);
  const list = days.length && days[0].day === today ? days : [{ day: today }, ...days];
  $("#day").innerHTML = list.map((d) => `<option value="${d.day}">${d.day === today ? "Today" : d.day}</option>`).join("");
  loadDay($("#day").value);
}
async function loadDay(day) {
  const h = await api(`/api/history?day=${encodeURIComponent(day)}`).catch(() => null);
  if (!h) return;
  $("#h-today").textContent = $("#day").selectedOptions[0] ? $("#day").selectedOptions[0].textContent : day;
  $("#t-min").textContent = h.minutes_tracked ?? "--";
  $("#t-aligned").textContent = h.aligned_pct == null ? "--" : `${Math.round(h.aligned_pct)}%`;
  $("#t-avg").textContent = h.average == null ? "--" : Math.round(h.average);
  $("#t-alerts").textContent = h.alerts ?? 0;
  $("#t-fault").textContent = h.most_common_fault ? `most often: ${h.most_common_fault.replace(/_/g, " ")}` : "";
  const pts = h.time.map((t, i) => { const [a, b, c] = t.split(":").map(Number); return [a * 3600 + b * 60 + c, h.score[i]]; });
  dayChart.set([{ label: "Posture", color: "--posture", points: pts, digits: 0, gap: 120 }]);
}
$("#day").addEventListener("change", (e) => loadDay(e.target.value));
loadDays();
setInterval(() => { if ($("#day").selectedIndex === 0) loadDay($("#day").value); }, 60000);
window.addEventListener("resize", () => dayChart.draw());
