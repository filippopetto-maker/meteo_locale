"""
experiment_m2_wind_metar.py — M2 vento (velocità e direzione) addestrato sui METAR degli aeroporti del Lazio

Verità ufficiale con orientamento reale (nord vero): METAR/SPECI di 8 aeroporti (Iowa Mesonet,
dal 14/03/2024): LIRF Fiumicino, LIRA Ciampino, LIRU Urbe, LIRE Pratica di Mare, LIRL Latina,
LIRG Guidonia, LIRV Viterbo, LIRH Frosinone. Si confronta IFS grezzo, E (bias recente) e M2 sullo
stesso schema di experiment_m2_wind.py (velocità, residuo su log1p) e experiment_m2_wind_dir.py
(direzione: seno e coseno con due LightGBM, pesati con la velocità).

Vento in km/h (nodi × 1,852), un valore per ora: il rapporto METAR più vicino all'ora entro ±35 min.
Direzione variabile (VRB) esclusa. Metrica di direzione solo con vento osservato ≥ 3 km/h.

Split: addestramento fino a --cut (default 01/05/2026, early stopping sulle ultime 8 settimane),
test da --cut.

Utilizzo:
    python3 scripts/experiment_m2_wind_metar.py
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
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "model"))

import backtest_ifs_lead as bt  # noqa: E402
import experiment_m2_humidity as hx  # noqa: E402
import experiment_m2_wind as wx  # noqa: E402
import experiment_retrain_netatmo as ex  # noqa: E402
import features as ft  # noqa: E402
import inference as inf  # noqa: E402
from forecast import DEFAULT_LGB_PARAMS  # noqa: E402

logger = logging.getLogger("m2_wind_metar")
METAR_DIR = ROOT / "logs" / "metar_lazio"
IEM = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={icao}&data=drct&data=sknt&data=gust"
       "&year1=2024&month1=3&day1=14&year2=2026&month2=10&day2=23&tz=Etc/UTC&format=onlycomma&latlon=yes"
       "&missing=empty&report_type=3&report_type=4")
AIRPORTS = {"LIRF": 9001, "LIRA": 9002, "LIRU": 9003, "LIRE": 9004, "LIRL": 9005, "LIRG": 9006, "LIRV": 9007, "LIRH": 9008}
NAMES = {"LIRF": "Fiumicino", "LIRA": "Ciampino", "LIRU": "Urbe", "LIRE": "Pratica di Mare", "LIRL": "Latina",
         "LIRG": "Guidonia", "LIRV": "Viterbo", "LIRH": "Frosinone"}
MIN_SPEED = 3.0


def load_metar(icao: str) -> tuple[pd.DataFrame, float, float]:
    path = METAR_DIR / f"{icao}.csv"
    if not path.exists():
        METAR_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(requests.get(IEM.format(icao=icao), timeout=120).content)
    m = pd.read_csv(path)
    lat, lon = float(m.lat.iloc[0]), float(m.lon.iloc[0])
    m["t"] = pd.to_datetime(m.valid, utc=True)
    m["h"] = m.t.dt.round("h")
    m = m[(m.t - m.h).abs() <= pd.Timedelta(minutes=35)].copy()
    m["gap"] = (m.t - m.h).abs()
    m = m.sort_values("gap").drop_duplicates("h").sort_values("h")
    out = pd.DataFrame({"recorded_at": m.h.to_numpy(), "ws": (m.sknt * 1.852).to_numpy(),
                        "obs_dir": m.drct.to_numpy()})
    out["recorded_at"] = pd.to_datetime(out.recorded_at, utc=True)
    out.loc[out.ws < MIN_SPEED, "obs_dir"] = np.nan
    return out.dropna(subset=["ws"]), lat, lon


def ang_err(a, b):
    d = np.abs(np.asarray(a) - np.asarray(b)) % 360
    return np.minimum(d, 360 - d)


def fit_lgb(train, cols, label, weight_col=None, n_iter=None):
    params = {**DEFAULT_LGB_PARAMS, "seed": 42}
    w = None if weight_col is None else np.clip(train[weight_col].to_numpy(), 0.5, 15.0)
    if n_iter is None:
        cut = train.valid_for.max() - timedelta(days=56)
        a, b = train[train.valid_for <= cut], train[train.valid_for > cut]
        wa = None if weight_col is None else np.clip(a[weight_col].to_numpy(), 0.5, 15.0)
        wb = None if weight_col is None else np.clip(b[weight_col].to_numpy(), 0.5, 15.0)
        bst = lgb.train(params, lgb.Dataset(a[cols], label=a[label], weight=wa, categorical_feature=["station_cat"]),
                        num_boost_round=3000, valid_sets=[lgb.Dataset(b[cols], label=b[label], weight=wb)],
                        callbacks=[lgb.early_stopping(100, verbose=False)])
        n_iter = max(bst.best_iteration, 50)
    return lgb.train(params, lgb.Dataset(train[cols], label=train[label], weight=w, categorical_feature=["station_cat"]),
                     num_boost_round=n_iter), n_iter


def main() -> None:
    ap = argparse.ArgumentParser(description="M2 vento sui METAR del Lazio")
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--data-end", default="2026-10-22")
    ap.add_argument("--cut", default="2026-05-01")
    ap.add_argument("--pause", type=float, default=30.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    feature_cols = inf.load_lgbm("temperature").feature_name()
    api = bt.OpenMeteo(ROOT / "logs" / "backtest" / "cache", args.pause)
    start = pd.Timestamp(args.since, tz="UTC").to_pydatetime() - timedelta(days=2)
    end = pd.Timestamp(args.data_end, tz="UTC").to_pydatetime()

    frames = []
    for icao, sid in AIRPORTS.items():
        metar, lat, lon = load_metar(icao)
        st = {"id": sid, "lat": lat, "lon": lon, **ft.compute_static_orography(lat, lon)}
        st["microclima"] = "costiera" if st["dist_sea_km"] < 8 else "standard"
        nwp = bt.fetch_historical(api, [st], start, end)[sid]
        tgt = metar.assign(station_id=sid)[["recorded_at", "ws", "station_id"]]
        f = wx.station_frame(st, nwp, tgt, feature_cols)
        f = f.merge(metar.rename(columns={"recorded_at": "valid_for"})[["valid_for", "obs_dir"]], on="valid_for", how="left")
        w = nwp[["recorded_at", "wind_direction"]].copy()
        w["recorded_at"] = pd.to_datetime(w.recorded_at).dt.tz_localize("UTC")
        f = f.merge(w.rename(columns={"recorded_at": "valid_for", "wind_direction": "nwp_wd_valid"}), on="valid_for", how="left")
        both = w.merge(metar[["recorded_at", "obs_dir"]], on="recorded_at").dropna()
        delta = np.deg2rad(both.wind_direction - both.obs_dir)
        for name, comp in (("bias_dsin_hour", np.sin(delta)), ("bias_dcos_hour", np.cos(delta))):
            f[name] = ex.lookup_bias(ex.hour_bias_table(both.recorded_at, pd.Series(comp.to_numpy(), index=both.index)),
                                     f.valid_for)
        frames.append(f)
        logger.info(f"{icao} {NAMES[icao]:<16} ore METAR {len(metar):>6}, con direzione {metar.obs_dir.notna().sum():>6}, "
                    f"quota {st['altitude']} m, mare {st['dist_sea_km']} km")
    data = pd.concat(frames, ignore_index=True)
    data = data[data.valid_for >= pd.Timestamp(args.since, tz="UTC")].dropna(subset=["y_ws", "nwp_ws_valid"]).reset_index(drop=True)
    data["base"] = np.log1p(data.nwp_ws_valid)
    data["resid"] = np.log1p(data.y_ws) - data.base
    cut = pd.Timestamp(args.cut, tz="UTC")
    train, test = data[data.valid_for < cut], data[data.valid_for >= cut].reset_index(drop=True)
    logger.info(f"Train {len(train):,} ore fino al {cut:%d/%m/%Y}; test {len(test):,} ({test.valid_for.min():%d/%m} → {test.valid_for.max():%d/%m})")

    # ── velocità ──
    cols_s = [*feature_cols, "station_cat", "nwp_ws_valid", "nwp_wd_sin", "nwp_wd_cos", "bias_ws_hour"]
    bs, n_s = fit_lgb(train, cols_s, "resid")
    pred_s = np.expm1(test.base.to_numpy() + bs.predict(test[cols_s]))
    obs_s, ifs_s = test.y_ws.to_numpy(), test.nwp_ws_valid.to_numpy()
    e_s = np.expm1(test.base.to_numpy() - np.nan_to_num(test.bias_ws_hour.to_numpy()))
    rows = [{"variante": n, "mae_kmh": np.abs(p - obs_s).mean(), "bias_kmh": (p - obs_s).mean(),
             "corr": np.corrcoef(p, obs_s)[0, 1]} for n, p in (("IFS grezzo", ifs_s), ("E (bias log)", e_s), (f"M2 ({n_s} it.)", pred_s))]
    print(f"\n== VELOCITÀ, test {test.valid_for.min():%d/%m} → {test.valid_for.max():%d/%m}/2026, METAR medio {obs_s.mean():.1f} km/h, n = {len(test):,} ==\n")
    print(pd.DataFrame(rows).round(2).to_string(index=False))

    # ── direzione ──
    dtrain = train.dropna(subset=["obs_dir", "nwp_wd_valid"]).copy()
    dtest = test.dropna(subset=["obs_dir", "nwp_wd_valid"]).reset_index(drop=True)
    dtrain["y_sin"], dtrain["y_cos"] = np.sin(np.deg2rad(dtrain.obs_dir)), np.cos(np.deg2rad(dtrain.obs_dir))
    cols_d = [*feature_cols, "station_cat", "nwp_ws_valid", "nwp_wd_sin", "nwp_wd_cos", "bias_dsin_hour", "bias_dcos_hour"]
    pr = {}
    for lab in ("y_sin", "y_cos"):
        b, n = fit_lgb(dtrain, cols_d, lab, weight_col="y_ws")
        pr[lab] = b.predict(dtest[cols_d])
        logger.info(f"direzione {lab}: {n} iterazioni")
    m2_dir = np.rad2deg(np.arctan2(pr["y_sin"], pr["y_cos"])) % 360
    rot = np.rad2deg(np.arctan2(dtest.bias_dsin_hour.fillna(0), dtest.bias_dcos_hour.fillna(1)))
    e_dir = ((dtest.nwp_wd_valid - rot) % 360).to_numpy()
    rows = []
    for n, d in (("IFS grezzo", dtest.nwp_wd_valid.to_numpy()), ("E (IFS ruotato del bias)", e_dir), ("M2 direzione", m2_dir)):
        e = ang_err(d, dtest.obs_dir.to_numpy())
        rows.append({"variante": n, "mae_gradi": e.mean(), "entro_45gradi_%": 100 * (e <= 45).mean(), "n": len(e)})
    print(f"\n== DIREZIONE (METAR con vento ≥ 3 km/h; casuale = 90°) ==\n")
    print(pd.DataFrame(rows).round(1).to_string(index=False))

    inv = {v: k for k, v in AIRPORTS.items()}
    t2 = dtest.assign(m2=m2_dir, e=e_dir)
    per = t2.groupby("station_cat").apply(lambda g: pd.Series({
        "n": len(g), "ifs_gradi": ang_err(g.nwp_wd_valid, g.obs_dir).mean(), "E_gradi": ang_err(g.e, g.obs_dir).mean(),
        "M2_gradi": ang_err(g.m2, g.obs_dir).mean()}), include_groups=False)
    ps = test.assign(p=pred_s).groupby("station_cat").apply(lambda g: pd.Series({
        "mae_ifs_kmh": (g.nwp_ws_valid - g.y_ws).abs().mean(), "mae_M2_kmh": (g.p - g.y_ws).abs().mean(),
        "metar_medio": g.y_ws.mean()}), include_groups=False)
    per = per.join(ps)
    per.index = [f"{inv[i]} {NAMES[inv[i]]}" for i in per.index]
    print("\nPer aeroporto:\n" + per.round(1).to_string())
    pd.DataFrame(rows).to_csv(ROOT / "logs" / "retrain_netatmo_refresh" / "wind_metar_dir_test.csv", index=False)


if __name__ == "__main__":
    main()
