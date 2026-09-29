// Claude Pocket phone app. Talks only to the relay (same origin) with a bearer token.
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const view = $('#view');
const dock = $('#dock');

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

let token = store.get('pocket-token') || '';
let data = { sessions: [], requests: [], usage: null, settings: {}, files: [], threads: [] };
const threadCache = {};      // thread id -> messages
let pending = [];            // files picked or shared, not yet sent: {file, url}
let sendMode = 'claude';     // 'claude' | 'mac'
let sendTarget = '';
const drafts = {};
let renderQueued = false;

const NATIVE = !!window.PocketNative; // running inside the Android app

// Pairing link: https://relay/#token=…[&go=/m/ideas]  (the Android app always opens this way)
if (location.hash.startsWith('#token=')) {
  const params = new URLSearchParams(location.hash.slice(1));
  token = params.get('token') || '';
  store.set('pocket-token', token);
  const go = params.get('go');
  history.replaceState(null, '', '/#' + (go && go.startsWith('/') ? go : '/' + (store.get('pocket-page') || 'inbox')));
}

// ---------------------------------------------------------------- icons

const I = {
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5h13L22 12v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6z"/>',
  messages: '<path d="M12 3C6.5 3 2 6.8 2 11.5c0 2.6 1.4 4.9 3.6 6.5L5 22l4.3-2.4c.9.2 1.8.3 2.7.3 5.5 0 10-3.8 10-8.5S17.5 3 12 3z"/>',
  usage: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  send: '<path d="M12 19V5M5 12l7-7 7 7"/><path d="M4 21h16"/>',
  up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  tool: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z"/>',
  back: '<path d="M15 18l-6-6 6-6"/>',
  chev: '<path d="M9 18l6-6-6-6"/>',
  compass: '<circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/>',
  bulb: '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.4 1 1.1 1 1.8V16h5v-.3c0-.7.4-1.4 1-1.8A6 6 0 0 0 12 3z"/>',
};
const icon = (n, sw = 1.8) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round">${I[n]}</svg>`;

// ---------------------------------------------------------------- utils

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Just enough Markdown for transcripts: code fences, inline code, bold, paragraphs.
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

// Plain text with clickable links, for message bubbles.
function linkify(text) {
  return esc(text).replace(/https?:\/\/[^\s<]+[^\s<.,;:!?)\]'"]/g, (u) => `<a href="${u}" target="_blank" rel="noopener">${u}</a>`);
}

function ago(ts) {
  const s = Math.round((Date.now() - ts) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

const sameDay = (a, b) => new Date(a).toDateString() === new Date(b).toDateString();
const hhmm = (ts) => new Date(ts).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });

// iOS Messages list: "14:32", "Yesterday", "Monday", "12/10/2026"
function listTime(ts) {
  const now = Date.now();
  if (sameDay(ts, now)) return hhmm(ts);
  if (sameDay(ts, now - 864e5)) return 'Yesterday';
  if (now - ts < 6 * 864e5) return new Date(ts).toLocaleDateString('en-GB', { weekday: 'long' });
  return new Date(ts).toLocaleDateString('en-GB');
}

// iOS thread stamp: "Today 14:32", "Yesterday 09:10", "Mon 12 Oct at 09:10"
function stamp(ts) {
  const now = Date.now();
  const day = sameDay(ts, now) ? 'Today' : sameDay(ts, now - 864e5) ? 'Yesterday'
    : new Date(ts).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
  return `<b>${day}</b> ${hhmm(ts)}`;
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

const initials = (name) => (String(name).match(/[\p{L}\p{N}]+/gu) || ['?']).slice(0, 2).map((w) => w[0]).join('').toUpperCase();
const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch { return u; } };

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
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `Request failed (${res.status})`);
  return res.json();
}

// A newer Android build is waiting on the relay (only meaningful inside the app).
function updateReady() {
  if (!NATIVE || !data.app) return false;
  try { return data.app.versionCode > Number(PocketNative.versionCode()); } catch { return false; }
}
function installUpdate() { PocketNative.installUpdate(); }

const sessionName = (id) => data.sessions.find((s) => s.id === id)?.name || 'Session';

// ---------------------------------------------------------------- routing

function route() {
  const [, a = 'inbox', b = ''] = location.hash.split('/');
  return { page: a, id: decodeURIComponent(b) };
}
function go(hash) { if (location.hash !== hash) location.hash = hash; else render(); }
window.addEventListener('hashchange', () => {
  document.body.classList.remove('drawer-open');
  if (document.activeElement) document.activeElement.blur();
  renderQueued = false; render(); window.scrollTo(0, 0);
  const r = route();
  if (r.page === 'm') openThread(r.id);
});

// ---------------------------------------------------------------- data flow

async function refresh() {
  try {
    const prev = new Set(data.requests.map((r) => r.id));
    data = await api('GET', '/api/state');
    setOnline(true);
    if (refresh.loaded && data.requests.some((r) => !prev.has(r.id))) navigator.vibrate?.(60);
    refresh.loaded = true;
    const r = route();
    if (r.page === 'm') await loadThread(r.id, true);
    render();
  } catch { setOnline(false); }
}

async function loadThread(tid, markRead) {
  try {
    const { messages } = await api('GET', `/api/messages?thread=${encodeURIComponent(tid)}`);
    threadCache[tid] = messages;
    const t = data.threads.find((x) => x.id === tid);
    if (markRead && t?.unread) { t.unread = 0; api('POST', `/api/phone/threads/${tid}/read`).catch(() => {}); }
  } catch {}
}

async function openThread(tid) {
  await loadThread(tid, true);
  render();
  window.scrollTo(0, document.body.scrollHeight);
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

let online = false;
function setOnline(v) { online = v; const c = $('#conn'); if (c) c.className = 'conn ' + (v ? 'on' : 'off'); }

document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); if (es?.readyState === 2) connect(); } });

// ---------------------------------------------------------------- render

// Re-rendering while someone types would eat their input, so wait for blur.
function render() {
  const a = document.activeElement;
  if (a && (view.contains(a) || dock.contains(a)) && /^(TEXTAREA|INPUT|SELECT)$/.test(a.tagName)) { renderQueued = true; return; }
  renderQueued = false;
  dock.innerHTML = '';
  view.classList.remove('has-composer');
  if (!token) return renderSetup();

  const r = route();
  document.body.classList.toggle('ios', r.page === 'messages' || r.page === 'm');
  $('#awayWrap').hidden = r.page === 'messages' || r.page === 'm';
  $('#away').setAttribute('aria-checked', String(!!data.settings?.away));
  const unread = data.threads.reduce((n, t) => n + (t.unread || 0), 0);
  $('#menuDot').hidden = !(data.requests.length || unread || updateReady());
  renderDrawer(r, unread);

  const pages = { inbox: renderInbox, messages: renderThreads, m: renderThread, s: renderSession, usage: renderUsage, send: renderSend };
  (pages[r.page] || renderInbox)(r.id);
}
for (const el of [view, dock]) {
  el.addEventListener('focusout', () => setTimeout(() => {
    if (renderQueued && !view.contains(document.activeElement) && !dock.contains(document.activeElement)) render();
  }, 0));
}

function setTitle(html) { $('#title').innerHTML = html; }

function renderDrawer(r, unread) {
  const item = (hash, ic, label, count, on) =>
    `<button class="nav-item ${on ? 'on' : ''}" onclick="go('${hash}')">${icon(ic)}<span class="lbl">${label}</span>${count ? `<span class="count">${count}</span>` : ''}</button>`;
  const live = data.sessions.filter((s) => s.status !== 'ended');
  const old = data.sessions.filter((s) => s.status === 'ended').slice(0, 10);
  const sess = (s) => `<button class="nav-item ${r.page === 's' && r.id === s.id ? 'on' : ''}" onclick="go('#/s/${s.id}')">
      <span class="sdot ${esc(s.status)}"></span><span class="lbl">${esc(s.name)}</span></button>`;
  $('#drawer').innerHTML = `
    <div class="brand"><img src="/icon-192.png" alt="">Claude Pocket</div>
    ${item('#/inbox', 'inbox', 'Inbox', data.requests.length, r.page === 'inbox')}
    ${item('#/messages', 'messages', 'Messages', unread, r.page === 'messages' || r.page === 'm')}
    ${item('#/m/ideas', 'bulb', 'Ideas', 0, r.page === 'm' && r.id === 'ideas')}
    ${item('#/send', 'send', 'Send to Mac', 0, r.page === 'send')}
    ${item('#/usage', 'usage', 'Usage', 0, r.page === 'usage')}
    ${updateReady() ? `<button class="nav-item" onclick="installUpdate()">${icon('up')}<span class="lbl">Update app</span><span class="count">${esc(data.app.versionName)}</span></button>` : ''}
    <div class="nav-scroll">
      ${live.length ? `<div class="nav-head">Sessions</div>${live.map(sess).join('')}` : ''}
      ${old.length ? `<div class="nav-head">Earlier</div>${old.map(sess).join('')}` : ''}
    </div>
    <div class="drawer-foot"><span class="conn ${online ? 'on' : 'off'}" id="conn"></span>${online ? 'Connected' : 'Offline'}
      <button class="nav-item" style="width:auto;margin-left:auto;font-size:13px;color:var(--muted)" onclick="signOut()">Sign out</button></div>`;
}

function signOut() {
  if (!confirm('Disconnect this phone from the relay?')) return;
  token = ''; store.set('pocket-token', ''); es?.close();
  if (NATIVE) { PocketNative.resetPairing(); return; }
  document.body.classList.remove('drawer-open'); render();
}

function renderSetup() {
  document.body.classList.remove('ios');
  $('#awayWrap').hidden = true; $('#menuDot').hidden = true;
  setTitle('Claude Pocket');
  $('#drawer').innerHTML = '';
  view.innerHTML = `
    <div class="hero"><h2>Connect your relay</h2><p>Paste the POCKET_TOKEN you set on the relay server. You only do this once.</p></div>
    <div class="panel" style="margin-top:24px">
      <input type="password" id="tok" placeholder="Token" autocomplete="off">
      <div class="btns"><button class="btn" id="save">Connect</button></div>
    </div>`;
  $('#save').onclick = async () => {
    token = $('#tok').value.trim();
    store.set('pocket-token', token);
    try { await refresh(); connect(); go('#/inbox'); toast('Connected'); }
    catch { toast('That token did not work'); }
  };
}

function bindDrafts(root = document) {
  for (const el of root.querySelectorAll('[data-draft]')) {
    el.value = drafts[el.dataset.draft] || '';
    el.addEventListener('input', () => { drafts[el.dataset.draft] = el.value; autosize(el); });
    autosize(el);
  }
}
function autosize(el) {
  if (el.tagName !== 'TEXTAREA' || !el.closest('.composer, .ios-field')) return;
  el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

// ---- inbox

function updateBanner() {
  return updateReady() ? `<div class="panel"><div class="stat" style="align-items:center"><div><h3>Update ready</h3>
    <div class="meta">Claude Pocket ${esc(data.app.versionName)} is on your Mac.</div></div>
    <button class="btn" style="flex:0 0 auto" onclick="installUpdate()">Install</button></div></div>` : '';
}

function renderInbox() {
  setTitle('Inbox');
  if (!data.requests.length) {
    const away = data.settings?.away;
    view.innerHTML = updateBanner() + `<div class="hero"><h2>All caught up</h2><p>${away
      ? 'Away mode is on. Permission prompts, questions and finished turns show up here.'
      : 'Turn on Away mode to approve tools, answer questions and reply to Claude from your phone.'}</p></div>`;
    return;
  }
  view.innerHTML = updateBanner() + data.requests.map(requestCard).join('');
  bindDrafts(view);
}

function requestCard(r) {
  const head = `<div class="stat" style="align-items:center"><span class="pill"><span class="sdot" style="background:var(--warn)"></span>${esc(r.sessionName || sessionName(r.sessionId))}</span><span class="meta">${ago(r.createdAt)}</span></div>`;
  if (r.kind === 'permission') {
    const i = r.payload.input || {};
    const body = i.command ?? i.content ?? i.new_string ?? i.url ?? i.prompt ?? JSON.stringify(i, null, 2);
    const target = i.file_path || i.path || i.description || '';
    return `<div class="panel">${head}
      <h3 style="margin-top:12px">Allow ${esc(r.payload.tool)}?</h3>
      ${target ? `<div class="meta">${esc(target)}</div>` : ''}
      <pre>${esc(String(body).slice(0, 4000))}</pre>
      <input type="text" data-draft="deny-${r.id}" placeholder="Optional: tell Claude why or what to do instead">
      <div class="btns">
        <button class="btn" onclick="answer('${r.id}',{allow:true})">Allow</button>
        ${r.payload.suggestions?.length ? `<button class="btn outline" onclick="answer('${r.id}',{allow:true,always:true})">Always</button>` : ''}
        <button class="btn danger" onclick="deny('${r.id}')">Deny</button>
      </div>
    </div>`;
  }
  if (r.kind === 'question') {
    return `<div class="panel">${head}
      ${r.payload.questions.map((q, qi) => `
        <h3 style="margin-top:14px">${esc(q.question)}</h3>
        ${q.options.map((o, oi) => `
          <label class="opt"><input type="${q.multiSelect ? 'checkbox' : 'radio'}" name="q-${r.id}-${qi}" value="${oi}">
            <span>${esc(o.label)}${o.description ? `<small>${esc(o.description)}</small>` : ''}</span></label>`).join('')}
        <div class="field"><input type="text" data-draft="other-${r.id}-${qi}" placeholder="Something else…"></div>`).join('')}
      <div class="btns"><button class="btn" onclick="answerQuestions('${r.id}')">Send answer</button></div>
    </div>`;
  }
  return `<div class="panel">${head}
    <div class="turn-ai" style="margin-top:12px">${md(r.payload.last || 'Finished.')}</div>
    <div class="field"><textarea rows="3" data-draft="reply-${r.id}" placeholder="Reply so Claude keeps going…"></textarea></div>
    ${pendingThumbs()}
    <div class="btns">
      <label class="btn outline" style="flex:0 0 auto">Attach<input type="file" multiple hidden onchange="addFiles(this.files)"></label>
      <button class="btn" onclick="reply('${r.id}')">Reply</button>
      <button class="btn outline" onclick="answer('${r.id}',{done:true})">Done</button>
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

// ---- Claude Code session (AI-chat look)

function renderSession(id) {
  const s = data.sessions.find((x) => x.id === id);
  if (!s) { setTitle('Session'); view.innerHTML = '<div class="hero"><h2>Session not found</h2></div>'; return; }
  const stop = data.requests.find((r) => r.kind === 'stop' && r.sessionId === id);
  setTitle(`${esc(s.name)}<small>${esc(s.status)} · ${esc((s.cwd || '').split('/').pop())}</small>`);
  const stick = !renderSession.last || renderSession.last !== id || window.innerHeight + window.scrollY >= document.body.scrollHeight - 120;
  renderSession.last = id;
  view.classList.add('has-composer');
  view.innerHTML = `<div class="chat">${(s.messages || []).map((m) =>
    m.role === 'tool' ? `<div class="turn-tool">${icon('tool')}<span>${esc(m.text)}</span></div>`
      : m.role === 'user' ? `<div class="turn-user">${md(m.text)}</div>`
        : `<div class="turn-ai">${md(m.text)}</div>`).join('') || '<div class="hero"><p>No messages yet.</p></div>'}</div>`;
  dock.innerHTML = composer(`chat-${id}`, stop ? 'Reply to Claude' : 'Message Claude',
    stop ? 'Claude is waiting for you' : s.status === 'working' ? 'Delivered at Claude\'s next step' : 'Delivered with your next prompt', `chatSend('${id}')`);
  bindDrafts(dock);
  if (stick) window.scrollTo(0, document.body.scrollHeight);
}

function composer(key, placeholder, hint, onsend) {
  return `<div class="composer-wrap"><div class="composer">
    ${pendingThumbs()}
    <textarea rows="1" data-draft="${key}" placeholder="${esc(placeholder)}"></textarea>
    <div class="composer-row">
      <label class="round plain" aria-label="Attach">${icon('plus', 2)}<input type="file" multiple hidden onchange="addFiles(this.files)"></label>
      <span class="hint">${esc(hint)}</span>
      <button class="round go" aria-label="Send" onclick="${onsend}">${icon('up', 2.2)}</button>
    </div></div></div>`;
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
    toast(stop ? 'Sent to Claude' : 'Queued for Claude');
    document.activeElement?.blur(); render();
  } catch (e) { toast(e.message); }
}

// ---- Messages (iMessage look)

function renderThreads() {
  setTitle('');
  const list = data.threads.filter((t) => t.id === 'ideas' || t.last);
  view.innerHTML = `<div class="ios-large">Messages</div>
    ${list.length ? `<ul class="ios-list">${list.map((t) => {
      const ideas = t.id === 'ideas';
      const preview = t.last ? (t.last.from === 'me' && !ideas ? 'You: ' : '') + t.last.text : 'Write down ideas — Claude saves them for later.';
      return `<li class="ios-row" onclick="go('#/m/${t.id}')">
        <span class="unread ${t.unread ? '' : 'no'}"></span>
        <span class="avatar" ${ideas ? 'style="background:linear-gradient(180deg,#ffd60a,#ff9f0a)"' : ''}>${ideas ? icon('bulb', 2).replace('<svg', '<svg style="width:24px;height:24px"') : esc(initials(t.name))}</span>
        <div class="body"><div class="line1"><span class="name">${esc(t.name)}</span>
          <span class="time">${t.last ? listTime(t.last.ts) : ''}${icon('chev', 2).replace('<svg', '<svg style="width:14px;height:14px;opacity:.5"')}</span></div>
          <div class="preview">${esc(preview)}</div></div></li>`;
    }).join('')}</ul>` : ''}
    ${list.length <= 1 ? `<div class="ios-empty" style="padding-top:12vh"><b>No updates yet</b>Claude can text you here from anywhere — add <code>/mcp?token=…</code> on your relay as a connector, or run <code>pocket.mjs message</code>.</div>` : ''}`;
}

function renderThread(tid) {
  const t = data.threads.find((x) => x.id === tid);
  if (!t) { setTitle(''); view.innerHTML = '<div class="ios-empty"><b>Conversation not found</b></div>'; return; }
  const ideas = tid === 'ideas';
  setTitle(`<div class="ios-head"><span class="avatar" ${ideas ? 'style="background:linear-gradient(180deg,#ffd60a,#ff9f0a)"' : ''}>${ideas ? icon('bulb', 2).replace('<svg', '<svg style="width:18px;height:18px"') : esc(initials(t.name))}</span>${esc(t.name)}</div>`);
  $('#menuBtn').innerHTML = icon('back', 2.4) + '<span class="dotbadge" id="menuDot" hidden></span>';
  $('#menuBtn').dataset.back = '#/messages';

  const msgs = threadCache[tid] || [];
  const ideaNo = {};
  let n = 0;
  if (ideas) for (const m of msgs) if (m.from === 'me') ideaNo[m.id] = ++n;
  const lastMe = msgs.findLast((m) => m.from === 'me');
  let html = '';
  msgs.forEach((m, i) => {
    const prev = msgs[i - 1], next = msgs[i + 1];
    if (!prev || m.ts - prev.ts > 30 * 60e3) html += `<div class="stamp">${stamp(m.ts)}</div>`;
    else if (prev.from !== m.from) html += '<div class="gap"></div>';
    const side = m.from === 'me' ? 'out' : 'in';
    const lastOfGroup = !next || next.from !== m.from || next.ts - m.ts > 30 * 60e3;
    if (m.text) html += `<div class="bubble ${side} ${lastOfGroup && !m.link ? 'tail' : ''}">${linkify(m.text)}</div>`;
    if (m.link) {
      html += `<a class="linkcard" href="${esc(m.link.url)}" target="_blank" rel="noopener">
        <div class="lc-top">${icon('compass', 1.5)}</div>
        <div class="lc-body"><div class="lc-title">${esc(m.link.title || host(m.link.url))}</div><div class="lc-host">${esc(host(m.link.url))}</div></div></a>`;
    }
    if (ideas && m.from === 'me' && m.status && m.status !== 'new') html += `<div class="delivered">Idea #${ideaNo[m.id]} · ${esc(m.status)}</div>`;
    else if (m === lastMe && !ideas) html += '<div class="delivered">Delivered</div>';
  });
  view.classList.add('has-composer');
  view.innerHTML = msgs.length ? `<div class="thread">${html}</div>`
    : `<div class="ios-empty"><b>${ideas ? 'Your ideas' : esc(t.name)}</b>${ideas
      ? 'Jot down anything — an app, a 3D model, a fix. Claude saves each one as a numbered idea and can pick them up later.'
      : 'No messages yet.'}</div>`;
  dock.innerHTML = `<div class="ios-bar">
    <div class="ios-field"><textarea rows="1" data-draft="msg-${tid}" placeholder="${ideas ? 'New idea' : 'Message'}"></textarea>
      <button class="ios-send" id="iosSend" aria-label="Send" ${drafts[`msg-${tid}`]?.trim() ? '' : 'disabled'}>${icon('up', 2.6)}</button></div></div>`;
  bindDrafts(dock);
  const ta = $(`[data-draft="msg-${tid}"]`);
  ta.addEventListener('input', () => { $('#iosSend').disabled = !ta.value.trim(); ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 120) + 'px'; });
  $('#iosSend').onclick = () => sendMessage(tid);
}

async function sendMessage(tid) {
  const text = (drafts[`msg-${tid}`] || '').trim();
  if (!text) return;
  drafts[`msg-${tid}`] = '';
  const ta = $(`[data-draft="msg-${tid}"]`);
  if (ta) { ta.value = ''; ta.style.height = 'auto'; }
  $('#iosSend').disabled = true;
  (threadCache[tid] ||= []).push({ id: 'tmp', from: 'me', text, ts: Date.now() });
  const keep = document.activeElement === ta;
  renderQueued = false; ta?.blur(); render();
  window.scrollTo(0, document.body.scrollHeight);
  if (keep) $(`[data-draft="msg-${tid}"]`)?.focus();
  try {
    await api('POST', `/api/phone/threads/${tid}/reply`, { text });
  } catch (e) { toast(e.message); drafts[`msg-${tid}`] = text; }
}

// ---- usage

function meter(label, w) {
  if (!w) return '';
  const pct = Math.round(w.used_percentage);
  const cls = pct >= 90 ? 'bad' : pct >= 70 ? 'warn' : '';
  return `<div class="panel"><div class="stat"><h3>${label}</h3><b>${pct}%</b></div>
    <div class="meter ${cls}"><i style="width:${Math.min(100, pct)}%"></i></div>
    <div class="meta">Resets in ${until(w.resets_at)}</div></div>`;
}

function renderUsage() {
  setTitle('Usage');
  const u = data.usage;
  if (!u) {
    view.innerHTML = `<div class="hero"><h2>No usage yet</h2><p>Numbers appear after Claude Code on your Mac makes its first request.</p></div>`;
    return;
  }
  const rl = u.rateLimits || {};
  view.innerHTML = `
    <div class="section-title">Plan limits</div>
    ${meter('Current session', rl.five_hour)}
    ${meter('Weekly', rl.seven_day)}
    ${meter('Spend limit', rl.spend_limit)}
    ${!rl.five_hour && !rl.seven_day ? `<div class="panel meta">Plan limits only show for Pro and Max subscriptions.</div>` : ''}
    ${u.context ? `<div class="section-title">Context</div><div class="panel">
      <div class="stat"><h3>${esc(u.sessionName || sessionName(u.sessionId))}</h3><b>${Math.round(u.context.usedPercentage || 0)}%</b></div>
      <div class="meter"><i style="width:${Math.min(100, u.context.usedPercentage || 0)}%"></i></div>
      <div class="meta">of the context window used</div></div>` : ''}
    <div class="meta" style="text-align:center;margin-top:16px">${esc(u.model || '')} · updated ${ago(u.updatedAt)}</div>`;
}

// ---- send files

function pendingThumbs() {
  if (!pending.length) return '';
  return `<div class="thumbs">${pending.map((p, i) => `
    <div class="thumb">${p.url ? `<img src="${p.url}" alt="">` : esc(p.file.name)}
      <button onclick="removePending(${i})" aria-label="Remove">×</button></div>`).join('')}</div>`;
}

function addFiles(list) {
  for (const f of list) pending.push({ file: f, url: f.type.startsWith('image/') ? URL.createObjectURL(f) : '' });
  document.activeElement?.blur(); renderQueued = false; render();
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
  setTitle('Send to Mac');
  const sessions = data.sessions.filter((s) => s.status !== 'ended');
  view.classList.add('has-composer');
  view.innerHTML = `
    <div class="seg" style="margin-top:8px">
      <button class="${sendMode === 'claude' ? 'on' : ''}" onclick="sendMode='claude';render()">To Claude</button>
      <button class="${sendMode === 'mac' ? 'on' : ''}" onclick="sendMode='mac';render()">Just the file</button>
    </div>
    <p class="meta" style="margin:10px 4px">${sendMode === 'claude'
      ? 'Claude gets the file and your note at its next step, or right away if it is waiting on you. Tip: share screenshots straight from Android\'s share sheet.'
      : 'Saved to Downloads › Claude Pocket on the Mac.'}</p>
    ${sendMode === 'claude' ? `<select id="target">
        <option value="">Whichever session runs next</option>
        ${sessions.map((s) => `<option value="${s.id}" ${s.id === sendTarget ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}
      </select>` : ''}
    <div class="section-title">Recent</div>
    ${data.files.length ? data.files.slice(0, 20).map(fileRow).join('') : '<div class="meta" style="padding:0 4px">Nothing sent yet.</div>'}`;
  dock.innerHTML = composer('send-note', sendMode === 'claude' ? 'Add a note for Claude' : 'Pick files with +',
    pending.length ? `${pending.length} file${pending.length > 1 ? 's' : ''} ready` : 'Attach screenshots or files', 'sendNow()');
  bindDrafts(dock);
  const sel = $('#target'); if (sel) sel.onchange = () => (sendTarget = sel.value);
}

function fileRow(f) {
  const img = f.type?.startsWith('image/') && f.size
    ? `<img src="/api/files/${f.id}?token=${encodeURIComponent(token)}" alt="" loading="lazy">` : '<div class="ph"></div>';
  const status = f.forClaude ? (f.claudeTaken ? 'Claude has it' : 'Waiting for Claude') : (f.macTaken ? 'On your Mac' : 'Waiting for Mac');
  return `<div class="file-row">${img}<div class="grow"><div class="name">${esc(f.size ? f.name : f.note)}</div>
    <div class="meta">${status} · ${f.size ? size(f.size) + ' · ' : ''}${ago(f.createdAt)}</div></div></div>`;
}

async function sendNow() {
  const note = (drafts['send-note'] || '').trim();
  const claude = sendMode === 'claude';
  if (!pending.length && !(claude && note)) return toast('Attach a file or write a note');
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
  document.activeElement?.blur(); render();
}

// ---------------------------------------------------------------- share target

function applyShared(files, text) {
  for (const file of files) pending.push({ file, url: file.type.startsWith('image/') ? URL.createObjectURL(file) : '' });
  if (text) drafts['send-note'] = text;
  sendMode = 'claude';
  const stop = data.requests.find((r) => r.kind === 'stop');
  if (stop) { drafts[`reply-${stop.id}`] = text; go('#/inbox'); } else go('#/send');
}

// The Android app hands over shared files through the PocketNative bridge.
window.pocketReceiveShare = () => {
  if (!NATIVE || !token) return;
  let d; try { d = JSON.parse(PocketNative.takeShared()); } catch { return; }
  const files = d.files.map((f) => {
    const bin = atob(f.b64), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new File([bytes], f.name, { type: f.type });
  });
  if (files.length || d.text) applyShared(files, d.text);
};

// The service worker parks shared files in Cache Storage and opens /?share=1.
async function takeShared() {
  if (!new URLSearchParams(location.search).has('share')) return;
  history.replaceState(null, '', '/#/send');
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
    sendMode = 'claude';
    const stop = data.requests.find((r) => r.kind === 'stop');
    if (stop) { drafts[`reply-${stop.id}`] = text; history.replaceState(null, '', '/#/inbox'); }
  } catch {}
}

// ---------------------------------------------------------------- wiring

$('#menuBtn').onclick = () => {
  const back = $('#menuBtn').dataset.back;
  if (back && route().page === 'm') { history.length > 1 ? history.back() : go(back); return; }
  document.body.classList.add('drawer-open');
};
$('#scrim').onclick = () => document.body.classList.remove('drawer-open');

// The thread view swaps the menu button for a back chevron; restore it elsewhere.
const menuHtml = $('#menuBtn').innerHTML;
const baseRender = render;
render = function () {
  if (route().page !== 'm' && $('#menuBtn').dataset.back) { $('#menuBtn').innerHTML = menuHtml; delete $('#menuBtn').dataset.back; }
  baseRender();
};

$('#away').onclick = async () => {
  const away = !data.settings?.away;
  data.settings.away = away; render();
  try { await api('POST', '/api/phone/settings', { away }); toast(away ? 'Away mode on — prompts come here' : 'Away mode off'); }
  catch (e) { toast(e.message); }
};

if (!NATIVE && 'serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});

(async () => {
  if (!location.hash.startsWith('#/')) history.replaceState(null, '', '/#/' + (store.get('pocket-page') || 'inbox'));
  render();
  if (!token) return;
  await refresh();
  await takeShared();
  window.pocketReceiveShare();
  const r = route();
  if (r.page === 'm') await openThread(r.id); else render();
  connect();
})();
window.addEventListener('hashchange', () => { const p = route().page; if (['inbox', 'messages', 'usage', 'send'].includes(p)) store.set('pocket-page', p); });
setInterval(() => { if (['usage', 'inbox', 'messages'].includes(route().page)) render(); }, 30000);
