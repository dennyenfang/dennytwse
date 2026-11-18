const CACHE = 'macd-pwa-v6';
const ASSETS = new Map([
  ['/manifest.webmanifest', 'application/manifest+json'],
  ['/icon.svg', 'image/svg+xml']
]);

self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', event => event.waitUntil((async () => {
  for (const name of await caches.keys()) {
    if (name.startsWith('macd-pwa-') && name !== CACHE) await caches.delete(name);
  }
  await self.clients.claim();
})()));

async function fetchAsset(request, expectedType) {
  let response;
  try {
    response = await fetch(request);
  } catch (error) {
    const stored = await caches.match(request, {cacheName: CACHE});
    if (stored) return stored;
    throw error;
  }

  const type = response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase();
  const noStore = response.headers.get('Cache-Control')?.toLowerCase().includes('no-store');
  if (response.status === 200 && !response.redirected && response.url === request.url &&
      type === expectedType && !noStore) {
    try {
      const cache = await caches.open(CACHE);
      await cache.put(request, response.clone());
    } catch {
      // Browsers with storage disabled can still use the online response.
    }
  }
  return response;
}

self.addEventListener('fetch', event => {
  const {request} = event;
  const url = new URL(request.url);
  const type = ASSETS.get(url.pathname);
  if (type && request.method === 'GET' && request.mode !== 'navigate' &&
      url.origin === self.location.origin && !url.search) {
    event.respondWith(fetchAsset(request, type));
  }
});
