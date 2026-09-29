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

let state = { sessions: {}, requests: {}, usage: null, settings: { away: false }, files: {}, threads: {}, messages: [] };
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
    threads: threadList(),
    now: Date.now(),
  };
}

// ---------------------------------------------------------------- messages
// A lightweight "Claude texts me" channel, separate from Claude Code sessions:
// anything with the token (a routine, a script, the MCP connector) can post an
// update into a named thread, and the phone can reply.

const MAX_MESSAGES = 3000;

function slug(name) {
  return String(name || 'Claude').toLowerCase().normalize('NFKD').replace(/[^\w]+/g, '-').replace(/^-|-$/g, '').slice(0, 60) || 'claude';
}

const IDEAS = 'ideas';
state.threads[IDEAS] ||= { id: IDEAS, name: 'Ideas', unread: 0, updatedAt: 0 };
state.threads[IDEAS].pinned = true;

function ideaList() {
  return state.messages.filter((m) => m.thread === IDEAS && m.from === 'me').map((m, i) => ({ ...m, n: i + 1 }));
}

function threadList() {
  return Object.values(state.threads).map((t) => {
    const last = state.messages.findLast((m) => m.thread === t.id);
    return { ...t, last: last ? { text: last.text || last.link?.title || last.link?.url || '', from: last.from, ts: last.ts } : null };
  }).sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0) || b.updatedAt - a.updatedAt);
}

function addMessage({ thread, from, text, link, silent }) {
  const name = String(thread || 'Claude').slice(0, 80);
  const tid = slug(name);
  const t = state.threads[tid] ||= { id: tid, name, unread: 0, updatedAt: 0 };
  const msg = {
    id: id(), thread: tid, from,
    text: String(text || '').slice(0, 8000),
    link: link?.url && /^https?:\/\//i.test(link.url) ? { url: String(link.url).slice(0, 2000), title: String(link.title || '').slice(0, 200) } : null,
    ts: Date.now(),
  };
  if (!msg.text && !msg.link) throw Object.assign(new Error('text or link required'), { code: 400 });
  state.messages.push(msg);
  // Trim old updates, but never the user's ideas — their numbers must stay stable.
  if (state.messages.length > MAX_MESSAGES) {
    let drop = state.messages.length - MAX_MESSAGES;
    state.messages = state.messages.filter((x) => x.thread === IDEAS || drop-- <= 0);
  }
  t.updatedAt = msg.ts;
  if (from === 'claude' && !silent) {
    t.unread += 1;
    push(t.name, msg.text || msg.link.title || msg.link.url);
  }
  if (tid === IDEAS && from === 'me') {
    msg.status = 'new';
    const n = ideaList().length;
    state.messages.push({ id: id(), thread: IDEAS, from: 'claude', text: `Saved as idea #${n} ✓`, ts: Date.now() + 1, ack: true });
  }
  changed('messages');
  return msg;
}

function readMessages({ thread, since, from }) {
  return state.messages.filter((m) =>
    (!thread || m.thread === slug(thread)) && (!since || m.ts > Number(since)) && (!from || m.from === from));
}

// ---------------------------------------------------------------- MCP connector
// Minimal Streamable-HTTP MCP server (stateless, JSON responses) so Claude can
// text you from claude.ai, routines or Claude Code: add <relay>/mcp?token=… as a connector.

const MCP_TOOLS = [
  {
    name: 'send_message',
    description: 'Send a short update to the user\'s phone (shown like a text message in Claude Pocket, with a push notification). Use one thread per topic, e.g. "Index01 shipment", so related updates stay together. Include a link when there is something to open, such as a parcel tracking page.',
    inputSchema: {
      type: 'object',
      properties: {
        thread: { type: 'string', description: 'Conversation name, e.g. "Index01 shipment". Reuse the same name for follow-up updates.' },
        text: { type: 'string', description: 'The message. Plain text, keep it short like an SMS.' },
        link_url: { type: 'string', description: 'Optional URL shown as a tappable preview.' },
        link_title: { type: 'string', description: 'Optional title for the link preview.' },
      },
      required: ['thread', 'text'],
    },
  },
  {
    name: 'read_replies',
    description: 'Read what the user replied from their phone. Returns the user\'s messages, newest last.',
    inputSchema: {
      type: 'object',
      properties: {
        thread: { type: 'string', description: 'Only this thread (optional).' },
        since: { type: 'number', description: 'Only replies after this Unix time in milliseconds (optional).' },
      },
    },
  },
  {
    name: 'list_ideas',
    description: 'List the ideas the user has jotted down in the Ideas thread on their phone, with number and status (new / doing / done). Check this when the user asks what to work on or refers to "my ideas".',
    inputSchema: {
      type: 'object',
      properties: { status: { type: 'string', enum: ['new', 'doing', 'done', 'all'], description: 'Filter, default all.' } },
    },
  },
  {
    name: 'update_idea',
    description: 'Change an idea\'s status and optionally reply to it in the Ideas thread (e.g. "Started — see branch x").',
    inputSchema: {
      type: 'object',
      properties: {
        number: { type: 'number', description: 'Idea number from list_ideas.' },
        status: { type: 'string', enum: ['new', 'doing', 'done'] },
        reply: { type: 'string', description: 'Optional message shown to the user in the Ideas thread.' },
      },
      required: ['number'],
    },
  },
  {
    name: 'save_idea',
    description: 'Save an idea to the user\'s Ideas list on their behalf (only when they ask you to note something down for later).',
    inputSchema: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] },
  },
  {
    name: 'list_threads',
    description: 'List message threads with their latest message.',
    inputSchema: { type: 'object', properties: {} },
  },
];

function mcpCall(name, args = {}) {
  if (name === 'send_message') {
    const m = addMessage({ thread: args.thread, from: 'claude', text: args.text, link: args.link_url ? { url: args.link_url, title: args.link_title } : null });
    return `Sent to the user's phone in "${state.threads[m.thread].name}".`;
  }
  if (name === 'read_replies') {
    const list = readMessages({ thread: args.thread, since: args.since, from: 'me' })
      .filter((m) => args.thread || m.thread !== IDEAS).slice(-50);
    if (!list.length) return 'No replies.';
    return list.map((m) => `[${new Date(m.ts).toISOString()}] (${state.threads[m.thread]?.name}) ${m.text}`).join('\n');
  }
  if (name === 'list_ideas') {
    const list = ideaList().filter((i) => !args.status || args.status === 'all' || i.status === args.status);
    if (!list.length) return 'No ideas saved.';
    return list.map((i) => `#${i.n} [${i.status || 'new'}] ${new Date(i.ts).toISOString().slice(0, 10)} — ${i.text}`).join('\n');
  }
  if (name === 'update_idea') {
    const idea = ideaList().find((i) => i.n === Number(args.number));
    if (!idea) throw new Error(`No idea #${args.number}`);
    const stored = state.messages.find((m) => m.id === idea.id);
    if (args.status) stored.status = args.status;
    if (args.reply) addMessage({ thread: 'Ideas', from: 'claude', text: `Re #${idea.n}: ${args.reply}` });
    else changed('messages');
    return `Idea #${idea.n} is now ${stored.status}.`;
  }
  if (name === 'save_idea') {
    addMessage({ thread: 'Ideas', from: 'me', text: args.text });
    return `Saved as idea #${ideaList().length}.`;
  }
  if (name === 'list_threads') {
    const list = threadList();
    if (!list.length) return 'No threads yet.';
    return list.map((t) => `${t.name} — last: ${t.last?.from === 'me' ? 'user' : 'Claude'}: ${t.last?.text?.slice(0, 120) || ''}`).join('\n');
  }
  throw Object.assign(new Error(`Unknown tool ${name}`), { rpc: -32602 });
}

async function handleMcp(req, res) {
  if (req.method !== 'POST') return send(res, 405, { error: 'POST only' }, { Allow: 'POST' });
  const body = await readJson(req);
  const batch = Array.isArray(body) ? body : [body];
  const replies = [];
  for (const msg of batch) {
    if (msg.id === undefined || msg.id === null) continue; // notification
    const reply = { jsonrpc: '2.0', id: msg.id };
    try {
      if (msg.method === 'initialize') {
        reply.result = {
          protocolVersion: msg.params?.protocolVersion || '2025-06-18',
          capabilities: { tools: {} },
          serverInfo: { name: 'claude-pocket', version: '1.0.0' },
          instructions: [
            'This is the user\'s phone app (Claude Pocket). It is always available; use it without being asked to explain it.',
            'Text the user with send_message when they ask to be notified, reminded or kept updated, when a long task they asked about finishes, and for delivery/shipment updates (always pass the tracking link as link_url). Keep messages short like an SMS, one thread per topic (e.g. "Index01 shipment"), and reuse the thread name for follow-ups.',
            'When the user mentions an idea for later, save it with save_idea. When they ask what to work on or mention their ideas, check list_ideas and use update_idea to mark progress.',
          ].join(' '),
        };
      } else if (msg.method === 'tools/list') {
        reply.result = { tools: MCP_TOOLS };
      } else if (msg.method === 'tools/call') {
        try {
          reply.result = { content: [{ type: 'text', text: mcpCall(msg.params?.name, msg.params?.arguments) }] };
        } catch (e) {
          if (e.rpc) throw e;
          reply.result = { content: [{ type: 'text', text: e.message }], isError: true };
        }
      } else if (msg.method === 'ping') {
        reply.result = {};
      } else {
        reply.error = { code: -32601, message: `Method not found: ${msg.method}` };
      }
    } catch (e) {
      reply.error = { code: e.rpc || -32603, message: e.message };
    }
    replies.push(reply);
  }
  if (!replies.length) { res.writeHead(202); return res.end(); }
  return send(res, 200, Array.isArray(body) ? replies : replies[0]);
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

  if (p === '/mcp') {
    if (!authed(req, url)) return send(res, 401, { error: 'Bad token' });
    return handleMcp(req, res);
  }
  if (!p.startsWith('/api/')) {
    if (m === 'GET') return serveStatic(res, p);
    return send(res, 405, 'Method not allowed');
  }
  if (p === '/api/health') return send(res, 200, { ok: true });
  if (!authed(req, url)) return send(res, 401, { error: 'Bad token' });
  if (p === '/api/mcp' || p === '/mcp') return handleMcp(req, res);

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

  // ---- messages (anyone with the token: routines, scripts, the Mac CLI)
  if (m === 'POST' && p === '/api/messages') {
    const b = await readJson(req);
    const msg = addMessage({
      thread: b.thread || b.from, from: 'claude', text: b.text,
      link: b.url || b.link_url ? { url: b.url || b.link_url, title: b.urlTitle || b.link_title } : b.link,
    });
    return send(res, 200, msg);
  }
  if (m === 'GET' && p === '/api/messages') {
    return send(res, 200, { messages: readMessages({
      thread: url.searchParams.get('thread'), since: url.searchParams.get('since'), from: url.searchParams.get('from'),
    }).slice(-500) });
  }

  if (m === 'GET' && p === '/api/ideas') return send(res, 200, { ideas: ideaList() });

  // ---- phone side
  let tm = p.match(/^\/api\/phone\/threads\/([\w-]+)\/(read|reply)$/);
  if (tm && m === 'POST') {
    const t = state.threads[tm[1]];
    if (!t) return send(res, 404, { error: 'No such thread' });
    if (tm[2] === 'read') { t.unread = 0; changed('messages'); return send(res, 200, { ok: true }); }
    const b = await readJson(req);
    return send(res, 200, addMessage({ thread: t.name, from: 'me', text: b.text }));
  }
  tm = p.match(/^\/api\/phone\/threads\/([\w-]+)$/);
  if (tm && m === 'DELETE') {
    if (tm[1] === IDEAS) return send(res, 400, { error: 'The Ideas thread cannot be deleted' });
    delete state.threads[tm[1]];
    state.messages = state.messages.filter((x) => x.thread !== tm[1]);
    changed('messages');
    return send(res, 200, { ok: true });
  }

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
