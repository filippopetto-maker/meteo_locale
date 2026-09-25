"""
train_m2.py — Addestra e congela M2 (temperatura) per la prova in ombra

Stesso modello di experiment_retrain_netatmo.py (variante M2: IFS a valid_for +
residuo LightGBM, feature di produzione + stazione + bias IFS−Netatmo staz×ora sui
30 giorni precedenti), ma addestrato su TUTTI i dati disponibili, senza
validazione: il numero di iterazioni è quello trovato con early stopping nel run
con validazione ad agosto (logs/retrain_netatmo/lgbm_m2.txt).

Output (versionati):
  model/m2/lgbm_m2_temperature.txt.gz   modello LightGBM compresso
  model/m2/meta.json                    feature, periodo, parametri del bias

Utilizzo:
    python3 scripts/train_m2.py
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import lightgbm as lgb
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

logger = logging.getLogger("train_m2")

OUT_DIR = ROOT / "model" / "m2"
MODEL_FILE = OUT_DIR / "lgbm_m2_temperature.txt.gz"
META_FILE = OUT_DIR / "meta.json"
REFERENCE_MODEL = ROOT / "logs" / "retrain_netatmo" / "lgbm_m2.txt"


def main() -> None:
    ap = argparse.ArgumentParser(description="Addestra e congela M2 su tutti i dati")
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--data-end", default="2026-09-22")
    ap.add_argument("--pause", type=float, default=30.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    n_iter = lgb.Booster(model_file=str(REFERENCE_MODEL)).num_trees()
    logger.info(f"Iterazioni dal run con validazione ad agosto ({REFERENCE_MODEL.name}): {n_iter}")

    target = pd.read_parquet(ex.TARGET_FILE)
    target["recorded_at"] = pd.to_datetime(target.recorded_at, utc=True)
    stations = sorted(db.get_active_stations(), key=lambda s: s["id"])
    stations = [s for s in stations if s["id"] in set(target.station_id) and s["id"] not in ex.EXCLUDED_STATIONS]
    feature_cols = inf.load_lgbm("temperature").feature_name()

    api = bt.OpenMeteo(ROOT / "logs" / "backtest" / "cache", args.pause)
    start = pd.Timestamp(args.since, tz="UTC").to_pydatetime() - timedelta(days=2)
    end = pd.Timestamp(args.data_end, tz="UTC").to_pydatetime()
    frames = [ex.station_frame(st, bt.fetch_historical(api, [st], start, end)[st["id"]], target, feature_cols)
              for st in stations]
    data = pd.concat(frames, ignore_index=True)
    data = data[data.valid_for >= pd.Timestamp(args.since, tz="UTC")].dropna(subset=["y", "nwp_valid"])
    data["resid"] = data.y - data.nwp_valid
    cols = [*feature_cols, "station_cat", "nwp_valid", "bias_st_hour"]
    logger.info(f"Righe di training: {len(data):,} ({data.valid_for.min():%Y-%m-%d} → {data.valid_for.max():%Y-%m-%d}), "
                f"{data.station_cat.nunique()} stazioni, chiamate API nuove {api.calls}")

    dtr = lgb.Dataset(data[cols], label=data.resid, categorical_feature=["station_cat"])
    params = {**DEFAULT_LGB_PARAMS, "seed": 42}
    booster = lgb.train(params, dtr, num_boost_round=n_iter)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(MODEL_FILE, "wt", compresslevel=9) as f:
        f.write(booster.model_to_string())
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    tag = f"m2-{pd.Timestamp(args.data_end):%Y%m%d}"
    meta = {
        "model_tag": tag,
        "target": "temperature",
        "contract": "riga NWP a X → temperatura a X+1h; previsione = nwp_valid + residuo",
        "features": cols,
        "categorical": ["station_cat"],
        "nwp_model": "ecmwf_ifs",
        "bias": {"days": ex.BIAS_DAYS, "min_days": ex.BIAS_MIN_DAYS,
                 "definition": "media dei valori giornalieri (IFS − Netatmo) per stazione × ora UTC, "
                               "giorni precedenti a quello di emissione; NaN se meno di min_days"},
        "excluded_stations": sorted(ex.EXCLUDED_STATIONS),
        "train_period": [f"{data.valid_for.min():%Y-%m-%d %H:%M}", f"{data.valid_for.max():%Y-%m-%d %H:%M}"],
        "train_rows": int(len(data)),
        "num_boost_round": n_iter,
        "lgb_params": params,
        "git_commit": commit,
        "trained_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
    }
    META_FILE.write_text(json.dumps(meta, indent=1, default=str))
    logger.info(f"Salvati {MODEL_FILE} ({MODEL_FILE.stat().st_size / 1e6:.1f} MB) e {META_FILE} — tag {tag}")


if __name__ == "__main__":
    main()
