// prestazioni.js — pannello di misura (solo su richiesta: ?perf=1 lo accende e lo ricorda, ?perf=0 lo spegne).
// fps della pagina (requestAnimationFrame), fps della mappa 3D (fotogrammi disegnati da MapLibre), scatti (> 50 ms),
// compiti lunghi del browser, ping al sito (HEAD, fuori dal service worker), rete e dispositivo. "Copia" mette il riepilogo negli appunti.
(() => {
  const box = document.createElement('div');
  box.id = 'perf-hud';
  box.style.cssText = 'position:fixed;left:8px;top:calc(env(safe-area-inset-top,0px) + 70px);z-index:99999;padding:8px 10px;border-radius:10px;' +
    'background:rgba(8,11,18,.82);color:#e9e9ed;font:11px/1.45 ui-monospace,"SF Mono",Menlo,monospace;pointer-events:auto;min-width:170px;' +
    'backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);border:.5px solid rgba(255,255,255,.15)';
  box.innerHTML = '<div id="perf-t"></div><div style="display:flex;gap:6px;margin-top:6px">' +
    '<button id="perf-c" type="button" style="flex:1;font:inherit;padding:3px 6px;border-radius:6px;border:0;background:#7c6cf0;color:#fff">Copia</button>' +
    '<button id="perf-x" type="button" style="font:inherit;padding:3px 8px;border-radius:6px;border:0;background:rgba(255,255,255,.15);color:#fff">×</button></div>';
  document.body.appendChild(box);
  const out = box.querySelector('#perf-t');

  // fotogrammi della pagina: intervalli tra un requestAnimationFrame e il successivo, ultimi 10 s
  const dt = [];
  let prec = performance.now();
  const giro = now => { dt.push([now, now - prec]); prec = now; while (dt.length && now - dt[0][0] > 10000) dt.shift(); requestAnimationFrame(giro); };
  requestAnimationFrame(giro);

  // fotogrammi disegnati dalla mappa 3D (se aperta)
  const mapFr = []; let mappa = null;
  const aggancia = () => { const m = window.Metek3D?._map; if (m && m !== mappa) { mappa = m; m.on('render', () => mapFr.push(performance.now())); } };

  // compiti lunghi (Chrome/Edge; Safari non li espone)
  const lunghi = []; let lunghiOk = false;
  try { new PerformanceObserver(l => l.getEntries().forEach(e => lunghi.push([e.startTime, e.duration]))).observe({ type: 'longtask', buffered: true }); lunghiOk = true; } catch (e) {}

  // ping: HEAD su sw.js (il service worker lascia passare tutto ciò che non è GET)
  const ping = [];
  const misuraPing = async () => {
    const t0 = performance.now();
    try { await fetch('sw.js?ping=' + Date.now(), { method: 'HEAD', cache: 'no-store' }); ping.push(Math.round(performance.now() - t0)); } catch (e) { ping.push(null); }
    if (ping.length > 10) ping.shift();
  };
  misuraPing(); const pingT = setInterval(misuraPing, 5000);

  let gpu = 'n/d';
  try { const gl = document.createElement('canvas').getContext('webgl'); const x = gl.getExtension('WEBGL_debug_renderer_info');
    gpu = x ? gl.getParameter(x.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER); } catch (e) {}

  const attesa = m => Object.entries(m.style?.sourceCaches || {}).filter(([, c]) => Object.values(c._tiles || {}).some(t => t.state === 'loading' || t.state === 'reloading')).map(([id]) => id).join(', ') || '?';
  const pct = (a, p) => { if (!a.length) return null; const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; };
  const testo = () => {
    const now = performance.now(), d = dt.map(x => x[1]), sec = Math.min(10, (now - (dt[0]?.[0] ?? now)) / 1000) || 1;
    while (mapFr.length && now - mapFr[0] > 10000) mapFr.shift();
    while (lunghi.length && now - lunghi[0][0] > 10000) lunghi.shift();
    const okP = ping.filter(p => p != null), c = navigator.connection, mem = performance.memory;
    const r = [
      ['vista', window.Metek3D?.attivo ? '3D' : '2D'],
      ['3D pronto', window.Metek3D_t ? `stile ${window.Metek3D_t.stile ?? '…'} ms · tutto ${window.Metek3D_t.carica ?? '…'} ms` : '—'],
      ['velo ultima ora', window.Metek3D_t?.calcolo != null ? `calcolo ${window.Metek3D_t.calcolo} · disegno ${window.Metek3D_t.disegno} · fotogr. ${window.Metek3D_t.fotogramma ?? '…'} ms` : '—'],
      ['tessere', mappa ? (mappa.areTilesLoaded() ? 'tutte arrivate' : 'in attesa: ' + attesa(mappa)) : '—'],
      ['fps pagina', (d.length / sec).toFixed(0)],
      ['fps mappa 3D', mappa && window.Metek3D?.attivo ? (mapFr.length / sec).toFixed(0) : '—'],
      ['fotogr. p95', d.length ? pct(d, .95).toFixed(1) + ' ms' : '—'],
      ['scatti >50ms', String(d.filter(x => x > 50).length) + ' /10 s'],
      ['compiti lunghi', lunghiOk ? lunghi.length + ' (' + Math.round(lunghi.reduce((s, x) => s + x[1], 0)) + ' ms) /10 s' : 'n/d'],
      ['ping', okP.length ? pct(okP, .5) + ' ms (max ' + Math.max(...okP) + ')' : (ping.length ? 'errore' : '…')],
      ['rete', c ? `${c.effectiveType || '?'}${c.rtt != null ? ' rtt ' + c.rtt + ' ms' : ''}${c.downlink ? ' ' + c.downlink + ' Mb/s' : ''}` : 'n/d'],
      ['memoria JS', mem ? Math.round(mem.usedJSHeapSize / 1e6) + ' MB' : 'n/d'],
      ['schermo', `${innerWidth}×${innerHeight} @${devicePixelRatio}x` + (mappa ? ` · mappa @${mappa.getPixelRatio()}x` : '')],
      ['GPU', gpu.length > 38 ? gpu.slice(0, 38) + '…' : gpu],
    ];
    return r;
  };
  const scrivi = () => { aggancia(); out.innerHTML = testo().map(([k, v]) => `<div><span style="color:#9aa3b5">${k}</span> <b style="font-weight:600">${v}</b></div>`).join(''); };
  const timer = setInterval(scrivi, 1000); scrivi();

  box.querySelector('#perf-c').onclick = async () => {
    const s = `Metek prestazioni ${new Date().toLocaleString('it-IT')}\n${navigator.userAgent}\n` + testo().map(([k, v]) => `${k}: ${v}`).join('\n');
    try { await navigator.clipboard.writeText(s); box.querySelector('#perf-c').textContent = 'Copiato'; }
    catch (e) { prompt('Copia il riepilogo:', s); }
    setTimeout(() => { box.querySelector('#perf-c').textContent = 'Copia'; }, 1500);
  };
  box.querySelector('#perf-x').onclick = () => { clearInterval(timer); clearInterval(pingT); box.remove(); try { localStorage.removeItem('metek-perf'); } catch (e) {} };
})();
