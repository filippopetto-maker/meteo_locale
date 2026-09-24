"""
backtest_ifs_lead.py — Backtest del modello multi-lead alimentato da ECMWF IFS

Per ogni run IFS archiviato (Single Runs API) simula un'emissione al momento
in cui quel run era davvero disponibile (init + avail_lag ore), rigira il
modello di produzione (inference.predict_from_df) sui lead 1…N e confronta con
le osservazioni. Serie confrontate per ogni (stazione, emissione, lead):

  t_model  modello (LGBM + RF + ARSIAL) alimentato dal run IFS
  t_nwp    IFS grezzo a valid_for
  t_prod1  previsione di produzione lead 1 per lo stesso valid_for (input =
           blend default Open-Meteo) — riferimento, solo dove esiste
  t_obs    osservazione più vicina entro 60 min, qc_flag < 2 (stesso
           abbinamento della vista forecast_vs_observed, più il filtro QC)

Input del modello per un run R emesso a T = R + avail_lag:
  righe < R   → Historical Forecast API ecmwf_ifs (serie cucita, run già
                disponibili a T)
  righe ≥ R   → il run R, come lo vedeva la produzione a T
Le risposte API sono in cache su disco: rilanciare costa zero chiamate.

Utilizzo:
    python3 scripts/backtest_ifs_lead.py --start 2026-09-01 --end 2026-09-03
    python3 scripts/backtest_ifs_lead.py --start 2026-06-25 --end 2026-09-21 --runs 0,12
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "model"))

import db  # noqa: E402
import inference as inf  # noqa: E402
from historical import ERA5_RENAME, ERA5_VARIABLES  # noqa: E402

logger = logging.getLogger("backtest")

SINGLE_RUNS_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
HISTORICAL_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
NWP_MODEL = "ecmwf_ifs"
WARMUP_H = 48  # come past_days=2 in produzione


# ─────────────────────────────────────────────────────────────────────────────
# Open-Meteo con cache su disco
# ─────────────────────────────────────────────────────────────────────────────

class OpenMeteo:
    def __init__(self, cache_dir: Path, pause: float):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.pause = pause
        self.calls = 0

    def get(self, url: str, params: dict) -> list[dict]:
        key = hashlib.sha1(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text())

        for attempt in range(6):
            wait = 60 * (attempt + 1)
            try:
                r = requests.get(url, params=params, timeout=90)
            except requests.RequestException as exc:
                logger.warning(f"Errore di rete ({exc}), riprovo tra {wait}s")
                time.sleep(wait)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                logger.warning(f"HTTP {r.status_code} ({r.text[:120]}), riprovo tra {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            try:
                data = r.json()
            except ValueError:
                logger.warning(f"Risposta non JSON ({len(r.content)} byte), riprovo tra {wait}s")
                time.sleep(wait)
                continue
            break
        else:
            raise RuntimeError(f"Open-Meteo non risponde dopo 6 tentativi: {url}")

        data = data if isinstance(data, list) else [data]
        path.write_text(json.dumps(data))
        self.calls += 1
        time.sleep(self.pause)
        return data


def _to_frames(payload: list[dict]) -> list[pd.DataFrame]:
    frames = []
    for loc in payload:
        h = loc["hourly"]
        df = pd.DataFrame({"recorded_at": pd.to_datetime(h["time"])})
        for var in ERA5_VARIABLES:
            df[var] = h.get(var)
        frames.append(df.rename(columns=ERA5_RENAME))
    return frames


def _coords(stations: list[dict]) -> dict:
    return {
        "latitude": ",".join(str(s["lat"]) for s in stations),
        "longitude": ",".join(str(s["lon"]) for s in stations),
    }


def fetch_historical(api: OpenMeteo, stations, start: datetime, end: datetime) -> dict[int, pd.DataFrame]:
    params = {
        **_coords(stations),
        "hourly": ",".join(ERA5_VARIABLES),
        "models": NWP_MODEL,
        "start_date": start.date().isoformat(),
        "end_date": end.date().isoformat(),
        "timezone": "UTC",
    }
    frames = _to_frames(api.get(HISTORICAL_URL, params))
    return {s["id"]: f for s, f in zip(stations, frames)}


def fetch_run(api: OpenMeteo, stations, run_init: datetime, hours: int) -> dict[int, pd.DataFrame]:
    params = {
        **_coords(stations),
        "hourly": ",".join(ERA5_VARIABLES),
        "models": NWP_MODEL,
        "run": run_init.strftime("%Y-%m-%dT%H:%M"),
        "forecast_hours": hours,
        "timezone": "UTC",
    }
    frames = _to_frames(api.get(SINGLE_RUNS_URL, params))
    return {s["id"]: f for s, f in zip(stations, frames)}


def build_input(hist: pd.DataFrame, run: pd.DataFrame, run_init: datetime) -> pd.DataFrame:
    """Serie vista dalla produzione all'emissione: storico cucito prima di R, run R da R in poi."""
    r0 = run_init.replace(tzinfo=None)
    past = hist[(hist.recorded_at < r0) & (hist.recorded_at >= r0 - timedelta(hours=WARMUP_H))]
    df = pd.concat([past, run[run.recorded_at >= r0]], ignore_index=True)
    # Le variabili accumulate (precipitazione, radiazione) sono null allo step 0 del run.
    at_r0 = hist[hist.recorded_at == r0]
    if not at_r0.empty:
        mask = df.recorded_at == r0
        for col in df.columns:
            if col != "recorded_at" and df.loc[mask, col].isna().any():
                df.loc[mask, col] = at_r0[col].iloc[0]
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Supabase: osservazioni e previsioni di produzione lead 1
# ─────────────────────────────────────────────────────────────────────────────

def _paged(query_fn, page: int = 1000) -> list[dict]:
    out, offset = [], 0
    while True:
        rows = query_fn().range(offset, offset + page - 1).execute().data
        out.extend(rows)
        if len(rows) < page:
            return out
        offset += page


def fetch_observations(sid: int, start: datetime, end: datetime) -> pd.DataFrame:
    c = db.get_client()
    rows = _paged(lambda: c.table("observations")
                  .select("recorded_at, temperature")
                  .eq("station_id", sid)
                  .lt("qc_flag", 2)
                  .gte("recorded_at", start.isoformat())
                  .lte("recorded_at", end.isoformat())
                  .order("recorded_at"))
    df = pd.DataFrame(rows, columns=["recorded_at", "temperature"]).dropna()
    df["recorded_at"] = pd.to_datetime(df.recorded_at, utc=True, format="ISO8601")
    return df.rename(columns={"recorded_at": "obs_at", "temperature": "t_obs"}).sort_values("obs_at")


def fetch_prod_lead1(sid: int, start: datetime, end: datetime) -> pd.DataFrame:
    c = db.get_client()
    rows = _paged(lambda: c.table("forecasts")
                  .select("valid_for, temperature")
                  .eq("station_id", sid)
                  .eq("lead_hours", 1)
                  .gte("valid_for", start.isoformat())
                  .lte("valid_for", end.isoformat())
                  .order("valid_for"))
    df = pd.DataFrame(rows, columns=["valid_for", "temperature"])
    df["valid_for"] = pd.to_datetime(df.valid_for, utc=True, format="ISO8601")
    return df.rename(columns={"temperature": "t_prod1"}).drop_duplicates("valid_for", keep="last")


def attach_truth(pred: pd.DataFrame, obs: pd.DataFrame, prod: pd.DataFrame) -> pd.DataFrame:
    pred = pred.sort_values("valid_for")
    if not obs.empty:
        pred = pd.merge_asof(pred, obs, left_on="valid_for", right_on="obs_at",
                             direction="nearest", tolerance=pd.Timedelta(minutes=59, seconds=59))
    else:
        pred = pred.assign(obs_at=pd.NaT, t_obs=float("nan"))
    return pred.merge(prod, on="valid_for", how="left")


# ─────────────────────────────────────────────────────────────────────────────
# Sintesi
# ─────────────────────────────────────────────────────────────────────────────

def summarize(res: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    ok = res.dropna(subset=["t_obs", "t_model", "t_nwp"]).copy()
    ok["e_model"] = ok.t_model - ok.t_obs
    ok["e_nwp"] = ok.t_nwp - ok.t_obs
    ok["e_prod1"] = ok.t_prod1 - ok.t_obs

    g = ok.groupby("lead")
    by_lead = pd.DataFrame({
        "n": g.size(),
        "mae_model": g.e_model.apply(lambda s: s.abs().mean()),
        "mae_nwp": g.e_nwp.apply(lambda s: s.abs().mean()),
        "bias_model": g.e_model.mean(),
        "bias_nwp": g.e_nwp.mean(),
    })
    by_lead["skill_%"] = 100 * (1 - by_lead.mae_model / by_lead.mae_nwp)

    # Riferimento di produzione (lead 1, input default) sulle sole righe dove esiste
    both = ok.dropna(subset=["t_prod1"])
    gb = both.groupby("lead")
    by_lead["n_prod1"] = gb.size()
    by_lead["mae_model_on_prod1_rows"] = gb.e_model.apply(lambda s: s.abs().mean())
    by_lead["mae_prod1"] = gb.e_prod1.apply(lambda s: s.abs().mean())

    gs = ok[ok.lead.isin([1, 24, 48])].groupby(["station_id", "name", "lead"])
    by_station = pd.DataFrame({
        "n": gs.size(),
        "mae_model": gs.e_model.apply(lambda s: s.abs().mean()),
        "mae_nwp": gs.e_nwp.apply(lambda s: s.abs().mean()),
    })
    by_station["skill_%"] = 100 * (1 - by_station.mae_model / by_station.mae_nwp)
    by_station = by_station.unstack("lead")
    by_station.columns = [f"{m}_L{l}" for m, l in by_station.columns]
    return by_lead, by_station


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest modello multi-lead su run ECMWF IFS archiviati")
    ap.add_argument("--start", required=True, help="Primo giorno di run (YYYY-MM-DD)")
    ap.add_argument("--end", required=True, help="Ultimo giorno di run (YYYY-MM-DD)")
    ap.add_argument("--runs", default="0,12", help="Ore di init dei run (default 0,12)")
    ap.add_argument("--avail-lag", type=int, default=9,
                    help="Ore tra init e disponibilità del run (default 9: il run 00Z esce ~08:20Z)")
    ap.add_argument("--max-lead", type=int, default=48)
    ap.add_argument("--stations", default=None, help="Id separati da virgola (default: tutte le attive)")
    ap.add_argument("--pause", type=float, default=20.0,
                    help="Secondi di pausa dopo ogni chiamata non in cache (limite Open-Meteo free tier)")
    ap.add_argument("--out-dir", default=str(ROOT / "logs" / "backtest"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("inference", "httpx", "features"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    out_dir = Path(args.out_dir)
    api = OpenMeteo(out_dir / "cache", args.pause)

    stations = db.get_active_stations()
    if args.stations:
        wanted = {int(x) for x in args.stations.split(",")}
        stations = [s for s in stations if s["id"] in wanted]
    stations = sorted(stations, key=lambda s: s["id"])

    d0 = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    d1 = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    run_hours = [int(h) for h in args.runs.split(",")]
    runs = [d0 + timedelta(days=d, hours=h)
            for d in range((d1 - d0).days + 1) for h in run_hours]
    leads = list(range(1, args.max_lead + 1))
    # Ore 0 … avail_lag + max_lead del run: serve anche la riga a valid_for del lead massimo (t_nwp).
    run_len = args.avail_lag + args.max_lead + 1
    last_valid = runs[-1] + timedelta(hours=run_len - 1)

    logger.info(f"{len(stations)} stazioni × {len(runs)} run ({runs[0]:%Y-%m-%d %HZ} → {runs[-1]:%Y-%m-%d %HZ}), "
                f"emissione = init + {args.avail_lag}h, lead 1…{args.max_lead}")

    hist = fetch_historical(api, stations, runs[0] - timedelta(hours=WARMUP_H), last_valid)

    preds = []
    for i, r in enumerate(runs, 1):
        issue = r + timedelta(hours=args.avail_lag)
        run_frames = fetch_run(api, stations, r, run_len)
        for st in stations:
            df = build_input(hist[st["id"]], run_frames[st["id"]], r)
            for p in inf.predict_from_df(df, st, leads, now_utc=issue):
                preds.append({
                    "station_id": st["id"], "name": st.get("name", ""),
                    "run_init": r, "issue_at": issue, "lead": p["lead_hours"],
                    "valid_for": p["valid_for"], "t_model": p["temperature"],
                    "t_nwp": p["nwp_temperature"],
                })
        if i % 10 == 0 or i == len(runs):
            logger.info(f"run {i}/{len(runs)} ({r:%Y-%m-%d %HZ}) — chiamate API nuove: {api.calls}")

    pred = pd.DataFrame(preds)
    pred["valid_for"] = pd.to_datetime(pred.valid_for, utc=True)

    logger.info("Osservazioni e previsioni di produzione da Supabase…")
    parts = []
    for st in stations:
        sub = pred[pred.station_id == st["id"]]
        if sub.empty:
            continue
        obs = fetch_observations(st["id"], runs[0], last_valid + timedelta(hours=1))
        prod = fetch_prod_lead1(st["id"], runs[0], last_valid)
        parts.append(attach_truth(sub, obs, prod))
    res = pd.concat(parts, ignore_index=True)

    tag = f"{args.start}_{args.end}_r{args.runs.replace(',', '-')}_lag{args.avail_lag}"
    res.to_parquet(out_dir / f"backtest_{tag}.parquet", index=False)
    by_lead, by_station = summarize(res)
    by_lead.round(3).to_csv(out_dir / f"summary_by_lead_{tag}.csv")
    by_station.round(3).to_csv(out_dir / f"summary_by_station_{tag}.csv")

    n_obs = res.t_obs.notna().sum()
    print(f"\nRighe: {len(res)}  |  con osservazione: {n_obs} ({100 * n_obs / len(res):.0f}%)  |  "
          f"chiamate API nuove: {api.calls}")
    print("\n== MAE per lead (°C) — modello su IFS vs IFS grezzo ==")
    show = [1, 3, 6, 12, 24, 36, 48]
    print(by_lead.loc[[l for l in show if l in by_lead.index]].round(2).to_string())
    print("\n== Per stazione, lead 1 / 24 / 48 ==")
    print(by_station.round(2).to_string())
    print(f"\nFile in {out_dir}/ (tag {tag})")


if __name__ == "__main__":
    main()
