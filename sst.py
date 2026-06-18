"""
sst.py — Sea Surface Temperature da Open-Meteo Marine API, con cache su file.
Pure module-style, no DB dependencies (coerente con grid.py).
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

MARINE_API_URL = "https://marine-api.open-meteo.com/v1/marine"

# Punti offshore lungo la costa laziale (5-10 km al largo, per evitare celle
# di costa del modello marino che potrebbero tornare null).
# Coordinate da VERIFICARE empiricamente con una chiamata di test: se un punto
# torna null, spostarlo di 0.05-0.1° più al largo.
SST_POINTS = [
    {"name": "Civitavecchia", "lat": 42.05, "lon": 11.72},
    {"name": "Fiumicino",     "lat": 41.75, "lon": 12.10},
    {"name": "Anzio",         "lat": 41.43, "lon": 12.55},
    {"name": "Sabaudia",      "lat": 41.22, "lon": 12.95},
    {"name": "Gaeta",         "lat": 41.15, "lon": 13.45},
]

CACHE_PATH = Path(__file__).resolve().parent / "data" / "sst_cache.json"
CACHE_MAX_AGE_H = 4  # l'SST cambia lentamente, non serve aggiornarla ogni 30 min


def _fetch_sst_live(points: list[dict], max_retries: int = 3) -> dict[str, float] | None:
    """Chiama la Marine API per tutti i punti in un'unica request (lat/lon CSV)."""
    lats = ",".join(f"{p['lat']:.3f}" for p in points)
    lons = ",".join(f"{p['lon']:.3f}" for p in points)
    for attempt in range(max_retries):
        try:
            r = requests.get(
                MARINE_API_URL,
                params={"latitude": lats, "longitude": lons, "hourly": "sea_surface_temperature"},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                data = [data]
            result = {}
            for point, loc in zip(points, data):
                vals = loc["hourly"]["sea_surface_temperature"]
                # primo valore non-null (la API ritorna anche storico/forecast)
                val = next((v for v in vals if v is not None), None)
                if val is not None:
                    result[point["name"]] = float(val)
            if len(result) < 2:
                return None  # troppi punti falliti, non fidarsi del batch
            return result
        except Exception as exc:
            if attempt < max_retries - 1:
                time.sleep(5 * (attempt + 1))
            else:
                print(f"[SST] fetch fallito dopo {max_retries} tentativi: {exc}")
                return None


def get_sst_values(points: list[dict] = SST_POINTS) -> dict[str, float] | None:
    """
    Ritorna {nome_punto: sst_celsius}, usando la cache su file se recente.
    La cache vive nel repo (data/sst_cache.json) e viene committata dal
    workflow insieme a latest.json/wind_grid.json — i runner di GitHub
    Actions sono stateless tra un run e l'altro, quindi senza commit la
    cache non persisterebbe.
    """
    now = datetime.now(timezone.utc)

    if CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text())
            cached_at = datetime.fromisoformat(cached["fetched_at"])
            age_h = (now - cached_at).total_seconds() / 3600
            if age_h < CACHE_MAX_AGE_H:
                return cached["values"]
        except Exception:
            pass  # cache corrotta o assente, si procede al fetch live

    fresh = _fetch_sst_live(points)
    if fresh is None:
        # fallback: usa la cache anche se scaduta, se esiste
        if CACHE_PATH.exists():
            try:
                cached = json.loads(CACHE_PATH.read_text())
                return cached["values"]
            except Exception:
                pass
        return None  # nessun dato disponibile, il chiamante deve gestire il fallback

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps({"fetched_at": now.isoformat(), "values": fresh}, indent=2))
    return fresh
