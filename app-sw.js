// PS Medico service worker - offline support (auto-generated, do not edit)
const CACHE = 'psm-v1';
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.add(new Request(self.registration.scope))).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  const u = new URL(e.request.url);
  if (u.pathname === '/sw.js') return;
  if (/^\/(ping|find|report|reports|publish|sweep)$/.test(u.pathname)) return;  // always live
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const hit = await cache.match(e.request, {ignoreSearch: u.pathname === '/'});
    const live = fetch(e.request).then(r => {
      if (r && r.ok) cache.put(e.request, r.clone());
      return r;
    }).catch(() => null);
    if (hit && (u.pathname === '/' || /\/(idx|idx1|data|extra)\b/.test(u.pathname))) {
      live;  // refresh in the background, answer from cache now
      return hit;
    }
    return (await live) || hit || new Response('{"ok":false,"error":"offline"}',
      {status: 502, headers: {'Content-Type': 'application/json'}});
  })());
});
