"""
shadow_m2b.py — Prova in ombra di M2b (temperatura) 1–48 h

Una emissione per esecuzione (issue_at = ora UTC corrente troncata all'ora), stesso input IFS
(Forecast API, past_days=2, forecast_days=4) di shadow_m2.py. Scrive forecasts_shadow_m2b:

  t_ifs   IFS a valid_for
  t_m2b   M2b grezzo (model/m2b/), con le feature di bias_features
  t_m2c   t_m2b − λ · errore medio di M2b su sé stesso (stazione × ora UTC, giorni precedenti, ≥ 7 giorni)
  self_bias  la correzione usata (NULL se M2b non ha ancora abbastanza storia: t_m2c = t_m2b)

La storia per la correzione nasce dalle stesse emissioni di M2b, quindi i primi 7 giorni t_m2c = t_m2b.
Confronto con M2, E e IFS: join su (stazione, valid_for, lead) con forecasts_shadow.

Non scrive forecasts, forecasts_shadow né bias_table e non chiama Netatmo.

Trigger: dopo shadow_m2.py in shadow-m2.yml (09:15 e 21:15 UTC). bias_features: update_bias_features.py.

Utilizzo:
    python3 model/shadow_m2b.py --dry-run
    python3 model/shadow_m2b.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (_PROJECT_ROOT, _PROJECT_ROOT / "model"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import db  # noqa: E402
import inference as inf  # noqa: E402
from m2b import BIAS_FEATURES, M2b  # noqa: E402
from shadow_m2 import MAX_RUN_AGE_H, NWP_MODEL, _r, ifs_run_time  # noqa: E402

logger = logging.getLogger("shadow_m2b")


def load_features() -> tuple[dict[tuple[int, int], dict[str, float | None]], pd.Timestamp | None]:
    rows = db.get_bias_features()
    table = {(r["station_id"], r["hour_utc"]): {k: r.get(k) for k in BIAS_FEATURES} for r in rows}
    window_end = pd.Timestamp(max(r["window_end"] for r in rows)) if rows else None
    return table, window_end


def load_self_bias(issue_at: datetime, days: int, min_days: int) -> dict[tuple[int, int], float]:
    """(stazione, ora UTC) → errore medio di M2b sui valid_for dei giorni precedenti a oggi (UTC)."""
    day = issue_at.date()
    rows = db.get_m2b_self_bias(day, days)
    return {(r["station_id"], r["hour_utc"]): r["bias"] for r in rows if r["n_days"] >= min_days and r["bias"] is not None}


def run(leads: list[int], dry_run: bool) -> None:
    m2b = M2b()
    issue_at = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    run_at = ifs_run_time()
    if run_at is not None and issue_at - run_at > timedelta(hours=MAX_RUN_AGE_H):
        logger.warning(f"Run IFS vecchio: {run_at:%Y-%m-%d %HZ}")
    features, window_end = load_features()
    if window_end is None:
        logger.error("bias_features vuota: eseguire scripts/update_bias_features.py")
        sys.exit(1)
    age = (pd.Timestamp(issue_at) - window_end).days
    if age > 2:
        logger.warning(f"bias_features vecchia di {age} giorni (window_end {window_end:%Y-%m-%d})")
    sc = m2b.meta.get("self_correction", {})
    self_bias = load_self_bias(issue_at, int(sc.get("days", 30)), m2b.self_min_days)
    excluded = set(m2b.meta["excluded_stations"])
    stations = sorted((s for s in db.get_active_stations() if s["id"] not in excluded), key=lambda s: s["id"])
    logger.info(f"Emissione {issue_at:%Y-%m-%d %H:%M}Z, {len(stations)} stazioni, lead {leads[0]}–{leads[-1]}, "
                f"modello {m2b.tag}, bias_features window_end {window_end:%Y-%m-%d}, celle con errore proprio {len(self_bias)}")

    rows, failed = [], []
    valid = [pd.Timestamp(issue_at + timedelta(hours=L)) for L in leads]
    for st in stations:
        sid = st["id"]
        try:
            df = inf.fetch_forecast(st["lat"], st["lon"], past_days=2, forecast_days=4, model=NWP_MODEL)
        except Exception as exc:  # una stazione in errore non deve fermare le altre
            logger.error(f"st.{sid}: fetch IFS fallito ({exc})")
            failed.append(sid)
            continue
        by_hour = {h: features.get((sid, h), {}) for h in range(24)}
        t_m2b = m2b.predict(df, st, valid, by_hour, window_end)
        nwp = df.drop_duplicates("recorded_at", keep="last").set_index("recorded_at").temperature
        for L, vf in zip(leads, valid):
            raw = t_m2b.get(vf)
            sb = self_bias.get((sid, vf.hour))
            t_ifs = nwp.get(vf.tz_localize(None))
            rows.append({
                "station_id": sid, "issue_at": issue_at.isoformat(), "lead_hours": L, "valid_for": vf.isoformat(),
                "nwp_run_at": run_at.isoformat() if run_at else None, "t_ifs": _r(t_ifs), "t_m2b": _r(raw),
                "t_m2c": _r(m2b.correct(raw, sb)) if raw is not None else None,
                "self_bias": None if sb is None else round(float(sb), 3), "model_tag": m2b.tag,
            })

    out = pd.DataFrame(rows)
    if out.empty:
        logger.error("Nessuna riga prodotta")
        sys.exit(1)
    diff = (out.t_m2b - out.t_ifs).groupby(pd.to_datetime(out.valid_for, utc=True).dt.hour).mean().round(1)
    print("\nt_m2b − t_ifs medio per ora UTC (atteso: positivo la sera):")
    print(" ".join(f"{h:02d}:{v:+.1f}" for h, v in diff.items()))
    logger.info(f"Righe {len(out)} | t_m2b mancanti {out.t_m2b.isna().sum()} | con correzione propria {out.self_bias.notna().sum()} | "
                f"stazioni fallite {failed or '-'}")
    if dry_run:
        print(out[out.lead_hours.isin([1, 24, 48])].pivot_table(index="station_id", columns="lead_hours",
                                                                 values=["t_ifs", "t_m2b", "t_m2c"]).round(1).head(10).to_string())
        logger.info("--dry-run: nessuna scrittura")
        return
    n = db.upsert_forecasts_shadow_m2b(rows)
    logger.info(f"forecasts_shadow_m2b: {n} righe scritte")
    if failed:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Prova in ombra M2b (temperatura) 1–48 h")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-lead", type=int, default=48)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "inference", "features"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    run(list(range(1, args.max_lead + 1)), args.dry_run)


if __name__ == "__main__":
    main()
