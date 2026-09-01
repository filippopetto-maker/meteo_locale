"""
benchmark_timesfm3_vs_mos.py — Benchmark locale: pipeline MOS attuale vs TimesFM-3 zero-shot
Stazione: Roma Sud (id=3) | target: temperature | orizzonte: T+1h

Non fa parte della pipeline di produzione (inference.py / GitHub Actions).
Script sperimentale — non committare (in linea con la convenzione già in uso
per script locali e file dati generati automaticamente).

Uso:
    cd ~/Desktop/meteo_locale
    conda activate meteo
    pip install "timesfm[torch,xreg]"   # scarica anche il checkpoint al primo avvio
    python3 benchmark_timesfm3_vs_mos.py

⚠️ LICENZA: i pesi google/timesfm-3.0-pytorch sono sotto
timesfm-non-commercial-license-v1.0 (non-commercial, non-production).
Questo script è un test locale/offline. NON integrare in inference.py /
GitHub Actions senza rivalutare la licenza rispetto all'uso del progetto.

⚠️ DA VERIFICARE PRIMA DI LANCIARE (API troppo recente per essere certi):
    1. Nome esatto della classe/factory per il checkpoint 3.0
       (nel dubbio: `python3 -c "import timesfm; print(dir(timesfm))"`
       dopo l'installazione, e controllare il README aggiornato su
       https://github.com/google-research/timesfm e la model card
       https://huggingface.co/google/timesfm-3.0-pytorch)
    2. Nome del parametro per passare la covariata nota (past-future
       covariate) a model.forecast() — verificare negli esempi xreg
       del repo prima di eseguire su tutto il sottocampione.
   I due punti sono segnati con "# TODO-VERIFICA" nel codice sotto.
"""

from __future__ import annotations

import logging
import pickle
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error

from forecast import load_dataset, temporal_split, get_feature_cols

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
DATA_PATH     = "data/training_10y_h1.parquet"
MODEL_DIR     = Path("model")
STATION_ID    = 3          # Roma Sud — nel training set originale, no ARSIAL
TARGET        = "temperature"
HORIZON_HOURS = 1
VAL_FRAC      = 0.2        # deve coincidere con forecast.py / correttore.py
CONTEXT_HOURS = 512        # ~21 giorni di storico reale come contesto TimesFM
N_EVAL_POINTS = 300        # sottocampionamento per contenere i tempi CPU
RANDOM_SEED   = 42

# ─────────────────────────────────────────────────────────────────────────────
# 1. Dataset + split IDENTICO alla produzione
# ─────────────────────────────────────────────────────────────────────────────
df = load_dataset(DATA_PATH)
train_df, val_df = temporal_split(df, val_frac=VAL_FRAC)  # split globale multi-stazione

train_st = (train_df[train_df["station_id"] == STATION_ID]
            .sort_values("recorded_at").reset_index(drop=True))
val_st = (val_df[val_df["station_id"] == STATION_ID]
          .sort_values("recorded_at").reset_index(drop=True))

logger.info(
    f"Roma Sud — train: {len(train_st)} righe, val: {len(val_st)} righe "
    f"({val_st['recorded_at'].min()} → {val_st['recorded_at'].max()})"
)

target_col = f"target_{TARGET}"

# ─────────────────────────────────────────────────────────────────────────────
# 2. Baseline "nostro" — LightGBM + RF correttore (pipeline reale di produzione)
# ─────────────────────────────────────────────────────────────────────────────
feature_cols = get_feature_cols(df, TARGET)

booster = lgb.Booster(model_file=str(MODEL_DIR / f"lgbm_{TARGET}.txt"))
with open(MODEL_DIR / f"rf_correttore_{TARGET}.pkl", "rb") as f:
    rf = pickle.load(f)

X_val = val_st[feature_cols]
y_val = val_st[target_col].values

lgbm_pred = booster.predict(X_val)
X_val_rf  = X_val.assign(lgbm_pred=lgbm_pred)
our_pred  = lgbm_pred + rf.predict(X_val_rf)

our_mae_full = mean_absolute_error(y_val, our_pred)
logger.info(
    f"[Roma Sud] MOS attuale (LGBM+RF) — MAE su TUTTO il val set: "
    f"{our_mae_full:.4f}°C ({len(y_val)} punti)"
)

# ─────────────────────────────────────────────────────────────────────────────
# 3. Serie osservata reale (contesto TimesFM) + covariata nota (ERA5)
# ─────────────────────────────────────────────────────────────────────────────
# target_temperature a T = METAR osservato a T + HORIZON_HOURS.
# Ricostruiamo la serie osservata vera allineata a obs_time = T + horizon.
obs_source = pd.concat([train_st, val_st], ignore_index=True)[["recorded_at", target_col]].copy()
obs_source["obs_time"] = obs_source["recorded_at"] + pd.Timedelta(hours=HORIZON_HOURS)
obs_series = (
    obs_source.rename(columns={target_col: "temp_obs"})[["obs_time", "temp_obs"]]
    .dropna()
    .drop_duplicates("obs_time")
    .sort_values("obs_time")
    .set_index("obs_time")["temp_obs"]
    .asfreq("h")  # forza griglia oraria regolare; introduce NaN nei buchi METAR
)

# Covariata nota = ERA5 "temperature" grezza a T (stessa colonna che il MOS
# usa come input X). È lecito trattarla come nota anche nel futuro qui,
# perché ERA5 è rianalisi storica — esattamente come fa il MOS stesso.
covariate_series = (
    pd.concat([train_st, val_st], ignore_index=True)[["recorded_at", "temperature"]]
    .drop_duplicates("recorded_at")
    .sort_values("recorded_at")
    .set_index("recorded_at")["temperature"]
    .asfreq("h")
)

# ─────────────────────────────────────────────────────────────────────────────
# 4. Sottocampionamento dei punti di valutazione
# ─────────────────────────────────────────────────────────────────────────────
rng = np.random.default_rng(RANDOM_SEED)
n_pick = min(N_EVAL_POINTS, len(val_st))
eval_idx = np.sort(rng.choice(len(val_st), size=n_pick, replace=False))

eval_times = val_st["recorded_at"].iloc[eval_idx].reset_index(drop=True)
eval_truth = val_st[target_col].iloc[eval_idx].to_numpy()
eval_our_pred = our_pred[eval_idx]

our_mae_sub = mean_absolute_error(eval_truth, eval_our_pred)
logger.info(
    f"[Roma Sud] MOS attuale — MAE sul sottocampione "
    f"({n_pick} punti): {our_mae_sub:.4f}°C"
)

# ─────────────────────────────────────────────────────────────────────────────
# 5. TimesFM-3 zero-shot
# ─────────────────────────────────────────────────────────────────────────────
import torch
import timesfm

torch.set_float32_matmul_precision("high")

# TODO-VERIFICA (1): nome esatto classe/factory per il checkpoint 3.0.
# Controllare `dir(timesfm)` dopo l'installazione; se non esiste ancora una
# classe dedicata TimesFM_3_..., potrebbe servire lo stesso pattern usato
# per 2.5 (TimesFM_2p5_200M_torch.from_pretrained) puntato al repo 3.0.
model = timesfm.TimesFM_3_0_torch.from_pretrained("google/timesfm-3.0-pytorch")
model.compile(
    timesfm.ForecastConfig(
        max_context=CONTEXT_HOURS,
        max_horizon=HORIZON_HOURS,
        normalize_inputs=True,
    )
)


def timesfm_predict_one(t: pd.Timestamp) -> float | None:
    """Previsione zero-shot per l'ora t, usando solo storico reale fino a t-horizon."""
    ctx_end = t - pd.Timedelta(hours=HORIZON_HOURS)
    ctx_start = ctx_end - pd.Timedelta(hours=CONTEXT_HOURS - 1)

    context = obs_series.loc[ctx_start:ctx_end]
    if len(context) < CONTEXT_HOURS or context.isna().mean() > 0.1:
        return None  # contesto incompleto o troppi buchi METAR: salta il punto

    context_filled = context.interpolate(limit=6).ffill().bfill()
    if context_filled.isna().any():
        return None

    cov = covariate_series.loc[ctx_start:t].interpolate(limit=6).ffill().bfill()
    if cov.isna().any() or len(cov) < len(context_filled) + HORIZON_HOURS:
        return None

    # TODO-VERIFICA (2): nome del parametro per la covariata nota (past-future
    # covariate). Controllare gli esempi "xreg" del repo TimesFM prima di
    # lanciare su tutto il sottocampione — qui è solo un placeholder plausibile.
    point_forecast, _ = model.forecast(
        horizon=HORIZON_HOURS,
        inputs=[context_filled.to_numpy()],
        dynamic_numerical_covariates={"era5_temperature": [cov.to_numpy()]},
    )
    return float(np.asarray(point_forecast)[0, -1])


logger.info(f"TimesFM-3: inferenza zero-shot su {n_pick} punti (loop non ottimizzato)...")
timesfm_preds = [timesfm_predict_one(pd.Timestamp(t)) for t in eval_times]

mask_valid = np.array([p is not None for p in timesfm_preds])
n_skipped = int((~mask_valid).sum())
if n_skipped:
    logger.warning(
        f"TimesFM-3: {n_skipped}/{n_pick} punti saltati "
        f"(buchi nel contesto osservato o nella covariata)"
    )

y_true_tfm = eval_truth[mask_valid]
y_pred_tfm = np.array([p for p in timesfm_preds if p is not None])
tfm_mae = mean_absolute_error(y_true_tfm, y_pred_tfm) if len(y_true_tfm) else float("nan")

# stesso identico sottoinsieme per un confronto alla pari
our_pred_matched = eval_our_pred[mask_valid]
our_mae_matched = (mean_absolute_error(y_true_tfm, our_pred_matched)
                    if len(y_true_tfm) else float("nan"))

# ─────────────────────────────────────────────────────────────────────────────
# 6. Riepilogo
# ─────────────────────────────────────────────────────────────────────────────
sep = "=" * 62
print(f"\n{sep}")
print(f"Roma Sud (id={STATION_ID}) — target {TARGET}, T+{HORIZON_HOURS}h")
print(f"Val set completo    : {len(val_st)} righe")
print(f"Punti confrontati   : {len(y_true_tfm)} / {n_pick} campionati "
      f"({n_skipped} scartati per buchi dati)")
print(f"{'-' * 62}")
print(f"{'Modello':38s} {'MAE (°C)':>10s}")
print(f"{'MOS attuale (LGBM+RF), full val':38s} {our_mae_full:>10.4f}")
print(f"{'MOS attuale, stesso sottocampione TimesFM':38s} {our_mae_matched:>10.4f}")
print(f"{'TimesFM-3 zero-shot':38s} {tfm_mae:>10.4f}")
print(f"{sep}\n")
