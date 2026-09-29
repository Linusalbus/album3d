// Claude Pocket relay: the only piece both the Mac and the phone can reach over
// the internet. It holds session snapshots, pending approvals, usage numbers and
// files in transit, and hands them to whichever side asks. Zero dependencies.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 8787);
const TOKEN = process.env.POCKET_TOKEN || '';
const DATA_DIR = process.env.DATA_DIR || path.join(HERE, 'data');
const NTFY_URL = process.env.NTFY_URL || ''; // e.g. https://ntfy.sh/some-long-random-topic
const PUBLIC_URL = process.env.PUBLIC_URL || '';
const MAX_UPLOAD = 60 * 1024 * 1024;
const FILE_TTL_MS = 3 * 24 * 3600 * 1000;

if (TOKEN.length < 16) {
  console.error('POCKET_TOKEN must be set to a random string of at least 16 characters.');
  process.exit(1);
}

fs.mkdirSync(path.join(DATA_DIR, 'files'), { recursive: true });
const STATE_FILE = path.join(DATA_DIR, 'state.json');

let state = { sessions: {}, requests: {}, usage: null, settings: { away: false }, files: {} };
try { state = { ...state, ...JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')) }; } catch {}

let saveTimer = null;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    fs.writeFile(STATE_FILE + '.tmp', JSON.stringify(state), (err) => {
      if (!err) fs.rename(STATE_FILE + '.tmp', STATE_FILE, () => {});
    });
  }, 400);
}

// ---------------------------------------------------------------- live updates

const streams = new Set();   // phone SSE connections
const waiters = new Map();   // request id -> [resolve fns] for Mac long-polls

function broadcast(kind) {
  const line = `event: update\ndata: ${JSON.stringify({ kind, at: Date.now() })}\n\n`;
  for (const res of streams) res.write(line);
}

function changed(kind) { save(); broadcast(kind); }

async function push(title, body, priority = 'default') {
  if (!NTFY_URL) return;
  try {
    await fetch(NTFY_URL, {
      method: 'POST',
      body: body.slice(0, 900),
      headers: {
        Title: title.replace(/[^\x20-\x7e]/g, '').slice(0, 120) || 'Claude',
        Priority: priority,
        Tags: 'robot',
        ...(PUBLIC_URL ? { Click: PUBLIC_URL } : {}),
      },
      signal: AbortSignal.timeout(5000),
    });
  } catch (e) { console.warn('ntfy push failed:', e.message); }
}

// ---------------------------------------------------------------- helpers

const id = () => crypto.randomBytes(9).toString('base64url');

function authed(req, url) {
  const header = req.headers.authorization || '';
  const given = header.startsWith('Bearer ') ? header.slice(7) : url.searchParams.get('token') || '';
  const a = Buffer.from(given), b = Buffer.from(TOKEN);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function send(res, code, body, headers = {}) {
  const isJson = typeof body !== 'string' && !Buffer.isBuffer(body);
  res.writeHead(code, {
    'Content-Type': isJson ? 'application/json' : 'text/plain; charset=utf-8',
    'Cache-Control': 'no-store',
    ...headers,
  });
  res.end(isJson ? JSON.stringify(body) : body);
}

function readBody(req, limit = 2 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    const chunks = []; let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > limit) { reject(Object.assign(new Error('Too large'), { code: 413 })); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

async function readJson(req) {
  const buf = await readBody(req);
  try { return buf.length ? JSON.parse(buf.toString('utf8')) : {}; }
  catch { throw Object.assign(new Error('Bad JSON'), { code: 400 }); }
}

function publicFile(f) {
  const { diskName, ...rest } = f;
  return rest;
}

function openRequests() {
  return Object.values(state.requests).filter((r) => !r.answer && !r.cancelled);
}

function snapshot() {
  return {
    sessions: Object.values(state.sessions).sort((a, b) => b.updatedAt - a.updatedAt),
    requests: openRequests().sort((a, b) => a.createdAt - b.createdAt),
    usage: state.usage,
    settings: state.settings,
    files: Object.values(state.files).map(publicFile).sort((a, b) => b.createdAt - a.createdAt),
    now: Date.now(),
  };
}

function resolveRequest(reqId, patch) {
  const r = state.requests[reqId];
  if (!r) return false;
  Object.assign(r, patch, { closedAt: Date.now() });
  for (const fn of waiters.get(reqId) || []) fn();
  waiters.delete(reqId);
  changed('requests');
  return true;
}

function prune() {
  const now = Date.now();
  for (const [k, r] of Object.entries(state.requests)) {
    if (r.closedAt && now - r.closedAt > 3600e3) delete state.requests[k];
    else if (!r.closedAt && now - r.createdAt > 6 * 3600e3) delete state.requests[k];
  }
  for (const [k, f] of Object.entries(state.files)) {
    if (now - f.createdAt > FILE_TTL_MS) {
      fs.rm(path.join(DATA_DIR, 'files', f.diskName), () => {});
      delete state.files[k];
    }
  }
  for (const [k, s] of Object.entries(state.sessions)) {
    if (now - s.updatedAt > 7 * 24 * 3600e3) delete state.sessions[k];
  }
  save();
}
setInterval(prune, 10 * 60e3).unref();

// ---------------------------------------------------------------- static files

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.webmanifest': 'application/manifest+json',
  '.png': 'image/png', '.svg': 'image/svg+xml', '.json': 'application/json',
};

function serveStatic(res, pathname) {
  const rel = pathname === '/' ? 'index.html' : pathname.slice(1);
  const file = path.normalize(path.join(HERE, 'public', rel));
  if (!file.startsWith(path.join(HERE, 'public'))) return send(res, 404, 'Not found');
  fs.readFile(file, (err, buf) => {
    if (err) return send(res, 404, 'Not found');
    res.writeHead(200, {
      'Content-Type': MIME[path.extname(file)] || 'application/octet-stream',
      'Cache-Control': rel === 'sw.js' || rel === 'index.html' ? 'no-cache' : 'public, max-age=300',
    });
    res.end(buf);
  });
}

// ---------------------------------------------------------------- routes

async function handle(req, res) {
  const url = new URL(req.url, 'http://x');
  const p = url.pathname;
  const m = req.method;

  if (!p.startsWith('/api/')) {
    if (m === 'GET') return serveStatic(res, p);
    return send(res, 405, 'Method not allowed');
  }
  if (p === '/api/health') return send(res, 200, { ok: true });
  if (!authed(req, url)) return send(res, 401, { error: 'Bad token' });

  // ---- shared
  if (m === 'GET' && p === '/api/state') return send(res, 200, snapshot());

  if (m === 'GET' && p === '/api/events') {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store',
      Connection: 'keep-alive', 'X-Accel-Buffering': 'no',
    });
    res.write('event: hello\ndata: {}\n\n');
    streams.add(res);
    const ping = setInterval(() => res.write(': ping\n\n'), 20000);
    req.on('close', () => { clearInterval(ping); streams.delete(res); });
    return;
  }

  let fm = p.match(/^\/api\/files\/([\w-]+)$/);
  if (fm && m === 'GET') {
    const f = state.files[fm[1]];
    if (!f) return send(res, 404, { error: 'No such file' });
    const stream = fs.createReadStream(path.join(DATA_DIR, 'files', f.diskName));
    res.writeHead(200, {
      'Content-Type': f.type || 'application/octet-stream',
      'Content-Length': f.size,
      'Content-Disposition': `inline; filename="${encodeURIComponent(f.name)}"`,
      'Cache-Control': 'private, max-age=3600',
    });
    return stream.pipe(res);
  }
  if (fm && m === 'DELETE') {
    const f = state.files[fm[1]];
    if (f) { fs.rm(path.join(DATA_DIR, 'files', f.diskName), () => {}); delete state.files[fm[1]]; changed('files'); }
    return send(res, 200, { ok: true });
  }

  // ---- Mac side
  if (m === 'POST' && p === '/api/mac/session') {
    const s = await readJson(req);
    if (!s.id) return send(res, 400, { error: 'id required' });
    const prev = state.sessions[s.id] || {};
    state.sessions[s.id] = {
      ...prev,
      id: s.id,
      name: s.name || prev.name || path.basename(s.cwd || '') || 'Session',
      cwd: s.cwd ?? prev.cwd,
      status: s.status ?? prev.status ?? 'idle',
      messages: Array.isArray(s.messages) ? s.messages.slice(-120) : prev.messages || [],
      updatedAt: Date.now(),
    };
    changed('sessions');
    return send(res, 200, { ok: true });
  }

  if (m === 'POST' && p === '/api/mac/usage') {
    const u = await readJson(req);
    state.usage = { ...u, updatedAt: Date.now() };
    changed('usage');
    return send(res, 200, { ok: true });
  }

  if (m === 'POST' && p === '/api/mac/notify') {
    const n = await readJson(req);
    push(n.title || 'Claude', n.body || '', n.priority || 'default');
    return send(res, 200, { ok: true });
  }

  if (m === 'POST' && p === '/api/mac/request') {
    const r = await readJson(req);
    if (!['permission', 'question', 'stop'].includes(r.kind)) return send(res, 400, { error: 'bad kind' });
    const rid = id();
    state.requests[rid] = {
      id: rid, kind: r.kind, sessionId: r.sessionId, sessionName: r.sessionName,
      payload: r.payload || {}, createdAt: Date.now(), answer: null,
    };
    changed('requests');
    const titles = { permission: 'Claude needs permission', question: 'Claude has a question', stop: 'Claude is done — reply?' };
    push(titles[r.kind], r.summary || r.sessionName || '', r.kind === 'stop' ? 'default' : 'high');
    return send(res, 200, { id: rid });
  }

  let rm = p.match(/^\/api\/mac\/request\/([\w-]+)\/wait$/);
  if (rm && m === 'GET') {
    const rid = rm[1];
    const timeout = Math.min(Number(url.searchParams.get('timeout') || 25), 50) * 1000;
    const done = () => {
      const r = state.requests[rid];
      if (!r) return send(res, 404, { error: 'gone' });
      send(res, 200, { answer: r.answer, cancelled: !!r.cancelled });
    };
    const r = state.requests[rid];
    if (!r || r.answer || r.cancelled) return done();
    let finished = false;
    const fn = () => { if (!finished) { finished = true; clearTimeout(t); done(); } };
    const t = setTimeout(fn, timeout);
    waiters.set(rid, [...(waiters.get(rid) || []), fn]);
    req.on('close', () => { finished = true; clearTimeout(t); });
    return;
  }

  rm = p.match(/^\/api\/mac\/request\/([\w-]+)\/cancel$/);
  if (rm && m === 'POST') {
    resolveRequest(rm[1], { cancelled: true });
    return send(res, 200, { ok: true });
  }

  if (m === 'GET' && p === '/api/mac/inbox') {
    const session = url.searchParams.get('session') || '';
    const forClaude = url.searchParams.get('claude') === '1';
    const list = Object.values(state.files).filter((f) => {
      if (forClaude) return f.forClaude && !f.claudeTaken && (!f.target || f.target === session);
      return !f.macTaken;
    });
    return send(res, 200, { files: list.map(publicFile) });
  }

  rm = p.match(/^\/api\/mac\/inbox\/([\w-]+)\/ack$/);
  if (rm && m === 'POST') {
    const f = state.files[rm[1]];
    const body = await readJson(req);
    if (f) {
      if (body.claude) f.claudeTaken = Date.now();
      else f.macTaken = Date.now();
      changed('files');
    }
    return send(res, 200, { ok: true });
  }

  // ---- phone side
  rm = p.match(/^\/api\/phone\/answer\/([\w-]+)$/);
  if (rm && m === 'POST') {
    const body = await readJson(req);
    if (!body.answer) return send(res, 400, { error: 'answer required' });
    return send(res, resolveRequest(rm[1], { answer: body.answer }) ? 200 : 404, { ok: true });
  }

  if (m === 'POST' && p === '/api/phone/settings') {
    const body = await readJson(req);
    if (typeof body.away === 'boolean') state.settings.away = body.away;
    changed('settings');
    return send(res, 200, state.settings);
  }

  if (m === 'POST' && p === '/api/phone/upload') {
    const buf = await readBody(req, MAX_UPLOAD);
    const fid = id();
    const name = (url.searchParams.get('name') || 'file').replace(/[\/\\\0]/g, '_').slice(0, 120);
    const diskName = fid + path.extname(name).replace(/[^\w.]/g, '').slice(0, 10);
    await fs.promises.writeFile(path.join(DATA_DIR, 'files', diskName), buf);
    state.files[fid] = {
      id: fid, name, diskName, size: buf.length,
      type: (req.headers['content-type'] || 'application/octet-stream').split(';')[0],
      note: (url.searchParams.get('note') || '').slice(0, 4000),
      forClaude: url.searchParams.get('claude') === '1',
      target: url.searchParams.get('target') || '',
      createdAt: Date.now(),
    };
    changed('files');
    return send(res, 200, publicFile(state.files[fid]));
  }

  return send(res, 404, { error: 'Unknown route' });
}

http.createServer((req, res) => {
  handle(req, res).catch((e) => {
    if (!res.headersSent) send(res, e.code || 500, { error: e.message });
    else res.end();
  });
}).listen(PORT, () => console.log(`Claude Pocket relay on :${PORT}`));
