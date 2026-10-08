"""Fase 1 — catalogo degli eventi: ERA5 mese per mese, CAPE dello strato rimescolato, candidati.

Per ogni mese (aprile–novembre 2015–2025 per default):
1. ERA5 single levels orari sul dominio: CAPE (most-unstable ERA5), CIN, precipitazione
   convettiva, più 2t, 2d e pressione al suolo (servono alla CAPE rimescolata);
2. ERA5 pressure levels ogni 3 ore (00, 03, …, 21 UTC): T e q su 23 livelli (1000–150 hPa);
3. CAPE e CIN dello strato rimescolato (100 hPa) su tutte le colonne con `indici.ml_cape_cin`
   (validata contro MetPy in S14: errore mediano 3%);
4. tabella dei candidati: per ogni istante a 3 ore e ogni riquadro 5°×5° del dominio, statistiche
   di ML-CAPE, ML-CIN, CAPE ERA5 e precipitazione convettiva delle 3 ore successive;
5. caricamento su HF (sezione `catalogo`): `catalogo/era5_AAAA_MM.nc` e
   `catalogo/candidati_AAAA_MM.parquet`, poi cancellazione dal Mac.

La CAPE rimescolata serve a capire e selezionare gli eventi: la CAPE ERA5 most-unstable può
esplodere per uno strato umido sottile al suolo (S13: 9888 J/kg contro 2707 rimescolata, nessuna eco
radar). Riprende dove si era fermato: salta i mesi già su HF con versione ≥ VERSIONE.

Uso:
    caffeinate -i python catalogo.py --parallelo 2        # tutto il periodo, a sessioni, 2 mesi alla volta
    python catalogo.py --da 2019-07 --a 2019-07           # un mese
Con --parallelo N si elaborano N mesi insieme (le attese del CDS si sovrappongono); i caricamenti
su HF restano in fila (un lucchetto) perché ognuno riscrive registro.json.
"""
from __future__ import annotations

import argparse
import calendar
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np

import archivio as ar
import indici
from common import DOMAIN_BBOX, mb, rm
from fonti import era5

VERSIONE = 2                       # 1 = solo CAPE/CIN/cp orari (S12); 2 = + ML-CAPE/CIN e candidati
ORE_ML = list(range(0, 24, 3))
MESI_DEFAULT = range(4, 12)        # aprile–novembre (planning)
RIQUADRO = 5.0                     # gradi
VAR_ORARIE = era5.VAR_CATALOGO + era5.VAR_SUPERFICIE_ML
_LUCCHETTO_HF = threading.Lock()


def riquadri(bb=DOMAIN_BBOX, passo=RIQUADRO):
    out = []
    for la in np.arange(bb["lat_min"], bb["lat_max"], passo):
        for lo in np.arange(bb["lon_min"], bb["lon_max"], passo):
            out.append({"riquadro": f"{la:+05.1f}{lo:+06.1f}", "lat_min": la, "lat_max": la + passo,
                        "lon_min": lo, "lon_max": lo + passo})
    return out


def _richiesta_a_pezzi(dataset, variabili, anno, mese, ore, lavoro, livelli=None, parti=(1, 2, 4, 8)):
    """Una richiesta per il mese; se il CDS la rifiuta per dimensione, la divide in parti di giorni."""
    import xarray as xr
    giorni = list(range(1, calendar.monthrange(anno, mese)[1] + 1))
    for n_parti in parti:
        try:
            parti, peso = [], 0.0
            for g in np.array_split(giorni, n_parti):
                ds, p = era5.richiesta(dataset, variabili, datetime(anno, mese, int(g[0])), ore, lavoro,
                                       livelli=livelli, giorni=[int(x) for x in g])
                parti.append(ds)
                peso += p
            return (xr.concat(parti, dim="time") if len(parti) > 1 else parti[0]), peso, n_parti
        except Exception as e:  # noqa: BLE001
            msg = repr(e).lower()
            if not any(k in msg for k in ("cost", "too large", "limit", "exceed")) or n_parti == parti[-1]:
                raise
            print(f"  richiesta troppo grande, divido in {n_parti * 2} parti")


def ml_mese(pl, sl):
    """ML-CAPE/CIN per ogni istante di `pl` (pressure levels) con la superficie di `sl` alla stessa ora."""
    livelli = pl.pressure_level.values.astype(float)
    ordine = np.argsort(-livelli)
    livelli = livelli[ordine]
    cape, cin = [], []
    for t in pl.time.values:
        s = sl.sel(time=t)
        p = pl.sel(time=t).isel(pressure_level=ordine)
        ny, nx = s.sp.shape
        T = p.t.values.reshape(len(livelli), -1).T
        q = p.q.values.reshape(len(livelli), -1).T
        with np.errstate(all="ignore"):
            r = indici.ml_cape_cin(s.sp.values.ravel() / 100, s.t2m.values.ravel(), s.d2m.values.ravel(),
                                   livelli, T, q)
        cape.append(r["cape"].reshape(ny, nx).astype("float32"))
        cin.append(r["cin"].reshape(ny, nx).astype("float32"))
    return np.stack(cape), np.stack(cin)


def candidati(ds):
    """Statistiche per istante a 3 ore e riquadro: base della selezione delle finestre."""
    import pandas as pd
    righe = []
    lat, lon = ds.latitude.values, ds.longitude.values
    cp3 = ds.cp.rolling(time=3).sum().shift(time=-2)          # mm nelle 3 h che iniziano all'istante (m → mm sotto)
    for r in riquadri():
        mlat = (lat >= r["lat_min"]) & (lat < r["lat_max"])
        mlon = (lon >= r["lon_min"]) & (lon < r["lon_max"])
        sub = ds.isel(latitude=mlat, longitude=mlon)
        cps = cp3.isel(latitude=mlat, longitude=mlon).sel(time=ds.time_ml.values) * 1000
        mlc = sub.mlcape.values.reshape(len(ds.time_ml), -1)
        mln = sub.mlcin.values.reshape(len(ds.time_ml), -1)
        cpe = sub.cape.sel(time=ds.time_ml.values).values.reshape(len(ds.time_ml), -1)
        cpv = cps.values.reshape(len(ds.time_ml), -1)
        with np.errstate(all="ignore"):
            instab = mlc >= 500
            for i, t in enumerate(ds.time_ml.values):
                righe.append({
                    "time": pd.Timestamp(t), "riquadro": r["riquadro"], "lat_min": r["lat_min"], "lon_min": r["lon_min"],
                    "mlcape_p90": float(np.nanpercentile(mlc[i], 90)), "mlcape_max": float(np.nanmax(mlc[i])),
                    "frac_mlcape_500": float(instab[i].mean()), "frac_mlcape_1000": float((mlc[i] >= 1000).mean()),
                    "mlcin_mediana_instabile": float(np.nanmedian(mln[i][instab[i]])) if instab[i].any() else np.nan,
                    "cape_era5_p90": float(np.nanpercentile(cpe[i], 90)),
                    "cp3h_mm_media": float(np.nanmean(cpv[i])), "cp3h_mm_max": float(np.nanmax(cpv[i])),
                    "frac_cp3h_1mm": float((cpv[i] >= 1).mean()),
                })
    return pd.DataFrame(righe)


def elabora_mese(anno: int, mese: int) -> dict:
    import xarray as xr
    chiave = ar.chiave_catalogo(anno, mese)
    lavoro = ar.DATA / "lavoro" / chiave
    out = ar.STAGING / "catalogo"
    out.mkdir(parents=True, exist_ok=True)
    ar.controlla_budget(1.5, hf=False)                 # download del mese in lavorazione
    mis = {"versione": VERSIONE, "anno": anno, "mese": mese}
    t0 = time.perf_counter()
    sl, p1, n1 = _richiesta_a_pezzi(era5.SINGLE, VAR_ORARIE, anno, mese, list(range(24)), lavoro)
    mis["secondi_cds_orari"] = round(time.perf_counter() - t0, 1)
    t0 = time.perf_counter()
    pl, p2, n2 = _richiesta_a_pezzi(era5.PRESSURE, ["temperature", "specific_humidity"], anno, mese, ORE_ML,
                                    lavoro, livelli=era5.LIVELLI_ML, parti=(2, 4, 8))   # il mese intero supera il limite CDS
    mis["secondi_cds_profili"] = round(time.perf_counter() - t0, 1)
    mis["MB_scaricati"] = round(p1 + p2, 1)
    mis["richieste_cds_parti"] = [n1, n2]
    t0 = time.perf_counter()
    mlcape, mlcin = ml_mese(pl, sl)
    mis["secondi_mlcape"] = round(time.perf_counter() - t0, 1)
    ds = sl[["cape", "cin", "cp"]].copy()
    ds = ds.assign_coords(time_ml=pl.time.values)
    ds["mlcape"] = (("time_ml", "latitude", "longitude"), mlcape)
    ds["mlcin"] = (("time_ml", "latitude", "longitude"), mlcin)
    ds["mlcape"].attrs = {"units": "J kg-1", "long_name": "CAPE strato rimescolato 100 hPa (indici.ml_cape_cin)"}
    ds["mlcin"].attrs = {"units": "J kg-1", "long_name": "CIN strato rimescolato 100 hPa (≤ 0)"}
    ds.attrs = {"fonte": "ERA5 (Copernicus CDS)", "versione_catalogo": VERSIONE,
                "nota_cin": "cin = CIN ERA5 grezza (NaN = > 1000 J/kg o nessuna base della nube); mlcin calcolata"}
    for v in ds.variables:
        ds[v].encoding = {}
    t0 = time.perf_counter()
    tab = candidati(ds)
    mis["secondi_candidati"] = round(time.perf_counter() - t0, 1)
    f_nc = out / f"{chiave}.nc"
    f_pq = out / f"candidati_{anno:04d}_{mese:02d}.parquet"
    ds.to_netcdf(f_nc, encoding={v: {"dtype": "float32", "zlib": True, "complevel": 4} for v in ds.data_vars})
    tab.to_parquet(f_pq, index=False)
    rm(lavoro)
    mis |= {"MB_nc": round(mb(f_nc), 1), "MB_candidati": round(mb(f_pq), 2), "righe_candidati": len(tab),
            "mlcape_max": round(float(np.nanmax(mlcape)), 0), "cape_era5_max": round(float(ds.cape.max()), 0)}
    with _LUCCHETTO_HF:
        up = ar.carica("catalogo", chiave, {f"catalogo/{f_nc.name}": f_nc, f"catalogo/{f_pq.name}": f_pq}, mis)
    mis["secondi_upload"] = up["secondi_upload"]
    return mis


def mesi(da: str, a: str, solo_mesi=MESI_DEFAULT):
    y, m = map(int, da.split("-"))
    y1, m1 = map(int, a.split("-"))
    while (y, m) <= (y1, m1):
        if m in solo_mesi:
            yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--da", default="2015-04")
    ap.add_argument("--a", default="2025-11")
    ap.add_argument("--tutti-i-mesi", action="store_true", help="non limitarsi ad aprile–novembre")
    ap.add_argument("--parallelo", type=int, default=1, help="mesi elaborati insieme (memoria: ~1 GB ciascuno)")
    args = ap.parse_args()
    elenco = list(mesi(args.da, args.a, range(1, 13) if args.tutti_i_mesi else MESI_DEFAULT))
    reg = ar.registro()
    fatti = [k for k in elenco if reg["catalogo"].get(ar.chiave_catalogo(*k), {}).get("versione", 1) >= VERSIONE]
    print(f"{len(elenco)} mesi nel periodo, {len(fatti)} già su HF, {len(elenco) - len(fatti)} da fare")
    da_fare = [k for k in elenco if k not in fatti]

    def uno(k):
        t0 = time.perf_counter()
        m = elabora_mese(*k)
        return k, m, time.perf_counter() - t0

    stop = False
    with ThreadPoolExecutor(args.parallelo) as ex:
        futuri = {}
        coda = iter(da_fare)
        for k in coda:                                  # riempie i posti liberi
            futuri[ex.submit(uno, k)] = k
            if len(futuri) >= args.parallelo:
                break
        while futuri:
            for f in as_completed(list(futuri)):
                k = futuri.pop(f)
                try:
                    _, m, sec = f.result()
                    print(f"{k[0]}-{k[1]:02d}: OK in {sec:.0f} s — {m}", flush=True)
                except ar.BudgetSuperato as e:
                    print(f"{k[0]}-{k[1]:02d}: STOP, budget: {e}", flush=True)
                    stop = True
                except Exception as e:  # noqa: BLE001
                    print(f"{k[0]}-{k[1]:02d}: ERRORE {e!r}", flush=True)
                    traceback.print_exc()
                if not stop:
                    nuovo = next(coda, None)
                    if nuovo:
                        futuri[ex.submit(uno, nuovo)] = nuovo
                break


if __name__ == "__main__":
    main()
