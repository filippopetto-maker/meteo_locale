"""
netatmo_wind_backfill.py — Storico orario del vento Netatmo per le stazioni progetto

I moduli anemometro (NAModule2, id 06:00:00:…) sono moduli separati dal termometro e
non rientrano nei device di netatmo_backfill.py (che scarica solo temperatura e
umidità). Qui si cercano i moduli vento attorno alle stazioni e se ne scarica lo
storico orario con getmeasure (WindStrength, WindAngle, GustStrength, GustAngle).

Passi:
  discover   getpublicdata (required_data=wind) su due riquadri per stazione →
             logs/netatmo_backfill/wind_devices.json con distanza e conteggio per stazione
  select     per ogni stazione i moduli più vicini: fino a MAX_PER_STATION entro 5 km,
             almeno MIN_PER_STATION entro 15 km dove sono pochi → wind_selected.json
  download   getmeasure orario paginato, un parquet per modulo (riprende da dove era)
  build      per stazione e ora: mediana della velocità, direzione (media vettoriale),
             raffica, numero di moduli → data/netatmo_wind_hourly_target.parquet

Utilizzo:
    python3 scripts/netatmo_wind_backfill.py discover
    python3 scripts/netatmo_wind_backfill.py select
    python3 scripts/netatmo_wind_backfill.py download --since 2024-03-14
    python3 scripts/netatmo_wind_backfill.py build
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import mainMETEO as mm  # noqa: E402
import netatmo_backfill as nb  # noqa: E402

logger = logging.getLogger("netatmo_wind")

WIND_DEVICES_FILE = nb.WORK_DIR / "wind_devices.json"
# mezze ampiezze (gradi) dei due riquadri per stazione: ~5 km e ~11 km di raggio
BOXES = ((0.05, 0.065), (0.10, 0.13))
RADII_KM = (5, 10, 15)
SELECTED_FILE = nb.WORK_DIR / "wind_selected.json"
RAW_DIR = nb.WORK_DIR / "wind_raw"
TARGET_FILE = ROOT / "data" / "netatmo_wind_hourly_target.parquet"
MAX_PER_STATION = 8
MIN_PER_STATION = 3
MEASURES = "WindStrength,WindAngle,GustStrength,GustAngle"


def discover(api: nb.Netatmo) -> None:
    project = db.get_active_stations()
    raw: dict[str, dict] = {}
    for ps in project:
        for dlat, dlon in BOXES:
            box = {"lat_sw": ps["lat"] - dlat, "lat_ne": ps["lat"] + dlat,
                   "lon_sw": ps["lon"] - dlon, "lon_ne": ps["lon"] + dlon,
                   "required_data": "wind", "filter": "true"}
            for dev in api.get(mm.NETATMO_PUBDATA_URL, box).get("body", []):
                raw.setdefault(dev["_id"], dev)
        logger.info(f"st.{ps['id']:>2} {ps.get('name', ''):<26} moduli unici finora: {sum(len(_wind(d)) for d in raw.values())}")

    modules = []
    for dev_id, dev in raw.items():
        try:
            lon, lat = dev["place"]["location"]
            alt = dev["place"].get("altitude")
        except (KeyError, ValueError, TypeError):
            continue
        for mid in _wind(dev):
            modules.append({"device_id": dev_id, "module_id": mid, "lat": lat, "lon": lon, "alt": alt})

    out = {"modules": modules, "stations": {}}
    for ps in project:
        near = []
        for m in modules:
            km = mm._haversine_km_nt(ps["lat"], ps["lon"], m["lat"], m["lon"])
            if km <= max(RADII_KM):
                near.append({**m, "dist_km": round(km, 2)})
        near.sort(key=lambda m: m["dist_km"])
        out["stations"][str(ps["id"])] = {
            "name": ps.get("name", ""),
            "counts": {f"r{r}": sum(1 for m in near if m["dist_km"] <= r) for r in RADII_KM},
            "modules": near,
        }
    WIND_DEVICES_FILE.write_text(json.dumps(out, indent=1))
    logger.info(f"{len(modules)} moduli vento unici, scritto {WIND_DEVICES_FILE}")
    for sid, s in out["stations"].items():
        logger.info(f"st.{sid:>2} {s['name']:<26} " + "  ".join(f"≤{r}km {s['counts'][f'r{r}']:>3}" for r in RADII_KM))


def select() -> None:
    disc = json.loads(WIND_DEVICES_FILE.read_text())
    out, unique = {}, set()
    for sid, st in disc["stations"].items():
        mods = [m for m in st["modules"] if m["dist_km"] <= 5][:MAX_PER_STATION]
        if len(mods) < MIN_PER_STATION:
            mods = [m for m in st["modules"] if m["dist_km"] <= 15][:MIN_PER_STATION]
        out[sid] = {"name": st["name"], "modules": mods}
        unique |= {(m["device_id"], m["module_id"]) for m in mods}
        logger.info(f"st.{sid:>2} {st['name']:<26} {len(mods):>2} moduli"
                    + (f" (più lontano {mods[-1]['dist_km']} km)" if mods else ""))
    SELECTED_FILE.write_text(json.dumps(out, indent=1))
    logger.info(f"{len(unique)} moduli unici (~{len(unique) * 23} chiamate), scritto {SELECTED_FILE}")


def _unique_modules() -> list[dict]:
    sel = json.loads(SELECTED_FILE.read_text())
    seen = {}
    for st in sel.values():
        for m in st["modules"]:
            seen.setdefault(m["module_id"], m)
    return list(seen.values())


def _measure(api: nb.Netatmo, device_id: str, module_id: str, date_begin: int, date_end: int) -> list[list]:
    params = {"device_id": device_id, "module_id": module_id, "scale": "1hour", "type": MEASURES,
              "date_begin": date_begin, "date_end": date_end, "limit": 1024, "optimize": "true",
              "real_time": "false"}
    out = []
    for block in api.get(nb.GETMEASURE_URL, params).get("body") or []:
        t0, step = int(block["beg_time"]), int(block.get("step_time") or 0)
        for i, v in enumerate(block.get("value", [])):
            v = list(v) + [None] * (4 - len(v))
            out.append([t0 + i * step, *v[:4]])
    return out


def _save(path: Path, old: pd.DataFrame, new: list) -> pd.DataFrame:
    if not new:
        return old
    df = pd.concat([old, pd.DataFrame(new, columns=["ts", "ws", "wa", "gs", "ga"])], ignore_index=True)
    df = df.drop_duplicates("ts", keep="last").sort_values("ts")
    df.to_parquet(path, index=False)
    return df


def download(api: nb.Netatmo, since: str, until: str | None) -> None:
    mods = _unique_modules()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    # stessa convenzione di netatmo_backfill.download: finestre orarie ancorate a HH:30,
    # il valore è marcato a HH:00 (media HH−0:30 → HH+0:30).
    since_ts = int(pd.Timestamp(since, tz="UTC").floor("h").timestamp()) - nb.HALF_STEP
    end_ts = int(pd.Timestamp(until, tz="UTC").timestamp()) if until else int(time.time())
    failed = []
    for i, m in enumerate(mods, 1):
        path = RAW_DIR / f"{m['module_id'].replace(':', '')}.parquet"
        old = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=["ts", "ws", "wa", "gs", "ga"])
        in_range = old.ts[(old.ts >= since_ts) & (old.ts < end_ts)] if len(old) else old.ts
        if len(in_range) and in_range.max() >= end_ts - 3 * 3600:
            continue
        cursor = int(in_range.max()) + nb.HALF_STEP if len(in_range) else since_ts
        new = []
        try:
            while cursor < end_ts:
                vals = _measure(api, m["device_id"], m["module_id"], cursor, end_ts)
                if not vals:
                    break
                new.extend(vals)
                nxt = vals[-1][0] + nb.HALF_STEP
                if nxt <= cursor:
                    break
                cursor = nxt
                if len(new) >= 8000:
                    old = _save(path, old, new)
                    new = []
        except RuntimeError as exc:
            logger.error(f"modulo {m['module_id']} saltato: {exc}")
            failed.append(m["module_id"])
        _save(path, old, new)
        logger.info(f"modulo {i}/{len(mods)} {m['module_id']} — chiamate totali {api.calls}")
    if failed:
        logger.warning(f"{len(failed)} moduli non scaricati (rilanciare 'download'): {failed}")


def build() -> None:
    sel = json.loads(SELECTED_FILE.read_text())
    parts = []
    for sid, st in sel.items():
        frames = []
        for m in st["modules"]:
            path = RAW_DIR / f"{m['module_id'].replace(':', '')}.parquet"
            if path.exists():
                frames.append(pd.read_parquet(path).assign(module_id=m["module_id"]))
        if not frames:
            continue
        df = pd.concat(frames, ignore_index=True).dropna(subset=["ws"])
        df = df[(df.ws >= 0) & (df.ws < 200)]
        t = pd.to_datetime(df.ts, unit="s", utc=True)
        df = df[(t - t.dt.round("h")).abs() <= pd.Timedelta(minutes=5)].copy()
        df["recorded_at"] = pd.to_datetime(df.ts, unit="s", utc=True).dt.round("h")
        rad = np.deg2rad(df.wa.astype(float))
        df["u"], df["v"] = df.ws * np.sin(rad), df.ws * np.cos(rad)
        g = df.groupby("recorded_at")
        out = pd.DataFrame({"wind_speed": g.ws.median(), "gust": g.gs.median(), "n_modules": g.module_id.nunique(),
                            "u": g.u.mean(), "v": g.v.mean()})
        out["wind_direction"] = (np.rad2deg(np.arctan2(out.u, out.v)) % 360).round(0)
        out = out.drop(columns=["u", "v"]).reset_index()
        out["station_id"] = int(sid)
        parts.append(out)
        logger.info(f"st.{sid:>2} {st['name']:<26} {len(out):>6} ore ({out.recorded_at.min():%Y-%m-%d} → "
                    f"{out.recorded_at.max():%Y-%m-%d}), moduli per ora mediana {int(out.n_modules.median())}")
    res = pd.concat(parts, ignore_index=True)
    res.to_parquet(TARGET_FILE, index=False)
    logger.info(f"Salvato {TARGET_FILE}: {len(res):,} righe, {res.station_id.nunique()} stazioni")


def _wind(dev: dict) -> list[str]:
    return [mid for mid, m in dev.get("measures", {}).items() if "wind_strength" in m]


def main() -> None:
    ap = argparse.ArgumentParser(description="Storico vento Netatmo")
    ap.add_argument("step", choices=["discover", "select", "download", "build"])
    ap.add_argument("--since", default="2024-03-14")
    ap.add_argument("--until", default=None)
    ap.add_argument("--pause", type=float, default=7.5)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    api = nb.Netatmo(args.pause)
    if args.step == "discover":
        discover(api)
    elif args.step == "select":
        select()
    elif args.step == "download":
        download(api, args.since, args.until)
    else:
        build()
    logger.info(f"Chiamate Netatmo: {api.calls}")


if __name__ == "__main__":
    main()
