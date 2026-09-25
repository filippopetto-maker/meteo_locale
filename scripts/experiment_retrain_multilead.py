"""
experiment_retrain_multilead.py — M1/M2 del retraining Netatmo applicati da lead 1 a 48

Prende i modelli salvati da experiment_retrain_netatmo.py (addestrati sul
contratto lead 1: riga NWP a X → temperatura a X+1h) e li applica come fa
inference.predict_from_df per ogni lead: per valid_for = emissione + L si usa
la riga a valid_for − 1h dell'input visto all'emissione (storico cucito prima
dell'init, run IFS archiviato dopo). Lag e trend sono quindi calcolati sulla
catena del run, come in produzione.

Il bias IFS−Netatmo per stazione × ora (feature di M2 e correzione dell'opzione E)
è preso dal giorno dell'emissione: media dei 30 giorni precedenti a quel giorno,
nessun dato successivo all'emissione. --bias-source sceglie la verità del bias:
  target  storico ricostruito (media oraria dei device, come in training)
  live    osservazioni live Netatmo entro ±15 min dall'ora (come potrà fare la produzione)
  both    entrambe, valutate sulle stesse righe

Righe e verità dal backtest (logs/backtest/…lag9.parquet): run 00Z/12Z,
emissione init + 9h, osservazioni live Netatmo.

Utilizzo:
    python3 scripts/experiment_retrain_multilead.py
    python3 scripts/experiment_retrain_multilead.py --models logs/retrain_netatmo:2026-09-01
    python3 scripts/experiment_retrain_multilead.py --bias-source both --models logs/retrain_netatmo_senza2026:2026-06-25
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
import netatmo_backfill as nb  # noqa: E402

logger = logging.getLogger("retrain_multilead")

OUT_DIR = ROOT / "logs" / "retrain_netatmo_multilead"
HIST_START, HIST_END = "2024-03-14", "2026-09-22"  # stesso input cucito del training (in cache)
BANDS = [(1, 6), (7, 12), (13, 24), (25, 36), (37, 48)]


def live_truth(stations, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Osservazioni live Netatmo (QC < 2) entro ±15 min dall'ora esatta, media per ora."""
    client = db.get_client()
    parts = []
    for st in stations:
        obs = nb._paged_obs(client, st["id"], start, end)
        off = (obs.recorded_at - obs.recorded_at.dt.round("h")).abs()
        obs = obs[off <= pd.Timedelta(minutes=15)]
        hourly = obs.groupby(obs.recorded_at.dt.round("h")).temperature.mean()
        parts.append(pd.DataFrame({"station_id": st["id"], "recorded_at": hourly.index,
                                   "temperature": hourly.to_numpy()}))
    return pd.concat(parts, ignore_index=True)


def bias_tables(api, stations, target) -> dict[int, pd.Series]:
    """Bias IFS−verità per (giorno, ora) sui 30 giorni precedenti, per stazione."""
    start = pd.Timestamp(HIST_START, tz="UTC").to_pydatetime() - timedelta(days=2)
    end = pd.Timestamp(HIST_END, tz="UTC").to_pydatetime()
    out = {}
    for st in stations:
        nwp = bt.fetch_historical(api, [st], start, end)[st["id"]][["recorded_at", "temperature"]].copy()
        nwp["recorded_at"] = pd.to_datetime(nwp.recorded_at).dt.tz_localize("UTC")
        obs = target[target.station_id == st["id"]][["recorded_at", "temperature"]]
        both = nwp.merge(obs, on="recorded_at", suffixes=("_nwp", "_obs")).dropna()
        out[st["id"]] = ex.hour_bias_table(both.recorded_at, both.temperature_nwp - both.temperature_obs)
    return out


def build_rows(api, stations, bk, feature_cols, tables: dict[str, dict], target) -> pd.DataFrame:
    """Feature di ogni (run, stazione, lead) come le vede la produzione all'emissione."""
    all_st = sorted(db.get_active_stations(), key=lambda s: s["id"])
    runs = sorted(bk.run_init.unique())
    hist = bt.fetch_historical(api, all_st, runs[0].to_pydatetime() - timedelta(hours=bt.WARMUP_H),
                               (runs[-1] + timedelta(hours=57)).to_pydatetime())
    empty = target.iloc[0:0]
    parts = []
    for i, run_init in enumerate(runs, 1):
        grp = bk[bk.run_init == run_init]
        issue = grp.issue_at.iloc[0]
        run_frames = bt.fetch_run(api, all_st, run_init.to_pydatetime(), 58)
        for st in stations:
            rows = grp[grp.station_id == st["id"]]
            if rows.empty:
                continue
            df_in = bt.build_input(hist[st["id"]], run_frames[st["id"]], run_init.to_pydatetime())
            f = ex.station_frame(st, df_in, empty, feature_cols)
            f = f[f.valid_for.isin(rows.valid_for)].copy()
            # Bias noto all'emissione: riga del giorno di emissione, ora di valid_for.
            idx = pd.MultiIndex.from_arrays([pd.Series(issue.floor("D"), index=f.index), f.valid_for.dt.hour])
            for src, tab in tables.items():
                f[f"bias_{src}"] = tab[st["id"]].reindex(idx).to_numpy()
            parts.append(f.merge(rows[["station_id", "run_init", "lead", "valid_for", "t_obs", "t_nwp", "t_model"]]
                                 .rename(columns={"station_id": "station_cat"}), on=["station_cat", "valid_for"]))
        if i % 25 == 0 or i == len(runs):
            logger.info(f"run {i}/{len(runs)} — chiamate API nuove {api.calls}")
    return pd.concat(parts, ignore_index=True)


def evaluate(rows: pd.DataFrame, model_dir: Path, since: pd.Timestamp, feature_cols,
             bias_col: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    m1 = lgb.Booster(model_file=str(model_dir / "lgbm_m1.txt"))
    m2 = lgb.Booster(model_file=str(model_dir / "lgbm_m2.txt"))
    cols_m1 = [*feature_cols, "station_cat"]
    cols_m2 = [*cols_m1, "nwp_valid", "bias_st_hour"]
    te = rows[rows.valid_for >= since].dropna(subset=["t_obs", "t_nwp", "t_model"]).copy()
    te["bias_st_hour"] = te[bias_col]
    te["IFS grezzo"] = te.t_nwp
    te["E (IFS − bias 30gg)"] = te.t_nwp - te.bias_st_hour.fillna(0)
    te["Modello attuale"] = te.t_model
    te["M1 retrain"] = m1.predict(te[cols_m1])
    te["M2 retrain"] = te.nwp_valid + m2.predict(te[cols_m2])
    names = ["IFS grezzo", "E (IFS − bias 30gg)", "Modello attuale", "M1 retrain", "M2 retrain"]
    err = te[names].sub(te.t_obs, axis=0).abs()
    err["lead"], err["station_id"] = te.lead.to_numpy(), te.station_cat.to_numpy()
    return te, err


def report(err: pd.DataFrame, title: str) -> pd.DataFrame:
    names = [c for c in err.columns if c not in ("lead", "station_id")]
    by_lead = err.groupby("lead")[names].mean()
    bands = pd.DataFrame({f"{a}–{b}h": err[(err.lead >= a) & (err.lead <= b)][names].mean() for a, b in BANDS}).T
    bands.loc["tutti 1–48h"] = err[names].mean()
    wins = err.groupby("station_id")[names].mean().idxmin(axis=1).value_counts().to_dict()
    print(f"\n== {title} — {len(err):,} previsioni, MAE °C ==")
    print(bands.round(2).to_string())
    print("\nPer lead:")
    print(by_lead.loc[[1, 3, 6, 12, 18, 24, 30, 36, 42, 48]].round(2).to_string())
    print("Stazioni vinte (1–48h):", wins)
    return by_lead


def main() -> None:
    ap = argparse.ArgumentParser(description="M1/M2 del retraining Netatmo da lead 1 a 48")
    ap.add_argument("--models", nargs="+",
                    default=["logs/retrain_netatmo_senza2026:2026-06-25", "logs/retrain_netatmo:2026-09-01"],
                    help="cartella_modelli:inizio_test (il test deve essere dopo la fine del training)")
    ap.add_argument("--bias-source", choices=["target", "live", "both"], default="target")
    ap.add_argument("--pause", type=float, default=30.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features", "backtest", "retrain"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    target = pd.read_parquet(ex.TARGET_FILE)
    target["recorded_at"] = pd.to_datetime(target.recorded_at, utc=True)
    stations = sorted(db.get_active_stations(), key=lambda s: s["id"])
    stations = [s for s in stations if s["id"] in set(target.station_id) and s["id"] not in ex.EXCLUDED_STATIONS]
    feature_cols = inf.load_lgbm("temperature").feature_name()

    bk = pd.read_parquet(ex.BACKTEST_FILE)
    api = bt.OpenMeteo(ROOT / "logs" / "backtest" / "cache", args.pause)
    sources = ["target", "live"] if args.bias_source == "both" else [args.bias_source]
    truth = {"target": lambda: target,
             "live": lambda: live_truth(stations, pd.Timestamp("2026-05-01", tz="UTC"),
                                        pd.Timestamp(HIST_END, tz="UTC"))}
    tables = {src: bias_tables(api, stations, truth[src]()) for src in sources}
    rows = build_rows(api, stations, bk, feature_cols, tables, target)
    chk = (rows.nwp_valid - rows.t_nwp).abs().max()
    logger.info(f"{len(rows):,} righe (run × stazione × lead) — max |nwp_valid − t_nwp| = {chk:.3f}")

    for spec in args.models:
        model_dir, since = spec.rsplit(":", 1)
        summary = {}
        for src in sources:
            te, err = evaluate(rows, ROOT / model_dir, pd.Timestamp(since, tz="UTC"), feature_cols, f"bias_{src}")
            by_lead = report(err, f"modelli {model_dir}, test da {since}, bias da {src}")
            tag = f"{Path(model_dir).name}_bias-{src}"
            by_lead.round(3).to_csv(OUT_DIR / f"mae_by_lead_{tag}.csv")
            te.drop(columns=feature_cols).to_parquet(OUT_DIR / f"test_{tag}.parquet", index=False)
            summary[src] = {"righe": len(te), "bias mancante %": 100 * te.bias_st_hour.isna().mean(),
                            **err[["E (IFS − bias 30gg)", "M2 retrain"]].mean().to_dict()}
        if len(sources) > 1:
            print(f"\n== Confronto sorgenti del bias, stesse righe — {model_dir}, test da {since} ==")
            print(pd.DataFrame(summary).T.round(3).to_string())
    logger.info(f"File in {OUT_DIR}/ — chiamate API nuove {api.calls}")


if __name__ == "__main__":
    main()
