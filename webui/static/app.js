/* ════════════════════════════════════════════════════════════════
   pySdp WebUI — app.js
   ════════════════════════════════════════════════════════════════ */

const API   = '/api/sdpcli';
const FILES = '/api/files';
const DATA  = '/api/data';
const JOBS  = '/api/jobs';

// ── State ─────────────────────────────────────────────────────────────────────
const state = {
  serverAlive:      false,
  device:           'Disconnected',
  connectedDevice:  null,
  session:          null,
  captures:         [],   // [{sdpPath, captureId, sessionId}]
  lastAnalysisDir:      null, // captureDir from last completed analysis
  activeAnalysisJobId:  null, // job ID of running analysis (for cancel)
};

// One polling timer per operation section
const timers = { device: null, connect: null, launch: null, capture: null, analysis: null, logs: null };

// ── Explorer multi-tab state ─────────────────────────────────────────────────
const explorerTabs = {};  // tabId -> { sdpPath, sdpName, snapState, explorerState, questionsState, questionsCtrl, subTab, catFilterSel, catFilterAll }
let activeTabId = 'home';

function _hashString(str) {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    const ch = str.charCodeAt(i);
    hash = ((hash << 5) - hash) + ch;
    hash |= 0;
  }
  return 'exp_' + Math.abs(hash).toString(36);
}

function getTabState(tabId) { return explorerTabs[tabId]; }

function getTabEl(tabId) {
  return document.getElementById(`tab-${tabId}`);
}

// ── Log state ─────────────────────────────────────────────────────────────────
const logState = {
  filter:         'all',  // 'all' | 'info' | 'warning' | 'error'
  lastSeenId:     0,      // highest backend record ID the user has seen
  allRecords:     [],     // latest fetch (unfiltered, backend + frontend mixed)
  frontendUnread: 0,      // unseen frontend warn/error count
};

// ── Console capture ───────────────────────────────────────────────────────────

let _frontendLogId = -1;

function _interceptConsole() {
  const orig = { log: console.log, warn: console.warn, error: console.error };
  const lvl  = { log: 'info', warn: 'warning', error: 'error' };
  ['log', 'warn', 'error'].forEach(m => {
    console[m] = function (...args) {
      orig[m].apply(console, args);
      const msg = args.map(a =>
        (a instanceof Error) ? a.message : (typeof a === 'object' ? JSON.stringify(a) : String(a))
      ).join(' ');
      _pushFrontendLog(lvl[m], '[JS] ' + msg);
    };
  });
}

function _pushFrontendLog(level, message) {
  const rec = { id: _frontendLogId--, time: new Date().toISOString(), level, message };
  logState.allRecords.unshift(rec);
  if (logState.allRecords.length > 300) logState.allRecords.pop();

  const logsActive = document.getElementById('tab-logs')?.classList.contains('active');
  if (logsActive) {
    renderLogs();
  } else if (level === 'error' || level === 'warning') {
    logState.frontendUnread++;
    const backendUnread = logState.allRecords.filter(
      r => r.id > 0 && r.id > logState.lastSeenId && (r.level === 'error' || r.level === 'warning')
    ).length;
    updateLogBadge(backendUnread + logState.frontendUnread);
  }
}

// ── Low-level fetch helpers ───────────────────────────────────────────────────

async function apiPost(path, body = {}) {
  const res = await fetch(path, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(body),
  });
  return res.json();
}

async function apiGet(path) {
  const res = await fetch(path, { cache: 'no-store' });
  return res.json();
}

async function apiDelete(path) {
  const res = await fetch(path, { method: 'DELETE' });
  return res.json();
}

// ── SSE pause/resume (free connection slot when snapshot modal is open) ────────

function _pauseSSE() {
  if (_sse) { _sse.close(); _sse = null; }
}
function _resumeSSE() {
  setTimeout(_initSSE, 1000);
}

// ── SSE real-time updates ─────────────────────────────────────────────────────

let _sse = null;
function _initSSE() {
  if (_sse) return;
  _sse = new EventSource('/api/events');
  _sse.addEventListener('connected', () => console.log('[SSE] connected'));
  _bindSSEEvents();
}
function _sseRefreshData() {
  if (activeTabId !== 'home' && activeTabId !== 'logs' && explorerTabs[activeTabId]) {
    const ts = explorerTabs[activeTabId];
    if (ts.subTab === 'questions' && ts.snapState.snapshotId) {
      fetchQuestionsData(activeTabId);
    } else if (ts.subTab === 'explorer' && ts.explorerState.snapshotId) {
      loadExplorerDCs(activeTabId);
    }
  }
}

function _bindSSEEvents() {
  _sse.addEventListener('label_changed', () => _sseRefreshData());
  _sse.addEventListener('labels_changed', () => _sseRefreshData());
  _sse.addEventListener('ingest_done', () => _sseRefreshData());
  _sse.addEventListener('report_done', () => _sseRefreshData());
  _sse.addEventListener('pipeline_done', () => {
    _sseRefreshData();
    if (typeof scanSdpFiles === 'function') scanSdpFiles();
  });
}

// ── Progress UI ───────────────────────────────────────────────────────────────

function showProg(id, pct, phase) {
  const wrap  = document.getElementById(`${id}-prog`);
  const fill  = document.getElementById(`${id}-fill`);
  const label = document.getElementById(`${id}-plabel`);
  wrap.classList.remove('hidden');
  fill.style.width = `${Math.max(pct, 2)}%`;
  label.textContent = phase ? `${pct}% — ${phase}` : `${pct}%`;
}

function hideProg(id) {
  document.getElementById(`${id}-prog`).classList.add('hidden');
}

// ── Status messages ───────────────────────────────────────────────────────────

function setMsg(id, type, text) {
  const el = document.getElementById(`${id}-msg`);
  el.className = `status-msg s-${type}`;
  el.textContent = text;
}

// ── Job polling ───────────────────────────────────────────────────────────────

function pollJob(section, jobId, onTick, onDone, onError) {
  clearInterval(timers[section]);
  timers[section] = setInterval(async () => {
    let res;
    try {
      res = await apiGet(`${API}/jobs/${jobId}`);
    } catch (err) {
      clearInterval(timers[section]);
      onError('Network error: ' + err.message);
      return;
    }
    if (!res.ok) {
      clearInterval(timers[section]);
      onError(res.error || 'Job query failed');
      return;
    }
    const job = res.data;
    onTick(job);
    if (job.status === 'Completed') {
      clearInterval(timers[section]);
      onDone(job);
    } else if (job.status === 'Failed' || job.status === 'Cancelled') {
      clearInterval(timers[section]);
      onError(job.error || job.status);
    }
  }, 2000);
}

// ── Device status polling ─────────────────────────────────────────────────────

function startDevicePoll() {
  clearInterval(timers.device);
  timers.device = setInterval(syncDevice, 3000);
  syncDevice();   // immediate first fetch
}

async function syncDevice() {
  // 1. Liveness check via /api/status
  let alive = false;
  try {
    const sr = await apiGet(`${API}/status`);
    alive = sr?.ok === true;
  } catch { /* connection refused */ }

  if (!alive) {
    if (state.serverAlive) {          // transition: online → offline
      state.serverAlive = false;
      state.device      = 'Disconnected';
      state.connectedDevice = null;
      state.session         = null;
      refreshHeader();
      refreshSteps();
    }
    setBadge('server-badge', 'err', '● SDPCLI: Offline');
    return;
  }

  // 2. Device state
  let res;
  try {
    res = await apiGet(`${API}/device`);
  } catch {
    setBadge('server-badge', 'warn', '● SDPCLI: OK');
    return;
  }

  const wasOffline = !state.serverAlive;
  const prevDevice = state.device;
  state.serverAlive = true;
  setBadge('server-badge', 'ok', '● SDPCLI: OK');

  if (res.ok && res.data) {
    state.device          = res.data.status;
    state.connectedDevice = res.data.connectedDevice || null;
    state.session         = res.data.session || null;
    refreshHeader();
    refreshSteps();
    if (wasOffline) console.log('SDPCLI server reconnected');
    if (prevDevice !== state.device) onDeviceStateChange(prevDevice, state.device);
  }
}

function onDeviceStateChange(from, to) {
  // App process killed: SessionActive → Connected
  if (from === 'SessionActive' && to === 'Connected') {
    setMsg('launch', 'warn', 'App process ended — session closed');
  }
  // Full disconnect
  if (to === 'Disconnected' && from !== 'Disconnected') {
    setMsg('connect', 'info', 'Disconnected');
    setMsg('launch',  '',    '');
  }
}

function setBadge(id, cls, text) {
  const el = document.getElementById(id);
  el.className = `badge badge-${cls}`;
  el.textContent = text;
}

// ── Header & step gating ──────────────────────────────────────────────────────

const STATUS_BADGE = {
  Disconnected:  ['gray',   'Disconnected'],
  Connecting:    ['warn',   'Connecting…'],
  Connected:     ['ok',     'Connected'],
  Launching:     ['warn',   'Launching…'],
  SessionActive: ['active', 'Session Active'],
  Capturing:     ['warn',   'Capturing…'],
};

function refreshHeader() {
  const [cls, label] = STATUS_BADGE[state.device] || ['gray', state.device];
  const suffix = state.connectedDevice ? ` · ${state.connectedDevice}` : '';
  setBadge('device-badge', cls, label + suffix);
}

function refreshSteps() {
  const alive = state.serverAlive;
  const s     = state.device;
  // All buttons off when server is offline
  setCardEnabled('card-connect', alive);
  setCardEnabled('card-launch',  alive && s === 'Connected');
  setCardEnabled('card-capture', alive && s === 'SessionActive');
  setBtn('btn-connect',    alive && s === 'Disconnected');
  setBtn('btn-disconnect', alive && s !== 'Disconnected');
  setBtn('btn-launch',     alive && s === 'Connected');
  setBtn('btn-capture',    alive && s === 'SessionActive');
}

function setCardEnabled(id, enabled) {
  document.getElementById(id).classList.toggle('disabled', !enabled);
}

function setBtn(id, enabled) {
  document.getElementById(id).disabled = !enabled;
}

// ── Device / package / activity dropdowns ─────────────────────────────────────

async function refreshDeviceList() {
  const sel = document.getElementById('device-id');
  try {
    const res = await apiGet(`${API}/devices`);
    if (!res.ok) return;
    const current = sel.value;
    sel.innerHTML = '<option value="">— auto-select —</option>';
    (res.data || []).forEach(d => {
      const opt = document.createElement('option');
      opt.value = d.serial;
      opt.textContent = `${d.serial}  (${d.state})`;
      if (d.state !== 'device') opt.disabled = true;
      sel.appendChild(opt);
    });
    if (current) sel.value = current;
  } catch (_) {}
}

async function refreshPackageList() {
  const serial = document.getElementById('device-id').value.trim();
  const sel = document.getElementById('pkg');
  try {
    const url = serial ? `${API}/app/packages?serial=${encodeURIComponent(serial)}` : `${API}/app/packages`;
    const res = await apiGet(url);
    if (!res.ok) return;
    const current = sel.value;
    sel.innerHTML = '<option value="">— select package —</option>';
    (res.data || []).forEach(pkg => {
      const opt = document.createElement('option');
      opt.value = pkg;
      opt.textContent = pkg;
      sel.appendChild(opt);
    });
    if (current) sel.value = current;
    document.getElementById('activity').innerHTML = '<option value="">— default launch —</option>';
  } catch (_) {}
}

async function onPkgChange() {
  const serial  = document.getElementById('device-id').value.trim();
  const pkg     = document.getElementById('pkg').value.trim();
  const actSel  = document.getElementById('activity');
  actSel.innerHTML = '<option value="">— default launch —</option>';
  if (!pkg) return;
  try {
    const url = `${API}/app/activities?package=${encodeURIComponent(pkg)}` +
                (serial ? `&serial=${encodeURIComponent(serial)}` : '');
    const res = await apiGet(url);
    if (!res.ok) return;
    (res.data || []).forEach(act => {
      const opt = document.createElement('option');
      opt.value = act;
      opt.textContent = act;
      actSel.appendChild(opt);
    });
  } catch (_) {}
}

// ── Connect ───────────────────────────────────────────────────────────────────

async function doConnect() {
  const deviceId = document.getElementById('device-id').value.trim();
  setBtn('btn-connect', false);
  setMsg('connect', 'info', 'Submitting…');

  let res;
  try {
    res = await apiPost(`${API}/connect`, deviceId ? { deviceId } : {});
  } catch (err) {
    setMsg('connect', 'error', err.message);
    setBtn('btn-connect', true);

    return;
  }
  if (!res.ok) {
    setMsg('connect', 'error', res.error);
    setBtn('btn-connect', true);

    return;
  }

  const jobId = res.data.jobId;
  showProg('connect', 0, 'initializing_sdk');
  setMsg('connect', 'info', `Job ${jobId}`);


  pollJob('connect', jobId,
    job => showProg('connect', job.progress, job.phase),
    job => {
      hideProg('connect');
      const devId = job.result?.deviceId || '';
      setMsg('connect', 'success', `Connected${devId ? ': ' + devId : ''}`);
      setBtn('btn-disconnect', true);
      setBtn('btn-connect', false);
      refreshPackageList();
    },
    err => {
      hideProg('connect');
      setMsg('connect', 'error', err);
      setBtn('btn-connect', true);
    }
  );
}

async function doDisconnect() {
  setBtn('btn-disconnect', false);
  let res;
  try {
    res = await apiPost(`${API}/disconnect`, {});
  } catch (err) {
    setMsg('connect', 'error', err.message);
    return;
  }
  if (res.ok) {
    setMsg('connect', 'info', 'Disconnected');
    state.device = 'Disconnected';
    refreshHeader();
    refreshSteps();
  } else {
    setMsg('connect', 'error', res.error);
    setBtn('btn-disconnect', true);
  }
}

// ── Launch ────────────────────────────────────────────────────────────────────

async function doLaunch() {
  const pkg = document.getElementById('pkg').value.trim();
  const act = document.getElementById('activity').value.trim();
  if (!pkg) { setMsg('launch', 'error', 'Package is required'); return; }

  // act from /api/app/activities is already "pkg/activity" format, use as-is
  const packageActivity = act ? act : pkg;
  const renderingApi = parseInt(document.querySelector('input[name="rendering-api"]:checked')?.value ?? '8');
  setBtn('btn-launch', false);
  setMsg('launch', 'info', 'Submitting…');

  let res;
  try {
    res = await apiPost(`${API}/session/launch`, { packageActivity, renderingApi });
  } catch (err) {
    setMsg('launch', 'error', err.message);
    setBtn('btn-launch', state.device === 'Connected');

    return;
  }
  if (!res.ok) {
    setMsg('launch', 'error', res.error);
    setBtn('btn-launch', state.device === 'Connected');

    return;
  }

  const jobId = res.data.jobId;
  showProg('launch', 0, 'launching');
  setMsg('launch', 'info', `Job ${jobId}`);
  pollJob('launch', jobId,
    job => showProg('launch', job.progress, job.phase),
    _job => {
      hideProg('launch');
      setMsg('launch', 'success', 'Session active — ready to capture');
    },
    err => {
      hideProg('launch');
      setMsg('launch', 'error', err);
      setBtn('btn-launch', state.device === 'Connected');
    }
  );
}

// ── Capture ───────────────────────────────────────────────────────────────────

async function doCapture() {
  const label = document.getElementById('cap-label').value.trim();
  const projectId = document.getElementById('capture-project').value;
  const versionId = document.getElementById('capture-version').value;
  setBtn('btn-capture', false);
  setMsg('capture', 'info', 'Submitting…');

  const body = label ? { label } : {};
  let res;
  try {
    res = await apiPost(`${API}/capture`, body);
  } catch (err) {
    setMsg('capture', 'error', err.message);

    syncDevice();
    return;
  }
  if (!res.ok) {
    setMsg('capture', 'error', res.error);

    syncDevice();
    return;
  }

  const jobId = res.data.jobId;
  showProg('capture', 0, 'starting_capture');
  setMsg('capture', 'info', `Job ${jobId}`);
  pollJob('capture', jobId,
    job => showProg('capture', job.progress, job.phase),
    job => {
      hideProg('capture');
      const r = job.result || {};
      setMsg('capture', 'success', `Done  captureId: ${r.captureId ?? '—'}`);
      if (r.captureId != null) {
        addCaptureRow(r);
        state.captures.push(r);
      }
      syncDevice();
      // Ingest new SDP into DB, then refresh list
      if (r.sdpPath) {
        apiPost(`${FILES}/sdp/ingest`, { path: r.sdpPath, project_id: projectId || undefined, version_id: versionId || undefined }).catch(() => {});
      }
      if (document.getElementById('sdp-dir').value) scanSdpFiles();
    },
    err => {
      hideProg('capture');
      setMsg('capture', 'error', err);
      syncDevice();
    }
  );
}

function normPath(p) {
  return p ? p.replace(/\\/g, '/') : p;
}

function addCaptureRow(capture) {
  const list = document.getElementById('captures-list');
  const row  = document.createElement('div');
  row.className = 'capture-item';
  const path = normPath(capture.sdpPath) || '—';

  const tag = document.createElement('span');
  tag.className   = 'capture-tag';
  tag.textContent = `# ${capture.captureId}`;

  const pathSpan = document.createElement('span');
  pathSpan.className   = 'capture-path';
  pathSpan.title       = path;
  pathSpan.textContent = path;

  const btn = document.createElement('button');
  btn.className   = 'btn-secondary btn-sm';
  btn.textContent = 'Analyze →';
  btn.addEventListener('click', () => goAnalyze(path, capture.captureId));

  row.appendChild(tag);
  row.appendChild(pathSpan);
  row.appendChild(btn);
  list.prepend(row);
}

// ── Analysis ──────────────────────────────────────────────────────────────────

// sdpPath → captureDir, persists across file switches
const sdpAnalysisCache = {};
// sdpPath → api type ('Vulkan' | 'GLES')
const sdpApiCache = {};
// sdpPath → full file info {app, api, project_id, version_id, ...}
const sdpInfoCache = {};

const ALL_TARGETS     = ['screenshot','ingest','dc','shaders','textures','buffers','label','metrics','status','topdc','analysis'];
const DEFAULT_TARGETS = new Set(ALL_TARGETS);

// Targets handled by C# (SDK P/Invoke — must run on SDPCLI server)
const CS_TARGETS = new Set(['dc','shaders','textures','buffers','metrics']);


function initTargetChips() {
  const grid = document.getElementById('targets-grid');
  ALL_TARGETS.forEach(t => {
    const lbl = document.createElement('label');
    lbl.className = 'target-chip';
    lbl.innerHTML = `<input type="checkbox" id="tgt-${t}"${DEFAULT_TARGETS.has(t) ? ' checked' : ''}> ${t}`;
    grid.appendChild(lbl);
  });
}

function selectedTargets() {
  return ALL_TARGETS.filter(t => document.getElementById(`tgt-${t}`)?.checked).join(',');
}

// ── SDP file browser ──────────────────────────────────────────────────────────

async function rescanSdpFiles() {
  const dir = document.getElementById('sdp-dir').value.trim();
  if (!dir) { setMsg('sdp-scan', 'error', 'Set SDP directory in Settings first'); return; }
  const grid = document.getElementById('sdp-file-grid');
  grid.innerHTML = '';
  const prog = document.createElement('div');
  prog.className = 'sdp-init-progress';
  prog.innerHTML = '<div class="sdp-init-bar"><div class="sdp-init-fill"></div></div><span class="sdp-init-label">Rescanning…</span>';
  grid.appendChild(prog);
  try {
    await apiPost(`${FILES}/sdp/rescan?dir=${encodeURIComponent(dir)}`, {});
  } catch (e) { /* ignore */ }
  await scanSdpFiles();
}

let _allSdpFiles = [];

async function scanSdpFiles() {
  const grid = document.getElementById('sdp-file-grid');
  grid.innerHTML = '<span class="muted">Loading…</span>';
  document.getElementById('sdp-scan-msg').textContent = '';

  let res;
  try {
    res = await apiGet(`${FILES}/sdp`);
  } catch (err) {
    grid.innerHTML = '';
    setMsg('sdp-scan', 'error', err.message);
    return;
  }
  if (!res.ok) {
    grid.innerHTML = '';
    setMsg('sdp-scan', 'error', res.error);
    return;
  }

  if (!res.data || res.data.length === 0) {
    grid.innerHTML = '<span class="muted">No SDP files. Set directory in Settings and click Refresh.</span>';
    return;
  }

  _allSdpFiles = res.data || [];
  await _populateHomeFilterProjects();
  _applyHomeFilter();
}

let _homeProjects = [];
let _homeVersions = [];
let _allVersions = [];

async function _populateHomeFilterProjects() {
  const sel = document.getElementById('home-filter-project');
  const prev = sel.value;
  sel.innerHTML = '<option value="">All Projects</option>';
  try {
    const projRes = await apiGet(`${DATA}/projects`);
    _homeProjects = (projRes.ok && projRes.data) || [];
  } catch (_) { _homeProjects = []; }
  _homeProjects.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.name;
    sel.appendChild(opt);
  });
  if (prev) sel.value = prev;
  _allVersions = [];
  for (const p of _homeProjects) {
    try {
      const vr = await apiGet(`${DATA}/projects/${p.id}/versions`);
      if (vr.ok && vr.data) _allVersions.push(...vr.data);
    } catch (_) {}
  }
}

async function onHomeFilterProjectChange() {
  const projId = document.getElementById('home-filter-project').value;
  const sel = document.getElementById('home-filter-version');
  sel.innerHTML = '<option value="">All Versions</option>';
  _homeVersions = [];
  if (!projId) { sel.disabled = true; _applyHomeFilter(); return; }
  sel.disabled = false;
  try {
    const verRes = await apiGet(`${DATA}/projects/${projId}/versions`);
    _homeVersions = (verRes.ok && verRes.data) || [];
  } catch (_) { _homeVersions = []; }
  _homeVersions.forEach(v => {
    const opt = document.createElement('option');
    opt.value = v.id;
    opt.textContent = v.name;
    sel.appendChild(opt);
  });
  _applyHomeFilter();
}

function onHomeFilterVersionChange() {
  _applyHomeFilter();
}

function _applyHomeFilter() {
  const projId = document.getElementById('home-filter-project').value;
  const verId = document.getElementById('home-filter-version').value;

  let files = _allSdpFiles;
  if (projId) files = files.filter(f => f.project_id === projId);
  if (verId) files = files.filter(f => f.version_id === verId);

  const grid = document.getElementById('sdp-file-grid');
  grid.innerHTML = '';

  _renderFlatSdpFiles(grid, files);
  _enableExploreButtons(grid);
}

async function _renderGroupedSdpFiles(grid, files) {
  const projRes = await apiGet(`${DATA}/projects`);
  const projects = (projRes.ok && projRes.data) || [];
  const projMap = new Map(projects.map(p => [p.id, p]));

  // Group files by project_id
  const grouped = new Map();
  files.forEach(f => {
    const pid = f.project_id || '__none__';
    if (!grouped.has(pid)) grouped.set(pid, []);
    grouped.get(pid).push(f);
  });

  // Render each project as collapsible section
  for (const proj of projects) {
    const projFiles = grouped.get(proj.id);
    if (!projFiles || projFiles.length === 0) continue;

    const card = document.createElement('div');
    card.className = 'project-card';
    card.dataset.projectId = proj.id;

    const hdr = document.createElement('div');
    hdr.className = 'project-hdr';
    hdr.innerHTML = `<span class="project-chevron">&#9660;</span>
      <span class="project-name" style="color:${proj.color || 'var(--text)'}">${escHtml(proj.name)}</span>
      <span class="project-count">${projFiles.length} file${projFiles.length > 1 ? 's' : ''}</span>`;
    hdr.onclick = () => {
      const body = card.querySelector('.project-body');
      const chev = card.querySelector('.project-chevron');
      if (body.style.display === 'none') { body.style.display = ''; chev.innerHTML = '&#9660;'; }
      else { body.style.display = 'none'; chev.innerHTML = '&#9654;'; }
    };

    const body = document.createElement('div');
    body.className = 'project-body';

    // Group by version within project
    const verRes = await apiGet(`${DATA}/projects/${proj.id}/versions`);
    const versions = (verRes.ok && verRes.data) || [];
    const verMap = new Map(versions.map(v => [v.id, v]));

    const verGrouped = new Map();
    projFiles.forEach(f => {
      const vid = f.version_id || '__none__';
      if (!verGrouped.has(vid)) verGrouped.set(vid, []);
      verGrouped.get(vid).push(f);
    });

    for (const ver of versions) {
      const verFiles = verGrouped.get(ver.id);
      if (!verFiles || verFiles.length === 0) continue;
      const sec = document.createElement('div');
      sec.className = 'version-section';
      sec.innerHTML = `<div class="version-name">${escHtml(ver.name)}</div>`;
      const vGrid = document.createElement('div');
      vGrid.className = 'sdp-grid';
      verFiles.forEach(f => vGrid.appendChild(_buildSdpCard(f)));
      sec.appendChild(vGrid);
      body.appendChild(sec);
    }

    // Unversioned files in this project
    const unver = verGrouped.get('__none__');
    if (unver && unver.length > 0) {
      const vGrid = document.createElement('div');
      vGrid.className = 'sdp-grid';
      unver.forEach(f => vGrid.appendChild(_buildSdpCard(f)));
      body.appendChild(vGrid);
    }

    card.appendChild(hdr);
    card.appendChild(body);
    grid.appendChild(card);
  }

  // Uncategorized files (no project)
  const orphans = grouped.get('__none__');
  if (orphans && orphans.length > 0) {
    const card = document.createElement('div');
    card.className = 'project-card';
    const hdr = document.createElement('div');
    hdr.className = 'project-hdr';
    hdr.innerHTML = `<span class="project-chevron">&#9660;</span>
      <span class="project-name" style="color:var(--text-muted)">Uncategorized</span>
      <span class="project-count">${orphans.length} file${orphans.length > 1 ? 's' : ''}</span>`;
    hdr.onclick = () => {
      const body = card.querySelector('.project-body');
      const chev = card.querySelector('.project-chevron');
      if (body.style.display === 'none') { body.style.display = ''; chev.innerHTML = '&#9660;'; }
      else { body.style.display = 'none'; chev.innerHTML = '&#9654;'; }
    };
    const body = document.createElement('div');
    body.className = 'project-body';
    const vGrid = document.createElement('div');
    vGrid.className = 'sdp-grid';
    orphans.forEach(f => vGrid.appendChild(_buildSdpCard(f)));
    body.appendChild(vGrid);
    card.appendChild(hdr);
    card.appendChild(body);
    grid.appendChild(card);
  }
}

function _renderFlatSdpFiles(grid, files) {
  const addCard = document.createElement('div');
  addCard.className = 'sdp-card sdp-card-add';
  addCard.onclick = () => openModal('snapshot-modal');
  addCard.innerHTML = '<span class="sdp-card-add-icon">+</span>';
  grid.appendChild(addCard);
  files.forEach(f => grid.appendChild(_buildSdpCard(f)));
}

let _homeSelectedSdp = null;

function _buildSdpCard(f) {
  const fpath = normPath(f.path);
  if (f.info && f.info.api) sdpApiCache[fpath] = f.info.api;
  sdpInfoCache[fpath] = { ...(f.info || {}), project_id: f.project_id, version_id: f.version_id, size: f.size };
  const card = document.createElement('div');
  card.className = 'sdp-card';
  card.dataset.sdpPath = fpath;

  const thumb = f.thumbnail
    ? `<img class="sdp-card-thumb" src="${FILES}/image?path=${encodeURIComponent(f.thumbnail)}&t=${Date.now()}" alt="">`
    : `<span class="sdp-card-icon">&#128230;</span>`;
  const projName = _homeProjects.find(p => p.id === f.project_id)?.name || '';
  card.innerHTML = `${thumb}<span class="sdp-card-app">${projName}</span><span class="sdp-card-name" title="${fpath}">${f.name}</span>`;
  card.addEventListener('click', () => _selectSdpCard(fpath));
  return card;
}

function _selectSdpCard(fpath) {
  _homeSelectedSdp = fpath;
  document.querySelectorAll('#sdp-file-grid .sdp-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.sdpPath === fpath);
  });
  const info = sdpInfoCache[fpath] || {};
  const projName = (info.project_id && _homeProjects.find(p => p.id === info.project_id))?.name || '';
  const verName = (info.version_id && _allVersions.find(v => v.id === info.version_id))?.name || '';
  const timeStr = info.capture_time ? new Date(info.capture_time).toLocaleString() : '';
  const pairs = [
    projName ? ['Project', projName] : null,
    info.app ? ['App', info.app] : null,
    verName ? ['Version', verName] : null,
    timeStr ? ['Time', timeStr] : null,
    info.size ? ['Size', (info.size / 1048576).toFixed(1) + ' MB'] : null,
    info.api ? ['API', info.api] : null,
    info.gpu_renderer ? ['Device', info.gpu_renderer] : null,
  ].filter(Boolean);
  const detailEl = document.getElementById('home-selected-detail');
  detailEl.style.display = '';
  document.getElementById('home-detail-info').innerHTML = pairs.map(([k, v]) => `<span class="detail-meta"><span class="detail-key">${k}:</span> ${escHtml(v)}</span>`).join('');
  document.getElementById('home-btn-explore').disabled = !sdpAnalysisCache[fpath];
}

function doAnalyzeSelected() {
  if (_homeSelectedSdp) doAnalyze(_homeSelectedSdp);
}

function doExploreSelected() {
  if (_homeSelectedSdp) openExplorerTab(_homeSelectedSdp);
}

function toggleSetPopover() {
  const pop = document.getElementById('home-set-popover');
  if (pop.style.display === 'none') {
    pop.style.display = '';
    _loadSetPopover();
    setTimeout(() => document.addEventListener('click', _closeSetPopoverOutside), 0);
  } else {
    pop.style.display = 'none';
  }
}

function _closeSetPopoverOutside(e) {
  const pop = document.getElementById('home-set-popover');
  if (!e.target.isConnected) return;
  if (!pop.contains(e.target) && e.target.id !== 'home-btn-set') {
    pop.style.display = 'none';
    document.removeEventListener('click', _closeSetPopoverOutside);
  }
}

let _popoverSelectedProject = null;
let _popoverSelectedVersion = null;

async function _loadSetPopover() {
  const info = sdpInfoCache[_homeSelectedSdp] || {};
  _popoverSelectedProject = info.project_id || null;
  _popoverSelectedVersion = info.version_id || null;
  await _renderProjectList();
  await _renderVersionList();
}

let _popoverProjects = [];
let _popoverVersions = [];

async function _renderProjectList() {
  const list = document.getElementById('set-popover-project-list');
  list.innerHTML = '';
  const projRes = await apiGet(`${DATA}/projects`);
  _popoverProjects = (projRes.ok && projRes.data) || [];
  _drawProjectList();
}

function _drawProjectList() {
  const list = document.getElementById('set-popover-project-list');
  list.innerHTML = '';
  _popoverProjects.forEach(p => {
    list.appendChild(_makeListItem(p.id, p.name, 'project', p.id === _popoverSelectedProject));
  });
}

async function _renderVersionList() {
  if (!_popoverSelectedProject) {
    _popoverVersions = [];
    _drawVersionList();
    return;
  }
  const verRes = await apiGet(`${DATA}/projects/${_popoverSelectedProject}/versions`);
  _popoverVersions = (verRes.ok && verRes.data) || [];
  _drawVersionList();
}

function _drawVersionList() {
  const list = document.getElementById('set-popover-version-list');
  list.innerHTML = '';
  if (!_popoverSelectedProject) {
    list.innerHTML = '<span style="font-size:12px;color:var(--text-muted)">Select a project first</span>';
    return;
  }
  _popoverVersions.forEach(v => {
    list.appendChild(_makeListItem(v.id, v.name, 'version', v.id === _popoverSelectedVersion));
  });
}

function _makeListItem(id, name, type, isActive) {
  const item = document.createElement('div');
  item.className = 'popover-list-item' + (isActive ? ' active' : '');
  item.innerHTML = `<span class="pli-name">${escHtml(name)}</span>`;
  item.onclick = (e) => {
    e.stopPropagation();
    if (type === 'project') { _popoverSelectedProject = id; _popoverSelectedVersion = null; _drawProjectList(); _renderVersionList(); }
    else { _popoverSelectedVersion = id; _drawVersionList(); }
  };
  return item;
}

async function doApplySet() {
  if (!_homeSelectedSdp) return;
  const pid = _popoverSelectedProject || null;
  const vid = _popoverSelectedVersion || null;
  await fetch(`${FILES}/sdp/move`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ path: _homeSelectedSdp, project_id: pid, version_id: vid }) });
  document.getElementById('home-set-popover').style.display = 'none';
  document.removeEventListener('click', _closeSetPopoverOutside);
  const selected = _homeSelectedSdp;
  await scanSdpFiles();
  if (selected) _selectSdpCard(selected);
  refreshAllExplorerMetaBars();
}

async function doRemoveSelected() {
  if (!_homeSelectedSdp) return;
  if (!confirm('Remove this SDP file from the list?')) return;
  await fetch(`${FILES}/sdp/remove`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ path: _homeSelectedSdp }) });
  _homeSelectedSdp = null;
  document.getElementById('home-selected-detail').style.display = 'none';
  document.getElementById('home-set-popover').style.display = 'none';
  scanSdpFiles();
}

async function doGenThumbnail() {
  if (!_homeSelectedSdp) return;
  const orientation = document.getElementById('set-popover-orientation').value;
  const res = await fetch(`${FILES}/sdp/gen_thumbnail`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ path: _homeSelectedSdp, orientation }) });
  const data = await res.json();
  if (data.ok) {
    await scanSdpFiles();
    if (_homeSelectedSdp) _selectSdpCard(_homeSelectedSdp);
  } else {
    alert(data.error || 'Failed to generate thumbnail');
  }
}

async function _enableExploreButtons(grid) {
  try {
    const snapRes = await apiGet(`${DATA}/snapshots`);
    if (snapRes.ok) {
      const snapshots = snapRes.data || [];
      grid.querySelectorAll('.sdp-card').forEach(card => {
        const sdpPath = card.dataset.sdpPath;
        if (!sdpPath) return;
        const sdpName = sdpPath.replace(/\\/g, '/').split('/').pop();
        const sdpStem = sdpName.replace(/\.sdp$/i, '');
        const match = snapshots.find(s =>
          s.sdp_name === sdpName ||
          s.sdp_name === sdpStem ||
          s.run_name  === sdpStem
        );
        if (match) {
          sdpAnalysisCache[sdpPath] = match.snapshot_dir;
        }
      });
      if (_homeSelectedSdp) {
        document.getElementById('home-btn-explore').disabled = !sdpAnalysisCache[_homeSelectedSdp];
      }
    }
  } catch (_) {}
}

function renderSdpDetail(d, sizeMb) {
  if (!d) return '';
  const lines = [];
  if (sizeMb) lines.push(`<span class="sdp-info-label">Size:</span> <span class="sdp-info-val">${sizeMb} MB</span>`);
  if (d.app) lines.push(`<span class="sdp-info-label">App:</span> <span class="sdp-info-val">${d.app}</span>`);
  if (d.activity) lines.push(`<span class="sdp-info-label">Activity:</span> <span class="sdp-info-val">${d.activity}</span>`);
  if (d.device_model) {
    const dev = [d.device_manufacturer, d.device_model].filter(Boolean).join(' ');
    lines.push(`<span class="sdp-info-label">Device:</span> <span class="sdp-info-val">${dev}</span>`);
  }
  if (d.device_platform) lines.push(`<span class="sdp-info-label">SoC:</span> <span class="sdp-info-val">${d.device_platform}</span>`);
  if (d.gpu_renderer) lines.push(`<span class="sdp-info-label">GPU:</span> <span class="sdp-info-val">${d.gpu_renderer}</span>`);
  if (d.api) lines.push(`<span class="sdp-info-label">API:</span> <span class="sdp-info-val sdp-api-${d.api.toLowerCase()}">${d.api}</span>`);
  if (d.capture_time) {
    const t = new Date(d.capture_time).toLocaleString();
    lines.push(`<span class="sdp-info-label">Time:</span> <span class="sdp-info-val">${t}</span>`);
  }
  if (d.snapshot_count) lines.push(`<span class="sdp-info-label">Snapshots:</span> <span class="sdp-info-val">${d.snapshot_count}</span>`);
  return lines.join('<br>');
}

// ── Analysis ──────────────────────────────────────────────────────────────────

function goAnalyze(sdpPath, captureId) {
  const dirInput  = document.getElementById('sdp-dir');
  const parentDir = sdpPath.substring(0, sdpPath.lastIndexOf('/'));
  if (parentDir && !dirInput.value) {
    dirInput.value = parentDir;
    localStorage.setItem('sdpDir', parentDir);
  }
  document.getElementById('snapshot-id').value = captureId ?? 2;
  switchTab('home');  // triggers scanSdpFiles if dir is set
}

async function doAnalyze(sdpPath) {
  if (state.activeAnalysisJobId) {
    console.warn('Analysis already running');
    return;
  }

  const snapshotId = parseInt(document.getElementById('snapshot-id').value, 10);
  const allSelected = new Set(selectedTargets().split(',').filter(Boolean));

  if (!snapshotId || snapshotId < 1) {
    setMsg('analysis', 'error', 'Snapshot ID must be ≥ 1 (use 1 for all snapshots) — check Settings');
    openModal('analysis-modal');
    return;
  }
  if (allSelected.size === 0) {
    setMsg('analysis', 'error', 'Select at least one target in Settings');
    openModal('analysis-modal');
    return;
  }

  // Show progress card
  document.getElementById('analysis-progress-name').textContent = sdpPath.split('/').pop();
  openModal('analysis-modal');
  setMsg('analysis', 'info', 'Submitting…');
  showProg('analysis', 0, 'starting');
  document.querySelectorAll('.sdp-analyze-btn').forEach(b => b.disabled = true);

  // C# targets: intersection of selected + CS_TARGETS
  const csTargets = [...allSelected].filter(t => CS_TARGETS.has(t)).join(',');

  let res;
  try {
    res = await apiPost(`${API}/analysis`, { sdpPath, snapshotId, targets: csTargets || 'dc' });
  } catch (err) {
    _finishAnalysis(sdpPath, null, err.message);
    return;
  }
  if (!res.ok) {
    _finishAnalysis(sdpPath, null, res.error);
    return;
  }

  state.activeAnalysisJobId = res.data.jobId;
  try { localStorage.setItem('activeCsJob', JSON.stringify({ jobId: res.data.jobId, sdpPath, allSelected: [...allSelected] })); } catch {}

  // C# job occupies 0-70% of progress
  pollJob('analysis', res.data.jobId,
    job => showProg('analysis', Math.round(job.progress * 0.70), job.phase),
    job => {
      const r = job.result || {};
      // Single-snapshot result: { captureDir, sessionDir, ... }
      // Multi-snapshot result:  { captureIds: [...], sessionDir, ... }
      if (r.captureDir) {
        const captureDir = normPath(r.captureDir);
        _runPySteps(sdpPath, captureDir, allSelected);
      } else if (r.captureIds && r.captureIds.length > 0 && r.sessionDir) {
        const sessionDir = normPath(r.sessionDir);
        const captureDirs = r.captureIds.map(id => sessionDir + '/snapshot_' + id);
        _runPyStepsAll(sdpPath, captureDirs, allSelected);
      } else {
        _finishAnalysis(sdpPath, null, 'No captureDir in job result');
      }
    },
    err => _finishAnalysis(sdpPath, null, err)
  );
}

// Run Python pipeline for multiple capture dirs sequentially.
async function _runPyStepsAll(sdpPath, captureDirs, selected) {
  // Map C# extraction targets to their Python post-processing counterparts
  const pySelected = new Set(selected);
  if (pySelected.has('buffers'))  pySelected.add('mesh_stats');
  if (pySelected.has('textures')) pySelected.add('texture_stats');
  if (pySelected.has('shaders')) pySelected.add('gles_decompile');

  const total = captureDirs.length;
  for (let i = 0; i < total; i++) {
    const dir = captureDirs[i];
    showProg('analysis', 70 + Math.round((i / total) * 30), `python [${i + 1}/${total}] snapshot`);
    await new Promise(resolve => {
      const targets = ['screenshot', 'mesh_stats', 'texture_stats', 'gles_decompile', 'ingest', 'label', 'status', 'topdc', 'analysis']
        .filter(k => pySelected.has(k)).join(',');
      if (!targets) { resolve(); return; }
      apiPost(`/api/jobs/pipeline?snapshot_dir=${encodeURIComponent(dir)}&targets=${encodeURIComponent(targets)}`, {})
        .then(res => {
          if (!res.ok) {
            console.warn('Pipeline submit failed for', dir, res.error);
            resolve(); return;
          }
          const jobId = res.job_id;
          // Persist so a page refresh can resume the current snapshot
          try { localStorage.setItem('activePipelineJob', JSON.stringify({ jobId, sdpPath, captureDir: dir })); } catch {}
          const poll = setInterval(async () => {
            let pr;
            try { pr = await apiGet(`/api/jobs/pipeline/${jobId}`); } catch { return; }
            if (!pr.ok) {
              clearInterval(poll);
              try { localStorage.removeItem('activePipelineJob'); } catch {}
              console.warn('Pipeline poll failed for', dir, pr.error);
              resolve(); return;
            }
            const pct = 70 + Math.round(((i + pr.data.progress / 100) / total) * 30);
            showProg('analysis', pct, `[${i + 1}/${total}] ${pr.data.phase || pr.data.status}`);
            if (pr.data.status === 'completed') {
              clearInterval(poll);
              try { localStorage.removeItem('activePipelineJob'); } catch {}
              resolve();
            } else if (pr.data.status === 'failed' || pr.data.status === 'cancelled') {
              clearInterval(poll);
              try { localStorage.removeItem('activePipelineJob'); } catch {}
              console.warn('Pipeline failed for', dir, pr.data.error || pr.data.status);
              resolve(); // non-fatal — continue to next snapshot
            }
          }, 2000);
        })
        .catch(err => { console.warn('Pipeline error for', dir, err.message); resolve(); });
    });
  }
  const lastDir = captureDirs[total - 1] || null;
  _finishAnalysis(sdpPath, lastDir, null);
}

// Python pipeline: submit to server-side job manager, then poll.
// The pipeline runs in a background thread on the server — browser refresh does not interrupt it.
async function _runPySteps(sdpPath, captureDir, selected) {
  // Map C# extraction targets to their Python post-processing counterparts
  const pySelected = new Set(selected);
  if (pySelected.has('buffers'))  pySelected.add('mesh_stats');
  if (pySelected.has('textures')) pySelected.add('texture_stats');
  if (pySelected.has('shaders')) pySelected.add('gles_decompile');

  // Build ordered targets from the user's selection
  const targets = ['screenshot', 'mesh_stats', 'texture_stats', 'gles_decompile', 'ingest', 'label', 'status', 'topdc', 'analysis']
    .filter(k => pySelected.has(k)).join(',');

  if (!targets) {
    // Nothing to do on the Python side
    _finishAnalysis(sdpPath, captureDir, null);
    return;
  }

  // Submit pipeline job
  let res;
  try {
    res = await apiPost(`/api/jobs/pipeline?snapshot_dir=${encodeURIComponent(captureDir)}&targets=${encodeURIComponent(targets)}`, {});
  } catch (err) {
    _finishAnalysis(sdpPath, null, 'Pipeline submit error: ' + err.message);
    return;
  }
  if (!res.ok) {
    _finishAnalysis(sdpPath, null, res.error || 'Pipeline submit failed');
    return;
  }

  const jobId = res.job_id;
  // Persist job_id so a page refresh can resume polling
  try { localStorage.setItem('activePipelineJob', JSON.stringify({ jobId, sdpPath, captureDir })); } catch {}

  _pollPipelineJob(jobId, sdpPath, captureDir);
}

function _pollPipelineJob(jobId, sdpPath, captureDir) {
  clearInterval(timers.analysis);
  timers.analysis = setInterval(async () => {
    let res;
    try {
      res = await apiGet(`/api/jobs/pipeline/${jobId}`);
    } catch (err) {
      // Network blip — keep polling
      return;
    }
    if (!res.ok) {
      clearInterval(timers.analysis);
      try { localStorage.removeItem('activePipelineJob'); } catch {}
      _finishAnalysis(sdpPath, null, res.error || 'Pipeline poll failed');
      return;
    }

    const job = res.data;
    // Map pipeline 0-100 to the overall 70-100% progress band
    const pct = 70 + Math.round(job.progress * 0.30);
    showProg('analysis', pct, job.phase || job.status);

    if (job.status === 'completed') {
      clearInterval(timers.analysis);
      try { localStorage.removeItem('activePipelineJob'); } catch {}
      _finishAnalysis(sdpPath, captureDir, null);
    } else if (job.status === 'failed' || job.status === 'cancelled') {
      clearInterval(timers.analysis);
      try { localStorage.removeItem('activePipelineJob'); } catch {}
      _finishAnalysis(sdpPath, null, job.error || job.status);
    }
    // else: pending/running — keep polling
  }, 2000);
}

function _finishAnalysis(sdpPath, captureDir, error) {
  state.activeAnalysisJobId = null;
  try { localStorage.removeItem('activeCsJob'); } catch {}
  document.querySelectorAll('.sdp-analyze-btn').forEach(b => b.disabled = false);

  if (error) {
    setMsg('analysis', 'error', error);
    return;
  }

  setMsg('analysis', 'success', `Done: ${captureDir || '—'}`);
  showProg('analysis', 100, 'complete');

  if (captureDir) {
    state.lastAnalysisDir = captureDir;
    sdpAnalysisCache[sdpPath] = captureDir;
    const card = [...document.querySelectorAll('.sdp-card')]
                   .find(c => c.dataset.sdpPath === sdpPath);
    if (card) {
      const rb = card.querySelector('.sdp-results-btn');
      if (rb) {
        rb.disabled = false;
        rb.onclick  = () => openExplorerTab(sdpPath);
      }
    }

    // If an explorer tab for this SDP is already open, refresh its snapshots
    const tabId = _hashString(sdpPath);
    if (explorerTabs[tabId]) {
      loadSnapshots(tabId).then(() => {
        const ts = explorerTabs[tabId];
        if (!ts.snapState.snapshotId) return;
        const norm = normPath(captureDir);
        for (const run of ts.snapState.runs) {
          const s = run.snapshots.find(x => normPath(x.snapshot_dir) === norm);
          if (s) { selectSnapshot(tabId, s); break; }
        }
      }).catch(() => {});
    }
  }

  setTimeout(() => {
    closeModal('analysis-modal');
    setMsg('analysis', '', '');
  }, 3000);
}

async function cancelAnalysis() {
  // Cancel C# job if active
  if (state.activeAnalysisJobId) {
    try {
      await apiPost(`${API}/jobs/${state.activeAnalysisJobId}/cancel`, {});
      setMsg('analysis', 'warn', 'Cancelling…');
    } catch (err) {
      setMsg('analysis', 'error', 'Cancel failed: ' + err.message);
    }
  }
  // Cancel Python pipeline job if active
  const saved = _getActivePipelineJob();
  if (saved) {
    try {
      await apiPost(`/api/jobs/pipeline/${saved.jobId}/cancel`, {});
    } catch { /* ignore */ }
    try { localStorage.removeItem('activePipelineJob'); } catch {}
    clearInterval(timers.analysis);
  }
}

// Resume a C# analysis job that was running before a page refresh
function _resumeCsJobIfAny() {
  let saved;
  try { saved = JSON.parse(localStorage.getItem('activeCsJob') || 'null'); } catch { saved = null; }
  if (!saved) return;
  const { jobId, sdpPath, allSelected: allSelectedArr } = saved;
  const allSelected = new Set(allSelectedArr || []);

  apiGet(`${API}/jobs/${jobId}`).then(res => {
    if (!res.ok) { try { localStorage.removeItem('activeCsJob'); } catch {} return; }
    const job = res.data;
    if (job.status === 'Completed') {
      // C# done — jump straight to Python phase
      try { localStorage.removeItem('activeCsJob'); } catch {}
      const r = job.result || {};
      if (r.captureDir) {
        _runPySteps(sdpPath, normPath(r.captureDir), allSelected);
      } else if (r.captureIds && r.captureIds.length > 0 && r.sessionDir) {
        const captureDirs = r.captureIds.map(id => normPath(r.sessionDir) + '/snapshot_' + id);
        _runPyStepsAll(sdpPath, captureDirs, allSelected);
      } else {
        try { localStorage.removeItem('activeCsJob'); } catch {}
      }
      return;
    }
    if (job.status === 'Failed' || job.status === 'Cancelled') {
      try { localStorage.removeItem('activeCsJob'); } catch {}
      return;
    }
    // Still running — restore progress UI and resume polling
    state.activeAnalysisJobId = jobId;
    document.getElementById('analysis-progress-name').textContent = sdpPath.split('/').pop();
    openModal('analysis-modal');
    setMsg('analysis', 'info', `Resuming analysis job ${jobId}…`);
    showProg('analysis', Math.round(job.progress * 0.70), job.phase || 'running');
    document.querySelectorAll('.sdp-analyze-btn').forEach(b => b.disabled = true);
    pollJob('analysis', jobId,
      j => showProg('analysis', Math.round(j.progress * 0.70), j.phase),
      j => {
        try { localStorage.removeItem('activeCsJob'); } catch {}
        const r = j.result || {};
        if (r.captureDir) {
          _runPySteps(sdpPath, normPath(r.captureDir), allSelected);
        } else if (r.captureIds && r.captureIds.length > 0 && r.sessionDir) {
          const captureDirs = r.captureIds.map(id => normPath(r.sessionDir) + '/snapshot_' + id);
          _runPyStepsAll(sdpPath, captureDirs, allSelected);
        } else {
          _finishAnalysis(sdpPath, null, 'No captureDir in job result');
        }
      },
      err => { try { localStorage.removeItem('activeCsJob'); } catch {} _finishAnalysis(sdpPath, null, err); }
    );
  }).catch(() => { try { localStorage.removeItem('activeCsJob'); } catch {} });
}

function _getActivePipelineJob() {
  try {
    const raw = localStorage.getItem('activePipelineJob');
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

// Resume polling a pipeline job that was running before a page refresh
function _resumePipelineJobIfAny() {
  const saved = _getActivePipelineJob();
  if (!saved) return;
  apiGet(`/api/jobs/pipeline/${saved.jobId}`).then(res => {
    if (!res.ok || res.data.status === 'completed' || res.data.status === 'failed' || res.data.status === 'cancelled') {
      try { localStorage.removeItem('activePipelineJob'); } catch {}
      return;
    }
    // Job still running — restore progress UI and resume polling
    const { jobId, sdpPath, captureDir } = saved;
    document.getElementById('analysis-progress-name').textContent = sdpPath.split('/').pop();
    openModal('analysis-modal');
    setMsg('analysis', 'info', `Resuming pipeline job ${jobId}…`);
    showProg('analysis', 70 + Math.round(res.data.progress * 0.30), res.data.phase || 'running');
    document.querySelectorAll('.sdp-analyze-btn').forEach(b => b.disabled = true);
    _pollPipelineJob(jobId, sdpPath, captureDir);
  }).catch(() => {
    try { localStorage.removeItem('activePipelineJob'); } catch {}
  });
}

// ── Analysis settings ─────────────────────────────────────────────────────────

function toggleAnalysisSettings() {
  const body    = document.getElementById('analysis-settings-body');
  const chevron = document.getElementById('settings-chevron');
  const open    = body.style.display === 'none';
  body.style.display    = open ? '' : 'none';
  chevron.textContent   = open ? '▼' : '▶';
}

async function saveAnalysisSettings() {
  const dir = document.getElementById('sdp-dir').value.trim();
  const analysisDir = document.getElementById('shared-snap-dir').value.trim();
  const reportDir = document.getElementById('report-dir').value.trim();
  const snapshotId = document.getElementById('snapshot-id').value;
  const targets = selectedTargets();

  // Save to config.ini via API
  try {
    const res = await apiPost(`${FILES}/settings`, {
      sdpDir: dir,
      analysisDir: analysisDir,
      reportDir: reportDir,
      snapshotId: snapshotId,
      targets: targets,
    });
    if (res.ok) {
      setMsg('settings-save', 'success', 'Saved to config.ini');
    } else {
      setMsg('settings-save', 'error', res.error || 'Save failed');
    }
  } catch (e) {
    setMsg('settings-save', 'error', e.message);
  }

  // Also keep localStorage as fallback
  if (dir) localStorage.setItem('sdpDir', dir);
  if (analysisDir) localStorage.setItem('analysisRoot', analysisDir);
  localStorage.setItem('analysisSettings', JSON.stringify({ snapshotId, targets }));

  setTimeout(() => { const el = document.getElementById('settings-save-msg'); if (el) el.textContent = ''; }, 2000);
  if (dir) scanSdpFiles();
}

async function doReingest() {
  setMsg('settings-save', 'info', 'Re-ingesting all snapshots...');
  try {
    // Get all known snapshot dirs from DB
    const snapRes = await apiGet(`${DATA}/snapshots`);
    if (!snapRes.ok || !snapRes.data || snapRes.data.length === 0) {
      setMsg('settings-save', 'error', 'No snapshots found to re-ingest');
      return;
    }
    let ok = 0, fail = 0;
    for (const snap of snapRes.data) {
      const dir = snap.snapshot_dir;
      try {
        const r = await apiPost(`${JOBS}/ingest?snapshot_dir=${encodeURIComponent(dir)}`, {});
        if (r.ok) ok++; else fail++;
      } catch { fail++; }
    }
    setMsg('settings-save', 'success', `Re-ingest done: ${ok} ok, ${fail} failed`);
  } catch (e) {
    setMsg('settings-save', 'error', e.message);
  }
}

async function loadAnalysisSettings() {
  // Load from config.ini via API (single source of truth)
  try {
    const res = await apiGet(`${FILES}/settings`);
    if (res.ok && res.data) {
      const d = res.data;
      if (d.sdpDir) document.getElementById('sdp-dir').value = d.sdpDir;
      if (d.analysisDir) document.getElementById('shared-snap-dir').value = d.analysisDir;
      if (d.reportDir) document.getElementById('report-dir').value = d.reportDir;
      if (d.snapshotId) document.getElementById('snapshot-id').value = d.snapshotId;
      if (d.targets) {
        const tgtSet = new Set(d.targets.split(',').map(t => t.trim()));
        ALL_TARGETS.forEach(t => {
          const el = document.getElementById(`tgt-${t}`);
          if (el) el.checked = tgtSet.has(t);
        });
      }
    }
  } catch { /* API unavailable */ }
}

// ── Project Manager ─────────────────────────────────────────────────────────

let _pmSelectedProjectId = null;

async function openProjectManager() {
  closeModal('settings-modal');
  const existing = document.getElementById('project-manager-modal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'project-manager-modal';
  modal.className = 'modal-overlay';
  modal.style.display = 'flex';
  modal.innerHTML = `
    <div class="modal-backdrop" onclick="closeProjectManager()"></div>
    <div class="modal-panel" style="max-width:620px;width:90%">
      <div class="modal-header">
        <span class="modal-title">Projects &amp; Versions</span>
        <button class="modal-close" onclick="closeProjectManager()">&#10005;</button>
      </div>
      <div class="modal-body" style="padding:16px">
        <div style="display:flex;gap:16px">
          <div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:8px">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span style="font-weight:600;font-size:14px">Projects</span>
              <button class="btn-secondary btn-sm" onclick="pmAddProject()">+ New</button>
            </div>
            <div id="pm-project-list" style="display:flex;flex-direction:column;gap:4px;overflow-y:auto;max-height:360px"></div>
          </div>
          <div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:8px">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span style="font-weight:600;font-size:14px">Versions</span>
              <button class="btn-secondary btn-sm" id="pm-add-version-btn" onclick="pmAddVersion()" disabled>+ New</button>
            </div>
            <div id="pm-version-list" style="display:flex;flex-direction:column;gap:4px;overflow-y:auto;max-height:360px"></div>
          </div>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  _pmSelectedProjectId = null;
  await pmRefreshProjects();
}

function closeProjectManager() {
  const m = document.getElementById('project-manager-modal');
  if (m) m.remove();
}

async function pmRefreshProjects() {
  const res = await apiGet(`${DATA}/projects`);
  if (!res.ok) return;
  const list = document.getElementById('pm-project-list');
  if (!list) return;
  list.innerHTML = '';
  (res.data || []).forEach(p => {
    const item = document.createElement('div');
    item.className = 'pm-item' + (p.id === _pmSelectedProjectId ? ' pm-selected' : '');
    item.dataset.id = p.id;
    item.innerHTML = `<span style="flex:1;font-weight:500;color:${p.color}">${escHtml(p.name)}</span>
      <button class="btn-secondary btn-sm" onclick="event.stopPropagation();pmDeleteProject('${p.id}')" style="padding:2px 6px;font-size:11px">&#10005;</button>`;
    item.onclick = () => pmSelectProject(p.id);
    list.appendChild(item);
  });
}

async function pmSelectProject(pid) {
  _pmSelectedProjectId = pid;
  document.querySelectorAll('#pm-project-list .pm-item').forEach(el => {
    el.classList.toggle('pm-selected', el.dataset.id === pid);
  });
  document.getElementById('pm-add-version-btn').disabled = false;
  const res = await apiGet(`${DATA}/projects/${pid}/versions`);
  const list = document.getElementById('pm-version-list');
  list.innerHTML = '';
  if (!res.ok) return;
  (res.data || []).forEach(v => {
    const item = document.createElement('div');
    item.className = 'pm-item';
    item.innerHTML = `<span style="flex:1">${escHtml(v.name)}</span>
      <button class="btn-secondary btn-sm" onclick="pmDeleteVersion('${v.id}')" style="padding:2px 6px;font-size:11px">&#10005;</button>`;
    list.appendChild(item);
  });
}

async function pmAddProject() {
  const name = prompt('Project name:');
  if (!name || !name.trim()) return;
  const res = await apiPost(`${DATA}/projects`, { name: name.trim() });
  if (!res.ok) { alert(res.error || 'Failed'); return; }
  await pmRefreshProjects();
  pmSelectProject(res.data.id);
}

async function pmDeleteProject(pid) {
  if (!confirm('Delete this project and all its versions?')) return;
  await fetch(`${DATA}/projects/${pid}`, { method: 'DELETE' });
  if (_pmSelectedProjectId === pid) {
    _pmSelectedProjectId = null;
    const vl = document.getElementById('pm-version-list');
    if (vl) vl.innerHTML = '';
    document.getElementById('pm-add-version-btn').disabled = true;
  }
  await pmRefreshProjects();
}

async function pmAddVersion() {
  if (!_pmSelectedProjectId) return;
  const name = prompt('Version name (e.g. "v1.0 baseline"):');
  if (!name || !name.trim()) return;
  const res = await apiPost(`${DATA}/projects/${_pmSelectedProjectId}/versions`, { name: name.trim() });
  if (!res.ok) { alert(res.error || 'Failed'); return; }
  await pmSelectProject(_pmSelectedProjectId);
}

async function pmDeleteVersion(vid) {
  if (!confirm('Delete this version?')) return;
  await fetch(`${DATA}/versions/${vid}`, { method: 'DELETE' });
  if (_pmSelectedProjectId) await pmSelectProject(_pmSelectedProjectId);
}

// ── Results (per-tab) ────────────────────────────────────────────────────────

async function scanAnalyses(tabId, root, autoRunName, autoSnapId, silent = false) {
  const dir = root || localStorage.getItem('analysisRoot');
  if (!dir) return;
  if (!silent) {
    localStorage.setItem('analysisRoot', dir);
  }

  let res;
  try {
    res = await apiGet(`${FILES}/analyses?root=${encodeURIComponent(dir)}`);
  } catch (err) {
    return;
  }
  if (!res.ok) return;

  const runs = res.data || [];
  const ts = getTabState(tabId);
  if (!ts) return;
  ts._resultsState = { runs, activeRun: null, activeSnap: null };
  if (!silent) {
    renderRunSelector(tabId, autoRunName || null, autoSnapId || null);
  }
}

function renderRunSelector(tabId, autoRunName, autoSnapId) {
  const ts = getTabState(tabId);
  if (!ts || !ts._resultsState) return;
  const runs = ts._resultsState.runs;
  const el = getTabEl(tabId);
  if (!el) return;

  if (runs.length === 0) {
    el.querySelector('.explorer-snapshot-viewer').style.display = 'none';
    el.querySelector('.explorer-file-viewer').innerHTML = '';
    return;
  }

  const selectRun = autoRunName || runs[0].name;

  // Auto-select run
  const selectedRun = runs.find(r => r.name === selectRun) || runs[0];
  ts._resultsState.activeRun = selectedRun.name;
  renderResultSnapshotTabs(tabId, selectedRun, autoSnapId);
}

function renderResultSnapshotTabs(tabId, run, autoSnapId) {
  const el = getTabEl(tabId);
  if (!el) return;
  const panelsEl = el.querySelector('.explorer-snap-panels');
  const viewer   = el.querySelector('.explorer-snapshot-viewer');

  panelsEl.innerHTML = '';
  viewer.style.display = 'block';

  if (!run.snapshots || run.snapshots.length === 0) {
    panelsEl.innerHTML = '<span class="muted">No snapshots in this run.</span>';
    return;
  }

  const ts = getTabState(tabId);
  const activeSnap = autoSnapId || run.snapshots[0].id;
  if (ts && ts._resultsState) ts._resultsState.activeSnap = activeSnap;
  const snap = run.snapshots.find(s => s.id === activeSnap) || run.snapshots[0];

  const panel = document.createElement('div');
  panel.className = 'snap-panel';
  panel.appendChild(buildSnapPanel(snap, tabId));
  panelsEl.appendChild(panel);
}

function buildSnapPanel(snap, tabId) {
  const wrap = document.createElement('div');

  // ── Analysis section (default open) ─────────────────────────────
  wrap.appendChild(buildSection('Analysis', snap.analysis, snap.per_dc, true, tabId));
  // ── Statistics section ──────────────────────────────────────────
  wrap.appendChild(buildSection('Statistics', snap.statistics, null, false, tabId));
  // ── Raw section ─────────────────────────────────────────────────
  wrap.appendChild(buildSection('Raw', snap.raw, null, false, tabId));

  return wrap;
}

function buildSection(title, files, perDcFiles, defaultOpen, tabId) {
  const section = document.createElement('div');
  section.className = 'result-section';

  const hdr = document.createElement('div');
  hdr.className = 'result-section-hdr';
  hdr.innerHTML = `<span class="result-section-chevron">${defaultOpen ? '▼' : '▶'}</span> ${escHtml(title)}`;
  hdr.style.cursor = 'pointer';

  const body = document.createElement('div');
  body.className = 'result-section-body';
  body.style.display = defaultOpen ? '' : 'none';

  hdr.onclick = () => {
    const open = body.style.display !== 'none';
    body.style.display = open ? 'none' : '';
    hdr.querySelector('.result-section-chevron').textContent = open ? '▶' : '▼';
  };

  // File chips
  const grid = document.createElement('div');
  grid.className = 'file-grid';
  (files || []).forEach(f => {
    const btn = document.createElement('button');
    btn.className = `file-chip ext-${f.ext}`;
    btn.textContent = f.name;
    btn.onclick = () => viewFile(tabId, f.path, f.name, f.ext);
    grid.appendChild(btn);
  });
  body.appendChild(grid);

  // per_dc_content folder (only in Analysis section)
  if (perDcFiles && perDcFiles.length > 0) {
    const folder = document.createElement('div');
    folder.className = 'per-dc-folder';

    const folderHdr = document.createElement('div');
    folderHdr.className = 'per-dc-hdr';
    folderHdr.innerHTML = `<span class="per-dc-chevron">▶</span> per_dc_content/ <span class="muted" style="font-size:11px">${perDcFiles.length} files</span>`;
    folderHdr.style.cursor = 'pointer';

    const folderBody = document.createElement('div');
    folderBody.className = 'per-dc-body';
    folderBody.style.display = 'none';

    folderHdr.onclick = () => {
      const open = folderBody.style.display !== 'none';
      folderBody.style.display = open ? 'none' : '';
      folderHdr.querySelector('.per-dc-chevron').textContent = open ? '▼' : '▶';
    };

    const dcGrid = document.createElement('div');
    dcGrid.className = 'file-grid';
    perDcFiles.forEach(f => {
      const btn = document.createElement('button');
      btn.className = `file-chip ext-${f.ext}`;
      btn.textContent = f.name;
      btn.onclick = () => viewFile(tabId, f.path, f.name, f.ext);
      dcGrid.appendChild(btn);
    });
    folderBody.appendChild(dcGrid);

    folder.appendChild(folderHdr);
    folder.appendChild(folderBody);
    body.appendChild(folder);
  } else if (perDcFiles && perDcFiles.length === 0) {
    const folder = document.createElement('div');
    folder.className = 'per-dc-folder';
    folder.innerHTML = `<span class="muted" style="font-size:12px">per_dc_content/ (empty)</span>`;
    body.appendChild(folder);
  }

  if ((files || []).length === 0 && (!perDcFiles || perDcFiles.length === 0)) {
    grid.innerHTML = '<span class="muted" style="font-size:12px">No files.</span>';
  }

  section.appendChild(hdr);
  section.appendChild(body);
  return section;
}

async function viewFile(tabId, path, name, ext, scroll = true) {
  const el = getTabEl(tabId);
  if (!el) return;
  const viewer = el.querySelector('.explorer-file-viewer');
  viewer.innerHTML = '<span class="muted" style="padding:8px 0;display:block">Loading…</span>';

  let res;
  try {
    res = await apiGet(`${FILES}/read?path=${encodeURIComponent(path)}`);
  } catch (err) {
    viewer.innerHTML = `<span class="s-error">${escHtml(err.message)}</span>`;
    return;
  }
  if (!res.ok) {
    viewer.innerHTML = `<div class="s-error" style="padding:8px">${escHtml(res.error)}</div>`;
    return;
  }

  const content = res.data.content || '';

  const card = document.createElement('div');
  card.className = 'viewer-card';

  const hdr = document.createElement('div');
  hdr.className = 'viewer-header';
  const hdrTitle = document.createElement('span');
  hdrTitle.className = 'viewer-title';
  hdrTitle.textContent = name;
  const closeBtn = document.createElement('button');
  closeBtn.className = 'viewer-close';
  closeBtn.title = 'Close';
  closeBtn.textContent = '✕';
  closeBtn.onclick = () => { viewer.innerHTML = ''; };
  hdr.appendChild(hdrTitle);
  hdr.appendChild(closeBtn);

  const body = document.createElement('div');
  body.className = 'viewer-body';

  if (ext === 'md') {
    body.classList.add('md');
    body.innerHTML = typeof marked !== 'undefined'
      ? marked.parse(content)
      : `<pre class="code-pre">${escHtml(content)}</pre>`;
  } else if (ext === 'json') {
    let pretty = content;
    try { pretty = JSON.stringify(JSON.parse(content), null, 2); } catch { /* keep raw */ }
    body.innerHTML = codeWithLines(pretty);
  } else {
    body.innerHTML = codeWithLines(content);
  }

  card.appendChild(hdr);
  card.appendChild(body);
  viewer.innerHTML = '';
  viewer.appendChild(card);

  if (ext === 'md' && typeof mermaid !== 'undefined') {
    body.querySelectorAll('pre code.language-mermaid').forEach(code => {
      const div = document.createElement('div');
      div.className = 'mermaid';
      div.textContent = code.textContent;
      code.parentElement.replaceWith(div);
    });
    mermaid.run({ nodes: body.querySelectorAll('.mermaid') });
  }

  if (scroll) viewer.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// ── Log Viewer ────────────────────────────────────────────────────────────────

function startLogPoll() {
  clearInterval(timers.logs);
  timers.logs = setInterval(fetchLogs, 5000);
  fetchLogs();
}

async function fetchLogs() {
  let res;
  try {
    res = await apiGet('/api/logs?limit=200');
  } catch { return; }
  if (!res.ok) return;

  const records = res.data || [];
  logState.allRecords = records;

  const logsActive = document.getElementById('tab-logs')?.classList.contains('active');
  if (logsActive) {
    renderLogs();
    if (records.length > 0) logState.lastSeenId = Math.max(...records.map(r => r.id));
    updateLogBadge(0);
  } else {
    const unread = records.filter(
      r => r.id > 0 && r.id > logState.lastSeenId && (r.level === 'error' || r.level === 'warning')
    ).length;
    updateLogBadge(unread + logState.frontendUnread);
  }
}

function renderLogs() {
  const container = document.getElementById('log-list');
  let records = logState.allRecords;
  if (logState.filter !== 'all') records = records.filter(r => r.level === logState.filter);

  if (records.length === 0) {
    container.innerHTML = `<span class="muted log-empty">No ${logState.filter === 'all' ? '' : logState.filter + ' '}entries.</span>`;
    return;
  }
  container.innerHTML = '';
  records.forEach(rec => container.appendChild(buildLogEntry(rec)));
}

function buildLogEntry(rec) {
  const el = document.createElement('div');
  el.className = `log-entry log-${rec.level}`;

  const timeStr = rec.time.includes('T')
    ? rec.time.split('T')[1].replace(/\.\d+/, '').replace(/[+-]\d{2}:\d{2}$/, '')
    : rec.time;

  let ctxHtml = '';
  if (rec.context && Object.keys(rec.context).length > 0) {
    const parts = Object.entries(rec.context).map(([k, v]) => `${escHtml(k)}: ${escHtml(String(v))}`).join(' · ');
    ctxHtml = `<div class="log-ctx">${parts}</div>`;
  }

  const tbHtml = rec.traceback ? `
    <button class="log-tb-toggle" onclick="toggleTb(this)">▶ traceback</button>
    <pre class="log-tb hidden">${escHtml(rec.traceback.trim())}</pre>` : '';

  el.innerHTML = `
    <div class="log-header">
      <span class="log-lvl lvl-${rec.level}">${rec.level.toUpperCase()}</span>
      <span class="log-time">${timeStr}</span>
      <span class="log-msg">${escHtml(rec.message)}</span>
    </div>
    ${ctxHtml}${tbHtml}
  `;
  return el;
}

function toggleTb(btn) {
  const pre = btn.nextElementSibling;
  const isHidden = pre.classList.toggle('hidden');
  btn.textContent = isHidden ? '▶ traceback' : '▼ traceback';
}

function setLogFilter(level) {
  logState.filter = level;
  document.querySelectorAll('.log-filter-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.filter === level);
  });
  renderLogs();
}

async function clearLogs() {
  await apiDelete('/api/logs');
  logState.allRecords = [];
  logState.lastSeenId = 0;
  updateLogBadge(0);
  renderLogs();
}

function updateLogBadge(count) {
  const badge = document.getElementById('log-header-badge');
  if (!badge) return;
  if (count > 0) {
    badge.textContent = count > 99 ? '99+' : String(count);
    badge.classList.remove('hidden');
  } else {
    badge.classList.add('hidden');
  }
}

// ── Tab Bar Rendering ────────────────────────────────────────────────────────

function renderTabBar() {
  const bar = document.getElementById('tab-bar');
  bar.innerHTML = '';

  // Home button
  const homeBtn = document.createElement('button');
  homeBtn.className = 'tab-btn' + (activeTabId === 'home' ? ' active' : '');
  homeBtn.dataset.tab = 'home';
  homeBtn.textContent = 'Home';
  homeBtn.onclick = () => switchTab('home');
  bar.appendChild(homeBtn);

  // Explorer tabs
  for (const [tabId, ts] of Object.entries(explorerTabs)) {
    const btn = document.createElement('button');
    btn.className = 'tab-btn' + (activeTabId === tabId ? ' active' : '');
    btn.dataset.tab = tabId;

    const label = document.createElement('span');
    label.textContent = ts.sdpName;
    btn.appendChild(label);

    const closeSpan = document.createElement('span');
    closeSpan.className = 'tab-close';
    closeSpan.textContent = '×';
    closeSpan.onclick = (e) => { e.stopPropagation(); closeExplorerTab(tabId); };
    btn.appendChild(closeSpan);

    btn.onclick = () => switchTab(tabId);
    bar.appendChild(btn);
  }

}

// ── Explorer Tab Management ─────────────────────────────────────────────────

function openExplorerTab(sdpPath) {
  const tabId = _hashString(sdpPath);

  // If tab already exists, just switch to it
  if (explorerTabs[tabId]) {
    switchTab(tabId);
    return;
  }

  const sdpName = sdpPath.replace(/\\/g, '/').split('/').pop().replace(/\.sdp$/i, '');

  // Create state for this tab
  explorerTabs[tabId] = {
    sdpPath,
    sdpName,
    snapState: {
      snapshotId: null,
      snapshotDir: null,
      screenshot: null,
      runs: [],
      activeRun: null,
    },
    explorerState: {
      snapshotId: null,
      dcs: [],
      selectedApiId: null,
      colTab: 'params',
      sortCol: null,
      sortDir: 1,
    },
    questionsState: { snapshotId: null },
    questionsCtrl: {
      chartType: 'bar',
      allData: [],
      columns: [],
      selectedMetric: '',
      labelCorrs: {},
      corrCategory: null,
    },
    subTab: 'explorer',
    catFilterSel: new Set(),
    catFilterAll: [],
    _resultsState: { runs: [], activeRun: null, activeSnap: null },
  };

  // Create DOM
  createExplorerTabDOM(tabId);

  // Switch to the new tab
  switchTab(tabId);

  // Load snapshots
  loadSnapshots(tabId);
}

function closeExplorerTab(tabId) {
  destroyExplorerTabDOM(tabId);
  delete explorerTabs[tabId];
  if (activeTabId === tabId) {
    switchTab('home');
  }
  renderTabBar();
}

function createExplorerTabDOM(tabId) {
  const template = document.getElementById('explorer-tab-template');
  const clone = template.content.cloneNode(true);
  const section = clone.querySelector('section');
  section.id = `tab-${tabId}`;

  // Wire up sub-nav buttons
  section.querySelectorAll('.subnav-btn').forEach(btn => {
    btn.onclick = () => switchExplorerSubTab(tabId, btn.dataset.sub);
  });

  // Wire up chart type switches
  section.querySelectorAll('.q-switch-opt').forEach(opt => {
    opt.onclick = () => setQChartType(tabId, opt.dataset.chart);
  });

  // Wire up DC column tab buttons
  section.querySelectorAll('.dc-col-tab').forEach(btn => {
    btn.onclick = () => setDcColTab(tabId, btn.dataset.colTab);
  });

  // Wire up category filter button
  const catBtn = section.querySelector('.cat-filter-btn');
  if (catBtn) catBtn.onclick = (e) => toggleCatDropdown(tabId, e);

  // Wire up correlation reset button
  const corrReset = section.querySelector('.explorer-q-corr-reset');
  if (corrReset) corrReset.onclick = () => setQCorrCategory(tabId, null);

  // Wire up label select dropdown
  const labelSel = section.querySelector('.q-label-select');
  if (labelSel) labelSel.onchange = () => onQLabelSelect(tabId, labelSel.value);

  // Wire up detail button (go to Explorer for label)
  const detailBtn = section.querySelector('.explorer-detail-btn');
  if (detailBtn) detailBtn.onclick = () => goExplorerForLabel(tabId);

  // Append to main
  document.querySelector('main').appendChild(section);
}

function destroyExplorerTabDOM(tabId) {
  const el = document.getElementById(`tab-${tabId}`);
  if (el) el.remove();
}

// ── Snapshot Loading (per-tab) ───────────────────────────────────────────────

async function loadSnapshots(tabId) {
  const ts = getTabState(tabId);
  if (!ts) return;

  try {
    const res = await apiGet(`${DATA}/snapshots`);
    if (!res.ok) return;

    // Group by run_name
    const runMap = new Map();
    (res.data || []).forEach(s => {
      if (!runMap.has(s.run_name)) runMap.set(s.run_name, []);
      runMap.get(s.run_name).push(s);
    });
    ts.snapState.runs = [...runMap.entries()].map(([name, snaps]) => ({ name, snapshots: snaps }));

    // Try to find the run that matches this SDP
    const sdpName = ts.sdpPath.replace(/\\/g, '/').split('/').pop();
    const sdpStem = sdpName.replace(/\.sdp$/i, '');
    let matchedRun = null;
    let matchedSnap = null;
    for (const run of ts.snapState.runs) {
      const match = run.snapshots.find(s =>
        s.sdp_name === sdpName || s.sdp_name === sdpStem || s.run_name === sdpStem
      );
      if (match) {
        matchedRun = run;
        matchedSnap = match;
        break;
      }
    }

    if (matchedRun) {
      ts.snapState.activeRun = matchedRun.name;
      renderSnapshotTabs(tabId, matchedRun.snapshots);
      if (matchedSnap) selectSnapshot(tabId, matchedSnap);
    } else if (ts.snapState.runs.length) {
      ts.snapState.activeRun = ts.snapState.runs[0].name;
      renderSnapshotTabs(tabId, ts.snapState.runs[0].snapshots);
      if (ts.snapState.runs[0].snapshots.length) {
        selectSnapshot(tabId, ts.snapState.runs[0].snapshots[0]);
      }
    }
  } catch (err) {
    console.warn('loadSnapshots error:', err.message);
  }
}

function renderSnapshotTabs(tabId, snapshots) {
  const el = getTabEl(tabId);
  if (!el) return;
  const ts = getTabState(tabId);
  if (!ts) return;

  const container = el.querySelector('.explorer-snap-tabs');
  container.innerHTML = '';

  snapshots.forEach(s => {
    const btn = document.createElement('button');
    btn.className = 'snap-tab-btn' + (s.snapshot_id === ts.snapState.snapshotId ? ' active' : '');
    btn.title = s.snapshot_dir || '';
    btn.onclick = () => selectSnapshot(tabId, s);

    const label = document.createElement('span');
    label.textContent = `#${s.snapshot_id} ${s.sdp_name || s.run_name || ''}`;
    btn.appendChild(label);

    const isPinned = typeof chatState !== 'undefined' && chatState.pinnedSnapshotIds.includes(s.snapshot_id);
    const pinIcon = document.createElement('span');
    pinIcon.className = 'snap-pin-icon' + (isPinned ? ' pinned' : '');
    pinIcon.innerHTML = isPinned ? PIN_SVG : UNPIN_SVG;
    pinIcon.title = isPinned ? 'Unpin from chat' : 'Pin to chat';
    pinIcon.onclick = (e) => { e.stopPropagation(); togglePinSnapshot(s.snapshot_id, pinIcon); };
    btn.appendChild(pinIcon);

    container.appendChild(btn);
  });
}

function togglePinSnapshot(snapshotId, iconEl) {
  if (typeof chatState === 'undefined') return;
  const isPinned = chatState.pinnedSnapshotIds.includes(snapshotId);
  if (isPinned) {
    unpinSnapshot(snapshotId);
    iconEl.className = 'snap-pin-icon';
    iconEl.innerHTML = UNPIN_SVG;
    iconEl.title = 'Pin to chat';
  } else {
    pinSnapshot(snapshotId);
    iconEl.className = 'snap-pin-icon pinned';
    iconEl.innerHTML = PIN_SVG;
    iconEl.title = 'Unpin from chat';
  }
}

function selectSnapshot(tabId, snapRow) {
  const ts = getTabState(tabId);
  if (!ts) return;
  const el = getTabEl(tabId);
  if (!el) return;

  ts.snapState.snapshotId  = snapRow.snapshot_id;
  ts.snapState.snapshotDir = snapRow.snapshot_dir;
  ts.snapState.screenshot  = snapRow.screenshot || null;

  // Update snapshot tab active states
  el.querySelectorAll('.snap-tab-btn').forEach(b => {
    const text = b.textContent;
    const match = text.match(/^#(\d+)/);
    if (match) b.classList.toggle('active', parseInt(match[1], 10) === snapRow.snapshot_id);
  });

  // Notify Explorer state
  ts.explorerState.snapshotId = snapRow.snapshot_id;
  ts.explorerState.selectedApiId = null;
  ts.catFilterSel.clear();
  const cols = el.querySelector('.explorer-columns');
  if (cols) cols.style.display = 'none';
  const detail = el.querySelector('.explorer-detail-panel');
  if (detail) detail.innerHTML = '';

  // Notify Questions state
  ts.questionsState.snapshotId = snapRow.snapshot_id;
  // Reset drill-down
  ts.questionsCtrl.corrCategory = null;
  const corrTitle = el.querySelector('.explorer-q-corr-title');
  if (corrTitle) corrTitle.textContent = 'Clock Correlation (R²) — All DCs';
  const corrReset = el.querySelector('.explorer-q-corr-reset');
  if (corrReset) corrReset.style.display = 'none';

  // Sync chat context
  if (typeof chatState !== 'undefined') {
    chatState.activeSnapshotId = snapRow.snapshot_id;
    updateChatContextBar();
  }

  // Show meta row (contains subnav) + render metadata + screenshot
  const metaRow = el.querySelector('.explorer-meta-row');
  if (metaRow) metaRow.style.display = '';
  renderSessionMetaBar(tabId);
  renderSnapScreenshot(tabId, snapRow);

  // Load data based on active sub-tab
  if (ts.subTab === 'explorer') {
    loadExplorerDCs(tabId);
  } else if (ts.subTab === 'questions') {
    fetchClockCorrelation(tabId, snapRow.snapshot_id);
    fetchQuestionsData(tabId);
    if (snapRow.snapshot_dir) {
      const normDir = normPath(snapRow.snapshot_dir);
      const parts   = normDir.replace(/\\/g, '/').split('/');
      scanAnalyses(tabId, parts.slice(0, -2).join('/'), parts[parts.length - 2], parts[parts.length - 1]);
    }
  }
}

function renderSessionMetaBar(tabId) {
  const el = getTabEl(tabId);
  if (!el) return;
  const bar = el.querySelector('.explorer-meta-bar');
  if (!bar) return;

  const ts = getTabState(tabId);
  const info = (ts && sdpInfoCache[ts.sdpPath]) || {};

  const projectId = info.project_id;
  const versionId = info.version_id;
  const projName = (projectId && _homeProjects.find(p => p.id === projectId))?.name || '';
  const verName = (versionId && _allVersions.find(v => v.id === versionId))?.name || '';
  const api = info.api || sdpApiCache[ts.sdpPath] || '';

  const timeStr = info.capture_time ? new Date(info.capture_time).toLocaleString() : '';
  const chips = [
    projName ? { label: 'Project', value: projName } : null,
    info.app ? { label: 'App', value: info.app } : null,
    verName ? { label: 'Version', value: verName } : null,
    timeStr ? { label: 'Time', value: timeStr } : null,
    info.size ? { label: 'Size', value: (info.size / 1048576).toFixed(1) + ' MB' } : null,
    api ? { label: 'API', value: api } : null,
    info.gpu_renderer ? { label: 'Device', value: info.gpu_renderer } : null,
  ].filter(Boolean);

  const row = el.querySelector('.explorer-meta-row');
  if (row) row.style.display = '';

  bar.innerHTML = chips.map(c => {
    return `<span class="meta-chip"><span class="meta-chip-label">${c.label}</span><span class="meta-chip-value">${escHtml(c.value)}</span></span>`;
  }).join('');
}

function refreshAllExplorerMetaBars() {
  for (const tabId of Object.keys(explorerTabs)) {
    renderSessionMetaBar(tabId);
  }
}

function renderSnapScreenshot(tabId, snapRow) {
  const el = getTabEl(tabId);
  if (!el) return;
  const container = el.querySelector('.explorer-snap-screenshot');
  if (!container) return;

  if (!snapRow.screenshot) {
    container.innerHTML = '';
    return;
  }
  const ts = getTabState(tabId);
  const isVulkan = ts && sdpApiCache[ts.sdpPath] === 'Vulkan';
  const rotateParam = isVulkan ? '&rotate=-90' : '';
  const src = `${FILES}/image?path=${encodeURIComponent(snapRow.screenshot)}${rotateParam}`;
  container.innerHTML = `<img class="snap-screenshot-img" src="${src}" alt="screenshot" onerror="this.style.display='none'">`;
}

async function reIngest(tabId) {
  const ts = getTabState(tabId);
  if (!ts) return;
  const snapDir = ts.snapState.snapshotDir;
  if (!snapDir) return;
  try {
    const res = await apiPost(`${JOBS}/ingest?snapshot_dir=${encodeURIComponent(snapDir)}`, {});
    if (!res.ok) return;
    if (ts.subTab === 'explorer') loadExplorerDCs(tabId);
    else if (ts.subTab === 'questions') { fetchClockCorrelation(tabId, res.snapshot_id); fetchQuestionsData(tabId); }
  } catch (err) {
    console.warn('reIngest error:', err.message);
  }
}

// ── Explorer DC Loading (per-tab) ───────────────────────────────────────────

// Category filter helpers scoped to tab
function _catFilterActive(tabId) {
  const ts = getTabState(tabId);
  if (!ts) return false;
  return ts.catFilterSel.size > 0 && ts.catFilterSel.size < ts.catFilterAll.length;
}

function _buildCatDropdown(tabId, categories) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  ts.catFilterAll = categories;
  const dd = el.querySelector('.cat-filter-dropdown');
  if (!dd) return;
  dd.innerHTML = '';

  // "All" toggle
  const allLbl = document.createElement('label');
  allLbl.className = 'cat-dd-all';
  const allChk = document.createElement('input');
  allChk.type = 'checkbox';
  allChk.checked = ts.catFilterSel.size === 0;
  allChk.onchange = () => {
    ts.catFilterSel.clear();
    dd.querySelectorAll('.cat-item-chk').forEach(c => { c.checked = false; });
    allChk.checked = true;
    _applyCatFilter(tabId);
  };
  allLbl.appendChild(allChk);
  allLbl.appendChild(document.createTextNode('All'));
  dd.appendChild(allLbl);

  const sep = document.createElement('div');
  sep.className = 'cat-dd-sep';
  dd.appendChild(sep);

  categories.forEach(cat => {
    const lbl = document.createElement('label');
    const chk = document.createElement('input');
    chk.type = 'checkbox';
    chk.className = 'cat-item-chk';
    chk.checked = ts.catFilterSel.has(cat);
    chk.dataset.cat = cat;
    chk.onchange = () => {
      if (chk.checked) ts.catFilterSel.add(cat);
      else             ts.catFilterSel.delete(cat);
      // If none checked → treat as all
      if (ts.catFilterSel.size === 0) allChk.checked = true;
      else allChk.checked = false;
      _applyCatFilter(tabId);
    };
    const dot = document.createElement('span');
    dot.style.cssText = `display:inline-block;width:10px;height:10px;border-radius:2px;background:${_catColor(cat)};flex-shrink:0`;
    lbl.appendChild(chk);
    lbl.appendChild(dot);
    lbl.appendChild(document.createTextNode(' ' + cat));
    dd.appendChild(lbl);
  });
}

function _updateCatFilterBtn(tabId) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  const btn = el.querySelector('.cat-filter-btn');
  if (!btn) return;
  if (ts.catFilterSel.size === 0 || ts.catFilterSel.size === ts.catFilterAll.length) {
    btn.textContent = 'All categories ▾';
  } else if (ts.catFilterSel.size === 1) {
    btn.textContent = [...ts.catFilterSel][0] + ' ▾';
  } else {
    btn.textContent = `${ts.catFilterSel.size} categories ▾`;
  }
}

function _applyCatFilter(tabId) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  _updateCatFilterBtn(tabId);
  const filtered = _catFilterActive(tabId)
    ? ts.explorerState.dcs.filter(dc => ts.catFilterSel.has(dc.category || ''))
    : ts.explorerState.dcs;
  const cnt = el.querySelector('.explorer-dc-count');
  if (cnt) cnt.textContent = `(${filtered.length} DCs)`;
  renderExplorerDCTable(tabId, filtered);
  renderClockChart(tabId, filtered);
}

function toggleCatDropdown(tabId, e) {
  e.stopPropagation();
  const el = getTabEl(tabId);
  if (!el) return;
  const dd = el.querySelector('.cat-filter-dropdown');
  if (!dd) return;
  dd.style.display = dd.style.display === 'none' ? '' : 'none';
}

// Close dropdown when clicking outside
document.addEventListener('click', e => {
  // Close any open cat-filter-dropdown
  document.querySelectorAll('.cat-filter-dropdown').forEach(dd => {
    if (dd.style.display !== 'none') {
      const wrap = dd.closest('.cat-filter-wrap');
      if (wrap && !wrap.contains(e.target)) {
        dd.style.display = 'none';
      }
    }
  });
});

async function loadExplorerDCs(tabId) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  const snapId = ts.explorerState.snapshotId;
  if (!snapId) return;
  // Always fetch all DCs; filter client-side so dropdown stays populated
  const url = `${DATA}/draw_calls?snapshot_id=${snapId}`;

  const tbl = el.querySelector('.explorer-dc-table');
  tbl.innerHTML = '<span class="muted" style="font-size:12px">Loading…</span>';
  el.querySelector('.explorer-columns').style.display = '';

  try {
    const res = await apiGet(url);
    if (!res.ok) {
      tbl.innerHTML = `<span class="s-error">${escHtml(res.error)}</span>`;
      return;
    }
    ts.explorerState.dcs = res.data || [];

    // Collect unique categories in appearance order
    const catSeen = new Set();
    const cats = [];
    for (const dc of ts.explorerState.dcs) {
      const c = dc.category || '';
      if (!catSeen.has(c)) { catSeen.add(c); cats.push(c); }
    }
    // Remove stale selections that no longer exist
    for (const c of [...ts.catFilterSel]) {
      if (!catSeen.has(c)) ts.catFilterSel.delete(c);
    }
    _buildCatDropdown(tabId, cats);
    _updateCatFilterBtn(tabId);
    _applyCatFilter(tabId);

    // Auto-select first DC and show detail
    if (ts.explorerState.dcs.length > 0 && !ts.explorerState.selectedApiId) {
      loadExplorerDCDetail(tabId, ts.explorerState.dcs[0].api_id);
    }
  } catch (err) {
    tbl.innerHTML = `<span class="s-error">${escHtml(err.message)}</span>`;
  }
}

// ── DC list column-tab state ─────────────────────────────────────────────────

function setDcColTab(tabId, tab) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  ts.explorerState.colTab = tab;
  el.querySelectorAll('.dc-col-tab').forEach(b =>
    b.classList.toggle('active', b.dataset.colTab === tab));
  _applyCatFilter(tabId);
}

// Column definitions per tab.
// Each col: { key, label, val(dc, seqNo) }
function _dcColDefs(tab) {
  const SEQ   = { key: '_seq',     label: '#',        val: (dc, i) => i + 1 };
  const APID  = { key: 'api_id',   label: 'API ID',   val: dc => dc.api_id };
  const NAME  = { key: 'api_name', label: 'API Name', val: dc => dc.api_name || '—' };

  if (tab === 'params') return [
    SEQ, APID, NAME,
    { key: 'vertex_count',   label: 'Verts',    val: dc => dc.vertex_count   ?? '—' },
    { key: 'index_count',    label: 'Indices',  val: dc => dc.index_count    ?? '—' },
    { key: 'instance_count', label: 'Inst',     val: dc => dc.instance_count ?? '—' },
    { key: 'first_vertex',   label: 'fVtx',     val: dc => dc.first_vertex   ?? '—' },
    { key: 'first_index',    label: 'fIdx',     val: dc => dc.first_index    ?? '—' },
    { key: 'vertex_offset',  label: 'vOff',     val: dc => dc.vertex_offset  ?? '—' },
    { key: 'first_instance', label: 'fInst',    val: dc => dc.first_instance ?? '—' },
    { key: 'draw_count',     label: 'drawCnt',  val: dc => dc.draw_count     ?? '—' },
  ];

  if (tab === 'metrics') return [
    SEQ, APID, NAME,
    { key: 'clocks',                 label: 'Clocks',        val: dc => dc.clocks               ?? '—' },
    { key: 'fragments_shaded',       label: 'Frags',         val: dc => dc.fragments_shaded      ?? '—' },
    { key: 'vertices_shaded',        label: 'Verts',         val: dc => dc.vertices_shaded       ?? '—' },
    { key: 'read_total_bytes',       label: 'Read(B)',       val: dc => dc.read_total_bytes      ?? '—' },
    { key: 'write_total_bytes',      label: 'Write(B)',      val: dc => dc.write_total_bytes     ?? '—' },
    { key: 'shaders_busy_pct',       label: 'ShBusy%',      val: dc => _fmt1(dc.shaders_busy_pct) },
    { key: 'shaders_stalled_pct',    label: 'ShStall%',     val: dc => _fmt1(dc.shaders_stalled_pct) },
    { key: 'time_alus_working_pct',  label: 'ALU%',         val: dc => _fmt1(dc.time_alus_working_pct) },
    { key: 'tex_fetch_stall_pct',    label: 'TexStall%',    val: dc => _fmt1(dc.tex_fetch_stall_pct) },
    { key: 'tex_l1_miss_pct',        label: 'TexL1Miss%',   val: dc => _fmt1(dc.tex_l1_miss_pct) },
    { key: 'tex_pipes_busy_pct',     label: 'TexPipes%',    val: dc => _fmt1(dc.tex_pipes_busy_pct) },
    { key: 'lrz_pixels_killed',      label: 'LRZ',          val: dc => dc.lrz_pixels_killed     ?? '—' },
  ];

  // label tab
  return [
    SEQ, APID, NAME,
    { key: 'category',     label: 'Category',   val: dc => dc.category     || '—' },
    { key: 'subcategory',  label: 'Subcategory',val: dc => dc.subcategory  || '—' },
    { key: 'detail',       label: 'Detail',     val: dc => dc.detail       || '—' },
    { key: 'confidence',   label: 'Conf',       val: dc => dc.confidence != null ? dc.confidence.toFixed(2) : '—' },
    { key: 'label_source', label: 'Source',     val: dc => dc.label_source || '—' },
  ];
}

function _fmt1(v) { return v != null ? (+v).toFixed(1) : '—'; }

function renderExplorerDCTable(tabId, dcs) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  const container = el.querySelector('.explorer-dc-table');
  if (!dcs || dcs.length === 0) {
    container.innerHTML = '<span class="muted" style="font-size:12px">No draw calls found.</span>';
    return;
  }

  const cols    = _dcColDefs(ts.explorerState.colTab);
  const sortCol = ts.explorerState.sortCol;
  const sortDir = ts.explorerState.sortDir;

  // Build indexed array preserving original order as seq number
  let rows = dcs.map((dc, i) => ({ dc, seq: i + 1 }));

  // Sort — null sortCol = original order (seq)
  if (sortCol && sortCol !== '_seq') {
    let colDef = cols.find(c => c.key === sortCol);
    if (!colDef) {
      for (const t of ['params', 'metrics', 'label']) {
        colDef = _dcColDefs(t).find(c => c.key === sortCol);
        if (colDef) break;
      }
    }
    if (colDef) {
      rows.sort((a, b) => {
        const va = colDef.val(a.dc, a.seq - 1);
        const vb = colDef.val(b.dc, b.seq - 1);
        const aNone = va === '—' || va == null || va === '';
        const bNone = vb === '—' || vb == null || vb === '';
        if (aNone && bNone) return 0;
        if (aNone) return 1;
        if (bNone) return -1;
        if (!isNaN(+va) && !isNaN(+vb))
          return sortDir * (+va - +vb);
        return sortDir * String(va).localeCompare(String(vb));
      });
    }
  } else if (sortCol === '_seq') {
    rows.sort((a, b) => sortDir * (a.seq - b.seq));
  }

  const table = document.createElement('table');
  table.className = 'explorer-dc-table';

  // Header
  const thead = table.createTHead();
  const hrow  = thead.insertRow();
  cols.forEach(col => {
    const th = document.createElement('th');
    const isSorted = col.key === sortCol;
    th.innerHTML = escHtml(col.label)
      + (isSorted ? (sortDir === 1 ? ' <span class="sort-arrow">▲</span>' : ' <span class="sort-arrow">▼</span>') : '');
    if (col.key !== '_seq' && col.key !== '_groups') {
      th.classList.add('sortable');
      th.onclick = () => {
        if (ts.explorerState.sortCol === col.key) {
          ts.explorerState.sortDir *= -1;
        } else {
          ts.explorerState.sortCol = col.key;
          ts.explorerState.sortDir = 1;
        }
        renderExplorerDCTable(tabId, ts.explorerState.dcs);
      };
    }
    hrow.appendChild(th);
  });

  // Body
  const tbody = table.createTBody();
  rows.forEach(({ dc, seq }) => {
    const tr = tbody.insertRow();
    tr.className = 'dc-row' + (dc.api_id === ts.explorerState.selectedApiId ? ' active' : '');
    tr.onclick = () => loadExplorerDCDetail(tabId, dc.api_id);

    cols.forEach((col, ci) => {
      const td = tr.insertCell();
      const val = col.val(dc, seq - 1);
      td.textContent = val;
      if (ci === 0) td.className = 'dc-seq-cell';  // seq column styling
    });
  });

  container.innerHTML = '';
  container.appendChild(table);
}

// ── DC clock bar chart ────────────────────────────────────────────────────────

// Category → bar colour (mirrors label tab colour cues)
const _CAT_COLORS = {
  Scene:      '#3b82f6',
  Shadow:     '#64748b',
  UI:         '#8b5cf6',
  PostFX:     '#ec4899',
  Character:  '#f59e0b',
  Terrain:    '#22c55e',
  Particles:  '#f97316',
  Compute:    '#06b6d4',
  Unknown:    '#94a3b8',
};
function _catColor(cat) {
  if (!cat) return '#94a3b8';
  for (const [k, v] of Object.entries(_CAT_COLORS)) {
    if (cat.toLowerCase().startsWith(k.toLowerCase())) return v;
  }
  return '#94a3b8';
}

function renderClockChart(tabId, dcs) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  const wrap = el.querySelector('.explorer-dc-clock-chart-wrap');
  const canvas = el.querySelector('.dc-clock-chart');
  const tooltip = el.querySelector('.dc-clock-tooltip');
  if (!wrap || !canvas) return;

  // Only show if at least some DCs have clock data
  const hasClock = dcs.some(dc => dc.clocks != null && dc.clocks > 0);
  if (!hasClock) { wrap.style.display = 'none'; return; }
  wrap.style.display = '';

  const PADDING = { top: 12, right: 8, bottom: 20, left: 44 };
  const W = canvas.offsetWidth || canvas.parentElement.offsetWidth || 600;
  const H = 220;
  canvas.width  = W;
  canvas.height = H;

  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, W, H);

  const chartW = W - PADDING.left - PADDING.right;
  const chartH = H - PADDING.top  - PADDING.bottom;

  const maxClock = Math.max(...dcs.map(dc => dc.clocks || 0));
  if (maxClock === 0) { wrap.style.display = 'none'; return; }

  const barW   = Math.max(1, chartW / dcs.length);
  const gap    = barW > 4 ? Math.max(1, barW * 0.15) : 0;
  const fillW  = barW - gap;

  // Y-axis labels
  ctx.fillStyle = '#94a3b8';
  ctx.font = '10px system-ui, sans-serif';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  const yTicks = 3;
  for (let i = 0; i <= yTicks; i++) {
    const v = (maxClock * i) / yTicks;
    const y = PADDING.top + chartH - (chartH * i / yTicks);
    ctx.fillText(_fmtK(v), PADDING.left - 4, y);
    ctx.strokeStyle = '#e5e7eb';
    ctx.lineWidth = 0.5;
    ctx.beginPath();
    ctx.moveTo(PADDING.left, y);
    ctx.lineTo(PADDING.left + chartW, y);
    ctx.stroke();
  }

  // Bars
  dcs.forEach((dc, i) => {
    const clk = dc.clocks || 0;
    const barH = (clk / maxClock) * chartH;
    const x = PADDING.left + i * barW + gap / 2;
    const y = PADDING.top + chartH - barH;
    ctx.fillStyle = dc.api_id === ts.explorerState.selectedApiId
      ? '#f59e0b'
      : _catColor(dc.category);
    ctx.fillRect(x, y, fillW, barH);
  });

  // X-axis baseline
  ctx.strokeStyle = '#cbd5e1';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(PADDING.left, PADDING.top + chartH);
  ctx.lineTo(PADDING.left + chartW, PADDING.top + chartH);
  ctx.stroke();

  // Hover / click handling
  canvas._chartMeta = { dcs, barW, gap, PADDING, chartH, maxClock, fillW };

  canvas.onmousemove = (e) => {
    const rect = canvas.getBoundingClientRect();
    const mx = (e.clientX - rect.left) * (canvas.width / rect.width);
    const meta = canvas._chartMeta;
    const idx = Math.floor((mx - meta.PADDING.left) / meta.barW);
    if (idx < 0 || idx >= meta.dcs.length) { tooltip.style.display = 'none'; return; }
    const dc = meta.dcs[idx];
    tooltip.style.display = '';
    tooltip.textContent = `#${dc.api_id} ${dc.api_name || ''} | clocks: ${(dc.clocks||0).toLocaleString()} | ${dc.category || '—'}`;
    const tx = Math.min(e.offsetX + 12, wrap.offsetWidth - tooltip.offsetWidth - 4);
    const ty = Math.max(0, e.offsetY - 28);
    tooltip.style.left = tx + 'px';
    tooltip.style.top  = ty + 'px';
  };
  canvas.onmouseleave = () => { tooltip.style.display = 'none'; };
  canvas.onclick = (e) => {
    const rect = canvas.getBoundingClientRect();
    const mx = (e.clientX - rect.left) * (canvas.width / rect.width);
    const meta = canvas._chartMeta;
    const idx = Math.floor((mx - meta.PADDING.left) / meta.barW);
    if (idx >= 0 && idx < meta.dcs.length) {
      loadExplorerDCDetail(tabId, meta.dcs[idx].api_id);
    }
  };
}

function _fmtK(v) {
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M';
  if (v >= 1_000)     return (v / 1_000).toFixed(0) + 'K';
  return String(Math.round(v));
}

async function loadExplorerDCDetail(tabId, apiId) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  ts.explorerState.selectedApiId = apiId;
  // Re-render table + chart to update active highlight
  _applyCatFilter(tabId);

  const panel = el.querySelector('.explorer-detail-panel');
  panel.innerHTML = '<div class="card"><span class="muted">Loading…</span></div>';

  try {
    const res = await apiGet(`${DATA}/dc/${apiId}?snapshot_id=${ts.explorerState.snapshotId}`);
    if (!res.ok) {
      panel.innerHTML = `<div class="card"><span class="s-error">${escHtml(res.error)}</span></div>`;
      return;
    }
    renderExplorerDCDetail(tabId, panel, res.data);
  } catch (err) {
    panel.innerHTML = `<div class="card"><span class="s-error">${escHtml(err.message)}</span></div>`;
  }
}

// Return a compact param string based on api_name
function _dcParamSummary(dc) {
  const n = dc.api_name || '';
  if (n === 'vkCmdDraw')
    return `vtx=${dc.vertex_count ?? 0} inst=${dc.instance_count ?? 0} fv=${dc.first_vertex ?? 0}`;
  if (n === 'vkCmdDrawIndexed')
    return `idx=${dc.index_count ?? 0} inst=${dc.instance_count ?? 0} fi=${dc.first_index ?? 0} vo=${dc.vertex_offset ?? 0}`;
  if (n === 'vkCmdDrawIndirect' || n === 'vkCmdDrawIndexedIndirect')
    return `drawCount=${dc.draw_count ?? 0}`;
  if (n === 'vkCmdDispatch')
    return `${dc.group_count_x ?? 0}×${dc.group_count_y ?? 0}×${dc.group_count_z ?? 0}`;
  // fallback
  if ((dc.vertex_count ?? 0) > 0) return `vtx=${dc.vertex_count}`;
  if ((dc.index_count  ?? 0) > 0) return `idx=${dc.index_count}`;
  return '—';
}

// Return full params kv pairs for the detail panel
function _dcParamRows(dc) {
  const n = dc.api_name || '';
  if (n === 'vkCmdDraw') return [
    ['vertexCount',   dc.vertex_count   ?? 0],
    ['instanceCount', dc.instance_count ?? 0],
    ['firstVertex',   dc.first_vertex   ?? 0],
    ['firstInstance', dc.first_instance ?? 0],
  ];
  if (n === 'vkCmdDrawIndexed') return [
    ['indexCount',    dc.index_count    ?? 0],
    ['instanceCount', dc.instance_count ?? 0],
    ['firstIndex',    dc.first_index    ?? 0],
    ['vertexOffset',  dc.vertex_offset  ?? 0],
    ['firstInstance', dc.first_instance ?? 0],
  ];
  if (n === 'vkCmdDrawIndirect' || n === 'vkCmdDrawIndexedIndirect') return [
    ['drawCount', dc.draw_count ?? 0],
  ];
  if (n === 'vkCmdDispatch') return [
    ['groupCountX', dc.group_count_x ?? 0],
    ['groupCountY', dc.group_count_y ?? 0],
    ['groupCountZ', dc.group_count_z ?? 0],
  ];
  // fallback — show whatever is non-zero
  const rows = [];
  if ((dc.vertex_count   ?? 0) !== 0) rows.push(['vertexCount',   dc.vertex_count]);
  if ((dc.index_count    ?? 0) !== 0) rows.push(['indexCount',    dc.index_count]);
  if ((dc.instance_count ?? 0) !== 0) rows.push(['instanceCount', dc.instance_count]);
  if ((dc.draw_count     ?? 0) !== 0) rows.push(['drawCount',     dc.draw_count]);
  if ((dc.group_count_x  ?? 0) !== 0) rows.push(['groupCountX',   dc.group_count_x]);
  return rows.length ? rows : [['(no params)', '—']];
}

function renderExplorerDCDetail(tabId, container, dc) {
  const ts = getTabState(tabId);
  if (!ts) return;
  container.innerHTML = '';

  const card = document.createElement('div');
  card.className = 'card';

  // ── Compact top: DC params + label + metrics in one card ──────────
  const titleRow = document.createElement('div');
  titleRow.className = 'dc-detail-title';
  titleRow.innerHTML = `<span>Draw Call #${dc.api_id}</span>`;
  card.appendChild(titleRow);

  const topGrid = document.createElement('div');
  topGrid.className = 'dc-detail-grid';

  // Params column
  const paramsCol = document.createElement('div');
  paramsCol.className = 'dc-detail-col';
  const lbl = dc.label;
  const paramRows = [
    ['API Name',    dc.api_name    || '—'],
    ['Pipeline',    dc.pipeline_id ?? '—'],
    ..._dcParamRows(dc),
    ['Category',    lbl?.category    || '—'],
    ['Subcategory', lbl?.subcategory || '—'],
    ['Detail',      lbl?.detail      || '—'],
    ['Confidence',  lbl?.confidence != null ? Number(lbl.confidence).toFixed(2) : '—'],
    ['Label src',   lbl?.label_source || '—'],
  ];
  paramRows.forEach(([k, v]) => {
    const row = document.createElement('div');
    row.className = 'dc-kv-row';
    row.innerHTML = `<span class="dc-kv-key">${escHtml(k)}</span><span class="dc-kv-val">${escHtml(String(v))}</span>`;
    paramsCol.appendChild(row);
  });
  // Relabel button
  const relabelBtn = document.createElement('button');
  relabelBtn.textContent = 'Relabel';
  relabelBtn.className = 'btn-secondary btn-sm';
  relabelBtn.style.cssText = 'margin-top:8px;font-size:11px;padding:2px 10px;cursor:pointer';
  relabelBtn.onclick = async () => {
    relabelBtn.disabled = true;
    relabelBtn.textContent = 'Labeling…';
    try {
      const snapDir = ts.snapState.snapshotDir;
      const res = await apiPost(`${JOBS}/relabel_single?snapshot_dir=${encodeURIComponent(snapDir)}&api_id=${dc.api_id}`);
      if (res.ok) {
        relabelBtn.textContent = 'Done';
        // Reload this DC detail to show new label
        setTimeout(() => loadExplorerDCDetail(tabId, dc.api_id), 500);
      } else {
        relabelBtn.textContent = res.error || 'Failed';
      }
    } catch (e) {
      relabelBtn.textContent = 'Error';
    }
    setTimeout(() => { relabelBtn.textContent = 'Relabel'; relabelBtn.disabled = false; }, 3000);
  };
  paramsCol.appendChild(relabelBtn);
  topGrid.appendChild(paramsCol);

  // Metrics column
  const metricsCol = document.createElement('div');
  metricsCol.className = 'dc-detail-col';
  const stats = dc.metric_stats || {};
  if (dc.metrics && Object.keys(dc.metrics).length > 0) {
    const metricEntries = Object.entries(dc.metrics).filter(([k]) =>
      !['snapshot_id', 'api_id'].includes(k));
    metricEntries.forEach(([k, v]) => {
      const row = document.createElement('div');
      row.className = 'dc-kv-row dc-metric-row';

      const s = stats[k];
      let heatBg = '';
      let medianHtml = '';
      if (s && s.min != null && s.max != null && s.median != null && typeof v === 'number') {
        const val = v, mn = s.min, mx = s.max, med = s.median;
        // normalise 0→green, 0.5→yellow, 1→red relative to [min, max]
        const range = mx - mn;
        const t = range > 0 ? Math.max(0, Math.min(1, (val - mn) / range)) : 0;
        const r = Math.round(t < 0.5 ? t * 2 * 255 : 255);
        const g = Math.round(t < 0.5 ? 255 : (1 - t) * 2 * 255);
        heatBg = `background:rgba(${r},${g},0,0.18);border-radius:3px;`;
        const fmtMed = med >= 1e6 ? (med/1e6).toFixed(1)+'M'
                     : med >= 1e3 ? (med/1e3).toFixed(1)+'K'
                     : med.toFixed(1);
        medianHtml = `<span class="dc-metric-median" title="median / min / max">`
          + `med:${fmtMed}</span>`;
      }

      const fmtVal = typeof v === 'number'
        ? (v >= 1e6 ? (v/1e6).toFixed(2)+'M' : v >= 1e3 ? (v/1e3).toFixed(1)+'K' : String(v))
        : (v != null ? String(v) : '—');

      row.style.cssText = heatBg;
      row.innerHTML = `<span class="dc-kv-key">${escHtml(k)}</span>`
        + `<span class="dc-kv-val">${escHtml(fmtVal)}</span>`
        + medianHtml;
      metricsCol.appendChild(row);
    });
  } else {
    metricsCol.innerHTML = '<span class="muted" style="font-size:12px">No metrics.</span>';
  }
  topGrid.appendChild(metricsCol);
  card.appendChild(topGrid);

  // ── Sub-lists ─────────────────────────────────────────────────────

  // Shaders sub-list
  const shadersSection = _buildDetailSection(`Shaders (${(dc.shader_stages||[]).length})`, true);
  if (dc.shader_stages && dc.shader_stages.length > 0) {
    const list = document.createElement('div');
    list.className = 'dc-sublist';
    dc.shader_stages.forEach(s => {
      const item = _buildSublistItem(
        `${s.stage || '?'} · ${s.entry_point || s.module_id || '—'}`,
        null
      );
      if (s.file_path) {
        const fp   = s.file_path;
        const name = fp.split(/[\\/]/).pop();
        const ext  = name.split('.').pop().toLowerCase();
        // Compute alternate file path: glsl↔disasm, hlsl↔spv
        const altMap = { glsl: 'disasm', disasm: 'glsl', hlsl: 'spv', spv: 'hlsl' };
        const altExt = altMap[ext];
        const altFp  = altExt ? fp.replace(/\.[^.]+$/, '.' + altExt) : null;
        item.setExpand(() => {
          const wrap = document.createElement('div');
          wrap.className = 'dc-sublist-preview';
          // Track current file for download
          let currentPath = (ext === 'glsl' || ext === 'hlsl') ? fp : (altFp || fp);
          // Toggle bar (source ↔ raw) + download button
          if (altFp) {
            const srcLabel = (ext === 'glsl' || ext === 'hlsl') ? ext.toUpperCase() : altExt.toUpperCase();
            const rawLabel = (ext === 'disasm' || ext === 'spv') ? ext.toUpperCase() : altExt.toUpperCase();
            const srcPath  = (ext === 'glsl' || ext === 'hlsl') ? fp : altFp;
            const rawPath  = (ext === 'disasm' || ext === 'spv') ? fp : altFp;
            const toggle = document.createElement('div');
            toggle.style.cssText = 'display:flex;gap:4px;margin-bottom:4px;align-items:center';
            const btnSrc = document.createElement('button');
            btnSrc.textContent = srcLabel;
            btnSrc.className = 'tab-btn active';
            btnSrc.style.cssText = 'font-size:11px;padding:2px 8px;cursor:pointer;border:1px solid var(--border);border-radius:3px;background:var(--bg-card)';
            const btnRaw = document.createElement('button');
            btnRaw.textContent = rawLabel;
            btnRaw.className = 'tab-btn';
            btnRaw.style.cssText = 'font-size:11px;padding:2px 8px;cursor:pointer;border:1px solid var(--border);border-radius:3px;background:transparent';
            // Download button
            const dlBtn = document.createElement('a');
            dlBtn.textContent = 'Download';
            dlBtn.className = 'btn-secondary btn-sm';
            dlBtn.style.cssText = 'margin-left:auto;font-size:11px;padding:2px 10px;text-decoration:none;cursor:pointer';
            dlBtn.href = `${FILES}/raw?path=${encodeURIComponent(currentPath)}&download=1`;
            dlBtn.download = currentPath.split(/[\\/]/).pop();
            const content = document.createElement('div');
            const loadFile = (path) => {
              currentPath = path;
              dlBtn.href = `${FILES}/raw?path=${encodeURIComponent(path)}&download=1`;
              dlBtn.download = path.split(/[\\/]/).pop();
              const n = path.split(/[\\/]/).pop();
              const e = n.split('.').pop().toLowerCase();
              viewFileInto(content, path, n, e);
            };
            btnSrc.onclick = () => {
              btnSrc.style.background = 'var(--bg-card)'; btnRaw.style.background = 'transparent';
              loadFile(srcPath);
            };
            btnRaw.onclick = () => {
              btnRaw.style.background = 'var(--bg-card)'; btnSrc.style.background = 'transparent';
              loadFile(rawPath);
            };
            // Recompile button — re-runs LLM decompile on the .disasm file
            const recompBtn = document.createElement('button');
            recompBtn.textContent = 'Recompile';
            recompBtn.className = 'btn-secondary btn-sm';
            recompBtn.style.cssText = 'font-size:11px;padding:2px 10px;cursor:pointer';
            recompBtn.onclick = async () => {
              recompBtn.disabled = true;
              recompBtn.textContent = 'Compiling…';
              try {
                const res = await apiPost(`${JOBS}/decompile_single?path=${encodeURIComponent(rawPath)}`);
                if (res.ok && res.glsl) {
                  recompBtn.textContent = 'Done';
                  btnSrc.style.background = 'var(--bg-card)'; btnRaw.style.background = 'transparent';
                  content.innerHTML = `<div style="max-height:400px;overflow-y:auto">${codeWithLines(res.glsl)}</div>`;
                  currentPath = srcPath;
                  dlBtn.href = `${FILES}/raw?path=${encodeURIComponent(srcPath)}&download=1`;
                  dlBtn.download = srcPath.split(/[\\/]/).pop();
                } else {
                  recompBtn.textContent = 'Failed';
                }
              } catch (e) {
                recompBtn.textContent = 'Error';
              }
              setTimeout(() => { recompBtn.textContent = 'Recompile'; recompBtn.disabled = false; }, 3000);
            };
            toggle.appendChild(btnSrc);
            toggle.appendChild(btnRaw);
            toggle.appendChild(dlBtn);
            toggle.appendChild(recompBtn);
            wrap.appendChild(toggle);
            wrap.appendChild(content);
            loadFile(srcPath);
          } else {
            const dlRow = document.createElement('div');
            dlRow.style.cssText = 'display:flex;margin-bottom:4px';
            const dlBtn = document.createElement('a');
            dlBtn.textContent = 'Download';
            dlBtn.className = 'btn-secondary btn-sm';
            dlBtn.style.cssText = 'margin-left:auto;font-size:11px;padding:2px 10px;text-decoration:none;cursor:pointer';
            dlBtn.href = `${FILES}/raw?path=${encodeURIComponent(fp)}&download=1`;
            dlBtn.download = name;
            dlRow.appendChild(dlBtn);
            wrap.appendChild(dlRow);
            const content = document.createElement('div');
            wrap.appendChild(content);
            viewFileInto(content, fp, name, ext);
          }
          return wrap;
        });
      }
      list.appendChild(item.el);
    });
    shadersSection.body.appendChild(list);
  } else {
    shadersSection.body.innerHTML = '<span class="muted" style="font-size:12px">No shaders.</span>';
  }
  // Textures sub-list
  const texSection = _buildDetailSection(`Textures (${(dc.textures||[]).length})`, true);
  if (dc.textures && dc.textures.length > 0) {
    const list = document.createElement('div');
    list.className = 'dc-sublist';
    dc.textures.forEach(t => {
      const dims = (t.width && t.height) ? `${t.width}x${t.height}${t.depth > 1 ? 'x'+t.depth : ''}` : '';
      const label = `#${t.texture_id} · ${dims || '—'} · ${t.format || '—'}`;
      const item = _buildSublistItem(label, null);
      if (t.file_path) {
        const fp   = t.file_path;
        const name = fp.split(/[\\/]/).pop();
        const ext  = name.split('.').pop().toLowerCase();
        item.setExpand(() => {
          const wrap = document.createElement('div');
          wrap.className = 'dc-sublist-preview';
          if (ext === 'png' || ext === 'jpg' || ext === 'jpeg' || ext === 'bmp') {
            const img = document.createElement('img');
            img.className = 'dc-preview-img';
            img.src = `${FILES}/image?path=${encodeURIComponent(fp)}`;
            img.alt = name;
            wrap.appendChild(img);
          } else {
            viewFileInto(wrap, fp, name, ext);
          }
          return wrap;
        });
      }
      list.appendChild(item.el);
    });
    texSection.body.appendChild(list);
  } else {
    texSection.body.innerHTML = '<span class="muted" style="font-size:12px">No textures.</span>';
  }
  // Render Targets sub-list
  const rtList = dc.render_targets || [];
  const rtSection = _buildDetailSection(`Render Targets (${rtList.length})`, true);
  if (rtList.length > 0) {
    const list = document.createElement('div');
    list.className = 'dc-sublist';
    rtList.forEach(rt => {
      const typeStr = rt.attachment_type || '—';
      const dims    = (rt.width && rt.height) ? `${rt.width}×${rt.height}` : '—';
      const fmt     = rt.format || '—';
      const label   = `[${rt.attachment_index ?? '?'}] ${typeStr} · ${dims} · ${fmt}`;
      const item    = _buildSublistItem(label, null);
      // Show extra fields on expand
      item.setExpand(() => {
        const wrap = document.createElement('div');
        wrap.style.cssText = 'padding:6px 10px;font-size:12px;display:grid;grid-template-columns:140px 1fr;gap:2px 8px';
        const rows = [
          ['resource_id',    rt.resource_id],
          ['renderpass_id',  rt.renderpass_id],
          ['framebuffer_id', rt.framebuffer_id],
          ['width',          rt.width],
          ['height',         rt.height],
          ['format',         rt.format],
          ['attachment_type',rt.attachment_type],
        ];
        rows.forEach(([k, v]) => {
          if (v == null) return;
          wrap.innerHTML += `<span style="color:var(--text-muted);font-weight:600">${escHtml(k)}</span>`
            + `<span style="font-family:monospace">${escHtml(String(v))}</span>`;
        });
        return wrap;
      });
      list.appendChild(item.el);
    });
    rtSection.body.appendChild(list);
  } else {
    rtSection.body.innerHTML = '<span class="muted" style="font-size:12px">No render targets.</span>';
  }
  // Buffers/Mesh sub-list
  const meshSection = _buildDetailSection('Buffers / Mesh', true);
  if (dc.mesh_file) {
    const list = document.createElement('div');
    list.className = 'dc-sublist';
    const fp   = dc.mesh_file;
    const name = fp.split(/[\\/]/).pop();
    const item = _buildSublistItem(name, null);
    item.setExpand(() => {
      const wrap = document.createElement('div');
      wrap.className = 'dc-sublist-preview';
      _buildMeshViewer(wrap, fp);
      return wrap;
    });
    list.appendChild(item.el);
    meshSection.body.appendChild(list);
  } else {
    meshSection.body.innerHTML = '<span class="muted" style="font-size:12px">No mesh file.</span>';
  }
  // Order: Textures → Mesh → Render Targets → Shaders
  card.appendChild(texSection.el);
  card.appendChild(meshSection.el);
  card.appendChild(rtSection.el);
  card.appendChild(shadersSection.el);

  container.appendChild(card);
  container.scrollTop = 0;
}

// Build a clickable sub-list item that expands inline on click.
// Returns {el, setExpand(fn)} — setExpand registers the expand factory function.
function _buildSublistItem(label, badge) {
  const el = document.createElement('div');
  el.className = 'dc-sublist-item';

  const hdr = document.createElement('div');
  hdr.className = 'dc-sublist-hdr';
  hdr.innerHTML = `<span class="dc-sublist-chevron">▶</span><span class="dc-sublist-label">${escHtml(label)}</span>`;
  if (badge) {
    const b = document.createElement('span');
    b.className = 'dc-sublist-badge';
    b.textContent = badge;
    hdr.appendChild(b);
  }
  el.appendChild(hdr);

  const expandEl = document.createElement('div');
  expandEl.className = 'dc-sublist-expand';
  expandEl.style.display = 'none';
  el.appendChild(expandEl);

  let expandFn = null;
  let loaded   = false;

  hdr.style.cursor = 'pointer';
  hdr.onclick = () => {
    const open = expandEl.style.display !== 'none';
    if (!open) {
      if (!loaded && expandFn) {
        expandEl.appendChild(expandFn());
        loaded = true;
      }
      expandEl.style.display = '';
      hdr.querySelector('.dc-sublist-chevron').textContent = '▼';
    } else {
      expandEl.style.display = 'none';
      hdr.querySelector('.dc-sublist-chevron').textContent = '▶';
    }
  };

  return {
    el,
    setExpand(fn) { expandFn = fn; hdr.classList.add('dc-sublist-hdr--expandable'); },
  };
}

// Load a file's content into a DOM element (no external viewer card).
async function viewFileInto(container, fp, name, ext) {
  container.innerHTML = '<span class="muted" style="font-size:12px">Loading…</span>';
  try {
    const res = await apiGet(`${FILES}/read?path=${encodeURIComponent(fp)}&_t=${Date.now()}`);
    if (!res.ok) {
      container.innerHTML = `<span class="s-error" style="font-size:12px">${escHtml(res.error)}</span>`;
      return;
    }
    const content = res.data.content || '';
    if (ext === 'md') {
      const div = document.createElement('div');
      div.className = 'md viewer-body';
      div.style.padding = '10px 0';
      div.innerHTML = typeof marked !== 'undefined' ? marked.parse(content) : `<pre class="code-pre">${escHtml(content)}</pre>`;
      container.innerHTML = '';
      container.appendChild(div);
      if (typeof mermaid !== 'undefined') {
        div.querySelectorAll('pre code.language-mermaid').forEach(code => {
          const d = document.createElement('div');
          d.className = 'mermaid';
          d.textContent = code.textContent;
          code.parentElement.replaceWith(d);
        });
        mermaid.run({ nodes: div.querySelectorAll('.mermaid') });
      }
    } else {
      const wrapper = document.createElement('div');
      wrapper.style.cssText = 'max-height:400px;overflow-y:auto';
      wrapper.innerHTML = codeWithLines(content);
      container.innerHTML = '';
      container.appendChild(wrapper);
    }
  } catch (err) {
    container.innerHTML = `<span class="s-error" style="font-size:12px">${escHtml(err.message)}</span>`;
  }
}

// ── 3D OBJ mesh viewer (Three.js) ────────────────────────────────────────────

function _buildMeshViewer(container, fp) {
  if (typeof THREE === 'undefined' || typeof THREE.OBJLoader === 'undefined') {
    container.innerHTML = '<span class="muted" style="font-size:12px">Three.js not loaded — cannot preview mesh.</span>';
    return;
  }

  const wrap = document.createElement('div');
  wrap.className = 'mesh-viewer-wrap';

  // Toolbar: wireframe toggle + stats
  const toolbar = document.createElement('div');
  toolbar.className = 'mesh-viewer-toolbar';
  const btnWire = document.createElement('button');
  btnWire.className = 'btn-secondary btn-sm';
  btnWire.textContent = 'Wireframe';
  btnWire.style.cssText = 'padding:2px 8px;font-size:11px';
  const statsSpan = document.createElement('span');
  statsSpan.className = 'mesh-viewer-stats';
  toolbar.appendChild(btnWire);
  toolbar.appendChild(statsSpan);

  const hint = document.createElement('div');
  hint.className = 'mesh-viewer-hint';
  hint.textContent = 'Drag to rotate · Scroll to zoom · Right-drag to pan';

  const loading = document.createElement('div');
  loading.className = 'mesh-viewer-loading';
  loading.textContent = 'Loading mesh…';

  wrap.appendChild(toolbar);
  wrap.appendChild(loading);
  wrap.appendChild(hint);
  container.appendChild(wrap);

  // Defer init until after the element is in the DOM and laid out
  setTimeout(() => {
    const W = wrap.offsetWidth  || 560;
    const H = wrap.offsetHeight || 340;

    // Scene
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x1e293b);

    const camera = new THREE.PerspectiveCamera(45, W / H, 0.01, 10000);
    camera.position.set(0, 0, 5);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(W, H);
    renderer.domElement.className = 'mesh-viewer-canvas';
    wrap.insertBefore(renderer.domElement, hint);

    // Lights
    scene.add(new THREE.AmbientLight(0xffffff, 0.55));
    const sun = new THREE.DirectionalLight(0xffffff, 0.9);
    sun.position.set(2, 3, 4);
    scene.add(sun);
    const fill = new THREE.DirectionalLight(0x8090cc, 0.35);
    fill.position.set(-3, -1, -2);
    scene.add(fill);

    // OrbitControls
    const controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;

    // Wireframe state
    let wireframe = false;
    const meshes = [];
    btnWire.onclick = () => {
      wireframe = !wireframe;
      btnWire.classList.toggle('active', wireframe);
      meshes.forEach(m => { m.material.wireframe = wireframe; });
    };

    // Load OBJ
    const loader = new THREE.OBJLoader();
    loader.load(
      `${FILES}/raw?path=${encodeURIComponent(fp)}`,
      obj => {
        loading.remove();

        const mat = new THREE.MeshPhongMaterial({
          color: 0x5b9bd5,
          specular: 0x333344,
          shininess: 50,
          side: THREE.DoubleSide,
        });

        let totalVerts = 0, totalTris = 0;
        obj.traverse(child => {
          if (!child.isMesh) return;
          child.material = mat.clone();
          meshes.push(child);
          const geo = child.geometry;
          const pos = geo.attributes.position;
          if (pos) totalVerts += pos.count;
          if (geo.index) totalTris += geo.index.count / 3;
          else if (pos)  totalTris += pos.count / 3;
        });

        statsSpan.textContent =
          `Verts: ${totalVerts.toLocaleString()} · Tris: ${Math.round(totalTris).toLocaleString()}`;

        // Center mesh at origin
        const box    = new THREE.Box3().setFromObject(obj);
        const center = new THREE.Vector3();
        box.getCenter(center);
        obj.position.sub(center);
        scene.add(obj);

        // Fit camera to bounding sphere
        const sphere = new THREE.Sphere();
        box.getBoundingSphere(sphere);
        const r      = sphere.radius || 1;
        const fovRad = THREE.MathUtils.degToRad(camera.fov);
        const dist   = (r / Math.sin(fovRad / 2)) * 1.25;
        camera.position.set(0, r * 0.4, dist);
        camera.near = dist / 200;
        camera.far  = dist * 20;
        camera.updateProjectionMatrix();
        controls.target.set(0, 0, 0);
        controls.update();
      },
      null,
      err => {
        loading.textContent = 'OBJ load failed';
        console.error('OBJ load error', err);
      }
    );

    // Animation loop — auto-stop when detached from DOM
    let animId;
    const tick = () => {
      animId = requestAnimationFrame(tick);
      controls.update();
      renderer.render(scene, camera);
      if (!document.body.contains(renderer.domElement)) {
        cancelAnimationFrame(animId);
        renderer.dispose();
      }
    };
    tick();

    // Resize observer
    new ResizeObserver(() => {
      const nW = wrap.offsetWidth;
      const nH = wrap.offsetHeight;
      if (nW > 0 && nH > 0) {
        camera.aspect = nW / nH;
        camera.updateProjectionMatrix();
        renderer.setSize(nW, nH);
      }
    }).observe(wrap);
  }, 50);
}

// Build a collapsible detail section (returns {el, body})
function _buildDetailSection(title, defaultOpen) {
  const section = document.createElement('div');
  section.className = 'result-section';

  const hdr = document.createElement('div');
  hdr.className = 'result-section-hdr';
  hdr.style.cursor = 'pointer';
  hdr.innerHTML = `<span class="result-section-chevron">${defaultOpen ? '▼' : '▶'}</span> ${escHtml(title)}`;

  const body = document.createElement('div');
  body.className = 'result-section-body';
  body.style.display = defaultOpen ? '' : 'none';

  hdr.onclick = () => {
    const open = body.style.display !== 'none';
    body.style.display = open ? 'none' : '';
    hdr.querySelector('.result-section-chevron').textContent = open ? '▶' : '▼';
  };

  section.appendChild(hdr);
  section.appendChild(body);
  return { el: section, body };
}

// ── Questions Tab (per-tab) ──────────────────────────────────────────────────

const _Q_AGGS = ['sum', 'median', 'min', 'max', 'variance'];

function setQChartType(tabId, type) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  ts.questionsCtrl.chartType = type;
  el.querySelectorAll('.q-switch-opt[data-chart]').forEach(b =>
    b.classList.toggle('active', b.dataset.chart === type));
  if (ts.questionsCtrl.allData.length) _renderQuestionsResult(tabId);
}

function setQCorrCategory(tabId, cat) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  ts.questionsCtrl.corrCategory = cat;
  const title  = el.querySelector('.explorer-q-corr-title');
  const reset  = el.querySelector('.explorer-q-corr-reset');
  if (title) title.textContent = cat
    ? `Clock Correlation (R²) — ${cat}`
    : 'Clock Correlation (R²) — All DCs';
  if (reset) reset.style.display = cat ? '' : 'none';

  // Keep label dropdown in sync
  const sel = el.querySelector('.q-label-select');
  if (sel) sel.value = cat || '';

  // Highlight selected row in the table
  el.querySelectorAll('.explorer-questions-table-wrap tr[data-cat]').forEach(tr => {
    tr.classList.toggle('q-row-selected', tr.dataset.cat === cat);
  });

  const snapId = ts.snapState.snapshotId;
  if (snapId) fetchClockCorrelation(tabId, snapId);
}

function _buildMetricButtons(tabId, columns) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  const wrap = el.querySelector('.q-metric-btns-wrap');
  if (!wrap) return;
  wrap.innerHTML = '';
  const nonClock = columns.filter(c => c !== 'clocks');
  nonClock.forEach(col => {
    const btn = document.createElement('button');
    btn.className = 'q-metric-btn' + (col === ts.questionsCtrl.selectedMetric ? ' active' : '');
    btn.textContent = col;
    btn.onclick = () => {
      ts.questionsCtrl.selectedMetric = col;
      wrap.querySelectorAll('.q-metric-btn').forEach(b => b.classList.toggle('active', b === btn));
      const snapId = ts.snapState.snapshotId;
      if (snapId) _fetchLabelCorrelations(tabId, snapId);
      _renderQuestionsResult(tabId);
    };
    wrap.appendChild(btn);
  });
  // If selectedMetric no longer in columns, pick first
  if (!nonClock.includes(ts.questionsCtrl.selectedMetric) && nonClock.length) {
    ts.questionsCtrl.selectedMetric = nonClock[0];
    wrap.querySelector('.q-metric-btn')?.classList.add('active');
  }
}

async function fetchQuestionsData(tabId) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  const snapId = ts.snapState.snapshotId;
  const msg    = el.querySelector('.explorer-questions-msg');
  if (!snapId) return;

  ts.questionsState.snapshotId = snapId;
  // Reset drill-down when switching snapshots
  ts.questionsCtrl.corrCategory = null;
  const _corrTitle = el.querySelector('.explorer-q-corr-title');
  if (_corrTitle) _corrTitle.textContent = 'Clock Correlation (R²) — All DCs';
  const _corrReset = el.querySelector('.explorer-q-corr-reset');
  if (_corrReset) _corrReset.style.display = 'none';
  const _labelSel = el.querySelector('.q-label-select');
  if (_labelSel) _labelSel.value = '';
  msg.textContent = 'Loading…';
  msg.className   = 'status-msg s-info';

  try {
    const res = await apiGet(`${DATA}/label_agg_multi?snapshot_id=${snapId}`);
    if (!res.ok) {
      msg.textContent = res.error;
      msg.className   = 'status-msg s-error';
      el.querySelector('.explorer-questions-result-card').style.display = 'none';
      return;
    }
    msg.textContent = '';
    msg.className   = 'status-msg';
    ts.questionsCtrl.allData = res.data    || [];
    ts.questionsCtrl.columns = res.columns || [];

    _buildMetricButtons(tabId, ts.questionsCtrl.columns);
    _renderQuestionsResult(tabId);
    fetchClockCorrelation(tabId, snapId);
    _fetchLabelCorrelations(tabId, snapId);
  } catch (err) {
    msg.textContent = err.message;
    msg.className   = 'status-msg s-error';
  }
}

async function fetchClockCorrelation(tabId, snapId) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  const card = el.querySelector('.explorer-q-corr-card');
  const body = el.querySelector('.explorer-q-corr-body');
  if (!card || !body) return;
  body.innerHTML = '<span class="muted" style="font-size:12px">Computing…</span>';
  card.style.display = '';
  const cat = ts.questionsCtrl.corrCategory;
  const url = `${DATA}/clock_correlation?snapshot_id=${snapId}` +
              (cat ? `&category=${encodeURIComponent(cat)}` : '');
  try {
    const res = await apiGet(url);
    if (!res.ok) { body.innerHTML = `<span class="s-error">${escHtml(res.error)}</span>`; return; }
    _renderCorrTable(tabId, res.data || []);
  } catch (err) {
    body.innerHTML = `<span class="s-error">${escHtml(err.message)}</span>`;
  }
}

function _renderCorrTable(tabId, rows) {
  const el = getTabEl(tabId);
  if (!el) return;
  const body = el.querySelector('.explorer-q-corr-body');
  if (!rows.length) {
    body.innerHTML = '<span class="muted" style="font-size:12px">Not enough paired data (need ≥10 DCs with both clocks and metric values).</span>';
    return;
  }

  const frag = document.createDocumentFragment();
  rows.forEach(row => {
    const r2 = row.r2 != null ? row.r2 : (row.r * row.r);
    const r  = row.r;
    // Color intensity based on R² strength
    const color = r2 > 0.5 ? '#3b82f6' : r2 > 0.2 ? '#60a5fa' : '#93c5fd';

    const div = document.createElement('div');
    div.className = 'q-corr-row';

    const name = document.createElement('div');
    name.className   = 'q-corr-name';
    name.textContent = row.metric;
    name.title       = `${row.metric} (r=${r >= 0 ? '+' : ''}${r.toFixed(3)})`;

    const barWrap = document.createElement('div');
    barWrap.className = 'q-corr-bar-wrap';
    barWrap.style.position = 'relative';
    barWrap.style.background = '#f0f2f5';
    barWrap.style.borderRadius = '3px';

    const bar = document.createElement('div');
    bar.style.position   = 'absolute';
    bar.style.left       = '0';
    bar.style.top        = '0';
    bar.style.height     = '100%';
    bar.style.width      = (r2 * 100).toFixed(1) + '%';
    bar.style.background = color;
    bar.style.borderRadius = '3px';
    bar.style.minWidth   = r2 > 0.005 ? '2px' : '0';
    barWrap.appendChild(bar);

    const rVal = document.createElement('div');
    rVal.className   = 'q-corr-r';
    rVal.textContent = (r2 * 100).toFixed(1) + '%';
    rVal.style.color = color;

    const nVal = document.createElement('div');
    nVal.className   = 'q-corr-n';
    nVal.textContent = 'n=' + row.n;

    div.appendChild(name);
    div.appendChild(barWrap);
    div.appendChild(rVal);
    div.appendChild(nVal);
    frag.appendChild(div);
  });

  body.innerHTML = '';
  body.appendChild(frag);
}

function _renderQLabelDropdown(tabId, rows) {
  const el = getTabEl(tabId);
  if (!el) return;
  const sel = el.querySelector('.q-label-select');
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">All labels</option>';
  (rows || []).forEach(r => {
    const opt = document.createElement('option');
    opt.value = r.category;
    opt.textContent = r.category + (r.dc_count != null ? ` (${r.dc_count})` : '');
    sel.appendChild(opt);
  });
  // Restore selection if still valid
  if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
}

function onQLabelSelect(tabId, cat) {
  setQCorrCategory(tabId, cat || null);
}

function goExplorerForLabel(tabId) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  const sel = el.querySelector('.q-label-select');
  const cat = sel ? sel.value : '';
  switchExplorerSubTab(tabId, 'explorer');
  // After sub-tab switch, apply the category filter
  if (cat) {
    ts.catFilterSel.clear();
    ts.catFilterSel.add(cat);
  } else {
    ts.catFilterSel.clear();
  }
  // Load DCs if not yet loaded, then apply filter
  if (ts.explorerState.snapshotId) {
    if (ts.explorerState.dcs.length > 0) {
      _buildCatDropdown(tabId, ts.catFilterAll);
      _applyCatFilter(tabId);
    } else {
      loadExplorerDCs(tabId);
    }
  }
}

function _renderQuestionsResult(tabId) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  const card = el.querySelector('.explorer-questions-result-card');
  const rows = ts.questionsCtrl.allData;
  if (!rows.length) {
    el.querySelector('.explorer-questions-table-wrap').innerHTML =
      '<span class="muted" style="font-size:12px">No data. Run analysis with ingest first.</span>';
    card.style.display = '';
    _renderQLabelDropdown(tabId, []);
    return;
  }
  card.style.display = '';
  _renderQLabelDropdown(tabId, rows);

  const metric = ts.questionsCtrl.selectedMetric;
  const type   = ts.questionsCtrl.chartType;

  const lbl2 = el.querySelector('.q-chart2-label');
  if (lbl2) lbl2.textContent = metric || '—';

  // Charts use sum for display
  const clockRows = rows.map(r => ({ category: r.category, value: r.clocks?.sum ?? 0, dc_count: r.dc_count }));
  const selRows   = rows.map(r => ({ category: r.category, value: r[metric]?.sum ?? 0, dc_count: r.dc_count }));

  const clockCanvas = el.querySelector('.q-chart-clock');
  const clockTip    = el.querySelector('.q-chart-clock-tip');
  const selCanvas   = el.querySelector('.q-chart-sel');
  const selTip      = el.querySelector('.q-chart-sel-tip');

  if (type === 'pie') {
    _drawQPie(tabId, clockCanvas, clockTip, clockRows, 'clocks', 'sum');
    _drawQPie(tabId, selCanvas, selTip, selRows, metric, 'sum');
  } else {
    _drawQBar(tabId, clockCanvas, clockTip, clockRows, 'clocks', 'sum');
    _drawQBar(tabId, selCanvas, selTip, selRows, metric, 'sum');
  }

  _renderQTable(tabId, rows);
}

// ── Shared bar renderer ───────────────────────────────────────────────────────
function _drawQBar(tabId, canvas, tooltip, rows, metric, agg) {
  if (!canvas) return;
  const W = canvas.offsetWidth || canvas.parentElement?.offsetWidth || 400;
  const H = 320;
  canvas.width  = W;
  canvas.height = H;

  const PAD = { top: 20, right: 8, bottom: 60, left: 56 };
  const cW  = W - PAD.left - PAD.right;
  const cH  = H - PAD.top  - PAD.bottom;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, W, H);

  const maxVal = Math.max(...rows.map(r => r.value || 0));
  if (!maxVal) {
    ctx.fillStyle = '#94a3b8'; ctx.font = '12px system-ui';
    ctx.textAlign = 'center';
    ctx.fillText('No data', W / 2, H / 2);
    return;
  }

  const barW  = cW / rows.length;
  const fillW = Math.max(1, barW * 0.72);
  const gap   = (barW - fillW) / 2;

  // Grid + Y labels
  ctx.font = '10px system-ui'; ctx.fillStyle = '#94a3b8'; ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const y = PAD.top + cH - (cH * i / 4);
    ctx.fillText(_fmtK(maxVal * i / 4), PAD.left - 4, y + 3);
    ctx.strokeStyle = '#e5e7eb'; ctx.lineWidth = 0.5;
    ctx.beginPath(); ctx.moveTo(PAD.left, y); ctx.lineTo(PAD.left + cW, y); ctx.stroke();
  }

  // Bars + X labels
  rows.forEach((row, i) => {
    const barH = ((row.value || 0) / maxVal) * cH;
    const x = PAD.left + i * barW + gap;
    ctx.fillStyle = _catColor(row.category);
    ctx.fillRect(x, PAD.top + cH - barH, fillW, barH);
    ctx.save();
    ctx.translate(x + fillW / 2, PAD.top + cH + 6);
    ctx.rotate(Math.PI / 4);
    ctx.fillStyle = '#475569'; ctx.font = '10px system-ui'; ctx.textAlign = 'left';
    ctx.fillText(row.category, 0, 0);
    ctx.restore();
  });

  // Baseline
  ctx.strokeStyle = '#cbd5e1'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(PAD.left, PAD.top + cH); ctx.lineTo(PAD.left + cW, PAD.top + cH); ctx.stroke();

  // Hover + click
  canvas._meta = { rows, barW, gap, fillW, PAD, cH, maxVal };
  canvas.style.cursor = 'pointer';
  canvas.onmousemove = (e) => {
    const rect = canvas.getBoundingClientRect();
    const mx   = (e.clientX - rect.left) * (canvas.width / rect.width);
    const meta = canvas._meta;
    const idx  = Math.floor((mx - meta.PAD.left) / meta.barW);
    if (idx < 0 || idx >= meta.rows.length) { tooltip.style.display = 'none'; return; }
    const r = meta.rows[idx];
    tooltip.style.display = '';
    tooltip.textContent = `${r.category}: ${_fmtK(r.value)} (${r.dc_count} DCs)`;
    tooltip.style.left = Math.min(e.offsetX + 12, canvas.offsetWidth - 160) + 'px';
    tooltip.style.top  = Math.max(0, e.offsetY - 28) + 'px';
  };
  canvas.onmouseleave = () => { tooltip.style.display = 'none'; };
  canvas.onclick = (e) => {
    const rect = canvas.getBoundingClientRect();
    const mx   = (e.clientX - rect.left) * (canvas.width / rect.width);
    const meta = canvas._meta;
    const idx  = Math.floor((mx - meta.PAD.left) / meta.barW);
    if (idx < 0 || idx >= meta.rows.length) return;
    const cat = meta.rows[idx].category;
    const ts = getTabState(tabId);
    if (ts) setQCorrCategory(tabId, ts.questionsCtrl.corrCategory === cat ? null : cat);
  };
}

// ── Shared pie renderer ───────────────────────────────────────────────────────
function _drawQPie(tabId, canvas, tooltip, rows, metric, agg) {
  if (!canvas) return;
  const W = canvas.offsetWidth || canvas.parentElement?.offsetWidth || 400;
  const H = 320;
  canvas.width  = W;
  canvas.height = H;

  const ctx   = canvas.getContext('2d');
  ctx.clearRect(0, 0, W, H);
  const total = rows.reduce((s, r) => s + (r.value || 0), 0);
  if (!total) {
    ctx.fillStyle = '#94a3b8'; ctx.font = '12px system-ui'; ctx.textAlign = 'center';
    ctx.fillText('No data', W / 2, H / 2); return;
  }

  const cx = W * 0.42;
  const cy = H / 2;
  const R  = Math.min(cx - 16, cy - 16);

  let angle = -Math.PI / 2;
  const slices = rows.filter(r => (r.value || 0) > 0).map(r => {
    const sweep = (r.value / total) * 2 * Math.PI;
    const s = { ...r, startAngle: angle, sweep };
    angle += sweep;
    return s;
  });

  slices.forEach(s => {
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, R, s.startAngle, s.startAngle + s.sweep);
    ctx.closePath();
    ctx.fillStyle = _catColor(s.category);
    ctx.fill();
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke();
  });

  // Legend
  const legX = cx + R + 16;
  let legY = Math.max(20, cy - slices.length * 10);
  ctx.font = '11px system-ui'; ctx.textAlign = 'left';
  slices.forEach(s => {
    ctx.fillStyle = _catColor(s.category);
    ctx.fillRect(legX, legY - 9, 10, 10);
    ctx.fillStyle = '#334155';
    ctx.fillText(`${s.category} ${((s.value / total) * 100).toFixed(1)}%`, legX + 14, legY);
    legY += 18;
  });

  // Hover + click
  canvas._pieMeta = { slices, cx, cy, R, total, W, H };
  canvas.style.cursor = 'pointer';
  const _pieHitSlice = (e) => {
    const rect = canvas.getBoundingClientRect();
    const mx   = (e.clientX - rect.left) * (W / rect.width) - canvas._pieMeta.cx;
    const my   = (e.clientY - rect.top)  * (H / rect.height) - canvas._pieMeta.cy;
    if (Math.sqrt(mx*mx + my*my) > canvas._pieMeta.R) return null;
    let ang = Math.atan2(my, mx);
    if (ang < -Math.PI / 2) ang += 2 * Math.PI;
    return canvas._pieMeta.slices.find(sl => ang >= sl.startAngle && ang < sl.startAngle + sl.sweep) || null;
  };
  canvas.onmousemove = (e) => {
    const s = _pieHitSlice(e);
    if (!s) { tooltip.style.display = 'none'; return; }
    tooltip.style.display = '';
    tooltip.textContent = `${s.category}: ${_fmtK(s.value)} (${((s.value/canvas._pieMeta.total)*100).toFixed(1)}%, ${s.dc_count} DCs)`;
    tooltip.style.left = (e.offsetX + 12) + 'px';
    tooltip.style.top  = Math.max(0, e.offsetY - 28) + 'px';
  };
  canvas.onmouseleave = () => { tooltip.style.display = 'none'; };
  canvas.onclick = (e) => {
    const s = _pieHitSlice(e);
    if (!s) return;
    const ts = getTabState(tabId);
    if (ts) setQCorrCategory(tabId, ts.questionsCtrl.corrCategory === s.category ? null : s.category);
  };
}

async function _fetchLabelCorrelations(tabId, snapId) {
  const ts = getTabState(tabId);
  if (!ts) return;
  const metric = ts.questionsCtrl.selectedMetric;
  if (!metric || metric === 'clocks') { ts.questionsCtrl.labelCorrs = {}; _reRenderQTable(tabId); return; }
  try {
    const res = await apiGet(`${DATA}/label_correlations?snapshot_id=${snapId}&metric=${encodeURIComponent(metric)}`);
    if (!res.ok) return;
    ts.questionsCtrl.labelCorrs = {};
    (res.data || []).forEach(d => { ts.questionsCtrl.labelCorrs[d.category] = d; });
    _reRenderQTable(tabId);
  } catch { /* ignore */ }
}

function _reRenderQTable(tabId) {
  const ts = getTabState(tabId);
  if (ts && ts.questionsCtrl.allData.length) _renderQTable(tabId, ts.questionsCtrl.allData);
}

// ── Table: Category + DCs + clocks×5aggs + selectedMetric×5aggs + r ──────────
function _renderQTable(tabId, rows) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;
  const wrap = el.querySelector('.explorer-questions-table-wrap');
  if (!rows.length) { wrap.innerHTML = ''; return; }

  const metric   = ts.questionsCtrl.selectedMetric;
  const showSel  = metric && metric !== 'clocks';
  const showCorr = showSel && Object.keys(ts.questionsCtrl.labelCorrs).length > 0;

  const AGG_LABELS = { sum: 'Sum', median: 'Med', min: 'Min', max: 'Max', variance: 'Var' };

  const headers = ['Category', 'DCs'];
  _Q_AGGS.forEach(a => headers.push(`clocks ${AGG_LABELS[a]}`));
  if (showSel) _Q_AGGS.forEach(a => headers.push(`${metric} ${AGG_LABELS[a]}`));
  if (showCorr) headers.push('r (clocks↔metric)');

  const table = document.createElement('table');
  table.className = 'questions-table';

  const thead = table.createTHead();
  const hrow  = thead.insertRow();
  headers.forEach(h => {
    const th = document.createElement('th');
    th.textContent = h;
    hrow.appendChild(th);
  });

  const tbody = table.createTBody();
  rows.forEach(row => {
    const tr = tbody.insertRow();
    tr.dataset.cat = row.category;
    tr.style.cursor = 'pointer';
    if (row.category === ts.questionsCtrl.corrCategory) tr.classList.add('q-row-selected');
    tr.onclick = () => setQCorrCategory(tabId, ts.questionsCtrl.corrCategory === row.category ? null : row.category);

    // Category
    const catTd = tr.insertCell();
    catTd.className = 'questions-category-cell';
    const dot = document.createElement('span');
    dot.style.cssText = `display:inline-block;width:9px;height:9px;border-radius:2px;background:${_catColor(row.category)};margin-right:6px;vertical-align:middle`;
    catTd.appendChild(dot);
    catTd.appendChild(document.createTextNode(row.category));

    // DCs
    const dcTd = tr.insertCell();
    dcTd.textContent = row.dc_count ?? '—';
    dcTd.style.textAlign = 'right';

    // Clocks × 5 aggs
    const clockAggs = row.clocks || {};
    _Q_AGGS.forEach(a => {
      const td = tr.insertCell();
      const v  = clockAggs[a];
      td.textContent = v != null ? _fmtK(v) : '—';
      td.style.textAlign = 'right';
    });

    // Selected metric × 5 aggs
    if (showSel) {
      const metAggs = row[metric] || {};
      _Q_AGGS.forEach(a => {
        const td = tr.insertCell();
        const v  = metAggs[a];
        td.textContent = v != null ? _fmtK(v) : '—';
        td.style.textAlign = 'right';
      });
    }

    // Correlation R²
    if (showCorr) {
      const corrInfo = ts.questionsCtrl.labelCorrs[row.category];
      const rTd = tr.insertCell();
      rTd.style.textAlign = 'right';
      rTd.style.fontFamily = 'ui-monospace, monospace';
      if (!corrInfo || corrInfo.r == null) {
        rTd.textContent = corrInfo ? `n=${corrInfo.n} (too few)` : '—';
        rTd.style.color = 'var(--text-muted)';
      } else {
        const r2 = corrInfo.r * corrInfo.r;
        rTd.textContent = (r2 * 100).toFixed(1) + `%  n=${corrInfo.n}`;
        rTd.style.color = r2 > 0.3 ? '#3b82f6' : '#93c5fd';
        rTd.style.fontWeight = '600';
      }
    }
  });

  wrap.innerHTML = '';
  wrap.appendChild(table);
}

// ── Tab navigation ────────────────────────────────────────────────────────────

function switchTab(id) {
  activeTabId = id;

  // Hide all tab contents
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));

  // Show the selected tab content
  const tabEl = document.getElementById(`tab-${id}`);
  if (tabEl) tabEl.classList.add('active');

  // Update tab bar active state
  renderTabBar();

  if (id === 'home') {
    scanSdpFiles();
  }

  if (id === 'logs') {
    logState.frontendUnread = 0;
    renderLogs();
    const backendIds = logState.allRecords.filter(r => r.id > 0).map(r => r.id);
    if (backendIds.length > 0) logState.lastSeenId = Math.max(...backendIds);
    updateLogBadge(0);
  }

  if (id === 'apidocs') {
    const iframe = document.getElementById('apidocs-iframe');
    if (iframe && !iframe.src) iframe.src = '/api/docs';
  }
}

// ── Explorer sub-tab switching ───────────────────────────────────────────────

function switchExplorerSubTab(tabId, subId) {
  const ts = getTabState(tabId);
  const el = getTabEl(tabId);
  if (!ts || !el) return;

  ts.subTab = subId;

  // Update sub-nav buttons
  el.querySelectorAll('.subnav-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.sub === subId));

  // Show/hide panels
  el.querySelector('.panel-explorer').style.display = subId === 'explorer' ? '' : 'none';
  el.querySelector('.panel-questions').style.display = subId === 'questions' ? '' : 'none';

  // Load data for the sub-tab
  if (subId === 'explorer') {
    if (ts.snapState.snapshotId) loadExplorerDCs(tabId);
  } else if (subId === 'questions') {
    const snapId = ts.snapState.snapshotId;
    if (snapId) { fetchClockCorrelation(tabId, snapId); fetchQuestionsData(tabId); }
    // Also load results/files
    const snapDir = ts.snapState.snapshotDir;
    if (snapDir) {
      const normDir = normPath(snapDir);
      const parts   = normDir.replace(/\\/g, '/').split('/');
      const analysisRoot = parts.slice(0, -2).join('/');
      const runName      = parts[parts.length - 2];
      const snapIdStr    = parts[parts.length - 1];
      if (!ts._resultsState || ts._resultsState.runs.length === 0) {
        scanAnalyses(tabId, analysisRoot, runName, snapIdStr);
      } else {
        renderRunSelector(tabId, runName, snapIdStr);
      }
    }
  }
}

// ── Legacy stubs (kept for backward compat with any external callers) ────────

function openResults(captureDir) {
  if (!captureDir) return;
  state.lastAnalysisDir = captureDir;
  // Try to find which SDP this belongs to
  const sdpPath = Object.keys(sdpAnalysisCache).find(k => sdpAnalysisCache[k] === captureDir);
  if (sdpPath) {
    openExplorerTab(sdpPath);
  }
}

function goToResults() {
  openResults(state.lastAnalysisDir);
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function codeWithLines(text) {
  const lines = text.split('\n');
  const pad = String(lines.length).length;
  const numbered = lines.map((l, i) =>
    `<span class="line-num">${String(i + 1).padStart(pad)}</span>${escHtml(l)}`
  ).join('\n');
  return `<pre class="code-pre code-lined">${numbered}</pre>`;
}

function escAttr(str) {
  return String(str).replace(/'/g, "\\'");
}

// ── Modal system ─────────────────────────────────────────────────────────────

function openModal(id) {
  const modal = document.getElementById(id);
  if (modal) {
    modal.style.display = '';
    if (id === 'snapshot-modal') {
      _pauseSSE();
      syncDevice();
      _loadCaptureProjects();
    }
  }
}

async function _loadCaptureProjects() {
  const res = await apiGet(`${DATA}/projects`);
  if (!res.ok) return;
  const sel = document.getElementById('capture-project');
  sel.innerHTML = '<option value="">— select project —</option>';
  (res.data || []).forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.name;
    sel.appendChild(opt);
  });
}

async function onCaptureProjectChange() {
  const pid = document.getElementById('capture-project').value;
  const vSel = document.getElementById('capture-version');
  if (!pid) {
    vSel.disabled = true;
    vSel.innerHTML = '<option value="">— select version —</option>';
    return;
  }
  const res = await apiGet(`${DATA}/projects/${pid}/versions`);
  vSel.disabled = false;
  vSel.innerHTML = '<option value="">— select version —</option>';
  if (!res.ok) return;
  (res.data || []).forEach(v => {
    const opt = document.createElement('option');
    opt.value = v.id;
    opt.textContent = v.name;
    vSel.appendChild(opt);
  });
}

function closeModal(id) {
  const modal = document.getElementById(id);
  if (modal) modal.style.display = 'none';
  if (id === 'snapshot-modal') {
    _resumeSSE();
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  // Intercept console output → log panel
  _interceptConsole();

  // Mermaid config (startOnLoad:false — we call run() manually after DOM insertion)
  if (typeof mermaid !== 'undefined') {
    mermaid.initialize({ startOnLoad: false, theme: 'default', maxTextSize: 2000000 });
  }

  // Render initial tab bar (Home + Logs)
  renderTabBar();

  // Build target checkboxes, then restore saved settings
  initTargetChips();
  loadAnalysisSettings();

  // Initial step state
  refreshSteps();

  // Load SDP files first (most visible), then start background services
  scanSdpFiles().then(() => {
    startDevicePoll();
    refreshDeviceList();
    startLogPoll();
    _resumeCsJobIfAny();
    _resumePipelineJobIfAny();
    // SSE holds a persistent connection slot — delay to avoid starving device poll
    setTimeout(_initSSE, 3000);
  });
});
