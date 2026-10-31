"""
export_forecast_48h.py — forecasts_48h → docs/data/forecast_48h.json

Formato compatto, una voce per stazione, serie ordinate per valid_for:

  {"generated_at": "...", "issue_at": "...", "model_tag": "m2-...",
   "stations": {"<id>": {"name": "...", "valid_for": [...ISO...], "lead": [...],
                          "t": [...], "rh": [...], "ws": [...], "wd": [...],
                          "src": {"t": "m2", "rh": "ifs", "ws": "ifs"}}}}

Lo esegue publish.yml (modo static) dal 31/10/2026; la mappa lo usa per lo scorrimento orario.
Se l'ultima emissione è più vecchia di --max-age-h ore non scrive nulla (esce con errore) e il sito
tiene la copia precedente.

Utilizzo:
    python3 scripts/export_forecast_48h.py --out docs/data/forecast_48h.json
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import numpy as np  # noqa: E402

from grid import compute_sea_blend_weight  # noqa: E402

# Stessa griglia di scripts/export_static.py (temp_grid_* di latest.json)
LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, NX, NY = 41.18, 42.85, 11.40, 14.05, 250, 220

logger = logging.getLogger("export_forecast_48h")


def build(rows: list[dict], names: dict[int, str]) -> dict:
    by_station: dict[int, list[dict]] = {}
    for r in rows:
        by_station.setdefault(r["station_id"], []).append(r)
    stations = {}
    for sid, rs in sorted(by_station.items()):
        rs.sort(key=lambda r: r["valid_for"])
        stations[str(sid)] = {
            "name": names.get(sid, ""),
            "valid_for": [r["valid_for"] for r in rs],
            "lead": [r["lead_hours"] for r in rs],
            "t": [r["temperature"] for r in rs],
            "rh": [r["humidity"] for r in rs],
            "ws": [r["wind_speed"] for r in rs],
            "wd": [r["wind_direction"] for r in rs],
            "src": {"t": rs[0]["temp_source"], "rh": rs[0]["hum_source"], "ws": rs[0]["wind_source"]},
        }
    issue = max((r["issue_at"] for r in rows), default=None)
    tags = sorted({r["model_tag"] for r in rows if r.get("model_tag")})
    return {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "issue_at": issue, "model_tag": ",".join(tags), "stations": stations, "land_weight": land_weight()}


def land_weight() -> dict:
    """Peso terra (1 − peso del blend SST) sulla griglia di latest.json, 0–100 in byte base64.
    La mappa lo usa per non applicare al mare la variazione oraria delle stazioni di terra."""
    w = 1.0 - compute_sea_blend_weight(LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, NX, NY)
    raw = np.clip(np.round(w * 100), 0, 100).astype(np.uint8).tobytes()
    return {"nx": NX, "ny": NY, "b64": base64.b64encode(raw).decode()}


def main() -> None:
    ap = argparse.ArgumentParser(description="forecasts_48h → forecast_48h.json")
    ap.add_argument("--out", default=str(ROOT / "docs" / "data" / "forecast_48h.json"))
    ap.add_argument("--max-age-h", type=float, default=6.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    rows = db.get_forecasts_48h()
    if not rows:
        logger.error("forecasts_48h è vuota: nessun file scritto")
        sys.exit(1)
    newest = max(datetime.fromisoformat(r["issue_at"]) for r in rows)
    age_h = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
    if age_h > args.max_age_h:
        logger.error(f"forecasts_48h: ultima emissione {newest:%Y-%m-%d %H:%M}Z, vecchia di {age_h:.1f} h: nessun file scritto")
        sys.exit(1)
    # Solo l'ultima emissione: le ore di un'emissione precedente non ancora cancellate
    # (valid_for = issue_at della nuova) finirebbero in testa alle serie
    rows = [r for r in rows if datetime.fromisoformat(r["issue_at"]) == newest]
    names = {s["id"]: s["name"] for s in db.get_active_stations()}
    payload = build(rows, names)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, separators=(",", ":")))
    logger.info(f"{out}: {len(payload['stations'])} stazioni, {len(rows)} ore, {out.stat().st_size / 1e3:.0f} kB, "
                f"emissione {payload['issue_at']}")


if __name__ == "__main__":
    main()
