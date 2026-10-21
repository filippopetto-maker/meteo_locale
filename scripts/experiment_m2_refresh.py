"""
experiment_m2_refresh.py — Aggiornare M2 con i dati di settembre-ottobre 2026 aiuta?

M2 congelato il 22/09 mostra in ombra un errore sistematico freddo (−0,35 °C contro
−0,05 di E), cresciuto con il raffreddamento di ottobre. Test fuori campione sulle
stesse righe di training di M2 (IFS a valid_for ≈ lead 0, target = storico orario
Netatmo): si addestra fino a una data di taglio e si valuta su 01/10 → fine dati.

  A  stile modello congelato: dati fino al 21/09 (n_iter fisso)
  B  dati fino al 30/09, stesso n_iter
  C  come B con pesi che decadono col tempo (emivita --half-life giorni)
  E  riferimento: nwp_valid − bias_st_hour

Utilizzo:
    python3 scripts/experiment_m2_refresh.py
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

logger = logging.getLogger("m2_refresh")

REFERENCE_MODEL = ROOT / "logs" / "retrain_netatmo" / "lgbm_m2.txt"
OUT_DIR = ROOT / "logs" / "retrain_netatmo_refresh"


def fit(data: pd.DataFrame, cols: list[str], n_iter: int, weights: np.ndarray | None = None) -> lgb.Booster:
    dtr = lgb.Dataset(data[cols], label=data.resid, weight=weights, categorical_feature=["station_cat"])
    return lgb.train({**DEFAULT_LGB_PARAMS, "seed": 42}, dtr, num_boost_round=n_iter)


def report(name: str, test: pd.DataFrame, pred: np.ndarray) -> dict:
    err = pred - test.y.to_numpy()
    out = {"variante": name, "n": len(test), "mae": np.abs(err).mean(), "bias": err.mean()}
    weeks = pd.Series(err, index=test.index).groupby(test.valid_for.dt.to_period("W-SUN")).agg(
        mae=lambda s: s.abs().mean(), bias="mean")
    out["sett_bias"] = " ".join(f"{b:+.2f}" for b in weeks["bias"])
    hours = pd.Series(err, index=test.index).groupby(test.valid_for.dt.hour).mean()
    out["ore_bias_max"] = f"{hours.min():+.2f}/{hours.max():+.2f}"
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Test di aggiornamento di M2 con dati recenti")
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--data-end", default="2026-10-22")
    ap.add_argument("--cut-a", default="2026-09-22")
    ap.add_argument("--cut-b", default="2026-10-01")
    ap.add_argument("--half-life", type=float, default=180.0)
    ap.add_argument("--pause", type=float, default=30.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    n_iter = lgb.Booster(model_file=str(REFERENCE_MODEL)).num_trees()
    target = pd.read_parquet(ex.TARGET_FILE)
    target["recorded_at"] = pd.to_datetime(target.recorded_at, utc=True)
    logger.info(f"Target Netatmo fino a {target.recorded_at.max():%Y-%m-%d %H:%M}")
    stations = sorted(db.get_active_stations(), key=lambda s: s["id"])
    stations = [s for s in stations if s["id"] in set(target.station_id) and s["id"] not in ex.EXCLUDED_STATIONS]
    feature_cols = inf.load_lgbm("temperature").feature_name()

    api = bt.OpenMeteo(ROOT / "logs" / "backtest" / "cache", args.pause)
    start = pd.Timestamp(args.since, tz="UTC").to_pydatetime() - timedelta(days=2)
    end = pd.Timestamp(args.data_end, tz="UTC").to_pydatetime()
    frames = [ex.station_frame(st, bt.fetch_historical(api, [st], start, end)[st["id"]], target, feature_cols)
              for st in stations]
    data = pd.concat(frames, ignore_index=True)
    data = data[data.valid_for >= pd.Timestamp(args.since, tz="UTC")].dropna(subset=["y", "nwp_valid"])
    data["resid"] = data.y - data.nwp_valid
    cols = [*feature_cols, "station_cat", "nwp_valid", "bias_st_hour"]
    logger.info(f"Righe {len(data):,} ({data.valid_for.min():%Y-%m-%d} → {data.valid_for.max():%Y-%m-%d}), "
                f"chiamate API nuove {api.calls}")

    cut_a, cut_b = (pd.Timestamp(c, tz="UTC") for c in (args.cut_a, args.cut_b))
    test = data[data.valid_for >= cut_b].reset_index(drop=True)
    logger.info(f"Test {test.valid_for.min():%Y-%m-%d} → {test.valid_for.max():%Y-%m-%d}: {len(test):,} righe")

    rows = []
    test_e = test.dropna(subset=["bias_st_hour"])
    rows.append(report("E (nwp − bias)", test_e, (test_e.nwp_valid - test_e.bias_st_hour).to_numpy()))
    rows.append(report("IFS grezzo", test, test.nwp_valid.to_numpy()))

    tr_a = data[data.valid_for < cut_a]
    booster_a = fit(tr_a, cols, n_iter)
    rows.append(report(f"A fino al {cut_a:%d/%m}", test, test.nwp_valid.to_numpy() + booster_a.predict(test[cols])))

    tr_b = data[data.valid_for < cut_b]
    booster_b = fit(tr_b, cols, n_iter)
    rows.append(report(f"B fino al {cut_b:%d/%m}", test, test.nwp_valid.to_numpy() + booster_b.predict(test[cols])))

    age = (cut_b - tr_b.valid_for).dt.total_seconds().to_numpy() / 86400
    w = 0.5 ** (age / args.half_life)
    booster_c = fit(tr_b, cols, n_iter, weights=w)
    rows.append(report(f"C B+pesi (emivita {args.half_life:.0f} g)", test,
                       test.nwp_valid.to_numpy() + booster_c.predict(test[cols])))

    # D: il residuo parte da E (nwp − bias) invece che dall'IFS grezzo, quindi eredita
    # la sua capacità di seguire la deriva stagionale; bias_st_hour resta anche come feature
    for label, tr_cut in ((f"D su E, fino al {cut_a:%d/%m}", cut_a), (f"D su E, fino al {cut_b:%d/%m}", cut_b)):
        tr_d = data[(data.valid_for < tr_cut) & data.bias_st_hour.notna()].copy()
        tr_d["resid"] = tr_d.y - (tr_d.nwp_valid - tr_d.bias_st_hour)
        booster_d = fit(tr_d, cols, n_iter)
        base = (test_e.nwp_valid - test_e.bias_st_hour).to_numpy()
        rows.append(report(label, test_e, base + booster_d.predict(test_e[cols])))

    res = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print("\n" + res.round(3).to_string(index=False))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    res.to_csv(OUT_DIR / "refresh_test.csv", index=False)


if __name__ == "__main__":
    main()
