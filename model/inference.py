"""
inference.py — Previsione operativa real-time
Blocco 3 - Fase 4

Pipeline operativa:
  1. Per ciascuna stazione attiva, scarica le previsioni Open-Meteo
     (past_days=2 per warm-up di lag/rolling, forecast_days=2 per il futuro).
  2. Applica build_feature_matrix() come in training (stesse colonne, stesso ordine).
  3. Predice T+horizon con LightGBM (lgbm_temperature.txt).
  4. Se esiste rf_correttore_temperature.pkl, applica la correzione RF
     (corrected=True nel DB). Altrimenti, salva la previsione LGBM grezza
     (corrected=False).
  5. Inserisce la previsione su Supabase via db.insert_forecast().

Le previsioni di wind_speed / wind_direction sono pass-through dell'NWP
finché i modelli MOS dedicati non sono addestrati.

Utilizzo:
    python3 model/inference.py
    python3 model/inference.py --horizon 3
    python3 model/inference.py --dry-run
"""

from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")

import argparse
import json
import logging
import pickle
import sys
import time as _time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# Lo script vive in model/, ma forecast.py / db.py / features.py / historical.py
# sono nella project root un livello sopra. Aggiunge la root a sys.path.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import lightgbm as lgb
import pandas as pd
import requests

from features import build_feature_matrix
from forecast import get_feature_cols
from historical import ERA5_VARIABLES, ERA5_RENAME

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configurazione
# ─────────────────────────────────────────────────────────────────────────────

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Directory dei modelli (relativa alla project root).
MODEL_DIR = _PROJECT_ROOT / "model"


# ─────────────────────────────────────────────────────────────────────────────
# Fetch Open-Meteo Forecast
# ─────────────────────────────────────────────────────────────────────────────

def fetch_forecast(
    lat: float,
    lon: float,
    past_days: int = 2,
    forecast_days: int = 2,
    retries: int = 3,
) -> pd.DataFrame:
    """
    Scarica previsioni Open-Meteo (forecast API, non archive).

    past_days serve a popolare le feature lag/rolling che richiedono
    osservazioni delle ore precedenti (max lag = 6h, max rolling = 12h).

    Args:
        lat, lon:      coordinate della stazione.
        past_days:     giorni di storico (default 2).
        forecast_days: giorni di previsione futura (default 2).

    Returns:
        DataFrame con colonna 'recorded_at' (UTC naive) e le variabili
        rinominate secondo lo schema progetto (stessi nomi di historical.py).
    """
    params = {
        "latitude":      lat,
        "longitude":     lon,
        "hourly":        ",".join(ERA5_VARIABLES),
        "past_days":     past_days,
        "forecast_days": forecast_days,
        "timezone":      "UTC",
    }

    last_exc: Exception = RuntimeError("Nessun tentativo eseguito")
    for attempt in range(retries):
        try:
            r = requests.get(OPEN_METEO_FORECAST_URL, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            break
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries - 1:
                wait = 2 ** attempt
                logger.warning(
                    f"Open-Meteo tentativo {attempt + 1}/{retries} fallito: {exc}. "
                    f"Riprovo in {wait}s"
                )
                _time.sleep(wait)
    else:
        raise last_exc

    hourly = data["hourly"]
    df = pd.DataFrame({"recorded_at": pd.to_datetime(hourly["time"])})
    for var in ERA5_VARIABLES:
        if var in hourly:
            df[var] = hourly[var]

    df = df.rename(columns={k: v for k, v in ERA5_RENAME.items() if k in df.columns})
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Caricamento modelli (cache per evitare ricarichi in loop multi-stazione)
# ─────────────────────────────────────────────────────────────────────────────

_BOOSTER_CACHE: dict[Path, lgb.Booster] = {}
_RF_CACHE:      dict[Path, object]      = {}
_ARSIAL_BIAS:   dict | None             = None

# Mapping stazione progetto → stazione proxy ARSIAL per correzione bias mensile.
# Stazione id=3 (Roma Sud) esclusa: era nel training set, nessuna correzione.
ARSIAL_PROXY: dict[int, str] = {
    25: "FIUMICINO T. Lepre",        # Ostia Lido
    26: "ROMA P. Nona",              # EUR
    27: "ROMA Lanciani",             # Trastevere
    28: "S. GREGORIO DA SASSOLA",    # Tivoli
    29: "MONTECOMPATRI C. Mattia",   # Castelli Romani
    33: "ROMA Capocotta",            # Pratica di Mare
    34: "LADISPOLI",                 # Cerveteri Ladispoli
    35: "MONTEROTONDO G. Marozza",   # Saxa Rubra
    36: "FORMELLO",                  # Selva Nera
    37: "VELLETRI P. Lungo",         # Cisterna Latina
    38: "FORMELLO",                  # Bracciano (condiviso con Selva Nera)
}


def load_arsial_bias() -> dict:
    """
    Carica data/arsial_bias_table.json con i bias mensili ARSIAL–ERA5.
    Usa cache modulo-level per evitare ricarichi in loop multi-stazione.
    Ritorna dict vuoto se il file manca (fallback silenzioso — no crash).
    """
    global _ARSIAL_BIAS
    if _ARSIAL_BIAS is not None:
        return _ARSIAL_BIAS
    bias_path = _PROJECT_ROOT / "data" / "arsial_bias_table.json"
    try:
        with bias_path.open() as f:
            _ARSIAL_BIAS = json.load(f)
        logger.info(f"ARSIAL bias table caricata: {len(_ARSIAL_BIAS)} stazioni proxy")
    except Exception as exc:
        logger.warning(f"ARSIAL bias table non disponibile ({exc}) — nessuna correzione")
        _ARSIAL_BIAS = {}
    return _ARSIAL_BIAS


def load_lgbm(target: str) -> lgb.Booster:
    """Carica il booster LightGBM nativo (.txt) dalla cache o da disco."""
    path = MODEL_DIR / f"lgbm_{target}.txt"
    if path not in _BOOSTER_CACHE:
        if not path.exists():
            raise FileNotFoundError(
                f"Modello LGBM non trovato: {path}\n"
                f"Esegui prima: python3 forecast.py --target {target}"
            )
        _BOOSTER_CACHE[path] = lgb.Booster(model_file=str(path))
        logger.info(f"LGBM caricato: {path}")
    return _BOOSTER_CACHE[path]


def load_rf(target: str) -> Optional[object]:
    """
    Carica il correttore RF se esiste, altrimenti None (fallback a LGBM solo).
    """
    path = MODEL_DIR / f"rf_correttore_{target}.pkl"
    if path not in _RF_CACHE:
        if not path.exists():
            logger.info(f"RF correttore assente ({path.name}) — fallback LGBM solo")
            _RF_CACHE[path] = None
        else:
            with path.open("rb") as f:
                _RF_CACHE[path] = pickle.load(f)
            logger.info(f"RF correttore caricato: {path}")
    return _RF_CACHE[path]


# ─────────────────────────────────────────────────────────────────────────────
# Predizione per UNA stazione
# ─────────────────────────────────────────────────────────────────────────────

def predict_station(
    station: dict,
    horizon_hours: int,
    target: str = "temperature",
) -> Optional[dict]:
    """
    Genera la previsione T+horizon per una singola stazione.

    Returns:
        dict con forecast_at, valid_for, temperature, wind_speed, wind_direction,
        humidity, corrected, model_version  — pronto per db.insert_forecast().
        None se la stazione non ha dati sufficienti.
    """
    # ── 1. Fetch forecast NWP ────────────────────────────────────────────────
    df = fetch_forecast(station["lat"], station["lon"])
    if df.empty:
        logger.warning(f"[st.{station['id']}] Open-Meteo vuoto, skip")
        return None

    # ── 2. Feature engineering (stessi 5 strati di training) ─────────────────
    feat_df = build_feature_matrix(df, station)

    # ── 3. Allinea le colonne a quelle attese dal LGBM ───────────────────────
    booster      = load_lgbm(target)
    feature_cols = booster.feature_name()

    # Aggiungi colonne mancanti come NaN (es. microclima_* assenti su questa
    # stazione). LightGBM gestisce i NaN nativamente.
    for col in feature_cols:
        if col not in feat_df.columns:
            feat_df[col] = pd.NA

    # ── 4. Scegli la riga T_now e calcola valid_for = T_now + horizon ────────
    now_utc      = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    now_naive    = now_utc.replace(tzinfo=None)
    valid_for    = now_utc + timedelta(hours=horizon_hours)

    feat_df = feat_df.sort_values("recorded_at").reset_index(drop=True)
    # Usa la riga più vicina (≤) all'ora corrente: questa è la "snapshot" T
    # corrispondente all'input usato in training (ERA5 a T).
    eligible = feat_df[feat_df["recorded_at"] <= now_naive]
    if eligible.empty:
        logger.warning(
            f"[st.{station['id']}] Nessuna riga ≤ {now_naive}, "
            f"min disponibile: {feat_df['recorded_at'].min()}"
        )
        return None
    row = eligible.iloc[[-1]]   # DataFrame, non Series → mantiene 2D per predict()

    X = row[feature_cols].apply(pd.to_numeric, errors="coerce")

    # ── 5. LGBM predict ──────────────────────────────────────────────────────
    lgbm_pred = float(booster.predict(X)[0])

    # ── 6. Correttore RF se disponibile ──────────────────────────────────────
    rf = load_rf(target)
    if rf is not None:
        X_rf = X.assign(lgbm_pred=lgbm_pred)
        rf_correction = float(rf.predict(X_rf)[0])
        final_pred = lgbm_pred + rf_correction
        corrected  = True
    else:
        final_pred = lgbm_pred
        corrected  = False

    # ── 6b. Correzione ARSIAL bias mensile (stazioni 25–29) ─────────────────
    # bias_temp_med = ARSIAL − ERA5: positivo → zona più calda di ERA5.
    # Sommiamo il bias a final_pred perché il modello, addestrato su stazioni
    # pianura/costiere, sottostima sistematicamente le zone non nel training.
    if target == "temperature":
        arsial_proxy = ARSIAL_PROXY.get(station["id"])
        if arsial_proxy is not None:
            month_key = str(datetime.now(timezone.utc).month)
            try:
                bias = load_arsial_bias()[arsial_proxy]["monthly"][month_key]["bias_temp_med"]
                final_pred += bias
                logger.info(
                    f"[ARSIAL bias] st.{station['id']} {bias:+.3f}°C "
                    f"(mese {month_key}, stazione {arsial_proxy})"
                )
            except (KeyError, TypeError):
                pass  # JSON mancante o chiave assente — fallback silenzioso

    # ── 7. Pass-through NWP per i campi non ancora coperti dai modelli ───────
    nwp_at_horizon = df[df["recorded_at"] == valid_for.replace(tzinfo=None)]
    if not nwp_at_horizon.empty:
        nwp_row = nwp_at_horizon.iloc[0]
        wind_speed_nwp     = float(nwp_row.get("wind_speed",     float("nan")))
        wind_direction_nwp = float(nwp_row.get("wind_direction", float("nan")))
        humidity_nwp       = float(nwp_row.get("humidity",       float("nan")))
    else:
        wind_speed_nwp = wind_direction_nwp = humidity_nwp = float("nan")

    # Il target è la temperatura: gli altri campi sono NWP grezzo (corrected
    # si riferisce solo al campo temperature). Lo schema richiede tutti e tre.
    return {
        "forecast_at":    now_utc,
        "valid_for":      valid_for,
        "temperature":    round(final_pred, 2) if target == "temperature" else float("nan"),
        "wind_speed":     round(wind_speed_nwp, 2)     if pd.notna(wind_speed_nwp)     else None,
        "wind_direction": round(wind_direction_nwp, 1) if pd.notna(wind_direction_nwp) else None,
        "humidity":       round(humidity_nwp, 1)       if pd.notna(humidity_nwp)       else None,
        "corrected":      corrected,
        "lgbm_pred":      round(lgbm_pred, 2),  # solo per logging dry-run
    }


# ─────────────────────────────────────────────────────────────────────────────
# Orchestratore
# ─────────────────────────────────────────────────────────────────────────────

def run(
    horizon_hours: int = 1,
    target: str = "temperature",
    dry_run: bool = False,
    model_version: Optional[str] = None,
) -> list[dict]:
    """
    Esegue l'inference su tutte le stazioni attive.

    Args:
        horizon_hours: N in "T_now + N ore" (default 1).
        target:        variabile target del modello LGBM (default "temperature").
        dry_run:       se True, stampa le previsioni senza scriverle su DB.
        model_version: versione da loggare; se None usa "inference_v%Y%m%d_%H%M".

    Returns:
        Lista dei dict di previsione (uno per stazione completata).
    """
    if model_version is None:
        model_version = "inference_" + datetime.utcnow().strftime("v%Y%m%d_%H%M")

    # Lazy import: evita di richiedere dotenv/supabase solo per --help
    import db
    stations = db.get_active_stations()
    if not stations:
        logger.error("Nessuna stazione attiva in DB")
        return []

    sep = "=" * 70
    print(f"\n{sep}")
    print(f"Inference   : T+{horizon_hours}h  |  target={target}  |  "
          f"{'DRY-RUN' if dry_run else 'DB INSERT'}")
    print(f"Stazioni    : {len(stations)} attive")
    print(f"Versione    : {model_version}")
    print(f"{sep}")

    results: list[dict] = []
    for st in stations:
        sid  = st["id"]
        name = st.get("name", "")
        try:
            pred = predict_station(st, horizon_hours, target=target)
        except Exception as exc:
            logger.error(f"[st.{sid} {name}] Errore: {exc}")
            continue
        if pred is None:
            continue

        corr_tag = "RF" if pred["corrected"] else "LGBM only"
        print(
            f"st.{sid:>2} {name:<24} | "
            f"valid {pred['valid_for'].strftime('%Y-%m-%d %H:%M UTC')} | "
            f"T={pred['temperature']:>5.2f}°C  ({corr_tag}, lgbm_raw={pred['lgbm_pred']:.2f})"
        )

        if not dry_run:
            try:
                fid = db.insert_forecast(
                    station_id     = sid,
                    forecast_at    = pred["forecast_at"],
                    valid_for      = pred["valid_for"],
                    temperature    = pred["temperature"],
                    wind_speed     = pred["wind_speed"],
                    wind_direction = pred["wind_direction"],
                    humidity       = pred["humidity"],
                    model_version  = model_version,
                    corrected      = pred["corrected"],
                )
                print(f"    └─ Supabase id={fid}")
            except Exception as exc:
                logger.warning(f"[st.{sid}] Insert fallito (non bloccante): {exc}")

        results.append({**pred, "station_id": sid})

    print(f"{sep}")
    print(f"Completate  : {len(results)}/{len(stations)} stazioni")
    print(f"{sep}\n")
    return results


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
        description="Previsione operativa real-time (LGBM + opzionale correttore RF)"
    )
    ap.add_argument("--horizon", type=int, default=1,
                    help="Orizzonte previsionale in ore (default: 1)")
    ap.add_argument("--target",  default="temperature",
                    choices=["temperature", "wind_speed", "wind_direction",
                             "humidity", "pressure"],
                    help="Variabile target del modello LGBM (default: temperature)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Stampa le previsioni senza scriverle su Supabase")
    ap.add_argument("--model-version", default=None,
                    help="Versione da loggare in forecasts (default: timestamp automatico)")
    args = ap.parse_args()

    run(
        horizon_hours=args.horizon,
        target=args.target,
        dry_run=args.dry_run,
        model_version=args.model_version,
    )
