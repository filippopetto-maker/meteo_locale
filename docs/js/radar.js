(function () {
  'use strict';

  // Redesign "Metek" attivo SOLO in PWA standalone — stesso check di app.js, indipendente
  // (nessuna dipendenza cross-file: radar.js gira anche da solo prima che app.js finisca).
  const isPWA = document.documentElement.classList.contains('is-pwa');

  const META_URL   = 'https://api.rainviewer.com/public/weather-maps.json';
  const COLOR_SCHEME     = 2;     // "Universal Blue" — verificato su rainviewer.com/api/color-schemes.html
  const TILE_OPTIONS     = '1_1'; // smooth=1, snow=1
  const TILE_SIZE         = 256;
  const MAX_NATIVE_ZOOM   = 7; // oltre, RainViewer risponde con un placeholder "Zoom Level Not Supported"
  const OPACITY           = 0.6;
  const PLAY_INTERVAL_MS  = 500;
  const REFRESH_INTERVAL_MS = 10 * 60 * 1000;

  let map = null;
  let frames = [];        // [{ time, path, kind: 'past'|'nowcast', url }]
  let nowIndex = -1;      // indice dell'ultimo frame 'past' == "adesso"
  let currentIndex = -1;
  let tileLayer = null;
  let playTimer = null;
  let refreshTimer = null;
  let playing = false;
  let active = false;
  let updateCallback = null;

  // ─── DOM ──────────────────────────────────────────────────────────────
  let elTimeline, elPlayBtn, elSlider, elNowMarker,
      elLabelStart, elLabelNow, elLabelEnd, elUnavailable, elControls;

  // Disclaimer del dato radar — in PWA il nowcast non esiste più (solo `past`), quindi la
  // frase sul nowcast va tolta; nel pannello legacy il nowcast c'è ancora, disclaimer invariato.
  const RADAR_DISCLAIMER = isPWA
    ? `Dato di osservazione radar esterno (RainViewer), non previsione del modello MOS. Risoluzione aggregata ~1-2 km.`
    : `Dato di osservazione radar esterno (RainViewer), non previsione del modello MOS. Risoluzione aggregata ~1-2 km. I frame oltre 'Adesso' sono nowcast estrapolato, non output del nostro modello.`;

  function buildDom() {
    elTimeline = document.createElement('div');
    elTimeline.id = 'radar-timeline';
    elTimeline.className = 'radar-timeline';
    elTimeline.style.display = 'none';
    // PWA: card compatta come la barra Ora/+48 h — play, cursore e ora del fotogramma su una riga,
    // sotto la scala colori (spostata qui da app.js). Credito RainViewer nell'attribuzione della mappa.
    elTimeline.innerHTML = isPWA ?
      `<div class="radar-timeline-row" id="radar-controls">` +
      `<button id="radar-play" class="radar-play-btn" title="Play/Pausa" type="button">▶</button>` +
      `<div class="radar-track-wrap">` +
      `<input type="range" id="radar-slider" class="radar-slider" min="0" max="0" step="1" value="0" aria-label="Fotogramma radar">` +
      `<div id="radar-now-marker" class="radar-now-marker"></div>` +
      `</div>` +
      `<span id="radar-label-now" class="fc48-now radar-note" title="${RADAR_DISCLAIMER}"></span>` +
      `</div>` +
      `<div id="radar-labels-row" hidden><span id="radar-label-start"></span><span id="radar-label-end"></span></div>` +
      `<div class="radar-legend-slot"></div>` +
      `<div id="radar-unavailable" class="radar-unavailable" style="display:none">⚠️ Radar non disponibile</div>`
      :
      `<div class="radar-timeline-row" id="radar-controls">` +
      `<button id="radar-play" class="radar-play-btn" title="Play/Pausa" type="button">▶</button>` +
      `<div class="radar-track-wrap">` +
      `<input type="range" id="radar-slider" class="radar-slider" min="0" max="0" step="1" value="0">` +
      `<div id="radar-now-marker" class="radar-now-marker"></div>` +
      `</div>` +
      `</div>` +
      `<div class="radar-timeline-labels" id="radar-labels-row">` +
      `<span id="radar-label-start"></span>` +
      `<span id="radar-label-now"></span>` +
      `<span id="radar-label-end"></span>` +
      `</div>` +
      `<div id="radar-unavailable" class="radar-unavailable" style="display:none">⚠️ Radar non disponibile</div>` +
      `<div class="radar-credit">` +
      `Radar: <a href="https://www.rainviewer.com/" target="_blank" rel="noopener">RainViewer</a>` +
      ` · <span class="radar-note" title="${RADAR_DISCLAIMER}">ⓘ limiti del dato</span>` +
      `</div>`;
    document.body.appendChild(elTimeline);

    elControls    = document.getElementById('radar-controls');
    elPlayBtn     = document.getElementById('radar-play');
    elSlider      = document.getElementById('radar-slider');
    elNowMarker   = document.getElementById('radar-now-marker');
    elLabelStart  = document.getElementById('radar-label-start');
    elLabelNow    = document.getElementById('radar-label-now');
    elLabelEnd    = document.getElementById('radar-label-end');
    elUnavailable = document.getElementById('radar-unavailable');

    elPlayBtn.addEventListener('click', togglePlay);
    elSlider.addEventListener('input', (e) => {
      if (playing) pause();
      showFrame(parseInt(e.target.value, 10));
    });
  }

  // ─── Formattazione ────────────────────────────────────────────────────
  function formatEpoch(sec) {
    return new Date(sec * 1000).toLocaleTimeString('it-IT', {
      timeZone: 'Europe/Rome', hour: '2-digit', minute: '2-digit',
    });
  }

  function minutesLabel(min) {
    const abs = Math.abs(min);
    if (abs >= 60 && abs % 60 === 0) return `${abs / 60}h`;
    return `${abs}min`;
  }

  // ─── Fetch & costruzione frame ────────────────────────────────────────
  function buildUrl(host, path) {
    return `${host}${path}/${TILE_SIZE}/{z}/{x}/{y}/${COLOR_SCHEME}/${TILE_OPTIONS}.png`;
  }

  async function fetchRadarFrames() {
    try {
      const res = await fetch(META_URL);
      if (!res.ok) throw new Error(`weather-maps.json: HTTP ${res.status}`);
      const data = await res.json();
      const host     = data.host;
      const past     = (data.radar && data.radar.past)     || [];
      // PWA: timeline finisce ad "Adesso", niente nowcast (cambio funzionale richiesto dal
      // redesign). Browser normale: invariato, past + nowcast come oggi.
      const nowcast  = isPWA ? [] : ((data.radar && data.radar.nowcast) || []);

      const newFrames = [
        ...past.map(f    => ({ time: f.time, path: f.path, kind: 'past',    url: buildUrl(host, f.path) })),
        ...nowcast.map(f => ({ time: f.time, path: f.path, kind: 'nowcast', url: buildUrl(host, f.path) })),
      ];
      if (newFrames.length === 0) throw new Error('nessun frame radar disponibile');

      const wasEmpty = frames.length === 0;
      const previousTime = (!wasEmpty && currentIndex >= 0 && frames[currentIndex])
        ? frames[currentIndex].time : null;

      frames = newFrames;
      nowIndex = past.length - 1;

      if (wasEmpty) {
        currentIndex = nowIndex;
      } else if (previousTime != null) {
        const sameIdx = frames.findIndex(f => f.time === previousTime);
        currentIndex = sameIdx >= 0 ? sameIdx : Math.min(currentIndex, frames.length - 1);
      }
      return true;
    } catch (err) {
      console.error('Radar: fetch metadati fallito —', err);
      return false;
    }
  }

  function getLastObservedTime() {
    return (nowIndex >= 0 && frames[nowIndex]) ? frames[nowIndex].time : null;
  }

  // ─── Rendering frame ────────────────────────────────────────────────
  // Scorrimento fluido: un livello per fotogramma, caricato in anticipo (opacità 0); cambiare
  // fotogramma = cambiare quale livello è visibile, con una breve dissolvenza (.radar-frame in
  // index.html). Se il fotogramma scelto non ha ancora finito di caricare, quello di prima resta
  // visibile finché non arriva, così la mappa non resta vuota.
  // Per contenere rete e memoria sulla mappa stanno solo i fotogrammi vicini a quello mostrato
  // (±NEIGHBORS); durante il play tutti, perché l'animazione li attraversa in fila. I livelli
  // tolti liberano le immagini decodificate; i riquadri restano nella cache HTTP del browser.
  const frameLayers = new Map();   // path → { layer, loaded }
  let shownPath = null;            // fotogramma visibile adesso

  function layerFor(frame) {
    let entry = frameLayers.get(frame.path);
    if (entry) return entry;
    const layer = L.tileLayer(frame.url, {
      opacity: 0, maxZoom: 19, maxNativeZoom: MAX_NATIVE_ZOOM, className: 'radar-frame',
      attribution: isPWA ? 'Radar <a href="https://www.rainviewer.com/" target="_blank" rel="noopener">RainViewer</a>' : '',
    });
    entry = { layer, loaded: false };
    layer.on('loading', () => { entry.loaded = false; });
    layer.on('load', () => {
      entry.loaded = true;
      // era il fotogramma richiesto ma non ancora pronto: ora si può nascondere il precedente
      if (frames[currentIndex] && frames[currentIndex].path === frame.path) reveal(frame.path);
    });
    frameLayers.set(frame.path, entry);
    layer.addTo(map);
    return entry;
  }

  function reveal(path) {
    for (const [p, e] of frameLayers) e.layer.setOpacity(p === path ? OPACITY : 0);
    shownPath = path;
    tileLayer = frameLayers.get(path).layer;
  }

  const NEIGHBORS = 2;

  // Livelli per i fotogrammi da tenere pronti; via gli altri (mai quello ancora a schermo)
  function syncFrameLayers() {
    const wanted = playing
      ? frames
      : frames.filter((_, i) => Math.abs(i - currentIndex) <= NEIGHBORS);
    const keep = new Set(wanted.map(f => f.path));
    if (shownPath) keep.add(shownPath);
    for (const [p, e] of frameLayers) {
      if (!keep.has(p)) { map.removeLayer(e.layer); frameLayers.delete(p); }
    }
    // prima i più vicini al fotogramma corrente, così arrivano per primi
    wanted.slice()
      .sort((a, b) => Math.abs(frames.indexOf(a) - currentIndex) - Math.abs(frames.indexOf(b) - currentIndex))
      .forEach(layerFor);
  }

  function clearFrameLayers() {
    for (const e of frameLayers.values()) map.removeLayer(e.layer);
    frameLayers.clear();
    shownPath = null;
    tileLayer = null;
  }

  function showFrame(index) {
    if (!map || index < 0 || index >= frames.length) return;
    currentIndex = index;
    const frame = frames[index];
    const entry = layerFor(frame);
    // Non ancora pronto (e c'è già qualcosa a schermo): resta il precedente, il 'load' farà il cambio
    if (entry.loaded || !shownPath || !frameLayers.has(shownPath)) reveal(frame.path);
    syncFrameLayers();
    syncSliderPosition();
    updateNowLabel();
  }

  // ─── Timeline UI ────────────────────────────────────────────────────
  function renderTimelineStructure() {
    if (frames.length === 0) return;
    const total = frames.length;
    elSlider.max = String(total - 1);
    elSlider.value = String(currentIndex);

    const pastFrac = total > 1 ? (nowIndex / (total - 1)) * 100 : 100;
    elSlider.style.background =
      `linear-gradient(to right, rgba(255,255,255,0.25) 0%, rgba(255,255,255,0.25) ${pastFrac}%, ` +
      `var(--accent) ${pastFrac}%, var(--accent) 100%)`;
    elNowMarker.style.left = pastFrac + '%';

    const nowTime = getLastObservedTime();
    const startDiffMin = Math.round((frames[0].time - nowTime) / 60);
    elLabelStart.textContent = `${minutesLabel(startDiffMin)} fa`;
    elLabelNow.textContent = `Adesso (${formatEpoch(nowTime)})`;
    updateNowLabel();

    if (nowIndex < total - 1) {
      const endDiffMin = Math.round((frames[total - 1].time - nowTime) / 60);
      elLabelEnd.textContent = `+${minutesLabel(endDiffMin)}`;
      elLabelEnd.style.visibility = 'visible';
    } else {
      elLabelEnd.textContent = '';
      elLabelEnd.style.visibility = 'hidden';
    }
  }

  // PWA: l'etichetta a destra del cursore dice l'ora del fotogramma mostrato
  function updateNowLabel() {
    if (!isPWA || !elLabelNow || currentIndex < 0 || !frames[currentIndex]) return;
    const t = formatEpoch(frames[currentIndex].time);
    elLabelNow.textContent = currentIndex === nowIndex ? `Adesso ${t}` : t;
  }

  function syncSliderPosition() {
    if (document.activeElement !== elSlider) {
      elSlider.value = String(currentIndex);
    }
  }

  function showUnavailable(show) {
    elUnavailable.style.display = show ? '' : 'none';
    elControls.style.display = show ? 'none' : '';
    document.getElementById('radar-labels-row').style.display = show ? 'none' : '';
  }

  // ─── Play / pause ───────────────────────────────────────────────────
  function updatePlayButtonUI() {
    elPlayBtn.textContent = playing ? '⏸' : '▶';
  }

  function play() {
    if (playTimer || frames.length < 2) return;
    playing = true;
    updatePlayButtonUI();
    syncFrameLayers();   // in play servono tutti
    playTimer = setInterval(() => {
      let next = currentIndex + 1;
      if (next >= frames.length) next = 0;
      showFrame(next);
    }, PLAY_INTERVAL_MS);
  }

  function pause() {
    if (playTimer) { clearInterval(playTimer); playTimer = null; }
    playing = false;
    updatePlayButtonUI();
    if (map && active) syncFrameLayers();   // torna ai soli vicini
  }

  function togglePlay() { playing ? pause() : play(); }

  // ─── Auto refresh (ogni 10 min, senza resettare l'animazione) ──────────
  function startAutoRefresh() {
    stopAutoRefresh();
    refreshTimer = setInterval(async () => {
      const ok = await fetchRadarFrames();
      if (ok) {
        syncFrameLayers();
        renderTimelineStructure();
        if (updateCallback) updateCallback(getLastObservedTime());
      }
    }, REFRESH_INTERVAL_MS);
  }

  function stopAutoRefresh() {
    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  }

  // ─── API pubblica ───────────────────────────────────────────────────
  async function activate() {
    active = true;
    if (!elTimeline) buildDom();
    elTimeline.style.display = 'flex';

    const ok = await fetchRadarFrames();
    if (!ok) {
      showUnavailable(true);
      if (updateCallback) updateCallback(null);
      return;
    }
    showUnavailable(false);
    renderTimelineStructure();
    showFrame(currentIndex);   // carica anche i vicini
    startAutoRefresh();
    if (updateCallback) updateCallback(getLastObservedTime());
  }

  function deactivate() {
    active = false;
    stopAutoRefresh();
    pause();
    if (map) clearFrameLayers();
    if (elTimeline) elTimeline.style.display = 'none';
  }

  function init(leafletMap) {
    map = leafletMap;
  }

  window.RadarLayer = {
    init,
    activate,
    deactivate,
    isActive: () => active,
    onUpdate: (cb) => { updateCallback = cb; },
  };
})();
