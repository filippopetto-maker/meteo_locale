"""
historical.py — Tabella di training storica ERA5 / METAR
Blocco 3 - Fase 1

Costruisce il dataset per addestrare il modello locale:
  - Input  (X): reanalisi ERA5 oraria da Open-Meteo Archive API
  - Target (y): osservazioni METAR orarie da Iowa State IEM ASOS
  - Garanzia look-ahead: feature a T, target a T + horizon_hours

Flusso principale:
  1. fetch_era5()            → DataFrame ERA5 orario per lat/lon
  2. fetch_metar_iem()       → DataFrame METAR grezzo (frequenza variabile)
  3. resample_metar()        → METAR ricampionato a 1 h
  4. build_training_table()  → join + feature engineering + shift target
  5. build_full_training_set() → pipeline multi-stazione

Utilizzo da riga di comando:
  python historical.py --start 2023-01-01 --end 2024-12-31 --horizon 1
  python historical.py --station-id 4 --icao LIRF --out data/ostia.parquet
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime
from io import StringIO
from typing import Optional

import numpy as np
import pandas as pd
import requests

from features import build_feature_matrix

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configurazione
# ─────────────────────────────────────────────────────────────────────────────

# Variabili ERA5 richieste all'Open-Meteo Archive API.
# windspeed_10m → km/h (default Open-Meteo), coerente con lo schema del progetto.
ERA5_VARIABLES: list[str] = [
    "temperature_2m",
    "dewpoint_2m",
    "relativehumidity_2m",
    "surface_pressure",
    "windspeed_10m",
    "winddirection_10m",
    "precipitation",
    "cloudcover",
    "shortwave_radiation",
]

# ERA5 → nome colonna interno (schema coerente con observations e features.py).
ERA5_RENAME: dict[str, str] = {
    "temperature_2m":      "temperature",
    "dewpoint_2m":         "dewpoint",
    "relativehumidity_2m": "humidity",
    "surface_pressure":    "pressure",
    "windspeed_10m":       "wind_speed",
    "winddirection_10m":   "wind_direction",
    "precipitation":       "precipitation",
    "cloudcover":          "cloudcover",
    "shortwave_radiation": "shortwave_radiation",
}

# Stazione METAR (codice ICAO) più vicina a ciascuna stazione del progetto.
# LIRF = Roma Fiumicino  |  LIRA = Roma Ciampino
# Aggiorna quando aggiungi nuove stazioni.
STATION_ICAO: dict[int, str] = {
    1: "LIRA",   # Roma Nord        → Ciampino  (~26 km)
    2: "LIRA",   # Roma Centro      → Ciampino  (~11 km)
    3: "LIRF",   # Roma Sud         → Fiumicino (~12 km)
    4: "LIRF",   # Ostia            → Fiumicino (~8 km)
}

# Colonne METAR che diventano target nel training set.
TARGET_COLS: list[str] = [
    "temperature",
    "wind_speed",
    "wind_direction",
    "humidity",
    "pressure",
]

ERA5_API_URL = "https://archive-api.open-meteo.com/v1/archive"
IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


# ─────────────────────────────────────────────────────────────────────────────
# 1 — ERA5 da Open-Meteo Archive
# ─────────────────────────────────────────────────────────────────────────────

def fetch_era5(
    lat: float,
    lon: float,
    start_date: str,
    end_date: str,
    variables: Optional[list[str]] = None,
    retries: int = 3,
) -> pd.DataFrame:
    """
    Scarica dati ERA5 orari da Open-Meteo Archive API (gratuita, no API key).

    Args:
        lat, lon:   coordinate della stazione target.
        start_date: "YYYY-MM-DD" (inclusa).
        end_date:   "YYYY-MM-DD" (inclusa).
        variables:  variabili ERA5 da richiedere (default: ERA5_VARIABLES).
        retries:    tentativi con backoff esponenziale in caso di errore di rete.

    Returns:
        DataFrame con colonna 'time' (UTC naive) e le variabili ERA5
        rinominate secondo lo schema del progetto.
    """
    if variables is None:
        variables = ERA5_VARIABLES

    params = {
        "latitude":   lat,
        "longitude":  lon,
        "start_date": start_date,
        "end_date":   end_date,
        "hourly":     ",".join(variables),
        "timezone":   "UTC",
    }

    last_exc: Exception = RuntimeError("Nessun tentativo eseguito")
    for attempt in range(retries):
        try:
            r = requests.get(ERA5_API_URL, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            break
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries - 1:
                wait = 2 ** attempt
                logger.warning(
                    f"ERA5 tentativo {attempt + 1}/{retries} fallito: {exc}. "
                    f"Riprovo in {wait}s"
                )
                _time.sleep(wait)
    else:
        raise last_exc

    hourly = data["hourly"]
    df = pd.DataFrame({"time": pd.to_datetime(hourly["time"])})
    for var in variables:
        if var in hourly:
            df[var] = hourly[var]

    df = df.rename(columns={k: v for k, v in ERA5_RENAME.items() if k in df.columns})
    logger.info(
        f"ERA5: {len(df)} ore [{start_date} → {end_date}] "
        f"({lat:.4f}, {lon:.4f}), {len(df.columns) - 1} variabili"
    )
    return df


# ─────────────────────────────────────────────────────────────────────────────
# 2 — METAR da Iowa State IEM ASOS
# ─────────────────────────────────────────────────────────────────────────────

def fetch_metar_iem(
    icao: str,
    start_date: str,
    end_date: str,
    retries: int = 3,
) -> pd.DataFrame:
    """
    Scarica dati METAR storici da Iowa State IEM ASOS (gratuito, no API key).

    Copertura globale: include LIRF (Fiumicino) e LIRA (Ciampino).
    I dati restituiti sono grezzi (frequenza variabile, 20–60 min).

    Conversioni applicate:
        tmpf  (°F)       → temperature   (°C)
        sknt  (kt)       → wind_speed    (km/h, ×1.852)
        drct  (°)        → wind_direction (°)
        relh  (%)        → humidity      (%)
        mslp  (hPa)      → pressure      (hPa)  [fallback: alti inHg ×33.864]

    Args:
        icao:       codice ICAO della stazione (es. "LIRF", "LIRA").
        start_date: "YYYY-MM-DD".
        end_date:   "YYYY-MM-DD".

    Returns:
        DataFrame RAW con colonne: time (UTC naive), temperature, wind_speed,
        wind_direction, humidity, pressure  (subset disponibile).
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end   = datetime.strptime(end_date,   "%Y-%m-%d")

    params = {
        "station": icao,
        "data":    "tmpf,dwpf,relh,drct,sknt,alti,mslp",
        "year1":   start.year,  "month1": start.month,  "day1":  start.day,
        "year2":   end.year,    "month2": end.month,    "day2":  end.day,
        "tz":      "UTC",
        "format":  "onlycomma",
        "latlon":  "no",
        "elev":    "no",
        "missing": "M",
        "trace":   "T",
        "direct":  "no",
    }

    last_exc: Exception = RuntimeError("Nessun tentativo eseguito")
    for attempt in range(retries):
        try:
            r = requests.get(IEM_ASOS_URL, params=params, timeout=120)
            r.raise_for_status()
            break
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries - 1:
                wait = 2 ** attempt
                logger.warning(
                    f"METAR {icao} tentativo {attempt + 1}/{retries} fallito: {exc}. "
                    f"Riprovo in {wait}s"
                )
                _time.sleep(wait)
    else:
        raise last_exc

    df = pd.read_csv(
        StringIO(r.text),
        na_values=["M", "T", ""],
        comment="#",
        low_memory=False,
    )
    if df.empty or "valid" not in df.columns:
        raise ValueError(
            f"METAR {icao}: nessun dato per {start_date} → {end_date}. "
            f"Verifica che la stazione ICAO sia corretta e presente in IEM."
        )

    # Timestamp → UTC naive
    df = df.rename(columns={"valid": "time"})
    df["time"] = pd.to_datetime(df["time"])

    # Conversioni unità
    if "tmpf" in df.columns:
        df["temperature"] = (pd.to_numeric(df["tmpf"], errors="coerce") - 32) * 5 / 9
    if "sknt" in df.columns:
        df["wind_speed"] = pd.to_numeric(df["sknt"], errors="coerce") * 1.852  # kt → km/h
    if "drct" in df.columns:
        df["wind_direction"] = pd.to_numeric(df["drct"], errors="coerce")
    if "relh" in df.columns:
        df["humidity"] = pd.to_numeric(df["relh"], errors="coerce")

    # Pressione: preferisci mslp (già in hPa), fallback alti (inHg → hPa)
    if "mslp" in df.columns:
        df["pressure"] = pd.to_numeric(df["mslp"], errors="coerce")
    if "pressure" not in df.columns or df.get("pressure", pd.Series()).isna().all():
        if "alti" in df.columns:
            df["pressure"] = pd.to_numeric(df["alti"], errors="coerce") * 33.8639

    keep = ["time"] + [c for c in TARGET_COLS if c in df.columns]
    df   = df[keep].sort_values("time").reset_index(drop=True)

    logger.info(f"METAR {icao}: {len(df)} osservazioni [{start_date} → {end_date}]")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# 3 — Ricampionamento METAR → frequenza oraria
# ─────────────────────────────────────────────────────────────────────────────

def resample_metar(metar_df: pd.DataFrame, freq: str = "1h") -> pd.DataFrame:
    """
    Ricampiona le osservazioni METAR alla frequenza target (default 1 h).

    METAR può avere osservazioni ogni 20–30 min (con SPECI); aggrega per media
    entro ogni finestra temporale con label = inizio finestra, così che il
    timestamp 14:00 copra le osservazioni [14:00, 15:00).

    Questo allineamento è compatibile con ERA5 (Open-Meteo label="left").

    Args:
        metar_df: output di fetch_metar_iem().
        freq:     stringa pandas (es. "1h", "3h").

    Returns:
        DataFrame ricampionato con colonna 'time' e variabili target.
    """
    df = metar_df.set_index("time")
    df.index = pd.DatetimeIndex(df.index)
    resampled = df.resample(freq, label="left", closed="left").mean()
    resampled = resampled.dropna(how="all")
    return resampled.reset_index()


# ─────────────────────────────────────────────────────────────────────────────
# 4 — Training table per una stazione
# ─────────────────────────────────────────────────────────────────────────────

def build_training_table(
    station: dict,
    start_date: str,
    end_date: str,
    horizon_hours: int = 1,
    metar_icao: Optional[str] = None,
    era5_variables: Optional[list[str]] = None,
) -> pd.DataFrame:
    """
    Costruisce la tabella di training per UNA stazione.

    Struttura del join (look-ahead rispettato):

        ERA5[T]  (X: feature engineering, solo passato)
              └──► target_*  =  METAR[T + horizon_hours]  (y: label)

    Garanzia look-ahead:
      • ERA5 a T è una reanalisi: non contiene informazioni dal futuro.
      • Lag e rolling in features.py usano .shift(n≥1): guardano solo
        indietro nella serie temporale.
      • Il target è prodotto con .shift(-horizon_hours): sposta nella
        colonna target il valore futuro esplicito. È il label di
        addestramento, NON entra nelle feature X.

    Args:
        station:        dict con id, lat, lon (+ metadati orografici se presenti).
        start_date:     "YYYY-MM-DD".
        end_date:       "YYYY-MM-DD".
        horizon_hours:  N in "T + N ore" (default 1).
        metar_icao:     codice ICAO; se None usa STATION_ICAO[station['id']].
        era5_variables: variabili ERA5; se None usa ERA5_VARIABLES.

    Returns:
        DataFrame con:
          - feature ERA5 + feature engineering a T  (colonne X)
          - colonne 'target_*' = METAR a T + horizon_hours  (colonne y)
          - colonne 'station_id', 'horizon_hours', 'metar_icao'
        Usare .dropna(subset=target_cols) prima di passare a lgbm.Dataset().
    """
    station_id = station.get("id") or station.get("station_id")
    lat = station["lat"]
    lon = station["lon"]

    if metar_icao is None:
        metar_icao = STATION_ICAO.get(station_id)
        if metar_icao is None:
            raise ValueError(
                f"Nessun ICAO configurato per station_id={station_id}. "
                f"Aggiungi la voce in STATION_ICAO oppure passa metar_icao= esplicitamente."
            )

    # ── 1. ERA5 (feature input a T) ──────────────────────────────────────────
    era5_df = fetch_era5(lat, lon, start_date, end_date, variables=era5_variables)
    era5_df["time"] = pd.to_datetime(era5_df["time"]).dt.floor("h")

    # ── 2. METAR ricampionato (target a T + N) ───────────────────────────────
    metar_raw = fetch_metar_iem(metar_icao, start_date, end_date)
    metar_df  = resample_metar(metar_raw, freq="1h")
    metar_df["time"] = pd.to_datetime(metar_df["time"]).dt.floor("h")

    available_targets = [c for c in TARGET_COLS if c in metar_df.columns]

    # ── 3. Merge su 'time' (left: mantiene tutti gli slot ERA5) ─────────────
    merged = era5_df.merge(
        metar_df[["time"] + available_targets].rename(
            columns={c: f"_metar_{c}" for c in available_targets}
        ),
        on="time",
        how="left",
    ).sort_values("time").reset_index(drop=True)

    # ── 4. Shift target: riga T → METAR di T + horizon_hours ────────────────
    #
    # merged è ordinato per tempo crescente (ERA5 è già orario regolare).
    # shift(-N) porta in posizione i il valore che si trova in posizione i+N,
    # ovvero METAR all'ora T_i + N.
    #
    # Questo è il label di addestramento: "qual è la temperatura reale N ore
    # dopo che il modello NWP ha prodotto questa analisi?"
    # Non introduce look-ahead perché non entra nei predittori (colonne X).
    for col in available_targets:
        merged[f"target_{col}"] = merged[f"_metar_{col}"].shift(-horizon_hours)
        merged.drop(columns=[f"_metar_{col}"], inplace=True)

    # ── 5. Feature engineering sull'ERA5 (strati 1–5 di features.py) ────────
    # Rinomina 'time' → 'recorded_at' per compatibilità con build_feature_matrix.
    merged = merged.rename(columns={"time": "recorded_at"})
    merged = build_feature_matrix(merged, station)

    # ── 6. Metadati ───────────────────────────────────────────────────────────
    merged["station_id"]    = station_id
    merged["horizon_hours"] = horizon_hours
    merged["metar_icao"]    = metar_icao

    target_cols_present = [f"target_{c}" for c in available_targets]
    n_usable = merged.dropna(subset=target_cols_present).shape[0]
    logger.info(
        f"[st.{station_id}] {len(merged)} righe totali, "
        f"{n_usable} utilizzabili (target non-NaN), "
        f"{len(merged.columns)} colonne feature"
    )
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# 5 — Pipeline multi-stazione
# ─────────────────────────────────────────────────────────────────────────────

def build_full_training_set(
    stations: list[dict],
    start_date: str,
    end_date: str,
    horizon_hours: int = 1,
) -> pd.DataFrame:
    """
    Costruisce la tabella di training per TUTTE le stazioni e le concatena.

    Ogni stazione viene processata in isolamento (lag/rolling per stazione),
    poi i DataFrame vengono concatenati. Il campo 'station_id' permette
    al modello di distinguere le stazioni nelle feature orografiche.

    Args:
        stations:      lista di dict stazione (da db.get_active_stations()).
        start_date:    "YYYY-MM-DD".
        end_date:      "YYYY-MM-DD".
        horizon_hours: orizzonte previsionale.

    Returns:
        DataFrame multi-stazione pronto per lgbm.Dataset() dopo .dropna().
    """
    frames = []
    for st in stations:
        try:
            df = build_training_table(st, start_date, end_date, horizon_hours)
            frames.append(df)
        except Exception as exc:
            logger.error(f"[st.{st.get('id')} {st.get('name', '')}] Skippata: {exc}")

    if not frames:
        raise RuntimeError("Nessuna stazione completata con successo.")

    full_df = pd.concat(frames, ignore_index=True)
    logger.info(
        f"Full training set: {len(full_df):,} righe × {len(full_df.columns)} colonne "
        f"({len(frames)}/{len(stations)} stazioni completate)"
    )
    return full_df


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import os

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    ap = argparse.ArgumentParser(
        description="Costruisce il training set storico ERA5/METAR"
    )
    ap.add_argument("--start",      default="2023-01-01",
                    help="Data inizio YYYY-MM-DD (default: 2023-01-01)")
    ap.add_argument("--end",        default="2024-12-31",
                    help="Data fine   YYYY-MM-DD (default: 2024-12-31)")
    ap.add_argument("--horizon",    type=int, default=1,
                    help="Orizzonte previsionale in ore (default: 1)")
    ap.add_argument("--station-id", type=int, default=None,
                    help="ID stazione singola (default: tutte le stazioni attive)")
    ap.add_argument("--icao",       default=None,
                    help="Override codice ICAO (solo con --station-id)")
    ap.add_argument("--out",        default="data/training.parquet",
                    help="File output: .parquet (default) o .csv")
    args = ap.parse_args()

    from db import get_active_stations
    all_stations = get_active_stations()

    if args.station_id is not None:
        match = [s for s in all_stations if s["id"] == args.station_id]
        if not match:
            raise SystemExit(f"❌ Stazione {args.station_id} non trovata o non attiva")
        result_df = build_training_table(
            match[0], args.start, args.end, args.horizon, metar_icao=args.icao
        )
    else:
        result_df = build_full_training_set(
            all_stations, args.start, args.end, args.horizon
        )

    target_cols = [c for c in result_df.columns if c.startswith("target_")]
    clean_df    = result_df.dropna(subset=target_cols).reset_index(drop=True)

    print(f"\n{'='*60}")
    print(f"Training set : {len(clean_df):,} righe × {len(clean_df.columns)} colonne")
    print(f"Orizzonte    : T + {args.horizon}h  |  {args.start} → {args.end}")
    print(f"Target cols  : {target_cols}")
    if "target_temperature" in clean_df.columns:
        print(f"\nDistribuzione target_temperature (°C):")
        print(clean_df["target_temperature"].describe().to_string())

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    if args.out.endswith(".parquet"):
        clean_df.to_parquet(args.out, index=False)
    else:
        clean_df.to_csv(args.out, index=False)
    print(f"\n✅ Salvato in {args.out}")
