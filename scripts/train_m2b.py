"""
train_m2b.py — Addestra e congela M2b (temperatura): M2 senza stagionali, con più feature di bias

Risolve l'errore sistematico freddo di M2 (−0,3 °C): scripts/experiment_m2_bias.py ha trovato che
(1) doy_sin, doy_cos e month fanno memorizzare l'anomalia di soli 2,5 cicli stagionali e
(2) più riassunti del bias IFS−Netatmo (7 e 90 giorni per ora, media 30 giorni su tutte le ore,
    ultimo giorno, ultimi 3 giorni) riducono l'offset residuo.
Il resto è come M2: IFS a valid_for + residuo LightGBM, stesse feature di produzione, stesso numero
di iterazioni, stessi parametri, addestrato su TUTTI i dati disponibili.

Il training impila le stesse righe con ritardo 1, 2 e 3 giorni delle feature di bias rispetto al giorno
di valid_for e aggiunge la feature bias_lag: la bias_features è calcolata alle 03:00 con i dati fino a ieri
e vale per valid_for di oggi (ritardo 1), domani (2) e dopodomani (3). Test: experiment_m2b_stack.py.
La correzione sull'errore proprio (λ = 0,5) si applica fuori dal modello (model/m2b.py, shadow_m2b.py).

Output:
  model/m2b/lgbm_m2b_temperature.txt.gz   modello compresso
  model/m2b/meta.json                     feature, lag, definizione dei bias, parametri della correzione

Utilizzo:
    python3 scripts/train_m2b.py --cache logs/retrain_netatmo_refresh/frames_cache.parquet
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import subprocess
import sys
from pathlib import Path

import lightgbm as lgb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "model"))

import experiment_m2_bias as xb  # noqa: E402
import experiment_m2b_lag as lg  # noqa: E402
import inference as inf  # noqa: E402

logger = logging.getLogger("train_m2b")

OUT_DIR = ROOT / "model" / "m2b"
MODEL_FILE = OUT_DIR / "lgbm_m2b_temperature.txt.gz"
META_FILE = OUT_DIR / "meta.json"


def main() -> None:
    ap = argparse.ArgumentParser(description="Addestra e congela M2b su tutti i dati")
    ap.add_argument("--cache", required=True)
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--data-end", default="2026-10-22")
    ap.add_argument("--pause", type=float, default=30.0)
    ap.add_argument("--lags", default="1,2,3")
    ap.add_argument("--lambda", dest="lam", type=float, default=0.5)
    ap.add_argument("--self-days", type=int, default=30)
    ap.add_argument("--self-min", type=int, default=7)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    n_iter = lgb.Booster(model_file=str(xb.REFERENCE_MODEL)).num_trees()
    base = xb.build(args)
    base["resid"] = base.y - base.nwp_valid
    lags = [int(x) for x in args.lags.split(",")]
    parts = []
    for l in lags:
        d = lg.with_lag(base, l)
        d["bias_lag"] = l
        parts.append(d)
    del base
    data = pd.concat(parts, ignore_index=True)
    del parts
    feats = [c for c in inf.load_lgbm("temperature").feature_name() if c not in xb.SEASONAL and c != "bias_st_hour"]
    cols = [*feats, "station_cat", "nwp_valid", *lg.BIAS_FEATURES, "bias_lag"]
    logger.info(f"Righe di training: {len(data):,} ({data.valid_for.min():%Y-%m-%d} → {data.valid_for.max():%Y-%m-%d}), "
                f"{len(cols)} feature, ritardi {lags}, iterazioni {n_iter}")

    booster = xb.fit(data, cols, n_iter)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(MODEL_FILE, "wt", compresslevel=9) as f:
        f.write(booster.model_to_string())
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    tag = f"m2b-{pd.Timestamp(args.data_end) - pd.Timedelta(days=1):%Y%m%d}"
    meta = {
        "model_tag": tag,
        "target": "temperature",
        "contract": "riga NWP a X → temperatura a X+1h; previsione = nwp_valid + residuo",
        "features": cols,
        "categorical": ["station_cat"],
        "nwp_model": "ecmwf_ifs",
        "bias_lags_in_training": lags,
        "bias_features": {
            "bias_st_hour": "media dei valori giornalieri (IFS − Netatmo) per stazione × ora UTC, 30 giorni, minimo 10",
            "bias_h7": "come bias_st_hour su 7 giorni, minimo 4",
            "bias_h90": "come bias_st_hour su 90 giorni, minimo 30",
            "bias_all30": "media su tutte le ore del bias giornaliero, 30 giorni, minimo 10",
            "bias_d1": "bias medio dell'ultimo giorno disponibile",
            "bias_d3": "bias medio degli ultimi 3 giorni disponibili, minimo 2",
            "bias_lag": "giorni tra l'ultimo giorno con dati della bias_features e il giorno di valid_for (1, 2 o 3)",
        },
        "self_correction": {"days": args.self_days, "min_days": args.self_min, "lambda": args.lam,
                            "definition": "previsione − media dell'errore (previsione − osservato) per stazione × ora UTC "
                                          "sui giorni precedenti, dalle previsioni emesse dal modello stesso"},
        "excluded_stations": sorted(xb.ex.EXCLUDED_STATIONS),
        "train_period": [f"{data.valid_for.min():%Y-%m-%d %H:%M}", f"{data.valid_for.max():%Y-%m-%d %H:%M}"],
        "train_rows": int(len(data) // len(lags)),
        "num_boost_round": n_iter,
        "lgb_params": {**xb.DEFAULT_LGB_PARAMS, "seed": 42},
        "git_commit": commit,
        "trained_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
    }
    META_FILE.write_text(json.dumps(meta, indent=1, default=str))
    logger.info(f"Salvati {MODEL_FILE} ({MODEL_FILE.stat().st_size / 1e6:.1f} MB) e {META_FILE} — tag {tag}")


if __name__ == "__main__":
    main()
