"""
mainMETEO.py — Raccolta osservazioni meteo real-time (Phase 2)
Blocco 4 - Pipeline operativa ingestion

Fonte attiva (Phase 2a):
  • METAR da Iowa State IEM ASOS (https://mesonet.agron.iastate.edu),
    parametro `hours=2` per ottenere le ultime 2 ore. Per ogni stazione del
    progetto si prende l'osservazione più recente (timestamp massimo).

Per ogni osservazione raccolta:
  1. Conversione unità identica a historical.py (°F→°C, kt→km/h, ...).
  2. QC tramite qc.run_qc() con:
       - history  : ultime osservazioni della stazione da Supabase
       - neighbors: le altre stazioni attive del batch corrente
  3. db.insert_observation() solo se qc_flag < 2. Gli `issues` del QC
     vengono comunque loggati per ispezione manuale.

Architettura aperta a fonti future (Netatmo, Protezione Civile Lazio):
collect_observations() accetta una lista di callable "sorgente" che
producono osservazioni; ogni nuova fonte si aggiunge senza toccare
la pipeline QC + insert.

Avvio:
    python3 mainMETEO.py                # run reale
    python3 mainMETEO.py --dry-run      # nessuna scrittura DB
"""

from __future__ import annotations

import argparse
import logging
import sys
import time as _time
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import requests

# mainMETEO.py vive in project root: stesso pattern di inference.py.
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configurazione
# ─────────────────────────────────────────────────────────────────────────────

IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

# Stesso mapping di historical.py.  LIRA = Roma Ciampino  |  LIRF = Roma Fiumicino
STATION_ICAO: dict[int, str] = {
    1: "LIRA",   # Roma Nord        → Ciampino  (~26 km)
    2: "LIRA",   # Roma Centro      → Ciampino  (~11 km)
    3: "LIRF",   # Roma Sud         → Fiumicino (~12 km)
    4: "LIRF",   # Ostia            → Fiumicino (~8 km)
}

# Quante obs storiche passare a persistence_check (>= PERSISTENCE_WINDOW di qc.py).
HISTORY_FOR_QC = 3

# Finestra di fetch per la sorgente METAR. Con un cron a 30 min, 2 ore di
# buffer assorbono ritardi METAR senza perdere il punto più recente.
METAR_HOURS_WINDOW = 2


# ─────────────────────────────────────────────────────────────────────────────
# Sorgente Phase 2a — METAR da IEM ASOS
# ─────────────────────────────────────────────────────────────────────────────

def fetch_metar_latest(icao: str, hours: int = METAR_HOURS_WINDOW,
                       retries: int = 3) -> Optional[dict]:
    """
    Scarica le ultime `hours` ore di METAR per `icao` da IEM ASOS e ritorna
    l'osservazione più recente (timestamp massimo) come dict.

    Conversioni unità (identiche a historical.fetch_metar_iem):
        tmpf  (°F)  → temperature   (°C)
        sknt  (kt)  → wind_speed    (km/h, ×1.852)
        drct  (°)   → wind_direction (°)
        relh  (%)   → humidity      (%)
        mslp  (hPa) → pressure      (hPa) [fallback alti inHg ×33.8639]

    Returns None se nessuna osservazione utile è disponibile (es. tutte le
    righe hanno temperature NaN).
    """
    params = {
        "station": icao,
        "data":    "tmpf,dwpf,relh,drct,sknt,alti,mslp",
        "hours":   hours,            # IEM: ultime N ore relative a now
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
            r = requests.get(IEM_ASOS_URL, params=params, timeout=30)
            r.raise_for_status()
            break
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries - 1:
                wait = 2 ** attempt
                logger.warning(
                    f"METAR {icao} tentativo {attempt+1}/{retries} fallito: {exc}. "
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
        return None

    df = df.rename(columns={"valid": "time"})
    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time")

    # Conversioni unità
    if "tmpf" in df.columns:
        df["temperature"] = (pd.to_numeric(df["tmpf"], errors="coerce") - 32) * 5 / 9
    if "sknt" in df.columns:
        df["wind_speed"] = pd.to_numeric(df["sknt"], errors="coerce") * 1.852
    if "drct" in df.columns:
        df["wind_direction"] = pd.to_numeric(df["drct"], errors="coerce")
    if "relh" in df.columns:
        df["humidity"] = pd.to_numeric(df["relh"], errors="coerce")
    if "mslp" in df.columns:
        df["pressure"] = pd.to_numeric(df["mslp"], errors="coerce")
    if "pressure" not in df.columns or df["pressure"].isna().all():
        if "alti" in df.columns:
            df["pressure"] = pd.to_numeric(df["alti"], errors="coerce") * 33.8639

    # Prendi la riga più recente con temperatura non-NaN: temperature è il
    # campo critico per la pipeline QC (range + climatological + persistence).
    if "temperature" not in df.columns:
        return None
    df_valid = df[df["temperature"].notna()]
    if df_valid.empty:
        return None
    latest = df_valid.iloc[-1]

    def _val(col: str) -> Optional[float]:
        if col not in latest.index:
            return None
        v = latest[col]
        return float(v) if pd.notna(v) else None

    return {
        "recorded_at":    latest["time"].to_pydatetime(),
        "temperature":    _val("temperature"),
        "wind_speed":     _val("wind_speed"),
        "wind_direction": _val("wind_direction"),
        "humidity":       _val("humidity"),
        "pressure":       _val("pressure"),
        "raw_source":     f"IEM/{icao}",
    }


def fetch_observations_metar(stations: list[dict]) -> list[dict]:
    """
    Sorgente Phase 2a: una osservazione METAR (la più recente in
    METAR_HOURS_WINDOW) per ogni stazione attiva.

    Errori per singola stazione sono isolati: se LIRF fallisce, LIRA viene
    comunque processata.
    """
    observations: list[dict] = []
    for st in stations:
        station_id = st["id"]
        name = st.get("name", "?")
        icao = STATION_ICAO.get(station_id)
        if icao is None:
            logger.warning(f"st.{station_id} {name}: nessun ICAO mappato, skip")
            continue
        try:
            obs = fetch_metar_latest(icao)
        except Exception as exc:
            logger.error(f"st.{station_id} {name} | {icao} | fetch fallito: {exc}")
            continue
        if obs is None:
            logger.warning(
                f"st.{station_id} {name} | {icao} | "
                f"nessuna obs valida nelle ultime {METAR_HOURS_WINDOW}h"
            )
            continue
        # Arricchisci con metadati stazione (lat/lon servono al QC spaziale).
        obs["station_id"]   = station_id
        obs["station_name"] = name
        obs["lat"]          = st["lat"]
        obs["lon"]          = st["lon"]
        observations.append(obs)
    return observations


# ─────────────────────────────────────────────────────────────────────────────
# Stub fonti future (architettura aperta — non ancora implementate)
# ─────────────────────────────────────────────────────────────────────────────

# TODO Phase 2b: fetch_netatmo(bbox) → lista obs da stazioni private Netatmo
# def fetch_netatmo(stations: list[dict]) -> list[dict]:
#     """
#     Osservazioni Netatmo Weather Map nel bounding box dell'area di Roma.
#     Richiede OAuth2 (client_id/client_secret in env var NETATMO_*).
#     Output: stesso schema di fetch_observations_metar (station_id virtuale o
#     nuova stazione attiva da censire prima in `stations`).
#     """
#     raise NotImplementedError("Phase 2b — Netatmo Weather Map API")


# TODO Phase 2c: fetch_protezione_civile_lazio() → API OpenAmbiente
# def fetch_protezione_civile_lazio(stations: list[dict]) -> list[dict]:
#     """
#     Osservazioni dalla rete pluvio/anemometrica della Protezione Civile
#     Lazio, esposte via OpenAmbiente (https://www.openambiente.com).
#     """
#     raise NotImplementedError("Phase 2c — OpenAmbiente Protezione Civile Lazio")


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline: collect → QC → insert
# ─────────────────────────────────────────────────────────────────────────────

SourceFn = Callable[[list[dict]], list[dict]]


def collect_observations(
    sources: Optional[list[SourceFn]] = None,
    dry_run: bool = False,
) -> dict:
    """
    Pipeline completa: per ogni fonte fetcha le obs, applica run_qc(),
    inserisce in `observations` se qc_flag < 2.

    Args:
        sources: lista di callable `fn(stations) -> list[dict]`. Ogni dict
                 deve contenere station_id, lat, lon, recorded_at e i campi
                 osservati (temperature, wind_speed, ...).
                 Default: [fetch_observations_metar] (solo Phase 2a).
        dry_run: se True, non scrive su DB.

    Returns:
        Conteggi {"ok": int, "suspect": int, "discarded": int, "errors": int}.
    """
    # Lazy import: permette `python3 mainMETEO.py --help` senza dotenv/supabase
    # installati (stesso pattern di model/inference.py).
    import db
    from qc import run_qc

    stations = db.get_active_stations()
    if not stations:
        logger.error("Nessuna stazione attiva in DB.")
        return {"ok": 0, "suspect": 0, "discarded": 0, "errors": 0}

    if sources is None:
        sources = [fetch_observations_metar]

    # 1. Raccoglie da tutte le fonti.
    all_obs: list[dict] = []
    for source_fn in sources:
        try:
            batch = source_fn(stations)
            logger.info(f"Sorgente {source_fn.__name__}: {len(batch)} obs")
            all_obs.extend(batch)
        except Exception as exc:
            logger.error(f"Sorgente {source_fn.__name__} fallita: {exc}")

    if not all_obs:
        logger.warning("Nessuna osservazione raccolta.")
        return {"ok": 0, "suspect": 0, "discarded": 0, "errors": 0}

    counts = {"ok": 0, "suspect": 0, "discarded": 0, "errors": 0}

    # 2. Processa ogni osservazione.
    for obs in all_obs:
        station_id = obs["station_id"]
        name       = obs.get("station_name", "?")
        source     = obs.get("raw_source", "?")

        try:
            # History per persistence_check: ultime HISTORY_FOR_QC obs della
            # stazione (qc_ok_only=True per non incatenare flag su flag).
            try:
                hist_full = db.get_observations(station_id, hours=6, qc_ok_only=True)
                history   = hist_full[-HISTORY_FOR_QC:] if hist_full else []
            except Exception as exc:
                logger.warning(f"st.{station_id}: history fetch fallita: {exc}")
                history = []

            # Neighbors per spatial_check: tutte le altre obs del batch
            # corrente, con lat/lon e i campi richiesti.
            neighbors = [
                {
                    "lat":         o["lat"],
                    "lon":         o["lon"],
                    "temperature": o.get("temperature"),
                    "wind_speed":  o.get("wind_speed"),
                }
                for o in all_obs if o["station_id"] != station_id
            ]

            qc_input = {
                "station_id":     station_id,
                "lat":            obs["lat"],
                "lon":            obs["lon"],
                "recorded_at":    obs["recorded_at"],
                "temperature":    obs.get("temperature"),
                "wind_speed":     obs.get("wind_speed"),
                "wind_direction": obs.get("wind_direction"),
                "humidity":       obs.get("humidity"),
                "pressure":       obs.get("pressure"),
            }
            qc_flag, issues = run_qc(qc_input, history, neighbors)

            t      = obs.get("temperature")
            t_str  = f"T={t:.1f}°C" if t is not None else "T=—"
            qc_str = {0: "OK", 1: "SOSPETTO", 2: "SCARTATO"}[qc_flag]
            logger.info(f"st.{station_id} {name} | {source} | {t_str} | QC={qc_str}")

            if qc_flag == 0:
                counts["ok"] += 1
            elif qc_flag == 1:
                counts["suspect"] += 1
            else:
                counts["discarded"] += 1

            # Log issues sempre (anche se la riga viene scartata: serve a
            # ricostruire perché è stata flaggata).
            for iss in issues:
                logger.info(
                    f"  └─ QC {iss['check_type']} | "
                    f"{iss['field_name']} | {iss['reason']}"
                )

            # Insert solo se qc_flag < 2.
            if qc_flag >= 2:
                continue
            if dry_run:
                logger.info("  └─ [DRY-RUN] nessuna scrittura DB")
                continue

            inserted_id = db.insert_observation(
                station_id=station_id,
                recorded_at=obs["recorded_at"],
                temperature=obs.get("temperature"),
                wind_speed=obs.get("wind_speed"),
                wind_direction=obs.get("wind_direction"),
                humidity=obs.get("humidity"),
                pressure=obs.get("pressure"),
                qc_flag=qc_flag,
                raw_source=obs.get("raw_source"),
            )
            logger.info(f"  └─ inserito observations.id={inserted_id}")

        except Exception as exc:
            logger.error(f"st.{station_id} {name}: errore pipeline: {exc}")
            counts["errors"] += 1

    return counts


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Raccolta osservazioni meteo real-time. "
            "Phase 2a: METAR via IEM ASOS → QC → Supabase."
        )
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Esegui fetch + QC ma non scrivere su DB.",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    start = datetime.now(timezone.utc)
    logger.info(f"mainMETEO start | {start.isoformat()} | dry_run={args.dry_run}")

    counts = collect_observations(dry_run=args.dry_run)

    duration_s = (datetime.now(timezone.utc) - start).total_seconds()
    logger.info(
        f"mainMETEO end | OK={counts['ok']} SOSPETTI={counts['suspect']} "
        f"SCARTATE={counts['discarded']} ERRORI={counts['errors']} "
        f"| {duration_s:.1f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
