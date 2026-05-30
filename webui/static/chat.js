/* ════════════════════════════════════════════════════════════════
   pysdp — Chat Sidebar (chat.js)
   ════════════════════════════════════════════════════════════════ */

const chatState = {
  messages: [],
  isOpen: false,
  isStreaming: false,
  activeSnapshotId: null,
  pinnedSnapshotIds: JSON.parse(localStorage.getItem('chatPinnedSnapshots') || '[]'),
  skills: [],
  activeSkill: null,  // currently selected skill object
  _startTime: null,
  _statusTimer: null,
};

function toggleChatPanel() {
  chatState.isOpen = !chatState.isOpen;
  document.body.classList.toggle('chat-open', chatState.isOpen);
  if (chatState.isOpen) {
    if (chatState.skills.length === 0) loadSkills();
    if (chatState.messages.length === 0) renderWelcome();
  }
}

async function loadSkills() {
  try {
    const resp = await fetch('/api/chat/skills');
    if (resp.ok) {
      const data = await resp.json();
      chatState.skills = data.skills || [];
      renderSkillsDropdown();
    }
  } catch (_) {}
}

function renderSkillsDropdown() {
  const container = document.getElementById('chat-skills-dropdown');
  if (!container || !chatState.skills.length) return;
  const activeId = chatState.activeSkill ? chatState.activeSkill.id : null;
  container.innerHTML = chatState.skills.map(s =>
    `<div class="chat-skill-item${s.id === activeId ? ' active' : ''}" onclick="invokeSkill('${s.id}')">
      <span class="skill-icon">${s.icon}</span>
      <div><div class="skill-name">${s.button_label}</div><div class="skill-desc">${s.description}</div></div>
      ${s.id === activeId ? '<span class="skill-check">&#10003;</span>' : ''}
    </div>`
  ).join('');
}

function toggleSkillsDropdown() {
  const dd = document.getElementById('chat-skills-dropdown');
  if (!dd) return;
  if (dd.style.display === 'none') {
    if (chatState.skills.length === 0) { loadSkills(); return; }
    renderSkillsDropdown();
    dd.style.display = 'block';
  } else {
    dd.style.display = 'none';
  }
}

function invokeSkill(skillId) {
  const dd = document.getElementById('chat-skills-dropdown');
  if (dd) dd.style.display = 'none';

  const skill = chatState.skills.find(s => s.id === skillId);
  if (!skill) return;

  if (chatState.activeSkill && chatState.activeSkill.id === skillId) {
    clearActiveSkill();
  } else {
    chatState.activeSkill = skill;
    renderActiveSkillBadge();
    document.getElementById('chat-input').focus();
  }
}

function clearActiveSkill() {
  chatState.activeSkill = null;
  renderActiveSkillBadge();
}

function renderActiveSkillBadge() {
  const badge = document.getElementById('chat-skill-badge');
  if (!badge) return;
  if (chatState.activeSkill) {
    badge.innerHTML = `${chatState.activeSkill.icon} ${chatState.activeSkill.button_label} <button class="skill-badge-clear" onclick="clearActiveSkill()">×</button>`;
    badge.style.display = 'flex';
  } else {
    badge.style.display = 'none';
  }
}

function renderWelcome() {
  const el = document.getElementById('chat-messages');
  el.innerHTML = `
    <div class="chat-welcome">
      <div class="chat-welcome-title">GPU Profiling Assistant</div>
      <div class="chat-welcome-hint">Ask about draw call performance, bottlenecks, or metric correlations.</div>
      <div class="chat-welcome-examples">
        <button class="chat-example-btn" onclick="sendExample(this)">What are the top bottlenecks?</button>
        <button class="chat-example-btn" onclick="sendExample(this)">Show category breakdown by clocks</button>
        <button class="chat-example-btn" onclick="sendExample(this)">Which metrics correlate with GPU time?</button>
      </div>
    </div>
  `;
}

function sendExample(btn) {
  const input = document.getElementById('chat-input');
  input.value = btn.textContent;
  sendChatMessage();
}

function appendSystemMessage(text) {
  const msgArea = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'chat-msg chat-msg-system';
  div.innerHTML = `<div class="chat-msg-content">${text}</div>`;
  msgArea.appendChild(div);
  autoScroll();
}

const PIN_SVG = `<svg class="pin-icon" viewBox="0 0 16 16"><path d="M10.5 2.5L13.5 5.5L12 7.5L11 11L5 5L8.5 4L10.5 2.5Z" fill="currentColor"/><path d="M5 11L2 14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
const UNPIN_SVG = `<svg class="pin-icon" viewBox="0 0 16 16"><path d="M10.5 2.5L13.5 5.5L12 7.5L11 11L5 5L8.5 4L10.5 2.5Z" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M5 11L2 14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;

function updateChatContextBar() {
  const container = document.getElementById('chat-context-chips');
  if (!container) return;
  let html = '';
  if (chatState.activeSnapshotId) {
    const isPinned = chatState.pinnedSnapshotIds.includes(chatState.activeSnapshotId);
    if (isPinned) {
      html += `<span class="chat-snap-chip active"><span class="chip-pin pinned" onclick="unpinSnapshot(${chatState.activeSnapshotId})" title="Unpin">${PIN_SVG}</span>#${chatState.activeSnapshotId}</span>`;
    } else {
      html += `<span class="chat-snap-chip active"><span class="chip-pin" onclick="pinSnapshot(${chatState.activeSnapshotId})" title="Pin to chat">${UNPIN_SVG}</span>#${chatState.activeSnapshotId}</span>`;
    }
  }
  for (const sid of chatState.pinnedSnapshotIds) {
    if (sid !== chatState.activeSnapshotId) {
      html += `<span class="chat-snap-chip pinned"><span class="chip-pin pinned" onclick="unpinSnapshot(${sid})" title="Unpin">${PIN_SVG}</span>#${sid}</span>`;
    }
  }
  container.innerHTML = html;
}

async function toggleSnapshotPicker() {
  const picker = document.getElementById('chat-snapshot-picker');
  if (!picker) return;
  if (picker.style.display !== 'none') {
    picker.style.display = 'none';
    return;
  }
  picker.innerHTML = '<div class="snap-picker-loading">Loading...</div>';
  picker.style.display = 'block';

  let snapshots = [];
  // Collect from already-loaded explorer tab states
  if (typeof explorerTabs !== 'undefined') {
    const seen = new Set();
    for (const ts of Object.values(explorerTabs)) {
      if (ts.snapState && ts.snapState.runs) {
        for (const run of ts.snapState.runs) {
          for (const s of (run.snapshots || [])) {
            if (!seen.has(s.snapshot_id)) {
              seen.add(s.snapshot_id);
              snapshots.push(s);
            }
          }
        }
      }
    }
  }
  // Fallback: fetch from API
  if (!snapshots.length) {
    try {
      const resp = await fetch('/api/data/snapshots');
      if (resp.ok) snapshots = await resp.json();
    } catch (_) {}
  }

  if (!snapshots.length) {
    picker.innerHTML = '<div class="snap-picker-empty">Open an SDP file first to see snapshots</div>';
    return;
  }
  const pinned = chatState.pinnedSnapshotIds;
  picker.innerHTML = snapshots.map(s => {
    const isPinned = pinned.includes(s.snapshot_id);
    return `<div class="snap-picker-item${isPinned ? ' pinned' : ''}" onclick="togglePinFromPicker(${s.snapshot_id})">
      <span class="snap-picker-icon">${isPinned ? PIN_SVG : UNPIN_SVG}</span>
      <span class="snap-picker-label">#${s.snapshot_id} ${s.sdp_name || s.run_name || ''}</span>
    </div>`;
  }).join('');
}

function togglePinFromPicker(snapshotId) {
  const isPinned = chatState.pinnedSnapshotIds.includes(snapshotId);
  if (isPinned) {
    unpinSnapshot(snapshotId);
  } else {
    pinSnapshot(snapshotId);
  }
  toggleSnapshotPicker();
  toggleSnapshotPicker();
}

function getActiveSnapshotIds() {
  const ids = [];
  if (chatState.activeSnapshotId) ids.push(chatState.activeSnapshotId);
  for (const sid of chatState.pinnedSnapshotIds) {
    if (!ids.includes(sid)) ids.push(sid);
  }
  return ids;
}

function pinSnapshot(snapshotId) {
  if (!chatState.pinnedSnapshotIds.includes(snapshotId)) {
    chatState.pinnedSnapshotIds.push(snapshotId);
    localStorage.setItem('chatPinnedSnapshots', JSON.stringify(chatState.pinnedSnapshotIds));
    updateChatContextBar();
  }
}

function unpinSnapshot(snapshotId) {
  chatState.pinnedSnapshotIds = chatState.pinnedSnapshotIds.filter(s => s !== snapshotId);
  localStorage.setItem('chatPinnedSnapshots', JSON.stringify(chatState.pinnedSnapshotIds));
  updateChatContextBar();
}

// ── Status line ──────────────────────────────────────────────────
function setChatStatus(text, showElapsed) {
  const el = document.getElementById('chat-status-line');
  if (!el) return;
  if (showElapsed && chatState._startTime) {
    const sec = ((Date.now() - chatState._startTime) / 1000).toFixed(0);
    el.innerHTML = `<span class="chat-status-dot"></span> ${text} <span class="chat-status-time">${sec}s</span>`;
  } else {
    el.innerHTML = `<span class="chat-status-dot"></span> ${text}`;
  }
  el.style.display = 'flex';
}

function setChatStatusDone() {
  const el = document.getElementById('chat-status-line');
  if (!el) return;
  const elapsed = chatState._startTime ? ((Date.now() - chatState._startTime) / 1000) : 0;
  let timeStr;
  if (elapsed >= 60) {
    const m = Math.floor(elapsed / 60);
    const s = Math.floor(elapsed % 60);
    timeStr = `${m}m${s}s`;
  } else {
    timeStr = `${elapsed.toFixed(1)}s`;
  }
  el.innerHTML = `<span class="chat-status-done">&#10003;</span> Done in ${timeStr}`;
  el.style.display = 'flex';
  if (chatState._statusTimer) clearInterval(chatState._statusTimer);
  chatState._statusTimer = null;
  chatState._startTime = null;
  setTimeout(() => { el.style.display = 'none'; }, 5000);
}

function startChatTimer(initialStatus) {
  chatState._startTime = Date.now();
  setChatStatus(initialStatus, false);
  if (chatState._statusTimer) clearInterval(chatState._statusTimer);
  chatState._statusTimer = setInterval(() => {
    if (!chatState.isStreaming) {
      clearInterval(chatState._statusTimer);
      chatState._statusTimer = null;
      return;
    }
    const el = document.getElementById('chat-status-line');
    if (el && el.dataset.currentText) {
      setChatStatus(el.dataset.currentText, true);
    }
  }, 1000);
}

function updateStatusText(text) {
  const el = document.getElementById('chat-status-line');
  if (el) el.dataset.currentText = text;
  setChatStatus(text, true);
}

async function sendChatMessage() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text || chatState.isStreaming) return;

  input.value = '';
  input.style.height = 'auto';

  // Check for slash command typed directly
  if (text.startsWith('/') && !chatState.activeSkill) {
    const cmd = text.split(/\s/)[0];
    const skill = chatState.skills.find(s => s.slash_command === cmd);
    if (skill) {
      const rest = text.slice(cmd.length).trim();
      const displayText = rest ? `${cmd} ${rest}` : cmd;
      chatState.messages.push({ role: 'user', content: displayText });
      appendMessageBubble('user', displayText);
      streamWithSkill(skill.id, getActiveSnapshotIds(), rest);
      return;
    }
  }

  // Clear welcome
  const msgArea = document.getElementById('chat-messages');
  if (msgArea.querySelector('.chat-welcome')) {
    msgArea.innerHTML = '';
  }

  // If a skill is active, send with skill context
  if (chatState.activeSkill) {
    const skill = chatState.activeSkill;
    const displayText = `${skill.slash_command} ${text}`;
    chatState.messages.push({ role: 'user', content: displayText });
    appendMessageBubble('user', displayText);
    clearActiveSkill();
    streamWithSkill(skill.id, getActiveSnapshotIds(), text);
    return;
  }

  chatState.messages.push({ role: 'user', content: text });
  appendMessageBubble('user', text);
  await streamChat();
}

async function streamWithSkill(skillId, snapshotIds, userPrompt) {
  const msgArea = document.getElementById('chat-messages');
  if (msgArea.querySelector('.chat-welcome')) msgArea.innerHTML = '';

  chatState.isStreaming = true;
  document.getElementById('chat-send-btn').disabled = true;
  startChatTimer('Thinking...');
  const assistantEl = appendMessageBubble('assistant', '');
  const contentEl = assistantEl.querySelector('.chat-msg-content');
  let fullContent = '';

  try {
    updateStatusText('Running skill...');
    const body = {
      messages: chatState.messages,
      snapshot_ids: snapshotIds,
      skill_id: skillId,
    };
    if (userPrompt) body.skill_params = { user_prompt: userPrompt };
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const err = await response.json();
      contentEl.textContent = `Error: ${err.error || response.statusText}`;
      return;
    }
    updateStatusText('Streaming...');
    fullContent = await readSSEStream(response, contentEl, assistantEl);
  } catch (e) {
    contentEl.textContent = `Network error: ${e.message}`;
  } finally {
    chatState.isStreaming = false;
    document.getElementById('chat-send-btn').disabled = false;
    if (fullContent) chatState.messages.push({ role: 'assistant', content: fullContent });
    setChatStatusDone();
    autoScroll();
  }
}

async function streamChat() {
  chatState.isStreaming = true;
  document.getElementById('chat-send-btn').disabled = true;
  startChatTimer('Thinking...');
  const assistantEl = appendMessageBubble('assistant', '');
  const contentEl = assistantEl.querySelector('.chat-msg-content');
  let fullContent = '';

  try {
    const snapshotIds = getActiveSnapshotIds();
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: chatState.messages, snapshot_ids: snapshotIds }),
    });

    if (!response.ok) {
      const err = await response.json();
      contentEl.textContent = `Error: ${err.error || response.statusText}`;
      return;
    }
    updateStatusText('Streaming...');
    fullContent = await readSSEStream(response, contentEl, assistantEl);
  } catch (e) {
    contentEl.textContent = `Network error: ${e.message}`;
  } finally {
    chatState.isStreaming = false;
    document.getElementById('chat-send-btn').disabled = false;
    if (fullContent) chatState.messages.push({ role: 'assistant', content: fullContent });
    setChatStatusDone();
    autoScroll();
  }
}

async function readSSEStream(response, contentEl, assistantEl) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let fullContent = '';
  let eventType = '';

  function processLines(lines) {
    for (const line of lines) {
      if (line.startsWith('event: ')) {
        eventType = line.slice(7).trim();
      } else if (line.startsWith('data: ') && eventType) {
        try {
          const data = JSON.parse(line.slice(6));
          handleSSEEvent(eventType, data, contentEl, assistantEl);
          if (eventType === 'token') fullContent += data.content || '';
        } catch (_) {}
        eventType = '';
      } else if (line === '') {
        eventType = '';
      }
    }
  }

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    processLines(lines);
  }

  if (buffer.trim()) {
    processLines(buffer.split('\n'));
  }

  return fullContent;
}

const TOOL_STATUS_MAP = {
  get_snapshots: 'Fetching snapshots...',
  get_draw_calls: 'Querying draw calls...',
  get_dc_detail: 'Inspecting draw call...',
  get_clock_correlation: 'Analyzing correlations...',
  get_label_agg: 'Aggregating metrics...',
  execute_python: 'Running code...',
  create_skill: 'Creating skill...',
  save_report: 'Saving report...',
};

function handleSSEEvent(type, data, contentEl, assistantEl) {
  if (type === 'token') {
    updateStatusText('Writing...');
    contentEl.innerHTML = renderMarkdown(contentEl.dataset.raw = (contentEl.dataset.raw || '') + (data.content || ''));
    autoScroll();
  } else if (type === 'tool_call') {
    const statusText = TOOL_STATUS_MAP[data.name] || `Calling ${data.name}...`;
    updateStatusText(statusText);
    const indicator = document.createElement('div');
    indicator.className = 'chat-tool-indicator';
    if (data.name === 'execute_python' && data.args && data.args.code) {
      indicator.innerHTML = `<span class="chat-tool-spinner"></span> Running code...<pre class="chat-code-block"><code>${escapeHtml(data.args.code)}</code></pre>`;
    } else if (data.name === 'create_skill' && data.args) {
      indicator.innerHTML = `<span class="chat-tool-spinner"></span> Creating skill <b>${data.args.slash_command || data.args.id}</b>...`;
    } else {
      indicator.innerHTML = `<span class="chat-tool-spinner"></span> Calling <b>${data.name}</b>...`;
    }
    assistantEl.insertBefore(indicator, contentEl);
    autoScroll();
  } else if (type === 'tool_result') {
    updateStatusText('Thinking...');
    const indicators = assistantEl.querySelectorAll('.chat-tool-indicator');
    const last = indicators[indicators.length - 1];
    if (last) {
      let extra = '';
      if (data.name === 'execute_python' && data.result) {
        const r = typeof data.result === 'string' ? JSON.parse(data.result) : data.result;
        if (r.error) {
          extra = `<pre class="chat-code-error">${escapeHtml(r.error)}</pre>`;
        } else if (r.result !== null && r.result !== undefined) {
          const resultStr = typeof r.result === 'string' ? r.result : JSON.stringify(r.result, null, 2);
          extra = `<pre class="chat-code-result">${escapeHtml(resultStr.slice(0, 2000))}</pre>`;
        }
        if (r.output) {
          extra = `<pre class="chat-code-output">${escapeHtml(r.output.slice(0, 1000))}</pre>` + extra;
        }
      } else if (data.name === 'create_skill' && data.result) {
        const r = typeof data.result === 'string' ? JSON.parse(data.result) : data.result;
        if (r.ok) {
          extra = `<span class="chat-skill-created">${r.message}</span>`;
          loadSkills();
        } else if (r.error) {
          extra = `<span class="chat-code-error">${escapeHtml(r.error)}</span>`;
        }
      } else if (data.name === 'save_report' && data.result) {
        const r = typeof data.result === 'string' ? JSON.parse(data.result) : data.result;
        console.log('[save_report] result:', r);
        if (r.ok) {
          const safePath = (r.path || '').replace(/\\/g, '/');
          extra = `<div class="chat-report-saved"><span class="chat-tool-done">&#128196;</span> Report saved: <a href="#" class="report-open-link" onclick="openReportTab('${safePath}','${escapeHtml(r.filename)}');return false;">${escapeHtml(r.filename)}</a></div>`;
        } else if (r.error) {
          extra = `<span class="chat-code-error">${escapeHtml(r.error)}</span>`;
        }
      }
      last.innerHTML = `<span class="chat-tool-done">&#10003;</span> <b>${data.name}</b> <span class="chat-tool-time">${data.duration_ms}ms</span>${extra}`;
    }
    // Render images from execute_python
    if (data.images && data.images.length > 0) {
      for (const b64 of data.images) {
        const imgWrap = document.createElement('div');
        imgWrap.className = 'chat-inline-image';
        imgWrap.innerHTML = `<img src="data:image/png;base64,${b64}" alt="Chart" onclick="openImageZoom(this.src)">`;
        assistantEl.insertBefore(imgWrap, contentEl);
      }
    }
    autoScroll();
  } else if (type === 'error') {
    contentEl.textContent = `Error: ${data.message}`;
  }
}

function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function openImageZoom(src) {
  const overlay = document.createElement('div');
  overlay.className = 'chat-image-zoom-overlay';
  overlay.onclick = () => overlay.remove();
  overlay.innerHTML = `<img src="${src}" class="chat-image-zoom-img">`;
  document.body.appendChild(overlay);
}

function openReportTab(filepath, filename) {
  const tabId = 'report-' + filename.replace(/[^a-z0-9]/gi, '_');

  // If tab exists, switch to it
  if (document.getElementById(`tab-${tabId}`)) {
    switchTab(tabId);
    return;
  }

  // Create tab content
  const section = document.createElement('section');
  section.id = `tab-${tabId}`;
  section.className = 'tab-content report-tab';
  section.innerHTML = '<div class="report-loading">Loading report...</div>';
  document.querySelector('main').appendChild(section);

  // Add tab button to bar
  const bar = document.getElementById('tab-bar');
  const btn = document.createElement('button');
  btn.className = 'tab-btn';
  btn.dataset.tab = tabId;
  const label = document.createElement('span');
  label.textContent = filename;
  btn.appendChild(label);
  const closeSpan = document.createElement('span');
  closeSpan.className = 'tab-close';
  closeSpan.textContent = '×';
  closeSpan.onclick = (e) => { e.stopPropagation(); section.remove(); btn.remove(); if (activeTabId === tabId) switchTab('home'); };
  btn.appendChild(closeSpan);
  btn.onclick = () => switchTab(tabId);
  bar.appendChild(btn);

  switchTab(tabId);

  // Fetch and render
  fetch(`/api/files/read?path=${encodeURIComponent(filepath)}`)
    .then(r => r.json())
    .then(res => {
      if (res.ok) {
        // Convert local image paths to API URLs
        let html = renderMarkdown(res.data.content);
        html = html.replace(/src="([^"]+\.png)"/g, (_, p) => {
          const absPath = p.startsWith('/') || p.includes(':') ? p : filepath.replace(/[^/\\]+$/, '') + p;
          return `src="/api/files/raw?path=${encodeURIComponent(absPath.replace(/\\/g, '/'))}"`;
        });
        section.innerHTML = `<div class="report-content">${html}</div>`;
      } else {
        section.innerHTML = `<div class="report-error">Failed to load: ${res.error}</div>`;
      }
    })
    .catch(e => {
      section.innerHTML = `<div class="report-error">Network error: ${e.message}</div>`;
    });
}

function copyChatConversation() {
  const msgArea = document.getElementById('chat-messages');
  if (!msgArea) return;
  const parts = [];
  for (const msg of msgArea.querySelectorAll('.chat-msg')) {
    const isUser = msg.classList.contains('chat-msg-user');
    const isAssistant = msg.classList.contains('chat-msg-assistant');
    if (!isUser && !isAssistant) continue;
    const prefix = isUser ? 'User' : 'Assistant';

    let msgParts = [];

    // Walk children in DOM order (tools appear before content)
    for (const child of msg.children) {
      if (child.classList.contains('chat-tool-indicator')) {
        const name = child.querySelector('b')?.textContent || '';
        const code = child.querySelector('.chat-code-block code')?.textContent;
        const result = child.querySelector('.chat-code-result')?.textContent;
        const error = child.querySelector('.chat-code-error')?.textContent;
        let line = `> [${name}]`;
        if (code) {
          const lines = code.trim().split('\n');
          line += ' `' + lines.slice(0, 3).join('; ') + (lines.length > 3 ? '...' : '') + '`';
        }
        if (result) line += ' → ' + result.trim().slice(0, 300);
        if (error) line += ' ERR: ' + error.trim().split('\n').pop();
        msgParts.push(line);
      } else if (child.classList.contains('chat-msg-content')) {
        const raw = child.dataset?.raw || child.innerText || '';
        if (raw.trim()) msgParts.push(raw.trim());
      }
    }

    if (msgParts.length) parts.push(`**${prefix}:** ${msgParts.join('\n')}`);
  }
  const text = parts.join('\n\n');
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.querySelector('.chat-copy-btn');
    if (btn) { btn.textContent = '✓'; setTimeout(() => { btn.textContent = '📋'; }, 1500); }
  });
}

function appendMessageBubble(role, content) {
  const msgArea = document.getElementById('chat-messages');
  const bubble = document.createElement('div');
  bubble.className = `chat-msg chat-msg-${role}`;
  bubble.innerHTML = `<div class="chat-msg-content">${content ? renderMarkdown(content) : ''}</div>`;
  if (content) bubble.querySelector('.chat-msg-content').dataset.raw = content;
  msgArea.appendChild(bubble);
  autoScroll();
  return bubble;
}

function autoScroll() {
  const el = document.getElementById('chat-messages');
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  if (atBottom) el.scrollTop = el.scrollHeight;
}

function renderMarkdown(text) {
  if (typeof marked !== 'undefined') {
    try { return marked.parse(text); } catch (_) {}
  }
  return text.replace(/</g, '&lt;').replace(/\n/g, '<br>');
}

// Slash command autocomplete
function updateSlashAutocomplete() {
  const input = document.getElementById('chat-input');
  const dropdown = document.getElementById('chat-slash-dropdown');
  if (!input || !dropdown) return;

  const text = input.value;
  if (text.startsWith('/') && !text.includes(' ')) {
    const matches = chatState.skills.filter(s => s.slash_command.startsWith(text));
    if (matches.length > 0 && text.length > 1) {
      dropdown.innerHTML = matches.map(s =>
        `<div class="slash-item" onmousedown="selectSlashCommand('${s.slash_command}')">${s.icon} <b>${s.slash_command}</b> — ${s.description}</div>`
      ).join('');
      dropdown.style.display = 'block';
      return;
    }
  }
  dropdown.style.display = 'none';
}

function selectSlashCommand(cmd) {
  const input = document.getElementById('chat-input');
  input.value = cmd;
  document.getElementById('chat-slash-dropdown').style.display = 'none';
  input.focus();
}

// Input handling
document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('chat-input');
  if (!input) return;

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendChatMessage();
    }
  });

  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    updateSlashAutocomplete();
  });

  input.addEventListener('blur', () => {
    setTimeout(() => {
      const dd = document.getElementById('chat-slash-dropdown');
      if (dd) dd.style.display = 'none';
    }, 150);
  });

  // Close dropdowns on outside click
  document.addEventListener('click', (e) => {
    const dd = document.getElementById('chat-skills-dropdown');
    const btn = document.getElementById('chat-skills-toggle');
    if (dd && dd.style.display !== 'none' && !dd.contains(e.target) && e.target !== btn) {
      dd.style.display = 'none';
    }
    const picker = document.getElementById('chat-snapshot-picker');
    const pinBtn = document.querySelector('.chat-pin-add-btn');
    if (picker && picker.style.display !== 'none' && !picker.contains(e.target) && e.target !== pinBtn) {
      picker.style.display = 'none';
    }
  });
});
