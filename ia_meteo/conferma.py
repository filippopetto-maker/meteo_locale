"""Fase 1 — conferma osservata dei periodi candidati: il CAPE e la convezione di ERA5 si confrontano
con quello che è successo davvero.

Per ogni periodo da 12 h (da selezione.py) e ogni riquadro 5°×5°:
- radar OPERA, un composito all'ora (fonti.opera): copertura, frazione dell'area coperta con eco
  ≥ 35 e ≥ 45 dBZ, eco massima. Statistiche sui pixel nativi del composito (2 km / 1 km);
- radar italiano IT-DPC-SRI sui riquadri che toccano l'Italia (OPERA non copre l'Italia): copertura,
  frazione con intensità ≥ 10 mm/h, massimo;
- fulmini MTG LI dal 04/07/2024: somma di flash×pixel nel periodo.
Etichetta del riquadro (soglie in testa al file):
- `confermato`        ERA5 convettivo e convezione osservata;
- `mancato_dal_modello` convezione osservata ma ERA5 non convettivo (evento vero, il modello lo perde);
- `falso_allarme`     ERA5 convettivo, area osservabile e nessuna convezione osservata;
- `negativo_vero`     ERA5 instabile senza convezione e osservazione pulita (il caso negativo buono);
- `non_confermabile`  area non coperta da radar né da LI.
Le statistiche vanno su HF nella sezione `conferme` (una voce per periodo), così si rifà la
selezione senza riscaricare il radar. Riprende dove si era fermato.

Uso:
    python conferma.py --da-selezione --n 600          # rosa larga da selezione.py, poi conferma
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import archivio as ar
from common import DOMAIN_BBOX, rm
from fonti import eumetsat as eu, itdpc, opera

PASSO_RADAR_H = 1
DBZ_CONV, DBZ_FORTE = 35.0, 45.0
FRAZ_ECO_CONV = 0.002          # ≥ 0,2% dell'area coperta con eco ≥ 35 dBZ (≈ 50 km² su un riquadro)
MMH_CONV = 10.0                # IT-DPC-SRI: intensità da cella convettiva
FRAZ_ITDPC_CONV = 0.002
FLASH_CONV = 50.0              # flash×pixel nel periodo (LI)
COPERTURA_MIN = 0.5
INIZIO_LI = datetime(2024, 7, 4)
LATO = 5.0
NX = int((DOMAIN_BBOX["lon_max"] - DOMAIN_BBOX["lon_min"]) / LATO)
NY = int((DOMAIN_BBOX["lat_max"] - DOMAIN_BBOX["lat_min"]) / LATO)


def nome_riquadro(i: int) -> str:
    la = DOMAIN_BBOX["lat_min"] + (i // NX) * LATO
    lo = DOMAIN_BBOX["lon_min"] + (i % NX) * LATO
    return f"{la:+05.1f}{lo:+06.1f}"


def indice_riquadro(lat, lon):
    """Indice 0..NX*NY-1 del riquadro di ogni punto; -1 fuori dominio."""
    iy = np.floor((lat - DOMAIN_BBOX["lat_min"]) / LATO).astype(int)
    ix = np.floor((lon - DOMAIN_BBOX["lon_min"]) / LATO).astype(int)
    ok = (iy >= 0) & (iy < NY) & (ix >= 0) & (ix < NX)
    return np.where(ok, iy * NX + ix, -1)


class _Geometrie:
    """Indici di riquadro per pixel, calcolati una volta per griglia radar (ODYSSEY, CIRRUS)."""
    def __init__(self):
        self.cache = {}

    def opera(self, o):
        k = o["raw"].shape
        if k not in self.cache:
            lon, lat = opera.lonlat(o)
            self.cache[k] = indice_riquadro(lat, lon).ravel()
        return self.cache[k]


def _conta(idx, maschera):
    v = idx[maschera & (idx >= 0)]
    return np.bincount(v, minlength=NX * NY)


def stat_opera(t0, t1, geo, lavoro):
    """Per riquadro: copertura media, frazione max (sulle ore) con eco ≥ 35/45 dBZ, eco max."""
    n = NX * NY
    cop, f35, f45, mx, ore = np.zeros(n), np.zeros(n), np.zeros(n), np.full(n, -np.inf), 0
    t = t0
    while t < t1:
        try:
            f, _ = opera.scarica_dbzh(t, lavoro)
            o = opera.leggi_odim(f)
            rm(f)
        except FileNotFoundError:
            t += timedelta(hours=PASSO_RADAR_H)
            continue
        idx = geo.opera(o)
        v = opera.dbz(o).ravel()
        tot = np.bincount(idx[idx >= 0], minlength=n)
        coperti = _conta(idx, np.isfinite(v))
        c35 = _conta(idx, v >= DBZ_CONV)
        c45 = _conta(idx, v >= DBZ_FORTE)
        with np.errstate(all="ignore"):
            cop += np.where(tot > 0, coperti / tot, 0)
            f35 = np.maximum(f35, np.where(coperti > 0, c35 / coperti, 0))
            f45 = np.maximum(f45, np.where(coperti > 0, c45 / coperti, 0))
        vv = np.where(np.isfinite(v) & (idx >= 0), v, -np.inf)
        np.maximum.at(mx, np.where(idx >= 0, idx, 0), vv)
        ore += 1
        t += timedelta(hours=PASSO_RADAR_H)
    return {"opera_ore": ore, "opera_copertura": cop / max(ore, 1), "opera_frac35": f35, "opera_frac45": f45,
            "opera_max_dbz": np.where(np.isfinite(mx), mx, np.nan)}


def stat_itdpc(t0, t1, ds_it, idx_it):
    n = NX * NY
    var = itdpc.variabile(ds_it)
    sub = ds_it[var].sel(time=slice(np.datetime64(t0), np.datetime64(t1 - timedelta(seconds=1))))
    sub = sub.isel(time=slice(None, None, max(1, int(round(3600 / _passo_s(sub))))))       # uno all'ora
    tot = np.bincount(idx_it[idx_it >= 0], minlength=n)
    cop, fr, mx, ore = np.zeros(n), np.zeros(n), np.full(n, -np.inf), 0
    for i in range(sub.sizes["time"]):
        v = sub.isel(time=i).values.ravel()
        coperti = _conta(idx_it, np.isfinite(v))
        forti = _conta(idx_it, v >= MMH_CONV)
        with np.errstate(all="ignore"):
            cop += np.where(tot > 0, coperti / tot, 0)
            fr = np.maximum(fr, np.where(coperti > 0, forti / coperti, 0))
        np.maximum.at(mx, np.where(idx_it >= 0, idx_it, 0), np.where(np.isfinite(v) & (idx_it >= 0), v, -np.inf))
        ore += 1
    return {"itdpc_ore": ore, "itdpc_copertura": cop / max(ore, 1), "itdpc_frac10": fr,
            "itdpc_max_mmh": np.where(np.isfinite(mx), mx, np.nan)}


def _passo_s(da):
    t = da.time.values
    return float((t[1] - t[0]) / np.timedelta64(1, "s")) if len(t) > 1 else 3600.0


def stat_li(t0, t1, lavoro, tok):
    n = NX * NY
    somma = np.zeros(n)
    for p in eu.cerca(eu.LI_AF, t0, t1, tok):
        f = eu.scarica(p, lavoro, eu.li_body(p))
        la, lo, w, _ = eu.li_flash(f)
        rm(f)
        idx = indice_riquadro(la, lo)
        somma += np.bincount(idx[idx >= 0], weights=w[idx >= 0], minlength=n)
    return {"li_flash": somma}


def etichetta(r) -> str:
    oss_conv = ((r.opera_copertura >= COPERTURA_MIN) & (r.opera_frac35 >= FRAZ_ECO_CONV)) \
        or ((r.itdpc_copertura >= COPERTURA_MIN) & (r.itdpc_frac10 >= FRAZ_ITDPC_CONV)) \
        or (r.li_flash >= FLASH_CONV)
    osservabile = (r.opera_copertura >= COPERTURA_MIN) or (r.itdpc_copertura >= COPERTURA_MIN) or r.li_disponibile
    if oss_conv:
        return "confermato" if r.era5_convettivo else "mancato_dal_modello"
    if not osservabile:
        return "non_confermabile"
    if r.era5_convettivo:
        return "falso_allarme"
    return "negativo_vero" if r.era5_negativo else "pulito"


def conferma_periodo(pid, t0, t1, conv, neg, geo, ds_it, idx_it, tok) -> pd.DataFrame:
    lavoro = ar.DATA / "lavoro" / f"conferma_{pid}"
    s = stat_opera(t0, t1, geo, lavoro)
    s |= stat_itdpc(t0, t1, ds_it, idx_it) if t0.year <= 2025 else {}
    li = t0 >= INIZIO_LI
    s |= stat_li(t0, t1, lavoro, tok) if li else {"li_flash": np.zeros(NX * NY)}
    rm(lavoro)
    df = pd.DataFrame({k: v for k, v in s.items() if isinstance(v, np.ndarray)})
    df.insert(0, "riquadro", [nome_riquadro(i) for i in range(NX * NY)])
    df.insert(0, "periodo", pid)
    df["opera_ore"], df["itdpc_ore"] = s.get("opera_ore", 0), s.get("itdpc_ore", 0)
    df["li_disponibile"] = li
    df["era5_convettivo"] = df.riquadro.isin(conv)
    df["era5_negativo"] = df.riquadro.isin(neg)
    df["etichetta"] = df.apply(etichetta, axis=1)
    return df


def conferma(periodi: pd.DataFrame) -> pd.DataFrame:
    """`periodi` con colonne id, inizio, fine, riquadri_convettivi, riquadri_negativi."""
    reg = ar.registro()
    geo, tok = _Geometrie(), eu.token()
    ds_it = itdpc.apri()
    idx_it = indice_riquadro(ds_it["lat"].values, ds_it["lon"].values).ravel()
    out = []
    for _, p in periodi.iterrows():
        if ar.presente("conferme", p.id, reg):
            f = ar.scarica("conferme", p.id)[0]
            out.append(pd.read_parquet(f))
            continue
        t = time.perf_counter()
        df = conferma_periodo(p.id, p.inizio.to_pydatetime(), p.fine.to_pydatetime(), set(p.riquadri_convettivi),
                              set(p.riquadri_negativi), geo, ds_it, idx_it, tok)
        f = ar.STAGING / "conferme" / f"{p.id}.parquet"
        f.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(f, index=False)
        ar.carica("conferme", p.id, {f"conferme/{f.name}": f},
                  {"inizio": str(p.inizio), "fine": str(p.fine),
                   "etichette": df.etichetta.value_counts().to_dict()})
        print(f"{p.id}: {df.etichetta.value_counts().to_dict()} ({time.perf_counter() - t:.0f} s)", flush=True)
        out.append(df)
    return pd.concat(out, ignore_index=True)


def main():
    import selezione as se
    ap = argparse.ArgumentParser()
    ap.add_argument("--da-selezione", action="store_true")
    ap.add_argument("--n", type=int, default=600)
    a = ap.parse_args()
    cand = se.scegli(se.periodi(se.classifica(se.carica_candidati())), a.n)
    tutto = conferma(cand)
    f = ar.DATA / "conferme.parquet"
    tutto.to_parquet(f, index=False)
    print(tutto.etichetta.value_counts().to_string())


if __name__ == "__main__":
    main()
