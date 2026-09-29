#!/usr/bin/env node
// Claude Pocket bridge for the Mac. Claude Code runs this as a hook and as the
// status line; it mirrors sessions to the relay and, in away mode, waits for the
// phone to approve tools, answer questions or reply when Claude finishes.
//
//   node pocket.mjs install --relay https://… --token …   one-time setup
//   node pocket.mjs hook                                   (called by Claude Code)
//   node pocket.mjs statusline                             (called by Claude Code)
//   node pocket.mjs inbox                                  download files sent from the phone
//   node pocket.mjs watch                                  keep downloading files as they arrive
//   node pocket.mjs message "text" [--thread T] [--url U]  text the phone (Messages tab)
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFile, spawn } from 'node:child_process';

const HOME = os.homedir();
const CONF_DIR = path.join(HOME, '.claude-pocket');
const CONF_FILE = path.join(CONF_DIR, 'config.json');
const SETTINGS_FILE = path.join(HOME, '.claude', 'settings.json');
const SELF = path.resolve(process.argv[1]);

function loadConfig() {
  try { return JSON.parse(fs.readFileSync(CONF_FILE, 'utf8')); } catch { return null; }
}
const conf = loadConfig();
const INBOX = conf?.inbox || path.join(HOME, 'Downloads', 'Claude Pocket');

// ---------------------------------------------------------------- relay client

async function api(method, p, body, timeoutMs = 4000) {
  const res = await fetch(conf.relay.replace(/\/$/, '') + p, {
    method,
    headers: { Authorization: `Bearer ${conf.token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!res.ok) throw new Error(`${method} ${p} → ${res.status}`);
  return res.json();
}

async function quiet(fn) { try { return await fn(); } catch { return null; } }

function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (c) => (data += c));
    process.stdin.on('end', () => { try { resolve(JSON.parse(data || '{}')); } catch { resolve({}); } });
  });
}

function out(obj) { process.stdout.write(JSON.stringify(obj)); }

// ---------------------------------------------------------------- transcript → chat

function tailLines(file, maxBytes = 768 * 1024) {
  try {
    const fd = fs.openSync(file, 'r');
    const size = fs.fstatSync(fd).size;
    const start = Math.max(0, size - maxBytes);
    const buf = Buffer.alloc(size - start);
    fs.readSync(fd, buf, 0, buf.length, start);
    fs.closeSync(fd);
    const lines = buf.toString('utf8').split('\n');
    if (start > 0) lines.shift(); // first line is probably cut in half
    return lines.filter(Boolean);
  } catch { return []; }
}

function describeTool(name, input = {}) {
  const detail = input.command || input.file_path || input.pattern || input.url || input.description || input.prompt || '';
  return `${name}${detail ? ': ' + String(detail).split('\n')[0].slice(0, 140) : ''}`;
}

function parseTranscript(file) {
  const messages = [];
  let title = '';
  for (const line of tailLines(file)) {
    let e; try { e = JSON.parse(line); } catch { continue; }
    if (e.type === 'summary' && e.summary) title = e.summary;
    if (e.type === 'custom-title' && e.customTitle) title = e.customTitle;
    if ((e.type !== 'user' && e.type !== 'assistant') || e.isMeta || e.isSidechain) continue;
    const ts = Date.parse(e.timestamp) || Date.now();
    const content = e.message?.content;
    if (typeof content === 'string') {
      if (!content.startsWith('<')) messages.push({ role: e.type, text: content, ts });
      continue;
    }
    for (const part of content || []) {
      if (part.type === 'text' && part.text?.trim() && !part.text.startsWith('<')) {
        messages.push({ role: e.type, text: part.text, ts });
      } else if (part.type === 'tool_use') {
        messages.push({ role: 'tool', text: describeTool(part.name, part.input), ts });
      } else if (part.type === 'image' && e.type === 'user') {
        messages.push({ role: 'user', text: '[image]', ts });
      }
    }
  }
  return { title, messages: messages.slice(-120).map((mm) => ({ ...mm, text: mm.text.slice(0, 6000) })) };
}

async function syncSession(input, status) {
  const { title, messages } = input.transcript_path ? parseTranscript(input.transcript_path) : { messages: [] };
  const name = title || path.basename(input.cwd || '') || 'Session';
  await quiet(() => api('POST', '/api/mac/session', { id: input.session_id, cwd: input.cwd, name, status, messages }));
  return name;
}

// ---------------------------------------------------------------- files from the phone

async function download(file) {
  fs.mkdirSync(INBOX, { recursive: true });
  if (!file.size) return null; // text-only message
  let target = path.join(INBOX, file.name);
  if (fs.existsSync(target)) {
    const ext = path.extname(file.name);
    target = path.join(INBOX, `${path.basename(file.name, ext)}-${file.id.slice(0, 5)}${ext}`);
  }
  const res = await fetch(`${conf.relay.replace(/\/$/, '')}/api/files/${file.id}`, {
    headers: { Authorization: `Bearer ${conf.token}` }, signal: AbortSignal.timeout(60000),
  });
  if (!res.ok) throw new Error(`download ${file.name} → ${res.status}`);
  fs.writeFileSync(target, Buffer.from(await res.arrayBuffer()));
  return target;
}

// Fetch everything the phone marked "for Claude" and turn it into text Claude can act on.
async function takeClaudeInbox(sessionId) {
  const data = await quiet(() => api('GET', `/api/mac/inbox?claude=1&session=${encodeURIComponent(sessionId)}`));
  if (!data?.files?.length) return '';
  const parts = [];
  for (const f of data.files.sort((a, b) => a.createdAt - b.createdAt)) {
    const saved = await quiet(() => download(f));
    await quiet(() => api('POST', `/api/mac/inbox/${f.id}/ack`, { claude: true }));
    if (saved) parts.push(`- ${saved}${f.note ? ` — note: ${f.note}` : ''}`);
    else if (f.note) parts.push(`- Message: ${f.note}`);
  }
  if (!parts.length) return '';
  return 'The user just sent this from their phone (Claude Pocket). Image files are screenshots; open them with the Read tool and take them into account:\n' + parts.join('\n');
}

// ---------------------------------------------------------------- waiting on the phone

async function isAway() {
  const s = await quiet(() => api('GET', '/api/state', null, 3000));
  return !!s?.settings?.away;
}

// Posts a request and blocks until the phone answers, the request is cancelled,
// or `maxMs` passes. Returns the answer or null.
async function askPhone(kind, input, sessionName, payload, summary, maxMs) {
  const created = await quiet(() => api('POST', '/api/mac/request', {
    kind, sessionId: input.session_id, sessionName, payload, summary,
  }));
  if (!created) return null;
  const end = Date.now() + maxMs;
  const cancel = () => quiet(() => api('POST', `/api/mac/request/${created.id}/cancel`));
  process.on('SIGTERM', async () => { await cancel(); process.exit(0); });
  while (Date.now() < end) {
    const r = await quiet(() => api('GET', `/api/mac/request/${created.id}/wait?timeout=25`, null, 35000));
    if (r?.answer) return r.answer;
    if (r?.cancelled) return null;
    if (!r) await new Promise((ok) => setTimeout(ok, 3000)); // relay unreachable; retry
  }
  await cancel();
  return null;
}

// ---------------------------------------------------------------- hook entry

async function hook() {
  const input = await readStdin();
  if (!conf || !input.session_id) return;
  const ev = input.hook_event_name;
  const waitMs = (conf.waitMinutes ?? 30) * 60e3;

  if (ev === 'SessionStart' || ev === 'SessionEnd') {
    await syncSession(input, ev === 'SessionEnd' ? 'ended' : 'idle');
    return;
  }

  if (ev === 'UserPromptSubmit') {
    await syncSession(input, 'working');
    const extra = await takeClaudeInbox(input.session_id);
    if (extra) out({ hookSpecificOutput: { hookEventName: ev, additionalContext: extra } });
    return;
  }

  if (ev === 'PostToolUse') {
    const [, extra] = await Promise.all([syncSession(input, 'working'), takeClaudeInbox(input.session_id)]);
    if (extra) out({ hookSpecificOutput: { hookEventName: ev, additionalContext: extra } });
    return;
  }

  if (ev === 'Notification') {
    const name = await syncSession(input, 'waiting');
    // Permission prompts get their own push from the PermissionRequest flow.
    if (input.notification_type !== 'permission_prompt') {
      await quiet(() => api('POST', '/api/mac/notify', { title: name, body: input.message || 'Claude needs you' }));
    }
    return;
  }

  if (ev === 'PermissionRequest') {
    const name = await syncSession(input, 'waiting');
    if (!(await isAway())) {
      await quiet(() => api('POST', '/api/mac/notify', {
        title: `${name}: permission needed`, body: describeTool(input.tool_name, input.tool_input), priority: 'high',
      }));
      return; // fall through to the normal dialog on the Mac
    }
    const answer = await askPhone('permission', input, name,
      { tool: input.tool_name, input: input.tool_input, suggestions: input.permission_suggestions || [] },
      describeTool(input.tool_name, input.tool_input), waitMs);
    if (!answer) return;
    const decision = answer.allow
      ? { behavior: 'allow', ...(answer.always && input.permission_suggestions?.length ? { updatedPermissions: input.permission_suggestions } : {}) }
      : { behavior: 'deny', message: answer.message || 'Denied from phone.' };
    out({ hookSpecificOutput: { hookEventName: ev, decision } });
    return;
  }

  if (ev === 'PreToolUse' && input.tool_name === 'AskUserQuestion') {
    const name = await syncSession(input, 'waiting');
    if (!(await isAway())) {
      await quiet(() => api('POST', '/api/mac/notify', { title: `${name}: question`, body: input.tool_input?.questions?.[0]?.question || '' }));
      return;
    }
    const questions = input.tool_input?.questions || [];
    const answer = await askPhone('question', input, name, { questions }, questions[0]?.question || 'Question', waitMs);
    if (!answer?.answers) return;
    out({ hookSpecificOutput: {
      hookEventName: ev,
      permissionDecision: 'allow',
      updatedInput: { ...input.tool_input, answers: answer.answers },
    } });
    return;
  }

  if (ev === 'Stop') {
    const name = await syncSession(input, 'idle');
    if (!(await isAway())) {
      await quiet(() => api('POST', '/api/mac/notify', { title: `${name}: done`, body: (input.last_assistant_message || '').slice(0, 400) }));
      return;
    }
    const answer = await askPhone('stop', input, name,
      { last: (input.last_assistant_message || '').slice(0, 8000) },
      (input.last_assistant_message || 'Finished').slice(0, 300), waitMs);
    if (!answer || answer.done) return;
    const extra = await takeClaudeInbox(input.session_id);
    const reason = [answer.text ? `Message from the user (sent from their phone): ${answer.text}` : '', extra].filter(Boolean).join('\n\n');
    if (reason) {
      await quiet(() => api('POST', '/api/mac/session', { id: input.session_id, status: 'working' }));
      out({ decision: 'block', reason });
    }
  }
}

// ---------------------------------------------------------------- status line

async function statusline() {
  const input = await readStdin();
  let line = '';
  if (conf?.previousStatusLine) {
    line = await new Promise((resolve) => {
      const child = spawn('/bin/sh', ['-c', conf.previousStatusLine], { stdio: ['pipe', 'pipe', 'ignore'] });
      let buf = '';
      child.stdout.on('data', (c) => (buf += c));
      child.on('close', () => resolve(buf.replace(/\n+$/, '')));
      child.stdin.end(JSON.stringify(input));
    });
  } else {
    const rl = input.rate_limits || {};
    const bits = [input.model?.display_name];
    if (input.context_window?.used_percentage != null) bits.push(`ctx ${Math.round(input.context_window.used_percentage)}%`);
    if (rl.five_hour) bits.push(`5h ${Math.round(rl.five_hour.used_percentage)}%`);
    if (rl.seven_day) bits.push(`7d ${Math.round(rl.seven_day.used_percentage)}%`);
    line = bits.filter(Boolean).join(' · ');
  }
  process.stdout.write(line + '\n');

  // Throttle uploads: the status line re-renders constantly.
  if (!conf) return;
  const stamp = path.join(CONF_DIR, '.usage-sent');
  try { if (Date.now() - fs.statSync(stamp).mtimeMs < 15000) return; } catch {}
  fs.writeFileSync(stamp, '');
  await quiet(() => api('POST', '/api/mac/usage', {
    rateLimits: input.rate_limits || null,
    context: input.context_window ? {
      usedPercentage: input.context_window.used_percentage,
      size: input.context_window.context_window_size,
    } : null,
    model: input.model?.display_name,
    cost: input.cost?.total_cost_usd,
    sessionId: input.session_id,
    sessionName: input.session_name,
  }, 2000));
}

// ---------------------------------------------------------------- inbox / watch

async function pullInbox(verbose) {
  const data = await api('GET', '/api/mac/inbox');
  for (const f of data.files) {
    const saved = await download(f);
    await api('POST', `/api/mac/inbox/${f.id}/ack`, {});
    if (saved) {
      if (verbose) console.log(saved);
      if (process.platform === 'darwin') {
        execFile('osascript', ['-e', `display notification ${JSON.stringify(f.name)} with title "Claude Pocket" subtitle "Received from phone"`]);
      }
    } else if (f.note && verbose) console.log(`Message: ${f.note}`);
  }
  return data.files.length;
}

async function watch() {
  console.log(`Watching for files from the phone → ${INBOX}`);
  for (;;) {
    try { await pullInbox(true); } catch (e) { console.warn(e.message); }
    await new Promise((ok) => setTimeout(ok, 4000));
  }
}

// ---------------------------------------------------------------- message

async function message() {
  const flags = new Set(['--thread', '--url', '--title']);
  const words = process.argv.slice(3).filter((w, i, all) => !flags.has(w) && !flags.has(all[i - 1]));
  const text = words.join(' ').trim();
  if (!text) { console.error('Usage: node pocket.mjs message "text" [--thread "Index01 shipment"] [--url https://…] [--title …]'); process.exit(1); }
  await api('POST', '/api/messages', { thread: arg('thread') || 'Claude', text, url: arg('url'), urlTitle: arg('title') });
  console.log('Sent.');
}

// ---------------------------------------------------------------- install

function arg(name) {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : undefined;
}

async function install() {
  const relay = arg('relay') || conf?.relay;
  const token = arg('token') || conf?.token;
  if (!relay || !token) {
    console.error('Usage: node pocket.mjs install --relay https://your-relay.example --token YOUR_TOKEN');
    process.exit(1);
  }
  const res = await fetch(relay.replace(/\/$/, '') + '/api/state', { headers: { Authorization: `Bearer ${token}` } }).catch((e) => ({ ok: false, status: e.message }));
  if (!res.ok) { console.error(`Could not reach the relay with that token (${res.status}).`); process.exit(1); }

  let settings = {};
  try { settings = JSON.parse(fs.readFileSync(SETTINGS_FILE, 'utf8')); } catch {}
  try { fs.copyFileSync(SETTINGS_FILE, SETTINGS_FILE + '.pocket-backup'); } catch {}

  const node = process.execPath;
  const cmd = (sub) => `"${node}" "${SELF}" ${sub}`;
  const ours = (h) => h.hooks?.some((x) => x.command?.includes(SELF) || x.command?.includes('pocket.mjs'));
  const add = (event, entry) => {
    settings.hooks ??= {};
    settings.hooks[event] = (settings.hooks[event] || []).filter((h) => !ours(h));
    settings.hooks[event].push(entry);
  };
  const quick = { type: 'command', command: cmd('hook'), timeout: 15 };
  const long = { type: 'command', command: cmd('hook'), timeout: 3600 };
  add('SessionStart', { hooks: [quick] });
  add('SessionEnd', { hooks: [quick] });
  add('UserPromptSubmit', { hooks: [quick] });
  add('PostToolUse', { matcher: '*', hooks: [quick] });
  add('Notification', { hooks: [quick] });
  add('PermissionRequest', { matcher: '*', hooks: [long] });
  add('PreToolUse', { matcher: 'AskUserQuestion', hooks: [long] });
  add('Stop', { hooks: [long] });

  const prevStatus = settings.statusLine?.command && !settings.statusLine.command.includes('pocket.mjs')
    ? settings.statusLine.command : conf?.previousStatusLine;
  settings.statusLine = { type: 'command', command: cmd('statusline'), padding: 0 };

  fs.mkdirSync(path.dirname(SETTINGS_FILE), { recursive: true });
  fs.writeFileSync(SETTINGS_FILE, JSON.stringify(settings, null, 2) + '\n');
  fs.mkdirSync(CONF_DIR, { recursive: true });
  fs.writeFileSync(CONF_FILE, JSON.stringify({
    relay, token, inbox: INBOX, waitMinutes: conf?.waitMinutes ?? 30,
    ...(prevStatus ? { previousStatusLine: prevStatus } : {}),
  }, null, 2) + '\n', { mode: 0o600 });

  console.log('Claude Pocket installed.');
  console.log(`  hooks + status line → ${SETTINGS_FILE} (backup: settings.json.pocket-backup)`);
  console.log(`  config             → ${CONF_FILE}`);
  console.log(`  files from phone   → ${INBOX}`);
  console.log('Restart any running Claude Code sessions to pick up the hooks.');
}

async function uninstall() {
  let settings = {};
  try { settings = JSON.parse(fs.readFileSync(SETTINGS_FILE, 'utf8')); } catch { return; }
  for (const [event, list] of Object.entries(settings.hooks || {})) {
    settings.hooks[event] = list.filter((h) => !h.hooks?.some((x) => x.command?.includes('pocket.mjs')));
    if (!settings.hooks[event].length) delete settings.hooks[event];
  }
  if (settings.statusLine?.command?.includes('pocket.mjs')) {
    if (conf?.previousStatusLine) settings.statusLine = { type: 'command', command: conf.previousStatusLine };
    else delete settings.statusLine;
  }
  fs.writeFileSync(SETTINGS_FILE, JSON.stringify(settings, null, 2) + '\n');
  console.log('Claude Pocket hooks removed.');
}

// ---------------------------------------------------------------- main

const cmdName = process.argv[2];
const commands = { hook, statusline, install, uninstall, watch, message, inbox: () => pullInbox(true).then((n) => n || console.log('Nothing new.')) };
if (!commands[cmdName]) {
  console.error('Commands: install | uninstall | inbox | watch | message | hook | statusline');
  process.exit(1);
}
if (!conf && !['install', 'hook', 'statusline'].includes(cmdName)) {
  console.error('Not configured yet. Run: node pocket.mjs install --relay … --token …');
  process.exit(1);
}
// A hook must never break Claude Code, so swallow everything there.
commands[cmdName]().catch((e) => {
  if (cmdName === 'hook' || cmdName === 'statusline') process.exit(0);
  console.error(e.message); process.exit(1);
});
