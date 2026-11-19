// PS Medico service worker: the hosted copy opens instantly and works
// offline. The update manifest (manifest.json) is NEVER cached, so the app's
// own update banner always sees the live build; the app HTML itself is
// network-first, so a newly deployed build appears on the next open.
const C = 'psm-v3';
const SHELL = ['./', './PS_Medico.html', './index.html', './manifest.webmanifest', './icon-192.png', './icon-512.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(C).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;           // cross-origin: browser default
  if (url.pathname.endsWith('/manifest.json') || url.pathname.endsWith('extra_medicines.tsv') || url.pathname.endsWith('data_updates.json')) return;  // update check + supplement + data channel: always live
  const fresh = fetch(e.request).then(r => {
    if (r && r.ok) caches.open(C).then(c => c.put(e.request, r.clone()));
    return r;
  });
  const cached = caches.match(e.request);
  if (e.request.mode === 'navigate' || /PS_Medico\.html$|index\.html$|\/$/.test(url.pathname))
    e.respondWith(fresh.catch(() => cached.then(h => h || caches.match('./PS_Medico.html')).then(h => h || Response.error())));
  else
    e.respondWith(cached.then(h => h || fresh));
});
