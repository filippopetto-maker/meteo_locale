"""
experiment_m2_wind.py — M2 vento (velocità): IFS + residuo LightGBM sul logaritmo

Stesso schema di M2 temperatura/umidità applicato alla velocità del vento a 10 m, con il
target Netatmo ricostruito dai soli moduli anemometro che superano il controllo qualità
(qc_wind_modules.py → wind_qc.csv, flag "ok"): mediana oraria dei moduli buoni per stazione.

La velocità è positiva e asimmetrica, quindi si lavora in scala log1p:
    base   = log1p(IFS velocità a valid_for)
    target = log1p(osservata) − base
    previsione = expm1(base + residuo)
Feature: quelle di produzione (build_m2_frame) + station_cat + nwp_ws_valid (IFS, km/h) +
nwp_wd_sin/cos (direzione IFS) + bias_ws_hour (media sui 30 giorni precedenti di
log1p(IFS) − log1p(osservata) per stazione × ora UTC, come bias_st_hour).

Riferimenti: IFS grezzo; E_ws = IFS corretto con lo stesso bias (nel log, poi expm1).
Split: addestramento fino a --cut (default 01/09/2026, early stopping sulle ultime 8
settimane), test da --cut a fine dati. Metriche: MAE e errore sistematico in km/h, per classe
di vento osservato, per stazione, e correlazione oraria.

Utilizzo:
    python3 scripts/experiment_m2_wind.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "model"))

import backtest_ifs_lead as bt  # noqa: E402
import db  # noqa: E402
import experiment_m2_humidity as hx  # noqa: E402
import experiment_retrain_netatmo as ex  # noqa: E402
import inference as inf  # noqa: E402
import netatmo_wind_backfill as nw  # noqa: E402
from m2 import build_m2_frame  # noqa: E402

logger = logging.getLogger("m2_wind")
OUT_DIR = ROOT / "logs" / "retrain_netatmo_refresh"


def wind_target(qc: pd.DataFrame) -> pd.DataFrame:
    """Velocità oraria per stazione: mediana dei moduli con flag ok."""
    parts = []
    for sid, g in qc[qc.flag == "ok"].groupby("station_id"):
        frames = []
        for mid in g.module_id:
            raw = pd.read_parquet(nw.RAW_DIR / f"{mid.replace(':', '')}.parquet").dropna(subset=["ws"])
            raw = raw[(raw.ws >= 0) & (raw.ws < 200)]
            t = pd.to_datetime(raw.ts, unit="s", utc=True)
            ok = (t - t.dt.round("h")).abs() <= pd.Timedelta(minutes=5)
            frames.append(pd.Series(raw.ws.to_numpy(float)[ok.to_numpy()], index=t[ok].dt.round("h"),
                                    name=mid).groupby(level=0).mean())
        wide = pd.concat(frames, axis=1)
        parts.append(pd.DataFrame({"recorded_at": wide.index, "ws": wide.median(axis=1).to_numpy(),
                                   "n_modules": wide.notna().sum(axis=1).to_numpy(), "station_id": int(sid)}))
    return pd.concat(parts, ignore_index=True)


def station_frame(st: dict, nwp: pd.DataFrame, target: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
    feat = build_m2_frame(nwp, st, feature_cols)
    w = nwp[["recorded_at", "wind_speed", "wind_direction"]].copy()
    w["recorded_at"] = pd.to_datetime(w.recorded_at).dt.tz_localize("UTC")
    w["nwp_wd_sin"] = np.sin(np.deg2rad(w.wind_direction))
    w["nwp_wd_cos"] = np.cos(np.deg2rad(w.wind_direction))
    feat = feat.merge(w.rename(columns={"recorded_at": "valid_for", "wind_speed": "nwp_ws_valid"})
                      [["valid_for", "nwp_ws_valid", "nwp_wd_sin", "nwp_wd_cos"]], on="valid_for", how="left")
    obs = target[target.station_id == st["id"]][["recorded_at", "ws"]].rename(
        columns={"recorded_at": "valid_for", "ws": "y_ws"})
    feat = feat.merge(obs, on="valid_for", how="left")
    both = w.merge(target[target.station_id == st["id"]][["recorded_at", "ws"]], on="recorded_at").dropna(
        subset=["wind_speed", "ws"])
    err = np.log1p(both.wind_speed) - np.log1p(both.ws)
    table = ex.hour_bias_table(both.recorded_at, err)
    feat["bias_ws_hour"] = ex.lookup_bias(table, feat.valid_for)
    return feat


def metrics(name: str, obs: np.ndarray, pred: np.ndarray, sub: pd.DataFrame) -> dict:
    e = pred - obs
    r = {"variante": name, "n": len(obs), "mae": np.abs(e).mean(), "bias": e.mean(),
         "corr": float(np.corrcoef(pred, obs)[0, 1])}
    for lab, lo, hi in (("0-3", 0, 3), ("3-8", 3, 8), (">8", 8, 999)):
        m = (obs >= lo) & (obs < hi)
        r[f"mae_{lab}"] = np.abs(e[m]).mean() if m.any() else np.nan
    return r


def main() -> None:
    ap = argparse.ArgumentParser(description="M2 vento (velocità)")
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--data-end", default="2026-10-22")
    ap.add_argument("--cut", default="2026-09-01")
    ap.add_argument("--pause", type=float, default=30.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    qc = pd.read_csv(nw.nb.WORK_DIR / "wind_qc.csv")
    target = wind_target(qc)
    logger.info(f"Target vento: {len(target):,} ore, {target.station_id.nunique()} stazioni, "
                f"moduli per ora mediana {int(target.n_modules.median())}")
    stations = sorted(db.get_active_stations(), key=lambda s: s["id"])
    stations = [s for s in stations if s["id"] in set(target.station_id) and s["id"] not in ex.EXCLUDED_STATIONS]
    feature_cols = inf.load_lgbm("temperature").feature_name()

    api = bt.OpenMeteo(ROOT / "logs" / "backtest" / "cache", args.pause)
    start = pd.Timestamp(args.since, tz="UTC").to_pydatetime() - timedelta(days=2)
    end = pd.Timestamp(args.data_end, tz="UTC").to_pydatetime()
    frames = [station_frame(st, bt.fetch_historical(api, [st], start, end)[st["id"]], target, feature_cols)
              for st in stations]
    data = pd.concat(frames, ignore_index=True)
    data = data[data.valid_for >= pd.Timestamp(args.since, tz="UTC")].dropna(subset=["y_ws", "nwp_ws_valid"])
    data = data.reset_index(drop=True)
    data["base"] = np.log1p(data.nwp_ws_valid)
    data["resid"] = np.log1p(data.y_ws) - data.base
    cols = [*feature_cols, "station_cat", "nwp_ws_valid", "nwp_wd_sin", "nwp_wd_cos", "bias_ws_hour"]
    cut = pd.Timestamp(args.cut, tz="UTC")
    train, test = data[data.valid_for < cut], data[data.valid_for >= cut].reset_index(drop=True)
    logger.info(f"Train {len(train):,} righe fino al {cut:%d/%m/%Y}; test {len(test):,} "
                f"({test.valid_for.min():%d/%m} → {test.valid_for.max():%d/%m}), {data.station_cat.nunique()} stazioni")

    booster, n_iter = hx.fit(train, cols, "resid", None)
    pred = np.expm1(test.base.to_numpy() + booster.predict(test[cols]))
    obs, ifs = test.y_ws.to_numpy(), test.nwp_ws_valid.to_numpy()

    rows = [metrics("IFS grezzo", obs, ifs, test)]
    ok = test.bias_ws_hour.notna().to_numpy()
    e_ws = np.expm1(test.base.to_numpy() - np.nan_to_num(test.bias_ws_hour.to_numpy()))
    rows.append(metrics("E (IFS corretto col bias log)", obs[ok], e_ws[ok], test[ok]))
    rows.append(metrics(f"M2 vento ({n_iter} it.)", obs, pred, test))
    res = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    print(f"\nTest {test.valid_for.min():%d/%m} → {test.valid_for.max():%d/%m}/2026, velocità osservata media "
          f"{obs.mean():.2f} km/h\n")
    print(res.round(3).to_string(index=False))

    names = {s["id"]: s["name"] for s in stations}
    per = test.assign(pred=pred, ifs=ifs).groupby("station_cat").apply(
        lambda g: pd.Series({"n": len(g), "obs_media": g.y_ws.mean(), "mae_ifs": (g.ifs - g.y_ws).abs().mean(),
                             "mae_m2": (g.pred - g.y_ws).abs().mean(), "bias_m2": (g.pred - g.y_ws).mean()}),
        include_groups=False)
    per.index = [names[i] for i in per.index]
    print("\nPer stazione:\n" + per.round(2).sort_values("mae_m2").to_string())
    imp = pd.Series(booster.feature_importance("gain"), index=booster.feature_name()).sort_values(ascending=False)
    print("\nImportanza (gain) prime 8: " + ", ".join(f"{k} {v / imp.sum():.1%}" for k, v in imp.head(8).items()))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    res.to_csv(OUT_DIR / "wind_test.csv", index=False)


if __name__ == "__main__":
    main()
