"""
forecast.py — Addestramento LightGBM su dataset storico ERA5/METAR
Blocco 3 - Fase 2

Addestra un modello LightGBM separato per ogni target meteo.
Questa fase inizia da target_temperature; gli altri vengono dopo.

Input:  file .parquet prodotto da historical.py
Output: model/lgbm_{target}.pkl          — modello ricaricabile
        model/feature_importance_{target}.json — gain per feature

Utilizzo:
    python3 forecast.py --data data/training.parquet --target temperature
    python3 forecast.py --data data/training.parquet --target temperature --horizon 3
    python3 forecast.py --data data/training.parquet --target temperature --no-db
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from typing import Optional

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_squared_error

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configurazione
# ─────────────────────────────────────────────────────────────────────────────

# Colonne sempre escluse dalle feature X.
#
# station_id   → è un intero arbitrario (chiave DB), non un segnale predittivo.
#                Le proprietà fisiche della stazione sono già codificate nelle
#                colonne orografiche: altitude, dist_sea_km, microclima_*, ecc.
# recorded_at  → timestamp: già estratto in hour_sin/cos, doy_sin/cos, month.
# horizon_hours → costante all'interno di un run (nessun segnale da imparare).
# metar_icao   → stringa identificativa, non numerica e non predittiva.
NON_FEATURE_COLS: frozenset[str] = frozenset({
    "recorded_at",
    "station_id",
    "horizon_hours",
    "metar_icao",
})

# Iperparametri LightGBM di default per regressione.
# num_leaves=63 dà più capacità del default (31) su ~76 feature.
DEFAULT_LGB_PARAMS: dict = {
    "objective":        "regression",
    "metric":           ["mae", "rmse"],
    "learning_rate":    0.05,
    "num_leaves":       63,
    "min_data_in_leaf": 20,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq":     5,
    "lambda_l1":        0.1,
    "lambda_l2":        0.1,
    "verbose":          -1,
}


# ─────────────────────────────────────────────────────────────────────────────
# I/O dataset
# ─────────────────────────────────────────────────────────────────────────────

def load_dataset(path: str) -> pd.DataFrame:
    """Carica il parquet (o CSV) prodotto da historical.py."""
    if path.endswith(".parquet"):
        df = pd.read_parquet(path)
        # Alcune colonne orografiche (dist_sea_km, dist_center_km, bearing_sea, ...)
        # vengono serializzate come object dal parquet writer — LightGBM le rifiuta.
        obj_cols = df.select_dtypes(include="object").columns.difference(["recorded_at"])
        df[obj_cols] = df[obj_cols].apply(pd.to_numeric, errors="coerce")
    else:
        df = pd.read_csv(path)
    df["recorded_at"] = pd.to_datetime(df["recorded_at"])
    logger.info(f"Dataset: {len(df):,} righe × {len(df.columns)} colonne  [{path}]")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Split temporale
# ─────────────────────────────────────────────────────────────────────────────

def temporal_split(
    df: pd.DataFrame, val_frac: float = 0.2
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Split train/val rigorosamente temporale.

    MAI random shuffle: le feature lag e rolling sono calcolate su osservazioni
    passate. Un random split farebbe finire osservazioni "future" nel training
    set mentre il loro lag guarda a osservazioni che cadono nel val set →
    leakage diretto.

    Strategia: ordina per recorded_at, taglia a (1 - val_frac).
    Il taglio è globale (multi-stazione): tutte le stazioni passano al val
    dopo la stessa data soglia, il che riflette lo scenario reale di
    addestramento su dati storici e test sul futuro.

    Args:
        df:       DataFrame ordinato (o no, viene ordinato internamente).
        val_frac: frazione di val set (default 0.2 = 20%).

    Returns:
        (train_df, val_df)
    """
    df  = df.sort_values("recorded_at").reset_index(drop=True)
    cut = int(len(df) * (1 - val_frac))
    return df.iloc[:cut].copy(), df.iloc[cut:].copy()


# ─────────────────────────────────────────────────────────────────────────────
# Feature selection
# ─────────────────────────────────────────────────────────────────────────────

def get_feature_cols(df: pd.DataFrame, target: str) -> list[str]:
    """
    Ritorna le colonne da usare come feature X.

    Esclude:
      - NON_FEATURE_COLS (metadati e ID)
      - tutte le colonne target_* (label di training, mai feature)
    """
    exclude = NON_FEATURE_COLS | {c for c in df.columns if c.startswith("target_")}
    return [c for c in df.columns if c not in exclude]


# ─────────────────────────────────────────────────────────────────────────────
# Training
# ─────────────────────────────────────────────────────────────────────────────

def train_lgbm(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    n_estimators: int = 1000,
    early_stopping_rounds: int = 50,
    params: Optional[dict] = None,
) -> lgb.Booster:
    """
    Addestra LightGBM con early stopping su val-MAE.

    Args:
        X_train, y_train: dati di training.
        X_val,   y_val:   dati di validazione (solo per early stopping e metriche).
        n_estimators:     numero massimo di alberi.
        early_stopping_rounds: stop se val-MAE non migliora per N round consecutivi.
        params:           override degli iperparametri LightGBM.

    Returns:
        Booster addestrato al best_iteration (non all'ultimo round).
    """
    lgb_params = {**DEFAULT_LGB_PARAMS, **(params or {})}

    dtrain = lgb.Dataset(X_train, label=y_train, feature_name=list(X_train.columns))
    dval   = lgb.Dataset(X_val,   label=y_val,   reference=dtrain)

    callbacks = [
        lgb.early_stopping(stopping_rounds=early_stopping_rounds, verbose=False),
        lgb.log_evaluation(period=100),
    ]

    booster = lgb.train(
        lgb_params,
        dtrain,
        num_boost_round=n_estimators,
        valid_sets=[dtrain, dval],
        valid_names=["train", "val"],
        callbacks=callbacks,
    )
    return booster


# ─────────────────────────────────────────────────────────────────────────────
# Metriche
# ─────────────────────────────────────────────────────────────────────────────

def _metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    mae  = float(mean_absolute_error(y_true, y_pred))
    rmse = float(np.sqrt(mean_squared_error(y_true, y_pred)))
    return {"mae": round(mae, 4), "rmse": round(rmse, 4)}


# ─────────────────────────────────────────────────────────────────────────────
# Persistenza
# ─────────────────────────────────────────────────────────────────────────────

def save_model(booster: lgb.Booster, target: str, model_dir: str = "model") -> str:
    """
    Salva il Booster nel formato nativo LightGBM (testo).

    Più robusto di pickle: non rompe al cambio di versione della libreria
    e il file è leggibile/ispezionabile come testo.

    Ricarica:
        booster = lgb.Booster(model_file="model/lgbm_temperature.txt")
        preds   = booster.predict(X_new)
    """
    os.makedirs(model_dir, exist_ok=True)
    path = os.path.join(model_dir, f"lgbm_{target}.txt")
    booster.save_model(path)
    return path


def save_feature_importance(
    booster: lgb.Booster,
    feature_names: list[str],
    target: str,
    horizon_hours: int,
    model_dir: str = "model",
) -> str:
    """
    Salva l'importanza feature (gain) in JSON, ordinata per importanza decrescente.

    Il gain misura il miglioramento totale dell'obiettivo attribuito a ciascuna
    feature across tutti gli split: è più stabile del conteggio degli split.
    """
    gains   = booster.feature_importance(importance_type="gain")
    ranked  = sorted(zip(feature_names, gains.tolist()), key=lambda x: x[1], reverse=True)
    payload = {
        "target":          target,
        "horizon_hours":   horizon_hours,
        "importance_type": "gain",
        "features": [{"name": n, "gain": round(g, 4)} for n, g in ranked],
    }
    os.makedirs(model_dir, exist_ok=True)
    path = os.path.join(model_dir, f"feature_importance_{target}.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Orchestratore principale
# ─────────────────────────────────────────────────────────────────────────────

def run(
    data_path: str,
    target: str,
    horizon: int,
    val_frac: float = 0.2,
    model_dir: str = "model",
    n_estimators: int = 1000,
    early_stopping: int = 50,
    skip_db: bool = False,
    model_version: Optional[str] = None,
) -> dict:
    """
    Load → split → train → evaluate → save → log metriche.

    Args:
        data_path:      percorso file .parquet o .csv (da historical.py).
        target:         variabile target (es. "temperature").
        horizon:        orizzonte previsionale N usato per generare il dataset.
        val_frac:       frazione val set (default 0.2).
        model_dir:      directory output per modelli e importance.
        n_estimators:   massimo numero alberi LightGBM.
        early_stopping: round senza miglioramento prima dello stop.
        skip_db:        se True, salta l'insert su Supabase.
        model_version:  etichetta per model_metrics; se None genera un timestamp
                        automatico "v%Y%m%d_%H%M" al momento del training.

    Returns:
        dict con train_mae, train_rmse, val_mae, val_rmse, best_iteration, n_features.
    """
    if model_version is None:
        model_version = datetime.utcnow().strftime("v%Y%m%d_%H%M")

    target_col = f"target_{target}"

    # ── Carica e valida ──────────────────────────────────────────────────────
    df = load_dataset(data_path)
    if target_col not in df.columns:
        available = [c for c in df.columns if c.startswith("target_")]
        raise ValueError(
            f"Colonna '{target_col}' non trovata nel dataset. "
            f"Target disponibili: {available}"
        )

    df = df.dropna(subset=[target_col]).reset_index(drop=True)
    logger.info(f"Righe dopo dropna({target_col}): {len(df):,}")

    # ── Split temporale ──────────────────────────────────────────────────────
    train_df, val_df = temporal_split(df, val_frac=val_frac)

    train_start = train_df["recorded_at"].min().date()
    train_end   = train_df["recorded_at"].max().date()
    val_start   = val_df["recorded_at"].min().date()
    val_end     = val_df["recorded_at"].max().date()

    # ── Feature ──────────────────────────────────────────────────────────────
    feature_cols = get_feature_cols(df, target)

    X_train = train_df[feature_cols]
    y_train = train_df[target_col]
    X_val   = val_df[feature_cols]
    y_val   = val_df[target_col]

    logger.info(
        f"Split: train {len(X_train):,} ({train_start}→{train_end}), "
        f"val {len(X_val):,} ({val_start}→{val_end}), "
        f"feature {len(feature_cols)}"
    )

    # ── Training ─────────────────────────────────────────────────────────────
    booster = train_lgbm(
        X_train, y_train, X_val, y_val,
        n_estimators=n_estimators,
        early_stopping_rounds=early_stopping,
    )
    best_iter = booster.best_iteration

    # ── Metriche ─────────────────────────────────────────────────────────────
    train_m = _metrics(y_train.values, booster.predict(X_train, num_iteration=best_iter))
    val_m   = _metrics(y_val.values,   booster.predict(X_val,   num_iteration=best_iter))

    # ── Salvataggio ──────────────────────────────────────────────────────────
    model_path      = save_model(booster, target, model_dir)
    importance_path = save_feature_importance(
        booster, feature_cols, target, horizon, model_dir
    )

    # ── Riepilogo a schermo ───────────────────────────────────────────────────
    sep = "=" * 62
    print(f"\n{sep}")
    print(f"Target       : {target}  |  Orizzonte: T+{horizon}h")
    print(f"Train        : {len(X_train):,} righe  ({train_start} → {train_end})")
    print(f"Val          : {len(X_val):,} righe  ({val_start} → {val_end})")
    print(f"Feature      : {len(feature_cols)}")
    print(f"Best iter    : {best_iter} / {n_estimators}")
    print(f"{'-' * 62}")
    print(f"{'':22s} {'train':>10s} {'val':>10s}")
    print(f"{'MAE':22s} {train_m['mae']:>10.4f} {val_m['mae']:>10.4f}")
    print(f"{'RMSE':22s} {train_m['rmse']:>10.4f} {val_m['rmse']:>10.4f}")
    print(f"{'-' * 62}")
    gains  = booster.feature_importance(importance_type="gain")
    top10  = sorted(zip(feature_cols, gains), key=lambda x: x[1], reverse=True)[:10]
    print("Top-10 feature importance (gain):")
    for rank, (name, gain) in enumerate(top10, 1):
        print(f"  {rank:2d}. {name:<38s} {gain:>12,.1f}")
    print(f"{'-' * 62}")
    print(f"Modello      : {model_path}")
    print(f"Importance   : {importance_path}")

    # ── Supabase ─────────────────────────────────────────────────────────────
    if not skip_db:
        try:
            from db import insert_model_metrics
            metric_id = insert_model_metrics(
                target=target,
                horizon_hours=horizon,
                train_mae=train_m["mae"],
                train_rmse=train_m["rmse"],
                val_mae=val_m["mae"],
                val_rmse=val_m["rmse"],
                n_train=len(X_train),
                n_val=len(X_val),
                feature_count=len(feature_cols),
                best_iteration=best_iter,
                model_version=model_version,
            )
            print(f"Supabase     : model_metrics id={metric_id}")
        except Exception as exc:
            logger.warning(f"Supabase insert fallito (non bloccante): {exc}")
    else:
        print("Supabase     : skipped (--no-db)")

    print(f"{sep}\n")

    return {
        "train_mae":      train_m["mae"],
        "train_rmse":     train_m["rmse"],
        "val_mae":        val_m["mae"],
        "val_rmse":       val_m["rmse"],
        "best_iteration": best_iter,
        "n_features":     len(feature_cols),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    ap = argparse.ArgumentParser(description="Addestra LightGBM su dataset ERA5/METAR")
    ap.add_argument("--data",     required=True,
                    help="File .parquet o .csv da historical.py")
    ap.add_argument("--target",   default="temperature",
                    choices=["temperature", "wind_speed", "wind_direction",
                             "humidity", "pressure"],
                    help="Variabile target (default: temperature)")
    ap.add_argument("--horizon",  type=int, default=1,
                    help="Orizzonte previsionale N usato per costruire il dataset (default: 1)")
    ap.add_argument("--val-frac", type=float, default=0.2,
                    help="Frazione val set 0–1 (default: 0.2)")
    ap.add_argument("--n-estimators",    type=int, default=1000,
                    help="Massimo numero alberi (default: 1000)")
    ap.add_argument("--early-stopping",  type=int, default=50,
                    help="Early stopping rounds (default: 50)")
    ap.add_argument("--model-dir",       default="model",
                    help="Directory output per .txt e .json (default: model/)")
    ap.add_argument("--model-version",   default=None,
                    help="Versione modello per Supabase (default: timestamp automatico v%%Y%%m%%d_%%H%%M)")
    ap.add_argument("--no-db", action="store_true",
                    help="Salta l'inserimento metriche su Supabase")
    args = ap.parse_args()

    run(
        data_path=args.data,
        target=args.target,
        horizon=args.horizon,
        val_frac=args.val_frac,
        model_dir=args.model_dir,
        n_estimators=args.n_estimators,
        early_stopping=args.early_stopping,
        skip_db=args.no_db,
        model_version=args.model_version,
    )
