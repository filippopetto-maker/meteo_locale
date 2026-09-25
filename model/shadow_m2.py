"""
shadow_m2.py — Prova in ombra 1–48 h: IFS grezzo, opzione E, modello attuale, M2

Un'emissione per esecuzione (issue_at = ora UTC corrente troncata all'ora),
stesso input ECMWF IFS (Forecast API, past_days=2, forecast_days=4) per le
quattro varianti, scritte in forecasts_shadow. Non tocca forecasts né la mappa.

  t_ifs  IFS a valid_for                     (inference.predict_from_df, nwp_temperature)
  t_v1   modello attuale LGBM + RF + ARSIAL  (inference.predict_from_df, temperature)
  t_e    t_ifs − bias (ripiego station_bias) da bias_table
  t_m2   M2 congelato in model/m2/           (model/m2.py)

Trigger: cron-job.org alle 09:15 e 21:15 UTC (.github/workflows/shadow-m2.yml).

Utilizzo:
    python3 model/shadow_m2.py --dry-run
    python3 model/shadow_m2.py
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (_PROJECT_ROOT, _PROJECT_ROOT / "model"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import db  # noqa: E402
import inference as inf  # noqa: E402
from m2 import M2  # noqa: E402

logger = logging.getLogger("shadow_m2")

NWP_MODEL = "ecmwf_ifs"
IFS_META_URL = "https://api.open-meteo.com/data/ecmwf_ifs025/static/meta.json"
MAX_RUN_AGE_H = 15


def ifs_run_time() -> datetime | None:
    try:
        ts = requests.get(IFS_META_URL, timeout=20).json()["last_run_initialisation_time"]
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning(f"meta.json IFS non disponibile ({exc}): nwp_run_at resta NULL")
        return None


def load_bias() -> dict[tuple[int, int], tuple[float | None, float | None]]:
    """(stazione, ora UTC) → (bias, station_bias), una sola query."""
    return {(r["station_id"], r["hour_utc"]): (r["bias"], r["station_bias"]) for r in db.get_bias_table()}


def _r(x) -> float | None:
    return None if x is None or pd.isna(x) else round(float(x), 2)


def run(leads: list[int], dry_run: bool, json_out: str | None) -> None:
    m2 = M2()
    issue_at = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    run_at = ifs_run_time()
    if run_at is not None and issue_at - run_at > timedelta(hours=MAX_RUN_AGE_H):
        logger.warning(f"Run IFS vecchio: {run_at:%Y-%m-%d %HZ} ({(issue_at - run_at).total_seconds() / 3600:.0f} h)")
    excluded = set(m2.meta["excluded_stations"])
    stations = sorted((s for s in db.get_active_stations() if s["id"] not in excluded), key=lambda s: s["id"])
    bias = load_bias()
    logger.info(f"Emissione {issue_at:%Y-%m-%d %H:%M}Z, run IFS {run_at:%Y-%m-%d %HZ} " if run_at else
                f"Emissione {issue_at:%Y-%m-%d %H:%M}Z ")
    logger.info(f"{len(stations)} stazioni, lead {leads[0]}–{leads[-1]}, modello {m2.tag}, "
                f"bias_table {len(bias)} celle")

    rows, no_bias, failed = [], [], []
    for st in stations:
        sid = st["id"]
        try:
            df = inf.fetch_forecast(st["lat"], st["lon"], past_days=2, forecast_days=4, model=NWP_MODEL)
        except Exception as exc:  # una stazione in errore non deve fermare le altre
            logger.error(f"st.{sid}: fetch IFS fallito ({exc})")
            failed.append(sid)
            continue
        base = inf.predict_from_df(df.copy(), st, leads, now_utc=issue_at)
        hours = {h: bias.get((sid, h), (None, None)) for h in range(24)}
        if all(b is None for b, _ in hours.values()):
            no_bias.append(sid)
        t_m2 = m2.predict(df, st, [pd.Timestamp(p["valid_for"]) for p in base],
                          {h: b for h, (b, _) in hours.items()})
        for p in base:
            vf = pd.Timestamp(p["valid_for"])
            b, sb = hours[vf.hour]
            corr = b if b is not None else sb
            t_ifs = p.get("nwp_temperature")
            rows.append({
                "station_id": sid, "issue_at": issue_at.isoformat(), "lead_hours": int(p["lead_hours"]),
                "valid_for": vf.isoformat(), "nwp_run_at": run_at.isoformat() if run_at else None,
                "t_ifs": _r(t_ifs), "t_v1": _r(p.get("temperature")),
                "t_e": _r(t_ifs - corr) if t_ifs is not None and corr is not None else None,
                "t_m2": _r(t_m2.get(vf)), "bias": None if b is None else round(float(b), 3),
                "model_tag": m2.tag,
            })

    df_out = pd.DataFrame(rows)
    if df_out.empty:
        logger.error("Nessuna riga prodotta")
        sys.exit(1)
    vf = pd.to_datetime(df_out.valid_for, utc=True)
    diff = (df_out.t_m2 - df_out.t_ifs).groupby(vf.dt.hour).mean().round(1)
    print("\nt_m2 − t_ifs medio per ora UTC (atteso: segno opposto del bias, positivo la sera):")
    print(" ".join(f"{h:02d}:{v:+.1f}" for h, v in diff.items()))
    sample = df_out[df_out.lead_hours.isin([1, 24, 48])].pivot_table(
        index="station_id", columns="lead_hours", values=["t_ifs", "t_e", "t_v1", "t_m2"])
    if dry_run:
        print("\n" + sample.round(1).to_string())
    missing_m2 = df_out.t_m2.isna().sum()
    logger.info(f"Righe {len(df_out)} | t_m2 mancanti {missing_m2} | stazioni senza bias {no_bias or '-'} | "
                f"stazioni fallite {failed or '-'}")

    if json_out:
        Path(json_out).write_text(json.dumps(rows, indent=1))
    if dry_run:
        logger.info("--dry-run: nessuna scrittura")
        return
    n = db.upsert_forecasts_shadow(rows)
    logger.info(f"forecasts_shadow: {n} righe scritte")
    if failed:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Prova in ombra 1–48 h (IFS, E, modello attuale, M2)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-lead", type=int, default=48)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "inference", "features"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    run(list(range(1, args.max_lead + 1)), args.dry_run, args.json_out)


if __name__ == "__main__":
    main()
