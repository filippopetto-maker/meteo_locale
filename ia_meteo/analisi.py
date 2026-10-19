"""Statistiche per riquadro 5° e per ora di ogni finestra già su HF (dati completi della finestra).

Servono a tre cose:
- controllare i negativi con tutti i dati (radar ogni 5–15 min, nubi fredde IR, fulmini), non solo con
  i 4 istanti della scansione radar;
- confermare la convezione dove il radar non vede (mare, Nord Africa) con l'IR di SEVIRI/FCI,
  calibrato sui fulmini LI (dal 2024);
- dare a eventi.py i campi sinottici per il regime (minimo di pressione sul mare, vento, T850).

Per ogni finestra: data/cache/analisi/<id>_riquadri.parquet (riquadro × ora) e <id>_sinottica.parquet
(ora). I file della finestra si scaricano, si leggono e si cancellano.

Uso:
    python analisi.py            # tutte le finestre su HF non ancora analizzate
"""
from __future__ import annotations

import shutil

import numpy as np
import pandas as pd

import archivio as ar
import zone

CARTELLA = ar.CACHE / "analisi"
TB_FREDDA = 220.0          # K: nubi convettive profonde (cime fredde)
TB_MOLTO_FREDDA = 210.0
SORGENTI = ["satellite", "radar", "radar_italia", "fulmini", "ambiente", "meta"]
# minimo di pressione: solo celle di mare del Mediterraneo (niente Atlantico e Biscaglia, niente basse termiche
# di Iberia e Sahara), profondità rispetto all'anello a 4–6° di distanza
MARE_MED = {"lat": (31.0, 46.0), "lon": (-5.5, 29.0)}       # un grado dal bordo del dominio
ANELLO_KM = (400.0, 650.0)


def _a_nord(ds):
    return ds.sortby("lat", ascending=False) if "lat" in ds.dims else ds


def _ore(tempi, t0):
    return ((pd.to_datetime(tempi) - t0) / pd.Timedelta("1h")).astype(int)


def _per_riquadro(arr3, ore, fn, riq):
    """arr3 (tempo, lat, lon) → {(riquadro, ora): fn(blocco tempo×celle)}."""
    out = {}
    for h in np.unique(ore):
        if h < 0 or h > 11:
            continue
        sub = arr3[ore == h]
        for k, (r, c) in riq.items():
            out[(k, int(h))] = fn(sub[:, r, c])
    return out


def _stat_satellite(ds, ore, riq):
    ir = next(v for v in ("IR_108", "ir_105") if v in ds)
    wv = next(v for v in ("WV_062", "wv_63") if v in ds)
    tb = ds[ir].values
    diff = ds[wv].values - tb                          # WV − IR > 0: cime sopra la tropopausa (overshooting)

    def f(soglia):
        return lambda b: float(np.nanmax(np.nanmean(b < soglia, axis=(1, 2)))) if np.isfinite(b).any() else np.nan
    a = _per_riquadro(tb, ore, f(TB_FREDDA), riq)
    b = _per_riquadro(tb, ore, f(TB_MOLTO_FREDDA), riq)
    c = _per_riquadro(diff, ore, lambda x: float(np.nanmax(np.nanmean(x > 0, axis=(1, 2)))) if np.isfinite(x).any() else np.nan, riq)
    m = _per_riquadro(tb, ore, lambda x: float(np.nanmin(x)) if np.isfinite(x).any() else np.nan, riq)
    return {k: {"ir_frac220": a[k], "ir_frac210": b[k], "ir_frac_wv_gt_ir": c[k], "ir_min": m[k]} for k in a}


def _stat_radar(ds, ore, riq):
    v = ds.dbz.values

    def f(x):
        ok = np.isfinite(x)
        cop = float(ok.mean())
        if not ok.any():
            return {"opera_cop": 0.0, "opera_f40": np.nan, "opera_f45": np.nan, "opera_max": np.nan}
        n = np.maximum(ok.sum(axis=(1, 2)), 1)
        return {"opera_cop": cop, "opera_f40": float(np.max((x >= 40).sum(axis=(1, 2)) / n)),
                "opera_f45": float(np.max((x >= 45).sum(axis=(1, 2)) / n)), "opera_max": float(np.nanmax(x))}
    return _per_riquadro(v, ore, f, riq)


def _stat_italia(ds, ore, riq):
    v = ds.pioggia_max.values

    def f(x):
        ok = np.isfinite(x)
        if not ok.any():
            return {"itdpc_cop": 0.0, "itdpc_f10": np.nan, "itdpc_max": np.nan}
        n = np.maximum(ok.sum(axis=(1, 2)), 1)
        return {"itdpc_cop": float(ok.mean()), "itdpc_f10": float(np.max((x >= 10).sum(axis=(1, 2)) / n)),
                "itdpc_max": float(np.nanmax(x))}
    return _per_riquadro(v, ore, f, riq)


def _stat_fulmini(ds, ore, riq):
    v = ds.flash.values
    return _per_riquadro(v, ore, lambda x: {"li_flash": float(np.nansum(x)), "li_celle": int((np.nan_to_num(x) > 0).any(axis=0).sum())}, riq)


def _stat_ambiente(ds, t0):
    """ML-CAPE p90 per riquadro e ora (griglia 0,25°) e campi sinottici per ora."""
    lat, lon = ds.latitude.values, ds.longitude.values
    LO, LA = np.meshgrid(lon, lat)
    terra = zone.carica()["terra"]
    # terra a 0,25°: media delle celle 0,05° vicine
    ii = np.clip(np.rint((50 - zone.PASSO / 2 - LA) / zone.PASSO).astype(int), 0, zone.NY - 1)
    jj = np.clip(np.rint((LO - (-10 + zone.PASSO / 2)) / zone.PASSO).astype(int), 0, zone.NX - 1)
    mare = ~terra[ii, jj]
    med = mare & (LA >= MARE_MED["lat"][0]) & (LA <= MARE_MED["lat"][1]) & (LO >= MARE_MED["lon"][0]) & (LO <= MARE_MED["lon"][1])
    med &= ~((LA > 43.0) & (LO < 0.0))                                   # Biscaglia
    riq, sin = {}, []
    ore = _ore(ds.time.values, t0)
    for i, h in enumerate(ore):
        if h < 0 or h > 11:
            continue
        s = ds.isel(time=i)
        cape = s.mlcape.values
        for la0 in (30, 35, 40, 45):
            for lo0 in range(-10, 30, 5):
                m = (LA >= la0) & (LA < la0 + 5) & (LO >= lo0) & (LO < lo0 + 5)
                riq[(f"{la0:+05.1f}{lo0:+06.1f}", int(h))] = {"mlcape_p90": float(np.nanpercentile(cape[m], 90))}
        msl = s.msl.values / 100
        mm = np.where(med, msl, np.inf)
        k = np.unravel_index(np.argmin(mm), mm.shape)
        la_m, lo_m = float(LA[k]), float(LO[k])
        dist = 6371 * np.hypot(np.radians(LA - la_m), np.radians(LO - lo_m) * np.cos(np.radians(la_m)))
        anello = (dist >= ANELLO_KM[0]) & (dist <= ANELLO_KM[1])
        p500 = list(s.pressure_level.values).index(500.0)
        p850 = list(s.pressure_level.values).index(850.0)
        vic = dist <= 300
        sin.append({"ora": int(h), "msl_min": float(msl[k]), "msl_min_lat": la_m, "msl_min_lon": lo_m,
                    "msl_profondita": float(np.nanmean(msl[anello]) - msl[k]),
                    "vento10_max_vicino": float(np.nanmax(np.hypot(s.u10.values, s.v10.values)[vic])),
                    "t850_vicino": float(np.nanmean(s.t.values[p850][vic]) - 273.15)})
        # vento a 500 hPa e T850 per riquadro (per il plume: flusso da sud-ovest e aria calda)
        for la0 in (30, 35, 40, 45):
            for lo0 in range(-10, 30, 5):
                m = (LA >= la0) & (LA < la0 + 5) & (LO >= lo0) & (LO < lo0 + 5)
                riq[(f"{la0:+05.1f}{lo0:+06.1f}", int(h))] |= {
                    "u500": float(np.nanmean(s.u.values[p500][m])), "v500": float(np.nanmean(s.v.values[p500][m])),
                    "t850": float(np.nanmean(s.t.values[p850][m]) - 273.15)}
    return riq, pd.DataFrame(sin)


def analizza(fid: str, voce: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    import xarray as xr
    CARTELLA.mkdir(parents=True, exist_ok=True)
    t0 = pd.Timestamp(voce["inizio"])
    d = ar.scarica_finestra(fid, [s for s in SORGENTI if s in voce["sorgenti"] or s == "meta"])
    riq = zone.riquadri()
    righe: dict = {}

    def unisci(st):
        for k, v in st.items():
            righe.setdefault(k, {}).update(v)
    try:
        for nome, fn in (("satellite", _stat_satellite), ("radar", _stat_radar), ("radar_italia", _stat_italia),
                         ("fulmini", _stat_fulmini)):
            f = d / f"{nome}.nc"
            if f.exists():
                with xr.open_dataset(f) as ds:
                    ds = _a_nord(ds.load())
                    unisci(fn(ds, _ore(ds.time.values, t0), riq))
        sin = pd.DataFrame()
        if (d / "ambiente.nc").exists():
            with xr.open_dataset(d / "ambiente.nc") as ds:
                r, sin = _stat_ambiente(ds.load(), t0)
                unisci(r)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    tab = pd.DataFrame([{"riquadro": k[0], "ora": k[1]} | v for k, v in righe.items()])
    tab.insert(0, "id", fid)
    z = zone.carica()
    for k, (r, c) in riq.items():
        tab.loc[tab.riquadro == k, "terra"] = float(z["terra"][r, c].mean())
        tab.loc[tab.riquadro == k, "italia"] = float(z["italia"][r, c].mean())
        tab.loc[tab.riquadro == k, "balcani"] = float(z["balcani"][r, c].mean())
    sin.insert(0, "id", fid)
    tab.to_parquet(CARTELLA / f"{fid}_riquadri.parquet", index=False)
    sin.to_parquet(CARTELLA / f"{fid}_sinottica.parquet", index=False)
    return tab, sin


def tutte(forza: bool = False) -> int:
    reg = ar.registro()["finestre"]
    n = 0
    for fid, voce in sorted(reg.items()):
        if not forza and (CARTELLA / f"{fid}_sinottica.parquet").exists():
            continue
        try:
            analizza(fid, voce)
            n += 1
            print(f"{fid}: analizzata", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"{fid}: ERRORE {e!r:.200}", flush=True)
    return n


def carica() -> tuple[pd.DataFrame, pd.DataFrame]:
    r = [pd.read_parquet(f) for f in sorted(CARTELLA.glob("*_riquadri.parquet"))]
    s = [pd.read_parquet(f) for f in sorted(CARTELLA.glob("*_sinottica.parquet"))]
    return pd.concat(r, ignore_index=True), pd.concat(s, ignore_index=True)


LI_CELLE_CONV = 5          # riquadro-ora con fulmini su ≥ 5 celle 0,05°: convezione (i falsi LI sono isolati)


def calibra_ir(riq: pd.DataFrame, soglie=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.02)) -> pd.DataFrame:
    """IR contro fulmini sui riquadri-ora con LI: per ogni indicatore e soglia, POD, FAR e CSI.
    Serve a scegliere il criterio IR per confermare la convezione dove il radar non vede."""
    d = riq.dropna(subset=["li_celle", "ir_frac_wv_gt_ir"])
    vero = d.li_celle >= LI_CELLE_CONV
    righe = []
    for ind in ("ir_frac_wv_gt_ir", "ir_frac210", "ir_frac220"):
        for s in soglie:
            p = d[ind] >= s
            a, b, c = int((p & vero).sum()), int((p & ~vero).sum()), int((~p & vero).sum())
            righe.append({"indicatore": ind, "soglia": s, "pod": a / max(a + c, 1), "far": b / max(a + b, 1),
                          "csi": a / max(a + b + c, 1), "n_veri": int(vero.sum()), "n": len(d)})
    return pd.DataFrame(righe)


def controlla_negativi(riq: pd.DataFrame) -> pd.DataFrame:
    """Riquadri dichiarati negativi (rosa_negativa) rivisti con tutti i dati della finestra: eco massima su
    ogni immagine radar, nubi fredde e cime sopra la tropopausa, fulmini."""
    neg = pd.read_parquet(ar.DATA / "rosa_negativa.parquet")
    e = neg.explode("riquadri_negativi_oss").dropna(subset=["riquadri_negativi_oss"])
    e = e[["id", "riquadri_negativi_oss"]].rename(columns={"riquadri_negativi_oss": "riquadro"})
    d = riq.merge(e, on=["id", "riquadro"])
    if d.empty:
        return d
    g = d.groupby(["id", "riquadro"]).agg(opera_max=("opera_max", "max"), opera_f40=("opera_f40", "max"),
                                          itdpc_max=("itdpc_max", "max"), ir_frac220=("ir_frac220", "max"),
                                          ir_wv=("ir_frac_wv_gt_ir", "max"), li_celle=("li_celle", "max"),
                                          mlcape=("mlcape_p90", "max")).reset_index()
    g["eco40"] = (g.opera_max >= 40) | (g.itdpc_max >= 10)
    g["nubi_fredde"] = g.ir_frac220 >= 0.01
    g["cime_overshoot"] = g.ir_wv >= 0.001
    g["fulmini"] = g.li_celle >= LI_CELLE_CONV
    g["pulito"] = ~(g.eco40 | g.cime_overshoot | g.fulmini)
    return g


if __name__ == "__main__":
    print(f"analizzate {tutte()} finestre")
