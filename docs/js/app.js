(function () {
  'use strict';

  function degreesToCardinal(deg) {
    const labels = ['N','NNE','NE','ENE','E','ESE','SE','SSE',
                    'S','SSO','SO','OSO','O','ONO','NO','NNO'];
    return labels[Math.round(deg / 22.5) % 16];
  }

  const WIND_NAMES = {
    N:   'Tramontana', NNE: 'Bora',        NE: 'Grecale',   ENE: 'Schiavo',
    E:   'Levante',    ESE: 'Solano',      SE: 'Scirocco',  SSE: 'Africo',
    S:   'Ostro',      SSO: 'Cauro',       SO: 'Libeccio',  OSO: 'Etesia',
    O:   'Ponente',    ONO: 'Traversone',  NO: 'Maestrale', NNO: 'Zefiro',
  };

  function windName(deg) {
    if (deg == null) return '';
    const label = degreesToCardinal(deg);
    if (label.length <= 2) return WIND_NAMES[label];
    // 3 lettere: il primo carattere della sigla è sempre il vento a 1 lettera più vicino
    return `${WIND_NAMES[label[0]]} - ${WIND_NAMES[label]}`;
  }

  function formatTime(isoStr) {
    return new Date(isoStr).toLocaleTimeString('it-IT', {
      timeZone: 'Europe/Rome', hour: '2-digit', minute: '2-digit',
    });
  }

  const HUM_SCALE_MIN  =   0;  // %
  const HUM_SCALE_MAX  = 100;  // %
  const TEMP_MIN_SPAN  =  15;  // °C — larghezza minima scala temperatura

  // Palettes: ogni stop ha { t, r, g, b }
  const TEMP_PALETTE = [
    { t: 0.000, r: 0x2c, g: 0x3e, b: 0x95 },
    { t: 0.125, r: 0x3a, g: 0x6f, b: 0xc4 },
    { t: 0.250, r: 0x4f, g: 0xb8, b: 0xc4 },
    { t: 0.375, r: 0x6f, g: 0xc4, b: 0x6a },
    { t: 0.500, r: 0xd4, g: 0xd2, b: 0x4a },
    { t: 0.625, r: 0xf4, g: 0xa9, b: 0x3f },
    { t: 0.750, r: 0xe8, g: 0x54, b: 0x2f },
    { t: 0.875, r: 0xa5, g: 0x00, b: 0x26 },
    { t: 1.000, r: 0x67, g: 0x00, b: 0x1f },
  ];
  const HUM_PALETTE = [
    { t: 0.00, r: 0xd9, g: 0x6f, b: 0x27 }, // #d96f27 — arancio secco
    { t: 0.30, r: 0xfe, g: 0xe0, b: 0x80 }, // #fee080 — giallo paglierino
    { t: 0.50, r: 0xb0, g: 0xe0, b: 0x90 }, // #b0e090 — verde chiaro (pivot visivo)
    { t: 0.75, r: 0x31, g: 0x7e, b: 0xc8 }, // #317ec8 — blu medio
    { t: 1.00, r: 0x08, g: 0x30, b: 0x6b }, // #08306b — blu scuro umido
  ];
  const WIND_PALETTE = [
    { t: 0.00, r: 0x00, g: 0x33, b: 0x99 }, // blu scuro — calma
    { t: 0.20, r: 0x00, g: 0x99, b: 0xff }, // azzurro — brezza
    { t: 0.40, r: 0x00, g: 0xcc, b: 0x66 }, // verde — moderato
    { t: 0.60, r: 0xff, g: 0xdd, b: 0x00 }, // giallo — sostenuto
    { t: 0.80, r: 0xff, g: 0x66, b: 0x00 }, // arancio — forte
    { t: 1.00, r: 0xcc, g: 0x00, b: 0x00 }, // rosso — molto forte
  ];
  const WIND_SPEED_MIN = 0;   // fallback se wind_speed_grid non disponibile
  const WIND_SPEED_MAX = 50;

  function lerp(a, b, f) { return a + (b - a) * f; }

  function valueToColor(value, vMin, vMax, palette) {
    const norm = Math.max(0, Math.min(1, (value - vMin) / (vMax - vMin || 1)));
    let lo = palette[0], hi = palette[palette.length - 1];
    for (let i = 0; i < palette.length - 1; i++) {
      if (norm >= palette[i].t && norm <= palette[i + 1].t) {
        lo = palette[i]; hi = palette[i + 1]; break;
      }
    }
    const f = (norm - lo.t) / ((hi.t - lo.t) || 1);
    return [
      Math.round(lerp(lo.r, hi.r, f)),
      Math.round(lerp(lo.g, hi.g, f)),
      Math.round(lerp(lo.b, hi.b, f)),
    ];
  }

  function renderGridLayer(gridData, vMin, vMax, palette, alpha = 127) {
    const { nx, ny, lat_min, lat_max, lon_min, lon_max, values } = gridData;
    const canvas = document.createElement('canvas');
    canvas.width = nx; canvas.height = ny;
    const ctx = canvas.getContext('2d');
    const imgData = ctx.createImageData(nx, ny);
    for (let i = 0; i < ny * nx; i++) {
      const [r, g, b] = valueToColor(values[i], vMin, vMax, palette);
      imgData.data[i * 4]     = r;
      imgData.data[i * 4 + 1] = g;
      imgData.data[i * 4 + 2] = b;
      imgData.data[i * 4 + 3] = alpha;
    }
    ctx.putImageData(imgData, 0, 0);
    const bounds = [[lat_min, lon_min], [lat_max, lon_max]];
    return L.imageOverlay(canvas.toDataURL(), bounds, { opacity: 1.0 });
  }

  function ensureMinSpan(vMin, vMax, minSpan) {
    const span = vMax - vMin;
    if (span >= minSpan) return [vMin, vMax];
    const mid = (vMin + vMax) / 2;
    return [mid - minSpan / 2, mid + minSpan / 2];
  }

  function renderTemperature(latest, time) {
    const tg = time === 'forecast' ? latest.temp_grid_forecast : latest.temp_grid_observed;
    if (!tg || !tg.values || tg.values.length === 0) return null;
    return renderGridLayer(tg, globalTMin, globalTMax, TEMP_PALETTE, 127);
  }

  function renderHumidity(latest) {
    const hg = latest.humidity_grid;
    if (!hg || !hg.values || hg.values.length === 0) return null;
    return renderGridLayer(hg, HUM_SCALE_MIN, HUM_SCALE_MAX, HUM_PALETTE, 149);
  }

  function renderWindSpeed(latest) {
    const wg = latest.wind_speed_grid;
    if (!wg || !wg.values || wg.values.length === 0) return null;
    return renderGridLayer(wg, wg.ws_min, wg.ws_max, WIND_PALETTE, 149);
  }

  let windUnit = 'kmh'; // 'kmh' | 'kts'
  let currentWsMin = WIND_SPEED_MIN;
  let currentWsMax = WIND_SPEED_MAX;

  function formatWind(kmh) {
    if (windUnit === 'kts') return (kmh * 0.539957).toFixed(1) + ' kts';
    return kmh.toFixed(1) + ' km/h';
  }

  // Stato layer attivo
  let activeLayer = 'temperature';
  let activeTime = 'observed';  // 'observed' | 'forecast'

  // Range temperatura unificato tra le due griglie (Adesso e +1h)
  let globalTMin = 0;
  let globalTMax = 40;
  let heatOverlay = null;

  const MICROCLIMA_COLORS = {
    isola_calore: '#e74c3c',
    brezza_marina: '#3498db',
    collinare:     '#27ae60',
    standard:      '#f39c12',
  };

  function updateStationPopups(markers, stations) {
    stations.forEach((st, i) => {
      const fc = st.forecast;
      const ob = st.observation;
      const tPrev = fc?.temperature    != null ? fc.temperature.toFixed(1)  + '°C' : 'n/d';
      const tOss  = ob?.temperature    != null ? ob.temperature.toFixed(1)  + '°C' : 'n/d';
      const vento = fc?.wind_speed     != null ? formatWind(fc.wind_speed)          : 'n/d';
      const dir   = fc?.wind_direction != null ? degreesToCardinal(fc.wind_direction) : 'n/d';
      const wName = fc?.wind_direction != null ? windName(fc.wind_direction)        : '';
      const hum   = fc?.humidity       != null ? fc.humidity.toFixed(0)     + '%'  : 'n/d';
      const ore   = fc?.valid_for      ? formatTime(fc.valid_for) : '';
      markers[i].setPopupContent(
        `<b>${st.name}</b> <small style="opacity:.7">${st.microclima}</small><br>` +
        `🌡️ Prevista: <b>${tPrev}</b> — Osservata: <b>${tOss}</b><br>` +
        `💨 <b>${vento}</b> da <b>${dir}</b><br>` +
        (wName ? `<small style="opacity:.65;font-style:italic;margin-left:1.4em">${wName}</small><br>` : '') +
        `💧 Umidità: <b>${hum}</b><br>` +
        `<small style="opacity:.6">Valido ore ${ore}</small>`
      );
    });
  }

  function renderStations(map, stations) {
    const markers = stations.map(st => {
      const marker = L.circleMarker([st.lat, st.lon], {
        radius:      8,
        color:       '#fff',
        weight:      2,
        fillColor:   '#9ca3af',
        fillOpacity: 0.9,
      }).addTo(map);
      marker.bindPopup('', { maxWidth: 220, className: 'meteo-popup' });
      return marker;
    });
    updateStationPopups(markers, stations);
    return markers;
  }

  function renderWind(map, windGrid) {
    if (!windGrid || !windGrid[0] || !windGrid[0].data || windGrid[0].data.length === 0) {
      return null;
    }
    return L.velocityLayer({
      displayValues: false,
      displayOptions: {
        velocityType:   'Wind',
        position:       'bottomright',
        emptyString:    'N/D',
        angleConvention: 'bearingCW',
        speedUnit:      'm/s',
      },
      data:          windGrid,
      colorScale:    ['#ffffff'],
      velocityScale: 0.004,
      particleAge:   64,
      lineWidth:     2,
      opacity:       0.9,
    }).addTo(map);
  }

  const _localityCache = {};

  async function getLocalityName(lat, lng) {
    const key = `${lat.toFixed(2)},${lng.toFixed(2)}`;
    if (_localityCache[key]) return _localityCache[key];
    try {
      const res = await fetch(
        `https://nominatim.openstreetmap.org/reverse` +
        `?lat=${lat}&lon=${lng}&format=json&zoom=14&accept-language=it`,
        { headers: { 'User-Agent': 'meteo_locale/1.0' } }
      );
      const data = await res.json();
      const a = data.address || {};
      const city    = a.city || a.town || a.municipality || '';
      const quarter = a.neighbourhood || a.quarter || a.suburb || a.village || '';
      const name = city && quarter && quarter !== city
        ? `${city}, ${quarter}`
        : city || quarter || `${lat.toFixed(3)}°N, ${lng.toFixed(3)}°E`;
      _localityCache[key] = name;
      return name;
    } catch {
      return `${lat.toFixed(3)}°N, ${lng.toFixed(3)}°E`;
    }
  }

  let arrowMarkers = [];
  let stationMarkers = [];

  function clearArrowLayer(map) {
    arrowMarkers.forEach(m => map.removeLayer(m));
    arrowMarkers = [];
  }

  function arrowSpacingDeg(zoom) {
    const spacings = { 7: 1.0, 8: 0.75, 9: 0.45, 10: 0.32, 11: 0.20, 12: 0.12, 13: 0.08 };
    return spacings[Math.min(13, Math.max(7, zoom))] ?? 0.32;
  }

  function windBarbSVG(speedKmh, dirDeg, size) {
    const s = size;
    const halfS = s / 2;

    let remaining = Math.round(speedKmh / 5) * 5;
    const flags   = Math.floor(remaining / 50); remaining -= flags * 50;
    const full    = Math.floor(remaining / 10); remaining -= full * 10;
    const half    = Math.floor(remaining / 5);

    const shaft   = `M ${halfS} ${s * 0.1} L ${halfS} ${s * 0.85}`;
    const tipY    = s * 0.85;
    const tipSize = s * 0.13;
    const arrowTip = `M ${halfS} ${tipY} L ${halfS - tipSize} ${tipY - tipSize * 1.4} L ${halfS + tipSize} ${tipY - tipSize * 1.4} Z`;
    const step    = s * 0.12;
    const barbLen = s * 0.38;
    const halfBL  = s * 0.22;
    let barbs = '';
    let y = s * 0.15;

    for (let i = 0; i < flags; i++) {
      barbs += `M ${halfS} ${y} L ${halfS + barbLen} ${y + step * 0.5} L ${halfS} ${y + step} Z `;
      y += step;
    }
    for (let i = 0; i < full; i++) {
      barbs += `M ${halfS} ${y} L ${halfS + barbLen} ${y - step * 0.3} `;
      y += step;
    }
    if (half) {
      barbs += `M ${halfS} ${y} L ${halfS + halfBL} ${y - step * 0.3} `;
    }

    return `<svg width="${s}" height="${s}" viewBox="0 0 ${s} ${s}"
                style="transform:rotate(${dirDeg}deg);transform-origin:${halfS}px ${halfS}px"
                xmlns="http://www.w3.org/2000/svg">
              <path d="${shaft}" stroke="white" stroke-width="2" fill="none"
                    stroke-linecap="round"/>
              <path d="${arrowTip}" fill="white" stroke="none"/>
              <path d="${barbs}" stroke="white" stroke-width="1.8" fill="white"
                    stroke-linecap="round" stroke-linejoin="round"/>
            </svg>`;
  }

  function bilinearLookup(lat, lon, band) {
    const hdr = band.header;
    if (!hdr) return null;
    const { nx, ny, la1, la2, lo1, lo2 } = hdr;
    const data = band.data;

    const col = (lon - lo1) / (lo2 - lo1) * (nx - 1);
    const row = (la1 - lat) / (la1 - la2) * (ny - 1);
    if (col < 0 || col > nx - 1 || row < 0 || row > ny - 1) return null;

    const c0 = Math.floor(col), c1 = Math.min(c0 + 1, nx - 1);
    const r0 = Math.floor(row), r1 = Math.min(r0 + 1, ny - 1);
    const dc = col - c0, dr = row - r0;

    return data[r0*nx+c0]*(1-dr)*(1-dc) +
           data[r0*nx+c1]*(1-dr)*dc +
           data[r1*nx+c0]*dr*(1-dc) +
           data[r1*nx+c1]*dr*dc;
  }

  function renderArrowLayer(map, windGrid) {
    clearArrowLayer(map);
    if (!windGrid || !windGrid[0] || !windGrid[0].data.length) return;

    const uData = windGrid[0];
    const vData = windGrid[1];
    const { nx, ny, la1, la2, lo1, lo2 } = uData.header;

    const zoom    = map.getZoom();
    const spacing = arrowSpacingDeg(zoom);
    const bounds  = map.getBounds();

    const latMin = Math.max(la2, bounds.getSouth());
    const latMax = Math.min(la1, bounds.getNorth());
    const lonMin = Math.max(lo1, bounds.getWest());
    const lonMax = Math.min(lo2, bounds.getEast());

    for (let lat = latMin; lat <= latMax; lat += spacing) {
      for (let lon = lonMin; lon <= lonMax; lon += spacing) {
        const u = bilinearLookup(lat, lon, uData);
        const v = bilinearLookup(lat, lon, vData);
        if (u === null || v === null) continue;

        const speedMs  = Math.sqrt(u*u + v*v);
        const speedKmh = speedMs * 3.6;
        let dir = Math.atan2(-u, -v) * 180 / Math.PI;
        if (dir < 0) dir += 360;

        const size = zoom >= 11 ? 48 : zoom >= 9 ? 36 : 26;

        const icon = L.divIcon({
          className: '',
          html:       windBarbSVG(speedKmh, dir, size),
          iconSize:   [size, size],
          iconAnchor: [size/2, size/2],
        });

        const m = L.marker([lat, lon], { icon, interactive: false });
        m.addTo(map);
        arrowMarkers.push(m);
      }
    }
  }

  function hideStations(map, markers) {
    markers.forEach(m => map.removeLayer(m));
  }
  function showStations(map, markers) {
    markers.forEach(m => m.addTo(map));
  }

  // Round 6 — causa isolata: Leaflet non ha un ResizeObserver interno sul suo container,
  // ascolta SOLO l'evento nativo 'resize' di window (leaflet-src.js, _updateMapPanes:
  // `onOff(window, 'resize', this._onResize, this)`), poi lo rimbalza con rAF su
  // invalidateSize(). Round 3 aveva già confermato che window.visualViewport non spara mai
  // un evento 'resize' al boot in standalone iOS; per lo stesso motivo neanche il 'resize'
  // nativo di window scatta in quella finestra — non è un resize "vero" agli occhi del
  // sistema, è il compositor WebKit che si assesta da solo sul primissimo paint. Risultato:
  // L.map() misura this._container.clientHeight una volta sola, alla costruzione, e se in
  // quel preciso istante il valore non è ancora quello vero, Leaflet resta convinto di
  // avere meno spazio per sempre — nessun evento arriverà mai a smentirlo. Le tile/l'overlay
  // heatmap vengono quindi disegnati solo fino a quell'altezza sbagliata: sotto resta il
  // background di #map (#0e1119, quasi nero) non dipinto — la barra nera segnalata in foto.
  // Il vecchio criterio "fermati quando innerHeight smette di cambiare" (Round 5) è cieco a
  // questo, perché window.innerHeight risulta già stabile e corretto dal boot (Round 4,
  // ipotesi chiusa) — il valore sbagliato è tutto interno alla cache di Leaflet
  // (map.getSize()/this._size), mai controllato finora. Fix: invalidare su un calendario
  // fisso che copre l'intera finestra dello splash screen (min. 1700ms + 340ms fade, vedi
  // index.html — l'utente vede la mappa solo alla rimozione dello splash, molto dopo che il
  // vecchio ciclo breve si era già fermato), così anche un assestamento tardivo del
  // compositor viene comunque intercettato prima che l'utente guardi lo schermo.
  const STABILIZE_CHECKPOINTS_MS = [0, 100, 250, 500, 900, 1400, 2000, 2600, 3200];
  function stabilizeMapSize(map) {
    STABILIZE_CHECKPOINTS_MS.forEach(delay => {
      setTimeout(() => map.invalidateSize(), delay);
    });
  }

  async function init() {
    const map = L.map('map', { center: [41.85, 12.72], zoom: 8 });

    // ⚠️ DIAG TEMPORANEA (bug barra nera) — RIMUOVERE dopo verifica su device.
    // Atteso post-fix: "map rect height" == "screen height" (non più -47px).
    console.log('[diag-inset] map rect height:', document.getElementById('map').getBoundingClientRect().height,
      '| screen height:', window.screen.height / window.devicePixelRatio,
      '| innerHeight:', window.innerHeight);

    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
      subdomains: 'abcd',
      maxZoom: 19,
    }).addTo(map);

    let windLayer = null;

    // Redesign "Metek" (splash, brand lockup, rail, popover) attivo SOLO in PWA standalone —
    // vedi script di detection in <head> di index.html. Dichiarato qui (non dentro il try)
    // così resta leggibile anche dal catch, per lo splash e i messaggi d'errore.
    const isPWA = document.documentElement.classList.contains('is-pwa');

    // Solo PWA: trucco storico per lo stesso bug WebKit di miscalcolo del viewport — un
    // micro-scroll subito al boot imita l'evento di resize reale che la rotazione genera
    // (e che risolve il problema), senza chiedere all'utente di ruotare fisicamente lo
    // schermo. Va prima di qualunque lettura/uso delle dimensioni (zoomControl, stabilizeMapSize)
    // così, se funziona, quelle letture partono già dal viewport corretto.
    if (isPWA) {
      document.documentElement.style.overflow = 'auto';
      window.scrollTo(0, 1);
      requestAnimationFrame(() => {
        window.scrollTo(0, 0);
        document.documentElement.style.overflow = 'hidden'; // ripristina lo stato originale
      });
    }

    // Solo PWA: il controllo +/- nativo di Leaflet si sovrappone al brand lockup e non fa
    // parte del design. Pinch-to-zoom/scroll-wheel restano attivi: sono handler della mappa
    // indipendenti dal widget visivo, non toccati da questa rimozione. Legacy invariato.
    if (isPWA) map.zoomControl.remove();

    // Solo PWA: Leaflet misura #map una sola volta, a L.map(...), e congela il risultato in
    // this._size. Su iOS standalone il viewport reale può assestarsi dopo il primo paint —
    // se la misura iniziale cade prima di quel momento, la mappa resta convinta di avere
    // meno spazio di quanto ne abbia davvero, e tutto ciò che è ancorato al bottom del
    // documento (rail/legenda/timeline) finisce scorrelato dall'altezza vera dello schermo.
    if (isPWA) {
      // Round 3: confermato su device che window.visualViewport non emette mai un evento
      // 'resize' per l'assestamento iniziale (non è un resize agli occhi di quell'API), e un
      // doppio rAF non basta perché l'assestamento reale non ha una durata fissa in frame.
      // Calendario fisso di invalidateSize() su tutta la finestra dello splash (Round 6,
      // vedi commento sopra stabilizeMapSize) — copre il caso in cui l'assestamento del
      // compositor arrivi più tardi di quanto ci si aspetterebbe.
      stabilizeMapSize(map);

      // Fix strutturale, non solo una tantum: visualViewport è l'API pensata apposta per
      // "il viewport visibile è cambiato dopo il load" — copre le rotazioni schermo future
      // (confermato funzionante su device). Fallback su resize se non disponibile.
      if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', () => map.invalidateSize());
      } else {
        window.addEventListener('resize', () => map.invalidateSize());
      }

      // Round 6 — fix primario: Leaflet non osserva la vera box CSS di #map, solo l'evento
      // 'resize' di window (mai sparato al boot, vedi sopra). Un ResizeObserver diretto su
      // #map non dipende da nessun evento e da nessun calendario indovinato: scatta esattamente
      // nell'istante in cui il compositor WebKit cambia davvero le dimensioni del container,
      // qualunque esso sia — copre in un colpo solo sia il boot lento sia qualunque altro
      // ridimensionamento che sfugga a window/visualViewport. Supportato su iOS Safari da
      // versione 13.4 (2020), quindi sempre disponibile sui device target di questa PWA.
      if ('ResizeObserver' in window) {
        const mapEl = document.getElementById('map');
        let lastW = -1, lastH = -1;
        const mapResizeObserver = new ResizeObserver(entries => {
          for (const entry of entries) {
            const w = Math.round(entry.contentRect.width);
            const h = Math.round(entry.contentRect.height);
            if (w !== lastW || h !== lastH) {
              lastW = w; lastH = h;
              map.invalidateSize();
            }
          }
        });
        mapResizeObserver.observe(mapEl);
      }

      // Riapertura da background (app switcher) è, per questo bug, uno scenario analogo al
      // cold boot: il WebView può ripresentare un compositor non ancora risincronizzato con
      // il container. Stesso trattamento del boot, senza bisogno che l'utente ruoti lo schermo.
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') stabilizeMapSize(map);
      });
      window.addEventListener('pageshow', () => stabilizeMapSize(map));
    }

    try {
      const [latestRes, windRes] = await Promise.all([
        fetch('data/latest.json'),
        fetch('data/wind_grid.json'),
      ]);

      if (!latestRes.ok) throw new Error('latest.json non trovato');
      const latest   = await latestRes.json();
      const windGrid = windRes.ok ? await windRes.json() : null;

      // Range unificato T / T+1 — scala fissa per rendere confrontabile il toggle
      const tgObs = latest.temp_grid_observed;
      const tgFc  = latest.temp_grid_forecast;
      globalTMin = Math.min(tgObs?.t_min ?? Infinity,  tgFc?.t_min ?? Infinity);
      globalTMax = Math.max(tgObs?.t_max ?? -Infinity, tgFc?.t_max ?? -Infinity);

      // Il ramo `else` sotto è il pannello legacy, invariato, per qualunque visita da browser
      // normale; il ramo `if (isPWA)` costruisce brand lockup/rail/popover.
      const firstFc = (latest.stations || []).find(s => s.forecast?.valid_for);
      const validOre = firstFc ? formatTime(firstFc.forecast.valid_for) : '';

      // Righe di preferenze (Mostra vento / Frecce direzionali / unità) — markup condiviso
      // byte-per-byte, riusato sia nel pannello legacy sia nel popover PWA: stessi id, quindi
      // switchLayer()/i listener di wind-check/arrow-check/unità restano invariati sotto.
      const prefsRowsHtml =
        `<label class="ctrl-row" id="wind-toggle">` +
        `<input type="checkbox" id="wind-check" checked>` +
        `<span class="switch"></span>` +
        `<span>Mostra vento</span>` +
        `</label>` +
        `<label class="ctrl-row" id="arrow-toggle" style="display:none">` +
        `<input type="checkbox" id="arrow-check">` +
        `<span class="switch"></span>` +
        `<span>Frecce direzionali</span>` +
        `</label>` +
        `<div class="pill-group" id="wind-unit-group">` +
        `<label class="pill"><input type="radio" name="wind-unit" value="kmh" checked><span>km/h</span></label>` +
        `<label class="pill"><input type="radio" name="wind-unit" value="kts"><span>nodi</span></label>` +
        `</div>`;

      if (isPWA) {
        const railIconSvg = inner =>
          `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round">${inner}</svg>`;
        const ICON_WIND = railIconSvg('<path d="M3 8h11a3 3 0 100-6"></path><path d="M3 12h15a3.5 3.5 0 110 7"></path><path d="M3 16h7a2.5 2.5 0 110 5"></path>');
        const ICON_TEMP = railIconSvg('<path d="M14 14.8V5a2 2 0 10-4 0v9.8a4 4 0 104 0z"></path>');
        const ICON_DROP = railIconSvg('<path d="M12 3s6 6.4 6 10.4A6 6 0 016 13.4C6 9.4 12 3 12 3z"></path>');
        const ICON_RADAR = railIconSvg('<path d="M5 14a4 4 0 011.4-7.7A5.5 5.5 0 0117 7.6 3.6 3.6 0 0119 14z"></path><path d="M8 18l-1 2.5M12 18l-1 2.5M16 18l-1 2.5"></path>');
        const ICON_SLIDERS = railIconSvg('<path d="M4 7h9M17 7h3M4 17h3M11 17h9"></path><circle cx="15" cy="7" r="2.3"></circle><circle cx="9" cy="17" r="2.3"></circle>');

        // ─── Brand lockup (top-left) — riusa #info-mos/#info-radar/#valid-for-label così
        // switchLayer()/switchTime()/RadarLayer.onUpdate restano invariati ───
        const brand = L.DomUtil.create('div');
        brand.id = 'brand-lockup';
        brand.innerHTML =
          `<svg class="brand-mark" viewBox="0 0 180 180" xmlns="http://www.w3.org/2000/svg">` +
          `<g fill="none" stroke-linecap="round">` +
          `<path d="M-4 46c34 0 46 22 84 22s52-22 104-22" stroke="#4b5192" stroke-width="10"></path>` +
          `<path d="M-4 82c34 0 46 22 84 22s52-22 104-22" stroke="#9184d9" stroke-width="11"></path>` +
          `<path d="M-4 118c34 0 46 22 84 22s52-22 104-22" stroke="#c6bff0" stroke-width="10"></path>` +
          `</g><circle cx="126" cy="96" r="9" fill="#f4a93f"></circle>` +
          `</svg>` +
          `<div class="brand-text">` +
          `<div class="brand-word">Metek</div>` +
          `<div id="info-mos">` +
          `<div id="brand-meta">${formatTime(latest.generated_at)}` +
          `<span id="valid-for-label">${validOre ? ` · previsioni ${validOre}` : ''}</span>` +
          `</div>` +
          `</div>` +
          `<div id="info-radar" style="display:none"></div>` +
          `</div>`;
        document.body.appendChild(brand);

        // ─── Pillola tempo (top-right) — id "time-toggle" riusato dal pannello legacy ───
        const timeToggle = L.DomUtil.create('div');
        timeToggle.id = 'time-toggle';
        timeToggle.innerHTML =
          `<button id="btn-now" class="active">Adesso</button>` +
          `<button id="btn-plus1">+1h</button>`;
        document.body.appendChild(timeToggle);

        // ─── Rail layer (bottom-center) ───
        const rail = L.DomUtil.create('div');
        rail.id = 'layer-rail';
        rail.innerHTML =
          `<button class="rail-item" id="btn-wind">${ICON_WIND}<span>Vento</span></button>` +
          `<button class="rail-item active" id="btn-temp">${ICON_TEMP}<span>Temp.</span></button>` +
          `<button class="rail-item" id="btn-hum">${ICON_DROP}<span>Umidità</span></button>` +
          `<button class="rail-item" id="btn-radar">${ICON_RADAR}<span>Radar</span></button>` +
          `<div id="rail-divider"></div>` +
          `<button id="prefs-btn" title="Regolazioni">${ICON_SLIDERS}</button>`;
        document.body.appendChild(rail);

        // ─── Popover preferenze — chiuso di default ───
        const popover = L.DomUtil.create('div');
        popover.id = 'prefs-popover';
        popover.classList.add('hidden');
        popover.innerHTML =
          prefsRowsHtml +
          `<div class="panel-divider"></div>` +
          `<a class="dashboard-link" href="dashboard.html">` +
          `<span>Dashboard</span><span class="chevron">›</span>` +
          `</a>`;
        document.body.appendChild(popover);
      } else {
        // ─── Pannello di controllo unico — top-left (comportamento legacy, invariato) ───
        const infoPanel = L.DomUtil.create('div');
        infoPanel.id = 'control-panel';
        infoPanel.innerHTML =
          `<div class="panel-head">` +
          `<div class="info-title">🌦️ Meteo Locale — Roma</div>` +
          `<div id="info-mos">` +
          `<div class="info-update">Aggiornato: ${formatTime(latest.generated_at)}</div>` +
          `<div class="info-update" id="valid-for-label">${validOre ? `Previsioni per le ore ${validOre}` : ''}</div>` +
          `</div>` +
          `<div class="info-update" id="info-radar" style="display:none"></div>` +
          `</div>` +
          `<div class="layer-toggle">` +
          `<button id="btn-wind">💨 Vento</button>` +
          `<button id="btn-temp" class="active">🌡️ Temperatura</button>` +
          `<button id="btn-hum">💧 Umidità</button>` +
          `<button id="btn-radar">🌧️ Radar</button>` +
          `</div>` +
          `<div class="layer-toggle" id="time-toggle">` +
          `<button id="btn-now" class="active">Adesso</button>` +
          `<button id="btn-plus1">+1h</button>` +
          `</div>` +
          prefsRowsHtml +
          `<div class="panel-divider"></div>` +
          `<a class="dashboard-link" href="dashboard.html">` +
          `<span>📊 Dashboard</span><span class="chevron">›</span>` +
          `</a>`;
        document.body.appendChild(infoPanel);
      }

      // Legenda — bottom-right (aggiornata da updateLegend)
      const legend = L.DomUtil.create('div', 'temp-legend');
      legend.innerHTML =
        `<div id="legend-title" class="legend-title"></div>` +
        `<div id="legend-bar" class="legend-bar"></div>` +
        `<div id="legend-labels" class="legend-labels"></div>`;
      document.getElementById('map').appendChild(legend);

      function updateLegend(layer, vMin, vMax, unit, customTicks) {
        const unitLabel = unit.trim();
        const titles = {
          temperature: `Temperatura (${unitLabel})`,
          humidity:    `Umidità (${unitLabel})`,
          wind:        `Velocità vento (${unitLabel})`,
          radar:       `Intensità precipitazione (${unitLabel})`,
        };
        const gradients = {
          temperature: 'linear-gradient(to right, #2c3e95 0%, #3a6fc4 12.5%, #4fb8c4 25%, #6fc46a 37.5%, #d4d24a 50%, #f4a93f 62.5%, #e8542f 75%, #a50026 87.5%, #67001f 100%)',
          humidity:    'linear-gradient(to right, #d96f27, #fee080, #b0e090, #317ec8, #08306b)',
          wind:        'linear-gradient(to right, #003399, #0099ff, #00cc66, #ffdd00, #ff6600, #cc0000)',
          radar:       'linear-gradient(to right, #6ec6e0 0%, #4caf50 25%, #ffd54f 50%, #ff7043 75%, #b71c1c 100%)',
        };
        document.getElementById('legend-title').textContent = titles[layer] ?? layer;
        document.getElementById('legend-bar').style.background = gradients[layer] ?? '';
        const labelsEl = document.getElementById('legend-labels');
        labelsEl.innerHTML = '';
        if (customTicks) {
          customTicks.forEach((text, i) => {
            const pos = customTicks.length > 1 ? (i / (customTicks.length - 1)) * 100 : 0;
            const span = document.createElement('span');
            span.className = 'legend-tick';
            span.textContent = text;
            span.style.left = pos + '%';
            labelsEl.appendChild(span);
          });
          return;
        }
        // PWA: solo min/max, unità accodata solo al max. Browser normale: invariato (3 tick,
        // unità su tutti) — stesso identico comportamento di oggi.
        const ticks = isPWA ? [vMin, vMax] : [vMin, (vMin + vMax) / 2, vMax];
        ticks.forEach((v, i) => {
          const pos = ((v - vMin) / (vMax - vMin)) * 100;
          const span = document.createElement('span');
          span.className = 'legend-tick';
          const decimals = layer === 'temperature' ? 1 : 0;
          const showUnit = !isPWA || i === ticks.length - 1;
          span.textContent = v.toFixed(decimals) + (showUnit ? unit : '');
          span.style.left = pos + '%';
          labelsEl.appendChild(span);
        });
      }

      // Stato popover preferenze — solo PWA, il pannello legacy non ha un concetto di
      // apertura/chiusura (le righe sono sempre visibili inline come oggi).
      let prefsOpen = false;
      function renderPrefsVisibility() {
        if (!isPWA) return;
        document.getElementById('prefs-popover')?.classList.toggle('hidden', !prefsOpen);
        document.getElementById('prefs-btn')?.classList.toggle('active', prefsOpen);
      }

      // Solo PWA: sul layer Radar la timeline occupa spazio variabile in fondo (testo che
      // va a capo, stato "non disponibile" più basso della timeline normale, ecc.) — invece
      // di un bottom fisso indovinato via CSS, misuriamo l'altezza reale a runtime e
      // spostiamo la legenda sopra di essa + un margine. Richiamata da switchLayer(),
      // dall'update di RadarLayer (quando i frame arrivano/falliscono) e al resize.
      const RADAR_TIMELINE_BOTTOM = 112; // deve combaciare con .is-pwa .radar-timeline { bottom }
      const RADAR_LEGEND_GAP = 8;
      function updateRadarLegendOffset() {
        if (!isPWA) return;
        const legendEl = document.querySelector('.temp-legend');
        if (!legendEl) return;
        if (activeLayer !== 'radar') { legendEl.style.bottom = ''; return; }
        const timelineEl = document.getElementById('radar-timeline');
        const visible = timelineEl && getComputedStyle(timelineEl).display !== 'none';
        if (!visible) {
          // Al primo switch su Radar la timeline non esiste ancora (creata lazy dentro
          // RadarLayer.activate(), asincrono) o non è ancora visibile: misurarla ora darebbe
          // 0 e posizionerebbe la legenda troppo in basso, dentro lo spazio che la timeline
          // occuperà a breve — meglio lasciare il fallback CSS (.legend-radar-mode, 220px,
          // già abbondante) finché non c'è una misura vera da usare.
          legendEl.style.bottom = '';
          return;
        }
        const timelineH = timelineEl.getBoundingClientRect().height;
        legendEl.style.bottom = `${RADAR_TIMELINE_BOTTOM + timelineH + RADAR_LEGEND_GAP}px`;
      }

      function switchLayer(layer) {
        activeLayer = layer;
        if (heatOverlay) map.removeLayer(heatOverlay);
        heatOverlay = null;

        document.getElementById('btn-wind').classList.toggle('active', layer === 'wind');
        document.getElementById('btn-temp').classList.toggle('active', layer === 'temperature');
        document.getElementById('btn-hum').classList.toggle('active',  layer === 'humidity');
        document.getElementById('btn-radar').classList.toggle('active', layer === 'radar');
        document.getElementById('time-toggle').style.display = layer === 'temperature' ? 'flex' : 'none';

        const windToggle    = document.getElementById('wind-toggle');
        const arrowToggle   = document.getElementById('arrow-toggle');
        const windCheck     = document.getElementById('wind-check');
        const arrowCheck    = document.getElementById('arrow-check');
        const windUnitGroup = document.getElementById('wind-unit-group');
        const infoMos       = document.getElementById('info-mos');
        const infoRadar     = document.getElementById('info-radar');

        if (windUnitGroup) windUnitGroup.style.display = '';
        if (infoMos)   infoMos.style.display   = layer === 'radar' ? 'none' : '';
        if (infoRadar) infoRadar.style.display = layer === 'radar' ? '' : 'none';
        document.querySelector('.temp-legend')?.classList.toggle('legend-radar-mode', layer === 'radar');
        updateRadarLegendOffset();

        if (layer !== 'radar' && window.RadarLayer && window.RadarLayer.isActive()) {
          window.RadarLayer.deactivate();
        }

        if (isPWA) {
          // Radar: nessuna impostazione applicabile — tasto regolazioni e divider spariscono,
          // popover si chiude se era aperto (richiesta esplicita del design). Su qualunque
          // altro layer restano visibili.
          const prefsBtnEl = document.getElementById('prefs-btn');
          const railDividerEl = document.getElementById('rail-divider');
          if (prefsBtnEl)    prefsBtnEl.style.display    = layer === 'radar' ? 'none' : '';
          if (railDividerEl) railDividerEl.style.display = layer === 'radar' ? 'none' : '';
          if (layer === 'radar' && prefsOpen) {
            prefsOpen = false;
            renderPrefsVisibility();
          }
        }

        if (layer === 'radar') {
          if (windToggle)    windToggle.style.display    = 'none';
          if (arrowToggle)   arrowToggle.style.display   = 'none';
          if (windUnitGroup) windUnitGroup.style.display = 'none';
          clearArrowLayer(map);
          showStations(map, stationMarkers);
          if (windLayer) map.removeLayer(windLayer);
          updateLegend('radar', null, null, 'mm/h', ['debole', 'moderata', 'intensa']);
          if (window.RadarLayer) window.RadarLayer.activate();
        } else if (layer === 'wind') {
          if (windToggle)  windToggle.style.display  = 'none';
          if (arrowToggle) arrowToggle.style.display = '';
          heatOverlay = renderWindSpeed(latest);
          const wg    = latest.wind_speed_grid;
          currentWsMin = wg ? wg.ws_min : WIND_SPEED_MIN;
          currentWsMax = wg ? wg.ws_max : WIND_SPEED_MAX;
          updateWindLegend();
          // Frecce off di default → reset checkbox e mostra particelle
          if (arrowCheck) arrowCheck.checked = false;
          clearArrowLayer(map);
          if (windLayer) windLayer.addTo(map);
        } else {
          if (windToggle)  windToggle.style.display  = '';
          if (arrowToggle) arrowToggle.style.display = 'none';
          clearArrowLayer(map);
          showStations(map, stationMarkers);
          // Particelle: rispetta stato di #wind-check
          if (windLayer) {
            if (windCheck && windCheck.checked) windLayer.addTo(map);
            else map.removeLayer(windLayer);
          }
          if (layer === 'temperature') {
            heatOverlay = renderTemperature(latest, activeTime);
            updateLegend('temperature', globalTMin, globalTMax, '°C');
          } else {
            heatOverlay = renderHumidity(latest);
            if (latest.humidity_grid)
              updateLegend('humidity', latest.humidity_grid.h_min, latest.humidity_grid.h_max, '%');
          }
        }

        if (heatOverlay) heatOverlay.addTo(map);
      }

      function switchTime(time) {
        activeTime = time;
        document.getElementById('btn-now').classList.toggle('active', time === 'observed');
        document.getElementById('btn-plus1').classList.toggle('active', time === 'forecast');
        const label = document.getElementById('valid-for-label');
        if (label) {
          const firstFc  = (latest.stations || []).find(s => s.forecast?.valid_for);
          const firstFc1 = (latest.stations || []).find(s => s.forecast1?.valid_for);
          const src = time === 'observed' ? firstFc : firstFc1;
          const validOreNow = src ? formatTime(src.forecast?.valid_for || src.forecast1?.valid_for) : '';
          // Stessi valori (src, orario), solo il TESTO differisce: formato breve in PWA
          // (evita la collisione con la pillola tempo, vedi CSS), invariato in legacy.
          label.textContent = !src ? '' : (isPWA ? ` · previsioni ${validOreNow}` : `Previsioni per le ore ${validOreNow}`);
        }
        if (activeLayer === 'temperature') switchLayer('temperature');
      }

      document.getElementById('btn-wind').addEventListener('click', () => switchLayer('wind'));
      document.getElementById('btn-temp').addEventListener('click', () => switchLayer('temperature'));
      document.getElementById('btn-hum').addEventListener('click', () => switchLayer('humidity'));
      document.getElementById('btn-radar').addEventListener('click', () => switchLayer('radar'));
      document.getElementById('btn-now').addEventListener('click', () => switchTime('observed'));
      document.getElementById('btn-plus1').addEventListener('click', () => switchTime('forecast'));

      if (isPWA) {
        document.getElementById('prefs-btn')?.addEventListener('click', () => {
          prefsOpen = !prefsOpen;
          renderPrefsVisibility();
        });
      }

      if (window.RadarLayer) {
        window.RadarLayer.init(map);
        window.RadarLayer.onUpdate((epochSec) => {
          const el = document.getElementById('info-radar');
          if (el) {
            el.textContent = epochSec != null
              ? `🕐 Radar aggiornato: ${formatTime(new Date(epochSec * 1000).toISOString())}`
              : '🕐 Radar non disponibile';
          }
          // A questo punto showUnavailable() ha già applicato lo stato finale della timeline
          // (disponibile o "non disponibile", altezze diverse) — la misura è affidabile.
          updateRadarLegendOffset();
        });
      }

      if (isPWA) {
        window.addEventListener('resize', updateRadarLegendOffset);
      }

      const stations = latest.stations || [];
      stationMarkers = renderStations(map, stations);
      switchLayer('temperature');

      if (stations.length > 0) {
        const bounds = stations.map(st => [st.lat, st.lon]);
        map.fitBounds(bounds, { padding: [50, 50] });
      }

      if (isPWA) window.hideSplash?.();

      if (windGrid) {
        windLayer = renderWind(map, windGrid);
      }

      map.on('zoomend', () => {
        if (activeLayer === 'wind') {
          const arrowCheck = document.getElementById('arrow-check');
          if (arrowCheck && arrowCheck.checked) renderArrowLayer(map, windGrid);
        }
      });

      // IDW al punto cliccato
      map.on('click', async function (e) {
        if (isPWA && prefsOpen) {
          prefsOpen = false;
          renderPrefsVisibility();
        }

        const lat = e.latlng.lat;
        const lng = e.latlng.lng;

        const tgActive = activeTime === 'forecast' ? latest.temp_grid_forecast : latest.temp_grid_observed;
        const temp = tgActive ? lookupGrid(lat, lng, tgActive) : null;

        const windU = idwPoint(lat, lng, stations, st => {
          const s = st.forecast?.wind_speed    ?? null;
          const d = st.forecast?.wind_direction ?? null;
          if (s === null || d === null) return null;
          const rad = d * Math.PI / 180;
          return -(s / 3.6) * Math.sin(rad);
        });
        const windV = idwPoint(lat, lng, stations, st => {
          const s = st.forecast?.wind_speed    ?? null;
          const d = st.forecast?.wind_direction ?? null;
          if (s === null || d === null) return null;
          const rad = d * Math.PI / 180;
          return -(s / 3.6) * Math.cos(rad);
        });

        const speed = Math.sqrt(windU ** 2 + windV ** 2) * 3.6;
        let dir = Math.atan2(-windU, -windV) * 180 / Math.PI;
        if (dir < 0) dir += 360;

        const hum = latest.humidity_grid
          ? lookupGrid(lat, lng, latest.humidity_grid) : null;

        const cardinal = degreesToCardinal(dir);
        const wName    = windName(dir);

        function buildContent(localita) {
          return (
            `<b>${localita}</b><br>` +
            `🌡️ <b>${temp !== null ? temp.toFixed(1) + '°C' : 'n/d'}</b><br>` +
            `💨 <b>${formatWind(speed)}</b> — ${cardinal}<br>` +
            `<small style="opacity:.65;font-style:italic;margin-left:1.4em">${wName}</small><br>` +
            `💧 Umidità: <b>${hum !== null ? hum.toFixed(0) + '%' : 'n/d'}</b>`
          );
        }

        // Apri subito il popup con placeholder, poi aggiorna con la località
        let popupClosed = false;
        const popup = L.popup({ className: 'meteo-popup' })
          .setLatLng(e.latlng)
          .setContent(buildContent('📍 ...'))
          .openOn(map);
        popup.on('remove', () => { popupClosed = true; });

        const localita = await getLocalityName(lat, lng);
        if (!popupClosed) popup.setContent(buildContent(localita));
      });

      function updateWindLegend() {
        if (activeLayer !== 'wind') return;
        const isKts = document.querySelector('input[name="wind-unit"][value="kts"]')?.checked;
        const factor = isKts ? 0.539957 : 1;
        const unit = isKts ? ' kts' : ' km/h';
        updateLegend('wind', currentWsMin * factor, currentWsMax * factor, unit);
      }

      document.querySelectorAll('input[name="wind-unit"]').forEach(radio => {
        radio.addEventListener('change', e => {
          windUnit = e.target.value;
          updateStationPopups(stationMarkers, stations);
          updateWindLegend();
        });
      });

      document.getElementById('wind-check').addEventListener('change', e => {
        if (!windLayer) return;
        if (e.target.checked) windLayer.addTo(map);
        else map.removeLayer(windLayer);
      });

      document.getElementById('arrow-check').addEventListener('change', e => {
        if (activeLayer !== 'wind') return;
        if (e.target.checked) {
          if (windLayer) map.removeLayer(windLayer);
          hideStations(map, stationMarkers);
          renderArrowLayer(map, windGrid);
        } else {
          clearArrowLayer(map);
          showStations(map, stationMarkers);
          const windCheck = document.getElementById('wind-check');
          if (windLayer && windCheck && windCheck.checked) windLayer.addTo(map);
        }
      });

    } catch (err) {
      console.error('Errore caricamento dati:', err);
      if (isPWA) {
        // Bug preesistente (#updated-at non esiste nel markup, citato nel README): fixato
        // solo qui, nel ramo PWA. Il ramo legacy sotto resta byte-per-byte quello di oggi.
        const brandMeta = document.getElementById('brand-meta');
        if (brandMeta) brandMeta.textContent = 'Dati non disponibili';
        window.hideSplash?.();
      } else {
        document.getElementById('updated-at').textContent = 'Errore caricamento dati';
      }
    }

    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('./sw.js').catch(() => {});
    }
  }

  function lookupGrid(lat, lng, grid) {
    const { lat_min, lat_max, lon_min, lon_max, nx, ny, values } = grid;
    const clampLat = Math.max(lat_min, Math.min(lat_max, lat));
    const clampLng = Math.max(lon_min, Math.min(lon_max, lng));
    const row = (lat_max - clampLat) / (lat_max - lat_min) * (ny - 1);
    const col = (clampLng - lon_min) / (lon_max - lon_min) * (nx - 1);
    const r0 = Math.floor(row), r1 = Math.min(r0 + 1, ny - 1);
    const c0 = Math.floor(col), c1 = Math.min(c0 + 1, nx - 1);
    const dr = row - r0, dc = col - c0;
    return values[r0*nx+c0] * (1-dr)*(1-dc) +
           values[r0*nx+c1] * (1-dr)*dc +
           values[r1*nx+c0] * dr*(1-dc) +
           values[r1*nx+c1] * dr*dc;
  }

  function idwPoint(lat, lng, stations, getValue, power = 2) {
    let num = 0, den = 0;
    for (const st of stations) {
      const v = getValue(st);
      if (v === null || v === undefined || isNaN(v)) continue;
      const d = Math.sqrt((lat - st.lat) ** 2 + (lng - st.lon) ** 2);
      if (d < 1e-5) return v;
      const w = 1 / d ** power;
      num += w * v;
      den += w;
    }
    return den > 0 ? num / den : null;
  }

  window.addEventListener('DOMContentLoaded', init);
})();
