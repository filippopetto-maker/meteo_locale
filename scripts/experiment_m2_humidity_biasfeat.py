"""
experiment_m2_humidity_biasfeat.py — M2 umidità con più finestre di bias e senza feature stagionali (come M2b)

M2b (temperatura) ha ridotto l'errore sistematico togliendo le feature stagionali (doy_sin, doy_cos,
month) e aggiungendo riassunti del bias IFS − Netatmo su finestre diverse. In ombra M2 umidità (H1)
sovrastima di circa 2,4 punti. Qui si prova lo stesso sull'umidità relativa:

  bias_rh_hour   stazione × ora UTC, 30 giorni (min 10)         [già in H1]
  bias_h7        stazione × ora UTC, 7 giorni (min 4)
  bias_h90       stazione × ora UTC, 90 giorni (min 30)
  bias_all30     media su tutte le ore del bias giornaliero, 30 giorni (min 10)
  bias_d1        bias medio dell'ultimo giorno disponibile
  bias_d3        bias medio degli ultimi 3 giorni disponibili (min 2)

Tutti calcolati fino al giorno precedente (lag 1). Quattro varianti addestrate fino a --cut e valutate
da --cut: H1 base, H1 senza stagionali, H1 con le finestre, H1 con le finestre senza stagionali.
Riferimenti IFS ed E. Metriche: errore medio, errore sistematico (anche per settimana), con umidità
osservata >90%, notte e giorno.

Utilizzo:
    python3 scripts/experiment_m2_humidity_biasfeat.py
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

logger = logging.getLogger("m2_hum_biasfeat")
SEASONAL = ("doy_sin", "doy_cos", "month")
NEW = ["bias_h7", "bias_h90", "bias_all30", "bias_d1", "bias_d3"]


def bias_windows(nwp: pd.DataFrame, target: pd.DataFrame, sid: int, valid: pd.Series) -> pd.DataFrame:
    n = nwp[["recorded_at", "humidity"]].copy()
    n["recorded_at"] = pd.to_datetime(n.recorded_at).dt.tz_localize("UTC")
    obs = target[target.station_id == sid][["recorded_at", "humidity"]]
    both = n.merge(obs, on="recorded_at", suffixes=("_nwp", "_obs")).dropna()
    err = pd.DataFrame({"e": (both.humidity_nwp - both.humidity_obs).to_numpy()}, index=pd.DatetimeIndex(both.recorded_at))
    err["date"], err["hour"] = err.index.floor("D"), err.index.hour
    piv = err.pivot_table(index="date", columns="hour", values="e").asfreq("D")
    h7 = piv.rolling(7, min_periods=4).mean().shift(1).stack()
    h90 = piv.rolling(90, min_periods=30).mean().shift(1).stack()
    daily = piv.mean(axis=1)
    all30 = daily.rolling(30, min_periods=10).mean().shift(1)
    d1 = daily.shift(1)
    d3 = daily.rolling(3, min_periods=2).mean().shift(1)
    idx = pd.MultiIndex.from_arrays([valid.dt.floor("D"), valid.dt.hour])
    day = valid.dt.floor("D")
    return pd.DataFrame({"bias_h7": h7.reindex(idx).to_numpy(), "bias_h90": h90.reindex(idx).to_numpy(),
                         "bias_all30": all30.reindex(day).to_numpy(), "bias_d1": d1.reindex(day).to_numpy(),
                         "bias_d3": d3.reindex(day).to_numpy()}, index=valid.index)


def report(name: str, test: pd.DataFrame, pred: np.ndarray) -> dict:
    e = pred - test.y_rh.to_numpy()
    hrs = test.valid_for.dt.hour.to_numpy()
    sat = test.y_rh.to_numpy() > 90
    weeks = pd.Series(e).groupby(test.valid_for.dt.tz_localize(None).dt.to_period("W-SUN").to_numpy()).mean()
    return {"variante": name, "n": len(e), "mae": np.abs(e).mean(), "bias": e.mean(),
            "mae_sat90": np.abs(e[sat]).mean(), "mae_notte": np.abs(e[hrs <= 5]).mean(),
            "mae_giorno": np.abs(e[(hrs >= 9) & (hrs <= 17)]).mean(), "bias_sett": " ".join(f"{b:+.1f}" for b in weeks)}


def main() -> None:
    ap = argparse.ArgumentParser(description="M2 umidità: finestre di bias e senza stagionali")
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--data-end", default="2026-10-22")
    ap.add_argument("--cut", default="2026-09-22")
    ap.add_argument("--pause", type=float, default=30.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

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
        f = hx.humidity_frame(st, nwp, target, feature_cols)
        frames.append(pd.concat([f, bias_windows(nwp, target, st["id"], f.valid_for)], axis=1))
    data = pd.concat(frames, ignore_index=True)
    data = data[data.valid_for >= pd.Timestamp(args.since, tz="UTC")]
    data = data.dropna(subset=["y_rh", "nwp_rh_valid", "nwp_valid"]).reset_index(drop=True)
    data["resid_rh"] = data.y_rh - data.nwp_rh_valid
    cut = pd.Timestamp(args.cut, tz="UTC")
    train, test = data[data.valid_for < cut], data[data.valid_for >= cut].reset_index(drop=True)
    logger.info(f"Train {len(train):,} fino al {cut:%d/%m}; test {len(test):,} ({test.valid_for.min():%d/%m} → {test.valid_for.max():%d/%m})")

    base = [*feature_cols, "station_cat", "nwp_valid", "nwp_rh_valid", "bias_rh_hour"]
    no_seas = [c for c in base if c not in SEASONAL]
    variants = {"H1 base (in ombra)": base, "H1 senza stagionali": no_seas,
                "H1 + finestre di bias": [*base, *NEW], "H1 + finestre, senza stagionali (stile M2b)": [*no_seas, *NEW]}
    print("stagionali presenti nelle feature:", [c for c in SEASONAL if c in feature_cols])
    rows = []
    ok = test.bias_rh_hour.notna().to_numpy()
    rows.append(report("IFS grezzo", test, test.nwp_rh_valid.to_numpy()))
    sub = test[ok].reset_index(drop=True)
    rows.append(report("E (IFS − bias RH)", sub, np.clip(sub.nwp_rh_valid.to_numpy() - sub.bias_rh_hour.to_numpy(), 0, 100)))
    for name, cols in variants.items():
        booster, n_it = hx.fit(train, cols, "resid_rh", None)
        pred = np.clip(test.nwp_rh_valid.to_numpy() + booster.predict(test[cols]), 0, 100)
        rows.append({**report(f"{name} ({n_it} it.)", test, pred)})
        if "finestre" in name and "senza" not in name:
            imp = pd.Series(booster.feature_importance("gain"), index=booster.feature_name()).sort_values(ascending=False)
            logger.info("importanza (gain): " + ", ".join(f"{k} {v / imp.sum():.1%}" for k, v in imp.head(8).items()))
    res = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print(f"\nTest {test.valid_for.min():%d/%m} → {test.valid_for.max():%d/%m}/2026 (punti %)\n")
    print(res.round(2).to_string(index=False))
    res.to_csv(ROOT / "logs" / "retrain_netatmo_refresh" / "humidity_biasfeat_test.csv", index=False)


if __name__ == "__main__":
    main()
