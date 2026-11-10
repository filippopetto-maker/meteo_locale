"""
griglia_m2.py — M2 su griglia: la temperatura M2 su tutte le celle della mappa, da +1 a +48 h

Dal 11/2026. Ogni ora, nell'export:
  1. ultima corsa ICON-2I (2,2 km) e IFS 0,25° dall'archivio Open-Meteo su AWS (s3://openmeteo/data_run,
     anonimo, niente quota), letta solo sul riquadro del Lazio
  2. celle di ~5 km: campi del modello e quota media della cella, temperatura riportata a quella quota
     (−0,65 °C/100 m dalla quota del modello)
  3. pixel della mappa (250×220, ~1 km): campi delle celle interpolati e riportati alla quota del pixel,
     variabili di terreno del pixel (DEM Copernicus 90 m, WorldCover), correzione LightGBM addestrata su
     175 stazioni e 19 mesi più le corse vere a 48 h (spenta fra 1.600 e 2.100 m; nessuna correzione in mare)
  4. aggancio all'M2 delle stazioni: lo scarto M2 di stazione − griglia nel punto si spande entro ~6 km
     e a quote simili (±300 m); una stazione lontana sia dalla griglia sia da ICON (> 2,5 °C) non si aggancia

File (bundle dei modelli, models.lock): model/griglia_m2/modello.txt.gz, model/griglia_m2/statico.npz
(terreno di pixel, celle e stazioni; lo costruisce scripts/griglia_m2_statico.py).

Uscita: un dict pronto per docs/data/griglia_48h.json; temperature a passi di 0,2 °C, prima ora intera
e poi variazioni da un'ora all'altra, deflate e base64 (il sito le decomprime con DecompressionStream);
~0,9 MB per 48 ore.
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
ANCHOR_R_KM, ANCHOR_DZ, ANCHOR_MAX = 6.0, 300.0, 2.5
ORE = 48
PASSO = 0.2  # °C, quantizzazione del file
SRC = {
    "icon": dict(path="openmeteo/data_run/italia_meteo_arpae_icon_2i", lat0=33.7, dlat=0.02, lon0=3.0,
                 dlon=0.025, step_h=1, ogni_h=3,
                 vars=["temperature_2m", "temperature_850hPa", "temperature_700hPa"]),
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


def calcola(stazioni: dict[int, dict[str, float]], ora: datetime | None = None) -> dict | None:
    """stazioni: {id: {valid_for ISO 'AAAA-MM-GGTHH:00:00+00:00': t M2}}. Ritorna il dict del file o None."""
    import lightgbm as lgb
    import pandas as pd
    from scipy.ndimage import map_coordinates

    tic = time.time()
    S = np.load(DIR / "statico.npz")
    with gzip.open(DIR / "modello.txt.gz", "rt") as f:
        mdl = lgb.Booster(model_str=f.read())
    feats = mdl.feature_name()
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

    # pesi dell'aggancio (pixel × stazioni), fissi
    W = (np.exp(-(_km(PLA.ravel()[:, None], PLO.ravel()[:, None], st_lat[None], st_lon[None]) / ANCHOR_R_KM) ** 2)
         * np.exp(-((pix["z"][:, None] - sta["z"][None]) / ANCHOR_DZ) ** 2)).astype(np.float32)
    W[sea] = 0

    ih_c, fh_c, zc = S["icon_hsurf_c"], S["ifs_hsurf_c"], S["zc"]
    nt_icon = ic["temperature_2m"].shape[2]
    h3 = np.arange(fc["temperature_2m"].shape[2]) * SRC["ifs"]["step_h"]
    start = ora.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    times, out, n_agg = [], [], []
    for k in range(ORE):
        valid = start + timedelta(hours=k)
        li = int((valid - ti).total_seconds() // 3600)
        lf = (valid - tf).total_seconds() / 3600
        if li >= nt_icon or lf > h3[-1]:
            break

        def icv(v):
            return _interp(ic[v][:, :, li], SRC["icon"], ic["_i0"], ic["_j0"], CLA, CLO).reshape(CLA.shape)

        def ifv(v):
            a = fc[v]
            m = min(int(lf // 3), len(h3) - 2)
            w = (lf - h3[m]) / 3
            return _interp(a[:, :, m] * (1 - w) + a[:, :, m + 1] * w, SRC["ifs"], fc["_i0"], fc["_j0"],
                           CLA, CLO).reshape(CLA.shape)
        cells = {"t_icon": icv("temperature_2m") + LAPSE * (ih_c - zc),
                 "t_ifs": ifv("temperature_2m") + LAPSE * (fh_c - zc),
                 "t850": icv("temperature_850hPa"), "t700": icv("temperature_700hPa"),
                 "rh": ifv("relative_humidity_2m"), "cloud": ifv("cloud_cover"),
                 "sw": ifv("shortwave_radiation"),
                 "ws": np.hypot(ifv("wind_u_component_10m"), ifv("wind_v_component_10m")) * 3.6,
                 "zc": zc}

        def frame(ij, ter):
            v = {n: map_coordinates(a, ij, order=1, mode="nearest") for n, a in cells.items()}
            dz = v.pop("zc") - ter["z"]
            v["t_icon"] = v["t_icon"] + LAPSE * dz
            v["t_ifs"] = v["t_ifs"] + LAPSE * dz
            X = pd.DataFrame({**v, **ter})
            X["fg"] = X.t_icon
            X["d_ifs_icon"] = X.t_ifs - X.t_icon
            X["dt_850"] = X.fg - X.t850
            X["hour_sin"], X["hour_cos"] = np.sin(2 * np.pi * valid.hour / 24), np.cos(2 * np.pi * valid.hour / 24)
            doy = valid.timetuple().tm_yday
            X["doy_sin"], X["doy_cos"] = np.sin(2 * np.pi * doy / 365.25), np.cos(2 * np.pi * doy / 365.25)
            X["lead"] = li
            return X
        X = frame(pij, pix)
        m2g = X.fg.to_numpy() + np.where(sea, 0, mdl.predict(X[feats]) * fade)
        Xs = frame(sij, sta)
        fz = np.clip((FADE_Z1 - sta["z"]) / (FADE_Z1 - FADE_Z0), 0, 1)
        m2g_st = Xs.fg.to_numpy() + mdl.predict(Xs[feats]) * fz

        key = valid.strftime("%Y-%m-%dT%H:00:00+00:00")
        t_st = np.array([stazioni.get(int(s), {}).get(key, np.nan) for s in st_id], dtype=float)
        delta = t_st - m2g_st
        ok = np.isfinite(delta) & ((np.abs(delta) <= ANCHOR_MAX) | (np.abs(t_st - Xs.fg.to_numpy()) <= ANCHOR_MAX))
        d = np.where(ok, delta, 0.0).astype(np.float32)
        num = W[:, ok] @ d[ok]
        den = W[:, ok].sum(1)
        final = m2g + num / np.maximum(den, 1.0)
        times.append(key)
        out.append(final.reshape(ny, nx))
        n_agg.append(int(ok.sum()))

    if not out:
        log.warning("griglia M2: nessuna ora calcolabile")
        return None
    arr = np.stack(out)
    # passi di 0,2 °C: prima ora in int16, poi le variazioni da un'ora all'altra in int8 (−25…+25 °C)
    q = np.round(arr / PASSO).astype(np.int32)
    dq = np.diff(q, axis=0)
    if np.abs(dq).max() > 127:
        raise ValueError(f"variazione oraria fuori scala ({np.abs(dq).max() * PASSO:.1f} °C)")
    b0 = zlib.compress(q[0].astype("<i2").tobytes(), 9)
    b1 = zlib.compress(dq.astype(np.int8).tobytes(), 9)
    log.info(f"griglia M2: {len(times)} ore, {arr.min():.1f}…{arr.max():.1f} °C, stazioni agganciate "
             f"{min(n_agg)}–{max(n_agg)} su {len(st_id)}, {(len(b0) + len(b1)) / 1e6:.2f} MB, {time.time() - tic:.0f} s")
    return {
        "versione": 1,
        "corsa_icon": ti.strftime("%Y-%m-%dT%H:00:00+00:00"),
        "corsa_ifs": tf.strftime("%Y-%m-%dT%H:00:00+00:00"),
        **g, "nx": nx, "ny": ny,
        "times": times,
        "t_min": round(float(arr.min()), 1), "t_max": round(float(arr.max()), 1),
        "passo": PASSO,
        "codifica": "righe da nord, °C/passo; prima ora int16 LE, poi variazioni orarie int8; deflate (zlib), base64",
        "b64_prima": base64.b64encode(b0).decode(),
        "b64_variazioni": base64.b64encode(b1).decode(),
    }
