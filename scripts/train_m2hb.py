"""
train_m2hb.py — Addestra e congela M2 umidità H1b (finestre di bias, senza stagionali, ritardo di produzione)

Vedi experiment_m2hb_lag.py. Previsione = RH IFS a valid_for + residuo LightGBM, limitata a [0, 100];
feature di produzione senza doy_sin, doy_cos, month; station_cat, nwp_valid (T IFS), nwp_rh_valid;
sei riassunti del bias RH IFS − Netatmo (bias_rh_hour, bias_h7, bias_h90, bias_all30, bias_d1, bias_d3)
e bias_lag. Addestrato su TUTTI i dati con le righe impilate a ritardo 1, 2 e 3 giorni; il numero di
iterazioni viene dall'early stopping sulle ultime 8 settimane.

Output: model/m2hb/lgbm_m2hb_humidity.txt.gz, model/m2hb/meta.json

Utilizzo:
    python3 scripts/train_m2hb.py
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "model"))

import experiment_m2_humidity as hx  # noqa: E402
import experiment_m2hb_lag as lg  # noqa: E402
import experiment_retrain_netatmo as ex  # noqa: E402

logger = logging.getLogger("train_m2hb")
OUT_DIR = ROOT / "model" / "m2hb"


def main() -> None:
    ap = argparse.ArgumentParser(description="Addestra e congela M2 umidità H1b su tutti i dati")
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--data-end", default="2026-10-22")
    ap.add_argument("--lags", default="1,2,3")
    ap.add_argument("--pause", type=float, default=30.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    data, feature_cols = lg.build_frames(args.since, args.data_end, args.pause)
    lags = [int(x) for x in args.lags.split(",")]
    cols = lg.feature_list(feature_cols)
    stacked = lg.stack(data, lags)
    logger.info(f"Righe di training: {len(stacked):,} ({data.valid_for.min():%Y-%m-%d} → {data.valid_for.max():%Y-%m-%d}), "
                f"{len(cols)} feature, ritardi {lags}")
    booster, n_iter = hx.fit(stacked, cols, "resid_rh", None)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT_DIR / "lgbm_m2hb_humidity.txt.gz", "wt", compresslevel=9) as f:
        f.write(booster.model_to_string())
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    meta = {
        "model_tag": f"m2hb-{data.valid_for.max():%Y%m%d}",
        "target": "humidity",
        "variant": "H1b: residuo diretto su RH, finestre di bias, senza stagionali, ritardo di produzione; risultato in [0, 100]",
        "contract": "riga NWP a X → umidità a X+1h; previsione = nwp_rh_valid + residuo",
        "features": cols,
        "categorical": ["station_cat"],
        "nwp_model": "ecmwf_ifs",
        "bias_lags_in_training": lags,
        "bias_features": {
            "bias_rh_hour": "media dei valori giornalieri (RH IFS − RH Netatmo) per stazione × ora UTC, 30 giorni, minimo 10",
            "bias_h7": "come bias_rh_hour su 7 giorni, minimo 4",
            "bias_h90": "come bias_rh_hour su 90 giorni, minimo 30",
            "bias_all30": "media su tutte le ore del bias giornaliero, 30 giorni, minimo 10",
            "bias_d1": "bias medio dell'ultimo giorno disponibile",
            "bias_d3": "bias medio degli ultimi 3 giorni disponibili, minimo 2",
            "bias_lag": "giorni tra l'ultimo giorno con dati di bias_features_rh e il giorno di valid_for (1, 2 o 3)",
        },
        "excluded_stations": sorted(ex.EXCLUDED_STATIONS),
        "train_period": [f"{data.valid_for.min():%Y-%m-%d %H:%M}", f"{data.valid_for.max():%Y-%m-%d %H:%M}"],
        "train_rows": int(len(data)),
        "num_boost_round": n_iter,
        "expected": {"test_22_09_21_10": {"h1_in_ombra_mae": 4.02, "h1b_mae": 3.81, "ifs_mae": 8.42,
                                           "nota": "media dei ritardi 1-3; saturazione >90%: 4,17 contro 4,33"}},
        "git_commit": commit,
        "trained_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
    }
    (OUT_DIR / "meta.json").write_text(json.dumps(meta, indent=1, default=str))
    logger.info(f"Salvato model/m2hb — tag {meta['model_tag']}, iterazioni {n_iter}")


if __name__ == "__main__":
    main()
