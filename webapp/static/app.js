// UI logic: job lifecycle, result binding, legend, zone panel + monthly chart.
import { Viewer, rampCss, rampColor } from './viewer.js?v=2';

const $ = id => document.getElementById(id);
const state = {
  job: null, geometry: null, results: null,
  metric: 'heating_kwh', period: 'annual', ramp: 'sequential',
  selectedZone: null,
};

const viewer = new Viewer($('canvas-holder'), onZonePicked);

const METRIC_LABELS = {
  heating_kwh: 'Heating energy (kWh)', cooling_kwh: 'Cooling energy (kWh)',
  heating_kwh_m2: 'Heating (kWh/m²)', cooling_kwh_m2: 'Cooling (kWh/m²)',
  solar_gain_kwh: 'Solar gain, windows (kWh)',
  sunlit_frac: 'Sunlit fraction · shading',
  temp_avg_c: 'Mean air temp (°C)', operative_temp_c: 'Operative temp (°C)',
  rh_pct: 'Relative humidity (%)',
  temp_min_c: 'Min air temp (°C)', temp_max_c: 'Max air temp (°C)',
};
const MONTHLY_KEY = {
  heating_kwh: 'heating_kwh', cooling_kwh: 'cooling_kwh', temp_avg_c: 'temp_c',
  solar_gain_kwh: 'solar_gain_kwh', sunlit_frac: 'sunlit_frac',
  operative_temp_c: 'operative_temp_c', rh_pct: 'rh_pct',
};

// ---------------------------------------------------------------- catalogs
async function loadCatalogs() {
  const [weather, demos] = await Promise.all([
    fetch('/api/weather').then(r => r.json()),
    fetch('/api/demos').then(r => r.json()),
  ]);
  $('weather-select').innerHTML = weather
    .map(w => `<option value="${w.name}">${w.name}</option>`).join('');
  $('demo-list').innerHTML = '';
  for (const d of demos) {
    const b = document.createElement('button');
    b.innerHTML = `${d.name}<span class="meta">${d.type.toUpperCase()} · ${d.size_mb} MB</span>`;
    b.onclick = () => runDemo(d.name);
    $('demo-list').appendChild(b);
  }
}

// ---------------------------------------------------------------- job flow
function setBadge(text, cls) {
  const b = $('job-badge');
  b.textContent = text;
  b.className = `badge ${cls}`;
}

async function createJob(formData) {
  const res = await fetch('/api/jobs', { method: 'POST', body: formData });
  const job = await res.json();
  if (!res.ok) throw new Error(job.error || 'job creation failed');
  return job;
}

async function startRun(jobId) {
  const opts = {
    weather: $('weather-select').value,
    wwr: parseFloat($('wwr').value),
    window_mode: $('window-mode').value,
  };
  const res = await fetch(`/api/jobs/${jobId}/run`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(opts),
  });
  const job = await res.json();
  if (!res.ok) throw new Error(job.error || 'run failed');
  state.job = job;
  setBadge('running · queued', 'running');
  $('run-btn').disabled = true;
  $('log-toggle').hidden = false;
  $('reset-btn').hidden = false;
  progMax = 0;
  $('progress-wrap').hidden = false;
  updateProgress({ state: 'running', progress: 0, stage: '', detail: 'queued', elapsed_sec: 0 });
  poll(jobId);
}

// ---------------------------------------------------------------- progress
const STAGE_ORDER = ['convert', 'geometry', 'simulate', 'results'];
let progMax = 0; // monotonic: the server estimate must never move backwards

function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
}

function updateProgress(job) {
  const running = job.state === 'running', done = job.state === 'done';
  let p = done ? 1 : (job.progress || 0);
  progMax = Math.max(progMax, p);
  p = progMax;
  $('progress-fill').style.width = (p * 100).toFixed(1) + '%';
  $('progress-fill').classList.toggle('error', job.state === 'error');
  const cur = done ? STAGE_ORDER.length : STAGE_ORDER.indexOf(job.stage);
  document.querySelectorAll('#stage-steps li').forEach((li, i) => {
    li.className = i < cur ? 'done' : (i === cur ? 'active' : '');
  });
  $('progress-stage').textContent =
    done ? 'completed'
    : job.state === 'error' ? 'failed — see log'
    : `${Math.round(p * 100)}% · ${job.detail || job.stage || 'starting'}`;
  let eta = '';
  if (running && (job.elapsed_sec || 0) > 3 && p > 0.05 && p < 0.995) {
    eta = `${fmtDur(job.elapsed_sec)} elapsed · ~${fmtDur(job.elapsed_sec * (1 - p) / p)} left`;
  } else if (running) {
    eta = `${fmtDur(job.elapsed_sec || 0)} elapsed`;
  }
  $('progress-eta').textContent = eta;
}

async function poll(jobId) {
  if (!state.job || state.job.id !== jobId) return; // job was reset meanwhile
  const job = await fetch(`/api/jobs/${jobId}`).then(r => r.json());
  $('log').textContent = (job.log_tail || []).join('\n');
  $('log').scrollTop = $('log').scrollHeight;
  updateProgress(job);
  if (job.state === 'running') {
    setBadge(`running · ${job.stage || 'starting'}`, 'running');
    setTimeout(() => poll(jobId), 1500);
    return;
  }
  $('run-btn').disabled = false;
  if (job.state === 'done') {
    setBadge('done', 'done');
    await loadJobArtifacts(jobId, job);
  } else {
    setBadge('error', 'error');
    $('log').hidden = false;
    $('log-toggle').textContent = 'hide log';
  }
}

async function loadJobArtifacts(jobId, job) {
  const [geometry, results] = await Promise.all([
    fetch(`/api/jobs/${jobId}/geometry`).then(r => r.json()),
    fetch(`/api/jobs/${jobId}/results`).then(r => r.json()),
  ]);
  state.geometry = geometry;
  state.results = results;
  state.selectedZone = null;
  $('empty-hint').style.display = 'none';
  $('zone-panel').hidden = true;
  viewer.load(geometry);
  buildPeriodOptions();
  applyColors();
  showTotals(job.totals || results.totals);
  for (const [k, art] of [['dl-idf', 'idf'], ['dl-results', 'results'],
                          ['dl-report', 'report'], ['dl-err', 'err']]) {
    $(k).href = `/api/jobs/${jobId}/${art}`;
  }
  $('results-panel').hidden = false;
  buildZoneTable();
}

function showTotals(t) {
  if (!t) return;
  const kv = (label, val, unit) =>
    `<div class="kv"><span>${label}</span><b>${val ?? '–'}<small> ${unit}</small></b></div>`;
  $('totals').innerHTML =
    kv('Zones', t.zone_count, '') +
    kv('Floor area', t.floor_area_m2?.toLocaleString(), 'm²') +
    kv('Heating', Math.round(t.heating_kwh).toLocaleString(), 'kWh/yr') +
    kv('Cooling', Math.round(t.cooling_kwh).toLocaleString(), 'kWh/yr') +
    (t.heating_kwh_m2 !== undefined ? kv('Heating EUI', t.heating_kwh_m2, 'kWh/m²') : '') +
    (t.cooling_kwh_m2 !== undefined ? kv('Cooling EUI', t.cooling_kwh_m2, 'kWh/m²') : '');
}

// ---------------------------------------------------------------- coloring
function buildPeriodOptions() {
  const months = state.results?.months || [];
  const sel = $('period-select');
  sel.innerHTML = '<option value="annual">Annual</option>' +
    months.map((m, i) => `<option value="${i}">${m}</option>`).join('');
  sel.value = 'annual';
  state.period = 'annual';
}

function metricValues() {
  const zones = state.results?.zones || {};
  const values = new Map();
  for (const [name, z] of Object.entries(zones)) {
    let v;
    if (state.period === 'annual') {
      v = z[state.metric];
    } else {
      const key = MONTHLY_KEY[state.metric];
      v = key ? z.monthly?.[key]?.[+state.period] : z[state.metric];
    }
    if (v !== undefined && v !== null) values.set(name.toUpperCase(), v);
  }
  return values;
}

function applyColors() {
  if (!state.results) return;
  const values = metricValues();
  const { min, max } = viewer.colorize(values, state.ramp);
  const monthly = state.period !== 'annual' && MONTHLY_KEY[state.metric];
  const periodLabel = state.period === 'annual' ? 'annual'
    : (state.results.months?.[+state.period] || '');
  $('legend-title').textContent =
    `${METRIC_LABELS[state.metric]} · ${monthly || state.period === 'annual' ? periodLabel : 'annual value'}`;
  $('legend-bar').style.background = rampCss(state.ramp);
  const fmt = v => Math.abs(v) >= 100 ? Math.round(v).toLocaleString()
    : Math.abs(v) < 2 ? v.toFixed(2) : v.toFixed(1); // 2 decimals for 0-1 fractions
  $('legend-min').textContent = isFinite(min) ? fmt(min) : '–';
  $('legend-max').textContent = isFinite(max) ? fmt(max) : '–';
}

// ---------------------------------------------------------------- zone panel
function onZonePicked(zoneName) {
  state.selectedZone = zoneName;
  if (!zoneName || !state.results) { $('zone-panel').hidden = true; return; }
  const z = Object.entries(state.results.zones)
    .find(([n]) => n.toUpperCase() === zoneName.toUpperCase())?.[1];
  const gz = (state.geometry.zones || []).find(g => g.name.toUpperCase() === zoneName.toUpperCase());
  $('zone-title').textContent = zoneName;
  const kv = (label, val, unit) =>
    `<div class="kv"><span>${label}</span><b>${val ?? '–'}<small> ${unit}</small></b></div>`;
  $('zone-info').innerHTML =
    kv('Floor area', z?.area_m2 ?? gz?.area_m2, 'm²') +
    kv('Volume', z?.volume_m3 ?? gz?.volume_m3, 'm³') +
    kv('Heating', z ? Math.round(z.heating_kwh).toLocaleString() : '–', 'kWh/yr') +
    kv('Cooling', z ? Math.round(z.cooling_kwh).toLocaleString() : '–', 'kWh/yr') +
    kv('Solar gain', z?.solar_gain_kwh !== undefined
      ? Math.round(z.solar_gain_kwh).toLocaleString() : '–', 'kWh/yr') +
    kv('Sunlit', z?.sunlit_frac !== undefined
      ? (z.sunlit_frac * 100).toFixed(0) + '%' : '–', 'of exterior') +
    kv('Mean temp', z?.temp_avg_c, '°C') +
    kv('Storey', z?.storey || gz?.storey || '–', '');
  $('zone-panel').hidden = false;
  drawZoneChart(z);
}

// Monthly grouped bar chart — thin marks, rounded data ends, 2px gaps,
// recessive grid, hover tooltip (dataviz spec).
const HEAT = '#e66767', COOL = '#3987e5', GRID = '#2c2c2a', MUTED = '#898781', BASE = '#383835';
let chartHit = [];

function drawZoneChart(z) {
  const canvas = $('zone-chart');
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth || 300, H = 150;
  canvas.width = W * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  chartHit = [];
  const heat = z?.monthly?.heating_kwh || [];
  const cool = z?.monthly?.cooling_kwh || [];
  const n = Math.max(heat.length, cool.length, 12);
  const maxV = Math.max(1e-9, ...heat, ...cool);
  const padL = 34, padB = 16, padT = 6;
  const plotW = W - padL - 4, plotH = H - padT - padB;

  ctx.font = '9.5px system-ui'; ctx.fillStyle = MUTED;
  ctx.strokeStyle = GRID; ctx.lineWidth = 1;
  const ticks = 3;
  for (let i = 0; i <= ticks; i++) {
    const v = (maxV / ticks) * i;
    const y = padT + plotH - (plotH * i) / ticks;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - 4, y); ctx.stroke();
    ctx.textAlign = 'right';
    ctx.fillText(v >= 100 ? Math.round(v) : v.toFixed(1), padL - 4, y + 3);
  }
  ctx.strokeStyle = BASE;
  ctx.beginPath(); ctx.moveTo(padL, padT + plotH); ctx.lineTo(W - 4, padT + plotH); ctx.stroke();

  const group = plotW / n;
  const barW = Math.max(2, Math.min(9, (group - 6) / 2));
  const labels = 'JFMAMJJASOND';
  for (let m = 0; m < n; m++) {
    const x0 = padL + group * m + group / 2;
    ctx.fillStyle = MUTED; ctx.textAlign = 'center';
    if (group > 11 || m % 2 === 0) ctx.fillText(labels[m % 12] || '', x0, H - 4);
    const series = [[heat[m] || 0, HEAT, 'Heating'], [cool[m] || 0, COOL, 'Cooling']];
    series.forEach(([v, color, label], si) => {
      const h = Math.max(0, (v / maxV) * plotH);
      const x = x0 - barW - 1 + si * (barW + 2); // 2px gap between the pair
      const y = padT + plotH - h;
      ctx.fillStyle = color;
      if (h > 0.5) {
        ctx.beginPath();
        ctx.roundRect(x, y, barW, h, [2, 2, 0, 0]);
        ctx.fill();
      }
      chartHit.push({ x, y: padT, w: barW + 2, h: plotH, label, month: m, v });
    });
  }
}

$('zone-chart').addEventListener('mousemove', e => {
  const rect = e.target.getBoundingClientRect();
  const x = e.clientX - rect.left, y = e.clientY - rect.top;
  const hit = chartHit.find(b => x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h);
  const tip = $('chart-tip');
  if (!hit) { tip.hidden = true; return; }
  const months = state.results?.months || [];
  tip.textContent = `${months[hit.month] || 'M' + (hit.month + 1)} · ${hit.label}: ${hit.v.toFixed(1)} kWh`;
  tip.style.left = Math.min(x + 10, 190) + 'px';
  tip.style.top = (y - 8) + 'px';
  tip.hidden = false;
});
$('zone-chart').addEventListener('mouseleave', () => ($('chart-tip').hidden = true));

// ---------------------------------------------------------------- zone table
function buildZoneTable() {
  const zones = state.results?.zones || {};
  const rows = Object.entries(zones).sort((a, b) => (b[1].heating_kwh || 0) - (a[1].heating_kwh || 0));
  const t = $('zone-table');
  t.innerHTML = `<tr><th>Zone</th><th>Area m²</th><th>Heat kWh</th><th>Cool kWh</th>
    <th>Heat kWh/m²</th><th>Cool kWh/m²</th><th>Solar kWh</th><th>Sunlit</th><th>T̄ °C</th></tr>` +
    rows.map(([n, z]) => `<tr data-zone="${n}"><td>${n}</td><td>${z.area_m2 ?? ''}</td>
      <td>${Math.round(z.heating_kwh ?? 0)}</td><td>${Math.round(z.cooling_kwh ?? 0)}</td>
      <td>${z.heating_kwh_m2 ?? ''}</td><td>${z.cooling_kwh_m2 ?? ''}</td>
      <td>${z.solar_gain_kwh !== undefined ? Math.round(z.solar_gain_kwh) : ''}</td>
      <td>${z.sunlit_frac !== undefined ? (z.sunlit_frac * 100).toFixed(0) + '%' : ''}</td>
      <td>${z.temp_avg_c ?? ''}</td></tr>`).join('');
  t.querySelectorAll('tr[data-zone]').forEach(tr => {
    tr.onclick = () => { viewer.select(tr.dataset.zone); onZonePicked(tr.dataset.zone); };
  });
}

// ---------------------------------------------------------------- events
$('run-btn').onclick = async () => {
  try {
    const fd = new FormData();
    for (const [field, id] of [['ifc', 'file-ifc'], ['idf', 'file-idf'], ['epw', 'file-epw']]) {
      const f = $(id).files[0];
      if (f) fd.append(field, f);
    }
    setBadge('uploading', 'running');
    const job = await createJob(fd);
    await startRun(job.id);
  } catch (e) {
    setBadge('error', 'error');
    alert(e.message);
  }
};

async function runDemo(name) {
  try {
    const fd = new FormData();
    fd.append('demo', name);
    setBadge('loading demo', 'running');
    const job = await createJob(fd);
    await startRun(job.id);
  } catch (e) {
    setBadge('error', 'error');
    alert(e.message);
  }
}

$('metric-select').onchange = e => { state.metric = e.target.value; applyColors(); };
$('period-select').onchange = e => { state.period = e.target.value; applyColors(); };
$('ramp-select').onchange = e => { state.ramp = e.target.value; applyColors(); };
$('opacity').oninput = e => viewer.setOpacity(parseFloat(e.target.value));
$('show-context').onchange = e => viewer.setContextVisible(e.target.checked);
$('show-edges').onchange = e => viewer.setEdgesVisible(e.target.checked);
$('wwr').oninput = e => ($('wwr-value').textContent = (+e.target.value).toFixed(2));
$('log-toggle').onclick = () => {
  const l = $('log');
  l.hidden = !l.hidden;
  $('log-toggle').textContent = l.hidden ? 'show log' : 'hide log';
};
$('table-toggle').onclick = () => {
  const w = $('zone-table-wrap');
  w.hidden = !w.hidden;
};

// ------------------------------------------------------------ report / reset
$('report-btn').onclick = () => {
  if (state.job) window.open(`/api/jobs/${state.job.id}/report_summary`, '_blank');
};

$('reset-btn').onclick = async () => {
  if (!confirm('Delete the current analysis results and reset the viewer?')) return;
  const jobId = state.job?.id;
  resetUI();
  if (jobId) {
    try { await fetch(`/api/jobs/${jobId}`, { method: 'DELETE' }); } catch { /* best effort */ }
  }
};

function resetUI() {
  state.job = null; state.geometry = null; state.results = null; state.selectedZone = null;
  progMax = 0;
  $('progress-wrap').hidden = true;
  viewer.clear();
  setBadge('idle', 'idle');
  $('results-panel').hidden = true;
  $('zone-panel').hidden = true;
  $('zone-table-wrap').hidden = true;
  $('log').textContent = ''; $('log').hidden = true;
  $('log-toggle').hidden = true; $('log-toggle').textContent = 'show log';
  $('empty-hint').style.display = '';
  $('run-btn').disabled = false;
  $('reset-btn').hidden = true;
  $('legend-title').textContent = ''; $('legend-min').textContent = '';
  $('legend-max').textContent = ''; $('legend-bar').style.background = 'none';
  for (const id of ['file-ifc', 'file-idf', 'file-epw']) $(id).value = '';
}

// ---------------------------------------------------------------- splitters
function dragX(handle, begin, onEnd) {
  handle.addEventListener('pointerdown', e => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add('dragging');
    const x0 = e.clientX, move = begin();
    const onMove = ev => {
      move(ev.clientX - x0);
      window.dispatchEvent(new Event('resize')); // keep the 3D viewer in sync
    };
    const stop = () => {
      handle.classList.remove('dragging');
      handle.removeEventListener('pointermove', onMove);
      handle.removeEventListener('pointerup', stop);
      handle.removeEventListener('pointercancel', stop);
      onEnd?.();
    };
    handle.addEventListener('pointermove', onMove);
    handle.addEventListener('pointerup', stop);
    handle.addEventListener('pointercancel', stop);
  });
}

function initSplitters() {
  const sidebar = $('sidebar'), zwrap = $('zone-table-wrap');
  const savedSide = +localStorage.getItem('bem.sidebarW');
  if (savedSide) sidebar.style.width = savedSide + 'px';
  const savedZone = +localStorage.getItem('bem.zoneTableW');
  if (savedZone) zwrap.style.width = savedZone + 'px';

  dragX($('splitter'), () => {
    const w0 = sidebar.getBoundingClientRect().width;
    return dx => {
      const w = Math.round(Math.min(Math.max(240, w0 + dx), window.innerWidth * 0.6));
      sidebar.style.width = w + 'px';
      localStorage.setItem('bem.sidebarW', w);
    };
  }, () => {
    // sidebar width changed: redraw the zone chart at its new size
    if (state.selectedZone) onZonePicked(state.selectedZone);
  });

  dragX($('zt-splitter'), () => {
    const w0 = zwrap.getBoundingClientRect().width;
    return dx => {
      // handle sits on the panel's left edge: dragging left grows the panel
      const w = Math.round(Math.min(Math.max(300, w0 - dx), window.innerWidth * 0.8));
      zwrap.style.width = w + 'px';
      localStorage.setItem('bem.zoneTableW', w);
    };
  });
}

initSplitters();
loadCatalogs();
