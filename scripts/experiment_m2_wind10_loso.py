"""
experiment_m2_wind10_loso.py — M2 vento a 10 m (verità METAR) trasferibile a punti senza METAR?

Il vento da mostrare sulla mappa è quello standard a 10 m (verità: METAR di 8 aeroporti del Lazio).
Le 31 stazioni del progetto non hanno METAR, quindi il modello non può usare né l'identità della
stazione (station_cat) né il bias recente: solo IFS, feature di produzione e descrittori statici del
punto (quota, distanza dal mare, ecc.). Qui si misura se questo basta: leave-one-airport-out con
split temporale. Per ogni aeroporto, addestramento sugli altri 7 con valid_for < --cut e test
sull'aeroporto escluso da --cut. Confronto con IFS grezzo. Iterazioni fisse (--n-speed, --n-dir).

Utilizzo:
    python3 scripts/experiment_m2_wind10_loso.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "model"))

import backtest_ifs_lead as bt  # noqa: E402
import experiment_m2_wind as wx  # noqa: E402
import experiment_m2_wind_metar as wm  # noqa: E402
import features as ft  # noqa: E402
import inference as inf  # noqa: E402
from forecast import DEFAULT_LGB_PARAMS  # noqa: E402

logger = logging.getLogger("m2_wind10_loso")


def airport_frames(feature_cols: list[str], since: str, data_end: str, pause: float) -> tuple[pd.DataFrame, dict]:
    api = bt.OpenMeteo(ROOT / "logs" / "backtest" / "cache", pause)
    start = pd.Timestamp(since, tz="UTC").to_pydatetime() - timedelta(days=2)
    end = pd.Timestamp(data_end, tz="UTC").to_pydatetime()
    frames, statics = [], {}
    for icao, sid in wm.AIRPORTS.items():
        metar, lat, lon = wm.load_metar(icao)
        st = {"id": sid, "lat": lat, "lon": lon, **ft.compute_static_orography(lat, lon)}
        st["microclima"] = "costiera" if st["dist_sea_km"] < 8 else "standard"
        statics[icao] = st
        nwp = bt.fetch_historical(api, [st], start, end)[sid]
        f = wx.station_frame(st, nwp, metar.assign(station_id=sid)[["recorded_at", "ws", "station_id"]], feature_cols)
        f = f.merge(metar.rename(columns={"recorded_at": "valid_for"})[["valid_for", "obs_dir"]], on="valid_for", how="left")
        w = nwp[["recorded_at", "wind_direction"]].copy()
        w["recorded_at"] = pd.to_datetime(w.recorded_at).dt.tz_localize("UTC")
        f = f.merge(w.rename(columns={"recorded_at": "valid_for", "wind_direction": "nwp_wd_valid"}), on="valid_for", how="left")
        frames.append(f)
    data = pd.concat(frames, ignore_index=True)
    data = data[data.valid_for >= pd.Timestamp(since, tz="UTC")].dropna(subset=["y_ws", "nwp_ws_valid"]).reset_index(drop=True)
    data["base"] = np.log1p(data.nwp_ws_valid)
    data["resid"] = np.log1p(data.y_ws) - data.base
    return data, statics


def main() -> None:
    ap = argparse.ArgumentParser(description="M2 vento 10 m, leave-one-airport-out")
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--data-end", default="2026-10-22")
    ap.add_argument("--cut", default="2026-05-01")
    ap.add_argument("--n-speed", type=int, default=300)
    ap.add_argument("--n-dir", type=int, default=250)
    ap.add_argument("--pause", type=float, default=30.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    feature_cols = inf.load_lgbm("temperature").feature_name()
    data, _ = airport_frames(feature_cols, args.since, args.data_end, args.pause)
    cols = [*feature_cols, "nwp_ws_valid", "nwp_wd_sin", "nwp_wd_cos"]   # niente station_cat né bias
    cut = pd.Timestamp(args.cut, tz="UTC")
    params = {**DEFAULT_LGB_PARAMS, "seed": 42, "verbose": -1}
    inv = {v: k for k, v in wm.AIRPORTS.items()}
    rows, all_s, all_d = [], [], []
    for sid in wm.AIRPORTS.values():
        tr = data[(data.station_cat != sid) & (data.valid_for < cut)]
        te = data[(data.station_cat == sid) & (data.valid_for >= cut)].reset_index(drop=True)
        bs = lgb.train(params, lgb.Dataset(tr[cols], label=tr.resid), num_boost_round=args.n_speed)
        pred_s = np.expm1(te.base.to_numpy() + bs.predict(te[cols]))
        dtr = tr.dropna(subset=["obs_dir", "nwp_wd_valid"])
        dte = te.dropna(subset=["obs_dir", "nwp_wd_valid"]).reset_index(drop=True)
        pr = {}
        for lab, fn in (("y_sin", np.sin), ("y_cos", np.cos)):
            b = lgb.train(params, lgb.Dataset(dtr[cols], label=fn(np.deg2rad(dtr.obs_dir)),
                                              weight=np.clip(dtr.y_ws.to_numpy(), 0.5, 15.0)), num_boost_round=args.n_dir)
            pr[lab] = b.predict(dte[cols])
        m2_dir = np.rad2deg(np.arctan2(pr["y_sin"], pr["y_cos"])) % 360
        e_ifs, e_m2 = wm.ang_err(dte.nwp_wd_valid, dte.obs_dir), wm.ang_err(m2_dir, dte.obs_dir)
        rows.append({"aeroporto": f"{inv[sid]} {wm.NAMES[inv[sid]]}", "n_vel": len(te),
                     "vel_ifs": np.abs(te.nwp_ws_valid - te.y_ws).mean(), "vel_M2": np.abs(pred_s - te.y_ws).mean(),
                     "n_dir": len(dte), "dir_ifs": e_ifs.mean(), "dir_M2": e_m2.mean()})
        all_s.append(pd.DataFrame({"ifs": np.abs(te.nwp_ws_valid - te.y_ws), "m2": np.abs(pred_s - te.y_ws)}))
        all_d.append(pd.DataFrame({"ifs": e_ifs, "m2": e_m2}))
        logger.info(f"{inv[sid]} fatto")
    res = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(f"\nLeave-one-airport-out, test da {cut:%d/%m/%Y}; senza station_cat né bias (come sarebbe sulle 31 stazioni)\n")
    print(res.round(1).to_string(index=False))
    s, d = pd.concat(all_s), pd.concat(all_d)
    print(f"\nTotale velocità: IFS {s.ifs.mean():.2f} km/h → M2 {s.m2.mean():.2f} km/h (n = {len(s):,})")
    print(f"Totale direzione: IFS {d.ifs.mean():.1f}° → M2 {d.m2.mean():.1f}° (entro 45°: {100 * (d.ifs <= 45).mean():.0f}% → {100 * (d.m2 <= 45).mean():.0f}%, n = {len(d):,})")


if __name__ == "__main__":
    main()
