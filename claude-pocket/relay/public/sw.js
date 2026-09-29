// Handles Android's share sheet (Web Share Target) and keeps the app shell
// available offline. Shared files are parked in Cache Storage for app.js.
const SHELL = 'pocket-shell-v2';
const SHELL_FILES = ['/', '/app.js', '/manifest.webmanifest', '/icon.svg', '/icon-192.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k.startsWith('pocket-shell-') && k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

async function takeShare(request) {
  const form = await request.formData();
  const cache = await caches.open('pocket-share');
  const files = [];
  let i = 0;
  for (const f of form.getAll('files')) {
    if (!(f instanceof File)) continue;
    await cache.put(`/_share/${i}`, new Response(f, { headers: { 'Content-Type': f.type || 'application/octet-stream' } }));
    files.push({ i, name: f.name || `shared-${i}`, type: f.type || 'application/octet-stream' });
    i++;
  }
  const meta = { files, title: form.get('title') || '', text: form.get('text') || '', url: form.get('url') || '' };
  await cache.put('/_share/meta', new Response(JSON.stringify(meta), { headers: { 'Content-Type': 'application/json' } }));
  return Response.redirect('/?share=1', 303);
}

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;
  if (e.request.method === 'POST' && url.pathname === '/share') {
    e.respondWith(takeShare(e.request));
    return;
  }
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;
  // Network first so updates land immediately; cache only when offline.
  e.respondWith(fetch(e.request)
    .then((res) => {
      if (res.ok && SHELL_FILES.includes(url.pathname)) {
        const copy = res.clone();
        caches.open(SHELL).then((c) => c.put(e.request, copy));
      }
      return res;
    })
    .catch(() => caches.match(e.request).then((r) => r || caches.match('/'))));
});
