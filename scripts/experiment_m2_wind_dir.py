"""
experiment_m2_wind_dir.py — La direzione del vento si può prevedere con M2?

Dati: la direzione oraria (WindAngle) dei moduli anemometro Netatmo con QC, già nei parquet
di netatmo_wind_backfill.py. Per ogni stazione e ora: media vettoriale dei moduli (pesata con la
velocità) → angolo osservato.

Modello: l'angolo è circolare, quindi si prevedono seno e coseno della direzione osservata con
due LightGBM (stesse feature di M2 vento + direzione e velocità IFS + bias di direzione recente:
media dei 30 giorni precedenti di sin/cos della differenza IFS − osservato per stazione × ora UTC)
e si ricostruisce l'angolo con atan2.

Metrica: errore angolare assoluto medio (gradi) sulle ore con velocità osservata ≥ 3 km/h (sotto,
la direzione è rumore), per IFS grezzo, E (IFS ruotato del bias circolare per stazione × ora) e M2.
Split: addestramento fino a --cut (default 01/09/2026), test da --cut. Un errore casuale vale 90°.

Utilizzo:
    python3 scripts/experiment_m2_wind_dir.py
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
import db  # noqa: E402
import experiment_m2_humidity as hx  # noqa: E402
import experiment_m2_wind as wx  # noqa: E402
import experiment_retrain_netatmo as ex  # noqa: E402
import inference as inf  # noqa: E402
import netatmo_wind_backfill as nw  # noqa: E402
from forecast import DEFAULT_LGB_PARAMS  # noqa: E402

logger = logging.getLogger("m2_wind_dir")
MIN_SPEED = 3.0


def dir_target(qc: pd.DataFrame) -> pd.DataFrame:
    """Direzione oraria per stazione: media vettoriale (pesata con la velocità) dei moduli ok."""
    parts = []
    for sid, g in qc[qc.flag == "ok"].groupby("station_id"):
        us, vs = [], []
        for mid in g.module_id:
            raw = pd.read_parquet(nw.RAW_DIR / f"{mid.replace(':', '')}.parquet").dropna(subset=["ws", "wa"])
            raw = raw[(raw.ws >= 0) & (raw.ws < 200)]
            t = pd.to_datetime(raw.ts, unit="s", utc=True)
            ok = (t - t.dt.round("h")).abs() <= pd.Timedelta(minutes=5)
            raw, idx = raw[ok.to_numpy()], t[ok].dt.round("h")
            rad = np.deg2rad(raw.wa.astype(float).to_numpy())
            us.append(pd.Series(raw.ws.to_numpy() * np.sin(rad), index=idx).groupby(level=0).mean())
            vs.append(pd.Series(raw.ws.to_numpy() * np.cos(rad), index=idx).groupby(level=0).mean())
        u, v = pd.concat(us, axis=1).mean(axis=1), pd.concat(vs, axis=1).mean(axis=1)
        parts.append(pd.DataFrame({"recorded_at": u.index, "obs_u": u.to_numpy(), "obs_v": v.to_numpy(),
                                   "station_id": int(sid)}))
    out = pd.concat(parts, ignore_index=True)
    out["obs_speed_vec"] = np.hypot(out.obs_u, out.obs_v)
    out["obs_dir"] = np.rad2deg(np.arctan2(out.obs_u, out.obs_v)) % 360
    return out


def ang_err(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    d = np.abs(a - b) % 360
    return np.minimum(d, 360 - d)


def main() -> None:
    ap = argparse.ArgumentParser(description="M2 direzione del vento")
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--data-end", default="2026-10-22")
    ap.add_argument("--cut", default="2026-09-01")
    ap.add_argument("--pause", type=float, default=30.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    qc = pd.read_csv(nw.nb.WORK_DIR / "wind_qc.csv")
    speed_t, dir_t = wx.wind_target(qc), dir_target(qc)
    stations = sorted(db.get_active_stations(), key=lambda s: s["id"])
    stations = [s for s in stations if s["id"] in set(speed_t.station_id) and s["id"] not in ex.EXCLUDED_STATIONS]
    feature_cols = inf.load_lgbm("temperature").feature_name()
    api = bt.OpenMeteo(ROOT / "logs" / "backtest" / "cache", args.pause)
    start = pd.Timestamp(args.since, tz="UTC").to_pydatetime() - timedelta(days=2)
    end = pd.Timestamp(args.data_end, tz="UTC").to_pydatetime()

    frames = []
    for st in stations:
        nwp = bt.fetch_historical(api, [st], start, end)[st["id"]]
        f = wx.station_frame(st, nwp, speed_t, feature_cols)
        d = dir_t[dir_t.station_id == st["id"]].rename(columns={"recorded_at": "valid_for"})
        f = f.merge(d[["valid_for", "obs_dir", "obs_speed_vec"]], on="valid_for", how="left")
        w = nwp[["recorded_at", "wind_direction"]].copy()
        w["recorded_at"] = pd.to_datetime(w.recorded_at).dt.tz_localize("UTC")
        f = f.merge(w.rename(columns={"recorded_at": "valid_for", "wind_direction": "nwp_wd_valid"}), on="valid_for", how="left")
        both = w.merge(dir_t[dir_t.station_id == st["id"]].rename(columns={"recorded_at": "recorded_at"})
                       [["recorded_at", "obs_dir"]], on="recorded_at").dropna()
        delta = np.deg2rad(both.wind_direction - both.obs_dir)
        for name, comp in (("bias_dsin_hour", np.sin(delta)), ("bias_dcos_hour", np.cos(delta))):
            f[name] = ex.lookup_bias(ex.hour_bias_table(both.recorded_at, pd.Series(comp.to_numpy(), index=both.index)),
                                     f.valid_for)
        frames.append(f)
    data = pd.concat(frames, ignore_index=True)
    data = data[data.valid_for >= pd.Timestamp(args.since, tz="UTC")]
    data = data.dropna(subset=["obs_dir", "nwp_wd_valid", "nwp_ws_valid", "y_ws"]).reset_index(drop=True)
    data["y_sin"], data["y_cos"] = np.sin(np.deg2rad(data.obs_dir)), np.cos(np.deg2rad(data.obs_dir))
    cols = [*feature_cols, "station_cat", "nwp_ws_valid", "nwp_wd_sin", "nwp_wd_cos", "bias_dsin_hour", "bias_dcos_hour"]
    cut = pd.Timestamp(args.cut, tz="UTC")
    train, test = data[data.valid_for < cut], data[data.valid_for >= cut].reset_index(drop=True)
    logger.info(f"Train {len(train):,} fino al {cut:%d/%m/%Y}; test {len(test):,}, {data.station_cat.nunique()} stazioni")

    # Il peso della riga è la velocità osservata (direzione di vento debole = rumore).
    w_train = np.clip(train.y_ws.to_numpy(), 0.5, 15.0)
    preds = {}
    for lab in ("y_sin", "y_cos"):
        tr_, va_ = train[train.valid_for <= train.valid_for.max() - timedelta(days=56)], train[train.valid_for > train.valid_for.max() - timedelta(days=56)]
        params = {**DEFAULT_LGB_PARAMS, "seed": 42}
        b = lgb.train(params, lgb.Dataset(tr_[cols], label=tr_[lab], weight=np.clip(tr_.y_ws, 0.5, 15.0), categorical_feature=["station_cat"]),
                      num_boost_round=3000, valid_sets=[lgb.Dataset(va_[cols], label=va_[lab], weight=np.clip(va_.y_ws, 0.5, 15.0))],
                      callbacks=[lgb.early_stopping(100, verbose=False)])
        n_it = max(b.best_iteration, 50)
        b = lgb.train(params, lgb.Dataset(train[cols], label=train[lab], weight=w_train, categorical_feature=["station_cat"]),
                      num_boost_round=n_it)
        preds[lab] = b.predict(test[cols])
        logger.info(f"  {lab}: {n_it} iterazioni")
    m2_dir = np.rad2deg(np.arctan2(preds["y_sin"], preds["y_cos"])) % 360

    # E: IFS ruotato del bias circolare
    rot = np.rad2deg(np.arctan2(test.bias_dsin_hour.fillna(0), test.bias_dcos_hour.fillna(1)))
    e_dir = (test.nwp_wd_valid - rot) % 360
    strong = test.y_ws.to_numpy() >= MIN_SPEED
    rows = []
    for name, d in (("IFS grezzo", test.nwp_wd_valid.to_numpy()), ("E (IFS ruotato del bias)", e_dir.to_numpy()),
                    ("M2 direzione", m2_dir)):
        e = ang_err(d, test.obs_dir.to_numpy())
        rows.append({"variante": name, "mae_deg_vento>=3": e[strong].mean(), "mae_deg_tutte": e.mean(),
                     "entro_45deg_%": 100 * (e[strong] <= 45).mean(), "n_vento>=3": int(strong.sum())})
    res = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(f"\nTest {test.valid_for.min():%d/%m} → {test.valid_for.max():%d/%m}/2026 (un errore casuale vale 90°)\n")
    print(res.round(1).to_string(index=False))
    names = {s["id"]: s["name"] for s in stations}
    t2 = test.assign(m2=m2_dir)
    per = t2[strong].groupby("station_cat").apply(lambda g: pd.Series({
        "n": len(g), "ifs": ang_err(g.nwp_wd_valid.to_numpy(), g.obs_dir.to_numpy()).mean(),
        "m2": ang_err(g.m2.to_numpy(), g.obs_dir.to_numpy()).mean()}), include_groups=False)
    per.index = [names[i] for i in per.index]
    print("\nPer stazione (gradi, vento osservato ≥ 3 km/h):\n" + per.round(0).sort_values("m2").to_string())
    res.to_csv(ROOT / "logs" / "retrain_netatmo_refresh" / "wind_dir_test.csv", index=False)


if __name__ == "__main__":
    main()
