"""
experiment_m2_humidity.py — M2 per l'umidità: modello diretto o sul punto di rugiada?

Stesso schema di M2 temperatura (IFS a valid_for + residuo LightGBM, target = storico
orario Netatmo, bias IFS−Netatmo per stazione × ora UTC sui 30 giorni precedenti),
applicato all'umidità relativa. T e RH servono poi per temperatura percepita e bulbo
umido, quindi si confrontano due architetture per la coerenza fisica:

  H1  residuo diretto su RH, risultato limitato a [0, 100]
  H2  residuo sul punto di rugiada Td; RH ricostruita da T di M2 e Td di H2 (Magnus)

Riferimenti: IFS grezzo, E (nwp − bias per stazione×ora).
Metriche: RH (MAE, bias, per ora del giorno e RH osservata > 90 %), bulbo umido
(Stull 2011) e temperatura percepita senza vento (Steadman) calcolati dalle previsioni
contro gli stessi calcolati dalle osservazioni.

Split fuori campione: addestramento fino a --cut (default 22/09), test da --cut a fine dati.

Utilizzo:
    python3 scripts/experiment_m2_humidity.py
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
import experiment_retrain_netatmo as ex  # noqa: E402
import inference as inf  # noqa: E402
from forecast import DEFAULT_LGB_PARAMS  # noqa: E402
from m2 import build_m2_frame  # noqa: E402

logger = logging.getLogger("m2_humidity")

REFERENCE_MODEL = ROOT / "logs" / "retrain_netatmo" / "lgbm_m2.txt"
OUT_DIR = ROOT / "logs" / "retrain_netatmo_refresh"
A, B = 17.62, 243.12  # Magnus (WMO)


def dewpoint(t: np.ndarray, rh: np.ndarray) -> np.ndarray:
    g = np.log(np.clip(rh, 1.0, 100.0) / 100.0) + A * t / (B + t)
    return B * g / (A - g)


def rh_from(t: np.ndarray, td: np.ndarray) -> np.ndarray:
    return np.clip(100.0 * np.exp(A * td / (B + td) - A * t / (B + t)), 0.0, 100.0)


def wet_bulb(t: np.ndarray, rh: np.ndarray) -> np.ndarray:
    rh = np.clip(rh, 1.0, 100.0)
    return (t * np.arctan(0.151977 * np.sqrt(rh + 8.313659)) + np.arctan(t + rh) - np.arctan(rh - 1.676331)
            + 0.00391838 * rh ** 1.5 * np.arctan(0.023101 * rh) - 4.686035)


def apparent(t: np.ndarray, rh: np.ndarray) -> np.ndarray:
    e = np.clip(rh, 0.0, 100.0) / 100.0 * 6.105 * np.exp(17.27 * t / (237.7 + t))
    return t + 0.33 * e - 4.0


def humidity_frame(st: dict, nwp: pd.DataFrame, target: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
    """Righe di training/test: M2 temperatura + colonne umidità (nwp, verità, bias, punto di rugiada)."""
    feat = ex.station_frame(st, nwp, target, feature_cols)
    nwp_h = nwp[["recorded_at", "humidity", "temperature"]].copy()
    nwp_h["recorded_at"] = pd.to_datetime(nwp_h.recorded_at).dt.tz_localize("UTC")
    feat = feat.merge(nwp_h[["recorded_at", "humidity"]].rename(
        columns={"recorded_at": "valid_for", "humidity": "nwp_rh_valid"}), on="valid_for", how="left")
    obs = target[target.station_id == st["id"]][["recorded_at", "humidity"]].rename(
        columns={"recorded_at": "valid_for", "humidity": "y_rh"})
    feat = feat.merge(obs, on="valid_for", how="left")

    # bias IFS − Netatmo: umidità e punto di rugiada, per stazione × ora UTC, 30 giorni precedenti
    both = nwp_h.merge(target[target.station_id == st["id"]][["recorded_at", "temperature", "humidity"]],
                       on="recorded_at", suffixes=("_nwp", "_obs")).dropna()
    td_nwp = dewpoint(both.temperature_nwp.to_numpy(), both.humidity_nwp.to_numpy())
    td_obs = dewpoint(both.temperature_obs.to_numpy(), both.humidity_obs.to_numpy())
    for name, err in (("bias_rh_hour", both.humidity_nwp - both.humidity_obs),
                      ("bias_td_hour", pd.Series(td_nwp - td_obs, index=both.index))):
        table = ex.hour_bias_table(both.recorded_at, err)
        feat[name] = ex.lookup_bias(table, feat.valid_for)
    feat["nwp_td_valid"] = dewpoint(feat.nwp_valid.to_numpy(), feat.nwp_rh_valid.to_numpy())
    feat["y_td"] = dewpoint(feat.y.to_numpy(), feat.y_rh.to_numpy())
    return feat


def fit(df: pd.DataFrame, cols: list[str], label: str, n_iter: int | None, val_days: int = 56):
    """Se n_iter è None: early stopping sulle ultime val_days del training, poi si riaddestra su tutto."""
    params = {**DEFAULT_LGB_PARAMS, "seed": 42}
    if n_iter is None:
        cut = df.valid_for.max() - timedelta(days=val_days)
        tr, va = df[df.valid_for <= cut], df[df.valid_for > cut]
        dtr = lgb.Dataset(tr[cols], label=tr[label], categorical_feature=["station_cat"])
        dva = lgb.Dataset(va[cols], label=va[label], reference=dtr)
        b = lgb.train(params, dtr, num_boost_round=3000, valid_sets=[dva],
                      callbacks=[lgb.early_stopping(100, verbose=False)])
        n_iter = max(b.best_iteration, 50)
        logger.info(f"  {label}: early stopping a {n_iter} iterazioni")
    dtr = lgb.Dataset(df[cols], label=df[label], categorical_feature=["station_cat"])
    return lgb.train(params, dtr, num_boost_round=n_iter), n_iter


def metrics(name: str, t_obs, rh_obs, t_pred, rh_pred, test: pd.DataFrame) -> dict:
    e = rh_pred - rh_obs
    hrs = test.valid_for.dt.hour.to_numpy()
    wb = wet_bulb(t_pred, rh_pred) - wet_bulb(t_obs, rh_obs)
    ap = apparent(t_pred, rh_pred) - apparent(t_obs, rh_obs)
    sat, night = rh_obs > 90, hrs <= 5
    return {"variante": name, "rh_mae": np.abs(e).mean(), "rh_bias": e.mean(),
            "rh_mae_sat90": np.abs(e[sat]).mean(), "rh_mae_notte": np.abs(e[night]).mean(),
            "rh_mae_giorno": np.abs(e[(hrs >= 9) & (hrs <= 17)]).mean(),
            "wb_mae": np.abs(wb).mean(), "wb_bias": wb.mean(), "app_mae": np.abs(ap).mean(), "app_bias": ap.mean()}


def main() -> None:
    ap = argparse.ArgumentParser(description="M2 umidità: RH diretta o punto di rugiada")
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--data-end", default="2026-10-22")
    ap.add_argument("--cut", default="2026-09-22")
    ap.add_argument("--pause", type=float, default=30.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    target = pd.read_parquet(ex.TARGET_FILE)
    target["recorded_at"] = pd.to_datetime(target.recorded_at, utc=True)
    logger.info(f"Target Netatmo fino a {target.recorded_at.max():%Y-%m-%d %H:%M}")
    stations = sorted(db.get_active_stations(), key=lambda s: s["id"])
    stations = [s for s in stations if s["id"] in set(target.station_id) and s["id"] not in ex.EXCLUDED_STATIONS]
    feature_cols = inf.load_lgbm("temperature").feature_name()

    api = bt.OpenMeteo(ROOT / "logs" / "backtest" / "cache", args.pause)
    start = pd.Timestamp(args.since, tz="UTC").to_pydatetime() - timedelta(days=2)
    end = pd.Timestamp(args.data_end, tz="UTC").to_pydatetime()
    frames = [humidity_frame(st, bt.fetch_historical(api, [st], start, end)[st["id"]], target, feature_cols)
              for st in stations]
    data = pd.concat(frames, ignore_index=True)
    data = data[data.valid_for >= pd.Timestamp(args.since, tz="UTC")]
    data = data.dropna(subset=["y", "y_rh", "nwp_valid", "nwp_rh_valid"]).reset_index(drop=True)
    data["resid_t"] = data.y - data.nwp_valid
    data["resid_rh"] = data.y_rh - data.nwp_rh_valid
    data["resid_td"] = data.y_td - data.nwp_td_valid
    cut = pd.Timestamp(args.cut, tz="UTC")
    train, test = data[data.valid_for < cut], data[data.valid_for >= cut].reset_index(drop=True)
    logger.info(f"Train {len(train):,} righe fino al {cut:%d/%m}; test {len(test):,} righe "
                f"({test.valid_for.min():%d/%m} → {test.valid_for.max():%d/%m})")

    n_t = lgb.Booster(model_file=str(REFERENCE_MODEL)).num_trees()
    cols_t = [*feature_cols, "station_cat", "nwp_valid", "bias_st_hour"]
    booster_t, _ = fit(train.rename(columns={"resid_t": "lab"}), cols_t, "lab", n_t)
    t_m2 = test.nwp_valid.to_numpy() + booster_t.predict(test[cols_t])

    cols_h1 = [*feature_cols, "station_cat", "nwp_valid", "nwp_rh_valid", "bias_rh_hour"]
    b_h1, _ = fit(train, cols_h1, "resid_rh", None)
    rh_h1 = np.clip(test.nwp_rh_valid.to_numpy() + b_h1.predict(test[cols_h1]), 0, 100)

    cols_h2 = [*feature_cols, "station_cat", "nwp_valid", "nwp_rh_valid", "nwp_td_valid", "bias_td_hour"]
    b_h2, _ = fit(train, cols_h2, "resid_td", None)
    td_h2 = test.nwp_td_valid.to_numpy() + b_h2.predict(test[cols_h2])
    rh_h2 = rh_from(t_m2, td_h2)

    t_obs, rh_obs, t_ifs, rh_ifs = (test[c].to_numpy() for c in ("y", "y_rh", "nwp_valid", "nwp_rh_valid"))
    rows = [metrics("IFS grezzo", t_obs, rh_obs, t_ifs, rh_ifs, test)]
    ok = test.bias_rh_hour.notna().to_numpy()
    sub = test[ok].reset_index(drop=True)
    rows.append(metrics("E (nwp − bias RH)", t_obs[ok], rh_obs[ok], t_ifs[ok],
                        np.clip(rh_ifs[ok] - sub.bias_rh_hour.to_numpy(), 0, 100), sub))
    rows.append(metrics("H1 residuo RH diretto", t_obs, rh_obs, t_m2, rh_h1, test))
    rows.append(metrics("H2 punto di rugiada", t_obs, rh_obs, t_m2, rh_h2, test))
    rows.append(metrics("H1 con T di IFS", t_obs, rh_obs, t_ifs, rh_h1, test))

    res = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print(f"\nTest {test.valid_for.min():%d/%m} → {test.valid_for.max():%d/%m}, n = {len(test):,}\n")
    print(res.round(2).to_string(index=False))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    res.to_csv(OUT_DIR / "humidity_test.csv", index=False)


if __name__ == "__main__":
    main()
