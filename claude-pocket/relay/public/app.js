// Claude Pocket phone app. Talks only to the relay (same origin) with a bearer token.
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const view = $('#view');

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

let token = store.get('pocket-token') || '';
let tab = store.get('pocket-tab') || 'inbox';
let openSession = null;
let data = { sessions: [], requests: [], usage: null, settings: {}, files: [] };
let online = false;
let pending = [];          // files picked or shared, not yet sent: {file, url}
let sendMode = 'claude';   // 'claude' | 'mac'
let sendTarget = '';
const drafts = {};
let renderQueued = false;

// Pairing link: https://relay/#token=…
if (location.hash.startsWith('#token=')) {
  token = decodeURIComponent(location.hash.slice(7));
  store.set('pocket-token', token);
  history.replaceState(null, '', '/');
}

// ---------------------------------------------------------------- utils

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Just enough Markdown for chat bubbles: code fences, inline code, bold, paragraphs.
function md(text) {
  const parts = String(text || '').split(/```[\w-]*\n?/);
  return parts.map((chunk, i) => {
    if (i % 2) return `<pre>${esc(chunk.replace(/\n$/, ''))}</pre>`;
    return chunk.split(/\n{2,}/).filter((p) => p.trim()).map((p) =>
      '<p>' + esc(p)
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
        .replace(/\n/g, '<br>') + '</p>').join('');
  }).join('');
}

function ago(ts) {
  const s = Math.round((Date.now() - ts) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function until(epochSec) {
  const m = Math.max(0, Math.round((epochSec * 1000 - Date.now()) / 60000));
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${m % 60} min`;
  return `${Math.round(h / 24)} days`;
}

function size(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), 2200);
}

async function api(method, path, body, raw) {
  const res = await fetch(path, {
    method,
    headers: { Authorization: `Bearer ${token}`, ...(raw ? { 'Content-Type': raw } : body ? { 'Content-Type': 'application/json' } : {}) },
    body: raw ? body : body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) { token = ''; store.set('pocket-token', ''); render(); throw new Error('Token rejected'); }
  if (!res.ok) throw new Error(`Request failed (${res.status})`);
  return res.json();
}

const sessionName = (id) => data.sessions.find((s) => s.id === id)?.name || 'Session';

// ---------------------------------------------------------------- data flow

async function refresh() {
  try {
    const prev = new Set(data.requests.map((r) => r.id));
    data = await api('GET', '/api/state');
    setOnline(true);
    if (data.requests.some((r) => !prev.has(r.id)) && prev.size >= 0 && refresh.loaded) navigator.vibrate?.(60);
    refresh.loaded = true;
    render();
  } catch { setOnline(false); }
}

let es;
function connect() {
  es?.close();
  if (!token) return;
  es = new EventSource(`/api/events?token=${encodeURIComponent(token)}`);
  es.addEventListener('hello', () => { setOnline(true); refresh(); });
  es.addEventListener('update', () => refresh());
  es.onerror = () => setOnline(false);
}

function setOnline(v) {
  online = v;
  $('#dot').className = 'dot ' + (v ? 'on' : 'off');
}

document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); if (es?.readyState === 2) connect(); } });

// ---------------------------------------------------------------- render

// Re-rendering while someone types would eat their input, so wait for blur.
function render() {
  const a = document.activeElement;
  if (a && view.contains(a) && /^(TEXTAREA|INPUT|SELECT)$/.test(a.tagName)) { renderQueued = true; return; }
  renderQueued = false;
  if (!token) return renderSetup();
  $('#nav').hidden = false;
  $('#awayWrap').hidden = false;
  $('#away').setAttribute('aria-checked', String(!!data.settings?.away));
  const n = data.requests.length;
  $('#badge').hidden = !n; $('#badge').textContent = n;
  for (const b of document.querySelectorAll('nav button')) b.classList.toggle('on', b.dataset.tab === tab);
  ({ inbox: renderInbox, chats: renderChats, usage: renderUsage, send: renderSend })[tab]();
}
view.addEventListener('focusout', () => setTimeout(() => {
  if (renderQueued && !view.contains(document.activeElement)) render();
}, 0));

function renderSetup() {
  $('#nav').hidden = true; $('#awayWrap').hidden = true;
  view.innerHTML = `
    <div class="card">
      <h3>Connect to your relay</h3>
      <p class="sub">Paste the POCKET_TOKEN you set on the relay server. You only do this once.</p>
      <div class="field"><input type="password" id="tok" placeholder="Token" autocomplete="off"></div>
      <div class="btns"><button class="btn" id="save">Connect</button></div>
    </div>`;
  $('#save').onclick = async () => {
    token = $('#tok').value.trim();
    store.set('pocket-token', token);
    try { await api('GET', '/api/state'); connect(); render(); toast('Connected'); }
    catch { toast('That token did not work'); }
  };
}

// ---- inbox

function renderInbox() {
  if (!data.requests.length) {
    const away = data.settings?.away;
    view.innerHTML = `<div class="empty"><b>Nothing waiting</b>${away
      ? 'Away mode is on — permission prompts, questions and finished turns land here.'
      : 'Turn on Away mode to approve tools, answer questions and reply to Claude from here.'}</div>`;
    return;
  }
  view.innerHTML = data.requests.map(requestCard).join('');
  for (const el of view.querySelectorAll('[data-draft]')) {
    el.value = drafts[el.dataset.draft] || '';
    el.oninput = () => (drafts[el.dataset.draft] = el.value);
  }
}

function requestCard(r) {
  const head = `<div class="row"><div class="grow"><h2 style="margin:0">${esc(r.sessionName || sessionName(r.sessionId))}</h2></div><span class="sub">${ago(r.createdAt)}</span></div>`;
  if (r.kind === 'permission') {
    const i = r.payload.input || {};
    const body = i.command ?? i.content ?? i.new_string ?? i.url ?? i.prompt ?? JSON.stringify(i, null, 2);
    const target = i.file_path || i.path || i.description || '';
    return `<div class="card">${head}
      <h3 style="margin-top:10px">Allow ${esc(r.payload.tool)}?</h3>
      ${target ? `<div class="sub">${esc(target)}</div>` : ''}
      <pre>${esc(String(body).slice(0, 4000))}</pre>
      <div class="btns">
        <button class="btn" onclick="answer('${r.id}',{allow:true})">Allow</button>
        ${r.payload.suggestions?.length ? `<button class="btn ghost" onclick="answer('${r.id}',{allow:true,always:true})">Always allow</button>` : ''}
        <button class="btn danger" onclick="deny('${r.id}')">Deny</button>
      </div>
      <div class="field"><input type="text" data-draft="deny-${r.id}" placeholder="Optional: tell Claude why / what to do instead"></div>
    </div>`;
  }
  if (r.kind === 'question') {
    return `<div class="card">${head}
      ${r.payload.questions.map((q, qi) => `
        <h3 style="margin-top:12px">${esc(q.question)}</h3>
        ${q.options.map((o, oi) => `
          <label class="opt"><input type="${q.multiSelect ? 'checkbox' : 'radio'}" name="q-${r.id}-${qi}" value="${oi}">
            <span><b>${esc(o.label)}</b>${o.description ? `<small>${esc(o.description)}</small>` : ''}</span></label>`).join('')}
        <div class="field"><input type="text" data-draft="other-${r.id}-${qi}" placeholder="Other…"></div>`).join('')}
      <div class="btns"><button class="btn" onclick="answerQuestions('${r.id}')">Send answer</button></div>
    </div>`;
  }
  // stop: Claude finished a turn
  return `<div class="card">${head}
    <div class="sub" style="margin-top:6px">Claude finished and is waiting for you.</div>
    <div class="msg assistant" style="max-width:100%;margin-top:10px">${md(r.payload.last || '')}</div>
    <div class="field"><textarea rows="3" data-draft="reply-${r.id}" placeholder="Reply to Claude…"></textarea></div>
    ${pendingThumbs()}
    <div class="btns">
      <label class="btn ghost" style="flex:0 0 auto;text-align:center">Attach<input type="file" accept="image/*,video/*,*/*" multiple hidden onchange="addFiles(this.files)"></label>
      <button class="btn" onclick="reply('${r.id}')">Send</button>
      <button class="btn ghost" onclick="answer('${r.id}',{done:true})">Done</button>
    </div>
  </div>`;
}

async function answer(id, ans) {
  try { await api('POST', `/api/phone/answer/${id}`, { answer: ans }); toast('Sent'); }
  catch (e) { toast(e.message); }
}
function deny(id) { answer(id, { allow: false, message: drafts[`deny-${id}`] || undefined }); }

function answerQuestions(id) {
  const r = data.requests.find((x) => x.id === id);
  const answers = {};
  r.payload.questions.forEach((q, qi) => {
    const picked = [...view.querySelectorAll(`input[name="q-${id}-${qi}"]:checked`)].map((el) => q.options[el.value].label);
    const other = (drafts[`other-${id}-${qi}`] || '').trim();
    if (other) picked.push(other);
    answers[q.question] = picked.join(', ');
  });
  if (Object.values(answers).some((v) => !v)) return toast('Answer every question first');
  answer(id, { answers });
}

async function reply(id) {
  const r = data.requests.find((x) => x.id === id);
  const text = (drafts[`reply-${id}`] || '').trim();
  if (!text && !pending.length) return toast('Write something or attach a file');
  try {
    await uploadPending({ claude: true, target: r.sessionId });
    await api('POST', `/api/phone/answer/${id}`, { answer: { text: text || 'See the attached file(s).' } });
    drafts[`reply-${id}`] = '';
    toast('Sent to Claude');
  } catch (e) { toast(e.message); }
}

// ---- chats

function renderChats() {
  if (openSession) return renderChat(openSession);
  if (!data.sessions.length) {
    view.innerHTML = `<div class="empty"><b>No sessions yet</b>Start Claude Code on your Mac after installing the bridge.</div>`;
    return;
  }
  view.innerHTML = `<div class="card list">${data.sessions.map((s) => `
    <div class="list-item" onclick="openChat('${s.id}')">
      <div class="grow"><div class="t">${esc(s.name)}</div><div class="sub">${esc(s.cwd || '')} · ${ago(s.updatedAt)}</div></div>
      <span class="pill ${esc(s.status)}">${esc(s.status)}</span>
    </div>`).join('')}</div>`;
}

function openChat(id) { openSession = id; render(); window.scrollTo(0, document.body.scrollHeight); }

function renderChat(id) {
  const s = data.sessions.find((x) => x.id === id);
  if (!s) { openSession = null; return renderChats(); }
  const stop = data.requests.find((r) => r.kind === 'stop' && r.sessionId === id);
  const stick = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80;
  view.innerHTML = `
    <button class="back" onclick="openSession=null;render()">‹ All chats</button>
    <div class="row" style="margin-bottom:12px"><div class="grow"><h3>${esc(s.name)}</h3><div class="sub">${esc(s.cwd || '')}</div></div><span class="pill ${esc(s.status)}">${esc(s.status)}</span></div>
    <div class="msgs">${(s.messages || []).map((m) =>
      m.role === 'tool' ? `<div class="msg tool">▸ ${esc(m.text)}</div>` : `<div class="msg ${m.role}">${md(m.text)}</div>`).join('')}</div>
    <div class="composer">
      ${pendingThumbs()}
      <div class="row" style="margin-top:8px">
        <label class="btn ghost" style="flex:0 0 auto" aria-label="Attach">+<input type="file" multiple hidden onchange="addFiles(this.files)"></label>
        <textarea class="grow" rows="1" data-draft="chat-${id}" placeholder="${stop ? 'Reply to Claude…' : 'Message Claude…'}"></textarea>
        <button class="btn" style="flex:0 0 auto" onclick="chatSend('${id}')">Send</button>
      </div>
    </div>`;
  const ta = $(`[data-draft="chat-${id}"]`);
  ta.value = drafts[`chat-${id}`] || '';
  ta.oninput = () => (drafts[`chat-${id}`] = ta.value);
  if (stick) window.scrollTo(0, document.body.scrollHeight);
}

async function chatSend(id) {
  const text = (drafts[`chat-${id}`] || '').trim();
  if (!text && !pending.length) return;
  const stop = data.requests.find((r) => r.kind === 'stop' && r.sessionId === id);
  try {
    if (stop) {
      await uploadPending({ claude: true, target: id });
      await api('POST', `/api/phone/answer/${stop.id}`, { answer: { text: text || 'See the attached file(s).' } });
    } else {
      const hadFiles = pending.length > 0;
      await uploadPending({ claude: true, target: id, note: text });
      if (text && !hadFiles) await api('POST', `/api/phone/upload?name=message.txt&claude=1&target=${id}&note=${encodeURIComponent(text)}`, new Blob([]), 'text/plain');
    }
    drafts[`chat-${id}`] = '';
    toast(stop ? 'Sent to Claude' : 'Queued — Claude sees it at its next step');
    render();
  } catch (e) { toast(e.message); }
}

// ---- usage

function meter(label, w) {
  if (!w) return '';
  const pct = Math.round(w.used_percentage);
  const cls = pct >= 90 ? 'bad' : pct >= 70 ? 'warn' : '';
  return `<div class="card"><h2>${label}</h2>
    <div class="big">${pct}%</div>
    <div class="meter ${cls}"><i style="width:${Math.min(100, pct)}%"></i></div>
    <div class="sub">Resets in ${until(w.resets_at)}</div></div>`;
}

function renderUsage() {
  const u = data.usage;
  if (!u) {
    view.innerHTML = `<div class="empty"><b>No usage yet</b>Numbers appear after Claude Code on your Mac makes its first request.</div>`;
    return;
  }
  const rl = u.rateLimits || {};
  view.innerHTML = `
    ${meter('Current session · 5 hours', rl.five_hour)}
    ${meter('Weekly · 7 days', rl.seven_day)}
    ${meter('Spend limit', rl.spend_limit)}
    ${!rl.five_hour && !rl.seven_day ? `<div class="card"><div class="sub">Plan limits only show for Pro and Max subscriptions.</div></div>` : ''}
    ${u.context ? `<div class="card"><h2>Context window</h2>
      <div class="big">${Math.round(u.context.usedPercentage || 0)}%</div>
      <div class="meter"><i style="width:${Math.min(100, u.context.usedPercentage || 0)}%"></i></div>
      <div class="sub">${esc(u.sessionName || sessionName(u.sessionId))}</div></div>` : ''}
    <div class="card"><div class="row"><div class="grow"><div class="sub">Model</div><b>${esc(u.model || '—')}</b></div>
      <div style="text-align:right"><div class="sub">Updated</div><b>${ago(u.updatedAt)}</b></div></div></div>`;
}

// ---- send

function pendingThumbs() {
  if (!pending.length) return '';
  return `<div class="thumbs">${pending.map((p, i) => `
    <div class="thumb">${p.url ? `<img src="${p.url}" alt="">` : esc(p.file.name)}
      <button onclick="removePending(${i})" aria-label="Remove">×</button></div>`).join('')}</div>`;
}

function addFiles(list) {
  for (const f of list) pending.push({ file: f, url: f.type.startsWith('image/') ? URL.createObjectURL(f) : '' });
  render();
}
function removePending(i) { const [p] = pending.splice(i, 1); if (p?.url) URL.revokeObjectURL(p.url); render(); }

async function uploadPending({ claude, target = '', note = '' }) {
  const list = pending.slice();
  for (const [i, p] of list.entries()) {
    const q = new URLSearchParams({ name: p.file.name || 'file', claude: claude ? '1' : '0', target, note: i === 0 ? note : '' });
    await api('POST', `/api/phone/upload?${q}`, p.file, p.file.type || 'application/octet-stream');
  }
  for (const p of list) if (p.url) URL.revokeObjectURL(p.url);
  pending = [];
}

function renderSend() {
  const sessions = data.sessions.filter((s) => s.status !== 'ended');
  view.innerHTML = `
    <div class="card">
      <h2>Send to Mac</h2>
      <div class="seg">
        <button class="${sendMode === 'claude' ? 'on' : ''}" onclick="sendMode='claude';render()">To Claude</button>
        <button class="${sendMode === 'mac' ? 'on' : ''}" onclick="sendMode='mac';render()">Just the file</button>
      </div>
      <div class="sub" style="margin-top:8px">${sendMode === 'claude'
        ? 'Claude gets the file and your note at its next step (or right away if it is waiting on you).'
        : 'Saved to Downloads › Claude Pocket on the Mac.'}</div>
      ${sendMode === 'claude' ? `<div class="field"><span>Session</span><select id="target">
        <option value="">Whichever session runs next</option>
        ${sessions.map((s) => `<option value="${s.id}" ${s.id === sendTarget ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}
      </select></div>
      <div class="field"><span>Note for Claude</span><textarea id="note" rows="2" data-draft="send-note" placeholder="e.g. The button in this screenshot is misaligned"></textarea></div>` : ''}
      ${pendingThumbs()}
      <div class="btns">
        <label class="btn ghost" style="text-align:center">Choose files<input type="file" multiple hidden onchange="addFiles(this.files)"></label>
        <button class="btn" id="sendBtn" ${pending.length || sendMode === 'claude' ? '' : 'disabled'}>Send</button>
      </div>
      <div class="sub" style="margin-top:10px">Tip: share any screenshot straight from Android's share sheet to Claude Pocket.</div>
    </div>
    <div class="card"><h2>Recent</h2>${data.files.length ? data.files.slice(0, 20).map(fileRow).join('') : '<div class="sub">Nothing sent yet.</div>'}</div>`;
  const note = $('#note');
  if (note) { note.value = drafts['send-note'] || ''; note.oninput = () => (drafts['send-note'] = note.value); }
  const sel = $('#target'); if (sel) sel.onchange = () => (sendTarget = sel.value);
  $('#sendBtn').onclick = sendNow;
}

function fileRow(f) {
  const img = f.type?.startsWith('image/') && f.size
    ? `<img src="/api/files/${f.id}?token=${encodeURIComponent(token)}" alt="" loading="lazy">` : '<div class="ph"></div>';
  const status = f.forClaude ? (f.claudeTaken ? 'Claude has it' : 'Waiting for Claude') : (f.macTaken ? 'On Mac' : 'Waiting for Mac');
  return `<div class="file-row">${img}<div class="grow"><div class="t" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.size ? f.name : f.note)}</div>
    <div class="sub">${status} · ${f.size ? size(f.size) + ' · ' : ''}${ago(f.createdAt)}</div></div></div>`;
}

async function sendNow() {
  const note = (drafts['send-note'] || '').trim();
  const claude = sendMode === 'claude';
  if (!pending.length && !(claude && note)) return toast('Pick a file or write a note');
  const btn = $('#sendBtn'); btn.disabled = true; btn.textContent = 'Sending…';
  try {
    const target = claude ? sendTarget : '';
    const stop = claude && data.requests.find((r) => r.kind === 'stop' && (!target || r.sessionId === target));
    if (stop) {
      await uploadPending({ claude: true, target: stop.sessionId });
      await api('POST', `/api/phone/answer/${stop.id}`, { answer: { text: note || 'See the attached file(s).' } });
    } else if (pending.length) {
      await uploadPending({ claude, target, note: claude ? note : '' });
    } else {
      await api('POST', `/api/phone/upload?name=message.txt&claude=1&target=${target}&note=${encodeURIComponent(note)}`, new Blob([]), 'text/plain');
    }
    drafts['send-note'] = '';
    toast(stop ? 'Sent to Claude' : 'Sent');
  } catch (e) { toast(e.message); }
  render();
}

// ---------------------------------------------------------------- share target

// The service worker parks shared files in Cache Storage and opens /?share=1.
async function takeShared() {
  if (!new URLSearchParams(location.search).has('share')) return;
  history.replaceState(null, '', '/');
  try {
    const cache = await caches.open('pocket-share');
    const meta = await (await cache.match('/_share/meta'))?.json();
    if (!meta) return;
    for (const f of meta.files) {
      const res = await cache.match(`/_share/${f.i}`);
      if (res) {
        const file = new File([await res.blob()], f.name, { type: f.type });
        pending.push({ file, url: f.type.startsWith('image/') ? URL.createObjectURL(file) : '' });
      }
    }
    const text = [meta.title, meta.text, meta.url].filter(Boolean).join('\n');
    if (text) drafts['send-note'] = text;
    await caches.delete('pocket-share');
    tab = 'send'; sendMode = 'claude';
    const stop = data.requests.find((r) => r.kind === 'stop');
    if (stop) { tab = 'inbox'; drafts[`reply-${stop.id}`] = text; }
  } catch {}
}

// ---------------------------------------------------------------- wiring

document.querySelectorAll('nav button').forEach((b) => (b.onclick = () => {
  tab = b.dataset.tab; store.set('pocket-tab', tab);
  if (tab !== 'chats') openSession = null;
  if (document.activeElement) document.activeElement.blur();
  renderQueued = false; render(); window.scrollTo(0, 0);
}));

$('#away').onclick = async () => {
  const away = !data.settings?.away;
  data.settings.away = away; render();
  try { await api('POST', '/api/phone/settings', { away }); toast(away ? 'Away mode on — prompts come here' : 'Away mode off — prompts stay on the Mac'); }
  catch (e) { toast(e.message); }
};

if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});

(async () => {
  render();
  if (!token) return;
  await refresh();
  await takeShared();
  render();
  connect();
})();
setInterval(() => { if (tab === 'usage' || tab === 'inbox') render(); }, 30000);
