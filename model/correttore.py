"""
correttore.py — Secondo stadio: Random Forest correttore dei residui LightGBM
Blocco 3 - Fase 3

Implementa uno stacking a due livelli (MOS a due stadi):
  Stadio 1 — LightGBM: predizione grezza da ERA5       → forecast.py
  Stadio 2 — RF:       correzione dei residui sistematici → questo script

Perché funziona: LightGBM cattura la maggior parte del segnale, ma lascia
residui con pattern sistematici (es. sottostima in estate pomeriggio vicino
alla costa, sovrastima di notte nei canyon urbani). Il RF impara questi
pattern dai residui del train set e li compensa sul val.

Garanzia no-leakage:
  • Split train/val: stessa funzione di forecast.py → cut identico alla riga.
  • RF addestrato ESCLUSIVAMENTE sui residui del train set.
  • Val = black box: mai visto durante il training, usato solo per valutazione.

Utilizzo:
  python3 model/correttore.py --data data/training.parquet --target temperature
  python3 model/correttore.py --data data/training.parquet --target temperature --no-db
"""

from __future__ import annotations

import argparse
import logging
import pickle
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Lo script vive in model/, ma forecast.py e db.py sono nella project root
# un livello sopra. Aggiunge la root a sys.path così Python trova i moduli
# anche se lo script è in una subdirectory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import lightgbm as lgb
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error

# Importa le utility di forecast.py: garantisce split identico (stessa
# funzione, stessa logica di sort + cut) senza duplicare codice.
from forecast import load_dataset, temporal_split, get_feature_cols

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Metriche
# ─────────────────────────────────────────────────────────────────────────────

def _metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    mae  = float(mean_absolute_error(y_true, y_pred))
    rmse = float(np.sqrt(mean_squared_error(y_true, y_pred)))
    return {"mae": round(mae, 4), "rmse": round(rmse, 4)}


# ─────────────────────────────────────────────────────────────────────────────
# Persistenza RF
# ─────────────────────────────────────────────────────────────────────────────

def save_rf(rf: RandomForestRegressor, target: str, model_dir: Path) -> Path:
    """
    Salva il RandomForest con pickle.

    sklearn non ha un formato nativo testuale equivalente a lgb.save_model();
    pickle è lo standard de facto per i modelli sklearn.

    Ricarica a inferenza:
        import pickle
        with open("model/rf_correttore_temperature.pkl", "rb") as f:
            rf = pickle.load(f)
        correzione = rf.predict(X_val_rf)   # X_val_rf include colonna lgbm_pred
        pred_finale = lgbm_pred + correzione
    """
    model_dir.mkdir(parents=True, exist_ok=True)
    path = model_dir / f"rf_correttore_{target}.pkl"
    with path.open("wb") as f:
        pickle.dump(rf, f, protocol=pickle.HIGHEST_PROTOCOL)
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
    skip_db: bool = False,
    model_version: Optional[str] = None,
) -> dict:
    """
    Pipeline LGBM → residui → RF → valutazione → save → log.

    Args:
        data_path:     file .parquet o .csv da historical.py.
        target:        variabile target (es. "temperature").
        horizon:       orizzonte previsionale N (usato solo per il log).
        val_frac:      frazione val set — deve coincidere con forecast.py (default 0.2).
        model_dir:     directory con lgbm_*.txt; output rf_correttore_*.pkl.
        skip_db:       salta l'insert su Supabase.
        model_version: etichetta per model_metrics; se None, auto-generata.

    Returns:
        dict con lgbm_val_mae, lgbm_val_rmse, corrected_val_mae,
        corrected_val_rmse, delta_mae, delta_rmse.
    """
    if model_version is None:
        model_version = "rf_" + datetime.utcnow().strftime("v%Y%m%d_%H%M")

    model_dir_path  = Path(model_dir)
    target_col      = f"target_{target}"
    lgbm_model_path = model_dir_path / f"lgbm_{target}.txt"

    # ── 1. Dataset e split (identico a forecast.py) ──────────────────────────
    df = load_dataset(data_path)
    if target_col not in df.columns:
        raise ValueError(
            f"Colonna '{target_col}' non trovata. "
            f"Target disponibili: {[c for c in df.columns if c.startswith('target_')]}"
        )
    df = df.dropna(subset=[target_col]).reset_index(drop=True)

    # temporal_split importata da forecast.py: sort su recorded_at,
    # cut = int(N * (1 - val_frac)) — righe identiche a forecast.py.
    train_df, val_df = temporal_split(df, val_frac=val_frac)
    feature_cols     = get_feature_cols(df, target)

    X_train = train_df[feature_cols]
    y_train = train_df[target_col].values
    X_val   = val_df[feature_cols]
    y_val   = val_df[target_col].values

    train_start = train_df["recorded_at"].min().date()
    train_end   = train_df["recorded_at"].max().date()
    val_start   = val_df["recorded_at"].min().date()
    val_end     = val_df["recorded_at"].max().date()

    logger.info(
        f"Split: train {len(X_train):,} ({train_start}→{train_end}), "
        f"val {len(X_val):,} ({val_start}→{val_end})"
    )

    # ── 2. Carica LGBM e predizioni ──────────────────────────────────────────
    if not lgbm_model_path.exists():
        raise FileNotFoundError(
            f"Modello LGBM non trovato: {lgbm_model_path}\n"
            f"Esegui prima: python3 forecast.py --data {data_path} --target {target}"
        )
    booster = lgb.Booster(model_file=str(lgbm_model_path))
    logger.info(f"LGBM caricato: {lgbm_model_path}")

    # lgb.Booster caricato da file: predict() usa automaticamente le N alberi
    # salvate al best_iteration (save_model() salva già il modello potato).
    lgbm_pred_train = booster.predict(X_train)
    lgbm_pred_val   = booster.predict(X_val)

    lgbm_train_m = _metrics(y_train, lgbm_pred_train)
    lgbm_val_m   = _metrics(y_val,   lgbm_pred_val)

    # ── 3. Residui sul solo train set ─────────────────────────────────────────
    # residuo = quanto LightGBM ha mancato, e in che direzione.
    # Il RF imparerà a predire questo valore e compensarlo.
    residui_train = y_train - lgbm_pred_train

    # ── 4. Feature matrix RF = feature ERA5 + lgbm_pred ──────────────────────
    # lgbm_pred come feature aggiuntiva: permette al RF di condizionarsi
    # su "quanto ha previsto LightGBM" → impara il bias in funzione del range
    # di previsione (es. LightGBM tende a sovrastimare quando prevede >30°C).
    X_train_rf = X_train.assign(lgbm_pred=lgbm_pred_train)
    X_val_rf   = X_val.assign(lgbm_pred=lgbm_pred_val)

    # ── 5. Training RF sui residui di train ───────────────────────────────────
    # Il val non entra mai qui: nessun leakage.
    #
    # Iperparametri scelti per performance + regolarizzazione:
    #   n_jobs=-1         → parallelismo multicore (senza questo: single-core,
    #                       ~10-20 min su 264k righe invece di ~18 s).
    #   max_depth=6       → alberi corti: i residui LGBM sono già piccoli,
    #                       alberi profondi overfitterebbero il rumore.
    #   min_samples_leaf=10 → regolarizzazione extra per stabilità leaf-wise.
    rf = RandomForestRegressor(
        n_estimators=200,
        max_depth=6,
        min_samples_leaf=10,
        n_jobs=-1,
        random_state=42,
    )
    rf.fit(X_train_rf, residui_train)
    logger.info(
        "RF addestrato: n_estimators=200, max_depth=6, "
        "min_samples_leaf=10, n_jobs=-1, random_state=42"
    )

    # ── 6. Predizioni corrette ─────────────────────────────────────────────────
    corrected_train = lgbm_pred_train + rf.predict(X_train_rf)
    corrected_val   = lgbm_pred_val   + rf.predict(X_val_rf)

    rf_train_m = _metrics(y_train, corrected_train)
    rf_val_m   = _metrics(y_val,   corrected_val)

    delta_mae  = round(rf_val_m["mae"]  - lgbm_val_m["mae"],  4)
    delta_rmse = round(rf_val_m["rmse"] - lgbm_val_m["rmse"], 4)

    # ── 7. Salvataggio RF ─────────────────────────────────────────────────────
    rf_path = save_rf(rf, target, model_dir_path)

    # ── 8. Riepilogo ──────────────────────────────────────────────────────────
    unit     = {"temperature": "°C", "wind_speed": "km/h",
                "wind_direction": "°", "humidity": "%", "pressure": "hPa"}.get(target, "")
    sep      = "=" * 62
    sign_mae  = "+" if delta_mae  >= 0 else ""
    sign_rmse = "+" if delta_rmse >= 0 else ""

    print(f"\n{sep}")
    print(f"Target       : {target}  |  Orizzonte: T+{horizon}h")
    print(f"Train        : {len(X_train):,} righe  ({train_start} → {train_end})")
    print(f"Val          : {len(X_val):,} righe  ({val_start} → {val_end})")
    print(f"RF params    : n_estimators=200, max_depth=6, "
          f"min_samples_leaf=10, n_jobs=-1, random_state=42")
    print(f"{'-' * 62}")
    print(f"{'':24s} {'train':>9s} {'val':>9s}")
    print(f"{'LGBM solo — MAE':24s} {lgbm_train_m['mae']:>9.4f} {lgbm_val_m['mae']:>9.4f}")
    print(f"{'LGBM solo — RMSE':24s} {lgbm_train_m['rmse']:>9.4f} {lgbm_val_m['rmse']:>9.4f}")
    print(f"{'LGBM + RF — MAE':24s} {rf_train_m['mae']:>9.4f} {rf_val_m['mae']:>9.4f}")
    print(f"{'LGBM + RF — RMSE':24s} {rf_train_m['rmse']:>9.4f} {rf_val_m['rmse']:>9.4f}")
    print(f"{'-' * 62}")
    print(f"LGBM solo  → val MAE:  {lgbm_val_m['mae']:.4f}{unit}")
    print(f"LGBM + RF  → val MAE:  {rf_val_m['mae']:.4f}{unit}"
          f"   (delta: {sign_mae}{delta_mae:.4f}{unit})")
    print(f"LGBM solo  → val RMSE: {lgbm_val_m['rmse']:.4f}{unit}")
    print(f"LGBM + RF  → val RMSE: {rf_val_m['rmse']:.4f}{unit}"
          f"   (delta: {sign_rmse}{delta_rmse:.4f}{unit})")
    print(f"{'-' * 62}")
    print(f"RF salvato : {rf_path}")

    # ── 9. Supabase ───────────────────────────────────────────────────────────
    if not skip_db:
        try:
            from db import insert_model_metrics
            metric_id = insert_model_metrics(
                target=target,
                horizon_hours=horizon,
                train_mae=rf_train_m["mae"],
                train_rmse=rf_train_m["rmse"],
                val_mae=rf_val_m["mae"],
                val_rmse=rf_val_m["rmse"],
                n_train=len(X_train),
                n_val=len(X_val),
                feature_count=len(feature_cols) + 1,  # +1 per lgbm_pred
                model_version=model_version,
            )
            print(f"Supabase   : model_metrics id={metric_id}")
        except Exception as exc:
            logger.warning(f"Supabase insert fallito (non bloccante): {exc}")
    else:
        print("Supabase   : skipped (--no-db)")

    print(f"{sep}\n")

    return {
        "lgbm_val_mae":       lgbm_val_m["mae"],
        "lgbm_val_rmse":      lgbm_val_m["rmse"],
        "corrected_val_mae":  rf_val_m["mae"],
        "corrected_val_rmse": rf_val_m["rmse"],
        "delta_mae":          delta_mae,
        "delta_rmse":         delta_rmse,
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

    ap = argparse.ArgumentParser(
        description="Addestra RF correttore dei residui LightGBM"
    )
    ap.add_argument("--data",     required=True,
                    help="File .parquet o .csv da historical.py")
    ap.add_argument("--target",   default="temperature",
                    choices=["temperature", "wind_speed", "wind_direction",
                             "humidity", "pressure"],
                    help="Variabile target (default: temperature)")
    ap.add_argument("--horizon",  type=int, default=1,
                    help="Orizzonte previsionale (default: 1)")
    ap.add_argument("--val-frac", type=float, default=0.2,
                    help="Frazione val set — deve coincidere con forecast.py (default: 0.2)")
    ap.add_argument("--model-dir", default="model",
                    help="Directory con lgbm_*.txt e output rf_correttore_*.pkl (default: model/)")
    ap.add_argument("--model-version", default=None,
                    help="Versione per Supabase (default: timestamp automatico rf_v%%Y%%m%%d_%%H%%M)")
    ap.add_argument("--no-db", action="store_true",
                    help="Salta l'inserimento metriche su Supabase")
    args = ap.parse_args()

    run(
        data_path=args.data,
        target=args.target,
        horizon=args.horizon,
        val_frac=args.val_frac,
        model_dir=args.model_dir,
        skip_db=args.no_db,
        model_version=args.model_version,
    )
