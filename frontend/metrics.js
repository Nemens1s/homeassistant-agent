// Metrics tab. Fetches the telemetry summary + recent-requests feed and draws
// plain CSS/HTML panels (no charting library, no CDN). All fetch paths are
// relative (no leading slash) so they work behind Home Assistant's ingress
// prefix, matching the rule in app.js / main.py.

const daysSelect = document.getElementById('days');
const refreshBtn = document.getElementById('refresh');
const banner = document.getElementById('disabled-banner');
const loadMoreBtn = document.getElementById('load-more');

let nextCursor = null;

async function fetchJson(path) {
  const resp = await fetch(path);
  if (resp.status === 503) {
    return { disabled: true };
  }
  if (!resp.ok) {
    throw new Error('HTTP ' + resp.status);
  }
  return await resp.json();
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

// --- Panels -----------------------------------------------------------------
function renderBars(container, entries) {
  container.innerHTML = '';
  if (!entries.length) {
    container.appendChild(el('div', 'empty', 'No data yet.'));
    return;
  }
  let max = 0;
  for (const entry of entries) {
    if (entry.count > max) max = entry.count;
  }
  for (const entry of entries) {
    const row = el('div', 'bar-row');
    row.appendChild(el('div', 'bar-label', entry.label));
    const track = el('div', 'bar-track');
    const fill = el('div', 'bar-fill');
    const pct = max > 0 ? Math.round((entry.count / max) * 100) : 0;
    fill.style.width = pct + '%';
    track.appendChild(fill);
    row.appendChild(track);
    row.appendChild(el('div', 'bar-count', String(entry.count)));
    container.appendChild(row);
  }
}

function toEntries(counts) {
  const entries = [];
  for (const key of Object.keys(counts || {})) {
    entries.push({ label: key, count: counts[key] });
  }
  entries.sort(function (a, b) { return b.count - a.count; });
  return entries;
}

function fmtMs(value) {
  return value === null || value === undefined ? '—' : String(Math.round(value));
}

function fmtPct(value) {
  return value === null || value === undefined ? '—' : (value * 100).toFixed(0) + '%';
}

function renderCards(summary) {
  const cards = document.getElementById('stat-cards');
  cards.innerHTML = '';
  let totalRequests = 0;
  for (const key of Object.keys(summary.requests_by_path || {})) {
    totalRequests += summary.requests_by_path[key];
  }
  const labels = summary.label_counts || { total: 0, thumbs_up: 0, thumbs_down: 0 };

  const items = [
    { value: String(totalRequests), label: 'requests' },
    { value: fmtPct(summary.fast_path_hit_rate), label: 'fast-path hit rate' },
    { value: String(labels.thumbs_up || 0) + ' / ' + String(labels.thumbs_down || 0), label: '👍 / 👎' },
    { value: String(labels.total || 0), label: 'labels' },
  ];
  for (const item of items) {
    const card = el('div', 'card');
    card.appendChild(el('div', 'value', item.value));
    card.appendChild(el('div', 'label', item.label));
    cards.appendChild(card);
  }
}

function renderLatency(summary) {
  const table = document.getElementById('latency-table');
  table.innerHTML = '';
  const head = el('tr');
  ['path', 'dur p50', 'dur p95', 'ttft p50', 'ttft p95'].forEach(function (h) {
    head.appendChild(el('th', null, h));
  });
  table.appendChild(head);

  const paths = Object.keys(summary.duration_p50_by_path || {});
  if (!paths.length) {
    const row = el('tr');
    const cell = el('td', 'empty', 'No data yet.');
    cell.colSpan = 5;
    row.appendChild(cell);
    table.appendChild(row);
    return;
  }
  for (const path of paths) {
    const row = el('tr');
    row.appendChild(el('td', null, path));
    row.appendChild(el('td', null, fmtMs(summary.duration_p50_by_path[path])));
    row.appendChild(el('td', null, fmtMs(summary.duration_p95_by_path[path])));
    row.appendChild(el('td', null, fmtMs((summary.ttft_p50_by_path || {})[path])));
    row.appendChild(el('td', null, fmtMs((summary.ttft_p95_by_path || {})[path])));
    table.appendChild(row);
  }
}

function renderSummary(summary) {
  renderCards(summary);
  renderBars(document.getElementById('path-bars'), toEntries(summary.requests_by_path));
  renderBars(document.getElementById('outcome-bars'), toEntries(summary.outcome_counts));
  renderLatency(summary);
  const labels = summary.label_counts || {};
  renderBars(document.getElementById('label-bars'), [
    { label: '👍 up', count: labels.thumbs_up || 0 },
    { label: '👎 down', count: labels.thumbs_down || 0 },
  ]);
}

// --- Recent requests --------------------------------------------------------
function renderRequestDetail(row) {
  const parts = [];
  for (const mc of row.model_calls || []) {
    let line = 'step ' + mc.step + ' · ' + (mc.model || '?');
    if (mc.fast_path) line += ' [fast_path]';
    if (mc.duration_ms !== null && mc.duration_ms !== undefined) line += ' · ' + Math.round(mc.duration_ms) + 'ms';
    if (mc.thinking_text) line += '\n  think: ' + mc.thinking_text;
    if (mc.content_text) line += '\n  reply: ' + mc.content_text;
    parts.push(line);
  }
  for (const tc of row.tool_calls || []) {
    let line = 'tool ' + tc.tool + ' · ' + tc.status;
    if (tc.error_code) line += ' (' + tc.error_code + ')';
    if (tc.args_json) line += '\n  args: ' + tc.args_json;
    parts.push(line);
  }
  if (!parts.length) parts.push('(no steps recorded)');
  return parts.join('\n');
}

function appendRequestRows(rows) {
  const table = document.getElementById('requests-table');
  for (const row of rows) {
    const tr = el('tr', 'req-row');
    tr.appendChild(el('td', null, (row.ts_start || '').replace('T', ' ').slice(0, 19)));
    tr.appendChild(el('td', null, row.path || ''));
    tr.appendChild(el('td', 'outcome-' + (row.outcome || ''), row.outcome || ''));
    tr.appendChild(el('td', null, fmtMs(row.duration_ms)));
    tr.appendChild(el('td', null, (row.input_text || '').slice(0, 60)));
    table.appendChild(tr);

    const detail = el('tr', 'req-detail');
    detail.hidden = true;
    const cell = el('td', null, renderRequestDetail(row));
    cell.colSpan = 5;
    detail.appendChild(cell);
    table.appendChild(detail);

    tr.addEventListener('click', function () {
      detail.hidden = !detail.hidden;
    });
  }
}

function resetRequestsTable() {
  const table = document.getElementById('requests-table');
  table.innerHTML = '';
  const head = el('tr');
  ['when', 'path', 'outcome', 'ms', 'utterance'].forEach(function (h) {
    head.appendChild(el('th', null, h));
  });
  table.appendChild(head);
}

// --- Load orchestration -----------------------------------------------------
async function loadAll() {
  const days = daysSelect.value;
  try {
    const summary = await fetchJson('api/telemetry/summary?days=' + days);
    if (summary.disabled) {
      banner.hidden = false;
      return;
    }
    banner.hidden = true;
    renderSummary(summary);

    resetRequestsTable();
    nextCursor = null;
    await loadMore();
  } catch (err) {
    banner.hidden = false;
    banner.textContent = 'Failed to load metrics: ' + err.message;
  }
}

async function loadMore() {
  let path = 'api/telemetry/requests?limit=20';
  if (nextCursor) path += '&cursor=' + encodeURIComponent(nextCursor);
  const data = await fetchJson(path);
  if (data.disabled) {
    banner.hidden = false;
    return;
  }
  appendRequestRows(data.rows || []);
  nextCursor = data.next_cursor || null;
  loadMoreBtn.hidden = !nextCursor;
}

refreshBtn.addEventListener('click', loadAll);
daysSelect.addEventListener('change', loadAll);
loadMoreBtn.addEventListener('click', loadMore);

loadAll();
