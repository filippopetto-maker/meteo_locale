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
// Terzo ingresso: la posizione del dispositivo. All'apertura (sito e PWA) la scheda si apre da sola sul
// punto in cui si trova l'utente, se ha dato il permesso e se è dentro la mappa; nella ricerca c'è anche
// la voce "La tua posizione". Le coordinate restano nel browser (arrotondate a ~100 m prima di ogni uso).
// app.js chiama MeteoLuogo.init(...) quando mappa e dati sono pronti, poi MeteoLuogo.locateOnStart().
(function () {
  'use strict';

  const COL3 = 52;          // larghezza di una colonna da 3 ore (token col-3h)
  const COL1 = 44;          // colonna da 1 ora: più stretta, 48 colonne invece di 16
  const STEP_HOURS = [1, 4, 7, 10, 13, 16, 19, 22];
  const STEP_KEY = 'metek.luogo.passo';
  const GEO_URL = 'https://geocoding-api.open-meteo.com/v1/search';
  const FC_URL = 'https://api.open-meteo.com/v1/forecast';
  const RECENT_KEY = 'metek.luoghi.recenti';
  const GEO_OPTS = { enableHighAccuracy: false, timeout: 10000, maximumAge: 10 * 60 * 1000 };
  const WIND_STOPS = ['#003399', '#0099ff', '#00cc66', '#ffdd00', '#ff6600', '#cc0000']; // WIND_PALETTE, 0–50 km/h

  let ctx = null;           // {map, stations, isPWA, bounds, pointSeries, whenReady, nowTemp, windUnit, localityName}
  let userActed = false;    // l'utente ha già cercato o toccato la mappa: la posizione automatica non si impone
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
  function removeRecent(p) {
    if (!p) return;
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(readRecent().filter(r => r.name !== p.name))); } catch { /* storage pieno o bloccato */ }
  }
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
  /* v2: la ricerca si apre sopra la barra in alto, dalla lente nella barra */
  .is-pwa .ml-search { bottom: auto; top: calc(env(safe-area-inset-top, 0px) + 8px); left: 10px; }
  .is-pwa .ml-search:not(.open) { display: none; }
  .is-pwa .ml-search.open { right: 10px; }
  .is-pwa .ml-search .ml-field { height: 52px; border-radius: 18px; }
  .ml-search.open { right: 14px; }
  /* Sito: niente lente in basso, la ricerca si apre in alto dalla lente accanto a "Metek" (app.js) */
  html:not(.is-pwa) .ml-search { top: calc(10px + env(safe-area-inset-top, 0px)); bottom: auto; left: 10px; display: flex; flex-direction: column; }
  html:not(.is-pwa) .ml-search:not(.open) { display: none; }
  html:not(.is-pwa) .ml-search.open { right: 10px; }
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
  .ml-opt > div { flex: 1; min-width: 0; }
  .ml-del { flex: 0 0 auto; width: 28px; height: 28px; border: 0; border-radius: 999px; background: transparent; color: var(--ml-text-3);
    font: inherit; font-size: 18px; line-height: 1; cursor: pointer; }
  .ml-del:hover { background: rgba(255,255,255,.08); color: var(--ml-text); }
  .ml-pin { width: 8px; height: 8px; border-radius: 50%; border: 2px solid var(--ml-accent); flex: 0 0 auto; }
  .ml-pin.st { background: var(--ml-accent); }
  .ml-pin.gps { border-color: var(--ml-rain); background: var(--ml-rain); box-shadow: 0 0 0 3px rgba(94,176,245,.25); }
  .ml-toast { position: fixed; z-index: 1200; left: 50%; top: calc(env(safe-area-inset-top, 0px) + 12px); transform: translate(-50%, -8px);
    max-width: calc(100vw - 32px); padding: 8px 14px; border-radius: 999px; background: var(--ml-glass); border: 1px solid var(--ml-border);
    box-shadow: var(--ml-shadow); font-size: 13px; line-height: 18px; opacity: 0; transition: opacity .25s, transform .25s; pointer-events: none; }
  .ml-toast.on { opacity: 1; transform: translate(-50%, 0); }
  .is-pwa .ml-toast { top: calc(env(safe-area-inset-top, 0px) + 72px); }
  .ml-n { font-size: 14px; line-height: 20px; }
  .ml-s { font-size: 11px; line-height: 14px; color: var(--ml-text-2); }
  .ml-msg { padding: 10px 12px; font-size: 12px; color: var(--ml-text-3); }
  .ml-head-row { padding: 6px 12px 2px; font-size: 9.5px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; color: var(--ml-text-3); }

  .ml-ring { width: 26px; height: 26px; border-radius: 50%; border: 2px solid var(--ml-accent); box-shadow: 0 0 0 6px var(--ml-accent-fill), 0 0 12px rgba(0,0,0,.5); position: relative; box-sizing: border-box; }
  .ml-ring::after { content: ""; position: absolute; inset: 6px; border-radius: 50%; background: #fff; }

  .ml-sheet { position: fixed; z-index: 1090; left: 0; right: 0; bottom: 0; max-height: calc(100% - env(safe-area-inset-top, 0px) - 76px);
    display: flex; flex-direction: column;
    background: var(--ml-surface); border-top: 1px solid var(--ml-border); border-radius: 22px 22px 0 0; box-shadow: 0 -8px 24px rgba(0,0,0,.45);
    padding-bottom: env(safe-area-inset-bottom, 0px); transform: translateY(110%); transition: transform .42s cubic-bezier(.22,1,.36,1);
    will-change: transform; backface-visibility: hidden; }
  .ml-sheet.dragging { transition: none; }
  .ml-sheet:not(.open) { visibility: hidden; }
  @media (min-width: 900px) { .ml-sheet { left: 50%; right: auto; width: 880px; margin-left: -440px; } }
  .ml-grab { display: block; width: 100%; height: 22px; border: 0; padding: 0; background: transparent; cursor: grab; touch-action: none; flex: 0 0 auto; position: relative; }
  .ml-grab::after { content: ""; position: absolute; left: 50%; top: 9px; width: 40px; height: 5px; margin-left: -20px; border-radius: 999px; background: rgba(255,255,255,.22); }
  .ml-hero { padding: 0 18px 4px; touch-action: none; cursor: grab; flex: 0 0 auto; user-select: none; -webkit-user-select: none; }
  .ml-hero-row { display: flex; align-items: center; gap: 14px; }
  .ml-big { font-size: 56px; line-height: 1; font-weight: 300; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
  .ml-hero .ml-place b { font-size: 20px; line-height: 24px; font-weight: 700; }
  .ml-hero .ml-place span { font-size: 12.5px; line-height: 17px; color: var(--ml-text-2); }
  .ml-sky { flex: 0 0 46px; height: 46px; }
  .ml-say { margin: 10px 0 0; font-size: 15px; line-height: 21px; color: var(--ml-text); min-height: 21px; }
  .ml-chips { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
  .ml-chip { display: inline-flex; align-items: center; gap: 7px; min-height: 32px; padding: 5px 11px; border-radius: 999px; border: 0;
    background: rgba(255,255,255,.07); color: var(--ml-text-2); font: inherit; font-size: 12.5px; text-align: left; cursor: pointer; }
  .ml-chip b { color: var(--ml-text); }
  .ml-live { width: 8px; height: 8px; border-radius: 50%; background: #5ad19a; box-shadow: 0 0 0 3px rgba(90,209,154,.2); flex: 0 0 auto; }
  .ml-more { overflow-y: auto; overscroll-behavior: contain; flex: 1 1 auto; min-height: 0; padding-top: 16px; }
  .ml-tools { display: flex; align-items: center; justify-content: space-between; padding: 8px 16px 8px 18px; border-top: 1px solid var(--ml-border-soft);
    font-size: 12px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; color: var(--ml-text-3); }
  .ml-ver { padding: 0 16px 12px; display: flex; flex-direction: column; gap: 10px; }
  .ml-ver .ml-state { padding: 12px 0; }
  .ml-tiles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
  .ml-tile { background: rgba(255,255,255,.05); border-radius: 14px; padding: 10px 11px; display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .ml-tile span { font-size: 11px; color: var(--ml-text-3); }
  .ml-tile b { font-size: 22px; line-height: 28px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .ml-tile small { font-size: 11px; line-height: 14px; color: var(--ml-text-2); }
  .ml-tile small.ok { color: #7fdcae; } .ml-tile small.warn { color: var(--ml-warm); }
  .ml-vbox { background: rgba(255,255,255,.05); border-radius: 14px; padding: 10px 0 6px; }
  .ml-vhead { display: flex; justify-content: space-between; align-items: baseline; padding: 0 11px 6px; font-size: 12.5px; gap: 8px; }
  .ml-vhead span { font-size: 11px; color: var(--ml-text-3); display: flex; align-items: center; gap: 5px; white-space: nowrap; }
  .ml-vhead i { display: inline-block; width: 14px; height: 0; border-top: 2.5px solid var(--ml-warm); }
  .ml-vhead i.k-fc { border-top: 2px dashed var(--ml-rain); margin-left: 6px; }
  .ml-vchart { display: block; }
  .ml-vchart .ob { fill: none; stroke: var(--ml-warm); stroke-width: 2.2; stroke-linejoin: round; }
  .ml-vchart .obd { fill: var(--ml-warm); }
  .ml-vchart .fc { fill: none; stroke: var(--ml-rain); stroke-width: 1.8; stroke-dasharray: 5 4; }
  .ml-vchart .fut { fill: rgba(255,255,255,.03); }
  .ml-vchart .nowl { stroke: rgba(255,255,255,.35); stroke-width: 1; }
  .ml-vchart text { fill: var(--ml-text-3); font-size: 10px; }
  .ml-vchart .yl { text-anchor: start; }
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
    els.list.addEventListener('click', e => {
      const del = e.target.closest('.ml-del');
      if (del) { e.stopPropagation(); removeRecent(hits[+del.dataset.i]); renderRecent(); els.input.focus(); return; }
      const o = e.target.closest('.ml-opt'); if (o) pick(hits[+o.dataset.i]);
    });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && sheet.classList.contains('open') && !box.classList.contains('open')) closeSheet(); });
    // Toccare la mappa con la scheda alzata la riporta in basso
    ctx.map.on('click', () => { userActed = true; if (sheetState === 'half') setState('peek'); });
    bindDrag();

    const toast = document.createElement('div');
    toast.className = 'ml ml-toast';
    toast.setAttribute('role', 'status');
    document.body.appendChild(toast);
    els.toast = toast;
  }
  let toastTimer = null;
  function showToast(msg) {
    els.toast.textContent = msg;
    els.toast.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => els.toast.classList.remove('on'), 4500);
  }

  // ── Ricerca ────────────────────────────────────────────────────────────────
  function openSearch() {
    userActed = true;
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
    const rec = readRecent().filter(r => inBounds(r.lat, r.lon)).map(r => ({ ...r, recent: true }));
    const gps = geoSupported() ? [{ name: 'La tua posizione', sub: 'usa la posizione del dispositivo', gps: true }] : [];
    hits = [...gps, ...rec];
    active = 0;
    const gpsHTML = gps.length ? optHTML(gps[0], 0) : '';
    if (!rec.length) {
      showList(gpsHTML + '<div class="ml-msg">Scrivi il nome di un comune, una frazione o una stazione del Lazio.</div>');
      return;
    }
    showList(gpsHTML + '<div class="ml-head-row">Recenti</div>' + rec.map((r, i) => optHTML(r, i + gps.length)).join(''));
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
      `<span class="ml-pin${p.station ? ' st' : p.gps ? ' gps' : ''}"></span><div><div class="ml-n">${esc(p.name)}</div>` +
      (p.sub ? `<div class="ml-s">${esc(p.sub)}</div>` : '') + `</div>` +
      (p.recent ? `<button class="ml-del" type="button" data-i="${i}" aria-label="Togli ${esc(p.name)} dai recenti">×</button>` : '') +
      `</div>`;
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
    closeSearch();
    if (p.gps) { locate(false); return; }
    saveRecent({ name: p.name, sub: p.sub, lat: p.lat, lon: p.lon, elev: p.elev, station: p.station });
    const st = p.station && ctx.stations.find(s => s.name === p.name);
    if (st) { MeteoLuogo.openStation(st); return; }
    focusPlace(p);
  }

  // Centra la mappa sul luogo, mette l'anello e apre la scheda (ricerca, recenti e posizione).
  function focusPlace(p, state = 'peek', animate = true) {
    // Da computer (pannello classico) la scheda si apre subito alzata, come prima della v2
    if (!document.documentElement.classList.contains('is-pwa')) state = 'half';
    const map = ctx.map;
    map.closePopup();
    if (ringMarker) map.removeLayer(ringMarker);
    ringMarker = L.marker([p.lat, p.lon], {
      icon: L.divIcon({ className: 'ml', html: '<div class="ml-ring"></div>', iconSize: [26, 26], iconAnchor: [13, 13] }),
      interactive: false, keyboard: false,
    }).addTo(map);
    // Il foglio copre la parte bassa: il luogo va centrato nella parte di mappa che resta visibile
    // (in alto c'è anche la barra di Metek, ~70 px)
    const sheetH = (state === 'half' ? Math.min(window.innerHeight * 0.62, 520) : 230) - 70;
    const zoom = Math.max(map.getZoom(), 11);
    try {
      const pt = map.project([p.lat, p.lon], zoom).add([0, sheetH / 2]);
      // All'avvio niente volo: le invalidateSize() di assestamento della PWA lo interromperebbero a metà
      // (il volo prima allarga e poi stringe), lasciando la mappa lontana dal luogo
      if (animate) map.flyTo(map.unproject(pt, zoom), zoom, { duration: 0.8 });
      else map.setView(map.unproject(pt, zoom), zoom, { animate: false });
    } catch {
      // mappa a metà di un ridimensionamento: si centra senza animazione
      map.invalidateSize();
      map.setView([p.lat, p.lon], zoom, { animate: false });
    }
    openSheet(p, state);
  }

  // Senza posizione (permesso negato, http in rete locale…) si parte dall'ultimo luogo cercato
  function startFallback() {
    if (userActed) return;
    const r = readRecent().find(x => inBounds(x.lat, x.lon));
    if (r) focusPlace(r, 'peek', false);
  }

  // ── Posizione del dispositivo ──────────────────────────────────────────────
  // Serve un contesto sicuro (GitHub Pages è https). Precisione "di rete" (Wi-Fi/celle): basta per una
  // griglia da ~1 km ed è molto più rapida del GPS; una posizione di meno di 10 minuti fa va bene.
  const geoSupported = () => 'geolocation' in navigator && window.isSecureContext;
  const getPosition = () => new Promise((res, rej) => navigator.geolocation.getCurrentPosition(res, rej, GEO_OPTS));
  const GEO_ERR = {
    1: 'Permesso di posizione negato: puoi riattivarlo dalle impostazioni del browser.',
    2: 'Posizione non disponibile in questo momento.',
    3: 'La posizione non è arrivata in tempo. Riprova.',
  };

  // auto = true all'apertura: nessun messaggio se manca il permesso o se l'utente ha già fatto altro.
  async function locate(auto) {
    if (!geoSupported()) { if (!auto) showToast('Questo browser non fornisce la posizione.'); else startFallback(); return; }
    if (auto) {
      // Se il permesso è già stato negato non si chiede di nuovo (Safari vecchi non hanno l'API: si prova).
      try {
        const st = await navigator.permissions?.query({ name: 'geolocation' });
        if (st && st.state === 'denied') { startFallback(); return; }
      } catch { /* API assente */ }
    }
    let pos;
    try { pos = await getPosition(); }
    catch (e) { if (!auto) showToast(GEO_ERR[e.code] || GEO_ERR[2]); else startFallback(); return; }
    if (auto && userActed) return;  // nel frattempo l'utente ha cercato o toccato la mappa

    // ~100 m: più che sufficiente per la griglia, e le coordinate esatte non escono dal browser
    const lat = Math.round(pos.coords.latitude * 1000) / 1000;
    const lon = Math.round(pos.coords.longitude * 1000) / 1000;
    if (!inBounds(lat, lon)) { showToast('Sei fuori dall\'area coperta dalla mappa (Lazio).'); if (auto) startFallback(); return; }

    const acc = pos.coords.accuracy;
    const accTxt = acc && acc < 5000 ? ` · ±${acc < 1000 ? Math.round(acc / 10) * 10 + ' m' : fmt1(acc / 1000) + ' km'}` : '';
    const p = { name: 'La tua posizione', sub: 'dal dispositivo' + accTxt, lat, lon, here: true };
    focusPlace(p, 'peek', !auto);
    // Il nome arriva dopo (reverse geocoding già usato dal popup): si aggiorna l'intestazione al volo.
    if (ctx.localityName) {
      ctx.localityName(lat, lon).then(n => {
        if (!n || /°N/.test(n)) return;  // il ripiego sulle coordinate non è un nome
        p.name = n;
        p.sub = 'la tua posizione' + accTxt;
        if (els.sheet._place !== p) return;
        const b = els.sheet.querySelector('.ml-place b'), sp = els.sheet.querySelector('.ml-place span');
        if (b) b.textContent = n;
        if (sp) sp.textContent = p.sub;
      }).catch(() => {});
    }
  }

  // ── Scheda 48 h (v2: tre posizioni) ──────────────────────────────────
  // 'peek' = in basso, solo gradi, cielo, frase e stazione vicina (A1);
  // 'half' = alzata, con le 48 ore e, per le stazioni, misurato contro previsto (A2/B3);
  // giù del tutto = scheda chiusa, resta la mappa con i comandi in basso (B1).
  let sheetState = 'down';
  function closeSheet() {
    sheetState = 'down';
    els.sheet.classList.remove('open');
    els.sheet.style.transform = '';
    document.body.classList.remove('ml-sheet-open', 'ml-peek', 'ml-half');
    if (ringMarker) { ctx.map.removeLayer(ringMarker); ringMarker = null; }
  }
  // Quanto della scheda resta visibile in posizione bassa: maniglia + blocco iniziale
  function peekHeight() {
    const hero = els.sheet.querySelector('.ml-hero');
    if (!hero) return 160;
    return hero.offsetTop + hero.offsetHeight + 12;
  }
  function offsetFor(state) {
    const H = els.sheet.offsetHeight;
    if (state === 'half') return 0;
    if (state === 'peek') return Math.max(0, H - peekHeight());
    return H + 30;
  }
  function setState(state, animate = true) {
    if (state === 'down') { closeSheet(); return; }
    sheetState = state;
    const sheet = els.sheet;
    sheet.classList.toggle('dragging', !animate);
    sheet.classList.add('open');
    sheet.style.transform = `translate3d(0,${offsetFor(state)}px,0)`;
    document.body.classList.add('ml-sheet-open');
    document.body.classList.toggle('ml-peek', state === 'peek');
    document.body.classList.toggle('ml-half', state === 'half');
    sheet.querySelector('.ml-grab')?.setAttribute('aria-label', state === 'peek' ? 'Mostra le previsioni' : 'Riduci la scheda');
  }
  // Il contenuto è cambiato (dati arrivati): la posizione si ricalcola senza saltare
  function relayout() { if (sheetState !== 'down') setState(sheetState, true); }
  window.addEventListener('resize', () => relayout());

  // Trascinamento: dalla maniglia e dal blocco in alto. Un tocco senza movimento alterna basso/alto.
  function bindDrag() {
    const sheet = els.sheet;
    let y0 = null, base = 0, last = 0, lastT = 0, vel = 0, moved = false;
    sheet.addEventListener('pointerdown', e => {
      if (!e.target.closest('.ml-grab, .ml-hero')) return;
      const ctl = e.target.closest('button, a');
      if (ctl && !ctl.classList.contains('ml-grab')) return;   // il chip della stazione resta un bottone
      y0 = e.clientY; base = offsetFor(sheetState); last = e.clientY; lastT = e.timeStamp; vel = 0; moved = false;
      sheet.setPointerCapture(e.pointerId);
    });
    let raf = 0, pending = 0;
    sheet.addEventListener('pointermove', e => {
      if (y0 == null) return;
      const dy = e.clientY - y0;
      if (Math.abs(dy) > 6) moved = true;
      if (!moved) return;
      // Velocità smussata: un singolo scatto del dito non decide il lancio
      const dt = Math.max(1, e.timeStamp - lastT);
      vel = 0.7 * ((e.clientY - last) / dt) + 0.3 * vel; last = e.clientY; lastT = e.timeStamp;
      // Tutta alzata è il massimo: oltre non sale, così sotto non resta spazio vuoto
      pending = Math.max(0, base + dy);
      sheet.classList.add('dragging');
      if (!raf) raf = requestAnimationFrame(() => { raf = 0; sheet.style.transform = `translate3d(0,${pending}px,0)`; });
    });
    const end = e => {
      if (y0 == null) return;
      const dy = e.clientY - y0;
      y0 = null;
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      sheet.classList.remove('dragging');
      if (!moved) { setState(sheetState === 'peek' ? 'half' : 'peek'); return; }
      const off = base + dy;
      const peekOff = offsetFor('peek');
      // Lancio veloce: segue la direzione; altrimenti la posizione più vicina
      if (vel < -0.5) setState(sheetState === 'peek' || off < peekOff ? 'half' : 'peek');
      else if (vel > 0.5) setState(off > peekOff + 20 || sheetState === 'peek' ? 'down' : 'peek');
      else if (off > peekOff + 60) setState('down');
      else setState(off < peekOff / 2 ? 'half' : 'peek');
    };
    sheet.addEventListener('pointerup', end);
    sheet.addEventListener('pointercancel', end);
  }

  // ── Frase riassuntiva e stazione vicina ─────────────────────────────────────
  const dayKey = ms => new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Rome' }).format(new Date(ms));
  const hhmm = ms => new Date(ms).toLocaleTimeString('it-IT', { timeZone: 'Europe/Rome', hour: '2-digit', minute: '2-digit' });
  const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
  function whenTxt(c) {
    const nowH = romeParts(Date.now()).hour;
    if (dayKey(c.ms) === dayKey(Date.now())) return (c.hour >= 18 ? 'stasera alle ' : 'alle ') + c.hour;
    if (c.hour < 7 && nowH >= 17) return 'stanotte alle ' + c.hour;
    return 'domani alle ' + c.hour;
  }
  function summaryText(cols1, hasNWP) {
    const c24 = cols1.slice(0, 24).filter(c => c.t != null);
    if (!c24.length) return '';
    const hi = c24.reduce((a, b) => (b.t > a.t ? b : a)), lo = c24.reduce((a, b) => (b.t < a.t ? b : a));
    const out = [];
    if (hasNWP && cols1[0]?.wc != null) out.push(cap(SKY_TEXT(cols1[0].wc)) + '.');
    out.push(`Massima ${Math.round(hi.t)}° ${whenTxt(hi)}, minima ${Math.round(lo.t)}° ${whenTxt(lo)}.`);
    if (hasNWP) {
      const wet = c24.filter(c => c.pr != null && c.pr >= 0.2);
      if (!wet.length) out.push('Niente pioggia nelle prossime 24 ore.');
      else {
        const tot = wet.reduce((a, c) => a + c.pr, 0);
        out.push(`Pioggia ${whenTxt(wet[0])}, ${fmt1(tot)} mm in tutto.`);
      }
    }
    return out.join(' ');
  }
  function kmBetween(a, b, c, d) {
    const R = 6371, r = Math.PI / 180;
    const x = (d - b) * r * Math.cos((a + c) / 2 * r), y = (c - a) * r;
    return Math.sqrt(x * x + y * y) * R;
  }
  function nearestStation(p) {
    let best = null;
    for (const s of ctx.stations) {
      if (s.observation?.temperature == null) continue;
      const d = kmBetween(p.lat, p.lon, s.lat, s.lon);
      if (d <= 8 && (!best || d < best.d)) best = { s, d };
    }
    return best;
  }
  function chipsHTML(p) {
    if (p.st) return '';
    const n = nearestStation(p);
    if (!n) return '';
    const o = n.s.observation;
    return `<div class="ml-chips"><button type="button" class="ml-chip ml-chip-btn" data-st="${n.s.id}">` +
      `<span class="ml-live"></span><span>Stazione ${esc(n.s.name)}, a ${fmt1(n.d)} km: <b>${fmt1(o.temperature)}°</b> alle ${hhmm(Date.parse(o.recorded_at))}</span></button></div>`;
  }

  function heroHTML(p, now) {
    const sub = p.st ? 'stazione della rete' : (p.sub || `${p.lat.toFixed(3)}° N, ${p.lon.toFixed(3)}° E`);
    return `<button type="button" class="ml-grab" aria-label="Mostra le previsioni"></button>` +
      `<div class="ml-hero">` +
      `<div class="ml-hero-row">` +
      `<div class="ml-big">${now != null ? Math.round(now) + '°' : '–'}</div>` +
      `<div class="ml-place"><b>${esc(p.name)}</b><span>${esc(sub)}</span></div>` +
      `<div class="ml-sky"></div>` +
      `</div>` +
      `<p class="ml-say">${p.st ? 'Misurata adesso dalla stazione.' : 'Calcolo le previsioni…'}</p>` +
      chipsHTML(p) +
      `</div>`;
  }

  async function openSheet(p, state = 'peek') {
    const sheet = els.sheet;
    sheet._place = p;
    const now = p.st ? (p.st.observation?.temperature ?? ctx.nowTemp(p.lat, p.lon)) : ctx.nowTemp(p.lat, p.lon);
    sheet.innerHTML = heroHTML(p, now) + '<div class="ml-more"><div class="ml-state">Calcolo le previsioni…</div></div>';
    sheet.querySelector('.ml-chip-btn')?.addEventListener('click', e => {
      const s = ctx.stations.find(x => String(x.id) === e.currentTarget.dataset.st);
      if (s) MeteoLuogo.openStation(s);
    });
    sheetState = sheetState === 'down' ? state : (state === 'half' ? 'half' : sheetState);
    // Prima apertura: parte da sotto lo schermo e sale
    if (!sheet.classList.contains('open')) {
      sheet.classList.add('dragging');
      sheet.style.transform = `translateY(${sheet.offsetHeight + 30}px)`;
      sheet.offsetHeight;  // forza il layout prima dell'animazione
    }
    requestAnimationFrame(() => setState(sheetState));
    const token = (sheet._token = {});

    const ok = await ctx.whenReady;
    const series = ok ? ctx.pointSeries(p.lat, p.lon) : null;
    if (sheet._token !== token) return;
    if (!series || !series.length) {
      sheet.querySelector('.ml-state').textContent = 'Le previsioni a 48 ore non sono disponibili in questo momento.';
      sheet.querySelector('.ml-say').textContent = '';
      relayout();
      return;
    }
    const nwp = await fetchNWP(p.lat, p.lon);
    if (sheet._token !== token) return;

    const c1 = columns(series, nwp, 1);
    if (nwp && c1[0]) sheet.querySelector('.ml-sky').innerHTML = iconSVG(c1[0].wc, c1[0].isDay).replace('width="28" height="28"', 'width="46" height="46"');
    const say = summaryText(c1, !!nwp);
    sheet.querySelector('.ml-say').textContent = p.st ? `Misurata adesso. ${say}` : say;

    sheet.querySelector('.ml-more').innerHTML =
      (p.st ? `<div class="ml-ver"><div class="ml-state">Carico misure e previsioni passate…</div></div>` : '') +
      `<div class="ml-tools"><span>Prossime 48 ore</span><div class="ml-step" role="group" aria-label="Passo orario">` +
      [3, 1].map(v => `<button type="button" data-step="${v}" aria-pressed="${v === step}">${v} h</button>`).join('') + `</div></div>` +
      `<div class="ml-body"><div class="ml-gutter"></div><div class="ml-scroll"><div class="ml-grid"></div></div></div>` +
      `<div class="ml-foot"></div>`;
    if (p.st) renderVerification(p, series, token);
    relayout();
    const draw = () => {
      sheet.querySelectorAll('.ml-step button').forEach(b => b.setAttribute('aria-pressed', String(+b.dataset.step === step)));
      const grid = sheet.querySelector('.ml-grid'), gutter = sheet.querySelector('.ml-gutter');
      // Cambiando passo la tabella si svuota e si ridisegna: senza questo la scheda, svuotata per un
      // attimo, perdeva lo scorrimento verticale e tornava in cima (grafico della stazione)
      const more = sheet.querySelector('.ml-more'), keepY = more ? more.scrollTop : 0;
      if (more) more.style.minHeight = more.scrollHeight + 'px';
      grid.style.minHeight = grid.offsetHeight + 'px';
      grid.innerHTML = ''; gutter.innerHTML = '';
      sheet.querySelector('.ml-scroll').scrollLeft = 0;
      renderGrid(grid, gutter, columns(series, nwp, step), !!nwp, step === 3 ? COL3 : COL1);
      grid.style.minHeight = '';
      if (more) { more.style.minHeight = ''; more.scrollTop = keepY; }
      sheet.querySelector('.ml-foot').textContent =
        'Temperatura, percepita e vento medio: modello M2, stazioni della rete. ' +
        (nwp ? `Cielo e pioggia (mm ${step === 3 ? 'nelle 3 ore' : "nell'ora"}): ICON-2I via Open-Meteo.`
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
    relayout();
  }

  // ── Stazione: misurato contro previsto (B3) ─────────────────────────────────
  // data/dashboard_data.json (lo stesso della Dashboard): per ogni stazione le previsioni M2 ora per
  // ora e le misure degli ultimi giorni. Si scarica una volta sola, al primo tocco su una stazione.
  let dashP = null;
  const dashData = () => (dashP ||= fetch('data/dashboard_data.json', { cache: 'no-store' })
    .then(r => (r.ok ? r.json() : null)).catch(() => null));

  async function renderVerification(p, series, token) {
    const d = await dashData();
    const box = els.sheet.querySelector('.ml-ver');
    if (els.sheet._token !== token || !box) return;
    const ser = d?.series?.[String(p.st.id)];
    if (!ser?.observed?.length || !ser?.forecast?.length) {
      box.innerHTML = '<div class="ml-state">Lo storico di questa stazione non è disponibile.</div>';
      relayout();
      return;
    }
    const now = Date.now(), H = 3.6e6, t0 = now - 24 * H, t1 = now + 24 * H;
    const obs = ser.observed.map(o => ({ ms: Date.parse(o.t), t: o.temp, h: o.hum })).filter(o => o.t != null && o.ms >= t0 - H);
    // dashboard_data si aggiorna meno spesso di latest.json: la misura più recente si prende da lì
    const lo0 = p.st.observation;
    if (lo0?.temperature != null && lo0.recorded_at) {
      const ms = Date.parse(lo0.recorded_at);
      if (!obs.length || ms > obs[obs.length - 1].ms + 60e3) obs.push({ ms, t: lo0.temperature, h: lo0.humidity });
    }
    const fcPast = ser.forecast.map(f => ({ ms: Date.parse(f.t), t: f.temp, h: f.hum })).filter(f => f.t != null && f.ms >= t0 && f.ms <= now);
    const fcNext = series.map(s => ({ ms: Date.parse(s.time), t: s.t })).filter(f => f.t != null && f.ms <= t1);
    // Coppie: ogni ora prevista con la misura più vicina entro 35 minuti
    const pairs = [];
    for (const f of fcPast) {
      let best = null;
      for (const o of obs) { const dt = Math.abs(o.ms - f.ms); if (dt <= 35 * 60e3 && (!best || dt < best.dt)) best = { o, dt }; }
      if (best) pairs.push({ f, o: best.o });
    }
    const mae = pairs.length ? pairs.reduce((a, x) => a + Math.abs(x.o.t - x.f.t), 0) / pairs.length : null;
    const lastObs = obs.filter(o => o.ms <= now).pop();
    // Ultima misura contro la previsione più vicina nel tempo (entro 45 minuti)
    let lastPair = null;
    if (lastObs) {
      for (const f of [...fcPast, ...series.map(s => ({ ms: Date.parse(s.time), t: s.t, h: s.rh }))]) {
        const dt = Math.abs(f.ms - lastObs.ms);
        if (f.t != null && dt <= 45 * 60e3 && (!lastPair || dt < lastPair.dt)) lastPair = { o: lastObs, f, dt };
      }
    }
    const diff = lastPair ? lastPair.o.t - lastPair.f.t : null;
    const diffTxt = diff == null ? '' : Math.abs(diff) < 0.5 ? 'come previsto'
      : `${fmt1(Math.abs(diff))}° più ${diff > 0 ? 'calda' : 'fredda'} del previsto`;
    const hDiff = lastPair && lastPair.o.h != null && lastPair.f.h != null ? `prevista ${Math.round(lastPair.f.h)}%` : '';

    // Grafico: −24 h … +24 h, misura (arancio) e previsione (azzurro tratteggiato)
    const W = Math.max(280, els.sheet.clientWidth - 32), Hh = 120, padT = 10, padB = 18;
    const all = [...obs.filter(o => o.ms >= t0), ...fcPast, ...fcNext].map(x => x.t);
    const lo = Math.floor(Math.min(...all) - 1), hi = Math.ceil(Math.max(...all) + 1);
    const X = ms => ((ms - t0) / (t1 - t0)) * W;
    const Y = v => padT + (1 - (v - lo) / (hi - lo)) * (Hh - padT - padB);
    const path = arr => arr.length ? 'M' + arr.map(x => X(x.ms).toFixed(1) + ' ' + Y(x.t).toFixed(1)).join(' L') : '';
    const obsIn = obs.filter(o => o.ms >= t0 && o.ms <= now);
    const ticks = [-24, -12, 0, 12, 24].map(k => {
      const ms = now + k * H;
      const lab = k === 0 ? 'ora' : hhmm(ms).slice(0, 2);
      return `<text x="${Math.min(W - 8, Math.max(8, X(ms))).toFixed(1)}" y="${Hh - 3}" text-anchor="middle">${lab}</text>`;
    }).join('');
    const svg =
      `<svg class="ml-vchart" width="${W}" height="${Hh}" viewBox="0 0 ${W} ${Hh}" role="img" aria-label="Temperatura misurata e prevista, dalle 24 ore passate alle 24 future">` +
      `<rect x="${X(now)}" y="0" width="${W - X(now)}" height="${Hh - padB}" class="fut"/>` +
      `<line x1="${X(now)}" x2="${X(now)}" y1="0" y2="${Hh - padB}" class="nowl"/>` +
      `<text x="4" y="${padT + 4}" class="yl">${hi}°</text><text x="4" y="${Hh - padB - 2}" class="yl">${lo}°</text>` +
      `<path d="${path([...fcPast, ...fcNext])}" class="fc"/>` +
      `<path d="${path(obsIn)}" class="ob"/>` +
      (lastObs ? `<circle cx="${X(lastObs.ms)}" cy="${Y(lastObs.t)}" r="3.5" class="obd"/>` : '') +
      ticks + `</svg>`;

    box.innerHTML =
      `<div class="ml-tiles">` +
      `<div class="ml-tile"><span>Misurata ${lastObs ? 'alle ' + hhmm(lastObs.ms) : ''}</span><b>${lastObs ? fmt1(lastObs.t) + '°' : '–'}</b><small class="${diff != null && Math.abs(diff) >= 2 ? 'warn' : 'ok'}">${diffTxt}</small></div>` +
      `<div class="ml-tile"><span>Umidità</span><b>${lastObs?.h != null ? Math.round(lastObs.h) + '%' : '–'}</b><small>${hDiff}</small></div>` +
      `<div class="ml-tile"><span>Errore medio 24 h</span><b>${mae != null ? fmt1(mae) + '°' : '–'}</b><small>${pairs.length} ore confrontate</small></div>` +
      `</div>` +
      `<div class="ml-vbox"><div class="ml-vhead"><b>Misurato e previsto</b><span><i class="k-ob"></i>misura <i class="k-fc"></i>previsione M2</span></div>${svg}</div>`;
    relayout();
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
          // Open-Meteo scrive all'ora T ciò che è successo tra T−1 e T: pioggia, codice del cielo e
          // raffica. La colonna dell'ora h vale per h → h+n, quindi si leggono le ore h+1 … h+n.
          // (Prima il cielo si leggeva all'ora h: l'icona di pioggia finiva un'ora dopo i mm.)
          const nxt = nwp.get(ms + (k + 1) * 3.6e6);
          if (nxt?.wc != null) wc = wc == null ? nxt.wc : Math.max(wc, nxt.wc);
          if (nxt?.gust != null) {
            const g = Math.max(nxt.gust, wsAt.get(ms + k * 3.6e6) ?? 0);  // la raffica non è mai sotto la media
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
    // Chiamata da app.js dopo il fitBounds iniziale: apre la scheda sulla posizione, se possibile.
    locateOnStart() {
      if (ctx) locate(true);
    },
    // Sito: la lente accanto a "Metek" apre la ricerca
    openSearch() {
      if (ctx) openSearch();
    },
    // Popup della mappa: scheda 48 h sul punto toccato (non finisce nei recenti)
    open(p) {
      if (ctx && p) { userActed = true; focusPlace(p); }
    },
    // Tocco su una stazione: scheda alzata con misurato e previsto (B3)
    openStation(st) {
      if (!ctx || !st) return;
      userActed = true;
      focusPlace({ name: st.name, lat: st.lat, lon: st.lon, st }, 'half');
    },
  };
})();
