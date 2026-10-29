// 화면(html/js)은 네트워크 우선 → 재배포 즉시 반영, 오프라인이면 캐시.
// 코스맵 이미지·코스 JSON은 캐시 우선(골프장에서 통신 끊겨도 보이도록).
// /api/* 는 캐시하지 않음(서버가 원본). 단 /api/images/ 는 이미지라 캐시.
const C = 'yardage-v4';
const SHELL = ['/', '/index.html', '/manifest.json', '/icon.svg', '/icons/icon-192.png', '/icons/icon-512.png', '/icons/apple-touch-icon.png', '/icons/favicon-32.png', '/courses/seowon-hills.json'];

self.addEventListener('install', e => { self.skipWaiting(); e.waitUntil(caches.open(C).then(c => c.addAll(SHELL))); });
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))).then(() => self.clients.claim())));

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const u = new URL(req.url);
  const isImage = /\.(jpg|jpeg|png|webp|gif)$/i.test(u.pathname) || u.pathname.startsWith('/api/images/');
  if (u.origin === location.origin && u.pathname.startsWith('/api/') && !isImage) return;

  if (isImage) {
    e.respondWith(caches.match(req).then(hit => hit || fetch(req).then(res => {
      if (res.ok || res.type === 'opaque') { const cl = res.clone(); caches.open(C).then(c => c.put(req, cl)); }
      return res;
    }).catch(() => hit)));
    return;
  }
  if (u.origin !== location.origin) return;
  e.respondWith(fetch(req).then(res => {
    if (res.ok) { const cl = res.clone(); caches.open(C).then(c => c.put(req, cl)); }
    return res;
  }).catch(() => caches.match(req, { ignoreSearch: req.mode === 'navigate' }).then(hit => hit || caches.match('/index.html'))));
});
