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

/* -------------------------------------------------------- monthly chart */
function monthlyChart(months, setup, marks) {
  const NS = 'http://www.w3.org/2000/svg';
  const W = 560, H = 250, padT = 12, padB = 24, padR = 62, padL = 4;
  const wrap = el('div', 'chart-wrap');
  if (!months || months.length < 2) {
    wrap.append(el('p', 'muted small', 'Not enough monthly history to draw a chart.'));
    return wrap;
  }
  const levels = [marks.trigger, marks.stop].filter((v) => v);
  const lows = months.map((m) => m.low).concat(levels);
  const highs = months.map((m) => m.high).concat(levels);
  const lo = Math.min(...lows), hi = Math.max(...highs);
  const pad = (hi - lo) * 0.06 || 1;
  const yMin = lo - pad, yMax = hi + pad;
  const y = (v) => padT + ((yMax - v) / (yMax - yMin)) * (H - padT - padB);
  const plotW = W - padL - padR;
  const band = plotW / months.length;
  const bodyW = Math.max(3, Math.min(14, band * 0.58));

  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('width', '100%');
  svg.setAttribute('role', 'img');
  const add = (tag, attrs, parent) => {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    (parent || svg).append(node);
    return node;
  };
  const cx = (i) => padL + band * (i + 0.5);

  // alert candle highlight, behind everything
  const alertIndex = months.findIndex((m) => m.month === setup.alert_month);
  if (alertIndex >= 0) {
    add('rect', { x: padL + band * alertIndex, y: padT, width: band, height: H - padT - padB,
                  fill: 'var(--violet-wash)' });
  }

  // reference levels: dashed so a level never reads as data
  const level = (value, color, label) => {
    if (!value) return;
    add('line', { x1: padL, x2: W - padR, y1: y(value), y2: y(value), stroke: color,
                  'stroke-width': 1.5, 'stroke-dasharray': '5 4' });
    const text = add('text', { x: W - padR + 6, y: y(value) + 3.5, fill: 'var(--ink-2)', 'font-size': 10 });
    text.textContent = `${label} ${nf0.format(value)}`;
  };
  level(marks.trigger, 'var(--good)', 'Trigger');
  level(marks.stop, 'var(--critical)', 'Stop');
  if (marks.entry) level(marks.entry, 'var(--accent)', 'Entry');

  // candles
  months.forEach((m, i) => {
    const up = m.close >= m.open;
    const colour = up ? 'var(--good)' : 'var(--critical)';
    add('line', { x1: cx(i), x2: cx(i), y1: y(m.high), y2: y(m.low), stroke: colour, 'stroke-width': 1 });
    const top = y(Math.max(m.open, m.close));
    const height = Math.max(1.5, Math.abs(y(m.open) - y(m.close)));
    const rect = add('rect', { x: cx(i) - bodyW / 2, y: top, width: bodyW, height, rx: 1.5, fill: colour });
    if (m.forming) { rect.setAttribute('fill-opacity', '0.45'); }
    if (i === alertIndex) {
      add('rect', { x: cx(i) - bodyW / 2 - 2, y: y(m.high) - 2, width: bodyW + 4,
                    height: y(m.low) - y(m.high) + 4, rx: 3, fill: 'none',
                    stroke: 'var(--violet)', 'stroke-width': 1.5 });
    }
    const hit = add('rect', { x: padL + band * i, y: padT, width: band, height: H - padT - padB,
                              fill: 'transparent' });
    const title = document.createElementNS(NS, 'title');
    title.textContent = `${monthName(m.month)}${m.forming ? ' (in progress)' : ''}\nO ${money(m.open)}  H ${money(m.high)}\nL ${money(m.low)}  C ${money(m.close)}` +
      (m.ema5 ? `\n5 EMA ${money(m.ema5)}` : '');
    hit.append(title);
  });

  // the 5 EMA, 2px with round joins
  const points = [];
  months.forEach((m, i) => { if (m.ema5) points.push(`${cx(i)},${y(m.ema5)}`); });
  if (points.length > 1) {
    add('polyline', { points: points.join(' '), fill: 'none', stroke: 'var(--accent)',
                      'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' });
  }

  // month ticks, thinned so labels cannot collide
  const every = Math.ceil(months.length / 8);
  const last = months.length - 1;
  months.forEach((m, i) => {
    // the final month always gets a tick, so drop the one before it if they would touch
    if (i % every !== 0 && i !== last) return;
    if (i !== last && last - i < every / 2) return;
    const text = add('text', { x: cx(i), y: H - 8, 'text-anchor': 'middle',
                               fill: 'var(--muted)', 'font-size': 9 });
    const [year, month] = m.month.split('-');
    text.textContent = month === '01' ? year : monthName(m.month).slice(0, 3);
  });
  add('line', { x1: padL, x2: W - padR, y1: H - padB, y2: H - padB, stroke: 'var(--baseline)', 'stroke-width': 1 });

  svg.setAttribute('aria-label',
    `Monthly candles with the 5 EMA. Alert candle ${monthName(setup.alert_month)}, trigger ${money(marks.trigger)}, stop ${money(marks.stop)}.`);
  wrap.append(svg);

  const legend = el('div', 'legend');
  const key = (cls, label, style) => {
    const span = el('span');
    const dot = el('i', cls);
    Object.assign(dot.style, style);
    span.append(dot, document.createTextNode(label));
    return span;
  };
  legend.append(
    key('line', '5 EMA (monthly)', { background: 'var(--accent)' }),
    key('', 'Month up', { background: 'var(--good)' }),
    key('', 'Month down', { background: 'var(--critical)' }),
    key('', 'Alert candle', { background: 'var(--violet-wash)', border: '1.5px solid var(--violet)' }),
    key('line', 'Trigger / stop', { background: 'var(--baseline)' }),
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
    card.append(el('h3', '', 'Monthly candles and the 5 EMA'));
    card.append(monthlyChart(detail.months, setup, {
      trigger: setup.trigger, stop: setup.stop, entry: position ? position.entry : null,
    }));
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
