"""
update_bias_features.py — Feature di bias aggiuntive di M2b (tabella bias_features)

Per ogni stazione attiva (Sigillo esclusa) e ogni ora UTC, con i dati fino a ieri (oggi escluso):
  bias_st_hour  media dei valori giornalieri (IFS − Netatmo) alla stessa ora UTC, 30 giorni, minimo 10
  bias_h7       come sopra su 7 giorni, minimo 4
  bias_h90      come sopra su 90 giorni, minimo 30
  bias_all30    media su tutte le ore del bias giornaliero, 30 giorni, minimo 10
  bias_d1       bias medio dell'ultimo giorno (ieri)
  bias_d3       bias medio degli ultimi 3 giorni, minimo 2
Stesse definizioni di scripts/experiment_m2b_lag.py (with_lag), che calcola le feature di training.
La tabella vale per valid_for da oggi a dopodomani: il ritardo rispetto ai dati è 1, 2 o 3 giorni
(M2b lo riceve come feature bias_lag).

Verità e IFS come update_bias_table.py (osservazioni live Netatmo QC<2 entro ±15 min dall'ora,
Historical Forecast API ecmwf_ifs).

Utilizzo:
    python3 scripts/update_bias_features.py --dry-run
    python3 scripts/update_bias_features.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import update_bias_table as ub  # noqa: E402

logger = logging.getLogger("bias_features")

FEATURES = ["bias_st_hour", "bias_h7", "bias_h90", "bias_all30", "bias_d1", "bias_d3"]


def station_features(ifs_t: pd.Series, obs_t: pd.Series, window_end: pd.Timestamp) -> pd.DataFrame:
    """24 righe (ora UTC) × 6 feature, con i dati dei giorni precedenti a window_end."""
    err = (ifs_t - obs_t.reindex(ifs_t.index)).dropna()
    err = err[(err.index >= window_end - timedelta(days=90)) & (err.index < window_end)]
    days = pd.date_range(window_end - timedelta(days=90), window_end - timedelta(days=1), freq="D", tz="UTC")
    piv = (err.groupby([err.index.floor("D"), err.index.hour]).mean().unstack(level=1)
           .reindex(index=days, columns=range(24)))
    daily = piv.mean(axis=1)

    def per_hour(n: int, min_days: int) -> pd.Series:
        w = piv.tail(n)
        return w.mean().where(w.count() >= min_days)

    def level(n: int, min_days: int) -> float:
        w = daily.tail(n)
        return float(w.mean()) if w.count() >= min_days else np.nan

    out = pd.DataFrame(index=range(24))
    out["bias_st_hour"] = per_hour(30, 10)
    out["bias_h7"] = per_hour(7, 4)
    out["bias_h90"] = per_hour(90, 30)
    out["bias_all30"] = level(30, 10)
    out["bias_d1"] = float(daily.iloc[-1]) if pd.notna(daily.iloc[-1]) else np.nan
    out["bias_d3"] = level(3, 2)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Aggiorna bias_features (feature di bias di M2b)")
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
        f = station_features(ifs[st["id"]].t, obs.t, window_end)
        if f.bias_st_hour.isna().all():
            missing.append(st["id"])
        for hour, r in f.iterrows():
            rows.append({"station_id": st["id"], "hour_utc": int(hour),
                         **{k: (None if pd.isna(r[k]) else round(float(r[k]), 3)) for k in FEATURES},
                         "window_end": window_end.isoformat(),
                         "computed_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")})
    df = pd.DataFrame(rows)
    print("\nMedia sulle stazioni (°C, IFS − Netatmo):")
    print(df[FEATURES].mean().round(2).to_string())
    print("Celle senza valore:", df[FEATURES].isna().sum().to_dict(), "| stazioni senza bias:", missing or "-")
    if args.dry_run:
        logger.info("--dry-run: nessuna scrittura")
        return
    n = db.upsert_bias_features(rows)
    logger.info(f"bias_features: {n} righe scritte (window_end {window_end:%Y-%m-%d})")


if __name__ == "__main__":
    main()
