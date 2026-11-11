"""
griglia_m2.py — M2 su griglia: temperatura, umidità e vento M2 su tutte le celle della mappa, ora attuale e +1…+48 h

Dal 11/2026 (temperatura; umidità e vento dal 12/11/2026). Ogni ora, nell'export:
  1. ultima corsa ICON-2I (2,2 km) e IFS 0,25° dall'archivio Open-Meteo su AWS (s3://openmeteo/data_run,
     anonimo, niente quota), letta solo sul riquadro del Lazio
  2. celle di ~5 km: campi del modello e quota media della cella, temperatura riportata a quella quota
     (−0,65 °C/100 m dalla quota del modello)
  3. pixel della mappa (250×220, ~1 km): campi delle celle interpolati e riportati alla quota del pixel,
     variabili di terreno del pixel (DEM Copernicus 90 m, WorldCover), correzioni LightGBM
     (spente fra 1.600 e 2.100 m; nessuna correzione in mare):
       temperatura  175 stazioni, 19 mesi, più le corse vere a 48 h
       umidità      ICON-2I portata alla quota del pixel per punto di rugiada + correzione (181 stazioni)
       vento 10 m   taratura del vento di ICON-2I sui 7 aeroporti METAR (senza terreno: 7 punti in pianura
                    non bastano) più le corse vere; la direzione resta quella di ICON-2I
  4. aggancio alle stazioni: lo scarto stazione − griglia nel punto si spande entro ~6 km e a quote simili
     (±300 m; vento 15 km e ±500 m, a 6 km lasciava buchi); una stazione lontana sia dalla griglia sia da
     ICON (> 2,5 °C, 15 punti %, fattore 2 nel vento) non si aggancia.
     Ore di previsione: M2 delle stazioni (forecast_48h). Ora attuale: misure delle stazioni (meno di
     90 minuti) per temperatura e umidità; per il vento l'M2 della +1h (gli anemometri Netatmo sono
     riparati e misurano meno del vento a 10 m).

File (bundle dei modelli, models.lock): model/griglia_m2/{modello,modello_umidita,modello_vento}.txt.gz,
model/griglia_m2/statico.npz (terreno di pixel, celle e stazioni; lo costruisce scripts/griglia_m2_statico.py).

Uscita: un dict {nome file: contenuto} per docs/data/: griglia_48h.json (temperatura, passi di 0,2 °C),
griglia_umidita_48h.json (passi dell'1%), griglia_vento_48h.json (passi di 1 km/h). Stesso formato per
tutti: prima ora intera e poi variazioni da un'ora all'altra, deflate e base64 (il sito le decomprime con
DecompressionStream); "ora" è la prima ora del file se c'è l'ora attuale. ~0,9 + 0,7 + 0,6 MB.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import logging
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

log = logging.getLogger("griglia_m2")
ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "model" / "griglia_m2"
LAPSE = 0.0065
FADE_Z0, FADE_Z1 = 1600.0, 2100.0
LAPSE_TD = 0.0018  # punto di rugiada, °C/m
ANCHOR_R_KM, ANCHOR_DZ, ANCHOR_MAX = 6.0, 300.0, 2.5
ANCHOR_R_KM_W, ANCHOR_DZ_W = 15.0, 500.0
ANCHOR_MAX_RH, ANCHOR_MAX_LOGW = 15.0, 0.7
OBS_MAX_MIN = 90  # minuti: misure più vecchie non agganciano l'ora attuale
ORE = 48
PASSO = 0.2  # °C, quantizzazione del file della temperatura
VARIABILI = {  # chiave, file, modello, passo, unità
    "t": ("griglia_48h.json", "modello.txt.gz", 0.2, "°C"),
    "rh": ("griglia_umidita_48h.json", "modello_umidita.txt.gz", 1.0, "%"),
    "ws": ("griglia_vento_48h.json", "modello_vento.txt.gz", 1.0, "km/h"),
}
SRC = {
    "icon": dict(path="openmeteo/data_run/italia_meteo_arpae_icon_2i", lat0=33.7, dlat=0.02, lon0=3.0,
                 dlon=0.025, step_h=1, ogni_h=3,
                 vars=["temperature_2m", "temperature_850hPa", "temperature_700hPa", "relative_humidity_2m",
                       "wind_speed_10m", "wind_direction_10m", "wind_speed_850hPa"]),
    "ifs": dict(path="openmeteo/data_run/ecmwf_ifs025", lat0=-90.0, dlat=0.25, lon0=-180.0, dlon=0.25,
                step_h=3, ogni_h=6,
                vars=["temperature_2m", "relative_humidity_2m", "cloud_cover", "shortwave_radiation",
                      "wind_u_component_10m", "wind_v_component_10m"]),
}


# ── lettura delle corse ──────────────────────────────────────────────────────

async def _leggi(fs, src: dict, run: datetime, box, sem) -> dict[str, np.ndarray]:
    import omfiles
    i0 = int(np.floor((box[0] - src["lat0"]) / src["dlat"])) - 1
    i1 = int(np.ceil((box[1] - src["lat0"]) / src["dlat"])) + 2
    j0 = int(np.floor((box[2] - src["lon0"]) / src["dlon"])) - 1
    j1 = int(np.ceil((box[3] - src["lon0"]) / src["dlon"])) + 2
    base = f"{src['path']}/{run:%Y/%m/%d/%H%M}Z"

    async def var(v):
        r = await omfiles.OmFileReaderAsync.from_fsspec(fs, f"{base}/{v}.om")
        nt = r.shape[2]

        async def riga(i):
            async with sem:
                return await r.read_array((slice(i, i + 1), slice(j0, j1), slice(0, nt)))
        return np.concatenate(await asyncio.gather(*[riga(i) for i in range(i0, i1)])).astype(np.float32)

    arrs = await asyncio.gather(*[var(v) for v in src["vars"]])
    out = dict(zip(src["vars"], arrs))
    out["_i0"], out["_j0"] = i0, j0
    return out


async def _ultima(fs, src: dict, ora: datetime, box, sem):
    """Corsa più recente completa (tutti i file presenti), cercando indietro fino a 36 h."""
    t = ora.replace(minute=0, second=0, microsecond=0)
    t -= timedelta(hours=t.hour % src["ogni_h"])
    for _ in range(36 // src["ogni_h"] + 1):
        base = f"{src['path']}/{t:%Y/%m/%d/%H%M}Z"
        try:
            ok = all([await fs._exists(f"{base}/{v}.om") for v in src["vars"]])
        except Exception:
            ok = False
        if ok:
            try:
                return t, await _leggi(fs, src, t, box, sem)
            except Exception as exc:  # corsa ancora in scrittura o rete: si prova la precedente
                log.warning(f"{base}: {exc}")
        t -= timedelta(hours=src["ogni_h"])
    raise RuntimeError(f"nessuna corsa completa per {src['path']}")


async def _corse(ora: datetime, box):
    import s3fs
    fs = s3fs.S3FileSystem(anon=True, asynchronous=True)
    session = await fs.set_session()
    sem = asyncio.Semaphore(64)
    try:
        (ti, ic), (tf, fc) = await asyncio.gather(_ultima(fs, SRC["icon"], ora, box, sem),
                                                  _ultima(fs, SRC["ifs"], ora, box, sem))
    finally:
        await session.close()
    return ti, ic, tf, fc


# ── calcolo ──────────────────────────────────────────────────────────────────

def _interp(a2d: np.ndarray, src: dict, i0: int, j0: int, lat, lon) -> np.ndarray:
    from scipy.ndimage import map_coordinates
    i = (np.ravel(lat) - src["lat0"]) / src["dlat"] - i0
    j = (np.ravel(lon) - src["lon0"]) / src["dlon"] - j0
    return map_coordinates(a2d, [i, j], order=1, mode="nearest")


def _km(lat1, lon1, lat2, lon2):
    p = np.pi / 180
    a = (np.sin((lat2 - lat1) * p / 2) ** 2
         + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * np.arcsin(np.sqrt(a))


def _td(t, rh):
    a, b = 17.62, 243.12
    g = np.log(np.clip(rh, 1, 100) / 100) + a * t / (b + t)
    return b * g / (a - g)


def _rh(t, td):
    a, b = 17.62, 243.12
    return np.clip(100 * np.exp(a * td / (b + td) - a * t / (b + t)), 1, 100)


def _booster(nome: str):
    import lightgbm as lgb
    p = DIR / nome
    if not p.exists():
        return None
    with gzip.open(p, "rt") as f:
        return lgb.Booster(model_str=f.read())


def _codifica(arr: np.ndarray, passo: float) -> tuple[bytes, bytes]:
    """passi interi: prima ora int16, poi le variazioni da un'ora all'altra in int8."""
    q = np.round(arr / passo).astype(np.int32)
    dq = np.diff(q, axis=0)
    if np.abs(dq).max() > 127:
        raise ValueError(f"variazione oraria fuori scala ({np.abs(dq).max() * passo:.1f})")
    return zlib.compress(q[0].astype("<i2").tobytes(), 9), zlib.compress(dq.astype(np.int8).tobytes(), 9)


def calcola(f48: dict, osservazioni: dict | None = None, ora: datetime | None = None) -> dict[str, dict]:
    """f48: contenuto di forecast_48h.json ({"stations": {id: {valid_for, t, rh, ws, ...}}}).
    osservazioni: {id: {recorded_at, temperature, humidity}} (ultime misure; facoltative).
    Ritorna {nome file: dict} (vuoto se nessuna ora è calcolabile)."""
    import pandas as pd
    from scipy.ndimage import map_coordinates

    tic = time.time()
    S = np.load(DIR / "statico.npz")
    mdl = {k: _booster(v[1]) for k, v in VARIABILI.items()}
    if mdl["t"] is None:
        raise FileNotFoundError(DIR / VARIABILI["t"][1])
    g = {k: float(S["griglia"][n]) for n, k in enumerate(["lat_min", "lat_max", "lon_min", "lon_max"])}
    ny, nx = S["z"].shape
    clat, clon = S["cell_lat"], S["cell_lon"]
    CLA, CLO = np.meshgrid(clat, clon, indexing="ij")
    box = (clat[0] - 0.05, clat[-1] + 0.05, clon[0] - 0.05, clon[-1] + 0.05)

    ora = ora or datetime.now(timezone.utc)
    ti, ic, tf, fc = asyncio.run(_corse(ora, box))
    log.info(f"corse ICON-2I {ti:%d/%m %H} UTC, IFS {tf:%d/%m %H} UTC ({time.time() - tic:.0f} s)")

    # pixel e stazioni: coordinate nella griglia delle celle
    plat = np.linspace(g["lat_max"], g["lat_min"], ny)
    plon = np.linspace(g["lon_min"], g["lon_max"], nx)
    PLA, PLO = np.meshgrid(plat, plon, indexing="ij")
    ter_names = ["z", "tpi1", "tpi3", "relief3", "slope", "aspect_sin", "aspect_cos", "dist_coast",
                 "urb1", "urb3", "water3"]
    pix = {k: S[k].ravel().astype(np.float64) for k in ter_names}
    sta = {k: S[f"st_{k}"].astype(np.float64) for k in ter_names}
    st_id, st_lat, st_lon = S["st_id"], S["st_lat"], S["st_lon"]
    dlat, dlon = clat[1] - clat[0], clon[1] - clon[0]

    def cell_ij(lat, lon):
        return [(np.ravel(lat) - clat[0]) / dlat, (np.ravel(lon) - clon[0]) / dlon]
    pij, sij = cell_ij(PLA, PLO), cell_ij(st_lat, st_lon)
    sea = pix["z"] <= 0
    fade = np.clip((FADE_Z1 - pix["z"]) / (FADE_Z1 - FADE_Z0), 0, 1)
    fz = np.clip((FADE_Z1 - sta["z"]) / (FADE_Z1 - FADE_Z0), 0, 1)

    # pesi dell'aggancio (pixel × stazioni), fissi
    km = _km(PLA.ravel()[:, None], PLO.ravel()[:, None], st_lat[None], st_lon[None])
    dzz = pix["z"][:, None] - sta["z"][None]
    W = (np.exp(-(km / ANCHOR_R_KM) ** 2) * np.exp(-(dzz / ANCHOR_DZ) ** 2)).astype(np.float32)
    Ww = (np.exp(-(km / ANCHOR_R_KM_W) ** 2) * np.exp(-(dzz / ANCHOR_DZ_W) ** 2)).astype(np.float32)
    del km, dzz
    W[sea] = 0
    Ww[sea] = 0

    # valori delle stazioni per ora: M2 (forecast_48h) e misure dell'ora attuale
    st48 = {}
    for sid, s48 in (f48 or {}).get("stations", {}).items():
        st48[int(sid)] = {vf: (s48["t"][n], s48["rh"][n], s48["ws"][n]) for n, vf in enumerate(s48["valid_for"])}
    adesso = ora.replace(minute=0, second=0, microsecond=0)
    obs = {}
    for sid, o in (osservazioni or {}).items():
        try:
            age = (ora - datetime.fromisoformat(str(o["recorded_at"]).replace("Z", "+00:00"))).total_seconds() / 60
        except Exception:
            continue
        if age <= OBS_MAX_MIN:
            obs[int(sid)] = (o.get("temperature"), o.get("humidity"))

    def st_vals(key: str, k_var: int) -> np.ndarray:
        return np.array([(st48.get(int(s), {}).get(key) or (np.nan,) * 3)[k_var] for s in st_id], dtype=float)

    def aggancia(grid, at_st, val, fg_st, lim, Wm):
        delta = val - at_st
        ok = np.isfinite(delta) & ((np.abs(delta) <= lim) | (np.abs(val - fg_st) <= lim))
        d = np.where(ok, delta, 0.0).astype(np.float32)
        return grid + (Wm[:, ok] @ d[ok]) / np.maximum(Wm[:, ok].sum(1), 1.0), int(ok.sum())

    ih_c, fh_c, zc = S["icon_hsurf_c"], S["ifs_hsurf_c"], S["zc"]
    nt_icon = ic["temperature_2m"].shape[2]
    h3 = np.arange(fc["temperature_2m"].shape[2]) * SRC["ifs"]["step_h"]
    times, n_agg = [], []
    out = {k: [] for k in VARIABILI}
    for k in range(ORE + 1):  # k = 0: ora attuale
        valid = adesso + timedelta(hours=k)
        li = int((valid - ti).total_seconds() // 3600)
        lf = (valid - tf).total_seconds() / 3600
        if li >= nt_icon or lf > h3[-1]:
            break
        if li < 0 or lf < 0:
            continue

        def icv(v):
            return _interp(ic[v][:, :, li], SRC["icon"], ic["_i0"], ic["_j0"], CLA, CLO).reshape(CLA.shape)

        def ifv(v):
            a = fc[v]
            m = min(int(lf // 3), len(h3) - 2)
            w = (lf - h3[m]) / 3
            return _interp(a[:, :, m] * (1 - w) + a[:, :, m + 1] * w, SRC["ifs"], fc["_i0"], fc["_j0"],
                           CLA, CLO).reshape(CLA.shape)
        ws_i, wd_i = icv("wind_speed_10m") * 3.6, np.radians(icv("wind_direction_10m"))
        # direzione: si interpolano le componenti (un angolo non si media)
        cells = {"t_icon": icv("temperature_2m") + LAPSE * (ih_c - zc),
                 "t_ifs": ifv("temperature_2m") + LAPSE * (fh_c - zc),
                 "t850": icv("temperature_850hPa"), "t700": icv("temperature_700hPa"),
                 "rh": ifv("relative_humidity_2m"), "cloud": ifv("cloud_cover"),
                 "sw": ifv("shortwave_radiation"),
                 "ws": np.hypot(ifv("wind_u_component_10m"), ifv("wind_v_component_10m")) * 3.6,
                 "rh_icon": icv("relative_humidity_2m"), "ws_icon": ws_i, "ws850": icv("wind_speed_850hPa") * 3.6,
                 "u": ws_i * np.sin(wd_i), "v": ws_i * np.cos(wd_i), "ih": ih_c, "zc": zc}

        def frame(ij, ter):
            v = {n: map_coordinates(a, ij, order=1, mode="nearest") for n, a in cells.items()}
            dz = v.pop("zc") - ter["z"]
            v["t_icon"] = v["t_icon"] + LAPSE * dz
            v["t_ifs"] = v["t_ifs"] + LAPSE * dz
            X = pd.DataFrame({**v, **ter})
            X["dz_icon"] = X.pop("ih") - X.z
            X["fg"] = X.t_icon
            X["d_ifs_icon"] = X.t_ifs - X.t_icon
            X["dt_850"] = X.fg - X.t850
            X["hour_sin"], X["hour_cos"] = np.sin(2 * np.pi * valid.hour / 24), np.cos(2 * np.pi * valid.hour / 24)
            doy = valid.timetuple().tm_yday
            X["doy_sin"], X["doy_cos"] = np.sin(2 * np.pi * doy / 365.25), np.cos(2 * np.pi * doy / 365.25)
            X["lead"] = max(li, 1)
            t_raw = X.t_icon - LAPSE * X.dz_icon
            X["fg_h"] = _rh(X.t_icon, _td(t_raw, X.rh_icon) + LAPSE_TD * X.dz_icon)
            X["d_rh"] = X.rh - X.rh_icon
            wd = np.arctan2(X.u, X.v)
            X["wd_sin"], X["wd_cos"] = np.sin(wd), np.cos(wd)
            return X
        X, Xs = frame(pij, pix), frame(sij, sta)
        key = valid.strftime("%Y-%m-%dT%H:00:00+00:00")
        key1 = (adesso + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00+00:00")
        if k == 0:
            val_t = np.array([(obs.get(int(s)) or (None, None))[0] for s in st_id], dtype=float)
            val_h = np.array([(obs.get(int(s)) or (None, None))[1] for s in st_id], dtype=float)
            val_w = st_vals(key1, 2)
        else:
            val_t, val_h, val_w = st_vals(key, 0), st_vals(key, 1), st_vals(key, 2)

        def predici(m, X_, f_):
            return m.predict(X_[m.feature_name()]) * f_
        t_g = X.fg.to_numpy() + np.where(sea, 0, predici(mdl["t"], X, fade))
        t_s = Xs.fg.to_numpy() + predici(mdl["t"], Xs, fz)
        t_f, nt = aggancia(t_g, t_s, val_t, Xs.fg.to_numpy(), ANCHOR_MAX, W)
        out["t"].append(t_f)
        na = [nt]
        if mdl["rh"] is not None:
            h_g = np.clip(X.fg_h.to_numpy() + np.where(sea, 0, predici(mdl["rh"], X, fade)), 1, 100)
            h_s = np.clip(Xs.fg_h.to_numpy() + predici(mdl["rh"], Xs, fz), 1, 100)
            h_f, nh = aggancia(h_g, h_s, val_h, Xs.fg_h.to_numpy(), ANCHOR_MAX_RH, W)
            out["rh"].append(np.clip(h_f, 1, 100))
            na.append(nh)
        if mdl["ws"] is not None:
            lw_g = np.log1p(X.ws_icon.to_numpy()) + np.where(sea, 0, predici(mdl["ws"], X, fade))
            lw_s = np.log1p(Xs.ws_icon.to_numpy()) + predici(mdl["ws"], Xs, fz)
            lw_f, nw = aggancia(lw_g, lw_s, np.log1p(np.clip(val_w, 0, None)), np.log1p(Xs.ws_icon.to_numpy()),
                                ANCHOR_MAX_LOGW, Ww)
            out["ws"].append(np.expm1(np.clip(lw_f, 0, None)))
            na.append(nw)
        times.append(key)
        n_agg.append(na)

    if not times:
        log.warning("griglia M2: nessuna ora calcolabile")
        return {}
    files = {}
    n_agg = np.array(n_agg)
    for n, (var, (nome, _, passo, unita)) in enumerate(VARIABILI.items()):
        if not out[var]:
            continue
        arr = np.stack(out[var]).reshape(len(times), ny, nx)
        try:
            b0, b1 = _codifica(arr, passo)
        except ValueError as exc:
            log.error(f"{nome} non scritto: {exc}")
            continue
        log.info(f"{nome}: {len(times)} ore, {arr.min():.1f}…{arr.max():.1f} {unita}, stazioni agganciate "
                 f"{n_agg[:, n].min()}–{n_agg[:, n].max()} su {len(st_id)}, {(len(b0) + len(b1)) / 1e6:.2f} MB")
        files[nome] = {
            "versione": 1,
            "variabile": var,
            "corsa_icon": ti.strftime("%Y-%m-%dT%H:00:00+00:00"),
            "corsa_ifs": tf.strftime("%Y-%m-%dT%H:00:00+00:00"),
            **g, "nx": nx, "ny": ny,
            "times": times,
            "ora": times[0] if times[0] == adesso.strftime("%Y-%m-%dT%H:00:00+00:00") else None,
            "t_min": round(float(arr.min()), 1), "t_max": round(float(arr.max()), 1),
            "passo": passo,
            "unita": unita,
            "codifica": f"righe da nord, {unita}/passo; prima ora int16 LE, poi variazioni orarie int8; deflate (zlib), base64",
            "b64_prima": base64.b64encode(b0).decode(),
            "b64_variazioni": base64.b64encode(b1).decode(),
        }
    log.info(f"griglia M2: {time.time() - tic:.0f} s")
    return files
