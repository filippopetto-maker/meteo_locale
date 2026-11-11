// vista3d.js — Metek 3D (dal 12/11/2026, solo app e telefono): il vento al suolo dell'M2 su griglia sopra il rilievo.
//
// Si carica solo quando si preme "3D" nella barra in alto (MapLibre ~800 kB non pesa sul 2D).
//   vento     data/griglia_vento_48h.json, lo stesso file della mappa 2D: velocità M2 a ~1 km, direzione ICON-2I
//             agganciata alle stazioni a ~2 km. Scie a nastro a pochi metri dal rilievo disegnato.
//   rilievo   3d/dem/{z}/{x}/{y}.webp (Copernicus GLO-90, terrarium, z6–11, ×2) e 3d/quota.bin (~550 m, per le
//             zone di avviso e il velo marroncino delle montagne)
//   stazioni  pillole col vento M2 di ogni stazione ora per ora (forecast_48h.json; per l'ora attuale latest.json)
//   Evento    cerca nelle prossime ore il tratto con raffiche stimate (1,5 × vento medio M2, la regola della scheda
//             48 h) ≥ 50 km/h su una zona del Lazio a terra e sotto i 1000 m, lo inquadra e fa scorrere le ore
//   Il centro della vista resta a 300 m (× rilievo) invece di appoggiarsi al terreno: in pianura le scie non "volano".
// Impostazioni prese dal sito: "Stazioni sulla mappa" e km/h | nodi. La ricerca (lente) vola sul luogo scelto.
(function () {
  'use strict';

  const ML = 'https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl';
  const EXAG = 2, PIVOT = 300 * EXAG;
  const KTS = 0.539957;
  let pronto = null, V = null, attivo = false;

  // ── caricamento di MapLibre (una volta) ───────────────────────────────────
  const carica = (tag, attr) => new Promise((ok, ko) => {
    const el = document.createElement(tag); Object.assign(el, attr); el.onload = ok; el.onerror = ko; document.head.appendChild(el);
  });
  async function maplibre() {
    if (window.maplibregl) return;
    await Promise.all([carica('link', { rel: 'stylesheet', href: ML + '.css' }), carica('script', { src: ML + '.js' })]);
  }

  // ── dati ──────────────────────────────────────────────────────────────────
  const inflate = async b64 => {
    const bin = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    return new Uint8Array(await new Response(new Blob([bin]).stream().pipeThrough(new DecompressionStream('deflate'))).arrayBuffer());
  };
  async function vento() {
    const j = await (await fetch('data/griglia_vento_48h.json')).json();
    if (j.versione !== 1 || !j.b64_dir_prima) throw new Error('griglia del vento senza direzione');
    const [b0, b1, d0, d1] = await Promise.all([j.b64_prima, j.b64_variazioni, j.b64_dir_prima, j.b64_dir_variazioni].map(inflate));
    const n = j.nx * j.ny, m = j.dir_nx * j.dir_ny, ntt = j.times.length, nd = 360 / j.passo_dir;
    const s0 = new Int16Array(b0.buffer, b0.byteOffset, n), sd = new Int8Array(b1.buffer, b1.byteOffset, n * (ntt - 1));
    const r0 = new Int16Array(d0.buffer, d0.byteOffset, m), rd = new Int8Array(d1.buffer, d1.byteOffset, m * (ntt - 1));
    // ore da mostrare: dall'ora attuale (o dalla prima ora non ancora passata) in poi
    const adesso = Date.now() - 3.6e6;
    let k0 = j.times.findIndex(t => Date.parse(t) >= adesso); if (k0 < 0) k0 = 0;
    const NT = ntt - k0, U = new Float32Array(NT * m), W = new Float32Array(NT * m);
    const sp = Int16Array.from(s0), di = Int16Array.from(r0);
    for (let k = 0; k < ntt; k++) {
      if (k > 0) {
        for (let c = 0; c < n; c++) sp[c] += sd[(k - 1) * n + c];
        for (let c = 0; c < m; c++) di[c] = ((di[c] + rd[(k - 1) * m + c]) % nd + nd) % nd;
      }
      if (k < k0) continue;
      const o = (k - k0) * m;
      for (let r = 0; r < j.dir_ny; r++) for (let c = 0; c < j.dir_nx; c++) {
        const ms = sp[(r * 2) * j.nx + c * 2] * j.passo / 3.6, a = di[r * j.dir_nx + c] * j.passo_dir * Math.PI / 180, i = o + r * j.dir_nx + c;
        U[i] = -ms * Math.sin(a); W[i] = -ms * Math.cos(a);   // direzione = da dove arriva
      }
    }
    const G = { lat0: j.lat_max, dlat: -2 * (j.lat_max - j.lat_min) / (j.ny - 1), lon0: j.lon_min, dlon: 2 * (j.lon_max - j.lon_min) / (j.nx - 1),
                nx: j.dir_nx, ny: j.dir_ny };
    return { G, U, W, NT, times: j.times.slice(k0) };
  }
  async function quote() {
    const r = await fetch('3d/quota.bin');
    const b = new Uint8Array(await new Response(r.body.pipeThrough(new DecompressionStream('deflate'))).arrayBuffer());
    const h = new DataView(b.buffer);   // intestazione: ny, nx (uint16), lat0, dlat, lon0, dlon (float32)
    const qm = { ny: h.getUint16(0, true), nx: h.getUint16(2, true), lat0: h.getFloat32(4, true), dlat: h.getFloat32(8, true),
                 lon0: h.getFloat32(12, true), dlon: h.getFloat32(16, true) };
    return { qm, Q: new Int16Array(b.buffer.slice(20)) };
  }
  async function stazioni() {
    const [la, f] = await Promise.all([fetch('data/latest.json').then(r => r.json()), fetch('data/forecast_48h.json').then(r => r.json()).catch(() => null)]);
    return (la.stations || []).filter(s => s.lat != null).map(s => {
      const ore = new Map(), p = f?.stations?.[String(s.id)];
      if (p) p.valid_for.forEach((v, i) => { if (p.ws[i] != null) ore.set(Date.parse(v), [p.ws[i], p.wd[i], p.t?.[i]]); });
      const fc = s.forecast;
      if (fc?.wind_speed != null) ore.set(-1, [fc.wind_speed, fc.wind_direction, fc.temperature]);   // ora senza previsione oraria
      return { id: s.id, name: s.name, lat: s.lat, lon: s.lon, ore };
    });
  }

  // ── interfaccia ───────────────────────────────────────────────────────────
  const CSS = `
  #map3d { position: fixed; inset: 0; z-index: 500; background: #0b0f17; display: none; }
  html.vista-3d #map3d { display: block; }
  html.vista-3d #map, html.vista-3d #dock, html.vista-3d .temp-legend, html.vista-3d #legend-ctl { visibility: hidden !important; }
  html.vista-3d #contour-toggle, html.vista-3d #wind-toggle, html.vista-3d #arrow-toggle,
  html.vista-3d #popup-style-group, html.vista-3d #prefs-popover .prefs-label { display: none !important; }
  #map3d .maplibregl-ctrl-attrib { font-size: 10px; }
  #map3d .maplibregl-ctrl-bottom-right { bottom: calc(176px + env(safe-area-inset-bottom, 0px)); }
  #dock3d { display: none; }
  html.vista-3d #dock3d {
    position: fixed; left: 8px; right: 8px; bottom: calc(8px + env(safe-area-inset-bottom, 0px)); z-index: 1000;
    display: flex; flex-direction: column; gap: 10px; padding: 12px;
    border-radius: 22px; background: var(--glass); border: .5px solid var(--border);
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); box-shadow: 0 12px 34px rgba(0, 0, 0, .55);
    font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    transition: transform .3s cubic-bezier(.2, .8, .2, 1), opacity .2s;
  }
  @media (min-width: 700px) { html.vista-3d #dock3d { left: 50%; right: auto; width: 560px; margin-left: -280px; } }
  body.ml-sheet-open #dock3d { transform: translateY(calc(100% + 40px)); opacity: 0; pointer-events: none; }
  #dock3d .riga3d { display: flex; align-items: center; gap: 10px; }
  #dock3d .radar-play-btn { width: 34px; height: 34px; flex: 0 0 auto; }
  #dock3d .leg3d { display: flex; align-items: center; gap: 8px; margin-top: -3px; }
  #dock3d .leg3d i { flex: 1; height: 5px; border-radius: 3px; background: linear-gradient(90deg,#5c6e8f,#b3c7e0 25%,#f2f5fa 50%,#ffc46b 75%,#ff6b5e); }
  #dock3d .leg3d span { font: 400 10px ui-monospace, "SF Mono", monospace; color: rgba(233, 233, 237, .7); white-space: nowrap; }
  #dock3d .rail3d { display: flex; padding: 3px; border-radius: 14px; background: rgba(0, 0, 0, .28); }
  #dock3d .rail3d .rail-item { flex: 1; width: auto; height: 50px; border-radius: 11px; }
  #dock3d .rail3d .rail-item.active { background: var(--accent); color: var(--accent-on); }
  #dock3d .rail3d .rail-item span { font-size: 10.5px; }
  .nav3d { display: none; }
  html.vista-3d .nav3d { position: fixed; right: 10px; top: calc(env(safe-area-inset-top, 0px) + 72px); z-index: 1000;
    display: flex; flex-direction: column; gap: 8px; }
  .anello3d { width: 56px; height: 56px; border-radius: 50%; background: var(--glass); border: .5px solid var(--border);
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); box-shadow: 0 6px 20px rgba(0, 0, 0, .45);
    cursor: grab; touch-action: none; user-select: none; -webkit-user-select: none; }
  .anello3d svg { display: block; width: 100%; height: 100%; }
  .did3d { display: none; }
  html.vista-3d .did3d:not([hidden]) { display: block; position: fixed; left: 50%; top: calc(env(safe-area-inset-top, 0px) + 72px); transform: translateX(-50%);
    z-index: 1000; max-width: min(460px, calc(100% - 96px)); padding: 10px 16px; border-radius: 16px; text-align: center;
    background: var(--glass); border: .5px solid var(--border); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    box-shadow: 0 10px 30px rgba(0, 0, 0, .45); font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: var(--text); }
  .did3d b { display: block; font-size: 15px; font-weight: 600; }
  .did3d b svg { vertical-align: -5px; margin-right: 7px; }
  .did3d span { color: var(--text-3); font-size: 12.5px; } .did3d .v { color: var(--text); font-weight: 600; }
  #map3d .st-pin { cursor: pointer; }
  #map3d.lontano .st-pin { display: none !important; }
  .casa3d { width: 14px; height: 14px; border-radius: 50%; background: #f2f5fa; box-shadow: 0 0 0 5px rgba(242,245,250,.25), 0 0 14px rgba(242,245,250,.6); }
  `;
  const ICON_EVENTO = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M3 8h11a3 3 0 100-6"></path><path d="M3 12h15a3.5 3.5 0 110 7"></path><path d="M3 16h7a2.5 2.5 0 110 5"></path></svg>';
  function interfaccia() {
    const st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);
    document.body.insertAdjacentHTML('beforeend', `
      <div id="map3d"></div>
      <div class="nav3d">
        <div class="anello3d" id="bussola3d" title="Trascina lungo l'anello per ruotare · tocca per il nord"><svg viewBox="0 0 58 58">
          <circle cx="29" cy="29" r="24" fill="none" stroke="rgba(255,255,255,.22)" stroke-width="5"/>
          <g id="ago3d"><path d="M29 2.5 33 10H25z" fill="#ff6b5e"/><text x="29" y="20" text-anchor="middle" font-size="9" font-weight="800" fill="#e9e9ed" font-family="Inter,-apple-system,sans-serif">N</text></g>
          <circle cx="29" cy="29" r="3" fill="rgba(255,255,255,.5)"/></svg></div>
        <div class="anello3d" id="incl3d" title="Trascina su e giù per inclinare · tocca per pianta o 3D"><svg viewBox="0 0 58 58">
          <circle cx="29" cy="29" r="24" fill="none" stroke="rgba(255,255,255,.14)" stroke-width="5"/>
          <path d="M29 5 A24 24 0 0 1 53 29" fill="none" stroke="rgba(255,255,255,.35)" stroke-width="5" stroke-linecap="round"/>
          <line id="incl3d-l" x1="29" y1="29" x2="29" y2="5" stroke="#e9e9ed" stroke-width="2" stroke-linecap="round"/><circle id="incl3d-p" cx="29" cy="5" r="4.5" fill="#fff"/>
          <text id="incl3d-t" x="22" y="47" text-anchor="middle" font-size="9.5" font-weight="700" fill="#e9e9ed" font-family="Inter,-apple-system,sans-serif"></text></svg></div>
      </div>
      <div class="did3d" id="did3d" hidden></div>
      <div id="dock3d">
        <div class="riga3d"><button class="radar-play-btn" id="play3d" type="button" title="Play/Pausa">▶</button>
          <div class="radar-track-wrap"><input type="range" id="ora3d-slider" class="radar-slider" min="0" max="1" step="0.02" value="0" aria-label="Ora mostrata"></div>
          <span class="fc48-now" id="ora3d"></span></div>
        <div class="leg3d"><span>0</span><i></i><span id="leg3d-max"></span></div>
        <div class="rail3d"><button class="rail-item" id="evento3d" type="button">${ICON_EVENTO}<span>Evento</span></button></div>
      </div>`);
  }

  // ── la vista ──────────────────────────────────────────────────────────────
  async function crea() {
    await maplibre();
    const [VM, QQ, ST] = await Promise.all([vento(), quote(), stazioni()]);
    interfaccia();
    const { G, U, W, NT } = VM, { qm, Q } = QQ, GN = G.nx * G.ny;
    const $ = id => document.getElementById(id);
    const unitaKts = () => !!document.querySelector('input[name="wind-unit"][value="kts"]')?.checked;
    const vU = kmh => Math.round(unitaKts() ? kmh * KTS : kmh), nomeU = () => (unitaKts() ? 'nodi' : 'km/h');
    const stazioniOn = () => document.getElementById('stations-check')?.checked !== false;
    const dem = new URL('3d/dem/{z}/{x}/{y}.webp', location.href).href.replace(/%7B/g, '{').replace(/%7D/g, '}');

    const map = new maplibregl.Map({
      container: 'map3d', attributionControl: { compact: true }, center: [12.72, 41.98], zoom: 7.7, pitch: 58, bearing: -10, maxPitch: 80,
      style: {
        version: 8, glyphs: 'https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf',
        sources: {
          base: { type: 'vector', url: 'https://tiles.openfreemap.org/planet',
            attribution: '© OpenStreetMap · OpenFreeMap · rilievo Copernicus · vento M2 Metek' },
          dem: { type: 'raster-dem', tiles: [dem], encoding: 'terrarium', tileSize: 256, minzoom: 6, maxzoom: 11, bounds: [11.3, 41.0, 14.7, 43.0] },
          ombra: { type: 'raster-dem', tiles: [dem], encoding: 'terrarium', tileSize: 256, minzoom: 6, maxzoom: 11, bounds: [11.3, 41.0, 14.7, 43.0] }
        },
        layers: [
          { id: 'terra', type: 'background', paint: { 'background-color': '#1b2520' } },
          { id: 'abitati', type: 'fill', source: 'base', 'source-layer': 'landuse', filter: ['in', ['get', 'class'], ['literal', ['residential', 'suburb', 'neighbourhood', 'industrial', 'commercial']]],
            paint: { 'fill-color': '#2a3242', 'fill-opacity': ['interpolate', ['linear'], ['zoom'], 7, .5, 11, .9] } },
          { id: 'mare', type: 'fill', source: 'base', 'source-layer': 'water', paint: { 'fill-color': '#0e2448' } },
          { id: 'fiumi', type: 'line', source: 'base', 'source-layer': 'waterway', filter: ['==', ['get', 'class'], 'river'],
            paint: { 'line-color': '#1d3f70', 'line-width': ['interpolate', ['linear'], ['zoom'], 8, .6, 12, 1.6] } },
          { id: 'shade', type: 'hillshade', source: 'ombra', paint: { 'hillshade-exaggeration': .45, 'hillshade-shadow-color': '#000000',
            'hillshade-highlight-color': '#66789a', 'hillshade-accent-color': '#1b2436', 'hillshade-illumination-direction': 315 } },
          { id: 'strade', type: 'line', source: 'base', 'source-layer': 'transportation', filter: ['in', ['get', 'class'], ['literal', ['motorway', 'trunk']]],
            paint: { 'line-color': '#3b4558', 'line-width': ['interpolate', ['linear'], ['zoom'], 8, .5, 12, 1.4] } },
          { id: 'confini', type: 'line', source: 'base', 'source-layer': 'boundary', filter: ['all', ['<=', ['get', 'admin_level'], 4], ['!=', ['get', 'maritime'], 1]],
            paint: { 'line-color': '#59647a', 'line-width': .8, 'line-dasharray': [3, 2] } }
        ],
        terrain: { source: 'dem', exaggeration: EXAG },
        sky: { 'sky-color': '#0b1220', 'horizon-color': '#1d2a44', 'fog-color': '#0b0f17', 'sky-horizon-blend': .6, 'horizon-fog-blend': .5, 'fog-ground-blend': .7 }
      }
    });
    // centro della vista a quota fissa (sul prototipo: i gesti lavorano su una copia della camera)
    const TP = Object.getPrototypeOf(map.transform);
    Object.defineProperty(TP, 'elevation', { configurable: true, get() { return this._elevation; },
      set() { if (this._elevation !== PIVOT) { this._elevation = PIVOT; this._constrain(); this._calcMatrices(); } } });
    TP.recalculateZoom = function () { if (this._elevation !== PIVOT) this.elevation = 0; };
    map.transform.elevation = 0;
    map.setMaxZoom(12.5);

    // campo di vento: bilineare nello spazio, lineare nel tempo
    let T = 0;
    function field(lon, lat) {
      const fx = (lon - G.lon0) / G.dlon, fy = (lat - G.lat0) / G.dlat;
      if (fx < 0 || fy < 0 || fx > G.nx - 1 || fy > G.ny - 1) return null;
      const x = Math.min(G.nx - 2, fx | 0), y = Math.min(G.ny - 2, fy | 0), dx = fx - x, dy = fy - y;
      const t0 = Math.min(NT - 1, T | 0), t1 = Math.min(NT - 1, t0 + 1), dt = T - t0;
      let u = 0, v = 0;
      for (const [t, wt] of [[t0, 1 - dt], [t1, dt]]) {
        if (!wt) continue;
        const b = t * GN;
        for (const [yy, xx, w] of [[y, x, (1 - dx) * (1 - dy)], [y, x + 1, dx * (1 - dy)], [y + 1, x, (1 - dx) * dy], [y + 1, x + 1, dx * dy]]) {
          const o = b + yy * G.nx + xx, ww = w * wt; u += U[o] * ww; v += W[o] * ww;
        }
      }
      return [u, v];
    }
    function quota(lon, lat) {
      const fx = (lon - qm.lon0) / qm.dlon, fy = (lat - qm.lat0) / qm.dlat;
      const x = Math.max(0, Math.min(qm.nx - 2, fx | 0)), y = Math.max(0, Math.min(qm.ny - 2, fy | 0));
      const dx = Math.min(1, Math.max(0, fx - x)), dy = Math.min(1, Math.max(0, fy - y)), n = qm.nx;
      return (Q[y * n + x] * (1 - dx) + Q[y * n + x + 1] * dx) * (1 - dy) + (Q[(y + 1) * n + x] * (1 - dx) + Q[(y + 1) * n + x + 1] * dx) * dy;
    }

    // particelle: scie di TRAIL punti in coordinate Mercatore, nascono soprattutto nella zona inquadrata
    const C = 40075016.686, TRAIL = 20, K = 0.0006, NP = 2600;
    const mx = lon => (lon + 180) / 360;
    const my = lat => { const p = lat * Math.PI / 180; return (1 - Math.log(Math.tan(Math.PI / 4 + p / 2)) / Math.PI) / 2; };
    const BOX = [G.lon0, G.lat0 + G.dlat * (G.ny - 1), G.lon0 + G.dlon * (G.nx - 1), G.lat0];
    const P = { n: NP, lon: new Float32Array(NP), lat: new Float32Array(NP), age: new Float32Array(NP), life: new Float32Array(NP),
                spd: new Float32Array(NP), tr: new Float32Array(NP * TRAIL * 3), head: new Int32Array(NP), fill: new Int32Array(NP) };
    let VIEW = null;
    function spawn(i, rnd) {
      let b = BOX;
      if (VIEW && Math.random() < .85) { const v = [Math.max(b[0], VIEW[0]), Math.max(b[1], VIEW[1]), Math.min(b[2], VIEW[2]), Math.min(b[3], VIEW[3])]; if (v[2] > v[0] && v[3] > v[1]) b = v; }
      P.lon[i] = b[0] + Math.random() * (b[2] - b[0]); P.lat[i] = b[1] + Math.random() * (b[3] - b[1]);
      P.life[i] = 60 + Math.random() * 120; P.age[i] = rnd ? Math.random() * P.life[i] : 0; P.fill[i] = 0; P.head[i] = 0;
    }
    for (let i = 0; i < NP; i++) spawn(i, true);
    const LL = new maplibregl.LngLat(0, 0); let SOPRA = 120;
    const terreno = (lon, lat) => { if (!map.terrain) return null; LL.lng = lon; LL.lat = lat;
      const h = map.terrain.getElevationForLngLatZoom(LL, map.transform.tileZoom); return h > 0 ? h : null; };
    function step() {
      const zm = map.getZoom(); SOPRA = Math.max(24, Math.min(120, 120 * Math.pow(2, 9 - zm)));   // da vicino ~12 m reali sopra il rilievo
      const kz = Math.max(.2, Math.min(1, Math.pow(2, 8.8 - zm)));                                // e passi più corti
      for (let i = 0; i < NP; i++) {
        const w = field(P.lon[i], P.lat[i]);
        if (!w || ++P.age[i] > P.life[i]) { spawn(i, false); continue; }
        const cl = Math.cos(P.lat[i] * Math.PI / 180), kk = K * kz;
        P.lon[i] += w[0] * kk / cl; P.lat[i] += w[1] * kk; P.spd[i] = Math.hypot(w[0], w[1]) * 3.6;
        const h = terreno(P.lon[i], P.lat[i]), z = (h != null ? h : Math.max(0, quota(P.lon[i], P.lat[i])) * EXAG) + SOPRA;
        const k = (P.head[i] = (P.head[i] + 1) % TRAIL), o = (i * TRAIL + k) * 3;
        P.tr[o] = mx(P.lon[i]); P.tr[o + 1] = my(P.lat[i]); P.tr[o + 2] = z / (C * cl);
        P.fill[i] = Math.min(TRAIL, P.fill[i] + 1);
      }
    }
    // colori: vento debole tenue, medio bianco, forte ambra, molto forte corallo (fondo scala 60 km/h)
    const RAMP = [[0, [.36, .43, .56], .38], [.25, [.7, .78, .88], .65], [.5, [.95, .96, .98], .85], [.75, [1, .77, .42], .95], [1, [1, .42, .37], 1]];
    function ramp(s) {
      for (let j = 1; j < RAMP.length; j++) if (s <= RAMP[j][0]) { const [a, ca, aa] = RAMP[j - 1], [b, cb, ab] = RAMP[j], f = (s - a) / (b - a);
        return [...ca.map((c, k) => c + (cb[k] - c) * f), aa + (ab - aa) * f]; }
      const e = RAMP[RAMP.length - 1]; return [...e[1], e[2]];
    }

    // nastri istanziati: più larghi vicino alla camera e col vento forte, coda che si assottiglia;
    // le montagne coprono le scie che stanno dietro
    const FL = 11;
    let prog, tpl, ibuf, vao, ext = null, inst = new Float32Array(NP * (TRAIL - 1) * FL);
    const AL = {}, UL = {};
    const VS = `uniform mat4 u_m; uniform vec2 u_vp; uniform float u_w0, u_wpx;
      attribute vec2 a_tpl; attribute vec3 a_p, a_q, a_rgb; attribute vec2 a_al; varying vec4 v_c; varying float v_s;
      void main(){ vec4 cp = u_m * vec4(a_p, 1.0), cq = u_m * vec4(a_q, 1.0);
        vec2 sp = cp.xy / cp.w * u_vp, sq = cq.xy / cq.w * u_vp; vec2 d = sq - sp; d = length(d) > 1e-4 ? normalize(d) : vec2(1.0, 0.0);
        vec2 n = vec2(-d.y, d.x); vec4 c = mix(cp, cq, a_tpl.x); float al = mix(a_al.x, a_al.y, a_tpl.x);
        float w = u_wpx * clamp(u_w0 / max(c.w, 1e-6), .35, 2.6) * (.3 + .7 * al);
        c.xy += n * a_tpl.y * w / u_vp * c.w; gl_Position = c; v_c = vec4(a_rgb, al); v_s = a_tpl.y; }`;
    const FS = `precision mediump float; varying vec4 v_c; varying float v_s;
      void main(){ float a = v_c.a * (1.0 - smoothstep(.45, 1.0, abs(v_s))); gl_FragColor = vec4(v_c.rgb * a, a); }`;
    const layer = {
      id: 'vento3d', type: 'custom', renderingMode: '3d',
      onAdd(m, gl) {
        const sh = (t, s) => { const o = gl.createShader(t); gl.shaderSource(o, s); gl.compileShader(o); return o; };
        prog = gl.createProgram(); gl.attachShader(prog, sh(gl.VERTEX_SHADER, VS)); gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FS)); gl.linkProgram(prog);
        for (const a of ['a_tpl', 'a_p', 'a_q', 'a_rgb', 'a_al']) AL[a] = gl.getAttribLocation(prog, a);
        for (const u of ['u_m', 'u_vp', 'u_w0', 'u_wpx']) UL[u] = gl.getUniformLocation(prog, u);
        tpl = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, tpl);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, -1, 1, -1, 0, 1, 0, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
        ibuf = gl.createBuffer();
        vao = gl.createVertexArray ? gl.createVertexArray() : null;
        if (!(typeof WebGL2RenderingContext !== 'undefined' && gl instanceof WebGL2RenderingContext)) ext = gl.getExtension('ANGLE_instanced_arrays');
      },
      render(gl, matrix) {
        let o = 0;
        for (let i = 0; i < NP; i++) {
          const f = P.fill[i]; if (f < 2) continue;
          const c = ramp(P.spd[i] / 60), life = Math.min(1, P.age[i] / 10, (P.life[i] - P.age[i]) / 15) * c[3];
          for (let s = 0; s < f - 1; s++) {
            const ka = (P.head[i] - s + TRAIL) % TRAIL, kb = (ka - 1 + TRAIL) % TRAIL, pa = (i * TRAIL + ka) * 3, pb = (i * TRAIL + kb) * 3;
            inst[o++] = P.tr[pa]; inst[o++] = P.tr[pa + 1]; inst[o++] = P.tr[pa + 2];
            inst[o++] = P.tr[pb]; inst[o++] = P.tr[pb + 1]; inst[o++] = P.tr[pb + 2];
            inst[o++] = c[0]; inst[o++] = c[1]; inst[o++] = c[2];
            inst[o++] = life * (1 - s / (f - 1)); inst[o++] = life * (1 - (s + 1) / (f - 1));
          }
        }
        const n = o / FL; if (!n) return;
        const cm = maplibregl.MercatorCoordinate.fromLngLat(map.getCenter(), PIVOT), m = matrix;
        const w0 = m[3] * cm.x + m[7] * cm.y + m[11] * cm.z + m[15];
        const div = (loc, d) => ext ? ext.vertexAttribDivisorANGLE(loc, d) : gl.vertexAttribDivisor(loc, d);
        if (vao) gl.bindVertexArray(vao);
        gl.useProgram(prog);
        gl.uniformMatrix4fv(UL.u_m, false, matrix);
        gl.uniform2f(UL.u_vp, gl.drawingBufferWidth / 2, gl.drawingBufferHeight / 2);
        gl.uniform1f(UL.u_w0, w0); gl.uniform1f(UL.u_wpx, 1.4 * (window.devicePixelRatio || 1));
        gl.bindBuffer(gl.ARRAY_BUFFER, tpl);
        gl.enableVertexAttribArray(AL.a_tpl); gl.vertexAttribPointer(AL.a_tpl, 2, gl.FLOAT, false, 0, 0); div(AL.a_tpl, 0);
        gl.bindBuffer(gl.ARRAY_BUFFER, ibuf); gl.bufferData(gl.ARRAY_BUFFER, inst.subarray(0, o), gl.DYNAMIC_DRAW);
        const S = FL * 4, A = [['a_p', 3, 0], ['a_q', 3, 12], ['a_rgb', 3, 24], ['a_al', 2, 36]];
        for (const [a, sz, off] of A) { gl.enableVertexAttribArray(AL[a]); gl.vertexAttribPointer(AL[a], sz, gl.FLOAT, false, S, off); div(AL[a], 1); }
        gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LEQUAL); gl.depthMask(false);
        gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
        if (ext) ext.drawArraysInstancedANGLE(gl.TRIANGLES, 0, 6, n); else gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, n);
        for (const [a] of A) { div(AL[a], 0); gl.disableVertexAttribArray(AL[a]); }
        gl.disableVertexAttribArray(AL.a_tpl);
        if (vao) gl.bindVertexArray(null);
        gl.depthMask(true);
        map.painter.context.setDirty();   // MapLibre riapplica il suo stato
      }
    };

    // raffiche stimate e zone di avviso (solo durante l'Evento), a velo sul rilievo
    const raffica = (t, i) => Math.hypot(U[t * GN + i], W[t * GN + i]) * 3.6 * 1.5;
    const WARN_COL = [[150, 160, 180], [244, 208, 63], [255, 138, 61], [255, 77, 77]];
    const livello = g => g >= 90 ? 3 : g >= 75 ? 2 : g >= 50 ? 1 : 0;
    const LAZIO = [[11.45, 42.38], [11.75, 42.82], [12.0, 42.7], [12.3, 42.62], [12.5, 42.42], [12.85, 42.6], [13.1, 42.78], [13.4, 42.7], [13.3, 42.4], [13.1, 42.15],
      [13.3, 41.95], [13.6, 41.85], [13.95, 41.65], [14.05, 41.42], [13.8, 41.22], [13.0, 41.22], [12.6, 41.42], [12.2, 41.7], [11.8, 42.0]];
    const inLazio = (x, y) => { let d = false; for (let i = 0, j = LAZIO.length - 1; i < LAZIO.length; j = i++) {
      const [xi, yi] = LAZIO[i], [xj, yj] = LAZIO[j]; if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) d = !d; } return d; };
    const cella = i => [G.lon0 + (i % G.nx) * G.dlon, G.lat0 + ((i / G.nx) | 0) * G.dlat];
    let MASK = null, EV_ON = false, oraAvv = -1;
    const maschera = () => MASK || (MASK = Uint8Array.from({ length: GN }, (_, i) => { const [lo, la] = cella(i), q = quota(lo, la); return inLazio(lo, la) && q > 2 && q < 1000 ? 1 : 0; }));
    const VELO = [[42, [244, 208, 63], 0], [55, [244, 208, 63], .42], [75, [255, 138, 61], .52], [95, [255, 77, 77], .6]];
    const velo = g => { if (g <= VELO[0][0]) return null;
      for (let j = 1; j < VELO.length; j++) if (g <= VELO[j][0]) { const [a, ca, aa] = VELO[j - 1], [b, cb, ab] = VELO[j], f = (g - a) / (b - a);
        return [...ca.map((c, k) => c + (cb[k] - c) * f), aa + (ab - aa) * f]; }
      return [...VELO[3][1], VELO[3][2]]; };
    const cv0 = document.createElement('canvas'), cv = document.createElement('canvas'), UP = 4;
    cv0.width = G.nx; cv0.height = G.ny; cv.width = G.nx * UP; cv.height = G.ny * UP;
    function avvisiImg() {
      const t = Math.min(NT - 1, Math.round(T)), g0 = cv0.getContext('2d'), im = g0.createImageData(G.nx, G.ny);
      if (EV_ON) { const M = maschera(); for (let i = 0; i < GN; i++) { if (!M[i]) continue; const c = velo(raffica(t, i)); if (!c) continue;
        const o = i * 4; im.data[o] = c[0]; im.data[o + 1] = c[1]; im.data[o + 2] = c[2]; im.data[o + 3] = c[3] * 255; } }   // riga 0 = nord
      g0.putImageData(im, 0, 0);
      const g = cv.getContext('2d'); g.clearRect(0, 0, cv.width, cv.height); g.filter = `blur(${UP * .9}px)`; g.drawImage(cv0, 0, 0, cv.width, cv.height); g.filter = 'none';
      oraAvv = t; return cv.toDataURL();
    }
    const avvisiOra = () => { const src = map.getSource('avvisi');
      if (src && (Math.round(T) !== oraAvv || avvisiOra.on !== EV_ON)) { avvisiOra.on = EV_ON; src.updateImage({ url: avvisiImg() }); } };
    const angoli = g => { const la = [g.lat0, g.lat0 + g.dlat * (g.ny - 1)], lo = [g.lon0, g.lon0 + g.dlon * (g.nx - 1)];
      const N = Math.max(...la) + Math.abs(g.dlat) / 2, S = Math.min(...la) - Math.abs(g.dlat) / 2, Wl = Math.min(...lo) - Math.abs(g.dlon) / 2, E = Math.max(...lo) + Math.abs(g.dlon) / 2;
      return [[Wl, N], [E, N], [E, S], [Wl, S]]; };

    // pillole delle stazioni, ora per ora, nell'unità scelta
    const freccia = wd => `<svg width="10" height="10" viewBox="0 0 16 16" style="transform:rotate(${Math.round((wd ?? 0) + 180)}deg)"><path d="M8 1 13 13 8 10 3 13z" fill="currentColor"/></svg>`;
    const markers = ST.map(s => {
      const el = document.createElement('div'); el.className = 'st-pin'; el.title = s.name;
      const pop = new maplibregl.Popup({ offset: 12, closeButton: false });
      const mk = new maplibregl.Marker({ element: el, anchor: 'center', opacityWhenCovered: '0.15' }).setLngLat([s.lon, s.lat]).setPopup(pop);
      mk._ora = ms => { const v = s.ore.get(ms) || (ms <= Date.now() + 3.6e6 ? s.ore.get(-1) : null);
        el.style.display = v ? '' : 'none'; if (!v) return;
        el.innerHTML = `${freccia(v[1])}${vU(v[0])}<small>${unitaKts() ? 'kts' : 'km/h'}</small>`;
        pop.setText(`${s.name} · ${vU(v[0])} ${nomeU()}${v[2] != null ? ' · ' + v[2].toFixed(1) + '°' : ''}`); };
      return mk;
    });
    let oraPill = null;
    const pilloleOra = forza => { const ms = Date.parse(VM.times[Math.min(NT - 1, Math.round(T))]);
      if (forza || ms !== oraPill) { oraPill = ms; markers.forEach(m => m._ora(ms)); } };
    const pillole = () => markers.forEach(m => stazioniOn() ? m.addTo(map) : m.remove());

    // nomi: i nostri (città, laghi, cime) e da vicino le località della base OSM
    const LUOGHI = [
      ['Roma', 12.496, 41.903, 1], ['Viterbo', 12.107, 42.417, 2], ['Rieti', 12.862, 42.404, 2], ['Latina', 12.904, 41.467, 2], ['Frosinone', 13.350, 41.640, 2],
      ['Terni', 12.646, 42.564, 2], ["L'Aquila", 13.399, 42.350, 2], ['Civitavecchia', 11.797, 42.093, 3], ['Fiumicino', 12.233, 41.771, 3], ['Tivoli', 12.798, 41.963, 3],
      ['Anzio', 12.628, 41.448, 3], ['Velletri', 12.777, 41.687, 3], ['Orvieto', 12.112, 42.719, 3], ['Avezzano', 13.426, 42.031, 3], ['Terracina', 13.250, 41.292, 3],
      ['Gaeta', 13.571, 41.214, 3], ['Cassino', 13.831, 41.492, 3], ['Colleferro', 13.003, 41.729, 3], ['Bracciano', 12.177, 42.103, 3], ['Tarquinia', 11.758, 42.249, 3],
      ['Subiaco', 13.093, 41.925, 3], ['Sora', 13.613, 41.718, 3], ['Fondi', 13.425, 41.356, 3], ['Montalto di Castro', 11.608, 42.351, 3], ['Narni', 12.515, 42.518, 3],
      ['Lago di Bolsena', 11.93, 42.60, 4], ['Lago di Bracciano', 12.23, 42.12, 4], ['Lago di Vico', 12.17, 42.32, 4],
      ['Terminillo 2217 m', 13.003, 42.475, 5], ['Monte Velino 2486 m', 13.385, 42.153, 5], ['Monte Viglio 2156 m', 13.390, 41.880, 5], ['Monte Cimino 1053 m', 12.205, 42.408, 5],
      ['Monte Cavo 949 m', 12.708, 41.751, 5], ['Monte Semprevisa 1536 m', 13.116, 41.580, 5], ['Gran Sasso 2912 m', 13.566, 42.469, 5]];

    let tickOn = false;
    const tick = () => { if (!tickOn) return; step(); map.triggerRepaint(); requestAnimationFrame(tick); };
    map.on('load', () => {
      map.addSource('luoghi', { type: 'geojson', data: { type: 'FeatureCollection', features: LUOGHI.map(([n, lo, la, k]) => ({ type: 'Feature', properties: { n, k }, geometry: { type: 'Point', coordinates: [lo, la] } })) } });
      map.addLayer({ id: 'luoghi', type: 'symbol', source: 'luoghi',
        filter: ['any', ['<=', ['get', 'k'], 1], ['all', ['==', ['get', 'k'], 2], ['>=', ['zoom'], 7.6]], ['all', ['>=', ['get', 'k'], 3], ['>=', ['zoom'], 8.8]]],
        layout: { 'text-field': ['get', 'n'], 'text-font': ['case', ['>=', ['get', 'k'], 4], ['literal', ['Noto Sans Italic']], ['literal', ['Noto Sans Medium']]],
          'text-size': ['match', ['get', 'k'], 1, 18, 2, 14, 3, 12, 11.5], 'text-letter-spacing': ['case', ['<=', ['get', 'k'], 2], .04, 0],
          'text-transform': ['case', ['<=', ['get', 'k'], 2], 'uppercase', 'none'], 'symbol-sort-key': ['get', 'k'],
          'text-anchor': ['case', ['==', ['get', 'k'], 5], 'bottom', 'center'], 'text-offset': ['case', ['==', ['get', 'k'], 5], ['literal', [0, -.4]], ['literal', [0, 0]]] },
        paint: { 'text-color': ['match', ['get', 'k'], 4, '#7fb4e6', 5, '#c9b99a', '#f4f6fb'], 'text-halo-color': 'rgba(8,11,18,.92)', 'text-halo-width': 1.6, 'text-halo-blur': .4 } });
      map.addLayer({ id: 'paesi', type: 'symbol', source: 'base', 'source-layer': 'place',
        filter: ['any', ['all', ['in', ['get', 'class'], ['literal', ['city', 'town']]], ['>=', ['zoom'], 9.4]],
          ['all', ['==', ['get', 'class'], 'village'], ['>=', ['zoom'], 10.4]], ['all', ['in', ['get', 'class'], ['literal', ['hamlet', 'suburb', 'quarter']]], ['>=', ['zoom'], 12]]],
        layout: { 'text-field': ['coalesce', ['get', 'name:it'], ['get', 'name']], 'text-font': ['literal', ['Noto Sans Regular']],
          'text-size': ['match', ['get', 'class'], 'city', 13, 'town', 12, 'village', 11, 10.5], 'text-max-width': 8,
          'symbol-sort-key': ['match', ['get', 'class'], 'city', 0, 'town', 1, 'village', 2, 3] },
        paint: { 'text-color': '#c3cbda', 'text-halo-color': 'rgba(8,11,18,.9)', 'text-halo-width': 1.4 } }, 'luoghi');
      // montagne: velo marroncino che sale piano con la quota (da ~450 m, pieno sopra i 1800 m)
      { const c = document.createElement('canvas'); c.width = qm.nx; c.height = qm.ny;
        const g = c.getContext('2d'), im = g.createImageData(qm.nx, qm.ny);
        for (let y = 0; y < qm.ny; y++) for (let x = 0; x < qm.nx; x++) {
          const a = Math.max(0, Math.min(1, (Q[y * qm.nx + x] - 450) / 1350)), o = ((qm.dlat < 0 ? y : qm.ny - 1 - y) * qm.nx + x) * 4;
          im.data[o] = 96; im.data[o + 1] = 76; im.data[o + 2] = 52; im.data[o + 3] = a * a * (3 - 2 * a) * 92; }
        g.putImageData(im, 0, 0);
        map.addSource('montagne', { type: 'image', url: c.toDataURL(), coordinates: angoli(qm) });
        map.addLayer({ id: 'montagne', type: 'raster', source: 'montagne', paint: { 'raster-fade-duration': 0 } }, 'shade'); }
      map.addSource('avvisi', { type: 'image', url: avvisiImg(), coordinates: angoli(G) });
      map.addLayer({ id: 'avvisi', type: 'raster', source: 'avvisi', paint: { 'raster-fade-duration': 0 } }, 'paesi');
      map.addLayer(layer, 'paesi');
      const vista = () => { const bb = map.getBounds(), c = map.getCenter(), r = Math.min(2.5, 1.6 * Math.pow(2, 8.5 - map.getZoom())), cl = Math.cos(c.lat * Math.PI / 180);
        VIEW = [Math.max(bb.getWest(), c.lng - r / cl), Math.max(bb.getSouth(), c.lat - r), Math.min(bb.getEast(), c.lng + r / cl), Math.min(bb.getNorth(), c.lat + r)]; };
      map.on('moveend', vista); vista();
      const lontano = () => map.getContainer().classList.toggle('lontano', map.getZoom() < 9.2);
      map.on('zoom', lontano); lontano();
      pillole(); pilloleOra(true);
      map.getContainer().querySelector('.maplibregl-ctrl-attrib')?.classList.remove('maplibregl-compact-show');   // crediti chiusi, si aprono con la (i)
    });

    // barra oraria (stessa card della mappa 2D) e legenda nell'unità scelta
    const ti = $('ora3d-slider'), PB = $('play3d'); ti.max = NT - 1;
    const legenda = () => { $('leg3d-max').textContent = `${vU(60)}+ ${unitaKts() ? 'kts' : 'km/h'}`; };
    const showT = () => {
      $('ora3d').textContent = new Date(VM.times[Math.min(NT - 1, Math.round(T))]).toLocaleString('it-IT', { timeZone: 'Europe/Rome', weekday: 'short', hour: '2-digit', minute: '2-digit' });
      pilloleOra(); avvisiOra();
    };
    let gira = false, tPrec = 0;
    const pausa = () => { gira = false; PB.textContent = '▶'; };
    ti.addEventListener('input', () => { pausa(); T = +ti.value; showT(); });
    PB.addEventListener('click', () => { if (gira) return pausa(); gira = true; PB.textContent = '❚❚'; tPrec = performance.now();
      const f = now => { if (!gira) return; T += (now - tPrec) / 1000 * 1.2; tPrec = now; if (T > NT - 1) T = 0; ti.value = T; showT(); requestAnimationFrame(f); };
      requestAnimationFrame(f); });
    const setT = t => { T = Math.max(0, Math.min(NT - 1, t)); ti.value = T; showT(); };
    // impostazioni del sito
    document.getElementById('stations-check')?.addEventListener('change', pillole);
    document.querySelectorAll('input[name="wind-unit"]').forEach(r => r.addEventListener('change', () => { legenda(); pilloleOra(true); }));
    legenda(); showT();

    // bussola e inclinazione
    const segnaNav = () => { $('ago3d').setAttribute('transform', `rotate(${-map.getBearing()} 29 29)`);
      const p = map.getPitch(), a = p * Math.PI / 180, x = 29 + 24 * Math.sin(a), y = 29 - 24 * Math.cos(a);
      $('incl3d-l').setAttribute('x2', x); $('incl3d-l').setAttribute('y2', y); $('incl3d-p').setAttribute('cx', x); $('incl3d-p').setAttribute('cy', y);
      $('incl3d-t').textContent = p < 3 ? '2D' : Math.round(p) + '°'; };
    map.on('rotate', segnaNav); map.on('pitch', segnaNav); segnaNav();
    const trascina = (el, giu, muovi, tocco) => { let s = null;
      el.addEventListener('pointerdown', e => { s = { x: e.clientX, y: e.clientY, mosso: false, ...giu(e) }; el.setPointerCapture(e.pointerId); stop(); });
      el.addEventListener('pointermove', e => { if (!s) return; if (Math.hypot(e.clientX - s.x, e.clientY - s.y) > 3) s.mosso = true; if (s.mosso) muovi(e, s); });
      el.addEventListener('pointerup', () => { if (s && !s.mosso) tocco(); s = null; }); };
    const BU = $('bussola3d');
    const angolo = e => { const r = BU.getBoundingClientRect(); return Math.atan2(e.clientX - r.left - r.width / 2, -(e.clientY - r.top - r.height / 2)) * 180 / Math.PI; };
    trascina(BU, e => ({ a0: angolo(e), b0: map.getBearing() }), (e, s) => map.setBearing(s.b0 - (angolo(e) - s.a0)), () => map.easeTo({ bearing: 0, duration: 700 }));
    trascina($('incl3d'), () => ({ p0: map.getPitch() }), (e, s) => map.setPitch(Math.max(0, Math.min(80, s.p0 + (s.y - e.clientY) * .6))),
      () => map.easeTo({ pitch: map.getPitch() > 10 ? 0 : 60, duration: 800 }));

    // voli
    const did = $('did3d'), EVB = $('evento3d');
    const barraAlta = () => (document.getElementById('brand-lockup')?.getBoundingClientRect().bottom || 60);
    const mostra = html => { did.innerHTML = html; did.hidden = false; did.style.top = (barraAlta() + 8) + 'px'; };
    let VOLO = 0;
    function stop() { VOLO++; did.hidden = true; EVB.classList.remove('active'); if (EV_ON) { EV_ON = false; avvisiOra(); } }
    for (const ev of ['dragstart', 'zoomstart', 'rotatestart', 'pitchstart']) map.on(ev, e => { if (e.originalEvent) stop(); });
    const attesa = (ms, id) => new Promise(r => setTimeout(() => r(VOLO === id), ms));
    function vola(cam, ms, t0, t1, id) {
      return new Promise(res => {
        let fermo = false; map.once('moveend', () => { fermo = true; });
        map.flyTo({ ...cam, duration: ms, essential: true, curve: 1.3 });
        const a = performance.now();
        const f = () => { if (VOLO !== id) return res(false);
          const e = performance.now() - a, k = Math.min(1, e / ms); if (t1 != null) setT(t0 + (t1 - t0) * k);
          if (k < 1 || (!fermo && e < ms + 1500)) requestAnimationFrame(f); else res(VOLO === id); };
        f();
      });
    }
    const quando = t => { const d = new Date(VM.times[Math.min(NT - 1, Math.round(t))]), oggi = new Date();
      const g = d.toDateString() === oggi.toDateString() ? 'oggi' : d.toDateString() === new Date(+oggi + 864e5).toDateString() ? 'domani' : d.toLocaleDateString('it-IT', { weekday: 'long' });
      return g + ' ore ' + String(d.getHours()).padStart(2, '0'); };
    const oraBreve = t => String(new Date(VM.times[t]).getHours()).padStart(2, '0');
    const iconaVento = lv => `<svg width="22" height="22" viewBox="0 0 14 14" fill="none" stroke="rgb(${WARN_COL[lv]})" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M1.5 5h7a2 2 0 1 0-2-2M1.5 8.5h9.5a2 2 0 1 1-2 2"/></svg>`;
    function trovaEvento() {
      const M = maschera(), ore = [];
      for (let t = 0; t < NT; t++) { let c = 0, m = 0; for (let i = 0; i < GN; i++) { if (!M[i]) continue; const g = raffica(t, i); if (g > m) m = g; if (g >= 50) c++; } ore.push({ t, c, mx: m }); }
      const tratti = []; let cur = null;
      for (const o of ore) { if (o.c >= 8) { if (cur) cur.push(o); else tratti.push(cur = [o]); } else cur = null; }
      return { ore, best: tratti.sort((a, b) => Math.max(...b.map(o => o.mx)) - Math.max(...a.map(o => o.mx)))[0], top: ore.reduce((a, o) => o.mx > a.mx ? o : a) };
    }
    async function evento() {
      const id = ++VOLO; EVB.classList.add('active'); EV_ON = true; avvisiOra();
      const E = trovaEvento();
      if (!E.best) {
        mostra(`<b>${iconaVento(0)}Nessuna raffica oltre ${vU(50)} ${nomeU()}</b><span>nelle prossime ${NT} ore in Lazio · massimo ${vU(E.top.mx)} ${nomeU()} ${quando(E.top.t)}</span>`);
        await vola({ center: [12.75, 41.95], zoom: 8.1, pitch: 50, bearing: 0 }, 3000, null, null, id);
        if (await attesa(5000, id)) stop();
        return;
      }
      const ev = E.best, a = ev[0], z = ev[ev.length - 1], picco = ev.reduce((p, o) => o.mx > p.mx ? o : p);
      // inquadratura sulle zone colorate: raffica massima per cella nelle ore dell'evento, pesata, estremi tagliati (5–95%)
      const M = maschera(), pts = [];
      for (let i = 0; i < GN; i++) { if (!M[i]) continue; let g = 0; for (const o of ev) g = Math.max(g, raffica(o.t, i)); if (g >= 50) { const [lo, la] = cella(i); pts.push([lo, la, g - 45]); } }
      const qtl = (k, p) => { const v = pts.map(x => [x[k], x[2]]).sort((x, y) => x[0] - y[0]), tot = v.reduce((s, x) => s + x[1], 0);
        let c = 0; for (const [x, w] of v) { c += w; if (c >= p * tot) return x; } return v[v.length - 1][0]; };
      const bb = [qtl(0, .05), qtl(1, .05), qtl(0, .95), qtl(1, .95)], H = innerHeight;
      const padding = { top: Math.min(H * .3, barraAlta() + 90), bottom: Math.min(H * .3, $('dock3d').offsetHeight + 30), left: Math.min(40, innerWidth * .06), right: Math.min(90, innerWidth * .2) };
      const cam = map.cameraForBounds([[bb[0] - .05, bb[1] - .05], [bb[2] + .05, bb[3] + .05]], { padding, pitch: 0, bearing: 0 }) || { center: [(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2], zoom: 8.6 };
      const zoom = Math.min(10, Math.max(8, cam.zoom - .35));
      const riga = o => `<b>${iconaVento(livello(o.mx))}${quando(o.t)}</b><span>raffiche fino a <span class="v">${vU(o.mx)} ${nomeU()}</span> · picco ${vU(picco.mx)} alle ${oraBreve(picco.t)}</span>`;
      mostra(`<b>${iconaVento(livello(picco.mx))}Raffiche fino a ${vU(picco.mx)} ${nomeU()}</b><span>${quando(a.t)} → ${oraBreve(z.t)} · stima dal vento M2</span>`);
      if (!await vola({ center: cam.center, zoom, pitch: 48, bearing: 0 }, 4500, T, a.t, id)) return;
      const dur = Math.max(6000, (z.t - a.t + 1) * 1400), t0 = performance.now();
      map.easeTo({ bearing: 12, pitch: 52, duration: dur, easing: x => x });
      await new Promise(res => { let h = -1; const f = () => { if (VOLO !== id) return res();
        const k = Math.min(1, (performance.now() - t0) / dur), t = a.t + (z.t + .99 - a.t) * k; setT(t);
        const o = E.ore[Math.min(NT - 1, Math.floor(t))]; if (o.t !== h) { h = o.t; mostra(riga(o)); }
        if (k < 1) requestAnimationFrame(f); else res(); }; f(); });
      if (await attesa(3000, id)) stop();
    }
    EVB.addEventListener('click', () => (EVB.classList.contains('active') ? stop() : evento()));

    // luogo scelto con la lente: si vola lì (la scheda 48 h del sito copre la parte bassa)
    let casa = null;
    async function vaiA(p) {
      const id = ++VOLO; EVB.classList.remove('active'); did.hidden = true; if (EV_ON) { EV_ON = false; avvisiOra(); }
      if (!field(p.lon, p.lat)) return;
      if (casa) casa.remove();
      const el = document.createElement('div'); el.className = 'casa3d';
      casa = new maplibregl.Marker({ element: el, opacityWhenCovered: '0.3' }).setLngLat([p.lon, p.lat]).addTo(map);
      const sotto = Math.min(innerHeight * .3, 230) / 2;
      await vola({ center: [p.lon, p.lat], zoom: 11, pitch: 68, bearing: map.getBearing(), offset: [0, -sotto] }, 4500, null, null, id);
    }

    return {
      mostra() { tickOn = true; map.resize(); tick(); },
      nascondi() { tickOn = false; pausa(); stop(); },
      vaiA,
    };
  }

  window.Metek3D = {
    get attivo() { return attivo; },
    async attiva() {
      attivo = true;
      document.documentElement.classList.add('vista-3d');
      const g = document.getElementById('wind-unit-group'); if (g) g.style.display = '';
      try {
        pronto = pronto || crea();
        V = await pronto;
        if (attivo) V.mostra();
      } catch (e) {
        console.error('3D non disponibile', e);
        pronto = null; this.disattiva();
        alert('La vista 3D non si è caricata. Riprova tra poco.');
      }
    },
    disattiva() {
      attivo = false;
      document.documentElement.classList.remove('vista-3d');
      if (V) V.nascondi();
    },
    vaiA(p) { if (attivo && V && p && p.lat != null) V.vaiA(p); },
  };
})();
