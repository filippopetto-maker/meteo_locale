"""
train_m2_hum.py — Addestra e congela M2 umidità (variante H1: residuo diretto su RH)

Stesso schema di train_m2.py applicato all'umidità relativa: previsione = RH dell'IFS a
valid_for + residuo LightGBM, feature di produzione + stazione + nwp_valid (temperatura IFS)
+ nwp_rh_valid + bias_rh_hour (bias IFS−Netatmo dell'umidità per stazione × ora UTC sui 30
giorni precedenti). Risultato limitato a [0, 100]. Il numero di iterazioni viene da early
stopping sulle ultime 8 settimane, poi si riaddestra su tutti i dati (vedi
experiment_m2_humidity.fit). La scelta di H1 rispetto al residuo sul punto di rugiada viene
da experiment_m2_humidity.py (22/09 → 21/10/2026: errore medio 3,81 contro 3,91).

Output (versionati):
  model/m2_hum/lgbm_m2_humidity.txt.gz
  model/m2_hum/meta.json

Utilizzo:
    python3 scripts/train_m2_hum.py
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

logger = logging.getLogger("train_m2_hum")

OUT_DIR = ROOT / "model" / "m2_hum"
MODEL_FILE = OUT_DIR / "lgbm_m2_humidity.txt.gz"
META_FILE = OUT_DIR / "meta.json"


def main() -> None:
    ap = argparse.ArgumentParser(description="Addestra e congela M2 umidità (H1) su tutti i dati")
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--data-end", default="2026-10-22")
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
    frames = [hx.humidity_frame(st, bt.fetch_historical(api, [st], start, end)[st["id"]], target, feature_cols)
              for st in stations]
    data = pd.concat(frames, ignore_index=True)
    data = data[data.valid_for >= pd.Timestamp(args.since, tz="UTC")]
    data = data.dropna(subset=["y_rh", "nwp_rh_valid", "nwp_valid"]).reset_index(drop=True)
    data["resid_rh"] = data.y_rh - data.nwp_rh_valid
    cols = [*feature_cols, "station_cat", "nwp_valid", "nwp_rh_valid", "bias_rh_hour"]
    logger.info(f"Righe di training: {len(data):,} ({data.valid_for.min():%Y-%m-%d} → {data.valid_for.max():%Y-%m-%d}), "
                f"{data.station_cat.nunique()} stazioni")

    booster, n_iter = hx.fit(data, cols, "resid_rh", None)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(MODEL_FILE, "wt", compresslevel=9) as f:
        f.write(booster.model_to_string())
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    tag = f"m2h-{data.valid_for.max():%Y%m%d}"
    meta = {
        "model_tag": tag,
        "target": "humidity",
        "variant": "H1 residuo diretto su RH, risultato limitato a [0, 100]",
        "contract": "riga NWP a X → umidità a X+1h; previsione = nwp_rh_valid + residuo",
        "features": cols,
        "categorical": ["station_cat"],
        "nwp_model": "ecmwf_ifs",
        "bias": {"days": ex.BIAS_DAYS, "min_days": ex.BIAS_MIN_DAYS,
                 "definition": "media dei valori giornalieri (RH IFS − RH Netatmo) per stazione × ora UTC, "
                               "giorni precedenti a quello di emissione; NaN se meno di min_days"},
        "excluded_stations": sorted(ex.EXCLUDED_STATIONS),
        "train_period": [f"{data.valid_for.min():%Y-%m-%d %H:%M}", f"{data.valid_for.max():%Y-%m-%d %H:%M}"],
        "train_rows": int(len(data)),
        "num_boost_round": n_iter,
        "git_commit": commit,
        "trained_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
    }
    META_FILE.write_text(json.dumps(meta, indent=1, default=str))
    logger.info(f"Salvati {MODEL_FILE} ({MODEL_FILE.stat().st_size / 1e6:.1f} MB) e {META_FILE} — tag {tag}")


if __name__ == "__main__":
    main()
