// vista3d.js — Metek 3D (dal 12/11/2026 in app e telefono, dal 13/11 anche da PC): il vento al suolo dell'M2 su griglia sopra il rilievo.
//
// Si carica solo quando si preme "3D" nella barra in alto (MapLibre ~800 kB non pesa sul 2D).
//   vento     data/griglia_vento_48h.json, lo stesso file della mappa 2D: velocità M2 a ~1 km, direzione ICON-2I
//             agganciata alle stazioni a ~2 km. Scie a nastro a pochi metri dal rilievo disegnato.
//   rilievo   3d/dem/{z}/{x}/{y}.webp (Copernicus GLO-90, terrarium, z6–11, ×1,5) e 3d/quota.bin (~550 m, per le
//             zone di avviso e il velo marroncino delle montagne); ombre da 3d/rilievo/ (cotte una volta sola
//             da tools/rilievo_cotto.py: più luci, occlusione ambientale, ombre portate; bluastre, al massimo −46%)
//   stazioni  pillole col vento M2 di ogni stazione ora per ora (forecast_48h.json; per l'ora attuale latest.json)
//   isolinee  interruttore accanto alla legenda (solo Temp./Umidità): linee ogni 1 °C / 10 % calcolate in un Web Worker
//             dalla stessa griglia del velo (marching squares); due livelli si danno il cambio (l'ora dopo già caricata),
//             il velo a fasce cambia solo quando le linee dell'ora sono pronte: stesso fotogramma; etichette da fermi
//   prestazioni  ?perf=1 accende il pannello di misura (js/prestazioni.js); velo su sorgente canvas, scie a 60 passi/s
//   tocco     sulla mappa apre il vento del punto all'ora mostrata (velocità M2 ~1 km, direzione delle scie), come nel 2D
//   Evento    cerca nelle prossime ore il tratto con raffiche stimate (1,5 × vento medio M2, la regola della scheda
//             48 h) ≥ 50 km/h su una zona del Lazio a terra e sotto i 1000 m, lo inquadra e fa scorrere le ore
//   Il centro della vista resta a 300 m (× rilievo) invece di appoggiarsi al terreno: in pianura le scie non "volano".
// Impostazioni prese dal sito: "Stazioni sulla mappa" e km/h | nodi. La ricerca (lente) vola sul luogo scelto.
(function () {
  'use strict';

  const ML = 'https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl';
  const EXAG = 1.5, PIVOT = 300 * EXAG;
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
    const S = new Int16Array(NT * n);   // velocità M2 a piena risoluzione (~1 km), per il valore al tocco
    const sp = Int16Array.from(s0), di = Int16Array.from(r0);
    for (let k = 0; k < ntt; k++) {
      if (k > 0) {
        for (let c = 0; c < n; c++) sp[c] += sd[(k - 1) * n + c];
        for (let c = 0; c < m; c++) di[c] = ((di[c] + rd[(k - 1) * m + c]) % nd + nd) % nd;
      }
      if (k < k0) continue;
      S.set(sp, (k - k0) * n);
      const o = (k - k0) * m;
      for (let r = 0; r < j.dir_ny; r++) for (let c = 0; c < j.dir_nx; c++) {
        const ms = sp[(r * 2) * j.nx + c * 2] * j.passo / 3.6, a = di[r * j.dir_nx + c] * j.passo_dir * Math.PI / 180, i = o + r * j.dir_nx + c;
        U[i] = -ms * Math.sin(a); W[i] = -ms * Math.cos(a);   // direzione = da dove arriva
      }
    }
    const G = { lat0: j.lat_max, dlat: -2 * (j.lat_max - j.lat_min) / (j.ny - 1), lon0: j.lon_min, dlon: 2 * (j.lon_max - j.lon_min) / (j.nx - 1),
                nx: j.dir_nx, ny: j.dir_ny };
    const G1 = { lat0: j.lat_max, dlat: -(j.lat_max - j.lat_min) / (j.ny - 1), lon0: j.lon_min, dlon: (j.lon_max - j.lon_min) / (j.nx - 1),
                 nx: j.nx, ny: j.ny, passo: j.passo };
    return { G, U, W, S, G1, NT, times: j.times.slice(k0) };
  }
  // isolinee: funzioni pure, usate sia nella pagina sia nel Web Worker che prepara tutte le ore (nel worker arrivano col toString)
  function lisciaPura(F, o, n, passo, nx, ny, passate) {   // medie 3×3: le linee e le fasce non seguono i gradini della griglia
    let V = new Float32Array(n); for (let c = 0; c < n; c++) V[c] = F[o + c] * passo;
    for (let p = 0; p < passate; p++) { const W = new Float32Array(n);
      for (let y = 0; y < ny; y++) for (let x = 0; x < nx; x++) { let s = 0, m = 0;
        for (let j = -1; j <= 1; j++) for (let i = -1; i <= 1; i++) { const xx = x + i, yy = y + j; if (xx >= 0 && yy >= 0 && xx < nx && yy < ny) { s += V[yy * nx + xx]; m++; } }
        W[y * nx + x] = s / m; }
      V = W; }
    return V;
  }
  function chaikinPura(c, chiusa) {   // due passate arrotondano gli spigoli lasciati dalle celle
    for (let p = 0; p < 2; p++) { const n = [];
      if (!chiusa) n.push(c[0]);
      for (let i = 0; i < c.length - 1; i++) { const a = c[i], b = c[i + 1];
        n.push([.75 * a[0] + .25 * b[0], .75 * a[1] + .25 * b[1]], [.25 * a[0] + .75 * b[0], .25 * a[1] + .75 * b[1]]); }
      if (chiusa) n.push(n[0]); else n.push(c[c.length - 1]);
      c = n; }
    return c;
  }
  function semplificaPura(c, tol) {   // via i punti più vicini di tol al precedente tenuto (gli estremi restano)
    const o = [c[0]]; let p = c[0];
    for (let i = 1; i < c.length - 1; i++) { const q = c[i]; if (Math.abs(q[0] - p[0]) + Math.abs(q[1] - p[1]) >= tol) { o.push(q); p = q; } }
    o.push(c[c.length - 1]); return o;
  }
  function lineePure(V, g, pa, mg, isT) {   // marching squares su ogni livello, segmenti uniti in linee
    const { nx, ny, lat0, dlat, lon0, dlon } = g;
    let lo = Infinity, hi = -Infinity; for (const v of V) { if (v < lo) lo = v; if (v > hi) hi = v; }
    const M = 6, feats = [];   // M: margine dentro la sfumatura del bordo
    for (let L = Math.ceil(lo / pa) * pa; L <= hi; L += pa) {
      const pt = new Map(), seg = new Map();   // punto su ogni lato attraversato, e i lati collegati
      const lato = (x, y, oriz) => { const id = (y * nx + x) * 2 + (oriz ? 0 : 1);
        if (!pt.has(id)) { const a = V[y * nx + x], b = oriz ? V[y * nx + x + 1] : V[(y + 1) * nx + x], f = (L - a) / (b - a);
          pt.set(id, oriz ? [lon0 + (x + f) * dlon, lat0 + y * dlat] : [lon0 + x * dlon, lat0 + (y + f) * dlat]); }
        return id; };
      const unisci = (a, b) => { (seg.get(a) || seg.set(a, []).get(a)).push(b); (seg.get(b) || seg.set(b, []).get(b)).push(a); };
      for (let y = M; y < ny - 1 - M; y++) for (let x = M; x < nx - 1 - M; x++) {
        const a = V[y * nx + x], b = V[y * nx + x + 1], c = V[(y + 1) * nx + x + 1], d = V[(y + 1) * nx + x];
        const cas = (a >= L) | (b >= L) << 1 | (c >= L) << 2 | (d >= L) << 3;
        if (cas === 0 || cas === 15) continue;
        const N = () => lato(x, y, true), E = () => lato(x + 1, y, false), S = () => lato(x, y + 1, true), O = () => lato(x, y, false);
        switch (cas) {
          case 1: case 14: unisci(O(), N()); break;  case 2: case 13: unisci(N(), E()); break;
          case 3: case 12: unisci(O(), E()); break;  case 4: case 11: unisci(E(), S()); break;
          case 6: case 9: unisci(N(), S()); break;   case 7: case 8: unisci(O(), S()); break;
          case 5: case 10: { const cen = (a + b + c + d) / 4 >= L;   // sella: decide il centro della cella
            if ((cas === 5) === cen) { unisci(O(), S()); unisci(N(), E()); } else { unisci(O(), N()); unisci(E(), S()); } } }
      }
      const fatto = new Set(), catena = s0 => { const c = [pt.get(s0)]; fatto.add(s0); let cur = s0;
        for (;;) { const q = (seg.get(cur) || []).find(z => !fatto.has(z)); if (q == null) break; fatto.add(q); c.push(pt.get(q)); cur = q; }
        return c; };
      const linee = [];   // dai capi aperti prima, poi gli anelli (niente anellini di rumore)
      const tol = Math.abs(dlon) * .35;   // ~350 m fra un punto e l'altro: Chaikin arrotonda, poi si sfoltisce
      for (const [id, v] of seg) if (v.length === 1 && !fatto.has(id)) { const c = catena(id); if (c.length > 4) linee.push(semplificaPura(chaikinPura(c, false), tol)); }
      for (const id of seg.keys()) if (!fatto.has(id)) { const c = catena(id); if (c.length > 14) { c.push(c[0]); linee.push(semplificaPura(chaikinPura(c, true), tol)); } }
      const e = isT ? `${Math.round(L)}°` : `${Math.round(L)}%`, mag = Math.abs(L / mg - Math.round(L / mg)) < 1e-6;
      for (const c of linee) if (c.length > 3) feats.push({ type: 'Feature', properties: { e, m: mag ? 1 : 0 }, geometry: { type: 'LineString', coordinates: c } });
    }
    return feats;
  }
  // il worker calcola tutte le ore una volta e le tiene compatte (Float32); la pagina chiede solo l'ora che le serve
  const ISO_WORKER = [lisciaPura, chaikinPura, semplificaPura, lineePure].map(f => f.toString()).join('\n') + `
const ORE = new Map();
self.onmessage = e => { const d = e.data;
  if (d.tipo === 'calcola') {
    for (const k of d.ks) { const V = lisciaPura(d.F, k * d.n, d.n, d.passo, d.g.nx, d.g.ny, d.passate);
      ORE.set(k, lineePure(V, d.g, d.pa, d.mg, d.isT).map(x => ({ e: x.properties.e, m: x.properties.m, c: Float32Array.from(x.geometry.coordinates.flat()) }))); }
    self.postMessage({ tipo: 'pronto' });
  } else if (d.tipo === 'ora') {
    const features = (ORE.get(d.k) || []).map(x => { const co = []; for (let i = 0; i < x.c.length; i += 2) co.push([x.c[i], x.c[i + 1]]);
      return { type: 'Feature', properties: { e: x.e, m: x.m }, geometry: { type: 'LineString', coordinates: co } }; });
    self.postMessage({ tipo: 'ora', id: d.id, fc: { type: 'FeatureCollection', features } });
  } };`;

  async function griglia(key) {
    const j = await (await fetch(FILE_VAR[key])).json();
    if (j.versione !== 1) throw new Error('griglia ' + key);
    const [b0, b1] = await Promise.all([j.b64_prima, j.b64_variazioni].map(inflate));
    const n = j.nx * j.ny, nt = j.times.length, F = new Int16Array(n * nt);
    F.set(new Int16Array(b0.buffer, b0.byteOffset, n));
    const d = new Int8Array(b1.buffer, b1.byteOffset, n * (nt - 1));
    for (let k = 1; k < nt; k++) { const o = k * n, p = o - n, q = (k - 1) * n; for (let c = 0; c < n; c++) F[o + c] = F[p + c] + d[q + c]; }
    let lo = j.t_min, hi = j.t_max;
    if (key === 't' && hi - lo < 15) { const m = (lo + hi) / 2; lo = m - 7.5; hi = m + 7.5; }   // come ensureMinSpan del 2D
    return { F, n, passo: j.passo, lo, hi, ore: new Map(j.times.map((t, k) => [Date.parse(t), k])),
      g: { lat0: j.lat_max, dlat: -(j.lat_max - j.lat_min) / (j.ny - 1), lon0: j.lon_min, dlon: (j.lon_max - j.lon_min) / (j.nx - 1), nx: j.nx, ny: j.ny } };
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
  /* stessi colori dell'app anche da PC, dove i token .is-pwa non ci sono */
  html.vista-3d { --glass: rgba(20, 22, 34, .86); --border: rgba(255, 255, 255, .12); --text: #e9e9ed; --text-3: rgba(233, 233, 237, .55);
    --text-off: rgba(233, 233, 237, .52); --accent: #9184d9; --accent-on: #14121f; }
  #map3d { position: fixed; inset: 0; z-index: 500; background: #0b0f17; display: none; }
  html.vista-3d #control-panel .layer-toggle:not(#layer3d), html.vista-3d #fc48-timeline, html.vista-3d #radar-timeline { display: none !important; }
  html.vista-3d #map3d { display: block; }
  html.vista-3d #map, html.vista-3d #dock, html.vista-3d .temp-legend, html.vista-3d #legend-ctl { visibility: hidden !important; }
  html.vista-3d #arrow-toggle { display: none !important; }
  #qualita3d-toggle { display: none !important; }
  html.vista-3d:not(.is-pwa) #qualita3d-toggle { display: flex !important; }
  #map3d .maplibregl-ctrl-attrib { font-size: 10px; }
  #map3d .maplibregl-popup-content { background: var(--glass); color: var(--text); border: .5px solid var(--border); border-radius: 10px;
    box-shadow: 0 6px 24px rgba(0, 0, 0, .5); padding: 8px 12px; font-size: 12px; line-height: 1.5;
    backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px); }
  #map3d .maplibregl-popup-anchor-bottom .maplibregl-popup-tip { border-top-color: var(--glass); }
  #map3d .maplibregl-popup-anchor-top .maplibregl-popup-tip { border-bottom-color: var(--glass); }
  #map3d .punto3d .k { color: var(--text-3); }
  #map3d .meteo-popup3d .maplibregl-popup-content { padding: 10px 26px 10px 12px; line-height: 1.6; }
  #map3d .meteo-popup3d .maplibregl-popup-close-button { color: var(--text-3); font-size: 18px; right: 4px; top: 4px; background: none; }
  #map3d .meteo-popup3d .maplibregl-popup-close-button:hover { color: var(--text); }
  #map3d .meteo-popup3d .pp-link { padding-right: 0; }
  #map3d .meteo-popup3d .pp:not(.pp-min) b { font-size: inherit; }
  #map3d .punto3d b { font-size: 18px; font-weight: 600; letter-spacing: -.01em; }
  #map3d .punto3d small { display: block; color: var(--text-3); }
  #map3d .punto3d i { color: var(--text-off); }
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
  #dock3d .radar-play-btn { width: 34px; height: 34px; flex: 0 0 auto; border-radius: 50%; border: none; background: var(--accent); color: var(--accent-on);
    font-size: 12px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
  #dock3d .radar-track-wrap { position: relative; flex: 1; height: 18px; display: flex; align-items: center; }
  #dock3d .radar-slider { -webkit-appearance: none; appearance: none; width: 100%; height: 5px; border-radius: 3px; background: rgba(255, 255, 255, .25); outline: none; cursor: pointer; margin: 0; }
  #dock3d .radar-slider::-webkit-slider-thumb { -webkit-appearance: none; width: 18px; height: 18px; border-radius: 50%; background: #fff; border: 2px solid var(--accent); cursor: pointer; }
  #dock3d .radar-slider::-moz-range-thumb { width: 18px; height: 18px; border-radius: 50%; background: #fff; border: 2px solid var(--accent); cursor: pointer; }
  #dock3d .fc48-now { flex: 0 0 88px; width: 88px; text-align: right; white-space: nowrap; font-weight: 600; font-size: 12px; color: var(--text); font-variant-numeric: tabular-nums; }
  #dock3d .rail-item { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px; background: transparent; border: none;
    cursor: pointer; color: var(--text-off); font-family: inherit; }
  #dock3d .rail-item svg { width: 20px; height: 20px; }
  #dock3d .rail-item span { font-weight: 500; }
  #dock3d .leg3d { display: flex; align-items: center; gap: 8px; margin-top: -3px; }
  #dock3d .leg3d i { flex: 1; height: 5px; border-radius: 3px; background: linear-gradient(90deg,#5c6e8f,#b3c7e0 25%,#f2f5fa 50%,#ffc46b 75%,#ff6b5e); }
  #dock3d .leg3d span { font: 400 10px ui-monospace, "SF Mono", monospace; color: rgba(233, 233, 237, .7); white-space: nowrap; }
  #dock3d .rail3d { display: flex; padding: 3px; border-radius: 14px; background: rgba(0, 0, 0, .28); }
  #dock3d .rail3d .rail-item { flex: 1; width: auto; height: 50px; border-radius: 11px; }
  #dock3d .rail3d .rail-item.active { background: var(--accent); color: var(--accent-on); }
  #dock3d .rail3d .rail-item span { font-size: 10.5px; }
  /* da PC le sezioni stanno nel pannello in alto, come nel 2D; il dock tiene solo tempo e legenda */
  #layer3d { display: none !important; }
  html.vista-3d:not(.is-pwa) #control-panel #layer3d { display: flex !important; }
  html.vista-3d:not(.is-pwa) #dock3d .rail3d { display: none; }
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
  const icona = inner => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round">${inner}</svg>`;
  const ICON_VENTO = icona('<path d="M3 8h11a3 3 0 100-6"></path><path d="M3 12h15a3.5 3.5 0 110 7"></path><path d="M3 16h7a2.5 2.5 0 110 5"></path>');
  const ICON_TEMP = icona('<path d="M14 14.8V5a2 2 0 10-4 0v9.8a4 4 0 104 0z"></path>');
  const ICON_GOCCIA = icona('<path d="M12 3s6 6.4 6 10.4A6 6 0 016 13.4C6 9.4 12 3 12 3z"></path>');
  const ICON_EVENTO = icona('<path d="M4 8V5a1 1 0 011-1h3M16 4h3a1 1 0 011 1v3M20 16v3a1 1 0 01-1 1h-3M8 20H5a1 1 0 01-1-1v-3"></path><circle cx="12" cy="12" r="2.6"></circle>');
  // palette e scale della mappa 2D (app.js), per avere gli stessi colori
  const PAL = {
    t: [[0, 0x2c, 0x3e, 0x95], [.125, 0x3a, 0x6f, 0xc4], [.25, 0x4f, 0xb8, 0xc4], [.375, 0x6f, 0xc4, 0x6a], [.5, 0xd4, 0xd2, 0x4a],
        [.625, 0xf4, 0xa9, 0x3f], [.75, 0xe8, 0x54, 0x2f], [.875, 0xa5, 0x00, 0x26], [1, 0x67, 0x00, 0x1f]],
    rh: [[0, 0xd9, 0x6f, 0x27], [.3, 0xfe, 0xe0, 0x80], [.5, 0xb0, 0xe0, 0x90], [.75, 0x31, 0x7e, 0xc8], [1, 0x08, 0x30, 0x6b]] };
  const FILE_VAR = { t: 'data/griglia_48h.json', rh: 'data/griglia_umidita_48h.json' };
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
        <div class="rail3d"><button class="rail-item active" data-var="ws" type="button">${ICON_VENTO}<span>Vento</span></button>
          <button class="rail-item" data-var="t" type="button">${ICON_TEMP}<span>Temp.</span></button>
          <button class="rail-item" data-var="rh" type="button">${ICON_GOCCIA}<span>Umidità</span></button>
          <button class="rail-item" id="evento3d" type="button">${ICON_EVENTO}<span>Evento</span></button></div>
      </div>`);
  }

  // ── la vista ──────────────────────────────────────────────────────────────
  async function crea() {
    await maplibre();
    const [VM, QQ, ST] = await Promise.all([vento(), quote(), stazioni()]);
    interfaccia();
    const { G, U, W, S, G1, NT } = VM, { qm, Q } = QQ, GN = G.nx * G.ny;
    const $ = id => document.getElementById(id);
    const unitaKts = () => !!document.querySelector('input[name="wind-unit"][value="kts"]')?.checked;
    const vU = kmh => Math.round(unitaKts() ? kmh * KTS : kmh), nomeU = () => (unitaKts() ? 'nodi' : 'km/h');
    const stazioniOn = () => document.getElementById('stations-check')?.checked !== false;
    const dem = new URL('3d/dem/{z}/{x}/{y}.webp', location.href).href.replace(/%7B/g, '{').replace(/%7D/g, '}');

    // densità di pixel della mappa: al massimo 1,5× (sugli schermi a 2–3× il rilievo 3D ridisegnato a ogni ora faceva scattare;
    // a 1,5× sul Mac 59 fps e nessuno scatto, 21/11/2026). ?pr=2 per provare la piena densità
    // densità dei pixel: 1,5 di base (a 2× ogni cambio d'ora ridisegnava il terreno con uno scatto);
    // "Qualità mappa" alta (impostazioni, solo PC) usa tutta la densità dello schermo. ?pr= vince su tutto (prove).
    const PR_URL = +new URLSearchParams(location.search).get('pr') || 0;
    let ALTA = false; try { ALTA = localStorage.getItem('metek3d-qualita') === 'alta'; } catch (e) {}
    const densita = () => Math.min(devicePixelRatio || 1, PR_URL || (ALTA ? 4 : 1.5));
    const PR = densita();
    // qualità alta: tessere del rilievo e del terreno chieste un livello più fini (tileSize 128 = un livello di zoom in più
    // a parità di vista): più dettaglio su creste e ombre, ma circa 4 volte le tessere da scaricare e tenere in memoria
    const TS = () => (ALTA && !PR_URL ? 128 : 256);
    const sorgDem = () => ({ type: 'raster-dem', tiles: [dem], encoding: 'terrarium', tileSize: TS(), minzoom: 6, maxzoom: 11, bounds: [11.3, 41.0, 14.7, 43.0] });
    const sorgRil = () => ({ type: 'raster', tiles: [dem.replace('/dem/', '/rilievo/')], tileSize: TS(), minzoom: 6, maxzoom: 11, bounds: [11.3, 41.0, 14.7, 43.0] });
    const map = new maplibregl.Map({
      container: 'map3d', attributionControl: { compact: true }, pixelRatio: PR, center: [12.72, 41.98], zoom: innerWidth > 700 ? 8.4 : 7.7, pitch: 58, bearing: -10,   // da PC il Lazio riempie lo schermo maxPitch: 80,
      style: {
        version: 8, glyphs: 'https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf',
        sources: {
          base: { type: 'vector', url: 'https://tiles.openfreemap.org/planet',
            attribution: '© OpenStreetMap · OpenFreeMap · rilievo Copernicus · vento M2 Metek' },
          dem: sorgDem(),
          // rilievo cotto (tools/rilievo_cotto.py): più luci, occlusione ambientale, ombre portate morbide
          rilievo: sorgRil()
        },
        layers: [
          { id: 'terra', type: 'background', paint: { 'background-color': '#2b3a32' } },
          { id: 'abitati', type: 'fill', source: 'base', 'source-layer': 'landuse', filter: ['in', ['get', 'class'], ['literal', ['residential', 'suburb', 'neighbourhood', 'industrial', 'commercial']]],
            paint: { 'fill-color': '#2a3242', 'fill-opacity': ['interpolate', ['linear'], ['zoom'], 7, .5, 11, .9] } },
          { id: 'mare', type: 'fill', source: 'base', 'source-layer': 'water', paint: { 'fill-color': '#0e2448' } },
          { id: 'fiumi', type: 'line', source: 'base', 'source-layer': 'waterway', filter: ['==', ['get', 'class'], 'river'],
            paint: { 'line-color': '#1d3f70', 'line-width': ['interpolate', ['linear'], ['zoom'], 8, .6, 12, 1.6] } },
          { id: 'rilievo', type: 'raster', source: 'rilievo', paint: { 'raster-fade-duration': 0, 'raster-resampling': 'linear', 'raster-opacity': .6 } },   // .6 col vento, piena sui veli
          { id: 'strade', type: 'line', source: 'base', 'source-layer': 'transportation', filter: ['in', ['get', 'class'], ['literal', ['motorway', 'trunk']]],
            paint: { 'line-color': '#3b4558', 'line-width': ['interpolate', ['linear'], ['zoom'], 8, .5, 12, 1.4] } },
          { id: 'confini', type: 'line', source: 'base', 'source-layer': 'boundary', filter: ['all', ['<=', ['get', 'admin_level'], 4], ['!=', ['get', 'maritime'], 1]],
            paint: { 'line-color': '#59647a', 'line-width': .8, 'line-dasharray': [3, 2] } }
        ],
        terrain: { source: 'dem', exaggeration: EXAG },
        sky: { 'sky-color': '#0a1424', 'horizon-color': '#2a3a58', 'fog-color': '#121a2a', 'sky-horizon-blend': .7, 'horizon-fog-blend': .6, 'fog-ground-blend': .45 }
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
    let T = 0, VAR = 'ws', TENUE = 1;   // variabile mostrata: ws (scie), t o rh (velo colorato, scie tenui)
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
    const RAMP = [[0, [.58, .66, .8], .6], [.25, [.84, .9, .97], .82], [.5, [.97, .98, 1], .92], [.75, [1, .77, .42], .97], [1, [1, .42, .37], 1]];
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
          const c = ramp(P.spd[i] / 60), life = Math.min(1, P.age[i] / 10, (P.life[i] - P.age[i]) / 15) * c[3] * TENUE;
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
    // zone di avviso: si ridisegnano solo durante l'Evento (prima anche da spente, a ogni ora: una tela sfocata + PNG per niente)
    const avvisiOra = () => { const src = map.getSource('avvisi'); if (!src) return;
      if (avvisiOra.on !== EV_ON) { avvisiOra.on = EV_ON; oraAvv = -1; map.setLayoutProperty('avvisi', 'visibility', EV_ON ? 'visible' : 'none'); }
      if (EV_ON && Math.round(T) !== oraAvv) src.updateImage({ url: avvisiImg() }); };
    const angoli = g => { const la = [g.lat0, g.lat0 + g.dlat * (g.ny - 1)], lo = [g.lon0, g.lon0 + g.dlon * (g.nx - 1)];
      const N = Math.max(...la) + Math.abs(g.dlat) / 2, S = Math.min(...la) - Math.abs(g.dlat) / 2, Wl = Math.min(...lo) - Math.abs(g.dlon) / 2, E = Math.max(...lo) + Math.abs(g.dlon) / 2;
      return [[Wl, N], [E, N], [E, S], [Wl, S]]; };

    // pillole delle stazioni, ora per ora, nell'unità scelta
    const freccia = wd => `<svg width="10" height="10" viewBox="0 0 16 16" style="transform:rotate(calc(${Math.round((wd ?? 0) + 180)}deg - var(--rot3d, 0deg)))"><path d="M8 1 13 13 8 10 3 13z" fill="currentColor"/></svg>`;
    const markers = ST.map(s => {
      const el = document.createElement('div'); el.className = 'st-pin'; el.title = s.name;
      const pop = new maplibregl.Popup({ offset: 12, closeButton: false });
      const mk = new maplibregl.Marker({ element: el, anchor: 'center', opacityWhenCovered: '1' }).setLngLat([s.lon, s.lat]).setPopup(pop);
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
    // scie a tempo, non a fotogramma: un passo ogni 1/60 s (prima su uno schermo a 120 Hz andavano al doppio e la mappa
    // si ridisegnava 120 volte al secondo); fotogrammi al massimo FPS (?fps=30 per provare), i passi persi si recuperano
    const FPS = Math.max(15, Math.min(120, +new URLSearchParams(location.search).get('fps') || 60));
    let ultimo = 0;
    const tick = (now = performance.now()) => {
      if (!tickOn) return; requestAnimationFrame(tick);
      if (ultimo && now - ultimo < 1000 / FPS - 2) return;
      const passi = ultimo ? Math.max(1, Math.min(4, Math.round((now - ultimo) / (1000 / 60)))) : 1; ultimo = now;
      for (let i = 0; i < passi; i++) step();
      map.triggerRepaint(); };
    // i nostri strati appena lo stile è pronto: 'load' aspetta anche l'ultima tessera, e se una richiesta resta appesa
    // (Brave, rete lenta) nomi, scie e veli non arrivavano mai
    const T0 = performance.now(); window.Metek3D_t = { stile: null, carica: null };
    map.once('load', () => { window.Metek3D_t.carica = Math.round(performance.now() - T0); });
    map.once('style.load', () => { window.Metek3D_t.stile = Math.round(performance.now() - T0); });
    map.once('style.load', () => {
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
        map.addLayer({ id: 'montagne', type: 'raster', source: 'montagne', paint: { 'raster-fade-duration': 0 } }, 'rilievo'); }
      map.addSource('clima', { type: 'canvas', canvas: cvc, animate: false, coordinates: [[11.4, 42.85], [14.05, 42.85], [14.05, 41.18], [11.4, 41.18]] });
      map.addLayer({ id: 'clima', type: 'raster', source: 'clima', layout: { visibility: VAR === 'ws' ? 'none' : 'visible' },   // scelta fatta prima del load
        paint: { 'raster-fade-duration': 0, 'raster-opacity': .7, 'raster-resampling': 'linear', 'raster-saturation': -.12 } }, 'rilievo');
      for (const id of ['isoA', 'isoB']) {
        map.addSource(id, { type: 'geojson', data: VUOTO, tolerance: .4 });
        map.addLayer({ id, type: 'line', source: id, layout: { 'line-join': 'round' },
          paint: { 'line-color': ['case', ['==', ['get', 'm'], 1], 'rgba(246,243,236,.95)', 'rgba(246,243,236,.5)'], 'line-opacity': 0,
            'line-width': ['interpolate', ['linear'], ['zoom'], 7, ['case', ['==', ['get', 'm'], 1], 1.1, .5], 11, ['case', ['==', ['get', 'm'], 1], 2, 1]] } }, 'strade'); }
      map.addSource('iso', { type: 'geojson', data: VUOTO, tolerance: .6 });
      map.addLayer({ id: 'iso-e', type: 'symbol', source: 'iso', filter: ['==', ['get', 'm'], 1],
        layout: { visibility: 'none', 'symbol-placement': 'line', 'symbol-spacing': 260, 'text-field': ['get', 'e'], 'text-font': ['literal', ['Noto Sans Medium']],
          'text-size': 11, 'text-keep-upright': true, 'text-max-angle': 35 },
        paint: { 'text-color': '#ffffff', 'text-halo-color': 'rgba(8,11,18,.8)', 'text-halo-width': 1.3 } }, 'paesi');
      map.addSource('avvisi', { type: 'image', url: avvisiImg(), coordinates: angoli(G) });
      map.addLayer({ id: 'avvisi', type: 'raster', source: 'avvisi', paint: { 'raster-fade-duration': 0 } }, 'paesi');
      map.addLayer(layer, 'paesi');
      const vista = () => { const bb = map.getBounds(), c = map.getCenter(), r = Math.min(2.5, 1.6 * Math.pow(2, 8.5 - map.getZoom())), cl = Math.cos(c.lat * Math.PI / 180);
        VIEW = [Math.max(bb.getWest(), c.lng - r / cl), Math.max(bb.getSouth(), c.lat - r), Math.min(bb.getEast(), c.lng + r / cl), Math.min(bb.getNorth(), c.lat + r)]; };
      map.on('moveend', vista); vista();
      const lontano = () => map.getContainer().classList.toggle('lontano', map.getZoom() < 9.2);
      map.on('zoom', lontano); lontano();
      pillole(); pilloleOra(true);
      if (VAR !== 'ws') { map.setLayoutProperty('montagne', 'visibility', 'none'); oraClima = null; climaOra(); oraIso = null; isoOra(); }
      map.getContainer().querySelector('.maplibregl-ctrl-attrib')?.classList.remove('maplibregl-compact-show');   // crediti chiusi, si aprono con la (i)
    });

    // barra oraria (stessa card della mappa 2D) e legenda nell'unità scelta
    const ti = $('ora3d-slider'), PB = $('play3d'); ti.max = NT - 1;
    const LEG_VENTO = getComputedStyle(document.querySelector('#dock3d .leg3d i')).backgroundImage;
    const legenda = () => {
      const D = DATI[VAR], [a, b] = document.querySelectorAll('#dock3d .leg3d span'), bar = document.querySelector('#dock3d .leg3d i');
      if (VAR === 'ws' || !D) { a.textContent = '0'; b.textContent = `${vU(60)}+ ${unitaKts() ? 'kts' : 'km/h'}`; bar.style.backgroundImage = LEG_VENTO; return; }
      const f = v => VAR === 't' ? v.toFixed(0) + '°' : Math.round(v) + '%';
      a.textContent = f(D.lo); b.textContent = f(D.hi);
      bar.style.backgroundImage = `linear-gradient(90deg,${PAL[VAR].map(([t, r, g, bb]) => `rgb(${r},${g},${bb}) ${t * 100}%`).join(',')})`;
    };
    let aggiornaPunto = () => {};   // il popup del punto segue l'ora (definito sotto)

    // temperatura e umidità: stessi file del 2D (già nella cache del browser), un'immagine per ora tenuta in memoria
    const DATI = {}, cvc = document.createElement('canvas'); cvc.width = cvc.height = 1;
    // bordi sfumati (~12 km) invece del taglio netto del riquadro dei dati
    let BORDO = null;
    const bordo = g => BORDO || (BORDO = Uint8Array.from({ length: g.nx * g.ny }, (_, c) => {
      const x = c % g.nx, y = (c / g.nx) | 0, d = Math.min(x, y, g.nx - 1 - x, g.ny - 1 - y) / 12, e = Math.min(1, d);
      return 255 * e * e * (3 - 2 * e); }));
    let oraClima = null;
    // isolinee (opzione): ogni 1 °C / 10 %, principali ogni 5 °C / 20 % con l'etichetta
    let ISO = false; try { ISO = localStorage.getItem('metek3d-iso') === '1'; } catch (e) {}
    let VENTO = true; try { VENTO = localStorage.getItem('metek3d-vento') !== '0'; } catch (e) {}   // scie sopra Temp. e Umidità
    const PASSO_ISO = { t: 1, rh: 10 }, MAGG_ISO = { t: 5, rh: 20 }, LISCIA = { t: 1, rh: 2 }, VUOTO = { type: 'FeatureCollection', features: [] };
    // campo lisciato con medie 3×3 (1 passata la temperatura, 2 l'umidità, più rumorosa): le linee e le fasce non seguono i gradini della griglia
    const CAMPI = new Map();
    function campo(key, k) {
      const ch = key + k; if (CAMPI.has(ch)) return CAMPI.get(ch);
      const D = DATI[key], V = lisciaPura(D.F, k * D.n, D.n, D.passo, D.g.nx, D.g.ny, LISCIA[key]);
      if (CAMPI.size > 8) CAMPI.delete(CAMPI.keys().next().value);
      CAMPI.set(ch, V); return V;
    }
    let oraIso = null;
    // le etichette (simboli MapLibre, aggiornati a parte) solo da fermi: col play sarebbero in ritardo sulle linee
    // linee: due livelli che si danno il cambio. Uno mostra l'ora, l'altro ha già caricato la successiva; al cambio d'ora
    // si scambia l'opacità (costante: immediata, nello stesso fotogramma del velo; a 0 MapLibre non disegna il livello)
    // e nel livello rimasto libero si carica l'ora dopo. Le linee di tutte le ore le calcola prima un Web Worker.
    const ISO_PRONTE = {}, BUF = { isoA: null, isoB: null }; let davanti = 'isoA', timerEt = 0;
    const dietro = () => davanti === 'isoA' ? 'isoB' : 'isoA';
    const caricaIn = (id, key, k) => { const b = BUF[id] = { ch: key + k, pronto: false };
      chiedi(key, k).then(fc => { if (b !== BUF[id]) return; b.fc = fc; map.getSource(id).setData(fc); attendi(); });
      const attendi = () => {
      // pronta quando le sue tessere sono rifatte (o dopo 400 ms, se nessuna è in vista)
      const ok = () => { if (b.pronto) return; b.pronto = true; map.off('sourcedata', f); if (b === BUF[id]) { climaOra(); isoOra(); } };
      const f = e => { if (e.sourceId === id && e.tile && map.isSourceLoaded(id)) ok(); };
      map.on('sourcedata', f); setTimeout(ok, 400); }; };
    // colori e linee della stessa ora: il velo cambia solo quando le linee dell'ora sono pronte (stesso fotogramma)
    const lineePronte = (key, k) => { const v = key + k; return BUF[davanti]?.ch === v && BUF[davanti].pronto || BUF[dietro()]?.ch === v && BUF[dietro()].pronto; };
    const ISO_W = {}, ATTESE = new Map(); let richiesta = 0;
    function preparaIso(key) {   // tutte le ore in un Web Worker che resta acceso: la pagina resta libera
      if (ISO_PRONTE[key]) return;
      ISO_PRONTE[key] = 'in corso';
      const D = DATI[key], ks = [...new Set(D.ore.values())].sort((a, b) => a - b);
      let w;
      try { w = new Worker(URL.createObjectURL(new Blob([ISO_WORKER], { type: 'text/javascript' }))); }
      catch (e) { console.warn('isolinee: worker non disponibile', e); ISO_PRONTE[key] = false; return; }
      w.onerror = e => { console.warn('isolinee: errore nel worker', e.message); ISO_PRONTE[key] = false; };
      w.onmessage = e => {
        const d = e.data;
        if (d.tipo === 'pronto') { ISO_W[key] = w; ISO_PRONTE[key] = true; oraClima = null; climaOra(); isoOra(); }
        else if (d.tipo === 'ora') { const f = ATTESE.get(d.id); ATTESE.delete(d.id); if (f) f(d.fc); }
      };
      w.postMessage({ tipo: 'calcola', F: D.F, n: D.n, passo: D.passo, g: D.g, ks, passate: LISCIA[key], pa: PASSO_ISO[key], mg: MAGG_ISO[key], isT: key === 't' });
    }
    const chiedi = (key, k) => new Promise(res => { const id = ++richiesta; ATTESE.set(id, res); ISO_W[key].postMessage({ tipo: 'ora', k, id }); });
    function isoOra() {
      const vis = ISO && VAR !== 'ws' && DATI[VAR];
      const on = vis && !gira && TPRIMA == null;
      if (map.getLayer('iso-e')) map.setLayoutProperty('iso-e', 'visibility', on ? 'visible' : 'none');
      let voglio = null;
      if (vis) {
        const t = Math.min(NT - 1, Math.round(T)), k = DATI[VAR].ore.get(Date.parse(VM.times[t]));
        if (ISO_PRONTE[VAR] === true) voglio = VAR + k; else preparaIso(VAR);
        if (voglio && map.getSource('isoA')) {
          const kd = DATI[VAR].ore.get(Date.parse(VM.times[(t + 1) % NT]));
          if (BUF[davanti]?.ch !== voglio) {
            const d = BUF[dietro()];
            if (d?.ch === voglio && d.pronto) {   // l'ora era già pronta dietro: scambio
              map.setPaintProperty(davanti, 'line-opacity', 0); davanti = dietro(); map.setPaintProperty(davanti, 'line-opacity', 1);
            } else if (d?.ch !== voglio) caricaIn(dietro(), VAR, k);   // salto del cursore: si mostra appena caricata
          } else map.setPaintProperty(davanti, 'line-opacity', 1);
          if (BUF[davanti]?.ch === voglio && BUF[dietro()]?.ch !== VAR + kd) caricaIn(dietro(), VAR, kd);   // l'ora dopo, in anticipo
        }
        const ch = VAR + t, v = VAR;
        // etichette: quando il cursore si ferma (trascinandolo sarebbero lavoro buttato), dalle linee già pronte
        if (on && ch !== oraIso) { clearTimeout(timerEt); timerEt = setTimeout(() => { if (VAR !== v || oraIso === ch) return; oraIso = ch;
          const b = [BUF.isoA, BUF.isoB].find(x => x?.ch === v + k && x.fc); if (b) map.getSource('iso')?.setData(b.fc); else oraIso = null; }, 250); }
      }
      if (!voglio && map.getLayer('isoA')) { map.setPaintProperty('isoA', 'line-opacity', 0); map.setPaintProperty('isoB', 'line-opacity', 0); BUF.isoA = BUF.isoB = null; }
    }
    // velo: una tela letta direttamente dalla mappa (sorgente canvas), niente PNG da codificare e decodificare a ogni ora.
    // Colori da una tabella di 256 tinte; in memoria i pixel delle ultime 12 ore.
    const PIX = new Map(), LUT = {};
    const lut = key => LUT[key] || (LUT[key] = (() => { const P = PAL[key], L = new Uint8Array(768);
      for (let i = 0; i < 256; i++) { const x = i / 255; let j = 1; while (j < P.length - 1 && x > P[j][0]) j++;
        const A = P[j - 1], B = P[j], f = (x - A[0]) / (B[0] - A[0] || 1);
        for (let q = 0; q < 3; q++) L[i * 3 + q] = A[q + 1] + (B[q + 1] - A[q + 1]) * f; }
      return L; })());
    const R_ISO = 2;   // con le isolinee: 2 pixel per cella, fasce che seguono le linee
    function veloDati(key, k) {
      const iso = ISO, ch = key + k + (iso ? 'f' : '');
      if (PIX.has(ch)) { const v = PIX.get(ch); PIX.delete(ch); PIX.set(ch, v); return v; }
      const D = DATI[key], { nx, ny } = D.g, L = lut(key), lo = D.lo, sc = 255 / (D.hi - D.lo || 1);
      bordo(D.g);
      let im;
      if (k == null) im = new ImageData(nx, ny);
      else if (!iso) {
        im = new ImageData(nx, ny); const d = im.data;
        for (let c = 0, o = k * D.n; c < D.n; c++) {
          const i = Math.max(0, Math.min(255, Math.round((D.F[o + c] * D.passo - lo) * sc))) * 3, q = c * 4;
          d[q] = L[i]; d[q + 1] = L[i + 1]; d[q + 2] = L[i + 2]; d[q + 3] = BORDO[c]; }
      } else {
        const V = campo(key, k), pa = PASSO_ISO[key], R = R_ISO, W = nx * R, H = ny * R;
        im = new ImageData(W, H); const d = im.data;
        const xi = new Int32Array(W), xu = new Float32Array(W);
        for (let X = 0; X < W; X++) { const f = Math.min(nx - 1.001, X / R); xi[X] = f | 0; xu[X] = f - (f | 0); }
        for (let Y = 0; Y < H; Y++) {
          const fy = Math.min(ny - 1.001, Y / R), y = fy | 0, w = fy - y, r0 = y * nx, r1 = r0 + nx;
          for (let X = 0, q = Y * W * 4; X < W; X++, q += 4) {
            const x = xi[X], u = xu[X];
            const v = (V[r0 + x] * (1 - u) + V[r0 + x + 1] * u) * (1 - w) + (V[r1 + x] * (1 - u) + V[r1 + x + 1] * u) * w;
            const i = Math.max(0, Math.min(255, Math.round(((Math.floor(v / pa) + .5) * pa - lo) * sc))) * 3;
            d[q] = L[i]; d[q + 1] = L[i + 1]; d[q + 2] = L[i + 2]; d[q + 3] = BORDO[r0 + x]; } }
      }
      PIX.set(ch, im); if (PIX.size > 12) PIX.delete(PIX.keys().next().value);
      return im;
    }
    const gVelo = cvc.getContext('2d', { willReadFrequently: true });   // solo putImageData: tela in memoria, caricata così com'è sulla GPU
    function disegnaVelo(key, k) {
      const t0 = performance.now(), im = veloDati(key, k), t1 = performance.now();
      if (cvc.width !== im.width || cvc.height !== im.height) { cvc.width = im.width; cvc.height = im.height; }
      const g = gVelo; g.putImageData(im, 0, 0);
      const t2 = performance.now(), M = window.Metek3D_t || {};
      M.calcolo = Math.round(t1 - t0); M.disegno = Math.round(t2 - t1);
      const src = map.getSource('clima');   // la tela si ricarica sulla GPU solo per il fotogramma dopo il disegno
      if (src) { src.play(); map.triggerRepaint(); map.once('render', () => { src.pause(); M.fotogramma = Math.round(performance.now() - t2); }); }
    }
    const oraFile = (key, t) => DATI[key].ore.get(Date.parse(VM.times[t]));
    function climaOra() {
      if (VAR === 'ws' || !DATI[VAR]) return;
      const t = Math.min(NT - 1, Math.round(T)), ch = VAR + t + (ISO ? 'f' : '');
      if (ch === oraClima) return;
      if (ISO && ISO_PRONTE[VAR] === true && map.getSource('isoA') && !lineePronte(VAR, oraFile(VAR, t))) { isoOra(); return; }   // si aspetta le linee
      oraClima = ch;
      disegnaVelo(VAR, oraFile(VAR, t));
      // l'ora dopo si prepara nei momenti liberi: col play è già pronta
      const v = VAR, k1 = oraFile(v, (t + 1) % NT), idle = f => window.requestIdleCallback ? requestIdleCallback(f, { timeout: 400 }) : setTimeout(f, 50);
      if (k1 != null) idle(() => { if (VAR === v) veloDati(v, k1); });
    }
    // una griglia per variabile, scaricata una volta sola (anche se la chiedono insieme il cursore e il popup)
    const PROM = {};
    const dati = key => DATI[key] ? Promise.resolve(DATI[key]) : (PROM[key] ||= griglia(key).then(D => (DATI[key] = D)).catch(e => { delete PROM[key]; throw e; }));
    async function scegli(key) {
      if (key !== 'ws' && !DATI[key]) {
        try { await dati(key); } catch (e) { console.warn('griglia 3D non caricata', key, e); return; }
      }
      VAR = key; TENUE = key === 'ws' ? 1 : VENTO ? .35 : 0; oraClima = null;
      document.querySelectorAll('#dock3d .rail-item[data-var]').forEach(b => b.classList.toggle('active', b.dataset.var === key));
      const vis = key === 'ws' ? 'none' : 'visible';
      if (map.getLayer('clima')) { map.setLayoutProperty('clima', 'visibility', vis); map.setLayoutProperty('montagne', 'visibility', key === 'ws' ? 'visible' : 'none'); }
      if (map.getLayer('rilievo')) map.setPaintProperty('rilievo', 'raster-opacity', key === 'ws' ? .6 : 1);
      interruttori();
      climaOra(); oraIso = null; isoOra(); legenda(); oraPunto = -1; aggiornaPunto();
    }
    // Isolinee e Mostra vento: gli stessi interruttori delle impostazioni del 2D. In 3D hanno uno stato loro
    // (isolinee e scie convivono, nel 2D le linee spengono le particelle): entrando si salva quello del 2D
    // e si mostra il nostro, uscendo si rimette com'era; i cambi fatti in 3D non arrivano al codice del 2D.
    const CC = () => document.getElementById('contour-check'), WC = () => document.getElementById('wind-check');
    let stato2D = null;
    function interruttori() {
      if (!document.documentElement.classList.contains('vista-3d')) return;
      const cc = CC(), wc = WC(), vis = VAR === 'ws' ? 'none' : '';
      if (cc) { cc.checked = ISO; cc.closest('label').style.display = vis; }
      if (wc) { wc.checked = VENTO; wc.disabled = false; const l = wc.closest('label'); l.style.display = vis; l.classList.remove('is-disabled'); }
    }
    // Qualità mappa (solo PC): interruttore nelle impostazioni, accanto a Isolinee e Mostra vento
    const wt = document.getElementById('wind-toggle');
    if (wt && !document.getElementById('qualita3d-toggle') && (devicePixelRatio || 1) > 1.5) {
      wt.insertAdjacentHTML('afterend', `<label class="ctrl-row" id="qualita3d-toggle" title="Più nitida, ma più pesante per la scheda grafica">` +
        `<input type="checkbox" id="qualita3d-check"${ALTA ? ' checked' : ''}><span class="switch"></span><span>Qualità mappa alta</span></label>`);
      document.getElementById('qualita3d-check').addEventListener('change', e => {
        if (e.target.checked && !confirm('Qualità mappa alta: la vista 3D usa tutta la risoluzione dello schermo.\n\n' +
            'Aumentando la risoluzione si rischiano problemi di fluidità (scatti al cambio d\'ora, ventole, batteria), ' +
            'soprattutto con Temperatura, Umidità e Isolinee.\n\nAttivarla?')) { e.target.checked = false; return; }
        ALTA = e.target.checked; try { localStorage.setItem('metek3d-qualita', ALTA ? 'alta' : 'base'); } catch (x) {}
        map.setPixelRatio(densita());
        // terreno e rilievo rifatti con le tessere della nuova qualità (stesso posto nella pila dei livelli)
        const lr = map.getStyle().layers.find(l => l.id === 'rilievo');
        map.setTerrain(null); map.removeLayer('rilievo'); map.removeSource('rilievo'); map.removeSource('dem');
        map.addSource('dem', sorgDem()); map.addSource('rilievo', sorgRil());
        map.addLayer(lr, 'strade'); map.setTerrain({ source: 'dem', exaggeration: EXAG });
      });
    }
    const entra2D = () => { const cc = CC(), wc = WC(); if (stato2D || !cc || !wc) return;
      stato2D = { c: cc.checked, w: wc.checked, wd: wc.disabled, cd: cc.closest('label').style.display, ld: wc.closest('label').style.display,
        li: wc.closest('label').classList.contains('is-disabled') };
      interruttori(); };
    const esci2D = () => { const cc = CC(), wc = WC(); if (!stato2D || !cc || !wc) return; const s = stato2D; stato2D = null;
      cc.checked = s.c; cc.closest('label').style.display = s.cd; wc.checked = s.w; wc.disabled = s.wd;
      const l = wc.closest('label'); l.style.display = s.ld; l.classList.toggle('is-disabled', s.li); };
    document.addEventListener('change', e => {
      if (!stato2D || (e.target.id !== 'contour-check' && e.target.id !== 'wind-check')) return;
      e.stopPropagation();
      if (e.target.id === 'contour-check') {
        ISO = e.target.checked; try { localStorage.setItem('metek3d-iso', ISO ? '1' : '0'); } catch (x) {}
        oraClima = null; climaOra(); oraIso = null; isoOra();
      } else {
        VENTO = e.target.checked; try { localStorage.setItem('metek3d-vento', VENTO ? '1' : '0'); } catch (x) {}
        if (VAR !== 'ws') TENUE = VENTO ? .35 : 0;
      }
    }, true);
    document.querySelectorAll('#dock3d .rail-item[data-var]').forEach(b => b.addEventListener('click', () => { if (b.dataset.var !== VAR) { stop(); scegli(b.dataset.var); } }));
    // da PC: le stesse sezioni nel pannello in alto, con l'aspetto del 2D; ogni pulsante preme quello del dock e ne copia lo stato
    if (!document.documentElement.classList.contains('is-pwa')) {
      const prima = document.querySelector('#control-panel .layer-toggle');
      if (prima && !$('layer3d')) {
        const box = document.createElement('div'); box.className = 'layer-toggle'; box.id = 'layer3d';
        const voci = [['ws', '💨 Vento'], ['t', '🌡️ Temperatura'], ['rh', '💧 Umidità'], ['ev', '🎬 Evento']];
        box.innerHTML = voci.map(([k, l]) => `<button type="button" data-k="${k}">${l}</button>`).join('');
        prima.after(box);
        const gemello = k => k === 'ev' ? $('evento3d') : document.querySelector(`#dock3d .rail-item[data-var="${k}"]`);
        box.addEventListener('click', e => { const b = e.target.closest('button'); if (b) gemello(b.dataset.k)?.click(); });
        const copia = () => box.querySelectorAll('button').forEach(b => b.classList.toggle('active', !!gemello(b.dataset.k)?.classList.contains('active')));
        new MutationObserver(copia).observe(document.querySelector('#dock3d .rail3d'), { subtree: true, attributes: true, attributeFilter: ['class'] });
        copia();
      }
    }
    const showT = () => {
      $('ora3d').textContent = new Date(VM.times[Math.min(NT - 1, Math.round(T))]).toLocaleString('it-IT', { timeZone: 'Europe/Rome', weekday: 'short', hour: '2-digit', minute: '2-digit' });
      pilloleOra(); avvisiOra(); climaOra(); isoOra(); aggiornaPunto();
    };
    let gira = false, tPrec = 0, TPRIMA = null;   // TPRIMA: ora da riprendere a fine Evento
    const pausa = () => { gira = false; PB.textContent = '▶'; isoOra(); };
    ti.addEventListener('input', () => { if (TPRIMA != null) stop(false); pausa(); T = +ti.value; showT(); });   // la barra toccata ferma l'Evento lì
    PB.addEventListener('click', () => { if (TPRIMA != null) return stop(); if (gira) return pausa(); gira = true; PB.textContent = '❚❚'; isoOra(); tPrec = performance.now();
      const f = now => { if (!gira) return; T += (now - tPrec) / 1000 * 1.2; tPrec = now; if (T > NT - 1) T = 0; ti.value = T; showT(); requestAnimationFrame(f); };
      requestAnimationFrame(f); });
    const setT = t => { T = Math.max(0, Math.min(NT - 1, t)); ti.value = T; showT(); };
    // impostazioni del sito
    document.getElementById('stations-check')?.addEventListener('change', pillole);
    document.querySelectorAll('input[name="wind-unit"]').forEach(r => r.addEventListener('change', () => { legenda(); pilloleOra(true); oraPunto = -1; aggiornaPunto(); }));
    legenda(); showT();

    // tocco sulla mappa: vento del punto all'ora mostrata, come il popup del 2D (velocità M2 a ~1 km, direzione delle scie)
    const SETTORI = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSO', 'SO', 'OSO', 'O', 'ONO', 'NO', 'NNO'];
    const NOMI = { N: 'Tramontana', NNE: 'Bora', NE: 'Grecale', ENE: 'Schiavo', E: 'Levante', ESE: 'Solano', SE: 'Scirocco', SSE: 'Africo',
      S: 'Ostro', SSO: 'Cauro', SO: 'Libeccio', OSO: 'Etesia', O: 'Ponente', ONO: 'Traversone', NO: 'Maestrale', NNO: 'Zefiro' };
    function velocita(lon, lat, t) {
      const fx = (lon - G1.lon0) / G1.dlon, fy = (lat - G1.lat0) / G1.dlat;
      if (fx < 0 || fy < 0 || fx > G1.nx - 1 || fy > G1.ny - 1) return null;
      const x = Math.min(G1.nx - 2, fx | 0), y = Math.min(G1.ny - 2, fy | 0), ax = fx - x, ay = fy - y, b = t * G1.nx * G1.ny;
      const v = (r, c) => S[b + r * G1.nx + c];
      return ((v(y, x) * (1 - ax) + v(y, x + 1) * ax) * (1 - ay) + (v(y + 1, x) * (1 - ax) + v(y + 1, x + 1) * ax) * ay) * G1.passo;
    }
    // tocco sulla mappa: lo stesso popup del 2D (completo o minimale, dalle impostazioni) all'ora mostrata.
    // Il nome del luogo apre la tenda delle previsioni 48 h sul punto (js/luogo.js), come nel 2D.
    const punto = new maplibregl.Popup({ closeButton: true, closeOnClick: false, className: 'punto3d meteo-popup3d', offset: 6, maxWidth: '240px' });
    const valore = (key, lng, lat, t) => {
      const D = DATI[key], k = D && D.ore.get(Date.parse(VM.times[t])); if (k == null) return null;
      const g = D.g, fx = (lng - g.lon0) / g.dlon, fy = (lat - g.lat0) / g.dlat;
      if (fx < 0 || fy < 0 || fx > g.nx - 1 || fy > g.ny - 1) return null;
      const x = Math.min(g.nx - 2, fx | 0), y = Math.min(g.ny - 2, fy | 0), ax = fx - x, ay = fy - y, v = (r, c) => D.F[k * D.n + r * g.nx + c];
      return ((v(y, x) * (1 - ax) + v(y, x + 1) * ax) * (1 - ay) + (v(y + 1, x) * (1 - ax) + v(y + 1, x + 1) * ax) * ay) * D.passo; };
    // stesse formule del 2D (app.js): percepita di Steadman (BoM) e bulbo umido di Stull
    const percepita = (t, rh, kmh) => { if (t == null || rh == null || kmh == null) return null;
      const e = 6.112 * Math.exp(17.62 * t / (243.12 + t)) * Math.min(100, Math.max(0, rh)) / 100; return t + .33 * e - .7 * Math.max(0, kmh) / 3.6 - 4; };
    const bulbo = (t, rh) => { if (t == null || rh == null) return null; const r = Math.min(99, Math.max(5, rh));
      return t * Math.atan(.151977 * Math.sqrt(r + 8.313659)) + Math.atan(t + r) - Math.atan(r - 1.676331) + .00391838 * Math.pow(r, 1.5) * Math.atan(.023101 * r) - 4.686035; };
    const vento1 = kmh => unitaKts() ? (kmh * KTS).toFixed(1) + ' kts' : kmh.toFixed(1) + ' km/h';
    const c1 = v => (v != null ? v.toFixed(1) + '°C' : 'n/d');
    const esc = x => String(x).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
    let nomePunto = '📍 ...';
    function scriviPunto() {
      if (!punto.isOpen()) return;
      const { lng, lat } = punto.getLngLat(), t = Math.min(NT - 1, Math.round(T)), Tp = T;
      T = t; const uv = field(lng, lat); T = Tp;
      const kmh = velocita(lng, lat, t);
      if (!uv || kmh == null) return punto.remove();
      let dir = Math.atan2(-uv[0], -uv[1]) * 180 / Math.PI; if (dir < 0) dir += 360;
      const sett = SETTORI[Math.round(dir / 22.5) % 16], nome = sett.length <= 2 ? NOMI[sett] : `${NOMI[sett[0]]} - ${NOMI[sett]}`;
      const tc = valore('t', lng, lat, t), rh = valore('rh', lng, lat, t);
      const titolo = `<button class="pp-name pp-link" type="button" data-p48 title="Previsioni 48 ore">${esc(nomePunto)}<span class="pp-chev">›</span></button>`;
      let corpo;
      if (localStorage.getItem('metek.popup.stile') === 'min') {
        corpo = VAR === 't' ? `<span class="pp-k">Temperatura</span> <b class="pp-v">${c1(tc)}</b>`
          : VAR === 'rh' ? `<span class="pp-k">Umidità</span> <b class="pp-v">${rh != null ? Math.round(rh) + '%' : 'n/d'}</b>`
          : `<span class="pp-k">Vento</span> <b class="pp-v">${vento1(kmh)}</b> <span class="pp-k">${sett}</span><small class="pp-wname">${nome}</small>`;
        corpo = `<div class="pp-line">${corpo}</div>`;
      } else {
        const a = VAR === 't' ? percepita(tc, rh, kmh) : null, wb = VAR === 'rh' ? bulbo(tc, rh) : null;
        corpo = `🌡️ <b>${c1(tc)}</b>` + (a != null ? ` · perc. <b>${a.toFixed(1)}°C</b>` : wb != null ? ` · 💦 <b>${wb.toFixed(1)}°C</b>` : '') + '<br>' +
          `💨 <b>${vento1(kmh)}</b> — ${sett}<br><small class="pp-wname" style="margin-left:1.4em">${nome}</small>` +
          `💧 Umidità: <b>${rh != null ? Math.round(rh) + '%' : 'n/d'}</b>`;
      }
      punto.setHTML(`<div class="pp${localStorage.getItem('metek.popup.stile') === 'min' ? ' pp-min' : ''}">${titolo}${corpo}</div>`);
    }
    let oraPunto = -1;
    map.on('click', e => {
      if (e.originalEvent?.target?.closest?.('.maplibregl-marker, .maplibregl-popup')) return;
      const { lng, lat } = e.lngLat;
      nomePunto = '📍 ...'; punto.setLngLat(e.lngLat).addTo(map); oraPunto = -1; scriviPunto();
      // temperatura e umidità servono al popup completo anche guardando il vento: si caricano al primo tocco
      Promise.allSettled([dati('t'), dati('rh')]).then(() => scriviPunto());
      (window.metekLocalita ? window.metekLocalita(lat, lng) : Promise.resolve(`${lat.toFixed(3)}°N, ${lng.toFixed(3)}°E`))
        .then(n => { const ll = punto.getLngLat(); if (ll.lng === lng && ll.lat === lat) { nomePunto = n; scriviPunto(); } });
    });
    map.getContainer().addEventListener('click', ev => {
      if (!ev.target.closest('.meteo-popup3d [data-p48]')) return;
      const { lng, lat } = punto.getLngLat();
      window.MeteoLuogo?.open({ name: nomePunto.startsWith('📍') ? 'Punto sulla mappa' : nomePunto, sub: 'punto sulla mappa', lat, lon: lng });
    });
    aggiornaPunto = () => { const t = Math.round(T); if (t !== oraPunto) { oraPunto = t; scriviPunto(); } };

    // bussola e inclinazione
    const segnaNav = () => { $('ago3d').setAttribute('transform', `rotate(${-map.getBearing()} 29 29)`);
      const p = map.getPitch(), a = p * Math.PI / 180, x = 29 + 24 * Math.sin(a), y = 29 - 24 * Math.cos(a);
      $('incl3d-l').setAttribute('x2', x); $('incl3d-l').setAttribute('y2', y); $('incl3d-p').setAttribute('cx', x); $('incl3d-p').setAttribute('cy', y);
      $('incl3d-t').textContent = p < 3 ? '2D' : Math.round(p) + '°'; };
    // le frecce del vento nelle pillole seguono la bussola (sono disegnate rispetto allo schermo)
    const giraFrecce = () => map.getContainer().style.setProperty('--rot3d', map.getBearing() + 'deg');
    map.on('rotate', giraFrecce); giraFrecce();
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
    // durante l'Evento il player sembra in riproduzione; alla fine (o se lo si ferma) si torna all'ora di prima
    function stop(ripristina = true) { VOLO++; did.hidden = true; EVB.classList.remove('active'); if (EV_ON) { EV_ON = false; avvisiOra(); }
      if (TPRIMA != null) { const t = TPRIMA; TPRIMA = null; PB.textContent = '▶'; if (ripristina) setT(t); } }
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
      pausa(); if (TPRIMA == null) TPRIMA = T; PB.textContent = '❚❚';
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
    EVB.addEventListener('click', async () => { if (EVB.classList.contains('active')) return stop(); if (VAR !== 'ws') await scegli('ws'); evento(); });

    // luogo scelto con la lente: si vola lì (la scheda 48 h del sito copre la parte bassa)
    let casa = null;
    async function vaiA(p) {
      stop(); const id = ++VOLO;
      if (!field(p.lon, p.lat)) return;
      if (casa) casa.remove();
      const el = document.createElement('div'); el.className = 'casa3d';
      casa = new maplibregl.Marker({ element: el, opacityWhenCovered: '0.3' }).setLngLat([p.lon, p.lat]).addTo(map);
      const sotto = Math.min(innerHeight * .3, 230) / 2;
      await vola({ center: [p.lon, p.lat], zoom: 11, pitch: 68, bearing: map.getBearing(), offset: [0, -sotto] }, 4500, null, null, id);
    }

    return {
      mostra() { entra2D(); if (!tickOn) { tickOn = true; ultimo = 0; map.resize(); tick(); } },
      nascondi() { tickOn = false; pausa(); stop(); esci2D(); },
      vaiA, map,
    };
  }

  window.Metek3D = {
    get attivo() { return attivo; },
    get _map() { return V && V.map; },   // per le prove dalla console
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
