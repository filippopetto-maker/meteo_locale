"""Fase 1 — conferma osservata dei periodi candidati: il CAPE e la convezione di ERA5 si confrontano
con quello che è successo davvero.

Per ogni periodo da 12 h (da selezione.py) e ogni riquadro 5°×5°:
- radar OPERA, un composito all'ora (fonti.opera): copertura, frazione dell'area coperta con eco
  ≥ 35, ≥ 45 e ≥ 55 dBZ (grandine probabile), eco massima. Statistiche sui pixel nativi del
  composito (2 km / 1 km); i pixel corrotti (> 80 dBZ, inf) sono esclusi in fonti.opera.dbz;
- radar italiano IT-DPC-SRI sui riquadri che toccano l'Italia (OPERA non copre l'Italia): copertura,
  frazione con intensità ≥ 10 e ≥ 50 mm/h, massimo (il massimo è rumoroso: clutter fino a 300 mm/h);
- fulmini MTG LI dal 04/07/2024: somma di flash×pixel nel periodo.
Etichetta del riquadro (soglie in testa al file):
- `confermato`        ERA5 convettivo e convezione osservata;
- `mancato_dal_modello` convezione osservata ma ERA5 non convettivo (evento vero, il modello lo perde);
- `falso_allarme`     ERA5 convettivo, area osservabile e nessuna convezione osservata;
  (la convezione vista anche solo in parte del riquadro conferma; per dire "pulito" serve copertura)
- `negativo_vero`     ERA5 instabile senza convezione e osservazione pulita (il caso negativo buono);
- `non_confermabile`  area non coperta da radar né da LI.
Le statistiche vanno su HF nella sezione `conferme` (una voce per periodo), così si rifà la
selezione senza riscaricare il radar. Riprende dove si era fermato.

Uso:
    python conferma.py --da-selezione                  # rosa larga di selezione.py (N_ROSA), poi conferma
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
DBZ_CONV, DBZ_FORTE, DBZ_GRANDINE = 35.0, 45.0, 55.0
FRAZ_ECO_CONV = 0.002          # ≥ 0,2% dell'area coperta con eco ≥ 35 dBZ (≈ 50 km² su un riquadro)
MMH_CONV = 10.0                # IT-DPC-SRI: intensità da cella convettiva
MMH_FORTE = 50.0
FRAZ_ITDPC_CONV = 0.002
FLASH_CONV = 50.0              # flash×pixel nel periodo (LI)
COPERTURA_MIN = 0.5            # per dichiarare un riquadro pulito (OPERA + IT-DPC-SRI)
COPERTURA_VISTA = 0.05         # basta che il radar veda una parte del riquadro per confermare la convezione
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


FLUSSI_OPERA = 4               # download in parallelo (prova 18/10/2026: 12 ore in 5 s invece di 34)
FLUSSI_LI = 6                  # oltre 6–8 la linea è satura (72 prodotti in 44 s invece di 160)


def _riprova(fun, *a, tentativi=3):
    for k in range(tentativi):
        try:
            return fun(*a)
        except FileNotFoundError:
            raise
        except Exception:  # noqa: BLE001
            if k == tentativi - 1:
                raise
            time.sleep(10 * (k + 1))


def _conta_opera(t, geo, lavoro):
    """Conteggi per riquadro di un composito orario; None se manca."""
    n = NX * NY
    try:
        f, _ = _riprova(opera.scarica_dbzh, t, lavoro)
    except FileNotFoundError:
        return None
    o = opera.leggi_odim(f)
    rm(f)
    idx = geo.opera(o)
    v = opera.dbz(o).ravel()
    mx = np.full(n, -np.inf)
    np.maximum.at(mx, np.where(idx >= 0, idx, 0), np.where(np.isfinite(v) & (idx >= 0), v, -np.inf))
    return {"tot": np.bincount(idx[idx >= 0], minlength=n), "coperti": _conta(idx, np.isfinite(v)),
            "c35": _conta(idx, v >= DBZ_CONV), "c45": _conta(idx, v >= DBZ_FORTE),
            "c55": _conta(idx, v >= DBZ_GRANDINE), "mx": mx}


def stat_opera(t0, t1, geo, lavoro, flussi=FLUSSI_OPERA):
    """Per riquadro: copertura media, frazione max (sulle ore) con eco ≥ 35/45/55 dBZ, eco max."""
    from concurrent.futures import ThreadPoolExecutor
    n = NX * NY
    ore_t = []
    t = t0
    while t < t1:
        ore_t.append(t)
        t += timedelta(hours=PASSO_RADAR_H)
    with ThreadPoolExecutor(flussi) as ex:
        conteggi = [c for c in ex.map(lambda x: _conta_opera(x, geo, lavoro), ore_t) if c is not None]
    cop, f35, f45, f55, mx = np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n), np.full(n, -np.inf)
    for c in conteggi:
        with np.errstate(all="ignore"):
            cop += np.where(c["tot"] > 0, c["coperti"] / c["tot"], 0)
            f35 = np.maximum(f35, np.where(c["coperti"] > 0, c["c35"] / c["coperti"], 0))
            f45 = np.maximum(f45, np.where(c["coperti"] > 0, c["c45"] / c["coperti"], 0))
            f55 = np.maximum(f55, np.where(c["coperti"] > 0, c["c55"] / c["coperti"], 0))
        mx = np.maximum(mx, c["mx"])
    ore = len(conteggi)
    return {"opera_ore": ore, "opera_copertura": cop / max(ore, 1), "opera_frac35": f35, "opera_frac45": f45,
            "opera_frac55": f55, "opera_max_dbz": np.where(np.isfinite(mx), mx, np.nan)}


def stat_itdpc(t0, t1, ds_it, idx_it):
    n = NX * NY
    var = itdpc.variabile(ds_it)
    sub = ds_it[var].sel(time=slice(np.datetime64(t0), np.datetime64(t1 - timedelta(seconds=1))))
    sub = sub.isel(time=slice(None, None, max(1, int(round(3600 / _passo_s(sub))))))       # uno all'ora
    tot = np.bincount(idx_it[idx_it >= 0], minlength=n)
    cop, fr, fr50, mx, ore = np.zeros(n), np.zeros(n), np.zeros(n), np.full(n, -np.inf), 0
    for i in range(sub.sizes["time"]):
        v = sub.isel(time=i).values.ravel()
        coperti = _conta(idx_it, np.isfinite(v))
        forti = _conta(idx_it, v >= MMH_CONV)
        fortissimi = _conta(idx_it, v >= MMH_FORTE)
        with np.errstate(all="ignore"):
            cop += np.where(tot > 0, coperti / tot, 0)
            fr = np.maximum(fr, np.where(coperti > 0, forti / coperti, 0))
            fr50 = np.maximum(fr50, np.where(coperti > 0, fortissimi / coperti, 0))
        np.maximum.at(mx, np.where(idx_it >= 0, idx_it, 0), np.where(np.isfinite(v) & (idx_it >= 0), v, -np.inf))
        ore += 1
    return {"itdpc_ore": ore, "itdpc_copertura": cop / max(ore, 1), "itdpc_frac10": fr, "itdpc_frac50": fr50,
            "itdpc_max_mmh": np.where(np.isfinite(mx), mx, np.nan)}


def _passo_s(da):
    t = da.time.values
    return float((t[1] - t[0]) / np.timedelta64(1, "s")) if len(t) > 1 else 3600.0


def stat_li(t0, t1, lavoro, tok, flussi=FLUSSI_LI):
    from concurrent.futures import ThreadPoolExecutor
    n = NX * NY

    def uno(p):
        corpo = eu.li_body(p)
        if corpo is None:                       # prodotto parziale senza dati
            return np.zeros(n)
        f = _riprova(eu.scarica, p, lavoro, corpo)
        la, lo, w, _ = eu.li_flash(f)
        rm(f)
        idx = indice_riquadro(la, lo)
        return np.bincount(idx[idx >= 0], weights=w[idx >= 0], minlength=n)

    prodotti = list(eu.cerca(eu.LI_AF, t0, t1, tok))
    with ThreadPoolExecutor(flussi) as ex:
        parti = list(ex.map(uno, prodotti))
    return {"li_flash": np.sum(parti, axis=0) if parti else np.zeros(n)}


def etichetta(r) -> str:
    """La convezione vista nella parte coperta basta a confermarla; per dichiarare un riquadro
    pulito serve invece copertura sufficiente (OPERA + IT-DPC-SRI insieme) o i fulmini LI."""
    oss_conv = ((r.opera_copertura >= COPERTURA_VISTA) & (r.opera_frac35 >= FRAZ_ECO_CONV)) \
        or ((r.itdpc_copertura >= COPERTURA_VISTA) & (r.itdpc_frac10 >= FRAZ_ITDPC_CONV)) \
        or (r.li_flash >= FLASH_CONV)
    osservabile = min(r.opera_copertura + r.itdpc_copertura, 1.0) >= COPERTURA_MIN or r.li_disponibile
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


TENTATIVI = 3                 # un periodo che fallisce (rete, server) torna in fondo alla coda


def _risorse():
    geo, tok = _Geometrie(), eu.token()
    ds_it = itdpc.apri()
    idx_it = indice_riquadro(ds_it["lat"].values, ds_it["lon"].values).ravel()
    return geo, tok, ds_it, idx_it


def _salva(sezione_chiave: str, df: pd.DataFrame, voce: dict) -> str:
    f = ar.STAGING / "conferme" / f"{sezione_chiave}.parquet"
    f.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(f, index=False)
    return ar.carica_o_parcheggia("conferme", sezione_chiave, {f"conferme/{f.name}": f}, voce)


def _fatto(chiave: str, reg: dict) -> bool:
    return ar.presente("conferme", chiave, reg) or ("conferme", chiave) in ar.parcheggiati("conferme")


def _in_coda(lavori, esegui, nome):
    """Esegue `esegui(x)` su ogni lavoro; gli errori rimettono il lavoro in fondo (TENTATIVI volte).
    Prima di ogni lavoro prova a svuotare il parcheggio e rispetta il suo tetto di 5 GB."""
    coda, tentativi = list(lavori), {}
    while coda:
        x = coda.pop(0)
        ar.attendi_parcheggio()
        try:
            esegui(x)
        except ar.BudgetSuperato:
            raise
        except Exception as e:  # noqa: BLE001
            k = nome(x)
            tentativi[k] = tentativi.get(k, 0) + 1
            print(f"{k}: ERRORE {e!r:.300}", flush=True)
            if tentativi[k] < TENTATIVI:
                print(f"{k}: riprovo in fondo alla coda", flush=True)
                coda.append(x)
                time.sleep(60)
    while ar.parcheggiati():                              # fine lista: ultimo lotto (o si aspetta HF)
        if ar.svuota_parcheggio(forza=True) == 0 and ar.parcheggiati():
            print(f"{len(ar.parcheggiati())} pezzi in attesa: riprovo HF fra 10 minuti", flush=True)
            time.sleep(600)


def conferma(periodi: pd.DataFrame, raccogli: bool = True) -> pd.DataFrame | None:
    """`periodi` con colonne id, inizio, fine, riquadri_convettivi, riquadri_negativi. Salta i periodi
    già su HF (o parcheggiati); con `raccogli` rilegge tutto e lo restituisce."""
    reg = ar.registro()
    da_fare = [p for _, p in periodi.iterrows() if not _fatto(p.id, reg)]
    print(f"{len(periodi)} periodi, {len(periodi) - len(da_fare)} già confermati, {len(da_fare)} da fare", flush=True)
    if da_fare:
        geo, tok, ds_it, idx_it = _risorse()

        def uno(p):
            t = time.perf_counter()
            df = conferma_periodo(p.id, p.inizio.to_pydatetime(), p.fine.to_pydatetime(),
                                  set(p.riquadri_convettivi), set(p.riquadri_negativi), geo, ds_it, idx_it, tok)
            esito = _salva(p.id, df, {"inizio": str(p.inizio), "fine": str(p.fine),
                                      "etichette": df.etichetta.value_counts().to_dict()})
            print(f"{p.id}: {df.etichetta.value_counts().to_dict()} ({time.perf_counter() - t:.0f} s, {esito})",
                  flush=True)

        _in_coda(da_fare, uno, lambda p: p.id)
    if not raccogli:
        return None
    out = []
    for _, p in periodi.iterrows():
        df = pd.read_parquet(ar.scarica("conferme", p.id)[0])
        df["etichetta"] = df.apply(etichetta, axis=1)          # regole aggiornate senza riscaricare il radar
        out.append(df)
    return pd.concat(out, ignore_index=True)


# ---------------------------------------------------------------- scansione radar di tutti i periodi
PASSO_SCANSIONE_H = 3


def scansiona_mese(anno: int, mese: int, geo, ds_it, idx_it) -> pd.DataFrame:
    """Radar a ogni istante a 3 h del mese (non solo nella rosa ERA5): una riga per istante e riquadro
    con le stesse misure della conferma. Serve a trovare i temporali piccoli e locali che ERA5 non vede
    (i "mancati dal modello" fuori dalla rosa). Senza LI: costerebbe ~25 GB al mese."""
    import calendar
    lavoro = ar.DATA / "lavoro" / f"scansione_{anno:04d}_{mese:02d}"
    righe = []
    t = datetime(anno, mese, 1)
    fine = t + timedelta(days=calendar.monthrange(anno, mese)[1])
    istanti = []
    while t < fine:
        istanti.append(t)
        t += timedelta(hours=PASSO_SCANSIONE_H)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(FLUSSI_OPERA) as ex:          # un istante = un composito: parallelo sugli istanti
        radar = list(ex.map(lambda x: stat_opera(x, x + timedelta(hours=1), geo, lavoro, flussi=1), istanti))
    for t, s in zip(istanti, radar):
        s = dict(s)
        s |= stat_itdpc(t, t + timedelta(hours=1), ds_it, idx_it) if t.year <= 2025 else {}
        df = pd.DataFrame({k: v for k, v in s.items() if isinstance(v, np.ndarray)})
        df.insert(0, "riquadro", [nome_riquadro(i) for i in range(NX * NY)])
        df.insert(0, "time", pd.Timestamp(t))
        df["opera_ore"], df["itdpc_ore"] = s.get("opera_ore", 0), s.get("itdpc_ore", 0)
        righe.append(df)
    rm(lavoro)
    out = pd.concat(righe, ignore_index=True)
    num = out.select_dtypes("float64").columns
    out[num] = out[num].astype("float32")
    return out


def scansione(mesi):
    reg = ar.registro()
    da_fare = [k for k in mesi if not _fatto(f"scansione_{k[0]:04d}_{k[1]:02d}", reg)]
    print(f"scansione: {len(mesi)} mesi, {len(mesi) - len(da_fare)} già fatti, {len(da_fare)} da fare", flush=True)
    if not da_fare:
        return
    geo, _, ds_it, idx_it = _risorse()

    def uno(k):
        t = time.perf_counter()
        chiave = f"scansione_{k[0]:04d}_{k[1]:02d}"
        df = scansiona_mese(*k, geo, ds_it, idx_it)
        conv = int(((df.opera_frac35 >= FRAZ_ECO_CONV) | (df.get("itdpc_frac10", 0) >= FRAZ_ITDPC_CONV)).sum())
        esito = _salva(chiave, df, {"anno": k[0], "mese": k[1], "istanti": int(df.time.nunique()),
                                    "riquadri_convettivi_radar": conv})
        print(f"{chiave}: {df.time.nunique()} istanti, {conv} riquadri-istante convettivi "
              f"({time.perf_counter() - t:.0f} s, {esito})", flush=True)

    _in_coda(da_fare, uno, lambda k: f"scansione_{k[0]:04d}_{k[1]:02d}")


def main():
    import selezione as se
    from catalogo import mesi as elenco_mesi
    ap = argparse.ArgumentParser()
    ap.add_argument("--da-selezione", action="store_true", help="conferma la rosa larga di selezione.py")
    ap.add_argument("--n", type=int, default=se.N_ROSA)
    ap.add_argument("--scansione", action="store_true", help="radar ogni 3 h su tutti i mesi del catalogo")
    ap.add_argument("--da", default="2015-04")
    ap.add_argument("--a", default="2025-11")
    ap.add_argument("--al-contrario", action="store_true")
    a = ap.parse_args()
    if a.scansione:
        m = list(elenco_mesi(a.da, a.a))
        scansione(m[::-1] if a.al_contrario else m)
        return
    rosa = ar.DATA / "rosa_larga.parquet"
    if rosa.exists() and a.n == se.N_ROSA:
        cand = pd.read_parquet(rosa)
    else:
        cand = se.scegli(se.periodi(se.classifica(se.carica_candidati())), a.n, se.MAX_PER_MESE_ROSA)
    conferma(cand, raccogli=False)
    print("conferma finita", flush=True)


if __name__ == "__main__":
    main()
