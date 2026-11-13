// ⚠️ INCREMENTARE CACHE_VERSION AD OGNI MODIFICA DI HTML/CSS/JS
// altrimenti le modifiche non compaiono sui dispositivi già installati
const CACHE_VERSION = 'meteo-v44';

// Asset statici dell'app — cache-first
const STATIC_ASSETS = [
  './',
  './index.html',
  './dashboard.html',
  './manifest.json',
  './js/app.js',
  './js/radar.js',
  './js/luogo.js',
  './js/vista3d.js',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png',
];

// Dati del modello — network-first con fallback su cache
const DATA_PATHS = [
  '/data/latest.json',
  '/data/wind_grid.json',
  '/data/forecast_48h.json',
  '/data/griglia_48h.json',
  '/data/griglia_umidita_48h.json',
  '/data/griglia_vento_48h.json',
  '/data/dashboard_data.json',
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(STATIC_ASSETS))
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names.filter((name) => name !== CACHE_VERSION).map((name) => caches.delete(name))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  const url = new URL(event.request.url);

  // Solo stesso dominio: tile radar/basemap e API esterne passano dritte alla
  // rete, non intercettate (URL con timestamp variabile → cache illimitata).
  if (url.origin !== self.location.origin) return;

  if (DATA_PATHS.some((p) => url.pathname.endsWith(p))) {
    event.respondWith(networkFirst(event.request));
    return;
  }

  event.respondWith(cacheFirst(event.request));
});

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) {
    const cache = await caches.open(CACHE_VERSION);
    cache.put(request, response.clone());
  }
  return response;
}

// Rete prima; se la rete non risponde entro 10 s (telefono con segnale scarso) e c'è una copia in
// cache si usa quella, mentre la risposta della rete, se arriva, aggiorna comunque la cache.
async function networkFirst(request) {
  const cache = await caches.open(CACHE_VERSION);
  const net = fetch(request).then((response) => {
    if (response.ok) cache.put(request, response.clone()).catch(() => {});
    return response;
  });
  net.catch(() => {});  // se si è già risposto con la cache, un errore di rete dopo non va segnalato
  const timeout = new Promise((resolve) => setTimeout(() => resolve(null), 10000));
  try {
    const first = await Promise.race([net, timeout]);
    if (first) return first;
    const cached = await caches.match(request);
    return cached || (await net);
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    throw err;
  }
}
