"""
experiment_m2hb_lag.py — H1b: M2 umidità con finestre di bias, senza stagionali e con ritardo di produzione

Come M2b (temperatura) ma per l'umidità relativa. In produzione le feature di bias (tabella
bias_features_rh) si calcolano alle 03:00 UTC con i dati fino a ieri e valgono per valid_for di oggi
(ritardo 1 giorno), domani (2) e dopodomani (3): il training impila le stesse righe con le feature
ricalcolate a ritardo 1, 2 e 3 e aggiunge bias_lag.

  bias_rh_hour  stazione × ora UTC, 30 giorni (min 10)
  bias_h7       7 giorni (min 4)               bias_h90   90 giorni (min 30)
  bias_all30    media su tutte le ore, 30 giorni (min 10)
  bias_d1       bias medio dell'ultimo giorno  bias_d3    ultimi 3 giorni (min 2)

Test fuori campione (addestramento fino a --cut, test da --cut) a ciascun ritardo, contro H1 in ombra
(addestrato con le sole feature a ritardo 1, applicato alle feature a ritardo 1, 2, 3 come in produzione)
e contro IFS ed E.

Utilizzo:
    python3 scripts/experiment_m2hb_lag.py
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

logger = logging.getLogger("m2hb_lag")
SEASONAL = ("doy_sin", "doy_cos", "month")
BIAS_FEATURES = ["bias_rh_hour", "bias_h7", "bias_h90", "bias_all30", "bias_d1", "bias_d3"]


def with_lag(data: pd.DataFrame, lag: int) -> pd.DataFrame:
    """Ricalcola le sei feature di bias con ritardo `lag` giorni rispetto al giorno di valid_for."""
    d = data.copy()
    d["_err"] = d.nwp_rh_valid - d.y_rh
    d["_date"], d["_hour"] = d.valid_for.dt.floor("D"), d.valid_for.dt.hour
    cols = {k: np.full(len(d), np.nan, dtype="float32") for k in BIAS_FEATURES}
    for sid, g in d.groupby("station_cat"):
        piv = g.pivot_table(index="_date", columns="_hour", values="_err").asfreq("D")
        daily = piv.mean(axis=1)
        idx = pd.MultiIndex.from_arrays([g._date, g._hour])
        pos = d.index.get_indexer(g.index)
        cols["bias_rh_hour"][pos] = piv.rolling(30, min_periods=10).mean().shift(lag).stack().reindex(idx).to_numpy()
        cols["bias_h7"][pos] = piv.rolling(7, min_periods=4).mean().shift(lag).stack().reindex(idx).to_numpy()
        cols["bias_h90"][pos] = piv.rolling(90, min_periods=30).mean().shift(lag).stack().reindex(idx).to_numpy()
        cols["bias_all30"][pos] = daily.rolling(30, min_periods=10).mean().shift(lag).reindex(g._date).to_numpy()
        cols["bias_d1"][pos] = daily.shift(lag).reindex(g._date).to_numpy()
        cols["bias_d3"][pos] = daily.rolling(3, min_periods=2).mean().shift(lag).reindex(g._date).to_numpy()
    for k, v in cols.items():
        d[k] = v
    return d.drop(columns=["_err", "_date", "_hour"])


def build_frames(since: str, data_end: str, pause: float) -> tuple[pd.DataFrame, list[str]]:
    target = pd.read_parquet(ex.TARGET_FILE)
    target["recorded_at"] = pd.to_datetime(target.recorded_at, utc=True)
    stations = sorted(db.get_active_stations(), key=lambda s: s["id"])
    stations = [s for s in stations if s["id"] in set(target.station_id) and s["id"] not in ex.EXCLUDED_STATIONS]
    feature_cols = inf.load_lgbm("temperature").feature_name()
    api = bt.OpenMeteo(ROOT / "logs" / "backtest" / "cache", pause)
    start = pd.Timestamp(since, tz="UTC").to_pydatetime() - timedelta(days=2)
    end = pd.Timestamp(data_end, tz="UTC").to_pydatetime()
    frames = [hx.humidity_frame(st, bt.fetch_historical(api, [st], start, end)[st["id"]], target, feature_cols)
              for st in stations]
    data = pd.concat(frames, ignore_index=True)
    data = data[data.valid_for >= pd.Timestamp(since, tz="UTC")]
    data = data.dropna(subset=["y_rh", "nwp_rh_valid", "nwp_valid"]).reset_index(drop=True)
    data["resid_rh"] = data.y_rh - data.nwp_rh_valid
    num = data.select_dtypes("float64").columns
    data[num] = data[num].astype("float32")
    return data, feature_cols


def stack(data: pd.DataFrame, lags: list[int]) -> pd.DataFrame:
    parts = []
    for l in lags:
        d = with_lag(data, l)
        d["bias_lag"] = l
        parts.append(d)
    return pd.concat(parts, ignore_index=True)


def feature_list(feature_cols: list[str]) -> list[str]:
    feats = [c for c in feature_cols if c not in SEASONAL]
    return [*feats, "station_cat", "nwp_valid", "nwp_rh_valid", *BIAS_FEATURES, "bias_lag"]


def main() -> None:
    ap = argparse.ArgumentParser(description="H1b: M2 umidità con ritardo di produzione")
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--data-end", default="2026-10-22")
    ap.add_argument("--cut", default="2026-09-22")
    ap.add_argument("--lags", default="1,2,3")
    ap.add_argument("--pause", type=float, default=30.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    data, feature_cols = build_frames(args.since, args.data_end, args.pause)
    lags = [int(x) for x in args.lags.split(",")]
    cut = pd.Timestamp(args.cut, tz="UTC")
    train, test = data[data.valid_for < cut].reset_index(drop=True), data[data.valid_for >= cut].reset_index(drop=True)
    logger.info(f"Train {len(train):,} righe fino al {cut:%d/%m}; test {len(test):,}")

    # H1 in ombra: feature a ritardo 1, tutte le feature di produzione
    base_cols = [*feature_cols, "station_cat", "nwp_valid", "nwp_rh_valid", "bias_rh_hour"]
    tr1 = with_lag(train, 1)
    h1, n1 = hx.fit(tr1, base_cols, "resid_rh", None)
    # H1b: righe impilate a ritardo 1, 2, 3
    cols_b = feature_list(feature_cols)
    stacked = stack(train, lags)
    hb, nb_ = hx.fit(stacked, cols_b, "resid_rh", None)
    logger.info(f"iterazioni: H1 {n1}, H1b {nb_}")
    del stacked

    rows = []
    for l in lags:
        te = with_lag(test, l)
        te["bias_lag"] = l
        y, ifs = te.y_rh.to_numpy(), te.nwp_rh_valid.to_numpy()
        ok = te.bias_rh_hour.notna().to_numpy()
        preds = {"IFS grezzo": ifs,
                 "E": np.clip(ifs - np.nan_to_num(te.bias_rh_hour.to_numpy()), 0, 100),
                 f"H1 in ombra ({n1} it.)": np.clip(ifs + h1.predict(te[base_cols]), 0, 100),
                 f"H1b ({nb_} it.)": np.clip(ifs + hb.predict(te[cols_b]), 0, 100)}
        sat = y > 90
        for name, p in preds.items():
            m = ok if name == "E" else np.ones(len(y), bool)
            e = (p - y)[m]
            rows.append({"ritardo_giorni": l, "variante": name, "n": int(m.sum()), "mae": np.abs(e).mean(),
                         "bias": e.mean(), "mae_sat90": np.abs((p - y)[m & sat]).mean()})
    res = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(f"\nTest {test.valid_for.min():%d/%m} → {test.valid_for.max():%d/%m}/2026 (punti % di umidità)\n")
    print(res.round(2).to_string(index=False))
    avg = res.groupby("variante")[["mae", "bias", "mae_sat90"]].mean()
    print("\nMedia sui tre ritardi:\n" + avg.round(2).to_string())
    res.to_csv(ROOT / "logs" / "retrain_netatmo_refresh" / "m2hb_lag_test.csv", index=False)


if __name__ == "__main__":
    main()
