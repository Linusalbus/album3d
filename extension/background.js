/* Talks to the local clipper server on behalf of the content script.
   Fetching from here (rather than from the page) keeps the request under the
   extension's host permissions instead of YouTube's CORS rules. */

const CANDIDATE_PORTS = [8765, 5000, 8000, 5001, 3000, 8080];
const PING_TIMEOUT_MS = 1200;

async function ping(base) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), PING_TIMEOUT_MS);
  try {
    const r = await fetch(base + "/clipper/ping", { signal: ctl.signal });
    if (!r.ok) return false;
    const d = await r.json();
    return d && d.app === "youtube-clipper";
  } catch (e) {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

/* Remembers the port that worked so later clips skip the scan. */
async function findServer(force) {
  const { serverBase, customBase } = await chrome.storage.local.get([
    "serverBase", "customBase",
  ]);
  const tries = [];
  if (customBase) tries.push(customBase.replace(/\/+$/, ""));
  if (!force && serverBase) tries.push(serverBase);
  for (const port of CANDIDATE_PORTS) tries.push(`http://127.0.0.1:${port}`);

  for (const base of tries) {
    if (await ping(base)) {
      await chrome.storage.local.set({ serverBase: base });
      return base;
    }
  }
  throw new Error(
    "Can't reach the clipper server. Start it with:  PORT=8765 python3 app.py"
  );
}

async function call(path, options) {
  let base = await findServer(false);
  try {
    return await request(base + path, options);
  } catch (e) {
    // the server may have moved to a different port since last time
    base = await findServer(true);
    return await request(base + path, options);
  }
}

async function request(url, options) {
  const r = await fetch(url, options);
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.error) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    try {
      if (msg.type === "server") {
        reply({ ok: true, base: await findServer(true) });
      } else if (msg.type === "info") {
        const base = await findServer(false);
        const data = await call(
          "/clipper/info?url=" + encodeURIComponent(msg.url)
        );
        reply({ ok: true, data, base });
      } else if (msg.type === "preview") {
        reply({
          ok: true,
          data: await call("/clipper/preview", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(msg.body),
          }),
        });
      } else if (msg.type === "render") {
        const base = await findServer(false);
        const data = await call("/clipper/render", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(msg.body),
        });
        reply({ ok: true, data, base });
      } else {
        reply({ ok: false, error: "Unknown request" });
      }
    } catch (e) {
      reply({ ok: false, error: e.message || String(e) });
    }
  })();
  return true; // keep the message channel open for the async reply
});

chrome.action.onClicked.addListener((tab) => {
  if (tab.id) chrome.tabs.sendMessage(tab.id, { type: "toggle" }).catch(() => {});
});
