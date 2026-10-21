"""
experiment_m2b_stack.py — Un solo M2b valido per tutti i ritardi della bias_table

experiment_m2b_lag.py: le feature di bias con ritardo 1 giorno valgono circa 0,06–0,08 °C di
MAE in più di quelle con ritardo 3 giorni, ma in produzione la bias_table (calcolata alle 03:00
con i dati fino a ieri) ha ritardo 1 per valid_for di oggi, 2 per domani, 3 per dopodomani.
Qui il training impila le stesse righe con ritardo 1, 2 e 3 e aggiunge la feature bias_lag; in
test ogni riga si valuta con il suo ritardo. Si confronta anche una correzione parziale
(+C con peso --lambdas) per non pagare rumore quando l'offset è già nullo.

Utilizzo:
    python3 scripts/experiment_m2b_stack.py --cache /percorso/frames.parquet
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
import experiment_m2b_lag as lg  # noqa: E402
import inference as inf  # noqa: E402

logger = logging.getLogger("m2b_stack")


def main() -> None:
    ap = argparse.ArgumentParser(description="M2b con ritardo del bias come feature")
    ap.add_argument("--cache", required=True)
    ap.add_argument("--lags", default="1,2,3")
    ap.add_argument("--lambdas", default="0,0.5,1")
    ap.add_argument("--self-days", type=int, default=30)
    ap.add_argument("--self-min", type=int, default=7)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    data = pd.read_parquet(args.cache)
    data["resid"] = data.y - data.nwp_valid
    lags = [int(x) for x in args.lags.split(",")]
    lambdas = [float(x) for x in args.lambdas.split(",")]
    n_iter = lgb.Booster(model_file=str(xb.REFERENCE_MODEL)).num_trees()
    feats = [c for c in inf.load_lgbm("temperature").feature_name() if c not in xb.SEASONAL and c != "bias_st_hour"]
    cols = [*feats, "station_cat", "nwp_valid", *lg.BIAS_FEATURES, "bias_lag"]

    by_lag = {}
    for l in lags:
        d = lg.with_lag(data, l)
        d["bias_lag"] = l
        by_lag[l] = d
    logger.info(f"Frame con ritardo {lags} pronti")

    rows = []
    for window, cut, ts, te in (("W1 ott 2026", "2026-09-22", "2026-10-01", None),
                                ("W2 estate 2026", "2025-11-01", "2026-07-05", "2026-09-22")):
        cut_t, ts_t = pd.Timestamp(cut, tz="UTC"), pd.Timestamp(ts, tz="UTC")
        end = data.valid_for.max() + pd.Timedelta(hours=1)
        te_t = pd.Timestamp(te, tz="UTC") if te else end
        train = pd.concat([d[d.valid_for < cut_t] for d in by_lag.values()], ignore_index=True)
        booster = xb.fit(train, cols, n_iter)
        logger.info(f"{window}: addestrato su {len(train):,} righe")
        for l, d in by_lag.items():
            full = d[(d.valid_for >= cut_t) & (d.valid_for < te_t)].reset_index(drop=True)
            mask = (full.valid_for >= ts_t).to_numpy()
            pred = full.nwp_valid.to_numpy() + booster.predict(full[cols])
            rows.append({**xb.row("E", window, full, (full.nwp_valid - full.bias_st_hour).to_numpy(), mask), "lag": l, "lambda": np.nan})
            for lam in lambdas:
                corr = pred if lam == 0 else pred - lam * (pred - lg.self_corrected_lag(full, pred, args.self_days, args.self_min, l))
                rows.append({**xb.row("M2b impilato", window, full, corr, mask), "lag": l, "lambda": lam})
            logger.info(f"  lag {l}: " + " | ".join(f"λ={r['lambda']}: {r['mae']:.3f}/{r['bias']:+.3f}" for r in rows[-len(lambdas):]))
    res = pd.DataFrame(rows)[["finestra", "lag", "variante", "lambda", "n", "mae", "bias", "bias_ore", "bias_staz_std", "staz_bias_<-0.5"]]
    pd.set_option("display.width", 250)
    print("\n" + res.round(3).to_string(index=False))
    res.to_csv(xb.OUT_DIR / "m2b_stack.csv", index=False)


if __name__ == "__main__":
    main()
