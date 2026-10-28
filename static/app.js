"use strict";

const POLL_MS = 1500;
const WINDOW_S = 600;
const GiB = 1024 ** 3;

const $ = (sel, root = document) => root.querySelector(sel);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const nf = d => new Intl.NumberFormat("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d });
const F0 = nf(0), F1 = nf(1);
const fmt = (v, f = F0) => (v == null || Number.isNaN(v)) ? "–" : f.format(v);
const gb = bytes => bytes == null ? "–" : fmt(bytes / GiB, bytes >= 100 * GiB ? F0 : F1) + " GB";
const gbMiB = mib => mib == null ? "–" : fmt(mib / 1024, F1) + " GB";
const clock = t => new Date(t * 1000).toLocaleTimeString("de-DE");

function duration(sec) {
  sec = Math.max(0, Math.round(sec));
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600);
  const m = Math.floor(sec % 3600 / 60), s = sec % 60;
  if (d) return `${d} T ${h} h`;
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min`;
  return `${s} s`;
}
const countdown = sec => {
  sec = Math.max(0, Math.round(sec));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
};

let samples = [];
let lastT = 0;
let state = null;
let serverOffset = 0;          // Serverzeit - Browserzeit, fuer Countdowns
let hoverFrac = null;          // Fadenkreuz als Anteil der Plotbreite, gilt fuer alle Diagramme
const busy = new Set();
const armed = new Map();       // Stopp-Knoepfe, die einen zweiten Klick erwarten
const logOpen = { ollama: false, comfy: false };
const now = () => Date.now() / 1000 + serverOffset;

function setHTML(el, html) {
  if (el && el._html !== html) { el._html = html; el.innerHTML = html; }
}

function toast(msg, ok = false) {
  const el = document.createElement("div");
  el.className = "toast" + (ok ? " ok" : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), ok ? 3500 : 9000);
}

// --- Kacheln ---------------------------------------------------------------

function vramParts(x) {
  const ol = x.vram_ollama || 0, co = x.vram_comfy || 0;
  return { ol, co, other: Math.max(0, (x.mem || 0) - ol - co) };
}

function renderTiles() {
  const x = samples.at(-1);
  if (!x) return;
  if (x.mem_total == null) {
    setHTML($("#tiles"), `<div class="hint">nvidia-smi liefert keine Werte.</div>`);
    return;
  }
  const p = vramParts(x);
  const pct = v => (100 * v / x.mem_total).toFixed(2) + "%";
  const tiles = [
    { k: "Auslastung", v: fmt(x.util), u: "%", d: `Takt ${fmt(x.clock)} / ${fmt(x.clock_max)} MHz` },
    {
      k: "VRAM", v: fmt(x.mem / 1024, F1), u: `/ ${fmt(x.mem_total / 1024)} GB`,
      extra: `<div class="bar" role="img" aria-label="VRAM: Ollama ${gbMiB(p.ol)}, ComfyUI ${gbMiB(p.co)}, sonst ${gbMiB(p.other)}">
        <span style="width:${pct(p.ol)};background:var(--s-ollama)"></span>
        <span style="width:${pct(p.co)};background:var(--s-comfy)"></span>
        <span style="width:${pct(p.other)};background:var(--s-other)"></span>
        <span class="free"></span></div>
        <div class="d">frei ${gbMiB(Math.max(0, x.mem_total - x.mem))}</div>`,
    },
    { k: "Temperatur", v: fmt(x.temp), u: "°C", d: `Lüfter ${fmt(x.fan)} %` },
    { k: "Leistung", v: fmt(x.power), u: "W", d: `Limit ${fmt(x.power_limit)} W` },
    { k: "CPU", v: fmt(x.cpu), u: "%", d: "Prozessor gesamt" },
    { k: "RAM", v: x.ram == null ? "–" : fmt(x.ram / GiB, F1), u: `/ ${x.ram_total ? fmt(x.ram_total / GiB) : "–"} GB`, d: "Arbeitsspeicher" },
  ];
  setHTML($("#tiles"), tiles.map(t => `
    <div class="tile">
      <div class="k">${esc(t.k)}</div>
      <div class="v">${esc(t.v)}<small>${esc(t.u)}</small></div>
      ${t.extra || `<div class="d">${esc(t.d)}</div>`}
    </div>`).join(""));
}

// --- Diagramme ---------------------------------------------------------------

const SVGNS = "http://www.w3.org/2000/svg";

const CHARTS = [
  {
    id: "chart-util",
    series: [{ label: "Auslastung", color: "var(--s-ollama)", get: x => x.util }],
    scale: () => ({ max: 100, ticks: [0, 50, 100] }),
    tick: v => `${v} %`,
    value: v => `${fmt(v)} %`,
  },
  {
    id: "chart-vram",
    stacked: true,
    series: [
      { label: "Ollama", color: "var(--s-ollama)", get: x => vramParts(x).ol },
      { label: "ComfyUI", color: "var(--s-comfy)", get: x => vramParts(x).co },
      { label: "Sonst", color: "var(--s-other)", get: x => vramParts(x).other },
    ],
    scale: () => {
      const total = samples.at(-1)?.mem_total || 24576;
      const step = Math.max(1, Math.ceil(total / 1024 / 4)) * 1024;
      return { max: step * 4, ticks: [0, step * 2, step * 4] };
    },
    tick: v => `${fmt(v / 1024)} GB`,
    value: v => gbMiB(v),
    now: x => x.mem,
  },
  {
    id: "chart-power",
    series: [{ label: "Leistung", color: "var(--s-comfy)", get: x => x.power }],
    scale: () => {
      const lim = samples.at(-1)?.power_limit || 450;
      return { max: lim, ticks: [0, lim / 2, lim] };
    },
    tick: v => `${fmt(v)} W`,
    value: v => `${fmt(v)} W`,
  },
];

function setupChart(cfg) {
  cfg.card = document.getElementById(cfg.id);
  cfg.plot = $(".plot", cfg.card);
  cfg.svg = document.createElementNS(SVGNS, "svg");
  cfg.svg.setAttribute("aria-hidden", "true");
  cfg.tip = document.createElement("div");
  cfg.tip.className = "tip";
  cfg.tip.hidden = true;
  cfg.plot.append(cfg.svg, cfg.tip);
  const move = e => {
    const r = cfg.plot.getBoundingClientRect();
    const L = cfg.left || 0, iw = r.width - L - 4;
    hoverFrac = Math.min(1, Math.max(0, (e.clientX - r.left - L) / iw));
    drawCharts();
  };
  cfg.plot.addEventListener("pointermove", move);
  cfg.plot.addEventListener("pointerdown", move);
  cfg.plot.addEventListener("pointerleave", () => { hoverFrac = null; drawCharts(); });
}

function nearest(t) {
  let lo = 0, hi = samples.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (samples[mid].t < t) lo = mid; else hi = mid;
  }
  return Math.abs(samples[lo].t - t) <= Math.abs(samples[hi].t - t) ? samples[lo] : samples[hi];
}

function drawChart(cfg) {
  const W = cfg.plot.clientWidth, H = cfg.plot.clientHeight;
  if (!W || !samples.length) return;
  const { max, ticks } = cfg.scale();
  const L = cfg.left = 44, R = 4, T = 6, B = 18;
  const iw = W - L - R, ih = H - T - B;
  const t1 = samples.at(-1).t, t0 = t1 - WINDOW_S;
  const X = t => L + (t - t0) / WINDOW_S * iw;
  const Y = v => T + ih - Math.min(max, Math.max(0, v)) / max * ih;
  const parts = [];

  for (const v of ticks) {
    const y = Y(v).toFixed(1);
    parts.push(`<line class="${v === 0 ? "baseline" : "gridline"}" x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/>`);
    parts.push(`<text class="tick" x="${L - 6}" y="${y}" text-anchor="end" dominant-baseline="middle">${esc(cfg.tick(v))}</text>`);
  }
  for (const [ago, label] of [[600, "−10 min"], [300, "−5 min"], [0, "jetzt"]]) {
    const x = X(t1 - ago);
    const anchor = ago === 600 ? "start" : ago === 0 ? "end" : "middle";
    parts.push(`<text class="tick" x="${x.toFixed(1)}" y="${H - 3}" text-anchor="${anchor}">${label}</text>`);
  }

  // Luecken (Leitstand war weg) unterbrechen die Linien statt sie zu verbinden
  const segments = [];
  let seg = [];
  for (const s of samples) {
    if (seg.length && s.t - seg.at(-1).t > 4) { segments.push(seg); seg = []; }
    seg.push(s);
  }
  if (seg.length) segments.push(seg);

  const n = cfg.series.length;
  for (const sg of segments) {
    const base = sg.map(() => 0);
    const tops = [];
    for (let i = 0; i < n; i++) {
      const top = sg.map((s, j) => (cfg.stacked ? base[j] : 0) + (cfg.series[i].get(s) || 0));
      const lower = cfg.stacked ? base.slice() : sg.map(() => 0);
      const up = sg.map((s, j) => `${X(s.t).toFixed(1)},${Y(top[j]).toFixed(1)}`);
      const down = sg.map((s, j) => `${X(s.t).toFixed(1)},${Y(lower[j]).toFixed(1)}`).reverse();
      const color = cfg.series[i].color;
      if (cfg.stacked) {
        parts.push(`<path d="M${up.join("L")}L${down.join("L")}Z" fill="${color}" fill-opacity="0.9"/>`);
        for (let j = 0; j < sg.length; j++) base[j] = top[j];
      } else {
        parts.push(`<path d="M${up.join("L")}L${down.join("L")}Z" fill="${color}" fill-opacity="0.16"/>`);
        parts.push(`<path d="M${up.join("L")}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`);
      }
      tops.push(up);
    }
    if (cfg.stacked) {  // 2px Flaechenabstand zwischen den Schichten
      for (let i = 0; i < n - 1; i++) {
        parts.push(`<path d="M${tops[i].join("L")}" fill="none" stroke="var(--surface-2)" stroke-width="2" stroke-linejoin="round"/>`);
      }
    }
  }

  const tip = cfg.tip;
  if (hoverFrac != null) {
    const s = nearest(t0 + hoverFrac * WINDOW_S);
    const x = X(s.t);
    parts.push(`<line class="cross" x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${T}" y2="${T + ih}"/>`);
    let acc = 0;
    const rows = [];
    for (const sr of cfg.series) {
      const v = sr.get(s) || 0;
      acc = cfg.stacked ? acc + v : v;
      parts.push(`<circle cx="${x.toFixed(1)}" cy="${Y(acc).toFixed(1)}" r="4" fill="${sr.color}" stroke="var(--surface-2)" stroke-width="2"/>`);
      rows.push(`<div class="row"><i style="background:${sr.color}"></i><b>${esc(cfg.value(v))}</b> ${esc(sr.label)}</div>`);
    }
    if (cfg.stacked) rows.push(`<div class="row"><i></i><b>${esc(cfg.value(acc))}</b> gesamt</div>`);
    if (cfg.stacked) rows.reverse();  // oberste Schicht zuerst, wie im Diagramm
    tip.innerHTML = `<div class="t">${esc(clock(s.t))}</div>${rows.join("")}`;
    tip.hidden = false;
    const tw = tip.offsetWidth;
    tip.style.left = (x + 12 + tw > W ? x - 12 - tw : x + 12) + "px";
  } else {
    tip.hidden = true;
  }
  cfg.svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  cfg.svg.innerHTML = parts.join("");

  const last = samples.at(-1);
  const nowEl = $("[data-now]", cfg.card);
  if (nowEl) nowEl.textContent = cfg.value(cfg.now ? cfg.now(last) : cfg.series[0].get(last));
  const legend = $("[data-legend]", cfg.card);
  if (legend) {
    setHTML(legend, cfg.series.map(sr =>
      `<span class="key"><i style="background:${sr.color}"></i>${esc(sr.label)}</span>`).join(""));
  }
}

function drawCharts() { CHARTS.forEach(drawChart); }

function renderTable() {
  const det = $("#table-view");
  if (!det.open || !samples.length) return;
  const rows = [];
  const end = samples.at(-1).t;
  for (let b = 0; b < 20; b++) {
    const hi = end - b * 30, lo = hi - 30;
    const group = samples.filter(s => s.t > lo && s.t <= hi);
    if (!group.length) continue;
    const avg = f => group.reduce((a, s) => a + (f(s) || 0), 0) / group.length;
    rows.push(`<tr><td>${esc(clock(hi))}</td><td>${fmt(avg(s => s.util))} %</td>
      <td>${gbMiB(avg(s => vramParts(s).ol))}</td><td>${gbMiB(avg(s => vramParts(s).co))}</td>
      <td>${gbMiB(avg(s => vramParts(s).other))}</td><td>${fmt(avg(s => s.power))} W</td><td>${fmt(avg(s => s.temp))} °C</td></tr>`);
  }
  setHTML($("tbody", det), rows.join(""));
}

// --- Dienste -----------------------------------------------------------------

function status(svc) {
  const s = svc.service, up = svc.up;
  if (!s.exists) return up ? ["warn", "läuft ohne Dienst"] : ["", "kein Dienst eingerichtet"];
  switch (s.active) {
    case "active":
      if (up) return ["good", "läuft"];
      return (s.since && now() - s.since > 90) ? ["crit", "antwortet nicht"] : ["warn pulse", "startet…"];
    case "activating": return ["warn pulse", "startet…"];
    case "deactivating": return ["warn pulse", "stoppt…"];
    case "failed": return ["crit", "Fehler"];
    default: return up ? ["warn", "läuft außerhalb des Dienstes"] : ["", "gestoppt"];
  }
}

function button(act, label, opts = {}) {
  const key = [act, opts.svc || "", opts.model || ""].join("|");
  const isBusy = busy.has(key), isArmed = armed.has(key);
  const cls = [opts.cls || "", opts.small ? "small" : "", isArmed ? "confirm" : ""].join(" ").trim();
  const text = isArmed ? (opts.confirm || "Sicher?") : label;
  const attrs = [`data-act="${esc(act)}"`, `data-key="${esc(key)}"`];
  if (opts.svc) attrs.push(`data-svc="${esc(opts.svc)}"`);
  if (opts.model) attrs.push(`data-model="${esc(opts.model)}"`);
  if (opts.confirm) attrs.push(`data-confirm="1"`);
  // solange ein Dienst startet/stoppt, sind alle seine Dienst-Knoepfe gesperrt
  const svcBusy = act === "svc" && [...busy].some(k => k.startsWith(`svc|${opts.svc.split("/")[0]}/`));
  const disabled = opts.disabled || isBusy || svcBusy;
  return `<button type="button" class="${cls}" ${attrs.join(" ")} ${disabled ? "disabled" : ""}>${isBusy ? '<span class="spin"></span>' : ""}${esc(text)}</button>`;
}

function serviceActions(key, svc) {
  const s = svc.service;
  const active = ["active", "activating", "reloading"].includes(s.active);
  const out = [];
  if (s.exists) {
    out.push(button("svc", "Starten", { svc: key + "/start", cls: "primary", disabled: active }));
    out.push(button("svc", "Stoppen", { svc: key + "/stop", cls: "danger", disabled: !active, confirm: "Wirklich stoppen?" }));
    out.push(button("svc", "Neu starten", { svc: key + "/restart", disabled: !active }));
  }
  return out;
}

function serviceMeta(svc, port) {
  const s = svc.service, bits = [];
  if (svc.version) bits.push(`<span>v${esc(svc.version)}</span>`);
  if (s.exists && s.since) {
    const word = ["active", "activating"].includes(s.active) ? "seit" : "gestoppt seit";
    bits.push(`<span>${word} <span data-since="${s.since}"></span></span>`);
  }
  bits.push(`<span>Port ${esc(port)}</span>`);
  if (s.exists) bits.push(`<span>Autostart ${s.enabled === "enabled" ? "an" : "aus"}</span>`);
  if (s.exists && s.active === "failed" && s.result) bits.push(`<span class="warnline">Letzter Lauf endete mit „${esc(s.result)}“. Siehe Log.</span>`);
  if (!s.exists) bits.push(`<span class="warnline">${esc(s.unit)} ist nicht installiert. Siehe install.sh.</span>`);
  if (!s.exists || s.active === "inactive") {
    if (svc.up) bits.push(`<span class="warnline">Die API antwortet, aber nicht aus dem Dienst heraus: Stoppen geht nur dort, wo sie gestartet wurde.</span>`);
  }
  return bits.join("");
}

function renderService(key, svc, port, extraActions = [], extraMeta = "") {
  const root = document.getElementById("svc-" + key);
  const [cls, label] = status(svc);
  setHTML($("[data-status]", root), `<span class="dot ${cls}"></span>${esc(label)}`);
  setHTML($("[data-meta]", root), serviceMeta(svc, port) + extraMeta);
  const actions = [...serviceActions(key, svc), ...extraActions,
    button("log", logOpen[key] ? "Log ausblenden" : "Log", { svc: key })];
  setHTML($("[data-actions]", root), actions.join(""));
  return root;
}

const portOf = url => { try { return new URL(url).port || "80"; } catch { return "?"; } };

function renderOllama(o) {
  const root = renderService("ollama", o, portOf(o.url));
  const running = new Set(o.running.map(m => m.name));
  let html;
  if (!o.up) html = `<div class="empty">Ollama läuft nicht.</div>`;
  else if (!o.running.length) html = `<div class="empty">Kein Modell geladen.</div>`;
  else html = o.running.map(m => {
    const gpu = m.size ? Math.round(100 * (m.vram || 0) / m.size) : 0;
    const split = gpu >= 100 ? "100 % GPU" : `${gpu} % GPU / ${100 - gpu} % CPU`;
    const exp = m.expires ? Date.parse(m.expires) / 1000 : null;
    const until = exp == null ? "" : (exp - now() > 365 * 86400)
      ? "<span>bleibt geladen</span>"
      : `<span>entlädt in <span class="tabnum" data-until="${exp}"></span></span>`;
    return `<div class="item">
      <div class="name">${esc(m.name)}</div>
      <div class="side">${button("ollama-unload", "Entladen", { model: m.name, small: true })}</div>
      <div class="info"><span>${gb(m.size)}</span><span>${split}</span>${m.context ? `<span>Kontext ${fmt(m.context)}</span>` : ""}${until}</div>
      <div class="bar" role="img" aria-label="${esc(split)}"><span style="width:${gpu}%;background:var(--s-ollama)"></span><span style="width:${100 - gpu}%;background:var(--s-other)"></span></div>
    </div>`;
  }).join("");
  setHTML($("[data-running]", root), html);

  $("[data-installed-count]", root).textContent = o.up ? `(${o.installed.length})` : "";
  setHTML($("[data-installed]", root), !o.up ? `<div class="empty">Ollama läuft nicht.</div>`
    : !o.installed.length ? `<div class="empty">Keine Modelle installiert.</div>`
    : o.installed.map(m => `<div class="item">
        <div class="name">${esc(m.name)}</div>
        <div class="side">${running.has(m.name)
          ? `<button type="button" class="small" disabled>geladen</button>`
          : button("ollama-load", "Laden", { model: m.name, small: true })}</div>
        <div class="info">${[m.params, m.quant, gb(m.size), m.family].filter(Boolean).map(v => `<span>${esc(v)}</span>`).join("")}</div>
      </div>`).join(""));
}

const KIND = {
  ckpt_name: "Checkpoint", unet_name: "Diffusionsmodell", vae_name: "VAE", lora_name: "LoRA",
  control_net_name: "ControlNet", clip_name: "Text-Encoder", clip_name1: "Text-Encoder",
  clip_name2: "Text-Encoder", clip_name3: "Text-Encoder", clip_name4: "Text-Encoder",
  model_name: "Modell", style_model_name: "Style-Modell", gligen_name: "GLIGEN",
};

// ComfyUI meldet nur die Modellklasse; die Art leitet sich aus ihr ab
function comfyKind(cls) {
  if (/AutoencoderKL|AutoencodingEngine|VAE/i.test(cls)) return "VAE";
  if (/TEModel|Clip|T5|TextEncoder|Llama|Qwen|Gemma|Mistral|Umt5/i.test(cls)) return "Text-Encoder";
  if (/ControlNet|ControlLora/i.test(cls)) return "ControlNet";
  if (/Upscale|ESRGAN|RRDB/i.test(cls)) return "Upscaler";
  return "Diffusionsmodell";
}

// Dateiname aus dem letzten Workflow, aber nur wenn die Zuordnung eindeutig ist:
// genau ein geladenes Modell dieser Art und genau eine passende Datei
function workflowFile(wf, kind, loadedOfKind) {
  if (!wf || loadedOfKind !== 1) return null;
  const same = wf.models.filter(m => KIND[m.input] === kind);
  if (same.length === 1) return same[0].file;
  const ckpt = wf.models.filter(m => m.input === "ckpt_name");
  return !same.length && ckpt.length === 1 ? ckpt[0].file : null;
}

// Die Auswahl des Servers; nur neu gezeichnet, wenn sich die Liste aendert
// (setHTML vergleicht), damit eine offene Auswahl nicht zuklappt.
function renderServers(c) {
  const root = document.getElementById("svc-comfy");
  const opts = (c.servers || []).map(s =>
    `<option value="${esc(s.id)}"${s.id === c.server?.id ? " selected" : ""}>${esc(s.name)}${s.local ? "" : ` · ${esc(s.url)}`}</option>`).join("");
  setHTML($("[data-servers]", root),
    `<label>Server <select data-server-select>${opts}<option value="+">+ Server hinzufügen…</option></select></label>` +
    (c.server && !c.server.local
      ? button("comfy-server-delete", "Entfernen", { model: c.server.id, small: true, confirm: "Wirklich entfernen?" }) : ""));
}

// Ein entfernter Server: kein Dienst hier, also Status, Adresse und was die API sagt.
function renderRemoteComfy(c, extra, queue) {
  const root = document.getElementById("svc-comfy");
  setHTML($("[data-status]", root), c.up ? `<span class="dot good"></span>läuft` : `<span class="dot crit"></span>antwortet nicht`);
  const bits = [c.version && `<span>v${esc(c.version)}</span>`, `<span>${esc(c.url)}</span>`,
    c.devices?.[0]?.name && `<span>${esc(c.devices[0].name.replace(/^cuda:\d+\s*/, "").replace(/\s*:\s*cudaMallocAsync$/, ""))}</span>`, queue,
    !c.up && `<span class="warnline">Läuft ComfyUI dort mit <code>--listen</code>, und ist der Port frei?</span>`];
  setHTML($("[data-meta]", root), bits.filter(Boolean).join(""));
  setHTML($("[data-actions]", root), extra.join(""));
  $("[data-log]", root).hidden = true;
  return root;
}

function renderComfy(c) {
  renderServers(c);
  const port = portOf(c.url);
  const remote = c.server && !c.server.local;
  if (remote) logOpen.comfy = false;   // das Log ist das des eigenen Dienstes
  const extra = [button("comfy-free", "Speicher freigeben", { disabled: !c.up })];
  // Der eigene: unter der Adresse, unter der der Leitstand offen ist (auch aus dem LAN).
  const open = remote ? c.url : `${location.protocol}//${location.hostname}:${port}`;
  if (c.up) extra.push(`<a class="btn" href="${esc(open + "/")}" target="_blank" rel="noopener">ComfyUI öffnen ↗</a>`);
  const queue = c.up && c.queue
    ? `<span>Warteschlange: ${c.queue.running ? "1 läuft" : "leer"}${c.queue.pending ? `, ${c.queue.pending} wartend` : ""}</span>` : "";
  const root = remote ? renderRemoteComfy(c, extra, queue) : renderService("comfy", c, port, extra, queue);

  const wf = c.workflow;
  let html;
  if (!c.up) html = `<div class="empty">ComfyUI läuft nicht.</div>`;
  else if (c.loaded == null) html = remote
    ? `<div class="hint">Auf diesem Server fehlt der Leitstand-Node: <code>comfy_node/leitstand_status.py</code> in dessen <code>custom_nodes</code> legen und ComfyUI neu starten.</div>`
    : `<div class="hint">Der Leitstand-Node fehlt in ComfyUI. <code>install.sh</code> verlinkt ihn nach <code>custom_nodes</code>, danach ComfyUI neu starten.</div>`;
  else if (!c.loaded.length) html = `<div class="empty">Keine Modelle im Speicher.</div>`;
  else html = c.loaded.map(m => {
    const kind = comfyKind(m.class);
    const file = workflowFile(wf, kind, c.loaded.filter(o => comfyKind(o.class) === kind).length);
    const share = m.size ? Math.min(100, Math.round(100 * (m.loaded || 0) / m.size)) : 0;
    const where = String(m.device || "").startsWith("cuda") ? `${gb(Math.min(m.loaded, m.size))} von ${gb(m.size)} auf der GPU` : `${gb(m.size)} auf ${esc(m.device)}`;
    return `<div class="item">
      <div class="name">${esc(file || m.class)}</div>
      <div class="info"><span>${esc(kind)}</span>${file ? `<span>${esc(m.class)}</span>` : ""}<span>${where}</span><span>${m.in_use ? "in Benutzung" : "zwischengespeichert"}</span></div>
      <div class="bar" role="img" aria-label="${share} % auf der GPU"><span style="width:${share}%;background:var(--s-comfy)"></span><span class="free"></span></div>
    </div>`;
  }).join("");
  setHTML($("[data-loaded]", root), html);

  $("[data-workflow-title]", root).textContent = wf && wf.state === "läuft" ? "Laufender Workflow" : "Letzter Workflow";
  setHTML($("[data-workflow]", root), !c.up ? `<div class="empty">–</div>`
    : !wf ? `<div class="empty">Seit dem Start lief noch kein Workflow.</div>`
    : !wf.models.length ? `<div class="empty">Der Workflow lädt keine Modelldateien.</div>`
    : wf.models.map(m => `<div class="item">
        <div class="name">${esc(m.file)}</div>
        <div class="info"><span>${esc(KIND[m.input] || m.input)}</span><span>${esc(m.node)}</span>${wf.state !== "läuft" && wf.state !== "fertig" ? `<span>Status: ${esc(wf.state)}</span>` : ""}</div>
      </div>`).join(""));
}

// --- Logs --------------------------------------------------------------------

async function refreshLog(key) {
  const root = document.getElementById("svc-" + key);
  const box = $("[data-log]", root), pre = $("pre", box);
  box.hidden = !logOpen[key];
  if (!logOpen[key]) return;
  try {
    const r = await fetch(`/api/logs/${key}?n=200`, { cache: "no-store" });
    const j = await r.json();
    const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
    const plain = s => s.replace(/\x1b\[[0-9;]*[A-Za-z]/g, "");  // ComfyUI loggt mit Farbcodes
    pre.textContent = j.error ? j.error : (j.lines.map(l => `${clock(l.t)}  ${plain(l.msg)}`).join("\n") || "(leer)");
    if (stick) pre.scrollTop = pre.scrollHeight;
  } catch (e) {
    pre.textContent = "Log nicht abrufbar: " + e;
  }
}

// --- Aktionen ----------------------------------------------------------------

async function post(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Leitstand": "1" },
    body: JSON.stringify(body || {}),
  });
  let j = {};
  try { j = await r.json(); } catch { /* leerer Body */ }
  if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

async function act(btn) {
  const { act: kind, key, svc, model } = btn.dataset;
  if (kind === "log") {
    logOpen[svc] = !logOpen[svc];
    refreshLog(svc);
    render();
    return;
  }
  if (btn.dataset.confirm && !armed.has(key)) {
    armed.set(key, setTimeout(() => { armed.delete(key); render(); }, 4000));
    render();
    return;
  }
  clearTimeout(armed.get(key));
  armed.delete(key);
  busy.add(key);
  render();
  try {
    if (kind === "svc") {
      await post(`/api/service/${svc}`);
    } else if (kind === "ollama-load") {
      await post("/api/ollama/load", { model });
    } else if (kind === "ollama-unload") {
      await post("/api/ollama/unload", { model });
    } else if (kind === "comfy-free") {
      await post("/api/comfy/free");
      toast("ComfyUI hat Modelle und Cache freigegeben.", true);
    } else if (kind === "comfy-server-delete") {
      await post(`/api/comfy/servers/${model}/delete`);
      toast("Server entfernt, es gilt wieder der eigene.", true);
    }
  } catch (e) {
    const what = { svc: `Dienst ${svc}`, "ollama-load": `Laden von ${model}`, "ollama-unload": `Entladen von ${model}`,
      "comfy-free": "Speicher freigeben", "comfy-server-delete": "Entfernen des Servers" }[kind];
    toast(`${what} fehlgeschlagen:\n${e.message}`);
  } finally {
    busy.delete(key);
    await poll();
  }
}

document.addEventListener("click", e => {
  const btn = e.target.closest("button[data-act]");
  if (btn && !btn.disabled) act(btn);
});

// --- ComfyUI-Server ----------------------------------------------------------

const serverForm = $("[data-server-add]");

async function selectServer(id, quiet = false) {
  try {
    const j = await post(`/api/comfy/servers/${id}/select`);
    if (!quiet) toast(`ComfyUI-Server: ${j.servers.find(s => s.id === id)?.name}`, true);
  } catch (e) {
    toast(`Umschalten fehlgeschlagen:\n${e.message}`);
  }
  await poll();
}

document.addEventListener("change", e => {
  const sel = e.target.closest("[data-server-select]");
  if (!sel) return;
  if (sel.value === "+") {
    sel.value = state?.comfy?.server?.id || "local";
    serverForm.hidden = false;
    serverForm.elements.name.focus();
    return;
  }
  selectServer(sel.value);
});

serverForm.addEventListener("submit", async e => {
  e.preventDefault();
  const f = serverForm.elements;
  try {
    const j = await post("/api/comfy/servers", { name: f.name.value, url: f.url.value });
    serverForm.hidden = true;
    serverForm.reset();
    toast(`${j.added.name} hinzugefügt${j.reachable ? "" : " -- antwortet gerade nicht"}.`, j.reachable);
    await selectServer(j.added.id, true);
  } catch (err) {
    toast(`Hinzufügen fehlgeschlagen:\n${err.message}`);
  }
});
$("[data-server-add-cancel]").addEventListener("click", () => { serverForm.hidden = true; serverForm.reset(); });

// --- Takt --------------------------------------------------------------------

function render() {
  if (!state) return;
  $("#host").textContent = state.host;
  document.title = `Leitstand · ${state.host}`;
  $("#gpu-name").textContent = state.gpu.name || "";
  renderTiles();
  drawCharts();
  renderTable();
  renderOllama(state.ollama);
  renderComfy(state.comfy);
  tick();
}

function tick() {
  const t = now();
  document.querySelectorAll("[data-since]").forEach(el => { el.textContent = duration(t - Number(el.dataset.since)); });
  document.querySelectorAll("[data-until]").forEach(el => { el.textContent = countdown(Number(el.dataset.until) - t); });
}

function setConn(ok) {
  setHTML($("#conn"), ok ? `<span class="dot good"></span><span class="label">live</span>`
    : `<span class="dot crit"></span><span class="label">keine Verbindung</span>`);
}

let timer = null;
async function poll() {
  clearTimeout(timer);
  try {
    const r = await fetch(`/api/state?since=${lastT}`, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const s = await r.json();
    serverOffset = s.time - Date.now() / 1000;
    for (const x of s.gpu.samples) samples.push(x);
    if (samples.length) lastT = samples.at(-1).t;
    const cutoff = s.time - WINDOW_S;
    while (samples.length && samples[0].t < cutoff) samples.shift();
    state = s;
    setConn(true);
    render();
  } catch (e) {
    setConn(false);
  }
  timer = setTimeout(poll, POLL_MS);
}

CHARTS.forEach(setupChart);
new ResizeObserver(() => drawCharts()).observe($("main"));
$("#table-view").addEventListener("toggle", renderTable);
setInterval(tick, 1000);
setInterval(() => { refreshLog("ollama"); refreshLog("comfy"); }, 3000);
poll();
