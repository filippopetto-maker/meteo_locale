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

  function renderGridLayer(gridData, vMin, vMax, palette, alpha = 153) {
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
    return renderGridLayer(tg, globalTMin, globalTMax, TEMP_PALETTE);
  }

  function renderHumidity(latest) {
    const hg = latest.humidity_grid;
    if (!hg || !hg.values || hg.values.length === 0) return null;
    return renderGridLayer(hg, HUM_SCALE_MIN, HUM_SCALE_MAX, HUM_PALETTE, 179);
  }

  let windUnit = 'kmh'; // 'kmh' | 'kts'

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
      marker.bindPopup('', { maxWidth: 220 });
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

  async function init() {
    const map = L.map('map', { center: [41.85, 12.72], zoom: 8 });

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 18,
    }).addTo(map);

    let windLayer = null;

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

      // Pannello info — top-left con toggle layer
      const infoPanel = L.DomUtil.create('div', 'info-panel');
      const firstFc = (latest.stations || []).find(s => s.forecast?.valid_for);
      const validOre = firstFc ? formatTime(firstFc.forecast.valid_for) : '';
      infoPanel.innerHTML =
        `<span class="info-title">🌦️ Meteo Locale — Roma</span><br>` +
        `<span class="info-update">Aggiornato: ${formatTime(latest.generated_at)}</span>` +
        `<br>` +
        `<span class="info-update" id="valid-for-label">${validOre ? `Previsioni per le ore ${validOre}` : ''}</span>` +
        `<div class="layer-toggle">` +
        `<button id="btn-temp" class="active">🌡️ Temperatura</button>` +
        `<button id="btn-hum">💧 Umidità</button>` +
        `</div>` +
        `<div class="layer-toggle" id="time-toggle">` +
        `<button id="btn-now" class="active">Adesso</button>` +
        `<button id="btn-plus1">+1h</button>` +
        `</div>`;
      document.getElementById('map').appendChild(infoPanel);

      // Legenda — bottom-right (aggiornata da updateLegend)
      const legend = L.DomUtil.create('div', 'temp-legend');
      legend.innerHTML =
        `<div id="legend-title" class="legend-title"></div>` +
        `<div id="legend-bar" class="legend-bar"></div>` +
        `<div id="legend-labels" class="legend-labels"></div>`;
      document.getElementById('map').appendChild(legend);

      function updateLegend(layer, vMin, vMax, unit) {
        document.getElementById('legend-title').textContent =
          layer === 'temperature' ? `Temperatura (${unit})` : `Umidità (${unit})`;
        document.getElementById('legend-bar').style.background =
          layer === 'temperature'
            ? 'linear-gradient(to right, #2c3e95 0%, #3a6fc4 12.5%, #4fb8c4 25%, #6fc46a 37.5%, #d4d24a 50%, #f4a93f 62.5%, #e8542f 75%, #a50026 87.5%, #67001f 100%)'
            : 'linear-gradient(to right, #d96f27, #fee080, #b0e090, #317ec8, #08306b)';
        const labelsEl = document.getElementById('legend-labels');
        labelsEl.innerHTML = '';
        const ticks = [vMin, (vMin + vMax) / 2, vMax];
        ticks.forEach(v => {
          const pos = ((v - vMin) / (vMax - vMin)) * 100;
          const span = document.createElement('span');
          span.className = 'legend-tick';
          span.textContent = Math.round(v) + unit;
          span.style.left = pos + '%';
          labelsEl.appendChild(span);
        });
      }

      function switchLayer(layer) {
        activeLayer = layer;
        if (heatOverlay) map.removeLayer(heatOverlay);
        document.getElementById('time-toggle').style.display = layer === 'temperature' ? 'flex' : 'none';
        if (layer === 'temperature') {
          heatOverlay = renderTemperature(latest, activeTime);
          document.getElementById('btn-temp').classList.add('active');
          document.getElementById('btn-hum').classList.remove('active');
          updateLegend('temperature', globalTMin, globalTMax, '°C');
        } else {
          heatOverlay = renderHumidity(latest);
          document.getElementById('btn-temp').classList.remove('active');
          document.getElementById('btn-hum').classList.add('active');
          if (latest.humidity_grid) {
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
          label.textContent = src ? `Previsioni per le ore ${formatTime(src.forecast?.valid_for || src.forecast1?.valid_for)}` : '';
        }
        if (activeLayer === 'temperature') switchLayer('temperature');
      }

      document.getElementById('btn-temp').addEventListener('click', () => switchLayer('temperature'));
      document.getElementById('btn-hum').addEventListener('click', () => switchLayer('humidity'));
      document.getElementById('btn-now').addEventListener('click', () => switchTime('observed'));
      document.getElementById('btn-plus1').addEventListener('click', () => switchTime('forecast'));

      switchLayer('temperature');
      const stations = latest.stations || [];
      const markers = renderStations(map, stations);

      if (stations.length > 0) {
        const bounds = stations.map(st => [st.lat, st.lon]);
        map.fitBounds(bounds, { padding: [50, 50] });
      }

      if (windGrid) {
        windLayer = renderWind(map, windGrid);
      }

      // IDW al punto cliccato
      map.on('click', async function (e) {
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

      document.querySelectorAll('input[name="wind-unit"]').forEach(radio => {
        radio.addEventListener('change', e => {
          windUnit = e.target.value;
          updateStationPopups(markers, stations);
        });
      });

      document.getElementById('wind-check').addEventListener('change', e => {
        if (!windLayer) return;
        if (e.target.checked) {
          windLayer.addTo(map);
        } else {
          map.removeLayer(windLayer);
        }
      });

    } catch (err) {
      console.error('Errore caricamento dati:', err);
      document.getElementById('updated-at').textContent = 'Errore caricamento dati';
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
