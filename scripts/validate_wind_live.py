"""
validate_wind_live.py — Il target vento ricostruito (moduli anemometro Netatmo con QC) coincide
con il vento che la produzione registra dal vivo?

Il vento live (observations.wind_speed) è la mediana dei moduli anemometro che getpublicdata
restituisce entro il raggio di 5 km; il target storico (netatmo_wind_backfill.py + QC) usa un
sottoinsieme fisso di moduli. Dove i due non coincidono, il bias di M2 vento calcolato dal vivo
non è confrontabile con quello di training e il confronto in ombra non è affidabile.

Per stazione: ore in comune (giu → oggi), medie, bias e MAE del target rispetto al vivo,
correlazione. Una stazione è "coerente" se ha almeno 1000 ore in comune, correlazione ≥ 0,55
e |bias| ≤ 2,2 km/h. Output: logs/netatmo_backfill/wind_live_agreement.csv.

Utilizzo:
    python3 scripts/validate_wind_live.py
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import experiment_m2_wind as w  # noqa: E402
import netatmo_wind_backfill as nw  # noqa: E402

MIN_HOURS, MIN_CORR, MAX_BIAS = 1000, 0.55, 2.2
OUT = nw.nb.WORK_DIR / "wind_live_agreement.csv"


def live_hourly(station_id: int, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    out, off = [], 0
    while True:
        r = (db.get_client().table("observations").select("recorded_at, wind_speed, raw_source")
             .eq("station_id", int(station_id)).lt("qc_flag", 2)
             .gte("recorded_at", start.isoformat()).lt("recorded_at", end.isoformat())
             .order("recorded_at").range(off, off + 999).execute().data)
        out += r
        if len(r) < 1000:
            break
        off += 1000
    df = pd.DataFrame(out)
    if df.empty:
        return pd.Series(dtype=float)
    df = df[df.raw_source.apply(lambda x: isinstance(x, dict) and x.get("source") == "netatmo_public")
            & df.wind_speed.notna()]
    t = pd.to_datetime(df.recorded_at, utc=True, format="ISO8601")
    near = (t - t.dt.round("h")).abs() <= pd.Timedelta(minutes=15)
    return pd.Series(df.wind_speed[near].astype(float).to_numpy(), index=t[near].dt.round("h")).groupby(level=0).mean()


def main() -> None:
    warnings.filterwarnings("ignore")
    tgt = w.wind_target(pd.read_csv(nw.nb.WORK_DIR / "wind_qc.csv"))
    names = {s["id"]: s["name"] for s in db.get_active_stations()}
    start, end = pd.Timestamp("2026-06-10", tz="UTC"), pd.Timestamp.now(tz="UTC").floor("D")
    rows = []
    for sid in sorted(tgt.station_id.unique()):
        both = pd.concat([live_hourly(sid, start, end), tgt[tgt.station_id == sid].set_index("recorded_at").ws],
                         axis=1, keys=["live", "bf"]).dropna()
        r = {"station_id": int(sid), "station": names[sid], "n": len(both)}
        if len(both) >= 50:
            d = both.bf - both.live
            r.update({"live_mean": both.live.mean(), "bf_mean": both.bf.mean(), "bias": d.mean(),
                      "mae": d.abs().mean(), "corr": both.live.corr(both.bf)})
        r["consistent"] = bool(r["n"] >= MIN_HOURS and r.get("corr", 0) >= MIN_CORR
                               and abs(r.get("bias", 99)) <= MAX_BIAS)
        rows.append(r)
    res = pd.DataFrame(rows)
    res.to_csv(OUT, index=False)
    pd.set_option("display.width", 200)
    print(res.round(2).to_string(index=False))
    print(f"\nStazioni coerenti: {int(res.consistent.sum())}/{len(res)} → {OUT}")


if __name__ == "__main__":
    main()
