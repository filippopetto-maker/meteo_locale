"""
experiment_retrain_netatmo.py — Retraining lead 1 sul target Netatmo storico

Il modello di produzione è addestrato su ERA5 → METAR (aeroporti). Qui lo si
riaddestra sul target che la mappa mostra davvero:

  target  data/netatmo_hourly_target.parquet (scripts/netatmo_backfill.py)
  input   ECMWF IFS, Historical Forecast API (stesso modello del backtest)

Due varianti, stesso contratto di produzione (riga NWP a X → temperatura a X+1h):
  M1  feature di produzione + stazione (categorica), target = temperatura
  M2  M1 + IFS a X+1h + bias IFS−Netatmo per stazione×ora sui 30 giorni
      precedenti (l'opzione E come feature), target = osservato − IFS a X+1h

Split temporale: train < --val-start ≤ validazione (early stopping) < --test-start.
Valutazione su --test-start in poi, in due modi:
  (a) input cucito (Historical Forecast), tutte le ore
  (b) lead 1 del backtest: input dai run IFS archiviati, verità = osservazioni
      live (tabella observations) — il confronto diretto con opzione E e
      modello attuale

Utilizzo:
    python3 scripts/experiment_retrain_netatmo.py
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
import inference as inf  # noqa: E402
from m2 import build_m2_frame  # noqa: E402
from forecast import DEFAULT_LGB_PARAMS  # noqa: E402

logger = logging.getLogger("retrain")

TARGET_FILE = ROOT / "data" / "netatmo_hourly_target.parquet"
BACKTEST_FILE = ROOT / "logs" / "backtest" / "backtest_2026-06-25_2026-09-21_r0-12_lag9.parquet"
OUT_DIR = ROOT / "logs" / "retrain_netatmo"
BIAS_DAYS, BIAS_MIN_DAYS = 30, 10
# Sigillo: un solo device e poche osservazioni live, target non affidabile.
EXCLUDED_STATIONS = {57}


def hour_bias_table(times: pd.Series, err: pd.Series) -> pd.Series:
    """Bias medio per (giorno, ora) sui BIAS_DAYS giorni precedenti, giorno corrente escluso."""
    e = pd.DataFrame({"e": err.to_numpy()}, index=pd.DatetimeIndex(times))
    e["date"], e["hour"] = e.index.floor("D"), e.index.hour
    piv = e.pivot_table(index="date", columns="hour", values="e").asfreq("D")
    roll = piv.rolling(BIAS_DAYS, min_periods=BIAS_MIN_DAYS).mean().shift(1)
    return roll.stack()


def lookup_bias(table: pd.Series, valid: pd.Series) -> np.ndarray:
    idx = pd.MultiIndex.from_arrays([valid.dt.floor("D"), valid.dt.hour])
    return table.reindex(idx).to_numpy()


def station_frame(st: dict, nwp: pd.DataFrame, target: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
    """Righe di training/test per una stazione: feature a X, verità a X+1h."""
    feat = build_m2_frame(nwp, st, feature_cols)
    nwp_t = nwp[["recorded_at", "temperature"]].copy()
    nwp_t["recorded_at"] = pd.to_datetime(nwp_t.recorded_at).dt.tz_localize("UTC")

    obs = target[target.station_id == st["id"]][["recorded_at", "temperature"]]
    feat = feat.merge(obs.rename(columns={"recorded_at": "valid_for", "temperature": "y"}),
                      on="valid_for", how="left")

    both = nwp_t.merge(obs, on="recorded_at", suffixes=("_nwp", "_obs")).dropna()
    table = hour_bias_table(both.recorded_at, both.temperature_nwp - both.temperature_obs)
    feat["bias_st_hour"] = lookup_bias(table, feat.valid_for)
    return feat


def train(df: pd.DataFrame, cols: list[str], label: str, val_start, params_extra=None) -> lgb.Booster:
    tr, va = df[df.valid_for < val_start], df[df.valid_for >= val_start]
    params = {**DEFAULT_LGB_PARAMS, **(params_extra or {})}
    dtr = lgb.Dataset(tr[cols], label=tr[label], categorical_feature=["station_cat"], free_raw_data=False)
    dva = lgb.Dataset(va[cols], label=va[label], reference=dtr, categorical_feature=["station_cat"])
    return lgb.train(params, dtr, num_boost_round=3000, valid_sets=[dva], valid_names=["val"],
                     callbacks=[lgb.early_stopping(100, verbose=False), lgb.log_evaluation(200)])


def main() -> None:
    ap = argparse.ArgumentParser(description="Retraining lead 1 sul target Netatmo storico")
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--val-start", default="2026-08-01")
    ap.add_argument("--test-start", default="2026-09-01")
    ap.add_argument("--test-end", default="2026-09-22")
    ap.add_argument("--data-end", default="2026-09-22",
                    help="Fine dell'input IFS: serve anche a (b), che valuta sempre fino alla fine del backtest")
    ap.add_argument("--pause", type=float, default=30.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    val_start = pd.Timestamp(args.val_start, tz="UTC")
    test_start = pd.Timestamp(args.test_start, tz="UTC")
    test_end = pd.Timestamp(args.test_end, tz="UTC")

    target = pd.read_parquet(TARGET_FILE)
    target["recorded_at"] = pd.to_datetime(target.recorded_at, utc=True)
    stations = sorted(db.get_active_stations(), key=lambda s: s["id"])
    stations = [s for s in stations if s["id"] in set(target.station_id) and s["id"] not in EXCLUDED_STATIONS]
    feature_cols = inf.load_lgbm("temperature").feature_name()
    logger.info(f"{len(stations)} stazioni con target storico, {len(feature_cols)} feature di produzione")

    # ── Input IFS cucito, una chiamata per stazione (in cache dopo la prima volta) ──
    api = bt.OpenMeteo(ROOT / "logs" / "backtest" / "cache", args.pause)
    start = pd.Timestamp(args.since, tz="UTC").to_pydatetime() - timedelta(days=2)
    frames = []
    for i, st in enumerate(stations, 1):
        nwp = bt.fetch_historical(api, [st], start, pd.Timestamp(args.data_end, tz="UTC").to_pydatetime())[st["id"]]
        frames.append(station_frame(st, nwp, target, feature_cols))
        logger.info(f"input {i}/{len(stations)} st.{st['id']} — chiamate API nuove {api.calls}")
    data = pd.concat(frames, ignore_index=True)
    data = data[data.valid_for >= pd.Timestamp(args.since, tz="UTC")]
    data["resid"] = data.y - data.nwp_valid

    cols_m1 = [*feature_cols, "station_cat"]
    cols_m2 = [*cols_m1, "nwp_valid", "bias_st_hour"]
    fit = data.dropna(subset=["y", "nwp_valid"])
    fit = fit[fit.valid_for < test_start]
    logger.info(f"Righe di training+validazione: {len(fit):,} (train < {args.val_start} ≤ val < {args.test_start})")

    m1 = train(fit, cols_m1, "y", val_start)
    m2 = train(fit, cols_m2, "resid", val_start)
    m1.save_model(str(OUT_DIR / "lgbm_m1.txt"))
    m2.save_model(str(OUT_DIR / "lgbm_m2.txt"))
    logger.info(f"M1 best_iteration {m1.best_iteration} | M2 best_iteration {m2.best_iteration}")

    # ── (a) Test su input cucito ──
    te = data[(data.valid_for >= test_start) & (data.valid_for < test_end)].dropna(subset=["y", "nwp_valid"])
    pa = pd.DataFrame({
        "IFS grezzo": te.nwp_valid,
        "E (IFS − bias staz×ora 30gg)": te.nwp_valid - te.bias_st_hour.fillna(0),
        "M1 retrain": m1.predict(te[cols_m1], num_iteration=m1.best_iteration),
        "M2 retrain residuo": te.nwp_valid + m2.predict(te[cols_m2], num_iteration=m2.best_iteration),
    }, index=te.index)
    mae_a = pa.sub(te.y, axis=0).abs().mean()

    # ── (b) Lead 1 del backtest: input dai run archiviati, verità = observations live ──
    # Tutte le righe del backtest da --test-start in poi (anche oltre --test-end).
    bk = pd.read_parquet(BACKTEST_FILE)
    bk = bk[(bk.lead == 1) & (bk.valid_for >= test_start)].dropna(subset=["t_obs", "t_nwp", "t_model"])
    bias_by_station = {st["id"]: data[data.station_cat == st["id"]].set_index("valid_for").bias_st_hour
                       for st in stations}
    all_st = sorted(db.get_active_stations(), key=lambda s: s["id"])
    runs = pd.date_range("2026-06-25", "2026-09-21 12:00", freq="12h", tz="UTC")
    hist_bk = bt.fetch_historical(api, all_st, runs[0].to_pydatetime() - timedelta(hours=bt.WARMUP_H),
                                  (runs[-1] + timedelta(hours=57)).to_pydatetime())
    rows = []
    by_id = {s["id"]: s for s in stations}
    for (run_init, issue), grp in bk.groupby(["run_init", "issue_at"]):
        run_frames = bt.fetch_run(api, all_st, run_init.to_pydatetime(), 58)
        for _, r in grp.iterrows():
            st = by_id.get(r.station_id)
            if st is None:
                continue
            df_in = bt.build_input(hist_bk[st["id"]], run_frames[st["id"]], run_init.to_pydatetime())
            f = station_frame(st, df_in, target.iloc[0:0], feature_cols)
            x = f[f.recorded_at == issue]
            if x.empty:
                continue
            x = x.assign(bias_st_hour=bias_by_station[st["id"]].get(r.valid_for, np.nan))
            rows.append({
                "station_id": st["id"], "valid_for": r.valid_for, "t_obs": r.t_obs,
                "IFS grezzo": r.t_nwp,
                "E (IFS − bias staz×ora 30gg)": r.t_nwp - (0 if pd.isna(x.bias_st_hour.iloc[0]) else x.bias_st_hour.iloc[0]),
                "Modello attuale": r.t_model,
                "M1 retrain": m1.predict(x[cols_m1], num_iteration=m1.best_iteration)[0],
                "M2 retrain residuo": r.t_nwp + m2.predict(x[cols_m2], num_iteration=m2.best_iteration)[0],
            })
    pb = pd.DataFrame(rows)
    names_b = [c for c in pb.columns if c not in ("station_id", "valid_for", "t_obs")]
    mae_b = pb[names_b].sub(pb.t_obs, axis=0).abs().mean()
    mae_b_st = pb.groupby("station_id")[names_b].apply(lambda g: g.sub(pb.loc[g.index, "t_obs"], axis=0).abs().mean())

    print(f"\nTest {args.test_start} → {args.test_end}")
    print(f"\n(a) input IFS cucito, tutte le ore — {len(te):,} righe, verità = storico Netatmo")
    print(mae_a.round(3).to_string())
    print(f"\n(b) lead 1 del backtest dal {max(test_start, bk.valid_for.min()):%Y-%m-%d} (run IFS archiviati, emissioni 09Z/21Z) — {len(pb):,} righe, "
          f"verità = observations live")
    print(mae_b.round(3).to_string())
    print("\nStazioni vinte (b):", mae_b_st.idxmin(axis=1).value_counts().to_dict())

    imp = pd.Series(m2.feature_importance("gain"), index=cols_m2).sort_values(ascending=False)
    print("\nM2 — top 10 feature (gain):", ", ".join(imp.head(10).index))
    pb.to_parquet(OUT_DIR / "test_lead1_backtest.parquet", index=False)
    mae_b_st.round(3).to_csv(OUT_DIR / "test_lead1_by_station.csv")
    logger.info(f"File in {OUT_DIR}/ — chiamate API nuove {api.calls}")


if __name__ == "__main__":
    main()
