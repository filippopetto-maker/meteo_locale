"""
benchmark_timesfm3_vs_mos.py — Benchmark locale: pipeline MOS attuale vs TimesFM-3 zero-shot
Stazione: Roma Sud (id=3) | target: temperature | orizzonte: T+1h

v2 — API timesfm3 verificata (TimesFM3Evaluator/ModelConfig/predict_batch),
inferenza in batch invece che punto-per-punto, introspezione automatica del
parametro covariata con fallback esplicito se non riconosciuto.

Non fa parte della pipeline di produzione (inference.py / GitHub Actions).
Script sperimentale — non committare i dati generati, solo il codice
(coerente con la convenzione già in uso per i file dati auto-generati).

Uso:
    cd ~/Desktop/meteo_locale
    conda activate meteo
    pip install "timesfm[torch,xreg]"   # scarica anche il checkpoint al primo avvio
    python3 benchmark_timesfm3_vs_mos.py

⚠️ LICENZA: i pesi google/timesfm-3.0-pytorch sono sotto
timesfm-non-commercial-license-v1.0 (non-commercial, non-production).
Questo script è un test locale/offline. NON integrare in inference.py /
GitHub Actions senza rivalutare la licenza rispetto all'uso del progetto.
"""

from __future__ import annotations

import inspect
import logging
import pickle
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import torch
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
DATA_PATH        = "data/training_10y_h1.parquet"
MODEL_DIR        = Path("model")
STATION_ID       = 3          # Roma Sud — nel training set originale, no ARSIAL
TARGET           = "temperature"
HORIZON_HOURS    = 1
VAL_FRAC         = 0.2        # deve coincidere con forecast.py / correttore.py
CONTEXT_HOURS    = 512        # ~21 giorni di storico reale come contesto TimesFM
N_EVAL_POINTS    = 300        # sottocampionamento del val set
PREDICT_BATCH_SZ = 32         # dimensione batch per predict_batch (CPU-friendly)
RANDOM_SEED      = 42

TIMESFM_CHECKPOINT = "google/timesfm-3.0-pytorch"

# ─────────────────────────────────────────────────────────────────────────────
# 1. Dataset + split IDENTICO alla produzione
# ─────────────────────────────────────────────────────────────────────────────
df = load_dataset(DATA_PATH)
train_df, val_df = temporal_split(df, val_frac=VAL_FRAC)

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
obs_source = pd.concat([train_st, val_st], ignore_index=True)[["recorded_at", target_col]].copy()
obs_source["obs_time"] = obs_source["recorded_at"] + pd.Timedelta(hours=HORIZON_HOURS)
obs_series = (
    obs_source.rename(columns={target_col: "temp_obs"})[["obs_time", "temp_obs"]]
    .dropna()
    .drop_duplicates("obs_time")
    .sort_values("obs_time")
    .set_index("obs_time")["temp_obs"]
    .asfreq("h")
)

covariate_series = (
    pd.concat([train_st, val_st], ignore_index=True)[["recorded_at", "temperature"]]
    .drop_duplicates("recorded_at")
    .sort_values("recorded_at")
    .set_index("recorded_at")["temperature"]
    .asfreq("h")
)

# ─────────────────────────────────────────────────────────────────────────────
# 4. Sottocampionamento + costruzione finestre di contesto (per il batch)
# ─────────────────────────────────────────────────────────────────────────────
rng = np.random.default_rng(RANDOM_SEED)
n_pick = min(N_EVAL_POINTS, len(val_st))
eval_idx = np.sort(rng.choice(len(val_st), size=n_pick, replace=False))

eval_times    = val_st["recorded_at"].iloc[eval_idx].reset_index(drop=True)
eval_truth    = val_st[target_col].iloc[eval_idx].to_numpy()
eval_our_pred = our_pred[eval_idx]

our_mae_sub = mean_absolute_error(eval_truth, eval_our_pred)
logger.info(f"[Roma Sud] MOS attuale — MAE sul sottocampione ({n_pick} punti): {our_mae_sub:.4f}°C")

contexts = []
cov_context_future = []   # covariata su contesto + horizon, per punto valido
kept_mask = np.zeros(n_pick, dtype=bool)

for i, t in enumerate(eval_times):
    t = pd.Timestamp(t)
    ctx_end   = t - pd.Timedelta(hours=HORIZON_HOURS)
    ctx_start = ctx_end - pd.Timedelta(hours=CONTEXT_HOURS - 1)

    ctx = obs_series.loc[ctx_start:ctx_end]
    if len(ctx) < CONTEXT_HOURS or ctx.isna().mean() > 0.1:
        continue
    ctx_filled = ctx.interpolate(limit=6).ffill().bfill()
    if ctx_filled.isna().any():
        continue

    cov = covariate_series.loc[ctx_start:t].interpolate(limit=6).ffill().bfill()
    if cov.isna().any() or len(cov) < len(ctx_filled) + HORIZON_HOURS:
        continue

    contexts.append(ctx_filled.to_numpy(dtype=np.float32))
    # shape (1, contesto+horizon): 1 canale covariata esplicito, come
    # nell'esempio multivariato del README upstream (canali, lunghezza).
    cov_context_future.append(cov.to_numpy(dtype=np.float32).reshape(1, -1))
    kept_mask[i] = True

n_skipped = n_pick - len(contexts)
if n_skipped:
    logger.warning(f"TimesFM-3: {n_skipped}/{n_pick} punti scartati (buchi nel contesto o covariata)")

y_true_tfm = eval_truth[kept_mask]
our_pred_matched = eval_our_pred[kept_mask]

# ─────────────────────────────────────────────────────────────────────────────
# 5. TimesFM-3 — API verificata: timesfm3.TimesFM3Evaluator / ModelConfig
# ─────────────────────────────────────────────────────────────────────────────
from timesfm3 import TimesFM3Evaluator, ModelConfig  # noqa: E402

device = "cuda" if torch.cuda.is_available() else (
    "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else "cpu"
)
logger.info(f"TimesFM-3: device={device}")

config = ModelConfig(
    checkpoint_path=TIMESFM_CHECKPOINT,
    per_core_batch_size=PREDICT_BATCH_SZ,
    device=device,
)
forecaster = TimesFM3Evaluator(config)

# ── Introspezione: scopriamo il vero nome del parametro covariata ──────────
sig = inspect.signature(forecaster.predict_batch)
param_names = list(sig.parameters.keys())
logger.info(f"predict_batch signature: {sig}")

# "past_future_covariates" è il nome verificato nel README upstream
# (google-research/timesfm, esempio "Multivariate Forecasting with
# Covariates"); gli altri restano come fallback nel caso l'API cambi.
# Sulla forma dell'array per un batch di serie univariate (non documentata
# esplicitamente: l'esempio upstream mostra solo il caso multivariato con
# shape (canali, contesto+horizon)) vedi lo shape_attempts qui sotto.
CANDIDATE_COV_PARAMS = [
    "past_future_covariates", "past_and_future_covariates",
    "dynamic_numerical_covariates", "future_covariates", "covariates",
    "xreg", "past_covariates",
]
cov_param = next((p for p in CANDIDATE_COV_PARAMS if p in param_names), None)

used_covariates = False
outputs = None

if cov_param is not None:
    logger.info(f"Parametro covariata riconosciuto: '{cov_param}' — tentativo con covariate ERA5")
    # Non è documentato se predict_batch, per un batch di serie univariate,
    # voglia la covariata come (1, T) esplicito (coerente con l'esempio
    # multivariato del README) o come vettore piatto (T,). Proviamo prima
    # la forma 2D e, solo su un fallimento che sembra di shape, ripieghiamo
    # sulla forma 1D piatta come seconda ipotesi.
    shape_attempts = [
        ("2D (1, T)", cov_context_future),
        ("1D piatta (T,)", [c.reshape(-1) for c in cov_context_future]),
    ]
    for shape_label, cov_batch in shape_attempts:
        try:
            kwargs = {cov_param: cov_batch}
            outputs = list(forecaster.predict_batch(
                contexts, horizon=HORIZON_HOURS, return_quantiles=False, **kwargs
            ))
            used_covariates = True
            logger.info(f"Covariate accettate con forma {shape_label}")
            break
        except Exception as exc:
            logger.warning(f"Forma {shape_label} fallita ({exc!r})")
            outputs = None

    if outputs is None:
        logger.warning("Entrambe le forme covariata fallite — ripiego su zero-shot univariato")

if outputs is None:
    logger.info("Esecuzione zero-shot SENZA covariate (baseline univariato)")
    outputs = list(forecaster.predict_batch(
        contexts, horizon=HORIZON_HOURS, return_quantiles=False
    ))

y_pred_tfm = np.array([np.asarray(o.forecast).reshape(-1)[-1] for o in outputs], dtype=np.float64)

tfm_mae = mean_absolute_error(y_true_tfm, y_pred_tfm) if len(y_true_tfm) else float("nan")
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
print(f"TimesFM-3 covariate : {'usate (' + cov_param + ')' if used_covariates else 'NON usate (fallback univariato)'}")
print(f"{'-' * 62}")
print(f"{'Modello':38s} {'MAE (°C)':>10s}")
print(f"{'MOS attuale (LGBM+RF), full val':38s} {our_mae_full:>10.4f}")
print(f"{'MOS attuale, stesso sottocampione TimesFM':38s} {our_mae_matched:>10.4f}")
print(f"{'TimesFM-3 zero-shot':38s} {tfm_mae:>10.4f}")
print(f"{sep}\n")
