/* GW Detector front-end */
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 2) => (v === null || v === undefined || !isFinite(v) ? "—" : Number(v).toFixed(d));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const DET_COLOR = { H1: "--c2", L1: "--c1", V1: "--c5" };
const DET_NAME = { H1: "LIGO Hanford", L1: "LIGO Livingston", V1: "Virgo" };
const state = { events: [], sort: "snr", q: "", result: null, job: null };

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 3500);
}
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}
function show(v) { for (const id of ["emptyView", "runView", "resultView"]) $("#" + id).classList.toggle("hidden", id !== v); }
const gpsToDate = (g) => new Date((g + 315964800 - 18) * 1000); // GPS epoch, minus leap seconds since 1980

/* ---------- event list ---------- */
function renderEvents() {
  const q = state.q.toLowerCase();
  let ev = state.events.filter((e) => !q || `${e.name} ${e.catalog} ${gpsToDate(e.gps).getUTCFullYear()}`.toLowerCase().includes(q));
  const key = { snr: (e) => -(e.snr || 0), date: (e) => -e.gps, mass: (e) => -((e.m1 || 0) + (e.m2 || 0)) }[state.sort];
  ev = ev.sort((a, b) => key(a) - key(b)).slice(0, 200);
  $("#evList").innerHTML = ev.map((e) => `
    <div class="ev" data-n="${esc(e.name)}">
      <b>${esc(e.name)}</b><span class="r">${e.snr ? "SNR " + fmt(e.snr, 1) : ""}</span>
      <span>${e.m1 ? `${fmt(e.m1, 0)} + ${fmt(e.m2, 1)} M☉` : "masses n/a"}${e.distance ? ` · ${fmt(e.distance, 0)} Mpc` : ""}</span>
      <span class="r">${gpsToDate(e.gps).toISOString().slice(0, 10)}</span>
    </div>`).join("") || `<p class="muted">No match.</p>`;
}
$("#evList").onclick = (e) => { const it = e.target.closest("[data-n]"); if (it) submit(it.dataset.n); };
$("#evSearch").oninput = (e) => { state.q = e.target.value; renderEvents(); };
$("#evSort").onclick = (e) => {
  const s = e.target.dataset.s; if (!s) return;
  state.sort = s; document.querySelectorAll("#evSort .tab").forEach((t) => t.classList.toggle("on", t.dataset.s === s)); renderEvents();
};
$("#gpsForm").onsubmit = (e) => { e.preventDefault(); const v = $("#gpsInput").value.trim(); if (v) submit(v); };

/* ---------- jobs ---------- */
async function submit(target) {
  // reuse a finished analysis if one exists
  const prev = (await api("/api/results")).find((r) => r.name === target);
  if (prev) { openResult(prev.file); return; }
  const j = await api("/api/jobs", { method: "POST", body: JSON.stringify({ target }) });
  state.job = j.id; show("runView"); poll();
}
async function poll() {
  const id = state.job; if (!id) return;
  const j = await api(`/api/jobs/${id}`);
  if (state.job !== id) return;
  $("#runTitle").textContent = j.target;
  $("#runBar").style.width = j.pct + "%";
  const log = $("#runLog"); log.textContent = j.status === "queued" ? "Waiting in queue…" : j.log.join("\n"); log.scrollTop = log.scrollHeight;
  if (j.status === "done") { state.job = null; refreshResults(); openResult(j.file); return; }
  if (j.status === "error") { state.job = null; toast(j.error); return; }
  setTimeout(poll, 1000);
}
async function refreshResults() {
  const rows = await api("/api/results").catch(() => []);
  const jobs = await api("/api/jobs").catch(() => []);
  const running = jobs.filter((j) => ["queued", "running"].includes(j.status));
  $("#queuePill").textContent = running.length ? `${running.length} running` : "Idle";
  $("#queuePill").classList.toggle("busy", running.length > 0);
  $("#resList").innerHTML = [
    ...running.map((j) => `<div class="res" data-job="${j.id}"><b>${esc(j.target)}</b><span class="t-run">${Math.round(j.pct)}%</span></div>`),
    ...rows.map((r) => `<div class="res ${state.result?.file === r.file ? "active" : ""}" data-file="${esc(r.file)}"><b>${esc(r.name)}</b><span class="t-${r.tone}">${esc(r.label)}</span></div>`),
  ].join("") || `<p class="muted">None yet.</p>`;
}
$("#resList").onclick = (e) => {
  const f = e.target.closest("[data-file]"); if (f) return openResult(f.dataset.file);
  const j = e.target.closest("[data-job]"); if (j) { state.job = j.dataset.job; show("runView"); poll(); }
};

/* ---------- plots ---------- */
const plotCfg = { displaylogo: false, responsive: true, displayModeBar: false };
function L(extra = {}) {
  return {
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", showlegend: false, hovermode: "closest",
    font: { color: css("--muted"), family: "IBM Plex Mono", size: 11 }, margin: { l: 50, r: 10, t: 8, b: 36 },
    ...extra,
    xaxis: { gridcolor: css("--grid"), zerolinecolor: css("--zero"), linecolor: css("--line-2"), ...(extra.xaxis || {}) },
    yaxis: { gridcolor: css("--grid"), zerolinecolor: css("--zero"), linecolor: css("--line-2"), ...(extra.yaxis || {}) },
  };
}

async function openResult(file) {
  const r = await api(`/api/results/${encodeURIComponent(file)}`);
  r.file = file; state.result = r;
  history.replaceState(null, "", "#r=" + encodeURIComponent(file));
  show("resultView"); render(); refreshResults();
}

function render() {
  const r = state.result, b = r.best, c = r.catalog, v = r.verdict, bg = r.background, pe = r.pe;
  const date = gpsToDate(b.t_peak);
  const zc = c?.redshift;
  const cmpRows = [
    ["Merger time (GPS)", fmt(b.t_peak, 3), c ? fmt(c.gps, 1) : "—"],
    ["Chirp mass, detector frame", `${fmt(pe.mc, 1)} M☉ (${fmt(pe.mc_lo, 1)}–${fmt(pe.mc_hi, 1)})`, c?.mchirp && zc != null ? `${fmt(c.mchirp * (1 + zc), 1)} M☉` : "—"],
    ["Chirp mass, source frame", pe.mc_src ? `${fmt(pe.mc_src, 1)} M☉` : "—", c?.mchirp ? `${fmt(c.mchirp, 1)} M☉` : "—"],
    ["Component masses (best template)", `${fmt(b.m1, 0)} + ${fmt(b.m2, 0)} M☉ (detector frame)`, c?.m1 ? `${fmt(c.m1, 1)} + ${fmt(c.m2, 1)} M☉ (source)` : "—"],
    ["Network SNR", fmt(b.net_snr, 1), c?.snr ? fmt(c.snr, 1) : "—"],
    ["Distance", r.per_detector.length ? `D_eff ${r.per_detector.map((p) => `${p.det} ${fmt(p.deff_mpc, 0)}`).join(", ")} Mpc` : "—", c?.distance ? `${fmt(c.distance, 0)} Mpc` : "—"],
  ];
  $("#resultView").innerHTML = `
  <div class="result">
    <div class="verdict tone-${v.tone}">
      <div>
        <div class="eyebrow">${esc(r.name)} · ${date.toISOString().slice(0, 19).replace("T", " ")} UTC · ${r.detectors.join(" + ")}</div>
        <h3>${esc(v.label)}</h3>
        <p>${esc(v.text)}</p>
        <p class="sub">${c ? `Catalogued in ${esc(c.catalog)}. ` : ""}Analysis took ${fmt(r.runtime_s, 0)} s.</p>
      </div>
      <div class="verdict-actions"><button class="btn small" id="dlJson">Download JSON</button>
        ${c ? `<a class="btn small" target="_blank" rel="noopener" href="https://gwosc.org/eventapi/html/${esc(c.catalog)}/${esc(r.name)}/">GWOSC ↗</a>` : ""}</div>
    </div>

    <div class="metrics">
      ${m("Network SNR", fmt(b.net_snr, 1), `χ²-weighted ${fmt(b.net_rw, 1)}`)}
      ${m("False-alarm prob.", bg.fap == null ? "—" : bg.louder === 0 ? `< ${fmt(bg.fap, 4)}` : fmt(bg.fap, 3), bg.slides ? `${bg.louder} of ${bg.slides.length} slides louder` : "needs 2 detectors")}
      ${m("Chirp mass (det.)", `${fmt(pe.mc, 1)} M☉`, `${fmt(pe.mc_lo, 1)}–${fmt(pe.mc_hi, 1)}`)}
      ${m("Chirp mass (source)", pe.mc_src ? `${fmt(pe.mc_src, 1)} M☉` : "—", c?.mchirp ? `published ${fmt(c.mchirp, 1)}` : "needs redshift")}
      ${m("Best template", `${fmt(b.m1, 0)}+${fmt(b.m2, 0)} M☉`, "detector frame")}
      ${m("Arrival times", r.per_detector.slice(1).map((p) => `${p.dt_ms >= 0 ? "+" : ""}${fmt(p.dt_ms, 1)} ms`).join(" / ") || "—", r.per_detector.slice(1).map((p) => `${p.det} vs ${r.per_detector[0].det}`).join(" / ") || "one detector")}
    </div>

    <div class="det-grid">${r.per_detector.map((p, i) => `
      <div class="card">
        <div class="det-head"><h3 style="color:${css(DET_COLOR[p.det])}">${DET_NAME[p.det] || p.det}</h3>
          <span class="stats">SNR ${fmt(p.snr, 1)} · χ²/dof ${fmt(p.chi_r, 1)} (≤ ${fmt(2.5 * p.chi_allow, 1)} ok)</span></div>
        <div class="label">Whitened strain and best-fit waveform</div>
        <div id="w${i}" class="plot w"></div>
        <div class="audio"><button class="btn small" data-play="${i}" data-kind="data">▶ Data</button><button class="btn small" data-play="${i}" data-kind="model">▶ Template</button>
          <label><input type="checkbox" class="pitch"> shift pitch up ×4 (for small speakers)</label></div>
        <div class="label" style="margin-top:12px">Time–frequency (constant-Q)</div>
        <div id="q${i}" class="plot q"></div>
        <div class="label" style="margin-top:12px">Matched-filter SNR</div>
        <div id="s${i}" class="plot s"></div>
        ${p.gates.length ? `<p class="muted" style="margin:6px 0 0">Gated ${p.gates.length} glitch(es): ${p.gates.map((g) => `${fmt(g.t_rel, 2)} s (${fmt(g.peak_sigma, 0)}σ)`).join(", ")}</p>` : ""}
      </div>`).join("")}
    </div>

    <div class="grid-2">
      <div class="card"><div class="panel-title"><h3>Comparison with the published analysis</h3></div>
        <table class="kv cmp"><tr><th></th><th>This search</th><th>Published</th></tr>
        ${cmpRows.map((x) => `<tr><td>${x[0]}</td><td>${x[1]}</td><td>${x[2]}</td></tr>`).join("")}</table>
        <p class="muted" style="font-size:12px">Waveforms here are non-spinning post-Newtonian inspirals with an approximate merger, so the SNR is lower
          than the published value, and masses can differ by 5–15%.</p></div>
      <div class="card"><div class="panel-title"><h3>Background (time slides)</h3><span>max network SNR when detectors are shifted out of coincidence</span></div>
        <div id="bgPlot" class="plot"></div></div>
    </div>

    <div class="grid-2">
      <div class="card"><div class="panel-title"><h3>Template bank</h3><span>network SNR by component masses</span></div><div id="bankPlot" class="plot" style="height:300px"></div></div>
      <div class="card"><div class="panel-title"><h3>Chirp mass</h3><span>inspiral-only templates, profile over mass ratio</span></div><div id="mcPlot" class="plot" style="height:300px"></div></div>
    </div>

    <div class="card"><div class="panel-title"><h3>Detector noise</h3><span>amplitude spectral density</span></div><div id="asdPlot" class="plot"></div>
      <details class="log" style="margin-top:8px"><summary>Analysis log</summary><pre class="console">${esc(r.log.join("\n"))}</pre></details></div>
  </div>`;

  $("#dlJson").onclick = () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(r, null, 1)], { type: "application/json" }));
    a.download = r.file; a.click();
  };
  r.per_detector.forEach((p, i) => {
    const col = css(DET_COLOR[p.det]);
    Plotly.react(`w${i}`, [
      { x: p.whitened.t, y: p.whitened.data, mode: "lines", line: { color: css("--point"), width: 1 }, name: "data" },
      { x: p.whitened.t, y: p.whitened.model, mode: "lines", line: { color: col, width: 1.8 }, name: "template" },
    ], L({ xaxis: { title: "Seconds from merger", range: [-0.35, 0.1] }, yaxis: { title: "Whitened" }, margin: { l: 50, r: 10, t: 6, b: 36 } }), plotCfg);
    Plotly.react(`q${i}`, [{ type: "heatmap", x: p.qscan.t, y: p.qscan.f, z: p.qscan.e, colorscale: [[0, css("--panel")], [0.15, css("--panel-2")], [0.4, "#e9b97a"], [0.7, "#d4661a"], [1, "#7a2e06"]],
      zmin: 0, zmax: 25, showscale: false, hovertemplate: "%{x:.3f} s · %{y:.0f} Hz<br>energy %{z}<extra></extra>" }],
      L({ xaxis: { title: "Seconds from merger", range: [-0.5, 0.15] }, yaxis: { title: "Hz", type: "log" }, margin: { l: 50, r: 10, t: 6, b: 36 } }), plotCfg);
    Plotly.react(`s${i}`, [{ x: p.snr_series.t, y: p.snr_series.snr, mode: "lines", line: { color: col, width: 1.2 } }],
      L({ xaxis: { title: "Seconds" }, yaxis: { title: "SNR" }, margin: { l: 50, r: 10, t: 6, b: 34 } }), plotCfg);
  });
  document.querySelectorAll("[data-play]").forEach((btn) => (btn.onclick = () => {
    const p = r.per_detector[+btn.dataset.play];
    const shift = btn.closest(".card").querySelector(".pitch").checked;
    play(p.audio[btn.dataset.kind], p.audio.rate, shift);
  }));

  if (bg.slides) {
    Plotly.react("bgPlot", [{ type: "histogram", x: bg.slides, marker: { color: css("--point") }, nbinsx: 40 }],
      L({ xaxis: { title: "Network SNR" }, yaxis: { title: "Time slides" },
        shapes: [{ type: "line", x0: b.net_snr, x1: b.net_snr, yref: "paper", y0: 0, y1: 1, line: { color: css("--accent"), width: 2 } }],
        annotations: [{ x: b.net_snr, yref: "paper", y: 1, text: "candidate", showarrow: false, yanchor: "bottom", font: { color: css("--accent") } }] }), plotCfg);
  } else $("#bgPlot").innerHTML = `<p class="empty-note">Needs data from at least two detectors.</p>`;

  Plotly.react("bankPlot", [{ type: "scatter", mode: "markers", x: r.bank.m1, y: r.bank.m2,
    marker: { color: r.bank.snr, size: 7, colorscale: [[0, css("--panel-2")], [0.5, "#e9b97a"], [1, "#b4530f"]], showscale: true, colorbar: { thickness: 10, outlinewidth: 0 } },
    hovertemplate: "%{x:.1f} + %{y:.1f} M☉<br>SNR %{marker.color:.1f}<extra></extra>" }],
    L({ xaxis: { title: "m₁ (M☉)", type: "log" }, yaxis: { title: "m₂ (M☉)", type: "log" } }), plotCfg);

  Plotly.react("mcPlot", [{ x: pe.profile.mc, y: pe.profile.snr, mode: "lines", line: { color: css("--text"), width: 1.5 } }],
    L({ xaxis: { title: "Chirp mass, detector frame (M☉)" }, yaxis: { title: "Network SNR" },
      shapes: [{ type: "rect", x0: pe.mc_lo, x1: pe.mc_hi, yref: "paper", y0: 0, y1: 1, fillcolor: css("--accent") + "33", line: { width: 0 } },
        ...(c?.mchirp && zc != null ? [{ type: "line", x0: c.mchirp * (1 + zc), x1: c.mchirp * (1 + zc), yref: "paper", y0: 0, y1: 1, line: { color: css("--c1"), dash: "dash" } }] : [])],
      annotations: c?.mchirp && zc != null ? [{ x: c.mchirp * (1 + zc), yref: "paper", y: 1, text: "published", showarrow: false, yanchor: "bottom", font: { color: css("--c1") } }] : [] }), plotCfg);

  Plotly.react("asdPlot", r.per_detector.map((p) => ({ x: p.asd.f, y: p.asd.a, mode: "lines", name: p.det, line: { color: css(DET_COLOR[p.det]), width: 1.2 } })),
    L({ showlegend: true, legend: { orientation: "h", x: 0, y: 1.12 }, xaxis: { title: "Frequency (Hz)", type: "log" }, yaxis: { title: "Strain / √Hz", type: "log", exponentformat: "e" } }), plotCfg);
}
const m = (k, v, e) => `<div class="metric"><div class="k">${k}</div><div class="v">${v}</div><div class="e">${e}</div></div>`;

/* ---------- audio ---------- */
let audioCtx;
function play(samples, rate, shift) {
  audioCtx = audioCtx || new AudioContext();
  // upsample by 4 (linear) so every browser accepts the buffer; optionally play 4x faster = 2 octaves higher
  const up = 4, out = new Float32Array(samples.length * up);
  for (let i = 0; i < samples.length - 1; i++) for (let k = 0; k < up; k++) out[i * up + k] = samples[i] + (samples[i + 1] - samples[i]) * k / up;
  const fade = 400;
  for (let i = 0; i < fade; i++) { out[i] *= i / fade; out[out.length - 1 - i] *= i / fade; }
  const buf = audioCtx.createBuffer(1, out.length, rate * up * (shift ? 4 : 1));
  buf.copyToChannel(out.map((x) => x * 0.8), 0);
  const src = audioCtx.createBufferSource(); src.buffer = buf; src.connect(audioCtx.destination); src.start();
}

/* ---------- theme ---------- */
function initTheme() {
  try { const t = localStorage.getItem("theme"); if (t) document.documentElement.dataset.theme = t; } catch {}
  $("#themeBtn").onclick = () => {
    const dark = css("--bg") === "#111214";
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("theme", document.documentElement.dataset.theme); } catch {}
    if (state.result && !$("#resultView").classList.contains("hidden")) render();
  };
}

(async function main() {
  initTheme();
  state.events = await api("/api/events").catch(() => []);
  renderEvents(); refreshResults();
  setInterval(refreshResults, 3000);
  if (location.hash.startsWith("#r=")) openResult(decodeURIComponent(location.hash.slice(3))).catch(() => show("emptyView"));
})();
