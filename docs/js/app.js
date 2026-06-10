(function () {
  'use strict';

  function degreesToCardinal(deg) {
    const labels = ['N','NNE','NE','ENE','E','ESE','SE','SSE',
                    'S','SSO','SO','OSO','O','ONO','NO','NNO'];
    return labels[Math.round(deg / 22.5) % 16];
  }

  function formatTime(isoStr) {
    return new Date(isoStr).toLocaleTimeString('it-IT', {
      timeZone: 'Europe/Rome', hour: '2-digit', minute: '2-digit',
    });
  }

  // 5-stop diverging palette: cold → hot
  const PALETTE = [
    { t: 0.00, r: 0x31, g: 0x36, b: 0x95 }, // #313695
    { t: 0.25, r: 0x74, g: 0xad, b: 0xd1 }, // #74add1
    { t: 0.50, r: 0xfe, g: 0xe0, b: 0x90 }, // #fee090
    { t: 0.75, r: 0xf4, g: 0x6d, b: 0x43 }, // #f46d43
    { t: 1.00, r: 0xa5, g: 0x00, b: 0x26 }, // #a50026
  ];

  function lerp(a, b, f) { return a + (b - a) * f; }

  function tempToColor(value, tMin, tMax) {
    const norm = Math.max(0, Math.min(1, (value - tMin) / (tMax - tMin || 1)));
    let lo = PALETTE[0], hi = PALETTE[PALETTE.length - 1];
    for (let i = 0; i < PALETTE.length - 1; i++) {
      if (norm >= PALETTE[i].t && norm <= PALETTE[i + 1].t) {
        lo = PALETTE[i];
        hi = PALETTE[i + 1];
        break;
      }
    }
    const f = (norm - lo.t) / ((hi.t - lo.t) || 1);
    return [
      Math.round(lerp(lo.r, hi.r, f)),
      Math.round(lerp(lo.g, hi.g, f)),
      Math.round(lerp(lo.b, hi.b, f)),
    ];
  }

  function renderTemperature(map, latest) {
    const tg = latest.temp_grid;
    if (!tg || !tg.values || tg.values.length === 0) return null;

    const { nx, ny, lat_min, lat_max, lon_min, lon_max, t_min, t_max, values } = tg;
    const canvas = document.createElement('canvas');
    canvas.width = nx;
    canvas.height = ny;
    const ctx = canvas.getContext('2d');
    const imgData = ctx.createImageData(nx, ny);

    for (let i = 0; i < ny * nx; i++) {
      const [r, g, b] = tempToColor(values[i], t_min, t_max);
      imgData.data[i * 4]     = r;
      imgData.data[i * 4 + 1] = g;
      imgData.data[i * 4 + 2] = b;
      imgData.data[i * 4 + 3] = 153; // 0.6 * 255
    }
    ctx.putImageData(imgData, 0, 0);

    const bounds = [[lat_min, lon_min], [lat_max, lon_max]];
    return L.imageOverlay(canvas.toDataURL(), bounds, { opacity: 1.0 }).addTo(map);
  }

  const MICROCLIMA_COLORS = {
    isola_calore: '#e74c3c',
    brezza_marina: '#3498db',
    collinare:     '#27ae60',
    standard:      '#f39c12',
  };

  function renderStations(map, stations) {
    stations.forEach(st => {
      const fc = st.forecast;
      const ob = st.observation;

      const marker = L.circleMarker([st.lat, st.lon], {
        radius:      8,
        color:       '#fff',
        weight:      2,
        fillColor:   '#9ca3af',
        fillOpacity: 0.9,
      }).addTo(map);

      const tPrev = fc?.temperature    != null ? fc.temperature.toFixed(1)  + '°C' : 'n/d';
      const tOss  = ob?.temperature    != null ? ob.temperature.toFixed(1)  + '°C' : 'n/d';
      const vento = fc?.wind_speed     != null ? fc.wind_speed.toFixed(1)   + ' km/h' : 'n/d';
      const dir   = fc?.wind_direction != null ? degreesToCardinal(fc.wind_direction) : 'n/d';
      const hum   = fc?.humidity       != null ? fc.humidity.toFixed(0)     + '%' : 'n/d';
      const ore   = fc?.valid_for      ? formatTime(fc.valid_for) : '';

      marker.bindPopup(
        `<b>${st.name}</b> <small style="opacity:.7">${st.microclima}</small><br>` +
        `🌡️ Prevista: <b>${tPrev}</b> — Osservata: <b>${tOss}</b><br>` +
        `💨 <b>${vento}</b> da <b>${dir}</b><br>` +
        `💧 Umidità: <b>${hum}</b><br>` +
        `<small style="opacity:.6">Valido ore ${ore}</small>`,
        { maxWidth: 220 }
      );
    });
  }

  function renderWind(map, windGrid) {
    if (!windGrid || !windGrid[0] || !windGrid[0].data || windGrid[0].data.length === 0) {
      return null;
    }
    return L.velocityLayer({
      displayValues: true,
      displayOptions: {
        velocityType:   'Wind',
        position:       'bottomright',
        emptyString:    'N/D',
        angleConvention: 'bearingCW',
        speedUnit:      'm/s',
      },
      data:          windGrid,
      colorScale:    ['#ffffff'],
      velocityScale: 0.005,
      particleAge:   64,
      lineWidth:     2,
      opacity:       0.9,
    }).addTo(map);
  }

  async function init() {
    const map = L.map('map', { center: [41.825, 12.525], zoom: 10 });

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

      // Pannello info — top-left
      const infoPanel = L.DomUtil.create('div', 'info-panel');
      infoPanel.innerHTML =
        `<span class="info-title">🌦️ Meteo Locale — Roma</span><br>` +
        `<span class="info-update">Aggiornato: ${formatTime(latest.generated_at)}</span>`;
      document.getElementById('map').appendChild(infoPanel);

      // Legenda temperatura — bottom-right
      if (latest.temp_grid) {
        const tMin = latest.temp_grid.t_min.toFixed(1);
        const tMax = latest.temp_grid.t_max.toFixed(1);
        const legend = L.DomUtil.create('div', 'temp-legend');
        legend.innerHTML =
          `<div class="legend-title">Temperatura (°C)</div>` +
          `<div class="legend-bar"></div>` +
          `<div class="legend-labels"><span>${tMin}°</span><span>${tMax}°</span></div>`;
        document.getElementById('map').appendChild(legend);
      }

      renderTemperature(map, latest);
      const stations = latest.stations || [];
      renderStations(map, stations);

      if (windGrid) {
        windLayer = renderWind(map, windGrid);
      }

      // IDW al punto cliccato
      map.on('click', function(e) {
        const lat = e.latlng.lat;
        const lng = e.latlng.lng;

        const temp = idwPoint(lat, lng, stations, st => st.forecast?.temperature);

        const windU = idwPoint(lat, lng, stations, st => {
          const s = (st.forecast?.wind_speed ?? null);
          const d = (st.forecast?.wind_direction ?? null);
          if (s === null || d === null) return null;
          const rad = d * Math.PI / 180;
          return -(s / 3.6) * Math.sin(rad);
        });
        const windV = idwPoint(lat, lng, stations, st => {
          const s = (st.forecast?.wind_speed ?? null);
          const d = (st.forecast?.wind_direction ?? null);
          if (s === null || d === null) return null;
          const rad = d * Math.PI / 180;
          return -(s / 3.6) * Math.cos(rad);
        });

        const speed = Math.sqrt(windU ** 2 + windV ** 2) * 3.6;
        let dir = Math.atan2(-windU, -windV) * 180 / Math.PI;
        if (dir < 0) dir += 360;

        L.popup({ className: 'meteo-popup' })
          .setLatLng(e.latlng)
          .setContent(
            `<b>${lat.toFixed(3)}°N, ${lng.toFixed(3)}°E</b><br>` +
            `🌡️ <b>${temp !== null ? temp.toFixed(1) + '°C' : 'n/d'}</b><br>` +
            `💨 <b>${speed.toFixed(1)} km/h</b> — ${degreesToCardinal(dir)}`
          )
          .openOn(map);
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
