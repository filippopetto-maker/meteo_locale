"""
fetch_netatmo() — Raccolta dati pubblici Netatmo per Roma
Phase 2b — da integrare in mainMETEO.py (sostituisce lo stub esistente)

Flusso:
  1. Refresh del token OAuth2 con client_id / client_secret / refresh_token
  2. GET /api/getpublicdata → tutte le stazioni pubbliche nel bbox di Roma
  3. Parsing: temperatura + umidità (sempre), vento se disponibile (NAModule2)
  4. Per ogni stazione progetto: mediana delle stazioni Netatmo entro 5 km
     (la mediana è più robusta della media contro i sensori mal posizionati)
  5. QC a 4 livelli via qc.run_qc()
  6. Insert in observations solo se qc_flag < 2

Variabili d'ambiente richieste (.env e GitHub Secrets):
  NETATMO_CLIENT_ID
  NETATMO_CLIENT_SECRET
  NETATMO_REFRESH_TOKEN
"""

import json
import logging
import math
import os
import statistics
import time
from datetime import datetime, timezone

import requests

# db e qc sono già importati nel mainMETEO.py in cui va integrata questa funzione
# from db import get_active_stations, get_observations, insert_observation, get_client
# from qc import run_qc

logger = logging.getLogger(__name__)

# ── Configurazione ────────────────────────────────────────────────────────────

NETATMO_TOKEN_URL   = "https://api.netatmo.com/oauth2/token"
NETATMO_PUBDATA_URL = "https://api.netatmo.com/api/getpublicdata"

# Bounding boxes Lazio completo (5 tile sovrapposti per coprire tutto il territorio)
LAZIO_BBOXES = [
    # Roma metropolitana (densa, bbox originale)
    {"lat_sw": 41.65, "lat_ne": 42.05, "lon_sw": 12.20, "lon_ne": 12.80},
    # Lazio nord (Viterbo, Santa Marinella, Fiano Romano)
    {"lat_sw": 41.90, "lat_ne": 42.55, "lon_sw": 11.55, "lon_ne": 12.55},
    # Lazio sud-ovest (Ardea, Latina, Sabaudia, Gaeta)
    {"lat_sw": 41.00, "lat_ne": 41.65, "lon_sw": 12.40, "lon_ne": 13.30},
    # Lazio est (Anagni, Ceccano, Frosinone, Cassino)
    {"lat_sw": 41.25, "lat_ne": 42.00, "lon_sw": 13.10, "lon_ne": 14.10},
    # Lazio nord-est (Monti Sabini, Rieti)
    {"lat_sw": 42.15, "lat_ne": 42.70, "lon_sw": 12.45, "lon_ne": 13.20},
]

NETATMO_RADIUS_KM = 5.0   # raggio aggregazione per stazione progetto
NETATMO_MIN_CLUSTER = 2   # minimo stazioni Netatmo nel raggio per procedere
NETATMO_MAX_AGE_S = 5400  # dati più vecchi di 90 min → scartati (Netatmo spesso ha stazioni offline)


# ── Helper privati ────────────────────────────────────────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distanza in km tra due punti geografici (formula di Haversine)."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _circular_mean_deg(angles: list[float]) -> float:
    """
    Media circolare di angoli in gradi.
    Necessaria per la direzione del vento: media di 350° e 10° deve dare 0°,
    non 180° come farebbe la media aritmetica.
    """
    sin_sum = sum(math.sin(math.radians(a)) for a in angles)
    cos_sum = sum(math.cos(math.radians(a)) for a in angles)
    return math.degrees(math.atan2(sin_sum / len(angles), cos_sum / len(angles))) % 360


def _refresh_netatmo_token() -> str:
    """
    Ottieni un access_token fresco usando il refresh_token dal .env.
    Il refresh_token non scade; l'access_token dura ~3h.
    Viene rinnovato ad ogni run (ogni 30 min) per semplicità — nessuna cache.
    """
    client_id     = os.getenv("NETATMO_CLIENT_ID")
    client_secret = os.getenv("NETATMO_CLIENT_SECRET")
    refresh_token = os.getenv("NETATMO_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        raise EnvironmentError(
            "Credenziali Netatmo mancanti nel .env: "
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


def _parse_netatmo_station(raw: dict, now_ts: int) -> dict | None:
    """
    Estrae temperatura, umidità e (se disponibile) vento da un record getpublicdata.

    Struttura risposta Netatmo:
      raw["place"]["location"] = [longitude, latitude]   ← ordine invertito!
      raw["measures"][module_id]["type"]  = ["temperature", "humidity"]
      raw["measures"][module_id]["res"]   = {ts_str: [val_t, val_h]}
      raw["measures"][module_id]["wind_strength"] = km/h   (solo NAModule2)
      raw["measures"][module_id]["wind_angle"]    = °

    Ritorna None se dati mancanti o troppo vecchi.
    """
    try:
        lon, lat = raw["place"]["location"]  # ATTENZIONE: [lon, lat] non [lat, lon]
    except (KeyError, ValueError, TypeError):
        return None

    temperature = None
    humidity    = None
    wind_speed  = None
    wind_dir    = None
    data_ts     = None

    for mod_data in raw.get("measures", {}).values():

        # Modulo temperatura/umidità (NAModule1 o main indoor)
        mtype = mod_data.get("type", [])
        if "temperature" in mtype and "res" in mod_data:
            res = mod_data["res"]
            if not res:
                continue
            ts_str, values = next(iter(res.items()))
            ts = int(ts_str)
            if now_ts - ts > NETATMO_MAX_AGE_S:
                continue  # dato stale
            idx_t = mtype.index("temperature")
            temperature = float(values[idx_t])
            if "humidity" in mtype:
                idx_h = mtype.index("humidity")
                if len(values) > idx_h:
                    humidity = float(values[idx_h])
            data_ts = ts

        # Modulo vento (NAModule2) — struttura diversa, non ha "type"/"res"
        elif "wind_strength" in mod_data and "wind_angle" in mod_data:
            wt = mod_data.get("wind_timeutc", 0)
            if now_ts - wt <= NETATMO_MAX_AGE_S:
                wind_speed = float(mod_data["wind_strength"])  # già in km/h
                wind_dir   = float(mod_data["wind_angle"])     # gradi

    if temperature is None or data_ts is None:
        return None  # senza temperatura la stazione non ci serve

    return {
        "lat":         lat,
        "lon":         lon,
        "temperature": temperature,
        "humidity":    humidity,
        "wind_speed":  wind_speed,
        "wind_dir":    wind_dir,
    }


# ── Funzione principale ───────────────────────────────────────────────────────

def fetch_netatmo(
    project_stations: list[dict],
    dry_run: bool = False,
) -> list[dict]:
    """
    Raccoglie dati pubblici Netatmo per l'area di Roma e li aggrega per stazione progetto.

    Per ogni stazione progetto (id 1–4):
      - Trova tutte le stazioni Netatmo pubbliche entro NETATMO_RADIUS_KM km
      - Calcola mediana di temperatura, umidità e (se presente) vento
      - Applica QC a 4 livelli
      - Inserisce in 'observations' se qc_flag < 2

    Args:
        project_stations: lista da db.get_active_stations()
        dry_run:          se True, stampa senza scrivere su DB

    Returns:
        Lista dei record inseriti (o che sarebbero stati inseriti in dry_run)
    """
    from db import get_observations, insert_observation, get_client
    from qc import run_qc

    logger.info("[Netatmo] ── Avvio raccolta dati pubblici ──────────────────")

    # ── 1. Token ──────────────────────────────────────────────────────────────
    try:
        token = _refresh_netatmo_token()
    except Exception as exc:
        logger.error(f"[Netatmo] Token refresh fallito: {exc}")
        return []

    # ── 2. Fetch getpublicdata per tutti i bbox Lazio ────────────────────────
    raw_stations_all = []
    for i, bbox in enumerate(LAZIO_BBOXES):
        try:
            r = requests.get(
                NETATMO_PUBDATA_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={**bbox, "required_data": "temperature", "filter": "true"},
                timeout=30,
            )
            r.raise_for_status()
            batch = r.json().get("body", [])
            raw_stations_all.extend(batch)
            logger.info(f"[Netatmo] bbox {i+1}/{len(LAZIO_BBOXES)}: {len(batch)} stazioni raw")
            if i < len(LAZIO_BBOXES) - 1:
                time.sleep(0.5)  # rate limiting gentile
        except Exception as exc:
            logger.error(f"[Netatmo] getpublicdata bbox {i+1} fallito: {exc}")

    # Deduplica per coordinata (la stessa stazione può apparire in bbox sovrapposti)
    seen = set()
    raw_stations = []
    for s in raw_stations_all:
        try:
            loc = tuple(s["place"]["location"])
            if loc not in seen:
                seen.add(loc)
                raw_stations.append(s)
        except Exception:
            pass

    if not raw_stations:
        logger.error("[Netatmo] Nessuna stazione ottenuta da nessun bbox")
        return []

    logger.info(f"[Netatmo] {len(raw_stations)} stazioni raw totali (dopo deduplicazione)")

    # ── 3. Parsing di tutti i record ──────────────────────────────────────────
    now_ts = int(datetime.now(timezone.utc).timestamp())
    parsed = [
        s for raw in raw_stations
        if (s := _parse_netatmo_station(raw, now_ts)) is not None
    ]

    logger.info(
        f"[Netatmo] {len(parsed)} stazioni valide con dati freschi "
        f"(scartate: {len(raw_stations) - len(parsed)} stale/incomplete)"
    )

    if not parsed:
        logger.warning("[Netatmo] Nessuna stazione con dati freschi — skip")
        return []

    # ── 4. Aggregazione per stazione progetto ─────────────────────────────────
    results = []
    now_dt  = datetime.now(timezone.utc)

    for ps in project_stations:
        ps_id   = ps["id"]
        ps_name = ps.get("name", f"st.{ps_id}")
        ps_lat  = ps["lat"]
        ps_lon  = ps["lon"]

        # Stazioni Netatmo entro il raggio
        nearby = [
            s for s in parsed
            if _haversine_km(ps_lat, ps_lon, s["lat"], s["lon"]) <= NETATMO_RADIUS_KM
        ]

        if len(nearby) < NETATMO_MIN_CLUSTER:
            logger.warning(
                f"[Netatmo] st.{ps_id} ({ps_name}): {len(nearby)} stazioni entro "
                f"{NETATMO_RADIUS_KM} km — sotto la soglia ({NETATMO_MIN_CLUSTER}), skip"
            )
            continue

        # Mediana temperatura (sempre disponibile)
        temps       = [s["temperature"] for s in nearby]
        temperature = statistics.median(temps)

        # Mediana umidità (dove disponibile)
        hums     = [s["humidity"] for s in nearby if s["humidity"] is not None]
        humidity = statistics.median(hums) if hums else None

        # Vento: media circolare direzione, mediana velocità
        wind_pairs = [
            (s["wind_speed"], s["wind_dir"])
            for s in nearby
            if s["wind_speed"] is not None and s["wind_dir"] is not None
        ]
        if wind_pairs:
            wind_speed = statistics.median([w[0] for w in wind_pairs])
            wind_dir   = _circular_mean_deg([w[1] for w in wind_pairs])
        else:
            wind_speed = None
            wind_dir   = None

        logger.info(
            f"[Netatmo] st.{ps_id} ({ps_name}): {len(nearby)} stazioni | "
            f"T={temperature:.1f}°C  "
            f"H={humidity:.0f}%" if humidity else f"T={temperature:.1f}°C  H=n/a"
            + (f"  W={wind_speed:.0f}km/h {wind_dir:.0f}°" if wind_speed else "  W=n/a")
        )

        # Struttura obs per QC (run_qc richiede lat/lon e recorded_at)
        obs = {
            "station_id":     ps_id,
            "lat":            ps_lat,
            "lon":            ps_lon,
            "recorded_at":    now_dt,
            "temperature":    round(temperature, 2),
            "wind_speed":     round(wind_speed, 1)  if wind_speed is not None else None,
            "wind_direction": round(wind_dir, 1)    if wind_dir   is not None else None,
            "humidity":       round(humidity, 1)    if humidity   is not None else None,
        }

        # ── 5. QC ─────────────────────────────────────────────────────────────
        history   = get_observations(ps_id, hours=3)

        # I "neighbors" per il check spaziale sono le stazioni Netatmo stesse
        # (ricca copertura spaziale = spatial check più efficace che con i soli 4 METAR)
        neighbors_for_qc = [
            {
                "lat":         s["lat"],
                "lon":         s["lon"],
                "temperature": s["temperature"],
                "wind_speed":  s["wind_speed"] or 0.0,
            }
            for s in nearby
        ]

        qc_flag, issues = run_qc(obs, history, neighbors_for_qc)

        # Log QC issues su qc_log
        if issues:
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

        # ── 6. Insert ─────────────────────────────────────────────────────────
        raw_src = {
            "source":     "netatmo_public",
            "n_stations": len(nearby),
            "temps_raw":  [round(t, 1) for t in temps],  # per debug/audit
        }

        if not dry_run:
            try:
                obs_id = insert_observation(
                    station_id    = ps_id,
                    recorded_at   = now_dt,
                    temperature   = obs["temperature"],
                    wind_speed    = obs["wind_speed"],
                    wind_direction= obs["wind_direction"],
                    humidity      = obs["humidity"],
                    qc_flag       = qc_flag,
                    raw_source    = raw_src,
                )
                logger.info(f"[Netatmo] st.{ps_id} INSERT OK → obs_id={obs_id}")
                results.append({**obs, "qc_flag": qc_flag, "obs_id": obs_id})
            except Exception as exc:
                logger.error(f"[Netatmo] st.{ps_id} INSERT FALLITO: {exc}")
        else:
            logger.info(
                f"[Netatmo] [DRY-RUN] st.{ps_id} OK | "
                f"T={obs['temperature']}°C  qc_flag={qc_flag}"
            )
            results.append({**obs, "qc_flag": qc_flag})

    logger.info(
        f"[Netatmo] ── Fine: {len(results)}/{len(project_stations)} stazioni inserite ──"
    )
    return results
