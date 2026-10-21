"""
experiment_m2_bias.py — Perché M2 temperatura ha un errore sistematico freddo (−0,3 °C)?

In tutti i test fuori campione M2 sbaglia in difetto di circa 0,25–0,35 °C, uniformemente su
ore e settimane, mentre E (IFS − bias staz×ora) è senza errore sistematico. Aggiungere dati
recenti, pesarli o addestrare sopra E non lo toglie (experiment_m2_refresh.py). Qui:

  diagnostica   offset IFS−Netatmo per mese e anno, offset di E, errore sistematico in-sample
  varianti      V0  M2 attuale (riferimento)
                V1  senza feature stagionali (doy_sin, doy_cos, month): con 2,5 cicli stagionali
                    possono memorizzare l'anomalia degli anni passati
                V2  con più feature di bias (7 giorni per ora, 90 giorni per ora, media 30 giorni
                    su tutte le ore, ieri, ultimi 3 giorni)
                V3  V1 + V2
                V4  V3 con obiettivo L1 (mediana invece di media)
  correzione    +C  M2 meno l'errore medio di M2 stesso per stazione × ora sui giorni precedenti
                    (solo errori fuori campione, finestra --self-days, minimo --self-min giorni)

Due finestre fuori campione:
  W1  addestramento fino al 21/09/2026, test 01/10 → fine dati (la deriva di ottobre)
  W2  addestramento fino al 31/10/2025, test 25/06 → 21/09/2026 (l'estate, senza dati 2026)

Utilizzo:
    python3 scripts/experiment_m2_bias.py --cache /percorso/frames.parquet
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "model"))

import backtest_ifs_lead as bt  # noqa: E402
import db  # noqa: E402
import experiment_retrain_netatmo as ex  # noqa: E402
import inference as inf  # noqa: E402
from forecast import DEFAULT_LGB_PARAMS  # noqa: E402

logger = logging.getLogger("m2_bias")

REFERENCE_MODEL = ROOT / "logs" / "retrain_netatmo" / "lgbm_m2.txt"
OUT_DIR = ROOT / "logs" / "retrain_netatmo_refresh"
SEASONAL = ["doy_sin", "doy_cos", "month"]
EXTRA = ["bias_h7", "bias_h90", "bias_all30", "bias_d1", "bias_d3"]


def extra_bias(nwp: pd.DataFrame, target: pd.DataFrame, sid: int, valid_for: pd.Series) -> pd.DataFrame:
    """Altri riassunti del bias IFS−Netatmo, tutti calcolati sui giorni precedenti a quello di valid_for."""
    nwp_t = nwp[["recorded_at", "temperature"]].copy()
    nwp_t["recorded_at"] = pd.to_datetime(nwp_t.recorded_at).dt.tz_localize("UTC")
    obs = target[target.station_id == sid][["recorded_at", "temperature"]]
    both = nwp_t.merge(obs, on="recorded_at", suffixes=("_nwp", "_obs")).dropna()
    e = pd.DataFrame({"e": (both.temperature_nwp - both.temperature_obs).to_numpy()},
                     index=pd.DatetimeIndex(both.recorded_at))
    e["date"], e["hour"] = e.index.floor("D"), e.index.hour
    piv = e.pivot_table(index="date", columns="hour", values="e").asfreq("D")
    daily = piv.mean(axis=1)

    idx = pd.MultiIndex.from_arrays([valid_for.dt.floor("D"), valid_for.dt.hour])
    day = valid_for.dt.floor("D")
    out = pd.DataFrame(index=valid_for.index)
    out["bias_h7"] = piv.rolling(7, min_periods=4).mean().shift(1).stack().reindex(idx).to_numpy()
    out["bias_h90"] = piv.rolling(90, min_periods=30).mean().shift(1).stack().reindex(idx).to_numpy()
    out["bias_all30"] = daily.rolling(30, min_periods=10).mean().shift(1).reindex(day).to_numpy()
    out["bias_d1"] = daily.shift(1).reindex(day).to_numpy()
    out["bias_d3"] = daily.rolling(3, min_periods=2).mean().shift(1).reindex(day).to_numpy()
    return out


def build(args) -> pd.DataFrame:
    cache = Path(args.cache)
    if cache.exists():
        logger.info(f"Frame dalla cache {cache}")
        return pd.read_parquet(cache)
    target = pd.read_parquet(ex.TARGET_FILE)
    target["recorded_at"] = pd.to_datetime(target.recorded_at, utc=True)
    stations = sorted(db.get_active_stations(), key=lambda s: s["id"])
    stations = [s for s in stations if s["id"] in set(target.station_id) and s["id"] not in ex.EXCLUDED_STATIONS]
    feature_cols = inf.load_lgbm("temperature").feature_name()
    api = bt.OpenMeteo(ROOT / "logs" / "backtest" / "cache", args.pause)
    start = pd.Timestamp(args.since, tz="UTC").to_pydatetime() - timedelta(days=2)
    end = pd.Timestamp(args.data_end, tz="UTC").to_pydatetime()
    frames = []
    for st in stations:
        nwp = bt.fetch_historical(api, [st], start, end)[st["id"]]
        f = ex.station_frame(st, nwp, target, feature_cols)
        frames.append(pd.concat([f, extra_bias(nwp, target, st["id"], f.valid_for)], axis=1))
    data = pd.concat(frames, ignore_index=True)
    data = data[data.valid_for >= pd.Timestamp(args.since, tz="UTC")].dropna(subset=["y", "nwp_valid"])
    data = data.reset_index(drop=True)
    data.to_parquet(cache)
    logger.info(f"Frame costruiti e salvati in {cache}: {len(data):,} righe, chiamate API nuove {api.calls}")
    return data


def diagnostics(data: pd.DataFrame) -> None:
    d = data.copy()
    d["ym"] = d.valid_for.dt.strftime("%Y-%m")
    d["off"] = d.y - d.nwp_valid                                  # osservato − IFS
    d["e_err"] = d.y - (d.nwp_valid - d.bias_st_hour)             # osservato − E
    g = d.groupby("ym").agg(n=("y", "size"), ifs_off=("off", "mean"), bias_feat=("bias_st_hour", "mean"),
                            e_err=("e_err", "mean"))
    print("\nOffset osservato−IFS per mese (tutte le stazioni, tutte le ore) e errore medio di E (osservato−E):")
    print(g.round(2).T.to_string())
    d["mon"] = d.valid_for.dt.month
    d["yr"] = d.valid_for.dt.year
    print("\nOffset osservato−IFS, ottobre per anno / giugno-settembre per anno:")
    print(d[d.mon == 10].groupby("yr").off.mean().round(2).to_dict(), d[d.mon.between(6, 9)].groupby("yr").off.mean().round(2).to_dict())
    print("\nErrore di E (osservato−E) per anno, stesse stagioni:")
    print(d[d.mon == 10].groupby("yr").e_err.mean().round(2).to_dict(), d[d.mon.between(6, 9)].groupby("yr").e_err.mean().round(2).to_dict())


def fit(df: pd.DataFrame, cols: list[str], n_iter: int, objective: str | None = None) -> lgb.Booster:
    params = {**DEFAULT_LGB_PARAMS, "seed": 42}
    if objective:
        params["objective"] = objective
    return lgb.train(params, lgb.Dataset(df[cols], label=df.resid, categorical_feature=["station_cat"]),
                     num_boost_round=n_iter)


def self_corrected(test: pd.DataFrame, pred: np.ndarray, days: int, min_days: int) -> np.ndarray:
    """pred − media (pred − y) per stazione × ora sui giorni precedenti, solo dentro la finestra di test."""
    t = pd.DataFrame({"sid": test.station_cat.to_numpy(), "d": test.valid_for.dt.floor("D"),
                      "h": test.valid_for.dt.hour, "err": pred - test.y.to_numpy()})
    out = np.full(len(t), np.nan)
    for sid, g in t.groupby("sid"):
        piv = g.pivot_table(index="d", columns="h", values="err").asfreq("D")
        roll = piv.rolling(days, min_periods=min_days).mean().shift(1).stack()
        idx = pd.MultiIndex.from_arrays([g.d, g.h])
        out[g.index.to_numpy()] = pred[g.index.to_numpy()] - roll.reindex(idx).to_numpy()
    return out


def row(name: str, window: str, test: pd.DataFrame, pred: np.ndarray, mask: np.ndarray | None = None) -> dict:
    ok = ~np.isnan(pred) if mask is None else (mask & ~np.isnan(pred))
    err = pred[ok] - test.y.to_numpy()[ok]
    t = test[ok]
    by_h = pd.Series(err).groupby(t.valid_for.dt.hour.to_numpy()).mean()
    by_s = pd.Series(err).groupby(t.station_cat.to_numpy()).mean()
    return {"finestra": window, "variante": name, "n": int(ok.sum()), "mae": np.abs(err).mean(), "bias": err.mean(),
            "bias_ore": f"{by_h.min():+.2f}/{by_h.max():+.2f}", "bias_staz_std": by_s.std(),
            "staz_bias_<-0.5": int((by_s < -0.5).sum())}


def run_window(data: pd.DataFrame, window: str, cut: str, test_start: str, test_end: str | None,
               n_iter: int, feats: list[str], args) -> list[dict]:
    cut_t, ts = pd.Timestamp(cut, tz="UTC"), pd.Timestamp(test_start, tz="UTC")
    te = pd.Timestamp(test_end, tz="UTC") if test_end else data.valid_for.max() + pd.Timedelta(hours=1)
    train = data[data.valid_for < cut_t]
    # M2 viene valutato dal giorno dopo l'addestramento, così le correzioni usano errori fuori campione
    full = data[(data.valid_for >= cut_t) & (data.valid_for < te)].reset_index(drop=True)
    eval_mask = (full.valid_for >= ts).to_numpy()
    logger.info(f"{window}: train {len(train):,} righe, test completo {len(full):,}, valutato da {ts:%d/%m} ({eval_mask.sum():,})")

    base_cols = [*feats, "station_cat", "nwp_valid", "bias_st_hour"]
    v1_cols = [c for c in base_cols if c not in SEASONAL]
    v2_cols = [*base_cols, *EXTRA]
    v3_cols = [*v1_cols, *EXTRA]
    rows = []
    e_pred = (full.nwp_valid - full.bias_st_hour).to_numpy()
    rows.append(row("E (riferimento)", window, full, e_pred, eval_mask))
    best_pred = {}
    for name, cols, obj in (("V0 M2 attuale", base_cols, None), ("V1 senza stagionali", v1_cols, None),
                            ("V2 più bias", v2_cols, None), ("V3 V1+V2", v3_cols, None),
                            ("V4 V3 obiettivo L1", v3_cols, "l1")):
        booster = fit(train, cols, n_iter, obj)
        pred = full.nwp_valid.to_numpy() + booster.predict(full[cols])
        best_pred[name] = pred
        rows.append(row(name, window, full, pred, eval_mask))
        corr = self_corrected(full, pred, args.self_days, args.self_min)
        rows.append(row(name + " +C", window, full, corr, eval_mask))
        logger.info(f"  {name}: mae {rows[-2]['mae']:.3f} bias {rows[-2]['bias']:+.3f} | +C mae {rows[-1]['mae']:.3f} bias {rows[-1]['bias']:+.3f}")
    # stesse righe per tutti (dove la correzione ha storia): il confronto E / V0+C è già alla pari
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Diagnosi e correzione del bias freddo di M2 temperatura")
    ap.add_argument("--cache", required=True)
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--data-end", default="2026-10-22")
    ap.add_argument("--pause", type=float, default=30.0)
    ap.add_argument("--self-days", type=int, default=30)
    ap.add_argument("--self-min", type=int, default=7)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    data = build(args)
    data["resid"] = data.y - data.nwp_valid
    diagnostics(data)
    n_iter = lgb.Booster(model_file=str(REFERENCE_MODEL)).num_trees()
    feats = inf.load_lgbm("temperature").feature_name()

    rows = run_window(data, "W1 ott 2026", "2026-09-22", "2026-10-01", None, n_iter, feats, args)
    rows += run_window(data, "W2 estate 2026", "2025-11-01", "2026-07-05", "2026-09-22", n_iter, feats, args)
    res = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print("\n" + res.round(3).to_string(index=False))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    res.to_csv(OUT_DIR / "bias_experiments.csv", index=False)


if __name__ == "__main__":
    main()
