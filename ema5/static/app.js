/* 5 EMA Monthly Desk -- dashboard logic.
   Reads /api/state, renders the four views, and posts the take / ignore /
   close actions. Nothing here decides strategy: every number comes from the
   server so the rules live in one place. */
'use strict';

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

const state = { data: null, view: 'candidates', token: '', busy: false, drawer: null };

/* ----------------------------------------------------------- formatting */
const nf = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const nf0 = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });

const money = (v) => (v === null || v === undefined || !isFinite(v) ? '--' : nf.format(v));
const pct = (v, digits = 2) =>
  v === null || v === undefined || !isFinite(v) ? '--' : `${v >= 0 ? '' : '-'}${Math.abs(v).toFixed(digits)}%`;
const rmul = (v) => (v === null || v === undefined || !isFinite(v) ? '--' : `${v >= 0 ? '+' : '-'}${Math.abs(v).toFixed(2)}R`);
const signClass = (v) => (v === null || v === undefined || !isFinite(v) ? '' : v > 0 ? 'pos' : v < 0 ? 'neg' : '');

function monthName(key) {
  if (!key) return '--';
  const [y, m] = key.split('-').map(Number);
  return `${['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][m - 1]} ${y}`;
}
function ago(iso) {
  if (!iso) return '';
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!isFinite(secs) || secs < 0) return '';
  if (secs < 60) return `${Math.round(secs)}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

function toast(message, kind = '') {
  const node = el('div', `toast ${kind}`, message);
  $('toasts').append(node);
  setTimeout(() => node.remove(), kind === 'bad' ? 7000 : 4000);
}

/* ------------------------------------------------------------------ api */
async function api(path, body) {
  const options = body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Desk-Token': state.token },
        body: JSON.stringify(body) }
    : {};
  const response = await fetch(path, options);
  let payload = {};
  try { payload = await response.json(); } catch { /* empty body */ }
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function absorb(payload) {
  if (payload.token) state.token = payload.token;
  if (payload.candidates) { state.data = payload; render(); }
}

/* ---------------------------------------------------------------- badges */
function debtBadge(row) {
  const verdict = row.debt_verdict || 'unknown';
  const debt = row.debt || {};
  const badge = el('span', `badge ${verdict}`);
  badge.append(el('i'));
  let text = row.debt_label || 'Debt unknown';
  if (debt.de !== null && debt.de !== undefined && verdict !== 'unknown') {
    text += `  ${debt.de.toFixed(2)}`;
  }
  badge.append(document.createTextNode(text));
  const parts = [];
  if (debt.de_nse !== null && debt.de_nse !== undefined) parts.push(`NSE ${debt.de_nse.toFixed(2)}`);
  if (debt.de_screener !== null && debt.de_screener !== undefined) parts.push(`screener.in ${debt.de_screener.toFixed(2)}`);
  if (debt.sector) parts.push(debt.sector);
  if (debt.interest_cover) parts.push(`interest cover ${debt.interest_cover.toFixed(1)}x`);
  badge.title = parts.join(' · ') || 'No debt figure yet -- open the stock to fetch it.';
  return badge;
}

function awayCell(row, band) {
  const wrap = el('div', 'away');
  const meter = el('div', 'meter');
  const closeness = Math.max(0, Math.min(1, 1 - (row.distance_pct || 0) / (band || 2)));
  meter.style.setProperty('--pct', `${(closeness * 100).toFixed(1)}%`);
  if (closeness > 0.66) meter.classList.add('hot');
  meter.append(el('i'));
  meter.setAttribute('role', 'img');
  meter.setAttribute('aria-label', `${pct(row.distance_pct)} below the trigger`);
  wrap.append(el('span', '', pct(row.distance_pct)), meter);
  return wrap;
}

/* ------------------------------------------------------------ R ladder */
function ladderStrip(position) {
  const NS = 'http://www.w3.org/2000/svg';
  const W = 200, H = 30, padL = 2, padR = 2;
  const peak = position.peak_r || 0;
  const now = position.r_now === null || position.r_now === undefined ? 0 : position.r_now;
  const top = Math.max(4, Math.ceil(Math.max(peak, now) + 1));
  const lo = -1.15;
  const x = (r) => padL + ((r - lo) / (top - lo)) * (W - padL - padR);

  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('class', 'rstrip');
  svg.setAttribute('preserveAspectRatio', 'none');
  const add = (tag, attrs) => {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    svg.append(node);
    return node;
  };

  // A neutral track, because the fill here is green or red: a blue track
  // would put a third hue on a bar that carries none.
  const mid = 15, h = 7;
  add('rect', { x: padL, y: mid - h / 2, width: W - padL - padR, height: h, rx: h / 2,
                fill: 'color-mix(in srgb, var(--ink) 13%, transparent)' });
  // the losing side of the entry, as a wash
  add('rect', { x: x(lo), y: mid - h / 2, width: Math.max(0, x(0) - x(lo)), height: h,
                fill: 'color-mix(in srgb, var(--critical) 20%, transparent)' });

  // progress from entry to the current R
  if (now > 0) {
    add('rect', { x: x(0), y: mid - h / 2, width: Math.max(1.5, x(Math.min(now, top)) - x(0)),
                  height: h, rx: 2, fill: 'var(--good)' });
  } else if (now < 0) {
    add('rect', { x: x(Math.max(now, lo)), y: mid - h / 2, width: Math.max(1.5, x(0) - x(Math.max(now, lo))),
                  height: h, rx: 2, fill: 'var(--critical)' });
  }

  // ladder rungs
  for (let r = 2; r <= top; r += 1) {
    add('line', { x1: x(r), x2: x(r), y1: mid - h / 2 - 3, y2: mid + h / 2 + 3,
                  stroke: 'var(--baseline)', 'stroke-width': 1 });
    const label = add('text', { x: x(r), y: H - 1, 'text-anchor': 'middle',
                                fill: 'var(--muted)', 'font-size': 7.5 });
    label.textContent = `${r}R`;
  }
  // entry line
  add('line', { x1: x(0), x2: x(0), y1: 2, y2: H - 10, stroke: 'var(--ink-2)', 'stroke-width': 1 });

  // where the trailing stop now sits
  const locked = position.locked_r;
  if (locked !== null && locked !== undefined && isFinite(locked)) {
    add('rect', { x: x(locked) - 1.25, y: mid - h / 2 - 4, width: 2.5, height: h + 8, rx: 1.25,
                  fill: locked >= 0 ? 'var(--good)' : 'var(--critical)',
                  stroke: 'var(--surface)', 'stroke-width': 1.5 });
  }
  // the live position, ringed in the surface colour so it stays legible
  add('circle', { cx: x(Math.max(lo, Math.min(now, top))), cy: mid, r: 4.5,
                  fill: 'var(--ink)', stroke: 'var(--surface)', 'stroke-width': 2 });

  const title = document.createElementNS(NS, 'title');
  title.textContent = `Now ${rmul(now)} · peak ${rmul(peak)} · stop locks ${rmul(locked)}`;
  svg.append(title);
  return svg;
}

/* ------------------------------------------------------- zoomable chart */
/* Monthly or daily candles. Wheel = zoom at the cursor, drag = pan, double-click
   = show everything. "Alert" and "Entry" jump to those candles. */
function zoomChart(detail, setup, marks, entryDate) {
  const NS = 'http://www.w3.org/2000/svg';
  const W = 640, H = 320, padT = 14, padB = 26, padR = 64, padL = 6;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const wrap = el('div', 'chart-wrap');
  const sets = {
    monthly: (detail.months || []).map((m) => ({ ...m, key: m.month })),
    daily: (detail.days || []).map((d) => ({ ...d, key: d.date })),
  };
  if (sets.monthly.length < 2) {
    wrap.append(el('p', 'muted small', 'Not enough monthly history to draw a chart.'));
    return wrap;
  }
  const alertMonth = setup.alert_month;
  const triggerDate = setup.triggered_on || null;
  let mode = 'monthly', data = sets.monthly, v0 = 0, v1 = data.length, hover = null;

  const bar = el('div', 'chart-bar');
  const HINT = 'Scroll to zoom · drag to pan · double-click to reset';
  const readout = el('div', 'chart-readout muted small', HINT);
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('width', '100%');
  svg.setAttribute('role', 'img');
  svg.classList.add('zoomable');

  const add = (tag, attrs, parent) => {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    (parent || svg).append(node);
    return node;
  };
  const minSpan = () => Math.min(data.length, mode === 'daily' ? 12 : 4);
  const clampView = () => {
    const n = data.length;
    const span = Math.min(Math.max(v1 - v0, minSpan()), n);
    if (v0 < 0) v0 = 0;
    if (v0 + span > n) v0 = n - span;
    v1 = v0 + span;
  };
  // index of the candle holding `key` (a date or month); daily falls to the next trading day
  const indexOfKey = (key, prefix) => {
    if (!key) return -1;
    if (mode === 'monthly') return data.findIndex((d) => d.key === key.slice(0, 7));
    return data.findIndex((d) => (prefix ? d.key.startsWith(key) : d.key >= key));
  };
  const alertRange = () => {
    if (!alertMonth) return null;
    if (mode === 'monthly') {
      const i = data.findIndex((d) => d.key === alertMonth);
      return i < 0 ? null : [i, i];
    }
    const idx = data.map((d, i) => (d.key.startsWith(alertMonth) ? i : -1)).filter((i) => i >= 0);
    return idx.length ? [idx[0], idx[idx.length - 1]] : null;
  };
  const focus = (centre, before, after) => {
    v0 = centre - before;
    v1 = centre + after + 1;
    draw();
  };
  const focusAlert = () => {
    const r = alertRange();
    if (!r) return;
    if (mode === 'monthly') focus(r[0], 6, 6);
    else focus(r[0], 12, r[1] - r[0] + 30);
  };
  const focusEntry = () => {
    const i = entryDate ? indexOfKey(entryDate) : -1;
    if (i < 0) return;
    if (mode === 'monthly') focus(i, 8, 4);
    else focus(i, 30, 30);
  };
  const showAll = () => { v0 = 0; v1 = data.length; draw(); };
  const setMode = (next) => {
    if (next === mode || !sets[next].length) return;
    mode = next;
    data = sets[next];
    if (mode === 'daily') {
      if (entryDate && indexOfKey(entryDate) >= 0) focusEntry(); else focusAlert();
    } else {
      showAll();
    }
    syncButtons();
  };

  const buttons = {};
  const button = (label, title, onClick, key) => {
    const b = el('button', 'btn small', label);
    b.type = 'button';
    b.title = title;
    b.addEventListener('click', onClick);
    bar.append(b);
    if (key) buttons[key] = b;
    return b;
  };
  const syncButtons = () => {
    buttons.monthly.classList.toggle('on', mode === 'monthly');
    if (buttons.daily) buttons.daily.classList.toggle('on', mode === 'daily');
  };
  const zoomBy = (factor) => {
    // zoom about the entry candle when it is on screen, else about the middle
    const e = entryDate ? indexOfKey(entryDate) + 0.5 : -1;
    const c = e >= v0 && e <= v1 ? e : (v0 + v1) / 2;
    const span = (v1 - v0) * factor, ratio = (c - v0) / (v1 - v0);
    v0 = c - ratio * span;
    v1 = v0 + span;
    draw();
  };
  button('Monthly', 'Monthly candles', () => setMode('monthly'), 'monthly');
  if (sets.daily.length) button('Daily', 'Daily candles', () => setMode('daily'), 'daily');
  bar.append(el('span', 'chart-gap'));
  if (alertMonth) button('Alert', 'Zoom to the alert candle', focusAlert);
  if (entryDate) button('Entry', 'Zoom to the entry candle', focusEntry);
  button('All', 'Show everything', showAll);
  button('+', 'Zoom in', () => zoomBy(0.6));
  button('−', 'Zoom out', () => zoomBy(1 / 0.6));

  const label = (d) => {
    if (mode === 'monthly') return monthName(d.key) + (d.forming ? ' (in progress)' : '');
    return new Date(`${d.key}T00:00:00`).toLocaleDateString('en-IN',
      { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
  };
  const tickLabel = (d) => {
    if (mode === 'monthly') {
      const [year, month] = d.key.split('-');
      return month === '01' ? year : monthName(d.key).slice(0, 3);
    }
    return new Date(`${d.key}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
  };

  function draw() {
    clampView();
    svg.replaceChildren();
    syncButtons();
    const n = data.length, span = v1 - v0, band = plotW / span;
    const x = (i) => padL + (i + 0.5 - v0) * band;
    const first = Math.max(0, Math.floor(v0)), last = Math.min(n - 1, Math.ceil(v1) - 1);
    const seen = data.slice(first, last + 1);
    if (!seen.length) return;

    // price range follows what is on screen; a level only widens it when it is close
    let lo = Math.min(...seen.map((d) => d.low)), hi = Math.max(...seen.map((d) => d.high));
    const reach = (hi - lo) * 0.6 || hi * 0.05;
    const levels = [
      [marks.trigger, 'var(--good)', 'Trigger'],
      [marks.stop, 'var(--critical)', 'Stop'],
      [marks.entry, 'var(--accent)', 'Entry'],
      mode === 'daily' ? [setup.ema5, 'var(--accent)', '5 EMA (alert)'] : null,
    ].filter((l) => l && l[0]);
    levels.filter((l) => l[0] >= lo - reach && l[0] <= hi + reach)
      .forEach((l) => { lo = Math.min(lo, l[0]); hi = Math.max(hi, l[0]); });
    if (mode === 'monthly') {
      seen.forEach((d) => { if (d.ema5) { lo = Math.min(lo, d.ema5); hi = Math.max(hi, d.ema5); } });
    }
    const pad = (hi - lo) * 0.07 || 1;
    const yMin = lo - pad, yMax = hi + pad;
    const y = (v) => padT + ((yMax - v) / (yMax - yMin)) * plotH;
    const clipId = `clip${Math.random().toString(36).slice(2, 8)}`;
    const clip = add('clipPath', { id: clipId }, add('defs', {}));
    add('rect', { x: padL, y: padT, width: plotW, height: plotH }, clip);
    const plot = add('g', { 'clip-path': `url(#${clipId})` });

    // grid + price axis
    const step = (yMax - yMin) / 4;
    for (let k = 0; k <= 4; k++) {
      const v = yMin + step * k;
      add('line', { x1: padL, x2: padL + plotW, y1: y(v), y2: y(v), stroke: 'var(--baseline)',
                    'stroke-opacity': 0.35, 'stroke-width': 1 }, plot);
      add('text', { x: W - padR + 6, y: y(v) + 3, fill: 'var(--muted)', 'font-size': 9 }).textContent = nf0.format(v);
    }

    // alert candle(s), behind the candles
    const ar = alertRange();
    if (ar) {
      add('rect', { x: padL + (ar[0] - v0) * band, y: padT, width: (ar[1] - ar[0] + 1) * band,
                    height: plotH, fill: 'var(--violet-wash)' }, plot);
    }

    // entry candle: its own shaded column, so it stands out at any zoom
    const entryIdx = entryDate ? indexOfKey(entryDate) : -1;
    if (entryIdx >= 0) {
      add('rect', { x: padL + (entryIdx - v0) * band, y: padT, width: band, height: plotH,
                    fill: 'var(--accent)', 'fill-opacity': 0.24 }, plot);
    }

    // alert / trigger-cross / entry markers
    const marker = (idx, color, text, row) => {
      if (idx < 0 || idx < v0 - 1 || idx > v1) return;
      add('line', { x1: x(idx), x2: x(idx), y1: padT, y2: padT + plotH, stroke: color,
                    'stroke-width': 1.5, 'stroke-dasharray': '2 3' }, plot);
      add('text', { x: x(idx) + 4, y: padT + 10 + row * 11, fill: color, 'font-size': 10,
                    'font-weight': 600 }, plot).textContent = text;
    };
    const trigIdx = triggerDate ? indexOfKey(triggerDate) : -1;
    if (ar) marker(ar[0], 'var(--violet)', 'Alert', 0);
    if (trigIdx >= 0 && trigIdx !== entryIdx) marker(trigIdx, 'var(--good)', 'Crossed trigger', 1);
    if (entryIdx >= 0) marker(entryIdx, 'var(--accent)', marks.entry ? 'Entry' : 'Entry (crossed trigger)', 2);

    // reference levels; one that is off screen pins to the edge, faded
    levels.forEach(([value, color, text]) => {
      const inside = value >= yMin && value <= yMax;
      const yy = Math.min(padT + plotH, Math.max(padT, y(value)));
      add('line', { x1: padL, x2: padL + plotW, y1: yy, y2: yy, stroke: color, 'stroke-width': 1.4,
                    'stroke-dasharray': '5 4', 'stroke-opacity': inside ? 1 : 0.35 }, plot);
      add('text', { x: W - padR + 6, y: yy + (inside ? -3 : 9), fill: color, 'font-size': 9.5,
                    'font-weight': 600 }).textContent = `${text} ${nf0.format(value)}`;
    });

    // monthly 5 EMA
    if (mode === 'monthly') {
      const pts = [];
      data.forEach((d, i) => { if (d.ema5 && i >= first - 1 && i <= last + 1) pts.push(`${x(i)},${y(d.ema5)}`); });
      if (pts.length > 1) {
        add('polyline', { points: pts.join(' '), fill: 'none', stroke: 'var(--accent)',
                          'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, plot);
      }
    }

    // candles
    const bodyW = Math.max(1.4, Math.min(24, band * 0.6));
    const ring = (i, d, color) => add('rect', {
      x: x(i) - bodyW / 2 - 2, y: y(d.high) - 2, width: bodyW + 4, height: y(d.low) - y(d.high) + 4,
      rx: 3, fill: 'none', stroke: color, 'stroke-width': 1.5 }, plot);
    seen.forEach((d, k) => {
      const i = first + k;
      const colour = d.close >= d.open ? 'var(--good)' : 'var(--critical)';
      add('line', { x1: x(i), x2: x(i), y1: y(d.high), y2: y(d.low), stroke: colour, 'stroke-width': 1 }, plot);
      const rect = add('rect', {
        x: x(i) - bodyW / 2, y: y(Math.max(d.open, d.close)), width: bodyW,
        height: Math.max(1.2, Math.abs(y(d.open) - y(d.close))), rx: bodyW > 5 ? 1.5 : 0, fill: colour }, plot);
      if (d.forming) rect.setAttribute('fill-opacity', '0.45');
      if (mode === 'monthly' && d.key === alertMonth) ring(i, d, 'var(--violet)');
      if (i === entryIdx) {
        const r = ring(i, d, 'var(--accent)');
        r.setAttribute('stroke-width', 2.2);
        const px = marks.entry || marks.trigger;
        if (px) {
          add('circle', { cx: x(i), cy: y(px), r: Math.max(3.5, Math.min(6, bodyW / 2)), fill: 'var(--accent)',
                          stroke: 'var(--surface-raised, #fff)', 'stroke-width': 1.5 }, plot);
        }
      }
    });

    // callout above the entry candle
    if (entryIdx >= first && entryIdx <= last) {
      const d = data[entryIdx];
      const text = marks.entry ? `Entry ${money(marks.entry)}` : `Crossed ${money(marks.trigger)}`;
      const w = text.length * 6 + 14;
      const cx = Math.min(padL + plotW - w / 2, Math.max(padL + w / 2, x(entryIdx)));
      const top = Math.max(padT + 2, y(d.high) - 34);
      add('rect', { x: cx - w / 2, y: top, width: w, height: 18, rx: 9, fill: 'var(--accent)' });
      add('text', { x: cx, y: top + 12.5, 'text-anchor': 'middle', fill: 'var(--accent-ink, #fff)',
                    'font-size': 10.5, 'font-weight': 700 }).textContent = text;
      add('path', { d: `M${x(entryIdx) - 4},${top + 18} L${x(entryIdx) + 4},${top + 18} L${x(entryIdx)},${top + 24} Z`,
                    fill: 'var(--accent)' });
    }

    // x ticks
    const every = Math.max(1, Math.ceil(span / 7));
    for (let i = Math.ceil(v0); i < v1; i++) {
      if (i < 0 || i >= n || (i - Math.ceil(v0)) % every !== 0) continue;
      add('text', { x: x(i), y: H - 9, 'text-anchor': 'middle', fill: 'var(--muted)', 'font-size': 9 })
        .textContent = tickLabel(data[i]);
    }
    add('line', { x1: padL, x2: padL + plotW, y1: padT + plotH, y2: padT + plotH,
                  stroke: 'var(--baseline)', 'stroke-width': 1 });

    // crosshair + readout
    if (hover !== null && hover >= 0 && hover < n) {
      add('line', { x1: x(hover), x2: x(hover), y1: padT, y2: padT + plotH, stroke: 'var(--ink-2)',
                    'stroke-width': 1, 'stroke-opacity': 0.5 });
      const d = data[hover];
      readout.textContent = `${label(d)} · O ${money(d.open)}  H ${money(d.high)}  L ${money(d.low)}  C ${money(d.close)}` +
        (mode === 'monthly' && d.ema5 ? `  · 5 EMA ${money(d.ema5)}` : '');
      readout.classList.remove('muted');
    } else {
      readout.textContent = HINT;
      readout.classList.add('muted');
    }
    svg.setAttribute('aria-label',
      `${mode} candles. Alert candle ${monthName(alertMonth || '')}, trigger ${money(marks.trigger)}, stop ${money(marks.stop)}.`);
  }

  // wheel zooms around the cursor; drag pans
  const toIndex = (event) => {
    const box = svg.getBoundingClientRect();
    const px = ((event.clientX - box.left) / box.width) * W;
    return v0 + ((px - padL) / plotW) * (v1 - v0);
  };
  svg.addEventListener('wheel', (event) => {
    event.preventDefault();
    const idx = toIndex(event);
    const span = v1 - v0;
    const next = Math.min(data.length, Math.max(minSpan(), span * Math.exp(event.deltaY * 0.0015)));
    const ratio = (idx - v0) / span;
    v0 = idx - ratio * next;
    v1 = v0 + next;
    draw();
  }, { passive: false });
  let drag = null;
  svg.addEventListener('pointerdown', (event) => {
    drag = { x: event.clientX, v0, v1 };
    svg.setPointerCapture(event.pointerId);
    svg.classList.add('dragging');
  });
  svg.addEventListener('pointermove', (event) => {
    if (drag) {
      const box = svg.getBoundingClientRect();
      const bars = ((event.clientX - drag.x) / box.width) * W / (plotW / (drag.v1 - drag.v0));
      v0 = drag.v0 - bars;
      v1 = drag.v1 - bars;
    }
    hover = Math.round(toIndex(event) - 0.5);
    draw();
  });
  const release = () => { drag = null; svg.classList.remove('dragging'); };
  svg.addEventListener('pointerup', release);
  svg.addEventListener('pointercancel', release);
  svg.addEventListener('pointerleave', () => { hover = null; draw(); });
  svg.addEventListener('dblclick', showAll);

  wrap.append(bar, readout, svg);
  draw();

  const legend = el('div', 'legend');
  const key = (cls, text, style) => {
    const span = el('span');
    const dot = el('i', cls);
    Object.assign(dot.style, style);
    span.append(dot, document.createTextNode(text));
    return span;
  };
  legend.append(
    key('line', '5 EMA', { background: 'var(--accent)' }),
    key('', 'Up', { background: 'var(--good)' }),
    key('', 'Down', { background: 'var(--critical)' }),
    key('', 'Alert candle', { background: 'var(--violet-wash)', border: '1.5px solid var(--violet)' }),
    key('', 'Entry candle', { background: 'var(--accent-wash)', border: '1.5px solid var(--accent)' }),
    key('line', 'Trigger / stop / entry', { background: 'var(--baseline)' }),
  );
  wrap.append(legend);
  return wrap;
}

/* ---------------------------------------------------------------- views */
function emptyState(title, note) {
  const box = el('div', 'empty');
  box.innerHTML = '<svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4.3-4.3"/></svg>';
  box.append(el('strong', '', title), el('span', '', note));
  return box;
}

function table(columns, rows, build, onRow) {
  const scroll = el('div', 'scroll');
  const node = el('table');
  const head = el('thead');
  const hrow = el('tr');
  columns.forEach((c) => {
    const th = el('th', c.left ? 'l' : '', c.label);
    if (c.title) th.title = c.title;
    hrow.append(th);
  });
  head.append(hrow);
  const body = el('tbody');
  rows.forEach((row) => {
    const tr = el('tr');
    build(tr, row);
    if (onRow) {
      tr.tabIndex = 0;
      tr.addEventListener('click', (event) => {
        if (event.target.closest('button, a')) return;
        onRow(row);
      });
      tr.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); onRow(row); }
      });
    } else {
      tr.classList.add('flat');
    }
    body.append(tr);
  });
  node.append(head, body);
  scroll.append(node);
  return scroll;
}

function cell(tr, content, cls) {
  const td = el('td', cls);
  if (content instanceof Node) td.append(content);
  else td.textContent = content;
  tr.append(td);
  return td;
}

function symbolCell(tr, row, sub) {
  const td = el('td', 'l');
  const name = el('span', 'sym', row.symbol);
  td.append(name);
  if (sub) td.append(el('span', 'sub', sub));
  tr.append(td);
}

function candidateView(rows, settings, heading, note) {
  const panel = document.createDocumentFragment();
  const head = el('div', 'panel-head');
  const left = el('div');
  left.append(el('h2', '', heading), el('p', '', note));
  head.append(left);
  panel.append(head);

  if (!rows.length) {
    panel.append(emptyState('Nothing here right now',
      state.data.scan.status === 'idle'
        ? 'Run a scan to read the monthly candles.'
        : 'No stock is inside this band. The desk re-checks every scan.'));
    return panel;
  }

  const columns = [
    { label: 'Stock', left: true },
    { label: 'Debt', left: true, title: 'Debt to equity, the stricter of NSE MarketLens and screener.in' },
    { label: 'Price' },
    { label: 'Trigger', title: 'Alert candle high plus the entry buffer' },
    { label: 'Away', title: 'How far price still has to rise' },
    { label: 'Stop', title: 'Alert candle low' },
    { label: 'Risk' },
    { label: '2R target' },
    { label: 'Alert', left: true },
    { label: '' },
  ];

  panel.append(table(columns, rows, (tr, row) => {
    symbolCell(tr, row, row.debt && row.debt.name ? row.debt.name.slice(0, 30) : '');
    cell(tr, debtBadge(row), 'l');
    const price = cell(tr, money(row.ltp));
    if (row.change_pct !== null && row.change_pct !== undefined) {
      price.append(el('span', `sub ${signClass(row.change_pct)}`, pct(row.change_pct, 1)));
    }
    cell(tr, money(row.trigger));
    cell(tr, awayCell(row, settings.max_distance_pct));
    cell(tr, money(row.stop));
    const risk = cell(tr, money(row.risk));
    risk.append(el('span', 'sub', pct(row.risk_pct, 1)));
    cell(tr, money(row.reward_2r));
    cell(tr, monthName(row.alert_month), 'l');

    const actions = el('div', 'actions');
    const take = el('button', 'btn small primary', 'Take');
    take.addEventListener('click', () => openTake(row));
    const skip = el('button', 'btn small ghost', 'Ignore');
    skip.addEventListener('click', () => doIgnore(row));
    actions.append(take, skip);
    cell(tr, actions);
  }, (row) => openDrawer(row.symbol)));

  if (state.data.hidden_by_debt.length && settings.low_debt_only) {
    const note2 = el('p', 'muted small');
    note2.style.padding = '10px 16px';
    note2.textContent = `${state.data.hidden_by_debt.length} stock(s) in the band are hidden because their debt is above ${settings.max_de}: ` +
      state.data.hidden_by_debt.map((r) => `${r.symbol} (${r.debt && r.debt.de ? r.debt.de.toFixed(2) : '?'})`).join(', ');
    panel.append(note2);
  }
  return panel;
}

function positionsView(rows) {
  const panel = document.createDocumentFragment();
  const head = el('div', 'panel-head');
  const left = el('div');
  left.append(el('h2', '', 'Open trades'),
    el('p', '', 'Tracked entries. The stop follows the published ladder: breakeven at 2R, +1R at 3R, then 2R behind the peak.'));
  head.append(left);
  panel.append(head);

  if (!rows.length) {
    panel.append(emptyState('No entries being tracked',
      'Take an entry from the trigger list and it will be followed here.'));
    return panel;
  }

  const columns = [
    { label: 'Stock', left: true },
    { label: 'Entry' },
    { label: 'Price' },
    { label: 'Now' },
    { label: 'Progress', left: true, title: 'Entry, the R ladder, the live price and where the stop sits' },
    { label: 'Stop now' },
    { label: 'Locked' },
    { label: 'Peak' },
    { label: '' },
  ];

  panel.append(table(columns, rows, (tr, row) => {
    symbolCell(tr, row, `${row.entry_date} · ${row.bars_held || 0} sessions`);
    const entry = cell(tr, money(row.entry));
    entry.append(el('span', 'sub', `risk ${money(row.risk)}`));
    const price = cell(tr, money(row.ltp));
    if (row.change_pct !== null && row.change_pct !== undefined) {
      price.append(el('span', `sub ${signClass(row.change_pct)}`, pct(row.change_pct, 1)));
    }
    cell(tr, rmul(row.r_now), `num ${signClass(row.r_now)}`);
    const strip = cell(tr, ladderStrip(row), 'l');
    strip.classList.add('rwrap');
    const stop = cell(tr, money(row.stop_now));
    if (row.stop_moved) stop.append(el('span', 'sub pos', 'trailed'));
    else stop.append(el('span', 'sub', 'initial'));
    cell(tr, rmul(row.locked_r), `num ${signClass(row.locked_r)}`);
    const peak = cell(tr, rmul(row.peak_r));
    if (row.next_step) peak.append(el('span', 'sub', `next ${row.next_step.r}R at ${money(row.next_step.target)}`));

    const actions = el('div', 'actions');
    const exit = el('button', 'btn small', 'Exit');
    exit.addEventListener('click', () => openClose(row));
    actions.append(exit);
    cell(tr, actions);

    if (row.entry_reached === false) {
      tr.title = 'Price has not actually traded at or above this entry since the entry date.';
    }
  }, (row) => openDrawer(row.symbol)));
  return panel;
}

function closedView(rows) {
  const panel = document.createDocumentFragment();
  const head = el('div', 'panel-head');
  const left = el('div');
  left.append(el('h2', '', 'Closed'), el('p', '', 'Stopped out, or an exit you recorded.'));
  head.append(left);
  panel.append(head);
  if (!rows.length) {
    panel.append(emptyState('Nothing closed yet', 'Trades appear here once the trailing stop is hit.'));
    return panel;
  }
  const columns = [
    { label: 'Stock', left: true }, { label: 'Entry' }, { label: 'Exit' },
    { label: 'Result' }, { label: 'Peak' }, { label: 'Exit date', left: true },
    { label: 'How', left: true }, { label: '' },
  ];
  panel.append(table(columns, rows, (tr, row) => {
    const exit = row.exit || {};
    symbolCell(tr, row, monthName(row.alert_month));
    cell(tr, money(row.entry));
    cell(tr, money(exit.price));
    cell(tr, rmul(exit.r), `num ${signClass(exit.r)}`);
    cell(tr, rmul(row.peak_r));
    cell(tr, exit.date || '--', 'l');
    const how = el('span', `badge ${exit.r > 0 ? 'win' : 'stopped'}`);
    how.append(el('i'), document.createTextNode(
      exit.manual ? 'Recorded by you' : exit.gap ? 'Gapped through the stop' : 'Trailing stop'));
    cell(tr, how, 'l');
    const forget = el('button', 'btn small ghost', 'Remove');
    forget.addEventListener('click', () => doForget(row.id, 'Trade removed from the record.'));
    const actions = el('div', 'actions');
    actions.append(forget);
    cell(tr, actions);
  }, (row) => openDrawer(row.symbol)));
  return panel;
}

function ignoredView(rows) {
  const panel = document.createDocumentFragment();
  const head = el('div', 'panel-head');
  const left = el('div');
  left.append(el('h2', '', 'Ignored'),
    el('p', '', 'Skipped for this alert candle. A fresh alert candle on the same stock comes back automatically.'));
  head.append(left);
  panel.append(head);
  if (!rows.length) {
    panel.append(emptyState('Nothing ignored', 'Stocks you pass on are listed here.'));
    return panel;
  }
  const columns = [
    { label: 'Stock', left: true }, { label: 'Alert candle', left: true },
    { label: 'Trigger' }, { label: 'Price' }, { label: 'Away' },
    { label: 'Note', left: true }, { label: '' },
  ];
  panel.append(table(columns, rows, (tr, row) => {
    symbolCell(tr, row);
    cell(tr, monthName(row.alert_month), 'l');
    cell(tr, money(row.trigger));
    cell(tr, money(row.ltp));
    cell(tr, row.distance_pct === undefined ? '--' : pct(row.distance_pct));
    cell(tr, row.note || '', 'l');
    const restore = el('button', 'btn small', 'Put back');
    restore.addEventListener('click', () => doRestore(row.id));
    const actions = el('div', 'actions');
    actions.append(restore);
    cell(tr, actions);
  }));
  return panel;
}

/* --------------------------------------------------------------- render */
function render() {
  const data = state.data;
  if (!data) return;
  const t = data.totals, s = data.settings;

  $('kpiCandidates').textContent = t.candidates;
  $('kpiCandidatesNote').textContent = `0% to ${s.max_distance_pct}% below the trigger`;
  $('kpiApproaching').textContent = t.approaching;
  $('kpiApproachingNote').textContent = `up to ${s.approach_pct}% away`;
  $('kpiOpen').textContent = t.open;
  $('kpiOpenNote').textContent = t.open ? `${rmul(t.open_r)} open` : 'tracked, not ordered';
  const locked = $('kpiLocked');
  locked.textContent = t.open ? rmul(t.locked_r) : '--';
  locked.className = `kpi-value small ${t.locked_r > 0 ? 'pos' : ''}`;
  const realised = $('kpiRealised');
  realised.textContent = t.closed ? rmul(t.realised_r) : '--';
  realised.className = `kpi-value small ${signClass(t.realised_r)}`;
  $('kpiRealisedNote').textContent = t.closed ? `${t.wins} won · ${t.losses} lost` : 'stopped out';

  $('countCandidates').textContent = t.candidates;
  $('countApproaching').textContent = t.approaching;
  $('countPositions').textContent = t.open;
  $('countClosed').textContent = t.closed;
  $('countIgnored').textContent = data.ignored.length;

  const market = $('marketPill');
  market.classList.toggle('live', !!data.market.open);
  $('marketText').textContent = data.market.open ? 'Market open' : 'Market closed';
  const source = $('sourcePill');
  if (data.market.source) {
    source.hidden = false;
    source.textContent = `${data.market.source} · ${ago(data.market.fetched_at) || 'just now'}`;
  } else source.hidden = true;

  const label = data.universe.label
    ? `${data.universe.label} · ${data.universe.scanned} read · ${data.scan.finished_at ? `${data.scan.from_saved ? 'saved scan from' : 'scanned'} ${ago(data.scan.finished_at)}` : 'scanning'}`
    : 'Not scanned yet';
  $('universeLabel').textContent = label;

  const scanning = data.scan.status === 'running';
  $('scanbar').hidden = !scanning && data.scan.status !== 'failed' && !data.scan.from_saved;
  $('scanMessage').textContent = data.scan.message || '';
  const fill = data.scan.total ? Math.min(100, (data.scan.done / data.scan.total) * 100) : scanning ? 6 : 0;
  $('scanFill').style.width = `${fill}%`;
  $('scanBtn').disabled = scanning;
  $('scanBtn').innerHTML = scanning ? '<span class="spin"></span> Scanning' : 'Run scan';

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.setAttribute('aria-selected', String(tab.dataset.view === state.view));
  });

  const panel = $('panel');
  panel.replaceChildren();
  if (state.view === 'candidates') {
    panel.append(candidateView(data.candidates, s, 'At the trigger',
      `Alert candle in force, price within ${s.max_distance_pct}% below the trigger, debt at or below ${s.max_de}.`));
  } else if (state.view === 'approaching') {
    panel.append(candidateView(data.approaching, s, 'Approaching',
      `Between ${s.max_distance_pct}% and ${s.approach_pct}% below the trigger. Debt is checked once a stock enters the narrow band.`));
  } else if (state.view === 'positions') {
    panel.append(positionsView(data.positions));
  } else if (state.view === 'closed') {
    panel.append(closedView(data.closed));
  } else {
    panel.append(ignoredView(data.ignored));
  }

  if (data.scan.errors && data.scan.errors.length && !state.warned) {
    state.warned = true;
    data.scan.errors.forEach((e) => toast(e, 'bad'));
  }
}

/* --------------------------------------------------------------- drawer */
async function openDrawer(symbol) {
  $('drawer').hidden = false;
  $('scrim').hidden = false;
  $('drawerTitle').textContent = symbol;
  $('drawerSub').textContent = 'Loading...';
  $('drawerBody').replaceChildren(el('p', 'muted', 'Reading monthly candles...'));
  $('drawer').focus();
  state.drawer = symbol;
  try {
    const detail = await api(`/api/detail?symbol=${encodeURIComponent(symbol)}`);
    if (state.drawer === symbol) renderDrawer(detail);
  } catch (error) {
    $('drawerBody').replaceChildren(el('p', 'callout bad', error.message));
  }
}

function closeDrawer() {
  $('drawer').hidden = true;
  $('scrim').hidden = true;
  state.drawer = null;
}

function fact(label, value, cls) {
  const box = el('div', 'fact');
  box.append(el('span', '', label), el('strong', cls || '', value));
  return box;
}

function renderDrawer(detail) {
  const setup = detail.setup || {};
  const position = detail.position;
  const candidate = detail.candidate;
  const debt = detail.debt || {};
  const body = $('drawerBody');
  body.replaceChildren();

  $('drawerTitle').textContent = detail.symbol + (debt.name ? '' : '');
  $('drawerSub').textContent = [debt.name, debt.sector].filter(Boolean).join(' · ') ||
    (setup.alert_month ? `Alert candle ${monthName(setup.alert_month)}` : '');

  if (setup.status === 'triggered') {
    body.append(el('p', 'callout warn',
      `The entry already fired on ${setup.triggered_on}, when price crossed ${money(setup.trigger)}.`));
  } else if (setup.status === 'no_alert') {
    body.append(el('p', 'callout quiet',
      'No alert candle: no completed month has its high below the 5 EMA.'));
  } else if (setup.status === 'short_history') {
    body.append(el('p', 'callout quiet', 'Too little monthly history for a trustworthy 5 EMA.'));
  }

  // chart
  if (setup.alert_month) {
    const card = el('div', 'card');
    card.append(el('h3', '', 'Chart · 5 EMA, alert and entry'));
    card.append(zoomChart(detail, setup, {
      trigger: setup.trigger, stop: setup.stop, entry: position ? position.entry : null,
    }, position ? position.entry_date : (setup.triggered_on || null)));
    body.append(card);
  }

  // the setup
  if (setup.alert_month) {
    const card = el('div', 'card');
    card.append(el('h3', '', `Alert candle · ${monthName(setup.alert_month)}`));
    const facts = el('div', 'facts');
    facts.append(
      fact('Alert high', money(setup.alert_high)),
      fact('Alert low', money(setup.alert_low)),
      fact('5 EMA then', money(setup.ema5)),
      fact('Trigger', money(setup.trigger)),
      fact('Stop', money(setup.stop)),
      fact('Risk', `${money(setup.risk)} (${pct(setup.risk_pct, 1)})`),
      fact('Price now', money(candidate ? candidate.ltp : setup.last_close)),
      fact('Away', candidate ? pct(candidate.distance_pct) : '--',
           candidate && candidate.distance_pct <= 1 ? 'pos' : ''),
    );
    card.append(facts);
    if (setup.months_since_alert > 0) {
      card.append(el('p', 'muted small',
        `This alert candle is ${setup.months_since_alert} month(s) old; the months since have had their highs at or above the 5 EMA, so they were ignored.`));
    }
    body.append(card);
  }

  // debt, both sources
  const debtCard = el('div', 'card');
  debtCard.append(el('h3', '', 'Debt check'));
  if (debt.verdict) {
    const top = el('div', 'src-row');
    const badge = el('span', `badge ${debt.verdict}`);
    badge.append(el('i'), document.createTextNode(debt.label || ''));
    top.append(badge, el('b', '', debt.de === null || debt.de === undefined ? 'no figure' : `D/E ${debt.de.toFixed(2)}`));
    debtCard.append(top);
    const row = (name, value, href) => {
      const line = el('div', 'src-row');
      if (href) {
        const link = el('a', '', name);
        link.href = href; link.target = '_blank'; link.rel = 'noreferrer noopener';
        line.append(link);
      } else line.append(el('span', '', name));
      line.append(el('b', '', value));
      return line;
    };
    debtCard.append(
      row('NSE MarketLens', debt.de_nse === null || debt.de_nse === undefined ? '--' : debt.de_nse.toFixed(2), debt.nse_url),
      row('screener.in', debt.de_screener === null || debt.de_screener === undefined ? '--' : debt.de_screener.toFixed(2), debt.screener_url),
    );
    if (debt.interest_cover) debtCard.append(row('Interest cover', `${debt.interest_cover.toFixed(1)}x`));
    if (debt.roce_pct) debtCard.append(row('ROCE', `${debt.roce_pct.toFixed(1)}%`));
    if (debt.promoter_pct) debtCard.append(row('Promoter holding', `${debt.promoter_pct.toFixed(1)}%`));
    if (debt.market_cap_cr) debtCard.append(row('Market cap', `${nf0.format(debt.market_cap_cr)} Cr`));
    if (debt.financial) {
      debtCard.append(el('p', 'callout warn',
        'A lender\'s balance sheet: borrowings are its raw material, so debt to equity does not mean what it means elsewhere. Judge this one yourself.'));
    }
    if (debt.disagree) {
      debtCard.append(el('p', 'callout quiet',
        'The two sources disagree by more than 0.15. The stricter figure is used. screener.in counts lease liabilities as borrowings; NSE does not.'));
    }
    const hasNse = debt.de_nse !== null && debt.de_nse !== undefined;
    const hasScr = debt.de_screener !== null && debt.de_screener !== undefined;
    if (hasNse && !hasScr) {
      debtCard.append(el('p', 'callout quiet',
        'screener.in had no figure for this one, so the verdict rests on NSE MarketLens alone.'));
    } else if (!hasNse && hasScr) {
      debtCard.append(el('p', 'callout quiet',
        'NSE MarketLens had no figure, so the verdict rests on screener.in alone.'));
    }
    if (debt.errors && debt.errors.length) {
      debtCard.append(el('p', 'muted small', debt.errors.join(' · ')));
    }
    const refresh = el('button', 'btn small ghost', 'Re-check now');
    refresh.addEventListener('click', async () => {
      refresh.disabled = true;
      refresh.textContent = 'Checking...';
      try {
        await api('/api/debt', { symbol: detail.symbol });
        await openDrawer(detail.symbol);
        toast('Debt figures refreshed.', 'ok');
      } catch (error) { toast(error.message, 'bad'); refresh.disabled = false; }
    });
    const foot = el('div', 'src-row');
    foot.append(el('span', 'muted small', debt.fetched_at ? `Checked ${ago(debt.fetched_at)}` : ''), refresh);
    debtCard.append(foot);
  } else {
    debtCard.append(el('p', 'muted small', 'No debt figures yet.'));
  }
  body.append(debtCard);

  // live position
  if (position) {
    const card = el('div', 'card');
    card.append(el('h3', '', position.status === 'stopped' ? 'Closed trade' : 'Open trade'));
    const facts = el('div', 'facts');
    facts.append(
      fact('Entry', money(position.entry)),
      fact('Entry date', position.entry_date),
      fact('Initial stop', money(position.stop_initial)),
      fact('Risk', money(position.risk)),
      fact('Now', rmul(position.r_now), signClass(position.r_now)),
      fact('Peak', rmul(position.peak_r)),
      fact('Stop now', money(position.stop_now)),
      fact('Locked', rmul(position.locked_r), signClass(position.locked_r)),
    );
    card.append(facts);
    if (position.entry_reached === false) {
      card.append(el('p', 'callout warn',
        'Price has not traded at or above this entry since the entry date, so this is being tracked as if filled.'));
    }
    if (position.exit) {
      card.append(el('p', position.exit.r > 0 ? 'callout' : 'callout bad',
        `Exited ${position.exit.date} at ${money(position.exit.price)} for ${rmul(position.exit.r)}` +
        (position.exit.gap ? ', gapping below the stop.' : '.')));
    } else {
      card.append(ladderStrip(position));
    }

    const ladderTable = el('table', 'ladder');
    const thead = el('thead');
    const hr = el('tr');
    ['Level', 'Price', 'Stop moves to', 'Locked'].forEach((h) => hr.append(el('th', '', h)));
    thead.append(hr);
    const tbody = el('tbody');
    (position.ladder || []).forEach((step) => {
      const tr = el('tr');
      if (position.peak_r >= step.r && (!position.ladder.find((s) => s.r > step.r && position.peak_r >= s.r))) {
        tr.className = 'now';
      }
      tr.append(el('td', '', `${step.r}R`), el('td', '', money(step.target)),
                el('td', '', money(step.stop)), el('td', '', rmul(step.locked_r)));
      tbody.append(tr);
    });
    ladderTable.append(thead, tbody);
    card.append(ladderTable);

    const actions = el('div', 'sheet-actions');
    if (!position.exit) {
      const exit = el('button', 'btn', 'Record an exit');
      exit.addEventListener('click', () => openClose(position));
      actions.append(exit);
    }
    const edit = el('button', 'btn ghost', 'Edit entry');
    edit.addEventListener('click', () => openTake(position, true));
    const remove = el('button', 'btn ghost danger', 'Stop tracking');
    remove.addEventListener('click', () => doForget(position.id, 'No longer tracking that entry.'));
    actions.append(edit, remove);
    card.append(actions);
    body.append(card);
  } else if (candidate) {
    const card = el('div', 'card');
    card.append(el('h3', '', 'If you take this entry'));
    const ladderTable = el('table', 'ladder');
    const thead = el('thead');
    const hr = el('tr');
    ['Level', 'Price', 'Stop moves to', 'Locked'].forEach((h) => hr.append(el('th', '', h)));
    thead.append(hr);
    const tbody = el('tbody');
    (detail.ladder || []).forEach((step) => {
      const tr = el('tr');
      tr.append(el('td', '', `${step.r}R`), el('td', '', money(step.target)),
                el('td', '', money(step.stop)), el('td', '', rmul(step.locked_r)));
      tbody.append(tr);
    });
    ladderTable.append(thead, tbody);
    card.append(ladderTable);
    const actions = el('div', 'sheet-actions');
    const take = el('button', 'btn primary', 'Take entry');
    take.addEventListener('click', () => openTake(candidate));
    const skip = el('button', 'btn ghost', 'Ignore');
    skip.addEventListener('click', () => doIgnore(candidate));
    actions.append(skip, take);
    card.append(actions);
    body.append(card);
  }
}

/* -------------------------------------------------------------- actions */
let takeTarget = null, takeEditing = false;

function openTake(row, editing = false) {
  takeTarget = row;
  takeEditing = editing;
  const entry = editing ? row.entry : row.trigger;
  const stop = editing ? (row.stop_initial !== undefined ? row.stop_initial : row.stop) : row.stop;
  $('takeTitle').textContent = editing ? `Edit ${row.symbol}` : `Take ${row.symbol}`;
  $('takeIntro').textContent = editing
    ? 'Change what is being tracked. The stop ladder is recalculated from these.'
    : `Defaults to the trigger price (${money(row.trigger)}). Replace it with your actual fill if you got a different price.`;
  $('takeEntry').value = Number(entry).toFixed(2);
  $('takeStop').value = Number(stop).toFixed(2);
  $('takeDate').value = editing ? row.entry_date : new Date().toISOString().slice(0, 10);
  $('takeDate').max = new Date().toISOString().slice(0, 10);
  $('takeNote').value = row.note || '';
  $('takeEntryHint').textContent = editing ? '' : `Alert candle high ${money(row.alert_high || row.trigger)}`;
  $('takeSubmit').textContent = editing ? 'Save changes' : 'Track this entry';
  updateTakeRisk();
  $('takeDialog').showModal();
}

function updateTakeRisk() {
  const entry = parseFloat($('takeEntry').value);
  const stop = parseFloat($('takeStop').value);
  const risk = entry - stop;
  const warn = $('takeWarn');
  if (isFinite(risk) && risk > 0) {
    $('takeRisk').textContent = money(risk);
    $('takeRiskPct').textContent = `${pct((risk / entry) * 100, 1)} of the entry · 2R at ${money(entry + 2 * risk)}`;
    warn.hidden = true;
  } else {
    $('takeRisk').textContent = '--';
    $('takeRiskPct').textContent = '';
    warn.hidden = false;
    warn.className = 'callout bad';
    warn.textContent = 'The stop must sit below the entry price.';
  }
}

async function submitTake() {
  if (!takeTarget) return;
  const payload = {
    symbol: takeTarget.symbol,
    entry: $('takeEntry').value,
    stop: $('takeStop').value,
    entry_date: $('takeDate').value,
    note: $('takeNote').value,
  };
  try {
    if (takeEditing) {
      absorb(await api('/api/entry', { id: takeTarget.id, ...payload }));
      toast(`${takeTarget.symbol} updated.`, 'ok');
    } else {
      absorb(await api('/api/take', payload));
      toast(`Tracking ${takeTarget.symbol} from ${money(parseFloat(payload.entry))}.`, 'ok');
      state.view = 'positions';
      render();
    }
    $('takeDialog').close();
    if (state.drawer) openDrawer(state.drawer);
  } catch (error) {
    toast(error.message, 'bad');
  }
}

async function doIgnore(row) {
  try {
    absorb(await api('/api/ignore', { symbol: row.symbol }));
    toast(`${row.symbol} ignored for ${monthName(row.alert_month)}.`);
    if (state.drawer) closeDrawer();
  } catch (error) { toast(error.message, 'bad'); }
}

async function doForget(id, message) {
  try {
    absorb(await api('/api/forget', { id }));
    toast(message || 'Removed.');
    if (state.drawer) closeDrawer();
  } catch (error) { toast(error.message, 'bad'); }
}

async function doRestore(id) {
  try {
    absorb(await api('/api/restore', { id }));
    toast('Back on the watchlist.', 'ok');
  } catch (error) { toast(error.message, 'bad'); }
}

let closeTarget = null;
function openClose(position) {
  closeTarget = position;
  $('closeIntro').textContent =
    `${position.symbol}: entry ${money(position.entry)}, stop now ${money(position.stop_now)}. ` +
    'Recording an exit here only updates your record.';
  $('closePrice').value = Number(position.ltp || position.stop_now).toFixed(2);
  $('closeDate').value = new Date().toISOString().slice(0, 10);
  $('closeDate').max = new Date().toISOString().slice(0, 10);
  $('closeNote').value = '';
  $('closeDialog').showModal();
}

async function submitClose() {
  if (!closeTarget) return;
  try {
    absorb(await api('/api/close', {
      id: closeTarget.id, price: $('closePrice').value,
      date: $('closeDate').value, note: $('closeNote').value,
    }));
    $('closeDialog').close();
    toast(`Exit recorded for ${closeTarget.symbol}.`, 'ok');
    state.view = 'closed';
    render();
    if (state.drawer) openDrawer(state.drawer);
  } catch (error) { toast(error.message, 'bad'); }
}

/* ------------------------------------------------------------- settings */
function openSettings() {
  const s = state.data.settings;
  $('setBand').value = s.max_distance_pct;
  $('setApproach').value = s.approach_pct;
  $('setBuffer').value = s.buffer_pct;
  $('setTop').value = s.top_n;
  $('setDe').value = s.max_de;
  $('setHistory').value = s.history_days;
  $('setSource').value = s.source;
  $('setAuto').value = s.auto_scan_minutes;
  $('setLowDebt').checked = !!s.low_debt_only;
  $('settingsDialog').showModal();
}

async function submitSettings() {
  const payload = {
    max_distance_pct: $('setBand').value, approach_pct: $('setApproach').value,
    buffer_pct: $('setBuffer').value, top_n: $('setTop').value, max_de: $('setDe').value,
    history_days: $('setHistory').value, source: $('setSource').value,
    auto_scan_minutes: $('setAuto').value, low_debt_only: $('setLowDebt').checked,
  };
  try {
    const before = state.data.settings;
    absorb(await api('/api/settings', payload));
    $('settingsDialog').close();
    const needsScan = ['buffer_pct', 'top_n', 'history_days', 'source']
      .some((k) => String(before[k]) !== String(state.data.settings[k]));
    toast(needsScan ? 'Saved. Run a scan to apply the new buffer or universe.' : 'Saved.', 'ok');
  } catch (error) { toast(error.message, 'bad'); }
}

/* ----------------------------------------------------------- scan loops */
async function startScan() {
  try {
    await api('/api/scan', {});
    state.warned = false;
    toast('Scan started.');
    pollScan();
  } catch (error) { toast(error.message, 'bad'); }
}

let polling = null;
function pollScan() {
  clearInterval(polling);
  polling = setInterval(async () => {
    try {
      const payload = await api('/api/state');
      absorb(payload);
      if (payload.scan.status !== 'running') {
        clearInterval(polling);
        polling = null;
        if (payload.scan.status === 'done') toast(payload.scan.message, 'ok');
        if (payload.scan.status === 'failed') toast(payload.scan.message, 'bad');
      }
    } catch { /* keep polling */ }
  }, 1200);
}

async function refreshPrices(quiet = false) {
  if (state.busy) return;
  state.busy = true;
  const button = $('refreshPrices');
  if (!quiet) button.disabled = true;
  try {
    absorb(await api('/api/prices'));
    if (!quiet) toast('Prices updated.', 'ok');
  } catch (error) {
    if (!quiet) toast(error.message, 'bad');
  } finally {
    state.busy = false;
    button.disabled = false;
  }
}

/* ----------------------------------------------------------------- boot */
function wire() {
  $('scanBtn').addEventListener('click', startScan);
  $('refreshPrices').addEventListener('click', () => refreshPrices());
  $('settingsBtn').addEventListener('click', openSettings);
  $('settingsSubmit').addEventListener('click', submitSettings);
  $('takeSubmit').addEventListener('click', submitTake);
  $('closeSubmit').addEventListener('click', submitClose);
  $('takeEntry').addEventListener('input', updateTakeRisk);
  $('takeStop').addEventListener('input', updateTakeRisk);
  $('drawerClose').addEventListener('click', closeDrawer);
  $('scrim').addEventListener('click', closeDrawer);
  document.querySelectorAll('[data-close]').forEach((button) => {
    button.addEventListener('click', () => button.closest('dialog').close());
  });
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => { state.view = tab.dataset.view; render(); });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !$('drawer').hidden) closeDrawer();
  });

  $('themeBtn').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('ema5-theme', next); } catch { /* private mode */ }
  });
  try {
    const saved = localStorage.getItem('ema5-theme');
    if (saved) document.documentElement.dataset.theme = saved;
  } catch { /* private mode */ }

  // Live prices while the market is open and the tab is in front.
  setInterval(() => {
    if (document.hidden || !state.data) return;
    if (!state.data.market.open) return;
    if (state.data.scan.status === 'running') return;
    if (!state.data.totals.candidates && !state.data.totals.open && !state.data.totals.approaching) return;
    refreshPrices(true);
  }, 25000);
}

(async function boot() {
  wire();
  try {
    absorb(await api('/api/state'));
    if (state.data && state.data.scan.status === 'running') pollScan();
    if (state.data && state.data.scan.status === 'idle') {
      toast('Run a scan to read this month\'s alert candles.');
    }
  } catch (error) {
    toast(error.message, 'bad');
  }
})();
