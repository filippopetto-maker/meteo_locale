"""
eval_wind10_shadow.py — Valutazione della prova in ombra del vento a 10 m (M2 Wind10) contro i METAR

Legge forecasts_shadow_wind10 (solo aeroporti, emissioni 09Z e 21Z) e scarica da Iowa Mesonet i METAR
dello stesso periodo. Per ogni (aeroporto, valid_for) confronta velocità e direzione di IFS e M2 con il
METAR più vicino all'ora (±35 min, nodi × 1,852). Direzione valutata solo con vento osservato ≥ 3 km/h.
Riporta errore medio per fascia di scadenza, per aeroporto e il totale; atteso (meta.json): velocità
2,73 km/h e direzione 29,4° a siti già visti, IFS 3,80 km/h e 36,4°.

Utilizzo:
    python3 scripts/eval_wind10_shadow.py
    python3 scripts/eval_wind10_shadow.py --since 2026-10-23
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

IEM = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={icao}&data=drct&data=sknt"
       "&year1={y1}&month1={m1}&day1={d1}&year2={y2}&month2={m2}&day2={d2}&tz=Etc/UTC&format=onlycomma"
       "&missing=empty&report_type=3&report_type=4")
MIN_SPEED = 3.0


def ang_err(a, b):
    d = np.abs(np.asarray(a) - np.asarray(b)) % 360
    return np.minimum(d, 360 - d)


def shadow_rows(since: pd.Timestamp) -> pd.DataFrame:
    out, off = [], 0
    while True:
        r = (db.get_client().table("forecasts_shadow_wind10").select("*").eq("kind", "airport")
             .gte("issue_at", since.isoformat()).order("issue_at").range(off, off + 999).execute().data)
        out += r
        if len(r) < 1000:
            break
        off += 1000
    df = pd.DataFrame(out)
    if df.empty:
        return df
    for c in ("issue_at", "valid_for"):
        df[c] = pd.to_datetime(df[c], utc=True)
    return df[df.issue_at.dt.hour.isin([9, 21])]


def metar(icao: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    url = IEM.format(icao=icao, y1=start.year, m1=start.month, d1=start.day, y2=end.year, m2=end.month, d2=end.day)
    m = pd.read_csv(io.StringIO(requests.get(url, timeout=120).text))
    if m.empty:
        return pd.DataFrame(columns=["valid_for", "obs_w", "obs_d"])
    m["t"] = pd.to_datetime(m.valid, utc=True)
    m["h"] = m.t.dt.round("h")
    m = m[(m.t - m.h).abs() <= pd.Timedelta(minutes=35)].copy()
    m["gap"] = (m.t - m.h).abs()
    m = m.sort_values("gap").drop_duplicates("h")
    out = pd.DataFrame({"valid_for": m.h.to_numpy(), "obs_w": (m.sknt * 1.852).to_numpy(), "obs_d": m.drct.to_numpy()})
    out["valid_for"] = pd.to_datetime(out.valid_for, utc=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Valutazione vento a 10 m in ombra contro i METAR")
    ap.add_argument("--since", default="2026-10-22 09:00")
    args = ap.parse_args()
    since = pd.Timestamp(args.since, tz="UTC")
    sh = shadow_rows(since)
    if sh.empty:
        print("Nessuna emissione 09Z/21Z negli aeroporti dopo", since)
        return
    end = pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=1)
    parts = []
    for icao, g in sh.groupby("point_id"):
        m = metar(icao, g.valid_for.min().floor("D"), end)
        parts.append(g.merge(m, on="valid_for", how="inner").assign(airport=icao))
    d = pd.concat(parts, ignore_index=True)
    d["band"] = pd.cut(d.lead_hours, [0, 6, 12, 24, 36, 48], labels=["1-6", "7-12", "13-24", "25-36", "37-48"])
    d["e_w_ifs"], d["e_w_m2"] = (d.w_ifs - d.obs_w).abs(), (d.w_m2 - d.obs_w).abs()
    strong = d[(d.obs_w >= MIN_SPEED) & d.obs_d.notna()].copy()
    strong["e_d_ifs"], strong["e_d_m2"] = ang_err(strong.d_ifs, strong.obs_d), ang_err(strong.d_m2, strong.obs_d)
    print(f"Emissioni 09Z/21Z dal {sh.issue_at.min():%d/%m %H}Z al {sh.issue_at.max():%d/%m %H}Z; "
          f"{len(d):,} confronti di velocità, {len(strong):,} di direzione (vento METAR ≥ {MIN_SPEED:g} km/h)\n")
    tot = pd.DataFrame({"velocità IFS (km/h)": [d.e_w_ifs.mean()], "velocità M2": [d.e_w_m2.mean()],
                        "direzione IFS (°)": [strong.e_d_ifs.mean()], "direzione M2": [strong.e_d_m2.mean()],
                        "dir. entro 45° IFS %": [100 * (strong.e_d_ifs <= 45).mean()],
                        "dir. entro 45° M2 %": [100 * (strong.e_d_m2 <= 45).mean()]})
    print("TOTALE\n" + tot.round(2).to_string(index=False))
    band = d.groupby("band", observed=True).agg(n=("e_w_ifs", "size"), vel_ifs=("e_w_ifs", "mean"), vel_m2=("e_w_m2", "mean"))
    band = band.join(strong.groupby("band", observed=True).agg(dir_ifs=("e_d_ifs", "mean"), dir_m2=("e_d_m2", "mean")))
    print("\nPer fascia di scadenza (ore)\n" + band.round(2).to_string())
    ap_ = d.groupby("airport").agg(n=("e_w_ifs", "size"), vel_ifs=("e_w_ifs", "mean"), vel_m2=("e_w_m2", "mean"))
    ap_ = ap_.join(strong.groupby("airport").agg(dir_ifs=("e_d_ifs", "mean"), dir_m2=("e_d_m2", "mean")))
    print("\nPer aeroporto\n" + ap_.round(2).to_string())


if __name__ == "__main__":
    main()
