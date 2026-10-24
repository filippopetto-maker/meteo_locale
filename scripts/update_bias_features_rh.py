"""
update_bias_features_rh.py — Feature di bias dell'umidità per M2 umidità H1b (tabella bias_features_rh)

Stesse sei feature di update_bias_features.py (M2b) applicate al bias dell'umidità relativa
(RH IFS − RH Netatmo, punti %), per stazione e ora UTC, con i dati fino a ieri (oggi escluso):
bias_rh_hour (30 giorni), bias_h7, bias_h90, bias_all30, bias_d1, bias_d3. Usa station_features di
update_bias_features.py e le funzioni di update_bias_table.py (IFS Historical Forecast API, osservazioni
live Netatmo QC<2 entro ±15 min dall'ora). Scrive solo bias_features_rh.

Utilizzo:
    python3 scripts/update_bias_features_rh.py --dry-run
    python3 scripts/update_bias_features_rh.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import update_bias_features as ubf  # noqa: E402
import update_bias_table as ub  # noqa: E402

logger = logging.getLogger("bias_features_rh")
FEATURES = ["bias_rh_hour", "bias_h7", "bias_h90", "bias_all30", "bias_d1", "bias_d3"]
SOURCE = ["bias_st_hour", "bias_h7", "bias_h90", "bias_all30", "bias_d1", "bias_d3"]  # nomi in update_bias_features


def upsert(rows: list[dict], chunk: int = 500) -> int:
    for i in range(0, len(rows), chunk):
        db.get_client().table("bias_features_rh").upsert(rows[i:i + chunk], on_conflict="station_id,hour_utc").execute()
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Aggiorna bias_features_rh (feature di bias di M2 umidità H1b)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--window-end", default=None, help="Giorno escluso di fine finestra (default: oggi UTC)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    window_end = (pd.Timestamp(args.window_end, tz="UTC") if args.window_end else pd.Timestamp.now(tz="UTC")).floor("D")
    start = window_end - timedelta(days=90)
    stations = sorted((s for s in db.get_active_stations() if s["id"] not in ub.EXCLUDED_STATIONS), key=lambda s: s["id"])
    logger.info(f"Finestra {start:%Y-%m-%d} → {window_end - timedelta(days=1):%Y-%m-%d}, {len(stations)} stazioni")

    ifs = ub.fetch_ifs(stations, start, window_end - timedelta(days=1))
    rows, missing = [], []
    for st in stations:
        obs = ub.hourly_obs(st["id"], start, window_end)
        f = ubf.station_features(ifs[st["id"]].rh, obs.rh, window_end)
        if f.bias_st_hour.isna().all():
            missing.append(st["id"])
        for hour, r in f.iterrows():
            rows.append({"station_id": st["id"], "hour_utc": int(hour),
                         **{k: (None if pd.isna(r[s]) else round(float(r[s]), 3)) for k, s in zip(FEATURES, SOURCE)},
                         "window_end": window_end.isoformat(),
                         "computed_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")})
    df = pd.DataFrame(rows)
    print("\nMedia sulle stazioni (punti %, RH IFS − Netatmo):")
    print(df[FEATURES].mean().round(2).to_string())
    print("Celle senza valore:", df[FEATURES].isna().sum().to_dict(), "| stazioni senza bias:", missing or "-")
    if args.dry_run:
        logger.info("--dry-run: nessuna scrittura")
        return
    logger.info(f"bias_features_rh: {upsert(rows)} righe scritte (window_end {window_end:%Y-%m-%d})")


if __name__ == "__main__":
    main()
