"""
train_m2_wind10.py — Addestra e congela M2 vento a 10 m (verità METAR) per la prova in ombra

Velocità (residuo su log1p) e direzione (seno e coseno, pesati con la velocità) addestrate sui METAR
degli 8 aeroporti del Lazio (experiment_m2_wind_metar.py) senza station_cat né bias, così il modello
può essere applicato a qualunque punto con le feature statiche (quota, distanza dal mare, ecc.).
Test: a siti già visti 2,73 km/h e 29,4° contro 3,80 e 36,4 dell'IFS; su siti mai visti
(leave-one-airport-out) 3,19 km/h e nessun guadagno sulla direzione (36,5°) — vedi
experiment_m2_wind10_loso.py.

Output (versionati):
  model/m2_wind10/lgbm_speed.txt.gz, lgbm_dir_sin.txt.gz, lgbm_dir_cos.txt.gz
  model/m2_wind10/meta.json   feature, iterazioni, aeroporti con coordinate e descrittori statici

Utilizzo:
    python3 scripts/train_m2_wind10.py
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "model"))

import experiment_m2_wind10_loso as lo  # noqa: E402
import experiment_m2_wind_metar as wm  # noqa: E402
import inference as inf  # noqa: E402

logger = logging.getLogger("train_m2_wind10")
OUT_DIR = ROOT / "model" / "m2_wind10"


def save(booster, name: str) -> None:
    with gzip.open(OUT_DIR / name, "wt", compresslevel=9) as f:
        f.write(booster.model_to_string())


def main() -> None:
    ap = argparse.ArgumentParser(description="Addestra e congela M2 vento 10 m sui METAR")
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--data-end", default="2026-10-22")
    ap.add_argument("--pause", type=float, default=30.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    feature_cols = inf.load_lgbm("temperature").feature_name()
    data, statics = lo.airport_frames(feature_cols, args.since, args.data_end, args.pause)
    cols = [*feature_cols, "nwp_ws_valid", "nwp_wd_sin", "nwp_wd_cos"]
    logger.info(f"Righe di training: {len(data):,} ({data.valid_for.min():%Y-%m-%d} → {data.valid_for.max():%Y-%m-%d})")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    b_s, n_s = wm.fit_lgb(data, cols, "resid")
    save(b_s, "lgbm_speed.txt.gz")
    d = data.dropna(subset=["obs_dir", "nwp_wd_valid"]).copy()
    d["y_sin"], d["y_cos"] = np.sin(np.deg2rad(d.obs_dir)), np.cos(np.deg2rad(d.obs_dir))
    b_sin, n_sin = wm.fit_lgb(d, cols, "y_sin", weight_col="y_ws")
    b_cos, n_cos = wm.fit_lgb(d, cols, "y_cos", weight_col="y_ws")
    save(b_sin, "lgbm_dir_sin.txt.gz")
    save(b_cos, "lgbm_dir_cos.txt.gz")

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    keep = ("lat", "lon", "altitude", "dist_sea_km", "dist_center_km", "bearing_sea", "microclima")
    meta = {
        "model_tag": f"m2w10-{data.valid_for.max():%Y%m%d}",
        "target": "wind_10m",
        "variant": "velocità km/h (residuo su log1p) e direzione (sin/cos pesati con la velocità), verità METAR",
        "features": cols,
        "num_boost_round": {"speed": n_s, "dir_sin": n_sin, "dir_cos": n_cos},
        "airports": {icao: {"id": int(st["id"]), "name": wm.NAMES[icao], **{k: st.get(k) for k in keep}}
                     for icao, st in statics.items()},
        "train_period": [f"{data.valid_for.min():%Y-%m-%d %H:%M}", f"{data.valid_for.max():%Y-%m-%d %H:%M}"],
        "train_rows": int(len(data)), "train_rows_dir": int(len(d)),
        "min_speed_dir_kmh": wm.MIN_SPEED,
        "expected": {"seen_sites": {"speed_mae_kmh": 2.73, "dir_mae_deg": 29.4},
                     "unseen_sites_loso": {"speed_mae_kmh": 3.19, "dir_mae_deg": 36.5},
                     "ifs": {"speed_mae_kmh": 3.80, "dir_mae_deg": 36.4}},
        "git_commit": commit,
        "trained_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
    }
    (OUT_DIR / "meta.json").write_text(json.dumps(meta, indent=1, default=str))
    logger.info(f"Salvato model/m2_wind10 — tag {meta['model_tag']}, iterazioni {meta['num_boost_round']}")


if __name__ == "__main__":
    main()
