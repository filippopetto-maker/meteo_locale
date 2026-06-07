"""
qc.py — Quality Control per dati meteo grezzi
Blocco 2 - Strato 1

Quattro livelli di controllo in sequenza:
  1. Range check          → valori fisicamente impossibili (barriera assoluta)
  2. Climatological check → valori impossibili per il mese e fascia oraria
  3. Persistence          → sensore bloccato
  4. Spatial check        → outlier rispetto alle stazioni vicine

Ritorna il dato con qc_flag aggiornato:
  0 = ok
  1 = sospetto (usato con cautela)
  2 = scartato (escluso dal modello)
"""

import math
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── Soglie fisiche assolute ───────────────────────────────────────────────────
RANGE_LIMITS = {
    "temperature":    (-15.0, 50.0),
    "wind_speed":     (0.0,   150.0),
    "wind_direction": (0.0,   360.0),
    "humidity":       (0.0,   100.0),
    "pressure":       (870.0, 1084.0),
}

PERSISTENCE_WINDOW = 3
SPATIAL_SIGMA      = 3.0

# ── Climatologia Roma (aggiornata ai cambiamenti climatici 2015-2024) ─────────
# mese → (min_notte, max_giorno, min_assoluta, max_assoluta)
CLIMA_ROMA = {
    1:  (  2.0,  18.0,  -8.0,  24.0),
    2:  (  2.5,  19.0,  -6.0,  26.0),
    3:  (  5.0,  22.0,  -3.0,  30.0),
    4:  (  8.0,  25.0,   1.0,  34.0),
    5:  ( 12.0,  30.0,   5.0,  38.0),
    6:  ( 16.0,  35.0,  10.0,  42.0),
    7:  ( 19.0,  38.0,  13.0,  46.0),  # luglio 2023: +3°C sui record storici
    8:  ( 19.0,  37.0,  13.0,  45.0),
    9:  ( 15.0,  31.0,   8.0,  39.0),
    10: ( 11.0,  26.0,   3.0,  33.0),
    11: (  6.0,  20.0,  -2.0,  27.0),
    12: (  3.0,  17.0,  -6.0,  23.0),
}

# ── Offset termico per tipo di stazione ──────────────────────────────────────
# Modifica le soglie climatologiche in base al contesto fisico
STATION_TEMP_OFFSET = {
    "standard":      0.0,   # posizione neutra
    "esposta_sole": +5.0,   # tetto, terrazzo soleggiato
    "urban_canyon": +3.0,   # centro storico, isola di calore
    "verde_parco":  -2.0,   # parco, zona verde
    "costiera":     -1.0,   # vicino al mare
    "quota":        -3.0,   # stazione in quota
}

# Mappa stazione_id → tipo (aggiorna quando aggiungi stazioni)
STATION_TYPES = {
    1: "standard",      # Roma Nord
    2: "urban_canyon",  # Roma Centro
    3: "standard",      # Roma Sud Casal Palocco
    4: "costiera",      # Ostia
}

def _fascia_oraria(ora: int) -> str:
    if 0 <= ora < 6:   return "notte"
    elif 6 <= ora < 10: return "mattina"
    elif 10 <= ora < 18: return "giorno"
    else: return "sera"


# ── 1. RANGE CHECK ───────────────────────────────────────────────────────────

def range_check(observation: dict) -> tuple[int, list[dict]]:
    issues = []
    for field, (low, high) in RANGE_LIMITS.items():
        value = observation.get(field)
        if value is None:
            continue
        if not (low <= value <= high):
            issues.append({
                "check_type": "range_check", "field_name": field,
                "original_value": value,
                "reason": f"{field} = {value} fuori range [{low}, {high}]"
            })
    if not issues:
        return 0, []
    critical = {"temperature", "wind_speed"}
    flagged  = {i["field_name"] for i in issues}
    return (2 if flagged & critical else 1), issues


# ── 2. CLIMATOLOGICAL CHECK ──────────────────────────────────────────────────

def climatological_check(observation: dict, recorded_at: datetime) -> tuple[int, list[dict]]:
    temperature = observation.get("temperature")
    if temperature is None:
        return 0, []

    mese   = recorded_at.month
    ora    = recorded_at.hour
    fascia = _fascia_oraria(ora)

    min_notte, max_giorno, min_ass, max_ass = CLIMA_ROMA[mese]

    station_id   = observation.get("station_id")
    station_type = STATION_TYPES.get(station_id, "standard")
    offset       = STATION_TEMP_OFFSET.get(station_type, 0.0)

    min_ass_c    = min_ass    - abs(offset) * 0.5
    max_ass_c    = max_ass    + offset
    min_notte_c  = min_notte  - abs(offset) * 0.5
    max_giorno_c = max_giorno + offset

    issues = []

    # Barriera 1: record assoluti mensili corretti per stazione
    if temperature < min_ass_c:
        issues.append({
            "check_type": "climatological_check", "field_name": "temperature",
            "original_value": temperature,
            "reason": (f"Temp {temperature}°C sotto minimo assoluto corretto "
                       f"{min_ass_c}°C per mese {mese} (stazione: {station_type}, offset: {offset:+.1f}°C)")
        })
        return 2, issues

    if temperature > max_ass_c:
        issues.append({
            "check_type": "climatological_check", "field_name": "temperature",
            "original_value": temperature,
            "reason": (f"Temp {temperature}°C sopra massimo assoluto corretto "
                       f"{max_ass_c}°C per mese {mese} (stazione: {station_type}, offset: {offset:+.1f}°C)")
        })
        return 2, issues

    # Barriera 2: plausibilità per fascia oraria
    # Tolleranza bilanciata: scostamento di 6°C dalla soglia tipica → sospetto
    qc_flag = 0
    if fascia == "notte" and temperature < min_notte_c - 6:
        issues.append({
            "check_type": "climatological_check", "field_name": "temperature",
            "original_value": temperature,
            "reason": f"Temp {temperature}°C molto bassa di notte per mese {mese} (min tipica: {min_notte_c}°C)"
        })
        qc_flag = 1
    elif fascia == "notte" and temperature > max_giorno_c + 3:
        issues.append({
            "check_type": "climatological_check", "field_name": "temperature",
            "original_value": temperature,
            "reason": f"Temp {temperature}°C anomalmente alta di notte per mese {mese} (max diurna: {max_giorno_c}°C)"
        })
        qc_flag = 1
    elif fascia == "giorno" and temperature < min_notte_c - 4:
        issues.append({
            "check_type": "climatological_check", "field_name": "temperature",
            "original_value": temperature,
            "reason": f"Temp {temperature}°C anomalmente bassa di giorno per mese {mese} (min notturna: {min_notte_c}°C)"
        })
        qc_flag = 1
    elif fascia == "giorno" and temperature > max_giorno_c + 5:
        issues.append({
            "check_type": "climatological_check", "field_name": "temperature",
            "original_value": temperature,
            "reason": f"Temp {temperature}°C molto alta di giorno per mese {mese} (max tipica: {max_giorno_c}°C)"
        })
        qc_flag = 1

    return qc_flag, issues


# ── 3. PERSISTENCE CHECK ─────────────────────────────────────────────────────

def persistence_check(current: dict, history: list[dict], window: int = PERSISTENCE_WINDOW) -> tuple[int, list[dict]]:
    if len(history) < window:
        return 0, []
    issues = []
    recent = history[-window:]
    for field in ("temperature", "wind_speed"):
        current_val = current.get(field)
        if current_val is None:
            continue
        hist_vals = [r.get(field) for r in recent if r.get(field) is not None]
        if len(hist_vals) < window:
            continue
        if all(v == current_val for v in hist_vals):
            issues.append({
                "check_type": "persistence_check", "field_name": field,
                "original_value": current_val,
                "reason": f"{field} = {current_val} invariato per {window+1} osservazioni consecutive — sensore bloccato?"
            })
    return (1 if issues else 0), issues


# ── 4. SPATIAL CHECK ─────────────────────────────────────────────────────────

def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def spatial_check(current: dict, neighbors: list[dict], max_radius_km=50.0, sigma=SPATIAL_SIGMA) -> tuple[int, list[dict]]:
    if not neighbors:
        return 0, []
    nearby = [n for n in neighbors if 0 < _haversine_km(current["lat"], current["lon"], n["lat"], n["lon"]) <= max_radius_km]
    if len(nearby) < 2:
        return 0, []
    issues = []
    for field in ("temperature", "wind_speed"):
        current_val = current.get(field)
        if current_val is None:
            continue
        vals = [n[field] for n in nearby if n.get(field) is not None]
        if len(vals) < 2:
            continue
        mean = sum(vals) / len(vals)
        std  = math.sqrt(sum((v-mean)**2 for v in vals) / len(vals))
        if std < 0.1:
            continue
        z = abs(current_val - mean) / std
        if z > sigma:
            issues.append({
                "check_type": "spatial_check", "field_name": field,
                "original_value": current_val,
                "reason": f"{field} = {current_val} è {z:.1f}σ dalla media spaziale ({mean:.1f}) di {len(vals)} stazioni vicine"
            })
    if not issues:
        return 0, []
    max_z = max(
        abs(current.get(i["field_name"], 0) - sum(n.get(i["field_name"], 0) for n in nearby)/len(nearby))
        / (math.sqrt(sum((n.get(i["field_name"],0) - sum(n.get(i["field_name"],0) for n in nearby)/len(nearby))**2 for n in nearby)/len(nearby)) or 1)
        for i in issues
    )
    return (2 if max_z > sigma*2 else 1), issues


# ── PIPELINE COMPLETA ─────────────────────────────────────────────────────────

def run_qc(observation: dict, history: list[dict], neighbors: list[dict]) -> tuple[int, list[dict]]:
    all_issues = []
    final_flag = 0

    # 1. Range check
    flag, issues = range_check(observation)
    all_issues.extend(issues)
    if flag == 2:
        logger.warning(f"[QC] st.{observation.get('station_id')} SCARTATO range_check")
        return 2, all_issues
    final_flag = max(final_flag, flag)

    # 2. Climatological check
    recorded_at = observation.get("recorded_at")
    if recorded_at:
        if isinstance(recorded_at, str):
            recorded_at = datetime.fromisoformat(recorded_at)
        flag, issues = climatological_check(observation, recorded_at)
        all_issues.extend(issues)
        if flag == 2:
            logger.warning(f"[QC] st.{observation.get('station_id')} SCARTATO climatological_check")
            return 2, all_issues
        if flag == 1:
            logger.warning(f"[QC] st.{observation.get('station_id')} SOSPETTO climatological_check")
        final_flag = max(final_flag, flag)

    # 3. Persistence check
    flag, issues = persistence_check(observation, history)
    all_issues.extend(issues)
    if flag > 0:
        logger.warning(f"[QC] st.{observation.get('station_id')} SOSPETTO persistence_check")
    final_flag = max(final_flag, flag)

    # 4. Spatial check
    flag, issues = spatial_check(observation, neighbors)
    all_issues.extend(issues)
    if flag == 2:
        logger.warning(f"[QC] st.{observation.get('station_id')} SCARTATO spatial_check")
        return 2, all_issues
    final_flag = max(final_flag, flag)

    if final_flag == 0:
        logger.debug(f"[QC] st.{observation.get('station_id')} OK")

    return final_flag, all_issues


# ── TEST STANDALONE ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)

    tests = [
        ("Dato normale",                      {"station_id":1,"lat":41.9,"lon":12.5,"temperature":18.5,"wind_speed":12.0,"wind_direction":270.0}, [], []),
        ("Temperatura impossibile (80°C)",    {"station_id":1,"lat":41.9,"lon":12.5,"temperature":80.0,"wind_speed":12.0,"wind_direction":270.0}, [], []),
        ("Sensore bloccato",                  {"station_id":1,"lat":41.9,"lon":12.5,"temperature":15.0,"wind_speed":5.0,"wind_direction":180.0},
                                              [{"temperature":15.0,"wind_speed":5.0},{"temperature":15.0,"wind_speed":5.0},{"temperature":15.0,"wind_speed":5.0}], []),
        ("Agosto giorno 4°C → scartato",     {"station_id":1,"lat":41.9,"lon":12.5,"temperature":4.0,"wind_speed":5.0,"wind_direction":90.0,"recorded_at":"2025-08-15T14:00:00"}, [], []),
        ("Agosto notte 5°C → scartato",      {"station_id":1,"lat":41.9,"lon":12.5,"temperature":5.0,"wind_speed":2.0,"wind_direction":180.0,"recorded_at":"2025-08-15T03:00:00"}, [], []),
        ("Maggio giorno 36°C → sospetto",    {"station_id":1,"lat":41.9,"lon":12.5,"temperature":36.0,"wind_speed":2.0,"wind_direction":180.0,"recorded_at":"2025-05-15T14:00:00"}, [], []),
        ("Gennaio notte 1°C → ok",           {"station_id":1,"lat":41.9,"lon":12.5,"temperature":1.0,"wind_speed":8.0,"wind_direction":0.0,"recorded_at":"2025-01-10T04:00:00"}, [], []),
        ("Luglio tetto esposto 48°C → sosp", {"station_id":2,"lat":41.9,"lon":12.5,"temperature":48.0,"wind_speed":5.0,"wind_direction":200.0,"recorded_at":"2025-07-20T14:00:00"}, [], []),
        ("Outlier spaziale (45°C vs ~18°C)", {"station_id":3,"lat":41.90,"lon":12.49,"temperature":45.0,"wind_speed":10.0,"wind_direction":90.0},
                                              [],
                                              [{"lat":41.75,"lon":12.36,"temperature":18.0,"wind_speed":10.0},
                                               {"lat":41.73,"lon":12.28,"temperature":17.5,"wind_speed":11.0},
                                               {"lat":42.01,"lon":12.50,"temperature":18.5,"wind_speed":9.0}]),
    ]

    print("\n" + "="*70)
    print(f"{'TEST':<40} {'FLAG':<6} {'ESITO'}")
    print("="*70)
    for nome, obs, hist, neigh in tests:
        flag, issues = run_qc(obs, hist, neigh)
        esito = {0:"✅ OK", 1:"⚠️  SOSPETTO", 2:"❌ SCARTATO"}[flag]
        reason = issues[0]["reason"][:50] + "..." if issues else ""
        print(f"{nome:<40} {flag:<6} {esito}")
        if reason:
            print(f"  → {reason}")
    print("="*70)
