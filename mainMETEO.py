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
import math
import os
import statistics
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

# Mapping stazione → ICAO aeroporto di riferimento per osservazioni METAR.
# Le stazioni Netatmo (id 25-29) non hanno aeroporto nel raggio utile:
# copertura esclusivamente Netatmo, nessun ICAO mappato.
STATION_ICAO: dict[int, str] = {
    3: "LIRF",   # Roma Sud (Casal Palocco) → Fiumicino (~12 km)
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

# ─────────────────────────────────────────────────────────────────────────────
# Sorgente Phase 2b — Netatmo (dati pubblici aggregati per cluster)
# ─────────────────────────────────────────────────────────────────────────────

NETATMO_TOKEN_URL   = "https://api.netatmo.com/oauth2/token"
NETATMO_PUBDATA_URL = "https://api.netatmo.com/api/getpublicdata"

# 5 sotto-bbox sovrapposti che coprono l'intero Lazio.
# La sovrapposizione è intenzionale: la dedup su _id evita duplicati.
LAZIO_BBOXES = [
    {"lat_sw": 41.50, "lat_ne": 42.20, "lon_sw": 12.05, "lon_ne": 12.95},  # Roma metro
    {"lat_sw": 41.75, "lat_ne": 42.70, "lon_sw": 11.40, "lon_ne": 12.70},  # Lazio nord
    {"lat_sw": 41.18, "lat_ne": 41.80, "lon_sw": 12.25, "lon_ne": 13.45},  # Lazio sud-ovest
    {"lat_sw": 41.18, "lat_ne": 42.15, "lon_sw": 12.95, "lon_ne": 14.05},  # Lazio est
    {"lat_sw": 42.00, "lat_ne": 42.85, "lon_sw": 12.30, "lon_ne": 13.35},  # Lazio nord-est
]

NETATMO_RADIUS_KM  = 5.0   # raggio aggregazione per stazione progetto
NETATMO_MIN_CLUSTER = 2    # minimo stazioni Netatmo nel raggio per procedere
NETATMO_MAX_AGE_S  = 5400  # dati più vecchi di 90 min → scartati


def _haversine_km_nt(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _circular_mean_deg(angles: list[float]) -> float:
    """Media circolare di angoli in gradi (corretta per direzione vento)."""
    sin_sum = sum(math.sin(math.radians(a)) for a in angles)
    cos_sum = sum(math.cos(math.radians(a)) for a in angles)
    return math.degrees(math.atan2(sin_sum / len(angles), cos_sum / len(angles))) % 360


def _refresh_netatmo_token() -> str:
    client_id     = os.getenv("NETATMO_CLIENT_ID")
    client_secret = os.getenv("NETATMO_CLIENT_SECRET")
    refresh_token = os.getenv("NETATMO_REFRESH_TOKEN")
    if not all([client_id, client_secret, refresh_token]):
        raise EnvironmentError(
            "Credenziali Netatmo mancanti: "
            "NETATMO_CLIENT_ID, NETATMO_CLIENT_SECRET, NETATMO_REFRESH_TOKEN"
        )
    r = requests.post(
        NETATMO_TOKEN_URL,
        data={
            "grant_type":    "refresh_token",
            "client_id":     client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    r.raise_for_status()
    payload = r.json()
    if "access_token" not in payload:
        raise RuntimeError(f"Token refresh: risposta inattesa: {payload}")
    logger.info("[Netatmo] Token rinnovato")
    return payload["access_token"]


def _parse_netatmo_station(raw: dict, now_ts: int) -> Optional[dict]:
    """
    Estrae temperatura, umidità e vento (se NAModule2) da un record getpublicdata.
    Ritorna None se dati mancanti o troppo vecchi.
    Nota: Netatmo restituisce [lon, lat] — ordine invertito rispetto allo standard.
    """
    try:
        lon, lat = raw["place"]["location"]
    except (KeyError, ValueError, TypeError):
        return None

    temperature = humidity = wind_speed = wind_dir = data_ts = None

    for mod_data in raw.get("measures", {}).values():
        mtype = mod_data.get("type", [])
        if "temperature" in mtype and "res" in mod_data:
            res = mod_data["res"]
            if not res:
                continue
            ts_str, values = next(iter(res.items()))
            ts = int(ts_str)
            if now_ts - ts > NETATMO_MAX_AGE_S:
                continue
            idx_t = mtype.index("temperature")
            temperature = float(values[idx_t])
            if "humidity" in mtype:
                idx_h = mtype.index("humidity")
                if len(values) > idx_h:
                    humidity = float(values[idx_h])
            data_ts = ts
        elif "wind_strength" in mod_data and "wind_angle" in mod_data:
            wt = mod_data.get("wind_timeutc", 0)
            if now_ts - wt <= NETATMO_MAX_AGE_S:
                wind_speed = float(mod_data["wind_strength"])  # già in km/h
                wind_dir   = float(mod_data["wind_angle"])

    if temperature is None or data_ts is None:
        return None

    return {
        "lat": lat, "lon": lon,
        "temperature": temperature, "humidity": humidity,
        "wind_speed": wind_speed, "wind_dir": wind_dir,
    }


def fetch_netatmo(
    project_stations: list[dict],
    dry_run: bool = False,
) -> list[dict]:
    """
    Phase 2b — Raccoglie dati pubblici Netatmo per Roma e li aggrega per
    stazione progetto.

    Per ogni stazione progetto:
      - Trova tutte le stazioni Netatmo pubbliche entro NETATMO_RADIUS_KM km
      - Calcola mediana di temperatura/umidità, media circolare direzione vento
      - QC a 4 livelli via qc.run_qc()
      - Insert in 'observations' se qc_flag < 2; log QC issues su qc_log

    Richiede le variabili d'ambiente: NETATMO_CLIENT_ID, NETATMO_CLIENT_SECRET,
    NETATMO_REFRESH_TOKEN (da .env in locale, da GitHub Secrets in CI).
    """
    from db import get_observations, insert_observation, get_client
    from qc import run_qc

    logger.info("[Netatmo] ── Avvio raccolta dati pubblici ──────────────────")

    try:
        token = _refresh_netatmo_token()
    except Exception as exc:
        logger.error(f"[Netatmo] Token refresh fallito: {exc}")
        return []

    raw_stations: list[dict] = []
    seen_ids: set[str] = set()
    for bbox in LAZIO_BBOXES:
        try:
            r = requests.get(
                NETATMO_PUBDATA_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={
                    **bbox,
                    "required_data": "temperature",
                    "filter":        "true",
                },
                timeout=30,
            )
            r.raise_for_status()
            batch = r.json().get("body", [])
        except Exception as exc:
            logger.warning(f"[Netatmo] getpublicdata fallito per bbox {bbox}: {exc}")
            continue
        new = 0
        for s in batch:
            sid = s.get("_id")
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                raw_stations.append(s)
                new += 1
        logger.info(f"[Netatmo] bbox {bbox}: {len(batch)} stazioni ({new} nuove dopo dedup)")
    logger.info(f"[Netatmo] {len(raw_stations)} stazioni raw totali (dedup su {len(LAZIO_BBOXES)} bbox)")

    now_ts = int(datetime.now(timezone.utc).timestamp())
    parsed = [
        s for raw in raw_stations
        if (s := _parse_netatmo_station(raw, now_ts)) is not None
    ]
    logger.info(
        f"[Netatmo] {len(parsed)} stazioni valide "
        f"(scartate: {len(raw_stations) - len(parsed)} stale/incomplete)"
    )

    if not parsed:
        logger.warning("[Netatmo] Nessuna stazione con dati freschi — skip")
        return []

    results = []
    now_dt  = datetime.now(timezone.utc)

    for ps in project_stations:
        ps_id, ps_name = ps["id"], ps.get("name", f"st.{ps['id']}")
        ps_lat, ps_lon = ps["lat"], ps["lon"]

        nearby = [
            s for s in parsed
            if _haversine_km_nt(ps_lat, ps_lon, s["lat"], s["lon"]) <= NETATMO_RADIUS_KM
        ]
        min_cluster = 1 if (
            ps["id"] >= 39
            or ps.get("microclima") in ("quota", "alta_quota", "colline_interne")
        ) else NETATMO_MIN_CLUSTER
        if len(nearby) < min_cluster:
            logger.warning(
                f"[Netatmo] st.{ps_id} ({ps_name}): {len(nearby)} stazioni entro "
                f"{NETATMO_RADIUS_KM} km — sotto soglia ({min_cluster}), skip"
            )
            continue

        temps       = [s["temperature"] for s in nearby]
        temperature = statistics.median(temps)
        hums        = [s["humidity"] for s in nearby if s["humidity"] is not None]
        humidity    = statistics.median(hums) if hums else None
        wind_pairs  = [
            (s["wind_speed"], s["wind_dir"])
            for s in nearby
            if s["wind_speed"] is not None and s["wind_dir"] is not None
        ]
        if wind_pairs:
            wind_speed = statistics.median([w[0] for w in wind_pairs])
            wind_dir   = _circular_mean_deg([w[1] for w in wind_pairs])
        else:
            wind_speed = wind_dir = None

        t_str = f"T={temperature:.1f}°C"
        h_str = f"H={humidity:.0f}%" if humidity is not None else "H=n/a"
        w_str = f"W={wind_speed:.0f}km/h {wind_dir:.0f}°" if wind_speed is not None else "W=n/a"
        logger.info(f"[Netatmo] st.{ps_id} ({ps_name}): {len(nearby)} stazioni | {t_str}  {h_str}  {w_str}")

        obs = {
            "station_id":     ps_id,
            "lat":            ps_lat,
            "lon":            ps_lon,
            "recorded_at":    now_dt,
            "temperature":    round(temperature, 2),
            "wind_speed":     round(wind_speed, 1) if wind_speed is not None else None,
            "wind_direction": round(wind_dir, 1)   if wind_dir   is not None else None,
            "humidity":       round(humidity, 1)   if humidity   is not None else None,
        }

        history          = get_observations(ps_id, hours=3)
        neighbors_for_qc = [
            {"lat": s["lat"], "lon": s["lon"],
             "temperature": s["temperature"], "wind_speed": s["wind_speed"] or 0.0}
            for s in nearby
        ]
        qc_flag, issues = run_qc(obs, history, neighbors_for_qc)

        for issue in issues:
            try:
                if not dry_run:
                    get_client().table("qc_log").insert({
                        "station_id":     ps_id,
                        "recorded_at":    now_dt.isoformat(),
                        "check_type":     issue["check_type"],
                        "field_name":     issue["field_name"],
                        "original_value": issue["original_value"],
                        "reason":         issue["reason"],
                    }).execute()
            except Exception as exc:
                logger.warning(f"[Netatmo] st.{ps_id} qc_log insert fallito: {exc}")

        if qc_flag >= 2:
            logger.warning(f"[Netatmo] st.{ps_id} SCARTATO dal QC (flag={qc_flag})")
            continue
        if qc_flag == 1:
            logger.warning(f"[Netatmo] st.{ps_id} SOSPETTO (flag=1) — inserito con cautela")

        raw_src = {
            "source":     "netatmo_public",
            "n_stations": len(nearby),
            "temps_raw":  [round(t, 1) for t in temps],
        }

        if not dry_run:
            try:
                obs_id = insert_observation(
                    station_id     = ps_id,
                    recorded_at    = now_dt,
                    temperature    = obs["temperature"],
                    wind_speed     = obs["wind_speed"],
                    wind_direction = obs["wind_direction"],
                    humidity       = obs["humidity"],
                    qc_flag        = qc_flag,
                    raw_source     = raw_src,
                )
                logger.info(f"[Netatmo] st.{ps_id} INSERT OK → obs_id={obs_id}")
                results.append({**obs, "qc_flag": qc_flag, "obs_id": obs_id})
            except Exception as exc:
                logger.error(f"[Netatmo] st.{ps_id} INSERT FALLITO: {exc}")
        else:
            logger.info(f"[Netatmo] [DRY-RUN] st.{ps_id} OK | T={obs['temperature']}°C  qc_flag={qc_flag}")
            results.append({**obs, "qc_flag": qc_flag})

    logger.info(f"[Netatmo] ── Fine: {len(results)}/{len(project_stations)} stazioni inserite ──")
    return results


# TODO Phase 2c: fetch_protezione_civile_lazio() → API OpenAmbiente
# def fetch_protezione_civile_lazio(stations: list[dict]) -> list[dict]:
#     """
#     Osservazioni dalla rete pluvio/anemometrica della Protezione Civile
#     Lazio, esposte via OpenAmbiente (https://www.openambiente.com).
#     """
#     raise NotImplementedError("Phase 2c — OpenAmbiente Protezione Civile Lazio")


# ─────────────────────────────────────────────────────────────────────────────
# Sorgente Phase 2d — METAR LIRE (Pratica di Mare, stazione 30)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_lire_metar() -> dict | None:
    """
    Scarica l'ultimo METAR da LIRE (Pratica di Mare) via aviationweather.gov.
    Ritorna un dict compatibile con insert_observation(), oppure None se fallisce.
    """
    from datetime import datetime, timezone

    url = "https://aviationweather.gov/api/data/metar"
    params = {"ids": "LIRE", "format": "json", "hours": 2}

    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.warning(f"[LIRE] fetch fallito: {exc}")
        return None

    if not data:
        logger.warning("[LIRE] risposta vuota")
        return None

    obs = data[0]  # più recente prima

    # Wind direction: "VRB" → None
    wdir_raw = obs.get("wdir")
    wind_dir = None if wdir_raw == "VRB" else (
        float(wdir_raw) if wdir_raw is not None else None
    )

    # Wind speed: knots → km/h
    wspd_raw = obs.get("wspd")
    wind_speed = round(float(wspd_raw) * 1.852, 1) if wspd_raw is not None else None

    return {
        "station_id":     33,           # id Pratica di Mare
        "recorded_at":    datetime.fromtimestamp(obs["obsTime"], tz=timezone.utc),
        "temperature":    float(obs["temp"])   if obs.get("temp")  is not None else None,
        "humidity":       None,                # METAR non fornisce RH diretta
        "wind_speed":     wind_speed,
        "wind_direction": wind_dir,
        "raw_source":     {"source": "metar_lire", "raw": obs.get("rawOb")},
    }


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

    # Phase 2b — Netatmo (self-contained: gestisce QC + insert internamente)
    fetch_netatmo(stations, dry_run=dry_run)

    # Phase 2d — METAR LIRE (Pratica di Mare, stazione 30)
    lire_obs = fetch_lire_metar()
    if lire_obs:
        history = db.get_observations(30, hours=3)
        qc_flag, issues = run_qc(lire_obs, history, neighbors=[])
        if qc_flag < 2:
            if not dry_run:
                db.insert_observation(**{k: v for k, v in lire_obs.items()
                                         if k != "raw_source"},
                                      qc_flag=qc_flag,
                                      raw_source=lire_obs["raw_source"])
            else:
                logger.info(f"[LIRE] [DRY-RUN] T={lire_obs.get('temperature')}°C  qc_flag={qc_flag}")
        else:
            logger.warning(f"[LIRE] SCARTATO dal QC (flag={qc_flag})")

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
