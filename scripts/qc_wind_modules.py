"""
qc_wind_modules.py — Controllo qualità dei moduli anemometro Netatmo scaricati

Per ogni modulo scelto da netatmo_wind_backfill.py select (wind_raw/*.parquet) confronta la
serie oraria di velocità con il vento a 10 m dell'IFS alla stazione di riferimento e segnala
moduli non utilizzabili come target:

  stuck     deviazione standard < 0,5 km/h o oltre il 60 % di valori nulli
  sparse    meno di 2000 ore con dato
  decorr    correlazione con l'IFS < 0,15 (sensore che non segue il tempo: ostruito o guasto)

Output: logs/netatmo_backfill/wind_qc.csv (una riga per modulo e stazione) e riepilogo per
stazione con i moduli che restano.

Utilizzo:
    python3 scripts/qc_wind_modules.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import backtest_ifs_lead as bt  # noqa: E402
import db  # noqa: E402
import netatmo_wind_backfill as nw  # noqa: E402

logger = logging.getLogger("qc_wind")
MIN_HOURS, MIN_STD, MAX_ZERO, MIN_CORR = 2000, 0.5, 0.6, 0.15


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    sel = json.loads(nw.SELECTED_FILE.read_text())
    stations = {s["id"]: s for s in db.get_active_stations()}
    api = bt.OpenMeteo(ROOT / "logs" / "backtest" / "cache", 30.0)
    start = pd.Timestamp("2024-03-14", tz="UTC").to_pydatetime() - timedelta(days=2)
    end = pd.Timestamp("2026-10-22", tz="UTC").to_pydatetime()

    rows = []
    for sid, st in sel.items():
        if not st["modules"]:
            continue
        nwp = bt.fetch_historical(api, [stations[int(sid)]], start, end)[int(sid)]
        ifs = pd.Series(nwp.wind_speed.to_numpy(float),
                        index=pd.to_datetime(nwp.recorded_at).dt.tz_localize("UTC"))
        for m in st["modules"]:
            path = nw.RAW_DIR / f"{m['module_id'].replace(':', '')}.parquet"
            r = {"station_id": int(sid), "station": st["name"], "module_id": m["module_id"],
                 "dist_km": m["dist_km"], "alt": m.get("alt")}
            if not path.exists():
                rows.append({**r, "flag": "missing"})
                continue
            raw = pd.read_parquet(path).dropna(subset=["ws"])
            raw = raw[(raw.ws >= 0) & (raw.ws < 200)]
            t = pd.to_datetime(raw.ts, unit="s", utc=True)
            ok = (t - t.dt.round("h")).abs() <= pd.Timedelta(minutes=5)
            ws = pd.Series(raw.ws.to_numpy(float)[ok.to_numpy()], index=t[ok].dt.round("h")).groupby(level=0).mean()
            both = pd.concat([ws, ifs], axis=1, keys=["obs", "ifs"]).dropna()
            r.update({
                "hours": len(ws), "first": ws.index.min() if len(ws) else None,
                "mean": ws.mean() if len(ws) else np.nan, "std": ws.std() if len(ws) else np.nan,
                "p95": ws.quantile(0.95) if len(ws) else np.nan, "zero_share": float((ws == 0).mean()) if len(ws) else np.nan,
                "ifs_mean": both.ifs.mean() if len(both) else np.nan,
                "ratio": both.obs.mean() / both.ifs.mean() if len(both) and both.ifs.mean() > 0 else np.nan,
                "corr": both.obs.corr(both.ifs) if len(both) > 100 else np.nan,
                "corr_log": np.log1p(both.obs).corr(np.log1p(both.ifs)) if len(both) > 100 else np.nan,
            })
            flags = []
            if r["hours"] < MIN_HOURS:
                flags.append("sparse")
            if not (r["std"] >= MIN_STD) or not (r["zero_share"] <= MAX_ZERO):
                flags.append("stuck")
            if not (r["corr"] >= MIN_CORR):
                flags.append("decorr")
            r["flag"] = ",".join(flags) or "ok"
            rows.append(r)
        logger.info(f"st.{sid:>2} {st['name']:<26} {len(st['modules'])} moduli")

    df = pd.DataFrame(rows)
    out = nw.nb.WORK_DIR / "wind_qc.csv"
    df.to_csv(out, index=False)
    pd.set_option("display.width", 220)
    print("\nModuli per esito:\n" + df.flag.value_counts().to_string())
    summ = df.groupby(["station_id", "station"]).apply(
        lambda g: pd.Series({"moduli": len(g), "ok": int((g.flag == "ok").sum()),
                             "ore_mediane_ok": g.loc[g.flag == "ok", "hours"].median(),
                             "rapporto_med": g.loc[g.flag == "ok", "ratio"].median(),
                             "corr_med": g.loc[g.flag == "ok", "corr"].median()}), include_groups=False)
    print("\nPer stazione (solo moduli ok):\n" + summ.round(2).to_string())
    logger.info(f"scritto {out}")


if __name__ == "__main__":
    main()
