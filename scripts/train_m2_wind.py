"""
train_m2_wind.py — Addestra e congela M2 vento (velocità) per la prova in ombra

Stesso schema di experiment_m2_wind.py (IFS a valid_for + residuo LightGBM sul logaritmo),
addestrato su tutti i dati del target vento ricostruito (moduli anemometro con QC). Il
numero di iterazioni viene dall'early stopping sulle ultime 8 settimane, poi si riaddestra
su tutto. La direzione non è modellata: resta quella dell'IFS.

Output (versionati):
  model/m2_wind/lgbm_m2_wind.txt.gz
  model/m2_wind/meta.json   feature, stazioni di training, stazioni "coerenti" col vento live
                            (validate_wind_live.py), parametri del bias

Utilizzo:
    python3 scripts/train_m2_wind.py
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

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "model"))

import backtest_ifs_lead as bt  # noqa: E402
import db  # noqa: E402
import experiment_m2_humidity as hx  # noqa: E402
import experiment_m2_wind as wx  # noqa: E402
import experiment_retrain_netatmo as ex  # noqa: E402
import inference as inf  # noqa: E402
import netatmo_wind_backfill as nw  # noqa: E402

logger = logging.getLogger("train_m2_wind")

OUT_DIR = ROOT / "model" / "m2_wind"
MODEL_FILE = OUT_DIR / "lgbm_m2_wind.txt.gz"
META_FILE = OUT_DIR / "meta.json"


def main() -> None:
    ap = argparse.ArgumentParser(description="Addestra e congela M2 vento su tutti i dati")
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--data-end", default="2026-10-22")
    ap.add_argument("--pause", type=float, default=30.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    target = wx.wind_target(pd.read_csv(nw.nb.WORK_DIR / "wind_qc.csv"))
    stations = sorted(db.get_active_stations(), key=lambda s: s["id"])
    stations = [s for s in stations if s["id"] in set(target.station_id) and s["id"] not in ex.EXCLUDED_STATIONS]
    feature_cols = inf.load_lgbm("temperature").feature_name()

    api = bt.OpenMeteo(ROOT / "logs" / "backtest" / "cache", args.pause)
    start = pd.Timestamp(args.since, tz="UTC").to_pydatetime() - timedelta(days=2)
    end = pd.Timestamp(args.data_end, tz="UTC").to_pydatetime()
    frames = [wx.station_frame(st, bt.fetch_historical(api, [st], start, end)[st["id"]], target, feature_cols)
              for st in stations]
    data = pd.concat(frames, ignore_index=True)
    data = data[data.valid_for >= pd.Timestamp(args.since, tz="UTC")].dropna(subset=["y_ws", "nwp_ws_valid"])
    data = data.reset_index(drop=True)
    data["resid"] = np.log1p(data.y_ws) - np.log1p(data.nwp_ws_valid)
    cols = [*feature_cols, "station_cat", "nwp_ws_valid", "nwp_wd_sin", "nwp_wd_cos", "bias_ws_hour"]
    logger.info(f"Righe di training: {len(data):,} ({data.valid_for.min():%Y-%m-%d} → {data.valid_for.max():%Y-%m-%d}), "
                f"{data.station_cat.nunique()} stazioni")

    booster, n_iter = hx.fit(data, cols, "resid", None)

    agreement = pd.read_csv(nw.nb.WORK_DIR / "wind_live_agreement.csv")
    consistent = sorted(int(s) for s in agreement[agreement.consistent].station_id)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(MODEL_FILE, "wt", compresslevel=9) as f:
        f.write(booster.model_to_string())
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    meta = {
        "model_tag": f"m2w-{data.valid_for.max():%Y%m%d}",
        "target": "wind_speed",
        "variant": "residuo su log1p(velocità IFS), velocità km/h; direzione non modellata (IFS)",
        "contract": "riga NWP a X → vento a X+1h; previsione = expm1(log1p(nwp_ws_valid) + residuo)",
        "features": cols,
        "categorical": ["station_cat"],
        "nwp_model": "ecmwf_ifs",
        "bias": {"days": ex.BIAS_DAYS, "min_days": ex.BIAS_MIN_DAYS,
                 "definition": "media dei valori giornalieri di log1p(velocità IFS) − log1p(velocità Netatmo) "
                               "per stazione × ora UTC, giorni precedenti a quello di emissione; NaN se meno di min_days"},
        "target_source": "mediana oraria dei moduli anemometro Netatmo con QC (wind_qc.csv, flag ok)",
        "train_stations": sorted(int(s) for s in data.station_cat.unique()),
        "live_consistent_stations": consistent,
        "live_consistent_rule": "validate_wind_live.py: ≥1000 ore in comune, correlazione ≥0,55, |bias| ≤2,2 km/h",
        "train_period": [f"{data.valid_for.min():%Y-%m-%d %H:%M}", f"{data.valid_for.max():%Y-%m-%d %H:%M}"],
        "train_rows": int(len(data)),
        "num_boost_round": n_iter,
        "git_commit": commit,
        "trained_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
    }
    META_FILE.write_text(json.dumps(meta, indent=1, default=str))
    logger.info(f"Salvati {MODEL_FILE} ({MODEL_FILE.stat().st_size / 1e6:.1f} MB) — tag {meta['model_tag']}, "
                f"stazioni coerenti {len(consistent)}")


if __name__ == "__main__":
    main()
