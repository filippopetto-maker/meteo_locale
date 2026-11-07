// Ricerca di un luogo → scheda delle previsioni a +48 h (design system "Metek": LocationSearch + Forecast48h).
// È un ingresso diverso dal click sulla mappa: il click apre il popup del punto, la ricerca centra la
// mappa sul luogo e apre un foglio dal basso con 16 colonne da 3 ore.
//
// Fonti nella scheda:
//   - temperatura, percepita, umidità, vento medio e direzione: M2 (data/forecast_48h.json), con la
//     stessa interpolazione della timeline della mappa (pointSeries in app.js);
//   - cielo (icona) e pioggia: ICON-2I via Open-Meteo, perché M2 non li prevede;
//   - raffiche: il massimo tra ICON-2I e IFS (Open-Meteo), mai sotto il vento medio M2 della stessa ora.
//     Contro le raffiche METAR del Lazio (nov 2024 – ott 2026) coglie l'86% degli eventi ≥40 km/h e il
//     73% di quelli ≥60, contro 75% e 67% di ICON-2I da solo (scripts/experiment_m2_gust.py);
//   - ricerca dei nomi: geocoding Open-Meteo (GeoNames), limitata al riquadro della mappa, più le stazioni.
// app.js chiama MeteoLuogo.init(...) quando mappa e dati sono pronti.
(function () {
  'use strict';

  const COL3 = 52;          // larghezza di una colonna da 3 ore (token col-3h)
  const COL1 = 44;          // colonna da 1 ora: più stretta, 48 colonne invece di 16
  const STEP_HOURS = [1, 4, 7, 10, 13, 16, 19, 22];
  const STEP_KEY = 'metek.luogo.passo';
  const GEO_URL = 'https://geocoding-api.open-meteo.com/v1/search';
  const FC_URL = 'https://api.open-meteo.com/v1/forecast';
  const RECENT_KEY = 'metek.luoghi.recenti';
  const WIND_STOPS = ['#003399', '#0099ff', '#00cc66', '#ffdd00', '#ff6600', '#cc0000']; // WIND_PALETTE, 0–50 km/h

  let ctx = null;           // {map, stations, isPWA, bounds, pointSeries, whenReady, nowTemp, windUnit}
  let ringMarker = null;
  let els = {};
  let hits = [], active = 0, searchSeq = 0, debounce = null;
  let step = 3;             // passo della scheda: 3 o 1 ora (scelta ricordata)
  try { if (localStorage.getItem(STEP_KEY) === '1') step = 1; } catch {}

  // ── Utilità ────────────────────────────────────────────────────────────────
  const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const norm = s => String(s).normalize('NFD').replace(/[̀-ͯ'’]/g, '').toLowerCase();
  const fmt1 = v => v.toFixed(1).replace('.', ',');
  const romeParts = (ms) => {
    const p = {};
    new Intl.DateTimeFormat('it-IT', { timeZone: 'Europe/Rome', weekday: 'long', day: 'numeric', hour: 'numeric', hourCycle: 'h23' })
      .formatToParts(new Date(ms)).forEach(x => { p[x.type] = x.value; });
    return { hour: parseInt(p.hour, 10), day: p.weekday.charAt(0).toUpperCase() + p.weekday.slice(1) + ' ' + p.day };
  };
  function readRecent() { try { return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; } catch { return []; } }
  function saveRecent(p) {
    try {
      const list = [p, ...readRecent().filter(r => r.name !== p.name)].slice(0, 4);
      localStorage.setItem(RECENT_KEY, JSON.stringify(list));
    } catch { /* storage non disponibile: niente recenti */ }
  }
  function inBounds(lat, lon) {
    const b = ctx.bounds;
    return !b || (lat >= b[0][0] && lat <= b[1][0] && lon >= b[0][1] && lon <= b[1][1]);
  }
  function windColor(v) {
    const t = Math.max(0, Math.min(1, v / 50)) * 5, i = Math.min(4, Math.floor(t)), f = t - i;
    const a = WIND_STOPS[i], b = WIND_STOPS[i + 1];
    const ch = (h, k) => parseInt(h.substr(1 + 2 * k, 2), 16);
    const c = [0, 1, 2].map(k => Math.round(ch(a, k) + (ch(b, k) - ch(a, k)) * f));
    return `rgba(${c.join(',')},0.45)`;
  }
  const windVal = kmh => (ctx.windUnit() === 'kts' ? kmh * 0.539957 : kmh);
  const windLabel = () => (ctx.windUnit() === 'kts' ? 'nodi' : 'km/h');

  // ── Icone (SVG, inchiostri dai token) ───────────────────────────────────────
  const SUN = '<circle class="sun" cx="11" cy="11" r="5"/><g class="ray"><path d="M11 2v2M11 18v2M2 11h2M18 11h2M4.6 4.6l1.4 1.4M16 16l1.4 1.4M4.6 17.4 6 16M16 6l1.4-1.4"/></g>';
  const MOON = '<path class="moon" d="M10 2a7 7 0 1 0 6 10.5A6 6 0 0 1 10 2z"/>';
  const CLOUD = '<path class="cl" d="M8 24h13a5 5 0 0 0 .6-10A7 7 0 0 0 8.3 15.2 4.4 4.4 0 0 0 8 24z"/>';
  const CLOUD_UP = '<g transform="translate(0 -4)">' + CLOUD + '</g>';
  function iconSVG(code, day) {
    let b;
    const sky = day ? SUN : MOON;
    if (code == null) b = CLOUD;
    else if (code <= 1) b = day ? '<g transform="translate(4 4)">' + SUN + '</g>' : '<g transform="translate(5 4)">' + MOON + '</g>';
    else if (code === 2) b = sky + CLOUD;
    else if (code === 3) b = '<g transform="translate(2 -3)">' + CLOUD + '</g>';
    else if (code === 45 || code === 48) b = CLOUD_UP + '<g class="fog"><path d="M6 25h18M9 28h12"/></g>';
    else if (code >= 95) b = CLOUD_UP + '<path class="bolt" d="M16 21l-3 4h2.5l-1.5 3.5 4-5h-2.5l1.5-2.5z"/><g class="drop"><path d="M10 25.5l-1 2.5M22 25.5l-1 2.5"/></g>';
    else if ((code >= 71 && code <= 77) || code === 85 || code === 86) b = CLOUD_UP + '<g class="snow"><circle cx="11" cy="26" r="1.3"/><circle cx="16" cy="28" r="1.3"/><circle cx="21" cy="26" r="1.3"/></g>';
    else if (code >= 51 && code <= 57) b = CLOUD_UP + '<g class="drop"><path d="M12 25.5l-.8 2M19 25.5l-.8 2"/></g>';
    else b = CLOUD_UP + '<g class="drop"><path d="M11 25.5l-1 2.5M15.5 25.5l-1 2.5M20 25.5l-1 2.5"/></g>';
    return `<svg width="28" height="28" viewBox="0 0 30 30" aria-hidden="true">${b}</svg>`;
  }
  const SKY_TEXT = c => (c == null ? '' : c >= 95 ? 'temporale' : (c >= 71 && c <= 77) || c === 85 || c === 86 ? 'neve'
    : c >= 61 ? 'pioggia' : c >= 51 ? 'pioviggine' : c >= 45 ? 'nebbia' : c === 3 ? 'nuvoloso' : c === 2 ? 'poco nuvoloso' : 'sereno');

  // ── Stile (token del design system Metek, tema scuro dell'app) ──────────────
  const CSS = `
  .ml { --ml-surface:#161826; --ml-glass:rgba(20,22,34,.93); --ml-night:#1e2138; --ml-border:rgba(255,255,255,.12);
        --ml-border-soft:rgba(255,255,255,.07); --ml-text:#e9e9ed; --ml-text-2:#a7afc0; --ml-text-3:#8892a4;
        --ml-accent:#9184d9; --ml-accent-fill:rgba(145,132,217,.18); --ml-warm:#f4a93f; --ml-temp-fill:rgba(244,169,63,.13);
        --ml-cloud:#c8ccd6; --ml-moon:#c6bff0; --ml-rain:#5eb0f5; --ml-shadow:0 8px 24px rgba(0,0,0,.45);
        font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: var(--ml-text); }
  .ml-search { position: fixed; z-index: 1100; left: 14px; bottom: calc(104px + env(safe-area-inset-bottom, 0px)); } /* sopra la barra Ora/+48 h */
  .is-pwa .ml-search { bottom: auto; top: calc(env(safe-area-inset-top, 0px) + 51px); }
  .ml-search.open { right: 14px; }
  html:not(.is-pwa) .ml-search { display: flex; flex-direction: column-reverse; }
  html:not(.is-pwa) .ml-list { margin: 0 0 8px; }
  @media (min-width: 700px) { html:not(.is-pwa) .ml-search.open { right: auto; width: 380px; } }
  html:not(.is-pwa) body.ml-sheet-open .ml-search:not(.open) { display: none; }
  .ml-field { display: flex; align-items: center; gap: 8px; height: 40px; padding: 0 6px 0 12px; background: var(--ml-glass);
    border: 1px solid var(--ml-border); border-radius: 999px; box-shadow: var(--ml-shadow);
    backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px); }
  .ml-search:not(.open) .ml-field { width: 40px; padding: 0; justify-content: center; cursor: pointer; }
  .ml-search:not(.open) .ml-input, .ml-search:not(.open) .ml-x { display: none; }
  .ml-field:focus-within { outline: 2px solid var(--ml-accent); outline-offset: 1px; }
  .ml-lens { color: var(--ml-text-2); flex: 0 0 auto; display: block; }
  .ml-input { flex: 1; min-width: 0; border: 0; outline: 0; background: transparent; color: var(--ml-text); font: inherit; font-size: 16px; }
  .ml-input::placeholder { color: var(--ml-text-3); }
  .ml-input::-webkit-search-cancel-button { display: none; }
  .ml-x { border: 0; background: transparent; color: var(--ml-text-2); font: inherit; font-size: 20px; line-height: 1; width: 32px; height: 32px; cursor: pointer; border-radius: 999px; }
  .ml-list { margin-top: 8px; background: var(--ml-surface); border: 1px solid var(--ml-border); border-radius: 18px; box-shadow: var(--ml-shadow); padding: 4px; max-height: 50vh; overflow-y: auto; }
  .ml-list[hidden] { display: none; }
  .ml-opt { display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-radius: 10px; cursor: pointer; }
  .ml-opt[aria-selected="true"] { background: var(--ml-accent-fill); }
  .ml-pin { width: 8px; height: 8px; border-radius: 50%; border: 2px solid var(--ml-accent); flex: 0 0 auto; }
  .ml-pin.st { background: var(--ml-accent); }
  .ml-n { font-size: 14px; line-height: 20px; }
  .ml-s { font-size: 11px; line-height: 14px; color: var(--ml-text-2); }
  .ml-msg { padding: 10px 12px; font-size: 12px; color: var(--ml-text-3); }
  .ml-head-row { padding: 6px 12px 2px; font-size: 9.5px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; color: var(--ml-text-3); }

  .ml-ring { width: 26px; height: 26px; border-radius: 50%; border: 2px solid var(--ml-accent); box-shadow: 0 0 0 6px var(--ml-accent-fill), 0 0 12px rgba(0,0,0,.5); position: relative; box-sizing: border-box; }
  .ml-ring::after { content: ""; position: absolute; inset: 6px; border-radius: 50%; background: #fff; }

  .ml-sheet { position: fixed; z-index: 1090; left: 0; right: 0; bottom: 0; max-height: 72vh; display: flex; flex-direction: column;
    background: var(--ml-surface); border-top: 1px solid var(--ml-border); border-radius: 18px 18px 0 0; box-shadow: 0 -8px 24px rgba(0,0,0,.45);
    padding-bottom: env(safe-area-inset-bottom, 0px); transform: translateY(105%); transition: transform .28s ease; }
  .ml-sheet.open { transform: none; }
  @media (min-width: 900px) { .ml-sheet { left: 50%; right: auto; width: 880px; margin-left: -440px; } }
  .ml-grab { width: 36px; height: 4px; border-radius: 999px; background: var(--ml-border); margin: 8px auto 0; }
  .ml-top { display: flex; align-items: flex-end; gap: 12px; padding: 8px 16px 10px; border-bottom: 1px solid var(--ml-border-soft); }
  .ml-now { font-size: 30px; line-height: 34px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .ml-now small { display: block; font-size: 11px; line-height: 14px; font-weight: 400; color: var(--ml-text-3); }
  .ml-place { flex: 1; min-width: 0; }
  .ml-place b { display: block; font-size: 17px; line-height: 22px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ml-place span { display: block; font-size: 11px; line-height: 14px; color: var(--ml-text-3); }
  .ml-close { flex: 0 0 auto; width: 32px; height: 32px; border-radius: 999px; border: 1px solid var(--ml-border); background: transparent; color: var(--ml-text-2); font: inherit; font-size: 18px; cursor: pointer; }
  .ml-step { flex: 0 0 auto; display: flex; gap: 2px; padding: 2px; border-radius: 999px; border: 1px solid var(--ml-border); align-self: center; }
  .ml-step button { border: 0; background: transparent; color: var(--ml-text-2); font: inherit; font-size: 12px; font-weight: 600; min-width: 38px; height: 28px; border-radius: 999px; cursor: pointer; font-variant-numeric: tabular-nums; }
  .ml-step button[aria-pressed="true"] { background: var(--ml-accent); color: #fff; }
  .ml-body { display: flex; overflow: hidden; }
  .ml-gutter { flex: 0 0 40px; border-right: 1px solid var(--ml-border-soft); }
  .ml-gutter div { display: flex; align-items: center; justify-content: flex-end; padding-right: 5px; font-size: 9.5px; font-weight: 600; letter-spacing: .02em; text-transform: uppercase; color: var(--ml-text-3); }
  .ml-scroll { overflow-x: auto; flex: 1; -webkit-overflow-scrolling: touch; }
  .ml-grid { position: relative; }
  .ml-row { display: flex; }
  .ml-cell { flex: 0 0 var(--ml-col, ${COL3}px); width: var(--ml-col, ${COL3}px); display: flex; align-items: center; justify-content: center; font-variant-numeric: tabular-nums; position: relative; }
  .ml-cell.night { background: var(--ml-night); }
  .ml-cell.dstart { border-left: 1px solid var(--ml-border); }
  .ml-days .ml-seg { box-sizing: border-box; display: flex; align-items: center; padding: 0 10px; height: 32px; font-size: 14px; font-weight: 600; white-space: nowrap; overflow: hidden; }
  .ml-days .ml-seg + .ml-seg { border-left: 1px solid var(--ml-border); }
  .ml-days .ml-seg span { position: sticky; left: 10px; }
  .r-hour { height: 22px; font-size: 12px; font-weight: 500; color: var(--ml-text-2); }
  .r-icon { height: 34px; }
  .r-temp { height: 54px; align-items: flex-end; padding-bottom: 2px; font-size: 15px; font-weight: 600; }
  .r-app { height: 20px; font-size: 12px; color: var(--ml-text-3); }
  .r-rain { height: 20px; font-size: 12px; font-weight: 500; color: var(--ml-rain); }
  .r-wind { height: 24px; font-size: 12px; font-weight: 500; border-top: 1px solid var(--ml-border-soft); }
  .r-gust { height: 26px; font-size: 12px; font-weight: 600; }
  .r-gust span { display: block; width: calc(100% - 4px); text-align: center; line-height: 22px; border-radius: 6px; }
  .r-dir { height: 26px; color: var(--ml-text-2); }
  .ml-curve { position: absolute; left: 0; pointer-events: none; z-index: 1; }
  .r-temp > span { position: relative; z-index: 2; }
  .ml-curve .area { fill: var(--ml-temp-fill); }
  .ml-curve .line { fill: none; stroke: var(--ml-warm); stroke-width: 1.5; opacity: .55; }
  .ml-foot { padding: 8px 16px 10px; border-top: 1px solid var(--ml-border-soft); font-size: 11px; line-height: 15px; color: var(--ml-text-3); }
  .ml-state { padding: 28px 16px; font-size: 13px; color: var(--ml-text-2); text-align: center; }
  .ml svg .sun { fill: var(--ml-warm); } .ml svg .ray { stroke: var(--ml-warm); stroke-width: 1.6; stroke-linecap: round; }
  .ml svg .cl { fill: var(--ml-cloud); } .ml svg .moon { fill: var(--ml-moon); }
  .ml svg .drop { stroke: var(--ml-rain); stroke-width: 1.8; stroke-linecap: round; } .ml svg .bolt { fill: var(--ml-warm); }
  .ml svg .snow { fill: var(--ml-text); } .ml svg .fog { stroke: var(--ml-cloud); stroke-width: 1.6; stroke-linecap: round; }
  `;

  // ── Costruzione DOM ─────────────────────────────────────────────────────────
  function build() {
    const st = document.createElement('style');
    st.textContent = CSS;
    document.head.appendChild(st);

    const box = document.createElement('div');
    box.className = 'ml ml-search';
    box.innerHTML =
      `<div class="ml-field">` +
      `<svg class="ml-lens" width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="m12.2 12.2 4 4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>` +
      `<input class="ml-input" type="search" placeholder="Cerca un luogo" autocomplete="off" autocapitalize="words" spellcheck="false" enterkeyhint="search" role="combobox" aria-expanded="false" aria-controls="ml-list" aria-label="Cerca un luogo">` +
      `<button class="ml-x" type="button" aria-label="Chiudi la ricerca">×</button>` +
      `</div><div class="ml-list" id="ml-list" role="listbox" hidden></div>`;
    document.body.appendChild(box);

    const sheet = document.createElement('section');
    sheet.className = 'ml ml-sheet';
    sheet.setAttribute('aria-label', 'Previsioni a 48 ore');
    document.body.appendChild(sheet);

    els = { box, sheet, field: box.querySelector('.ml-field'), input: box.querySelector('.ml-input'),
            x: box.querySelector('.ml-x'), list: box.querySelector('.ml-list') };
    L.DomEvent.disableClickPropagation(box);
    L.DomEvent.disableScrollPropagation(box);
    L.DomEvent.disableClickPropagation(sheet);
    L.DomEvent.disableScrollPropagation(sheet);

    els.field.addEventListener('click', () => { if (!box.classList.contains('open')) openSearch(); });
    els.x.addEventListener('click', e => { e.stopPropagation(); closeSearch(); });
    els.input.addEventListener('input', () => { active = 0; scheduleSearch(); });
    els.input.addEventListener('keydown', onKey);
    els.list.addEventListener('mousemove', e => {
      const o = e.target.closest('.ml-opt');
      if (o && +o.dataset.i !== active) { active = +o.dataset.i; paintActive(); }
    });
    els.list.addEventListener('click', e => { const o = e.target.closest('.ml-opt'); if (o) pick(hits[+o.dataset.i]); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && sheet.classList.contains('open') && !box.classList.contains('open')) closeSheet(); });
  }

  // ── Ricerca ────────────────────────────────────────────────────────────────
  function openSearch() {
    els.box.classList.add('open');
    els.input.value = '';
    els.input.focus();
    renderRecent();
  }
  function closeSearch() {
    els.box.classList.remove('open');
    els.list.hidden = true;
    els.input.setAttribute('aria-expanded', 'false');
    els.input.blur();
  }
  function onKey(e) {
    if (e.key === 'Escape') { closeSearch(); return; }
    if (els.list.hidden || !hits.length) return;
    if (e.key === 'ArrowDown') { active = Math.min(hits.length - 1, active + 1); paintActive(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { active = Math.max(0, active - 1); paintActive(); e.preventDefault(); }
    else if (e.key === 'Enter') { e.preventDefault(); if (hits[active]) pick(hits[active]); }
  }
  function renderRecent() {
    const rec = readRecent().filter(r => inBounds(r.lat, r.lon));
    hits = rec;
    active = 0;
    if (!rec.length) { showList('<div class="ml-msg">Scrivi il nome di un comune, una frazione o una stazione del Lazio.</div>'); return; }
    showList('<div class="ml-head-row">Recenti</div>' + rec.map(optHTML).join(''));
  }
  function scheduleSearch() {
    clearTimeout(debounce);
    const q = els.input.value.trim();
    if (q.length < 2) { renderRecent(); return; }
    debounce = setTimeout(() => search(q), 220);
  }
  async function search(q) {
    const seq = ++searchSeq;
    const nq = norm(q);
    const fromStations = ctx.stations
      .filter(s => norm(s.name).includes(nq))
      .map(s => ({ name: s.name, sub: 'stazione della rete', lat: s.lat, lon: s.lon, station: true }));
    let fromGeo = [];
    try {
      const url = `${GEO_URL}?name=${encodeURIComponent(q)}&count=20&language=it&countryCode=IT&format=json`;
      const res = await fetch(url);
      const j = res.ok ? await res.json() : {};
      fromGeo = (j.results || [])
        .filter(r => inBounds(r.latitude, r.longitude))
        .map(r => ({
          name: r.name,
          sub: [adminName(r.admin3), adminName(r.admin2), r.elevation != null ? Math.round(r.elevation) + ' m' : null]
            .filter((v, i, a) => v && norm(v) !== norm(r.name) && a.indexOf(v) === i).join(' · '),
          lat: r.latitude, lon: r.longitude, elev: r.elevation,
        }));
    } catch { /* rete assente: restano le stazioni */ }
    if (seq !== searchSeq) return;  // è arrivata una ricerca più recente
    // Prima il nome uguale, poi quelli che iniziano così; a parità le stazioni restano davanti
    const rank = p => { const n = norm(p.name); return n === nq ? 0 : n.startsWith(nq) ? 1 : 2; };
    hits = [...fromStations, ...fromGeo]
      .map((p, i) => ({ p, i }))
      .sort((a, b) => rank(a.p) - rank(b.p) || a.i - b.i)
      .map(x => x.p)
      .slice(0, 8);
    active = 0;
    if (!hits.length) {
      showList('<div class="ml-msg">Nessun luogo trovato nell\'area della mappa. Puoi toccare la mappa per il valore di un punto.</div>');
      return;
    }
    showList(hits.map(optHTML).join(''));
  }
  // "Comune di Fiumicino" → "Fiumicino", "Città metropolitana di Roma Capitale" → "Roma Capitale"
  function adminName(s) {
    return s ? s.replace(/^(Comune|Provincia|Città metropolitana|Libero consorzio comunale) di /i, '') : null;
  }
  function optHTML(p, i) {
    return `<div class="ml-opt" role="option" data-i="${i}" aria-selected="${i === active}">` +
      `<span class="ml-pin${p.station ? ' st' : ''}"></span><div><div class="ml-n">${esc(p.name)}</div>` +
      (p.sub ? `<div class="ml-s">${esc(p.sub)}</div>` : '') + `</div></div>`;
  }
  function showList(html) {
    els.list.innerHTML = html;
    els.list.hidden = false;
    els.input.setAttribute('aria-expanded', 'true');
  }
  function paintActive() {
    els.list.querySelectorAll('.ml-opt').forEach(o => o.setAttribute('aria-selected', String(+o.dataset.i === active)));
  }

  // ── Scelta del luogo ───────────────────────────────────────────────────────
  function pick(p) {
    if (!p) return;
    saveRecent({ name: p.name, sub: p.sub, lat: p.lat, lon: p.lon, elev: p.elev, station: p.station });
    closeSearch();
    const map = ctx.map;
    map.closePopup();
    if (ringMarker) map.removeLayer(ringMarker);
    ringMarker = L.marker([p.lat, p.lon], {
      icon: L.divIcon({ className: 'ml', html: '<div class="ml-ring"></div>', iconSize: [26, 26], iconAnchor: [13, 13] }),
      interactive: false, keyboard: false,
    }).addTo(map);
    // Il foglio copre la parte bassa: il luogo va centrato nella parte di mappa che resta visibile
    const sheetH = Math.min(window.innerHeight * 0.72, 470);
    const zoom = Math.max(map.getZoom(), 11);
    try {
      const pt = map.project([p.lat, p.lon], zoom).add([0, sheetH / 2]);
      map.flyTo(map.unproject(pt, zoom), zoom, { duration: 0.8 });
    } catch {
      // mappa a metà di un ridimensionamento: si centra senza animazione
      map.invalidateSize();
      map.setView([p.lat, p.lon], zoom, { animate: false });
    }
    openSheet(p);
  }

  // ── Scheda 48 h ────────────────────────────────────────────────────────────
  function closeSheet() {
    els.sheet.classList.remove('open');
    document.body.classList.remove('ml-sheet-open');
    if (ringMarker) { ctx.map.removeLayer(ringMarker); ringMarker = null; }
  }

  function headHTML(p, now, withStep) {
    const sub = p.station ? 'stazione della rete' : (p.sub || `${p.lat.toFixed(3)}° N, ${p.lon.toFixed(3)}° E`);
    return `<div class="ml-grab"></div><div class="ml-top">` +
      (now != null ? `<div class="ml-now">${Math.round(now)}°<small>adesso</small></div>` : '') +
      `<div class="ml-place"><b>${esc(p.name)}</b><span>${esc(sub)}</span></div>` +
      (withStep ? `<div class="ml-step" role="group" aria-label="Passo orario">` +
        [3, 1].map(v => `<button type="button" data-step="${v}" aria-pressed="${v === step}">${v} h</button>`).join('') + `</div>` : '') +
      `<button class="ml-close" type="button" aria-label="Chiudi la scheda">×</button></div>`;
  }

  async function openSheet(p) {
    const sheet = els.sheet;
    const now = ctx.nowTemp(p.lat, p.lon);
    sheet.innerHTML = headHTML(p, now) + '<div class="ml-state">Calcolo le previsioni…</div>';
    sheet.querySelector('.ml-close').addEventListener('click', closeSheet);
    document.body.classList.add('ml-sheet-open');
    requestAnimationFrame(() => sheet.classList.add('open'));
    const token = (sheet._token = {});

    const ok = await ctx.whenReady;
    const series = ok ? ctx.pointSeries(p.lat, p.lon) : null;
    if (sheet._token !== token) return;
    if (!series || !series.length) {
      sheet.querySelector('.ml-state').textContent = 'Le previsioni a 48 ore non sono disponibili in questo momento.';
      return;
    }
    const nwp = await fetchNWP(p.lat, p.lon);
    if (sheet._token !== token) return;

    sheet.innerHTML = headHTML(p, now, true) +
      `<div class="ml-body"><div class="ml-gutter"></div><div class="ml-scroll"><div class="ml-grid"></div></div></div>` +
      `<div class="ml-foot"></div>`;
    sheet.querySelector('.ml-close').addEventListener('click', closeSheet);
    const draw = () => {
      sheet.querySelectorAll('.ml-step button').forEach(b => b.setAttribute('aria-pressed', String(+b.dataset.step === step)));
      const grid = sheet.querySelector('.ml-grid'), gutter = sheet.querySelector('.ml-gutter');
      grid.innerHTML = ''; gutter.innerHTML = '';
      sheet.querySelector('.ml-scroll').scrollLeft = 0;
      renderGrid(grid, gutter, columns(series, nwp, step), !!nwp, step === 3 ? COL3 : COL1);
      sheet.querySelector('.ml-foot').textContent =
        'Temperatura, percepita e vento medio: modello M2, stazioni della rete. ' +
        (nwp ? `Cielo e pioggia (mm ${step === 3 ? 'nelle 3 ore' : "nell'ora"}): ICON-2I via Open-Meteo. ` +
               'Raffiche: la più forte tra ICON-2I e IFS, mai sotto il vento medio.'
             : 'Cielo, pioggia e raffiche non disponibili (Open-Meteo non risponde).');
    };
    sheet.querySelector('.ml-step').addEventListener('click', e => {
      const b = e.target.closest('button[data-step]');
      if (!b || +b.dataset.step === step) return;
      step = +b.dataset.step;
      try { localStorage.setItem(STEP_KEY, String(step)); } catch {}
      draw();
    });
    draw();
  }

  // Cielo, pioggia e raffiche ora per ora da ICON-2I; se mancano ore, si riprova col modello di default.
  // Le raffiche IFS arrivano da una seconda richiesta e la raffica dell'ora è la più forte delle due.
  async function fetchNWP(lat, lon) {
    const q = `latitude=${lat.toFixed(4)}&longitude=${lon.toFixed(4)}&hourly=precipitation,weather_code,wind_gusts_10m,is_day&timezone=UTC&forecast_days=3`;
    const get = async (model) => {
      try {
        const res = await fetch(`${FC_URL}?${q}${model ? '&models=' + model : ''}`);
        if (!res.ok) return null;
        const h = (await res.json()).hourly;
        if (!h?.time) return null;
        const m = new Map();
        h.time.forEach((t, i) => m.set(Date.parse(t + 'Z'), {
          pr: h.precipitation[i], wc: h.weather_code[i], gust: h.wind_gusts_10m[i], day: h.is_day[i],
        }));
        return m;
      } catch { return null; }
    };
    const ifsGusts = async () => {
      try {
        const res = await fetch(`${FC_URL}?latitude=${lat.toFixed(4)}&longitude=${lon.toFixed(4)}` +
          '&hourly=wind_gusts_10m&models=ecmwf_ifs025&timezone=UTC&forecast_days=3');
        if (!res.ok) return null;
        const h = (await res.json()).hourly;
        return h?.time ? new Map(h.time.map((t, i) => [Date.parse(t + 'Z'), h.wind_gusts_10m[i]])) : null;
      } catch { return null; }
    };
    const [icon, ifs] = await Promise.all([get('italia_meteo_arpae_icon_2i'), ifsGusts()]);
    let out = icon;
    const fill = await (icon && [...icon.values()].filter(v => v.wc == null).length < 6 ? null : get(null));
    if (!icon) out = fill;
    else if (fill) for (const [k, v] of icon) if (v.wc == null && fill.has(k)) icon.set(k, fill.get(k));
    if (out && ifs) for (const [k, v] of out) {
      const g = ifs.get(k);
      if (g != null) v.gust = v.gust == null ? g : Math.max(v.gust, g);
    }
    return out;
  }

  // Passo 3 h: 16 colonne, le ore 1, 4, 7 … 22 (ora italiana); ogni colonna vale per le 3 ore che apre.
  // Passo 1 h: tutte le 48 ore, ogni colonna vale per la sua ora.
  function columns(series, nwp, n = 3) {
    const out = [];
    const wsAt = new Map(series.map(s => [Date.parse(s.time), s.ws]));  // vento medio M2 ora per ora
    for (const s of series) {
      const ms = Date.parse(s.time);
      const rp = romeParts(ms);
      if (n === 3 && !STEP_HOURS.includes(rp.hour)) continue;
      const c = { ms, hour: rp.hour, day: rp.day, t: s.t, a: s.a, ws: s.ws, wd: s.wd,
                  wc: null, pr: null, gust: null, isDay: rp.hour >= 7 && rp.hour < 17 };
      if (nwp) {
        const h0 = nwp.get(ms);
        if (h0 && h0.day != null) c.isDay = h0.day === 1;
        let pr = 0, prN = 0, gust = null, wc = null;
        for (let k = 0; k < n; k++) {
          const cur = nwp.get(ms + k * 3.6e6);          // cielo e raffica: ore h, h+1, h+2
          const nxt = nwp.get(ms + (k + 1) * 3.6e6);    // pioggia: accumulata nell'ora precedente
          if (cur?.wc != null) wc = wc == null ? cur.wc : Math.max(wc, cur.wc);
          if (cur?.gust != null) {
            const g = Math.max(cur.gust, wsAt.get(ms + k * 3.6e6) ?? 0);  // la raffica non è mai sotto la media
            gust = gust == null ? g : Math.max(gust, g);
          }
          if (nxt?.pr != null) { pr += nxt.pr; prN++; }
        }
        c.wc = wc; c.gust = gust; c.pr = prN ? pr : null;
      }
      out.push(c);
    }
    return out;
  }

  function renderGrid(grid, gutter, cols, hasNWP, COL) {
    const N = cols.length;
    grid.style.setProperty('--ml-col', COL + 'px');
    grid.style.width = (N * COL) + 'px';

    const days = document.createElement('div');
    days.className = 'ml-row ml-days';
    const segs = [];
    cols.forEach(c => { const s = segs[segs.length - 1]; if (s && s.name === c.day) s.n++; else segs.push({ name: c.day, n: 1 }); });
    days.innerHTML = segs.map(s => `<div class="ml-seg" style="width:${s.n * COL}px" title="${esc(s.name)}">${s.n >= 2 ? `<span>${esc(s.name)}</span>` : ''}</div>`).join('');
    grid.appendChild(days);
    const g0 = document.createElement('div');
    g0.style.height = '32px';
    gutter.appendChild(g0);

    const row = (cls, label, fn) => {
      const r = document.createElement('div');
      r.className = 'ml-row';
      r.innerHTML = cols.map((c, i) =>
        `<div class="ml-cell ${cls}${c.isDay ? '' : ' night'}${i > 0 && cols[i - 1].day !== c.day ? ' dstart' : ''}">${fn(c)}</div>`).join('');
      grid.appendChild(r);
      const g = document.createElement('div');
      g.className = cls;
      g.textContent = label;
      gutter.appendChild(g);
      return r;
    };
    row('r-hour', '', c => c.hour);
    if (hasNWP) row('r-icon', '', c => `<span title="${SKY_TEXT(c.wc)}">${iconSVG(c.wc, c.isDay)}</span>`);
    const tRow = row('r-temp', '°C', c => `<span>${c.t != null ? Math.round(c.t) + '°' : '–'}</span>`);
    row('r-app', 'perc.', c => (c.a != null ? Math.round(c.a) + '°' : ''));
    if (hasNWP) row('r-rain', 'mm', c => (c.pr != null && c.pr >= 0.1 ? fmt1(c.pr) : ''));
    row('r-wind', windLabel(), c => (c.ws != null ? Math.round(windVal(c.ws)) : ''));
    if (hasNWP) row('r-gust', 'raff.', c => (c.gust != null
      ? `<span style="background:${windColor(c.gust)}">${Math.round(windVal(c.gust))}</span>` : ''));
    row('r-dir', '', c => (c.wd == null ? '' :
      `<svg width="16" height="16" viewBox="0 0 16 16" style="transform:rotate(${Math.round(c.wd + 180)}deg)" role="img" aria-label="vento da ${Math.round(c.wd)} gradi"><path d="M8 1 13 13 8 10 3 13z" fill="currentColor"/></svg>`));

    // Curva della temperatura dietro i valori
    const ts = cols.map(c => c.t).filter(v => v != null);
    if (ts.length > 1) {
      const top = tRow.offsetTop, h = tRow.offsetHeight;
      const lo = Math.min(...ts) - 1, hi = Math.max(...ts) + 1;
      const pts = cols.map((c, i) => (c.t == null ? null : [i * COL + COL / 2, 8 + (1 - (c.t - lo) / (hi - lo)) * (h - 30)])).filter(Boolean);
      const d = 'M' + pts.map(p => p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' L');
      grid.insertAdjacentHTML('beforeend',
        `<svg class="ml-curve" style="top:${top}px" width="${N * COL}" height="${h}" aria-hidden="true">` +
        `<path class="area" d="${d} L${pts[pts.length - 1][0]} ${h} L${pts[0][0]} ${h}Z"/><path class="line" d="${d}"/></svg>`);
    }
  }

  window.MeteoLuogo = {
    init(options) {
      if (ctx) return;
      ctx = options;
      build();
    },
  };
})();
