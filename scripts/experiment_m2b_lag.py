"""
experiment_m2b_lag.py — La ricetta V3 + correzione resta valida con il ritardo di produzione?

experiment_m2_bias.py ha trovato che M2 senza feature stagionali, con più feature di bias
(V3) e con la correzione sul proprio errore (+C) toglie l'errore sistematico freddo
(W1 0,620 °C, W2 0,624, errore sistematico ≈ 0). In quel test tutte le feature di bias
finivano il giorno prima di valid_for. In produzione la bias_table è calcolata alle 03:00
con i dati fino a ieri e vale per valid_for da oggi a dopodomani: il ritardo reale è da 1 a 3
giorni (tra 1 e 4 per valid_for oltre le 48 h). Qui tutte le feature di bias e la correzione
usano un ritardo fisso --lag (1, 2, 3) e si guarda quanto costa.

Le feature di bias sono ricalcolate dal frame in cache: l'errore IFS − Netatmo all'ora
valid_for è nwp_valid − y.

Utilizzo:
    python3 scripts/experiment_m2b_lag.py --cache /percorso/frames.parquet
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "model"))

import experiment_m2_bias as xb  # noqa: E402
import inference as inf  # noqa: E402

logger = logging.getLogger("m2b_lag")

BIAS_FEATURES = ["bias_st_hour", "bias_h7", "bias_h90", "bias_all30", "bias_d1", "bias_d3"]


def with_lag(data: pd.DataFrame, lag: int) -> pd.DataFrame:
    """Ricalcola le sei feature di bias con ritardo `lag` giorni rispetto al giorno di valid_for."""
    d = data.copy()
    d["_err"] = d.nwp_valid - d.y
    d["_date"], d["_hour"] = d.valid_for.dt.floor("D"), d.valid_for.dt.hour
    cols = {k: np.full(len(d), np.nan) for k in BIAS_FEATURES}
    for sid, g in d.groupby("station_cat"):
        piv = g.pivot_table(index="_date", columns="_hour", values="_err").asfreq("D")
        daily = piv.mean(axis=1)
        idx = pd.MultiIndex.from_arrays([g._date, g._hour])
        pos = d.index.get_indexer(g.index)
        cols["bias_st_hour"][pos] = piv.rolling(30, min_periods=10).mean().shift(lag).stack().reindex(idx).to_numpy()
        cols["bias_h7"][pos] = piv.rolling(7, min_periods=4).mean().shift(lag).stack().reindex(idx).to_numpy()
        cols["bias_h90"][pos] = piv.rolling(90, min_periods=30).mean().shift(lag).stack().reindex(idx).to_numpy()
        cols["bias_all30"][pos] = daily.rolling(30, min_periods=10).mean().shift(lag).reindex(g._date).to_numpy()
        cols["bias_d1"][pos] = daily.shift(lag).reindex(g._date).to_numpy()
        cols["bias_d3"][pos] = daily.rolling(3, min_periods=2).mean().shift(lag).reindex(g._date).to_numpy()
    for k, v in cols.items():
        d[k] = v
    return d.drop(columns=["_err", "_date", "_hour"])


def self_corrected_lag(test: pd.DataFrame, pred: np.ndarray, days: int, min_days: int, lag: int) -> np.ndarray:
    t = pd.DataFrame({"sid": test.station_cat.to_numpy(), "d": test.valid_for.dt.floor("D"),
                      "h": test.valid_for.dt.hour, "err": pred - test.y.to_numpy()})
    out = np.full(len(t), np.nan)
    for sid, g in t.groupby("sid"):
        piv = g.pivot_table(index="d", columns="h", values="err").asfreq("D")
        roll = piv.rolling(days, min_periods=min_days).mean().shift(lag).stack()
        idx = pd.MultiIndex.from_arrays([g.d, g.h])
        out[g.index.to_numpy()] = pred[g.index.to_numpy()] - roll.reindex(idx).to_numpy()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="V3 + correzione con ritardo di produzione")
    ap.add_argument("--cache", required=True)
    ap.add_argument("--lags", default="1,3")
    ap.add_argument("--self-days", type=int, default=30)
    ap.add_argument("--self-min", type=int, default=7)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    data = pd.read_parquet(args.cache)
    data["resid"] = data.y - data.nwp_valid
    n_iter = lgb.Booster(model_file=str(xb.REFERENCE_MODEL)).num_trees()
    feats = [c for c in inf.load_lgbm("temperature").feature_name() if c not in xb.SEASONAL and c != "bias_st_hour"]
    cols = [*feats, "station_cat", "nwp_valid", *BIAS_FEATURES]

    rows = []
    for lag in (int(x) for x in args.lags.split(",")):
        d = with_lag(data, lag)
        for window, cut, ts, te in (("W1 ott 2026", "2026-09-22", "2026-10-01", None),
                                    ("W2 estate 2026", "2025-11-01", "2026-07-05", "2026-09-22")):
            cut_t, ts_t = pd.Timestamp(cut, tz="UTC"), pd.Timestamp(ts, tz="UTC")
            te_t = pd.Timestamp(te, tz="UTC") if te else d.valid_for.max() + pd.Timedelta(hours=1)
            train = d[d.valid_for < cut_t]
            full = d[(d.valid_for >= cut_t) & (d.valid_for < te_t)].reset_index(drop=True)
            mask = (full.valid_for >= ts_t).to_numpy()
            booster = xb.fit(train, cols, n_iter)
            pred = full.nwp_valid.to_numpy() + booster.predict(full[cols])
            e_pred = (full.nwp_valid - full.bias_st_hour).to_numpy()
            rows.append({**xb.row("E", window, full, e_pred, mask), "lag": lag})
            rows.append({**xb.row("V3", window, full, pred, mask), "lag": lag})
            corr = self_corrected_lag(full, pred, args.self_days, args.self_min, lag)
            rows.append({**xb.row("V3 +C", window, full, corr, mask), "lag": lag})
            logger.info(f"lag {lag} {window}: V3 mae {rows[-2]['mae']:.3f} bias {rows[-2]['bias']:+.3f} | "
                        f"+C mae {rows[-1]['mae']:.3f} bias {rows[-1]['bias']:+.3f} | E mae {rows[-3]['mae']:.3f}")
    res = pd.DataFrame(rows)[["lag", "finestra", "variante", "n", "mae", "bias", "bias_ore", "bias_staz_std", "staz_bias_<-0.5"]]
    pd.set_option("display.width", 250)
    print("\n" + res.round(3).to_string(index=False))
    res.to_csv(xb.OUT_DIR / "m2b_lag.csv", index=False)


if __name__ == "__main__":
    main()
