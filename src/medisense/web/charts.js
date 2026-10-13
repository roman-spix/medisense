// Small canvas charts, no libraries (keeps an old Mac fast and works offline).
// Marks follow one spec: 2px lines, 8px end dots with a 2px surface ring,
// hairline solid gridlines, text in text tokens (never the series colour).

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, canvas.clientWidth), h = Math.max(1, canvas.clientHeight);
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

export function niceTicks(min, max, count = 4) {
  if (!isFinite(min) || !isFinite(max)) return [];
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  const step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const norm = step0 / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-6; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}

export function fmtClock(sec) {
  if (sec == null || !isFinite(sec)) return "";
  const s = Math.max(0, Math.round(sec));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
}

// ------------------------------------------------------------- tooltip

let tipEl = null;
function tooltip() {
  if (!tipEl) {
    tipEl = document.createElement("div");
    tipEl.className = "chart-tip";
    tipEl.setAttribute("role", "status");
    document.body.appendChild(tipEl);
  }
  return tipEl;
}
function showTip(html, x, y) {
  const t = tooltip();
  t.innerHTML = html;
  t.style.display = "block";
  const r = t.getBoundingClientRect();
  let left = x + 14, top = y - r.height - 10;
  if (left + r.width > window.innerWidth - 8) left = x - r.width - 14;
  if (top < 8) top = y + 14;
  t.style.left = `${left + window.scrollX}px`;
  t.style.top = `${top + window.scrollY}px`;
}
function hideTip() { if (tipEl) tipEl.style.display = "none"; }

// ------------------------------------------------------------ LineChart

// series: [{label, color (css var name), points: [[x, y|null], ...], unit, digits}]
// events: [{x, label}] drawn as small ticks on the baseline
export class LineChart {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.opts = Object.assign({ xFormat: fmtClock, yMin: null, yMax: null, padTop: 10, padBottom: 22, padLeft: 36, padRight: 44 }, opts);
    this.series = [];
    this.events = [];
    this.hoverX = null;
    canvas.addEventListener("mousemove", (e) => this._hover(e));
    canvas.addEventListener("mouseleave", () => { this.hoverX = null; hideTip(); this.draw(); });
    canvas.addEventListener("touchstart", (e) => this._hover(e.touches[0]), { passive: true });
    this._ro = new ResizeObserver(() => this.draw());
    this._ro.observe(canvas);
  }

  set(series, events = []) { this.series = series; this.events = events; this.draw(); }

  _extent() {
    let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
    for (const s of this.series) for (const [x, y] of s.points) {
      if (x < xmin) xmin = x; if (x > xmax) xmax = x;
      if (y == null) continue;
      if (y < ymin) ymin = y; if (y > ymax) ymax = y;
    }
    if (this.opts.xMin != null) xmin = this.opts.xMin;
    if (this.opts.xMax != null) xmax = Math.max(this.opts.xMax, xmax);
    if (this.opts.yMin != null) ymin = Math.min(this.opts.yMin, ymin);
    if (this.opts.yMax != null) ymax = Math.max(this.opts.yMax, ymax);
    if (!isFinite(ymin)) { ymin = this.opts.yMin ?? 0; ymax = this.opts.yMax ?? 1; }
    if (!isFinite(xmin)) { xmin = 0; xmax = 60; }
    if (xmax - xmin < 1) xmax = xmin + 1;
    const pad = (ymax - ymin) * 0.12 || 1;
    if (this.opts.yMin == null) ymin -= pad;
    if (this.opts.yMax == null) ymax += pad;
    if (this.opts.floor != null) ymin = Math.max(this.opts.floor, ymin);
    return { xmin, xmax, ymin, ymax };
  }

  draw() {
    const { ctx, w, h } = setupCanvas(this.canvas);
    const o = this.opts;
    ctx.clearRect(0, 0, w, h);
    const e = this._extent();
    const ticks = niceTicks(e.ymin, e.ymax, h < 120 ? 2 : 3);
    if (ticks.length) { e.ymin = Math.min(e.ymin, ticks[0]); e.ymax = Math.max(e.ymax, ticks[ticks.length - 1]); }
    const X = (x) => o.padLeft + (x - e.xmin) / (e.xmax - e.xmin) * (w - o.padLeft - o.padRight);
    const Y = (y) => o.padTop + (1 - (y - e.ymin) / (e.ymax - e.ymin)) * (h - o.padTop - o.padBottom);
    this._geom = { X, e, w, h };

    ctx.font = "11px system-ui, -apple-system, sans-serif";
    ctx.lineWidth = 1;
    ctx.strokeStyle = css("--grid");
    ctx.fillStyle = css("--text-muted");
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (const t of ticks) {
      const y = Math.round(Y(t)) + 0.5;
      ctx.beginPath(); ctx.moveTo(o.padLeft, y); ctx.lineTo(w - o.padRight, y); ctx.stroke();
      ctx.fillText(o.yFormat ? o.yFormat(t) : String(t), o.padLeft - 6, y);
    }
    // x ticks
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const xt = niceTicks(e.xmin, e.xmax, Math.max(2, Math.floor(w / 110)));
    for (const t of xt) {
      if (t < e.xmin || t > e.xmax) continue;
      ctx.fillText(o.xFormat(t), X(t), h - o.padBottom + 6);
    }
    ctx.strokeStyle = css("--axis");
    ctx.beginPath(); ctx.moveTo(o.padLeft, h - o.padBottom + 0.5); ctx.lineTo(w - o.padRight, h - o.padBottom + 0.5); ctx.stroke();

    // events on the baseline
    for (const ev of this.events) {
      if (ev.x < e.xmin || ev.x > e.xmax) continue;
      ctx.fillStyle = css("--text-muted");
      ctx.beginPath(); ctx.arc(X(ev.x), h - o.padBottom, 3, 0, Math.PI * 2); ctx.fill();
    }

    for (const s of this.series) {
      const color = css(s.color);
      if (s.dots) {
        for (const [x, y] of s.points) {
          if (y == null) continue;
          ctx.fillStyle = css("--surface"); ctx.beginPath(); ctx.arc(X(x), Y(y), 6, 0, Math.PI * 2); ctx.fill();
          ctx.fillStyle = color; ctx.beginPath(); ctx.arc(X(x), Y(y), 4, 0, Math.PI * 2); ctx.fill();
        }
        continue;
      }
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round"; ctx.lineCap = "round";
      ctx.beginPath();
      let pen = false, lastX = null;
      const gap = s.gap ?? Infinity;
      for (const [x, y] of s.points) {
        if (y == null || (lastX != null && x - lastX > gap)) { pen = false; }
        if (y == null) { lastX = x; continue; }
        if (!pen) { ctx.moveTo(X(x), Y(y)); pen = true; } else ctx.lineTo(X(x), Y(y));
        lastX = x;
      }
      ctx.stroke();
      // end dot + value label
      const last = [...s.points].reverse().find((p) => p[1] != null);
      if (last && !s.noEnd) {
        const [lx, ly] = last;
        ctx.fillStyle = css("--surface"); ctx.beginPath(); ctx.arc(X(lx), Y(ly), 6, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = color; ctx.beginPath(); ctx.arc(X(lx), Y(ly), 4, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = css("--text-secondary");
        ctx.textAlign = "left"; ctx.textBaseline = "middle";
        ctx.fillText(ly.toFixed(s.digits ?? 1), Math.min(X(lx) + 9, w - o.padRight + 9), Y(ly));
      }
    }

    if (this.hoverX != null) {
      ctx.strokeStyle = css("--axis"); ctx.lineWidth = 1;
      const x = Math.round(X(this.hoverX)) + 0.5;
      ctx.beginPath(); ctx.moveTo(x, o.padTop); ctx.lineTo(x, h - o.padBottom); ctx.stroke();
    }
  }

  _hover(ev) {
    if (!this._geom || !this.series.length) return;
    const r = this.canvas.getBoundingClientRect();
    const px = ev.clientX - r.left;
    const { e, w } = this._geom;
    const o = this.opts;
    const x = e.xmin + (px - o.padLeft) / (w - o.padLeft - o.padRight) * (e.xmax - e.xmin);
    if (x < e.xmin || x > e.xmax) { this.hoverX = null; hideTip(); this.draw(); return; }
    let rows = [], snapX = null;
    for (const s of this.series) {
      let best = null;
      for (const p of s.points) if (p[1] != null && (best == null || Math.abs(p[0] - x) < Math.abs(best[0] - x))) best = p;
      if (best && Math.abs(best[0] - x) <= (e.xmax - e.xmin) / 40 + 2) {
        snapX = snapX ?? best[0];
        rows.push(`<div class="tip-row"><span class="key" style="background:var(${s.color})"></span>${s.label}: <b>${best[1].toFixed(s.digits ?? 1)}</b>${s.unit ? " " + s.unit : ""}</div>`);
      }
    }
    for (const evn of this.events) {
      if (Math.abs(evn.x - x) <= (e.xmax - e.xmin) / 80 + 1) rows.push(`<div class="tip-row muted">${evn.label}</div>`);
    }
    this.hoverX = snapX ?? x;
    this.draw();
    if (rows.length) showTip(`<div class="tip-x">${o.xFormat(this.hoverX)}</div>${rows.join("")}`, ev.clientX, ev.clientY);
    else hideTip();
  }
}

// ------------------------------------------------------------ WaveChart

// Live breathing waveform, scrolling. Movement shown as a soft background wash.
export class WaveChart {
  constructor(canvas, seconds = 60) {
    this.canvas = canvas;
    this.seconds = seconds;
    this.data = []; // [t, value, motion]
    this.scale = 0.6;
    this.restless = 2.2;
    this._raf = null;
    canvas.addEventListener("mousemove", (e) => this._hover(e));
    canvas.addEventListener("mouseleave", () => hideTip());
    new ResizeObserver(() => this.draw()).observe(canvas);
  }

  push(samples) {
    for (const s of samples) this.data.push(s);
    const tEnd = this.data.length ? this.data[this.data.length - 1][0] : 0;
    while (this.data.length && this.data[0][0] < tEnd - this.seconds - 1) this.data.shift();
    if (!this._raf) this._raf = requestAnimationFrame(() => { this._raf = null; this.draw(); });
  }

  clear() { this.data = []; this.draw(); }

  draw() {
    const { ctx, w, h } = setupCanvas(this.canvas);
    ctx.clearRect(0, 0, w, h);
    const mid = h / 2;
    ctx.strokeStyle = css("--grid"); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, Math.round(mid) + 0.5); ctx.lineTo(w, Math.round(mid) + 0.5); ctx.stroke();
    if (this.data.length < 2) return;
    const tEnd = this.data[this.data.length - 1][0];
    const X = (t) => w - (tEnd - t) / this.seconds * (w - 8) - 8;
    // robust autoscale
    const vals = this.data.map((d) => Math.abs(d[1])).sort((a, b) => a - b);
    const p95 = vals[Math.floor(vals.length * 0.95)] || 0.5;
    this.scale += ((p95 || 0.5) - this.scale) * 0.08;
    const Y = (v) => mid - (v / Math.max(this.scale, 0.05)) * (h * 0.38);

    // movement wash
    ctx.fillStyle = css("--motion-wash");
    for (let i = 1; i < this.data.length; i++) {
      if (this.data[i][2] >= this.restless) {
        const x0 = X(this.data[i - 1][0]), x1 = X(this.data[i][0]);
        ctx.fillRect(x0, 0, Math.max(1, x1 - x0 + 0.5), h);
      }
    }
    ctx.strokeStyle = css("--breath");
    ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.beginPath();
    this.data.forEach(([t, v], i) => (i ? ctx.lineTo(X(t), Y(v)) : ctx.moveTo(X(t), Y(v))));
    ctx.stroke();
    const [lt, lv] = this.data[this.data.length - 1];
    ctx.fillStyle = css("--surface"); ctx.beginPath(); ctx.arc(X(lt), Y(lv), 6, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = css("--breath"); ctx.beginPath(); ctx.arc(X(lt), Y(lv), 4, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = css("--text-muted");
    ctx.font = "11px system-ui, -apple-system, sans-serif";
    ctx.textBaseline = "bottom";
    ctx.textAlign = "left"; ctx.fillText(`−${this.seconds} s`, 2, h - 2);
    ctx.textAlign = "right"; ctx.fillText("now", w - 2, h - 2);
  }

  _hover(ev) {
    if (this.data.length < 2) return;
    const r = this.canvas.getBoundingClientRect();
    const w = r.width;
    const tEnd = this.data[this.data.length - 1][0];
    const t = tEnd - (w - 8 - (ev.clientX - r.left)) / (w - 8) * this.seconds;
    let best = this.data[0];
    for (const d of this.data) if (Math.abs(d[0] - t) < Math.abs(best[0] - t)) best = d;
    const ago = Math.max(0, tEnd - best[0]);
    const m = best[2];
    const state = m >= 5 ? "moving" : m >= this.restless ? "restless" : "still";
    showTip(`<div class="tip-x">${ago.toFixed(0)} s ago</div><div class="tip-row">Movement: <b>${state}</b> (${m.toFixed(1)}×)</div>`, ev.clientX, ev.clientY);
  }
}

// ------------------------------------------------------------ Sparkline

export function sparkline(canvas, values, colorVar, opts = {}) {
  const { ctx, w, h } = setupCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  const v = values.filter((x) => x != null);
  if (v.length < 2) return;
  const max = Math.max(opts.min ?? 0, ...v), min = opts.floor ?? Math.min(...v);
  const X = (i) => 2 + i / (values.length - 1) * (w - 8);
  const Y = (x) => h - 3 - (x - min) / (max - min || 1) * (h - 8);
  if (opts.band != null) {
    ctx.strokeStyle = css("--grid"); ctx.lineWidth = 1;
    const y = Math.round(Y(opts.band)) + 0.5;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  ctx.strokeStyle = css(colorVar); ctx.lineWidth = 2; ctx.lineJoin = "round";
  ctx.beginPath();
  let pen = false;
  values.forEach((x, i) => {
    if (x == null) { pen = false; return; }
    if (!pen) { ctx.moveTo(X(i), Y(x)); pen = true; } else ctx.lineTo(X(i), Y(x));
  });
  ctx.stroke();
}

// --------------------------------------------------------------- Bars

// Progress across sessions: one column per session.
export class BarChart {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.opts = Object.assign({ padLeft: 36, padBottom: 22, padTop: 10, padRight: 12 }, opts);
    this.items = [];
    canvas.addEventListener("mousemove", (e) => this._hover(e));
    canvas.addEventListener("mouseleave", () => { hideTip(); this.hi = null; this.draw(); });
    new ResizeObserver(() => this.draw()).observe(canvas);
  }
  set(items) { this.items = items; this.draw(); } // [{label, value, tip}]
  draw() {
    const { ctx, w, h } = setupCanvas(this.canvas);
    const o = this.opts;
    ctx.clearRect(0, 0, w, h);
    const vals = this.items.map((d) => d.value).filter((v) => v != null);
    const max = Math.max(o.yMax ?? 0, ...vals, 1);
    const ticks = niceTicks(0, max, 3);
    const top = ticks[ticks.length - 1] || max;
    const Y = (v) => o.padTop + (1 - v / top) * (h - o.padTop - o.padBottom);
    ctx.font = "11px system-ui, -apple-system, sans-serif";
    ctx.strokeStyle = css("--grid"); ctx.fillStyle = css("--text-muted"); ctx.lineWidth = 1;
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (const t of ticks) {
      const y = Math.round(Y(t)) + 0.5;
      ctx.beginPath(); ctx.moveTo(o.padLeft, y); ctx.lineTo(w - o.padRight, y); ctx.stroke();
      ctx.fillText(String(t), o.padLeft - 6, y);
    }
    const n = this.items.length || 1;
    const slot = (w - o.padLeft - o.padRight) / n;
    const bw = Math.max(2, Math.min(24, slot - 2));
    this._slot = slot;
    this.items.forEach((d, i) => {
      if (d.value == null) return;
      const x = o.padLeft + i * slot + (slot - bw) / 2;
      const y = Y(d.value), y0 = Y(0);
      const r = Math.max(0, Math.min(4, bw / 2, y0 - y));
      ctx.fillStyle = css(o.color || "--breath");
      ctx.globalAlpha = this.hi == null || this.hi === i ? 1 : 0.55;
      ctx.beginPath();
      ctx.moveTo(x, y0); ctx.lineTo(x, y + r);
      ctx.arcTo(x, y, x + r, y, r); ctx.lineTo(x + bw - r, y);
      ctx.arcTo(x + bw, y, x + bw, y + r, r); ctx.lineTo(x + bw, y0);
      ctx.closePath(); ctx.fill();
      ctx.globalAlpha = 1;
    });
    ctx.fillStyle = css("--text-muted"); ctx.textAlign = "center"; ctx.textBaseline = "top";
    const every = Math.ceil(n / Math.max(1, Math.floor((w - o.padLeft) / 60)));
    this.items.forEach((d, i) => { if (i % every === 0) ctx.fillText(d.label, o.padLeft + (i + 0.5) * slot, h - o.padBottom + 6); });
  }
  _hover(ev) {
    const r = this.canvas.getBoundingClientRect();
    const i = Math.floor((ev.clientX - r.left - this.opts.padLeft) / (this._slot || 1));
    const d = this.items[i];
    if (!d || d.value == null) { hideTip(); this.hi = null; this.draw(); return; }
    this.hi = i; this.draw();
    showTip(d.tip, ev.clientX, ev.clientY);
  }
}
