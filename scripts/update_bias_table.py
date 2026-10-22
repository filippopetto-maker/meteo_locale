"""
update_bias_table.py — Bias IFS − Netatmo per stazione × ora UTC (tabella bias_table)

Per ogni stazione attiva (Sigillo esclusa) e ogni ora UTC:
  bias          media dei valori giornalieri (IFS − osservato) sui --window-days
                giorni precedenti a oggi (oggi escluso); NULL se i giorni con dato
                sono meno di --min-days. È la feature bias_st_hour di M2, calcolata
                come in training (experiment_retrain_netatmo.hour_bias_table).
  station_bias  media su tutte le ore della finestra: ripiego solo per l'opzione E.
Le stesse tre colonne per l'umidità relativa (bias_rh, station_bias_rh, n_samples_rh), usate
dalla prova in ombra di M2 umidità (bias_rh_hour, in punti percentuali), e per il vento
(bias_ws, station_bias_ws, n_samples_ws): differenza dei logaritmi log1p(velocità IFS) −
log1p(velocità Netatmo) in km/h, usata come bias_ws_hour da M2 vento.

Verità: osservazioni live Netatmo (QC < 2) entro ±15 min dall'ora esatta, media per ora.
IFS: Historical Forecast API, models=ecmwf_ifs, temperature_2m, relative_humidity_2m e wind_speed_10m (km/h), UTC.

Utilizzo:
    python3 scripts/update_bias_table.py --dry-run
    python3 scripts/update_bias_table.py
    python3 scripts/update_bias_table.py --window-end 2026-07-20 --dry-run   # ricalcolo per una data passata
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

logger = logging.getLogger("bias_table")

HISTORICAL_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
NWP_MODEL = "ecmwf_ifs"
EXCLUDED_STATIONS = {57}  # Sigillo: un solo device e poche osservazioni
MIN_STATION_DAYS = 7      # sotto questa soglia nessuna correzione, nemmeno station_bias


def fetch_ifs(stations: list[dict], start: pd.Timestamp, end: pd.Timestamp, retries: int = 4) -> dict[int, pd.DataFrame]:
    """Serie oraria IFS (colonne t, rh, ws), una chiamata multi-località."""
    params = {
        "latitude": ",".join(str(s["lat"]) for s in stations),
        "longitude": ",".join(str(s["lon"]) for s in stations),
        "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m", "models": NWP_MODEL, "timezone": "UTC",
        "start_date": start.date().isoformat(), "end_date": end.date().isoformat(),
    }
    for attempt in range(retries):
        try:
            r = requests.get(HISTORICAL_URL, params=params, timeout=90)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            data = r.json()
            break
        except (requests.RequestException, ValueError) as exc:
            wait = 30 * (attempt + 1)
            logger.warning(f"Historical Forecast API: {exc}, riprovo tra {wait}s")
            time.sleep(wait)
    else:
        raise RuntimeError("Historical Forecast API non risponde")
    data = data if isinstance(data, list) else [data]
    out = {}
    for st, loc in zip(stations, data):
        h = loc["hourly"]
        out[st["id"]] = pd.DataFrame({"t": h["temperature_2m"], "rh": h["relative_humidity_2m"],
                                      "ws": h["wind_speed_10m"]},
                                     index=pd.to_datetime(h["time"]).tz_localize("UTC"), dtype=float)
    return out


def hourly_obs(station_id: int, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Osservazioni Netatmo live per ora (colonne t, rh, ws): media dei valori entro ±15 min dall'ora."""
    rows = db.get_netatmo_observations(station_id, start, end)
    if not rows:
        return pd.DataFrame(columns=["t", "rh", "ws"], dtype=float)
    df = pd.DataFrame(rows)
    t = pd.to_datetime(df.recorded_at, utc=True, format="ISO8601")
    near = (t - t.dt.round("h")).abs() <= pd.Timedelta(minutes=15)
    extra = _extra(station_id, start, end, df)
    out = pd.DataFrame({"t": df.temperature[near].to_numpy(), "rh": extra["humidity"][near].to_numpy(),
                        "ws": extra["wind_speed"][near].to_numpy()}, index=t[near].dt.round("h"))
    return out.groupby(level=0).mean()


def _extra(station_id: int, start: pd.Timestamp, end: pd.Timestamp, df: pd.DataFrame) -> pd.DataFrame:
    """Umidità e vento delle stesse righe di `df` (get_netatmo_observations non li restituisce)."""
    hum, wind, offset = {}, {}, 0
    while True:
        rows = (db.get_client().table("observations").select("recorded_at, humidity, wind_speed")
                .eq("station_id", station_id).lt("qc_flag", 2)
                .gte("recorded_at", start.isoformat()).lt("recorded_at", end.isoformat())
                .order("recorded_at").range(offset, offset + 999).execute().data)
        hum.update({r["recorded_at"]: r["humidity"] for r in rows})
        wind.update({r["recorded_at"]: r["wind_speed"] for r in rows})
        if len(rows) < 1000:
            break
        offset += 1000
    return pd.DataFrame({"humidity": pd.to_numeric(df.recorded_at.map(hum), errors="coerce"),
                         "wind_speed": pd.to_numeric(df.recorded_at.map(wind), errors="coerce")})


def _hour_bias(err: pd.Series, window_end: pd.Timestamp, window_days: int):
    """(media oraria dei valori giornalieri, conteggi, giorni con dato, media complessiva)."""
    err = err.dropna()
    err = err[(err.index >= window_end - timedelta(days=window_days)) & (err.index < window_end)]
    daily = err.groupby([err.index.floor("D"), err.index.hour]).mean()  # (giorno, ora) → errore
    per_hour = daily.groupby(level=1).agg(["mean", "count"]) if len(daily) else pd.DataFrame(columns=["mean", "count"])
    return per_hour, err.index.floor("D").nunique(), len(err), (float(err.mean()) if len(err) else None)


def station_rows(station_id: int, ifs: pd.DataFrame, obs: pd.DataFrame, window_end: pd.Timestamp,
                 window_days: int, min_days: int) -> list[dict]:
    ph_t, n_days, n_t, mean_t = _hour_bias(ifs.t - obs.t.reindex(ifs.index), window_end, window_days)
    ph_h, n_days_h, n_h, mean_h = _hour_bias(ifs.rh - obs.rh.reindex(ifs.index), window_end, window_days)
    ph_w, n_days_w, n_w, mean_w = _hour_bias(np.log1p(ifs.ws.clip(lower=0)) - np.log1p(obs.ws.reindex(ifs.index).clip(lower=0)),
                                             window_end, window_days)
    enough, enough_h, enough_w = n_days >= MIN_STATION_DAYS, n_days_h >= MIN_STATION_DAYS, n_days_w >= MIN_STATION_DAYS
    rows = []
    for hour in range(24):
        n = int(ph_t["count"].get(hour, 0))
        n_hh = int(ph_h["count"].get(hour, 0))
        n_ww = int(ph_w["count"].get(hour, 0))
        bias_w = float(ph_w["mean"][hour]) if enough_w and n_ww >= min_days else None
        bias = float(ph_t["mean"][hour]) if enough and n >= min_days else None
        bias_h = float(ph_h["mean"][hour]) if enough_h and n_hh >= min_days else None
        rows.append({
            "station_id": station_id, "hour_utc": hour,
            "bias": None if bias is None else round(bias, 3),
            "station_bias": round(mean_t, 3) if enough and mean_t is not None else None,
            "n_samples": n, "n_station": int(n_t), "window_days": window_days,
            "bias_rh": None if bias_h is None else round(bias_h, 3),
            "station_bias_rh": round(mean_h, 3) if enough_h and mean_h is not None else None,
            "n_samples_rh": n_hh,
            "bias_ws": None if bias_w is None else round(bias_w, 4),
            "station_bias_ws": round(mean_w, 4) if enough_w and mean_w is not None else None,
            "n_samples_ws": n_ww,
            "window_end": window_end.isoformat(), "nwp_model": NWP_MODEL,
            "computed_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
        })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Aggiorna bias_table (IFS − Netatmo, stazione × ora UTC)")
    ap.add_argument("--dry-run", action="store_true", help="Calcola e stampa, non scrive")
    ap.add_argument("--window-days", type=int, default=30)
    ap.add_argument("--min-days", type=int, default=10)
    ap.add_argument("--window-end", default=None, help="Giorno escluso di fine finestra (default: oggi UTC)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    window_end = (pd.Timestamp(args.window_end, tz="UTC") if args.window_end
                  else pd.Timestamp.now(tz="UTC")).floor("D")
    start = window_end - timedelta(days=args.window_days)
    stations = sorted((s for s in db.get_active_stations() if s["id"] not in EXCLUDED_STATIONS),
                      key=lambda s: s["id"])
    logger.info(f"Finestra {start:%Y-%m-%d} → {window_end - timedelta(days=1):%Y-%m-%d}, {len(stations)} stazioni")

    ifs = fetch_ifs(stations, start, window_end - timedelta(days=1))
    all_rows, skipped = [], []
    for st in stations:
        rows = station_rows(st["id"], ifs[st["id"]], hourly_obs(st["id"], start, window_end),
                            window_end, args.window_days, args.min_days)
        n_null = sum(r["bias"] is None for r in rows)
        if rows[0]["station_bias"] is None:
            skipped.append(f"st.{st['id']} (meno di {MIN_STATION_DAYS} giorni di osservazioni)")
        elif n_null:
            skipped.append(f"st.{st['id']} ({n_null} ore senza bias)")
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    by_hour = df.groupby("hour_utc").bias.mean().round(2)
    print("\nBias medio IFS − Netatmo per ora UTC (atteso ~0 al mattino, da −2 a −3 °C pomeriggio/sera):")
    print(" ".join(f"{h:02d}:{v:+.1f}" for h, v in by_hour.items()))
    by_hour_h = df.groupby("hour_utc").bias_rh.mean().round(1)
    print("Bias medio IFS − Netatmo dell'umidità relativa per ora UTC (punti %):")
    print(" ".join(f"{h:02d}:{v:+.1f}" for h, v in by_hour_h.items()))
    by_hour_w = df.groupby("hour_utc").bias_ws.mean().round(2)
    print("Bias medio del vento (log1p IFS − log1p Netatmo) per ora UTC (positivo: IFS più forte):")
    print(" ".join(f"{h:02d}:{v:+.2f}" for h, v in by_hour_w.items()))
    print(f"Stazioni corrette: {df.groupby('station_id').bias.apply(lambda b: b.notna().any()).sum()}/{len(stations)}"
          + (f" | incomplete o saltate: {', '.join(skipped)}" if skipped else ""))

    if args.dry_run:
        logger.info("--dry-run: nessuna scrittura")
        return
    n = db.upsert_bias_table(all_rows)
    logger.info(f"bias_table: {n} righe scritte (window_end {window_end:%Y-%m-%d})")


if __name__ == "__main__":
    main()
