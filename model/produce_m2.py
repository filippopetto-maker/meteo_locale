"""
produce_m2.py — M2 in produzione: previsioni 1–48 h per la mappa (scheletro dati)

Una emissione per esecuzione (issue_at = ora UTC corrente troncata all'ora), stesso
input ECMWF IFS della produzione e della prova in ombra. Scrive:

  forecasts_48h   ultima emissione 1–48 h, una riga per (stazione, valid_for), sovrascritta
                  a ogni esecuzione (non cresce). È ciò che legge scripts/export_forecast_48h.py.
  forecasts       solo il lead 1, solo con --write-lead1 (default: spento). È la riga che la
                  mappa legge oggi: attivarlo equivale a sostituire inference.py come
                  scrittore del lead 1.

Per ogni variabile si registra da dove viene il valore (colonne *_source):
  temperatura  m2     M2 (model/m2/), ove la stazione ha il modello
               e      IFS − bias da bias_table (ripiego se M2 non dà valore)
               v1     modello attuale (stazioni escluse da M2, es. Sigillo)
  umidità      m2h    M2 umidità H1 (model/m2_hum/), ove la stazione ha il modello
               ifs    IFS a valid_for, come oggi in produzione (ripiego)
  vento        m2w10  M2 vento a 10 m (model/m2_wind10/, verità METAR): velocità e direzione
               ifs    IFS a valid_for (ripiego, e per le stazioni escluse da M2)

Non tocca inference.py, la mappa, la prova in ombra né forecasts_shadow.
Nessun trigger programmato: solo workflow_dispatch (.github/workflows/produce-m2.yml).

Utilizzo:
    python3 model/produce_m2.py --dry-run
    python3 model/produce_m2.py --dry-run --json-out /tmp/f48.json
    python3 model/produce_m2.py                 # scrive forecasts_48h
    python3 model/produce_m2.py --write-lead1   # in più il lead 1 in forecasts
"""

from __future__ import annotations

import argparse
import json
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
from m2 import M2, M2Humidity, M2Wind10  # noqa: E402
from shadow_m2 import MAX_RUN_AGE_H, NWP_MODEL, _r, ifs_run_time, load_bias, prefetch  # noqa: E402

logger = logging.getLogger("produce_m2")


def station_rows(st: dict, df: pd.DataFrame | None, m2: M2, m2h: M2Humidity, m10: M2Wind10, issue_at: datetime,
                 run_at: datetime | None, leads: list[int], bias: dict, in_m2: bool) -> list[dict]:
    sid = st["id"]
    if df is None:
        df = inf.fetch_forecast(st["lat"], st["lon"], past_days=2, forecast_days=4, model=NWP_MODEL)
    base = inf.predict_from_df(df.copy(), st, leads, now_utc=issue_at)
    hours = {h: bias.get((sid, h), (None, None, None, None)) for h in range(24)}
    valid = [pd.Timestamp(p["valid_for"]) for p in base]
    t_m2 = m2.predict(df, st, valid, {h: v[0] for h, v in hours.items()}) if in_m2 else {}
    h_m2 = m2h.predict(df, st, valid, {h: v[2] for h, v in hours.items()}) if in_m2 else {}
    wind = {}
    if in_m2:
        w = m10.predict(df, st, valid)
        wind = {pd.Timestamp(r.valid_for): (r.w_m2, r.d_m2) for r in w.itertuples()}

    rows = []
    for p in base:
        vf = pd.Timestamp(p["valid_for"])
        b, sb = hours[vf.hour][:2]
        corr = b if b is not None else sb
        t_ifs = p.get("nwp_temperature")
        if in_m2 and t_m2.get(vf) is not None:
            temp, src = t_m2[vf], "m2"
        elif in_m2 and t_ifs is not None and corr is not None:
            temp, src = t_ifs - corr, "e"
        else:
            temp, src = p.get("temperature"), "v1"
        if in_m2 and h_m2.get(vf) is not None:
            hum, hsrc = h_m2[vf], "m2h"
        else:
            hum, hsrc = p.get("humidity"), "ifs"
        if vf in wind:
            ws, wd, wsrc = _r(wind[vf][0]), _r(wind[vf][1]), "m2w10"
        else:
            ws, wd, wsrc = p.get("wind_speed"), p.get("wind_direction"), "ifs"
        rows.append({
            "station_id": sid, "valid_for": vf.isoformat(), "lead_hours": int(p["lead_hours"]),
            "issue_at": issue_at.isoformat(), "nwp_run_at": run_at.isoformat() if run_at else None,
            "temperature": _r(temp), "humidity": _r(hum),
            "wind_speed": ws, "wind_direction": wd,
            "temp_source": src, "hum_source": hsrc, "wind_source": wsrc,
            "model_tag": f"{m2.tag}+{m2h.tag}+{m10.tag}", "_nwp_t": _r(t_ifs), "_nwp_h": p.get("nwp_humidity"),
            "_corrected": bool(p.get("corrected", False)),
        })
    return rows


def run(leads: list[int], dry_run: bool, write_lead1: bool, json_out: str | None) -> None:
    m2 = M2()
    m2h = M2Humidity()
    m10 = M2Wind10()
    issue_at = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    run_at = ifs_run_time()
    if run_at is not None and issue_at - run_at > timedelta(hours=MAX_RUN_AGE_H):
        logger.warning(f"Run IFS vecchio: {run_at:%Y-%m-%d %HZ} ({(issue_at - run_at).total_seconds() / 3600:.0f} h)")
    excluded = set(m2.meta["excluded_stations"])
    stations = sorted(db.get_active_stations(), key=lambda s: s["id"])
    bias = load_bias()
    logger.info(f"Emissione {issue_at:%Y-%m-%d %H:%M}Z, {len(stations)} stazioni "
                f"({len(excluded & {s['id'] for s in stations})} fuori da M2), lead {leads[0]}–{leads[-1]}, "
                f"modelli {m2.tag} + {m2h.tag} + {m10.tag}, bias_table {len(bias)} celle")

    rows, failed = [], []
    pre = prefetch(stations)
    for st in stations:
        try:
            rows += station_rows(st, pre.get(st["id"]), m2, m2h, m10, issue_at, run_at, leads, bias,
                                 st["id"] not in excluded)
        except Exception as exc:  # una stazione in errore non deve fermare le altre
            logger.error(f"st.{st['id']}: {exc}")
            failed.append(st["id"])
    if not rows:
        logger.error("Nessuna riga prodotta")
        sys.exit(1)

    out = pd.DataFrame(rows)
    src = out.groupby("temp_source").size().to_dict()
    hsrc = out.groupby("hum_source").size().to_dict()
    wsrc = out.groupby("wind_source").size().to_dict()
    logger.info(f"Righe {len(out)} | fonte temperatura {src} | fonte umidità {hsrc} | fonte vento {wsrc} | "
                f"stazioni fallite {failed or '-'}")
    n_missing = out[["temperature", "humidity", "wind_speed"]].isna().sum().to_dict()
    logger.info(f"Valori mancanti {n_missing}")
    if dry_run:
        pivot = out[out.lead_hours.isin([1, 24, 48])].pivot_table(
            index="station_id", columns="lead_hours", values=["temperature", "humidity", "wind_speed"])
        print(pivot.round(1).to_string())

    table_rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    if json_out:
        Path(json_out).write_text(json.dumps(table_rows, indent=1))
    if dry_run:
        logger.info("--dry-run: nessuna scrittura")
        return

    db.upsert_forecasts_48h(table_rows)
    db.delete_forecasts_48h_before(issue_at)
    logger.info(f"forecasts_48h: {len(table_rows)} righe scritte, ore passate eliminate")
    if write_lead1:
        n = 0
        for r in rows:
            if r["lead_hours"] != 1:
                continue
            db.insert_forecast(
                station_id=r["station_id"], forecast_at=issue_at, valid_for=r["valid_for"],
                temperature=r["temperature"], wind_speed=r["wind_speed"], wind_direction=r["wind_direction"],
                humidity=r["humidity"], model_version=r["model_tag"], corrected=r["_corrected"], lead_hours=1,
                nwp_temperature=r["_nwp_t"], nwp_humidity=r["_nwp_h"], nwp_run_at=run_at)
            n += 1
        logger.info(f"forecasts: {n} righe lead 1 scritte (--write-lead1)")
    if failed:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="M2 in produzione: 1–48 h → forecasts_48h")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--write-lead1", action="store_true",
                    help="Scrive anche il lead 1 in forecasts (sostituisce inference.py come scrittore)")
    ap.add_argument("--max-lead", type=int, default=48)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "inference", "features"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    run(list(range(1, args.max_lead + 1)), args.dry_run, args.write_lead1, args.json_out)


if __name__ == "__main__":
    main()
