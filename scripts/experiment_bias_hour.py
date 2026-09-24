"""
experiment_bias_hour.py — MOS minimo: NWP grezzo meno il bias per stazione e ora

Legge il parquet di scripts/backtest_ifs_lead.py e confronta, su un periodo di
test mai usato per stimare i bias:

  A  ifs            IFS grezzo
  B  model          modello di produzione (LGBM + RF + ARSIAL) su IFS
  C  ifs_st         IFS − bias medio della stazione          (stima su train)
  D  ifs_st_hour    IFS − bias per stazione × ora UTC        (stima su train)
  E  ifs_rolling    IFS − bias per stazione × ora UTC sui 30 giorni prima di
                    ogni emissione (solo valid_for già passati): è la versione
                    che la produzione può calcolare davvero ogni giorno
  F  model_st_hour  modello − bias per stazione × ora UTC    (stima su train)

Bias = media di (previsione − osservazione) su tutti i lead: l'errore dipende
poco dal lead e molto dall'ora (backtest del 24/09/2026).

Utilizzo:
    python3 scripts/experiment_bias_hour.py
    python3 scripts/experiment_bias_hour.py --test-start 2026-09-01 --window-days 30
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IN = ROOT / "logs" / "backtest" / "backtest_2026-06-25_2026-09-21_r0-12_lag9.parquet"
MIN_N = 20  # sotto questo numero di campioni una cella stazione×ora ricade sul bias di stazione


def bias_table(train: pd.DataFrame, col: str) -> tuple[pd.Series, pd.Series]:
    err = train[col] - train.t_obs
    by_st = err.groupby(train.station_id).mean()
    g = err.groupby([train.station_id, train.hour])
    by_st_hour = g.mean()[g.size() >= MIN_N]
    return by_st, by_st_hour


def apply_bias(df: pd.DataFrame, col: str, by_st: pd.Series, by_st_hour: pd.Series | None) -> pd.Series:
    b = df.station_id.map(by_st)
    if by_st_hour is not None:
        idx = pd.MultiIndex.from_arrays([df.station_id, df.hour])
        b = pd.Series(by_st_hour.reindex(idx).to_numpy(), index=df.index).fillna(b)
    return df[col] - b.fillna(0.0)


def rolling_ifs(df: pd.DataFrame, test: pd.DataFrame, window_days: int) -> pd.Series:
    out = pd.Series(index=test.index, dtype=float)
    for issue, rows in test.groupby("issue_at"):
        past = df[(df.valid_for < issue) & (df.valid_for >= issue - pd.Timedelta(days=window_days))]
        by_st, by_st_hour = bias_table(past, "t_nwp")
        out.loc[rows.index] = apply_bias(rows, "t_nwp", by_st, by_st_hour)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="MOS minimo per stazione × ora sul backtest IFS")
    ap.add_argument("--input", default=str(DEFAULT_IN))
    ap.add_argument("--test-start", default="2026-09-01", help="Primo valid_for del periodo di test (UTC)")
    ap.add_argument("--window-days", type=int, default=30, help="Finestra della variante mobile E")
    args = ap.parse_args()

    df = pd.read_parquet(args.input).dropna(subset=["t_obs", "t_nwp", "t_model"])
    df["hour"] = df.valid_for.dt.hour
    cut = pd.Timestamp(args.test_start, tz="UTC")
    train, test = df[df.valid_for < cut], df[df.valid_for >= cut].copy()
    # Il train non deve vedere niente del test: esclude anche le emissioni successive al taglio.
    train = train[train.issue_at < cut]

    st_ifs, sth_ifs = bias_table(train, "t_nwp")
    _, sth_model = bias_table(train, "t_model")
    st_model = (train.t_model - train.t_obs).groupby(train.station_id).mean()

    preds = {
        "A ifs": test.t_nwp,
        "B model": test.t_model,
        "C ifs_st": apply_bias(test, "t_nwp", st_ifs, None),
        "D ifs_st_hour": apply_bias(test, "t_nwp", st_ifs, sth_ifs),
        f"E ifs_rolling{args.window_days}d": rolling_ifs(df, test, args.window_days),
        "F model_st_hour": apply_bias(test, "t_model", st_model, sth_model),
    }
    err = pd.DataFrame({k: v - test.t_obs for k, v in preds.items()})
    err["lead"], err["station_id"], err["name"], err["hour"] = test.lead, test.station_id, test.name, test.hour

    names = list(preds)
    print(f"Train: valid_for < {cut:%Y-%m-%d} ({len(train):,} righe) | Test: da {cut:%Y-%m-%d} "
          f"({len(test):,} righe, {test.issue_at.nunique()} emissioni)")

    print("\n== MAE (°C) per lead, periodo di test ==")
    mae_lead = err.groupby("lead")[names].apply(lambda g: g.abs().mean())
    show = [l for l in (1, 3, 6, 12, 24, 36, 48) if l in mae_lead.index]
    print(mae_lead.loc[show].round(2).to_string())
    print("\nTutti i lead:")
    print(err[names].abs().mean().round(2).to_string())

    print("\n== Bias medio per ora UTC (test) ==")
    print(err.groupby("hour")[names].mean().iloc[::3].round(2).to_string())

    print("\n== Per stazione (tutti i lead): MAE e vincitore ==")
    mae_st = err.groupby(["station_id", "name"])[names].apply(lambda g: g.abs().mean())
    mae_st["best"] = mae_st[names].idxmin(axis=1)
    print(mae_st.round(2).to_string())
    print("\nStazioni vinte da ciascuna variante:", mae_st.best.value_counts().to_dict())

    out = Path(args.input).with_name("experiment_bias_hour_by_lead.csv")
    mae_lead.round(3).to_csv(out)
    mae_st.round(3).to_csv(out.with_name("experiment_bias_hour_by_station.csv"))
    print(f"\nCSV in {out.parent}/")


if __name__ == "__main__":
    main()
