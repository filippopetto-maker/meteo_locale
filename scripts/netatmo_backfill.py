"""
netatmo_backfill.py — Storico orario del target Netatmo per le stazioni progetto

Ricostruisce lo stesso target che mainMETEO.fetch_netatmo() scrive dal vivo:
mediana dei device Netatmo pubblici entro NETATMO_RADIUS_KM dalla stazione,
con lo stesso minimo di device per stazione.

Passi (in ordine):
  discover   getpublicdata sui LAZIO_BBOXES + riquadri per stazione → device candidati
  select     tra i candidati, gli N device che la produzione usa davvero (confronto con
             temps_raw delle osservazioni live) → devices_selected.json
  probe      profondità dello storico orario per device + convenzione dei timestamp
  download   getmeasure orario paginato, un parquet per device (riprende da dove era)
  build      mediana oraria per stazione → data/netatmo_hourly_target.parquet
  validate   confronto con le osservazioni live Netatmo nel periodo in comune

Limiti Netatmo: ~500 chiamate/ora per utente → --pause 7.5 s di default.

Utilizzo:
    python3 scripts/netatmo_backfill.py discover
    python3 scripts/netatmo_backfill.py probe
    python3 scripts/netatmo_backfill.py download --since 2026-06-01
    python3 scripts/netatmo_backfill.py select --since 2026-06-10 --until 2026-08-01
    python3 scripts/netatmo_backfill.py build --selected
    python3 scripts/netatmo_backfill.py validate --selected --since 2026-06-10
    python3 scripts/netatmo_backfill.py download --selected --since 2024-03-14 --until 2026-06-01

--selected fa usare a download/build/validate devices_selected.json al posto di
devices.json (tutti i candidati entro 5 km).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402  (carica anche .env)
import mainMETEO as mm  # noqa: E402

logger = logging.getLogger("netatmo_backfill")

GETMEASURE_URL = "https://api.netatmo.com/api/getmeasure"
WORK_DIR = ROOT / "logs" / "netatmo_backfill"
DEVICES_FILE = WORK_DIR / "devices.json"
SELECTED_FILE = WORK_DIR / "devices_selected.json"
RAW_DIR = WORK_DIR / "raw"
TARGET_FILE = ROOT / "data" / "netatmo_hourly_target.parquet"
HALF_STEP = 1800  # metà della finestra oraria di getmeasure


# ─────────────────────────────────────────────────────────────────────────────
# Client Netatmo con token rinnovato e pausa tra le chiamate
# ─────────────────────────────────────────────────────────────────────────────

class Netatmo:
    def __init__(self, pause: float):
        self.pause = pause
        self.calls = 0
        self.max_5xx_retries = 3
        self._token = None
        self._token_at = 0.0

    def _auth(self) -> dict:
        if self._token is None or time.time() - self._token_at > 2 * 3600:
            self._token = mm._refresh_netatmo_token()
            self._token_at = time.time()
        return {"Authorization": f"Bearer {self._token}"}

    def get(self, url: str, params: dict) -> dict:
        for attempt in range(8):
            try:
                r = requests.get(url, headers=self._auth(), params=params, timeout=60)
            except requests.RequestException as exc:
                wait = 60 * (attempt + 1)
                logger.warning(f"Errore di rete ({exc}), riprovo tra {wait}s")
                time.sleep(wait)
                continue
            self.calls += 1
            time.sleep(self.pause)
            if r.ok:
                return r.json()
            try:
                code = r.json().get("error", {}).get("code")
            except ValueError:
                code = None
            if code in (2, 3):  # token scaduto o non valido
                self._token = None
                continue
            if r.status_code == 429 or code == 26:  # limite di utilizzo
                logger.warning("Limite Netatmo raggiunto, pausa di 15 minuti")
                time.sleep(900)
                continue
            if r.status_code >= 500 and attempt < self.max_5xx_retries:
                wait = 30 * (attempt + 1)
                logger.warning(f"Netatmo HTTP {r.status_code} code={code}, riprovo tra {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError(f"Netatmo HTTP {r.status_code} code={code}: {r.text[:200]}")
        raise RuntimeError(f"Netatmo non risponde dopo 8 tentativi: {url}")

    def measure(self, device_id: str, module_id: str, scale: str, date_begin: int,
                date_end: int | None = None, real_time: bool = False) -> list[tuple[int, float, float]]:
        params = {
            "device_id": device_id, "module_id": module_id, "scale": scale,
            "type": "temperature,humidity", "date_begin": date_begin,
            "limit": 1024, "optimize": "true", "real_time": str(real_time).lower(),
        }
        if date_end is not None:
            params["date_end"] = date_end
        body = self.get(GETMEASURE_URL, params).get("body") or []
        out = []
        for block in body:
            t0, step = int(block["beg_time"]), int(block.get("step_time") or 0)
            for i, vals in enumerate(block.get("value", [])):
                t = vals[0] if vals else None
                h = vals[1] if len(vals) > 1 else None
                out.append((t0 + i * step, t, h))
        return out


# ─────────────────────────────────────────────────────────────────────────────
# discover
# ─────────────────────────────────────────────────────────────────────────────

def discover(api: Netatmo) -> None:
    project = db.get_active_stations()
    # getpublicdata su un'area grande restituisce un sottoinsieme che cambia da chiamata a
    # chiamata: si uniscono le 5 aree della produzione e un riquadro di ~5 km per stazione.
    boxes = list(mm.LAZIO_BBOXES) + [
        {"lat_sw": ps["lat"] - 0.05, "lat_ne": ps["lat"] + 0.05,
         "lon_sw": ps["lon"] - 0.065, "lon_ne": ps["lon"] + 0.065}
        for ps in project
    ]
    raw: dict[str, dict] = {}
    for i, bbox in enumerate(boxes):
        body = api.get(mm.NETATMO_PUBDATA_URL,
                       {**bbox, "required_data": "temperature", "filter": "true"}).get("body", [])
        for s in body:
            if s.get("_id"):
                raw[s["_id"]] = s
        if i == len(mm.LAZIO_BBOXES) - 1:
            logger.info(f"getpublicdata aree grandi: {len(raw)} device unici")
    logger.info(f"getpublicdata + riquadri per stazione: {len(raw)} device unici")

    devices = []
    for dev_id, s in raw.items():
        try:
            lon, lat = s["place"]["location"]
        except (KeyError, ValueError, TypeError):
            continue
        module_id = next((mid for mid, m in s.get("measures", {}).items()
                          if "temperature" in m.get("type", [])), None)
        if module_id:
            devices.append({"device_id": dev_id, "module_id": module_id, "lat": lat, "lon": lon})

    out = {}
    for ps in project:
        near = []
        for d in devices:
            km = mm._haversine_km_nt(ps["lat"], ps["lon"], d["lat"], d["lon"])
            if km <= mm.NETATMO_RADIUS_KM:
                near.append({**d, "dist_km": round(km, 2)})
        min_cluster = 1 if (ps["id"] >= 39 or ps.get("microclima") in
                            ("quota", "alta_quota", "colline_interne")) else mm.NETATMO_MIN_CLUSTER
        out[str(ps["id"])] = {"name": ps.get("name", ""), "min_cluster": min_cluster,
                              "devices": sorted(near, key=lambda d: d["dist_km"])}
        logger.info(f"st.{ps['id']:>2} {ps.get('name', ''):<26} {len(near):>2} device (min {min_cluster})")

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    DEVICES_FILE.write_text(json.dumps(out, indent=1))
    n = len({d["device_id"] for st in out.values() for d in st["devices"]})
    logger.info(f"Salvato {DEVICES_FILE} — {n} device distinti, {api.calls} chiamate")


def _load_devices(selected: bool = False) -> dict:
    path = SELECTED_FILE if selected else DEVICES_FILE
    if not path.exists():
        sys.exit(f"Manca {path}: esegui prima '{'select' if selected else 'discover'}'")
    return json.loads(path.read_text())


# ─────────────────────────────────────────────────────────────────────────────
# select
# ─────────────────────────────────────────────────────────────────────────────

def select(since: str, until: str | None, tol: float = 0.25, min_hours: int = 200) -> None:
    """Sceglie per ogni stazione gli N device usati dalla produzione.

    getpublicdata su un'area grande restituisce solo una parte dei device: la mappa fa la
    mediana di quel sottoinsieme, non di tutti i device entro 5 km. Ogni osservazione live
    salva in raw_source i valori dei device usati (temps_raw) e quanti erano (n_stations).
    Per ogni candidato si conta in quante ore il suo valore orario cade entro `tol` °C da
    uno dei temps_raw; si tengono gli N più frequenti, N = mediana di n_stations.
    """
    stations = _load_devices()
    start = pd.Timestamp(since, tz="UTC")
    end = pd.Timestamp(until, tz="UTC") if until else pd.Timestamp.now(tz="UTC")
    client = db.get_client()

    out, rows = {}, []
    for sid, st in stations.items():
        obs = _paged_obs(client, int(sid), start, end, with_raw=True)
        off = (obs.recorded_at - obs.recorded_at.dt.round("h")).abs()
        obs = (obs[off <= pd.Timedelta(minutes=15)]
               .assign(hour=lambda d: d.recorded_at.dt.round("h"))
               .drop_duplicates("hour").set_index("hour"))
        if obs.empty:
            logger.warning(f"st.{sid}: nessuna osservazione live nel periodo, tengo tutti i candidati")
            out[sid] = st
            continue
        n_prod = int(round(obs.n_stations.median()))
        scores = {}
        for d in st["devices"]:
            s = _hourly_device(d["device_id"])
            if s is None:
                continue
            j = obs.join(s.rename("v"), how="inner")
            if len(j) < min_hours:
                continue
            hit = [any(abs(v - r) <= tol for r in raw) for v, raw in zip(j.v, j.temps_raw)]
            scores[d["device_id"]] = float(np.mean(hit))
        keep = sorted(scores, key=scores.get, reverse=True)[:n_prod]
        devs = [{**d, "match": round(scores[d["device_id"]], 3)} for d in st["devices"] if d["device_id"] in keep]
        out[sid] = {**st, "n_prod": n_prod, "devices": devs}
        rows.append((sid, st["name"], n_prod, len(st["devices"]), len(devs),
                     min((d["match"] for d in devs), default=np.nan)))
        if len(devs) < n_prod:
            logger.warning(f"st.{sid} {st['name']}: la produzione usa ~{n_prod} device, "
                           f"tra i candidati ne trovo {len(devs)}")

    SELECTED_FILE.write_text(json.dumps(out, indent=1))
    print(f"\n== Device scelti ({start:%Y-%m-%d} → {end:%Y-%m-%d}, tolleranza {tol} °C) ==")
    print(pd.DataFrame(rows, columns=["station", "name", "n_prod", "candidati", "scelti", "match_min"])
          .round(2).to_string(index=False))
    n = len({d["device_id"] for st in out.values() for d in st["devices"]})
    logger.info(f"Salvato {SELECTED_FILE} — {n} device distinti")


def _hourly_device(device_id: str) -> pd.Series | None:
    """Serie oraria di temperatura di un device già scaricato, solo valori allineati all'ora."""
    path = RAW_DIR / f"{device_id.replace(':', '')}.parquet"
    if not path.exists():
        return None
    f = pd.read_parquet(path).dropna(subset=["t"])
    f = f[(f.t > -25) & (f.t < 50)]
    t = pd.to_datetime(f.ts, unit="s", utc=True)
    ok = (t - t.dt.round("h")).abs() <= pd.Timedelta(minutes=5)
    return f[ok].assign(hour=t[ok].dt.round("h")).groupby("hour").t.mean()


def _unique_devices(stations: dict) -> list[dict]:
    seen = {}
    for st in stations.values():
        for d in st["devices"]:
            seen.setdefault(d["device_id"], d)
    return list(seen.values())


# ─────────────────────────────────────────────────────────────────────────────
# probe
# ─────────────────────────────────────────────────────────────────────────────

def probe(api: Netatmo, since: str) -> None:
    stations = _load_devices()
    devs = _unique_devices(stations)
    since_ts = int(pd.Timestamp(since, tz="UTC").timestamp())

    # 1. Convenzione dei timestamp orari: confronto con i dati grezzi dell'ultimo giorno.
    ref = devs[0]
    now = int(time.time())
    begin = now - 26 * 3600
    raw = pd.DataFrame(api.measure(ref["device_id"], ref["module_id"], "max", begin), columns=["ts", "t", "h"])
    for rt in (False, True):
        hourly = pd.DataFrame(api.measure(ref["device_id"], ref["module_id"], "1hour", begin, real_time=rt),
                              columns=["ts", "t", "h"])
        rows = []
        for _, hr in hourly.iterrows():
            for label, lo, hi in (("ora precedente", -3600, 0), ("centrata", -1800, 1800), ("ora successiva", 0, 3600)):
                m = raw[(raw.ts > hr.ts + lo) & (raw.ts <= hr.ts + hi)].t.mean()
                rows.append((label, abs(hr.t - m)))
        err = pd.DataFrame(rows, columns=["finestra", "diff"]).groupby("finestra")["diff"].mean().round(3)
        minutes = sorted({pd.Timestamp(t, unit="s").minute for t in hourly.ts})
        logger.info(f"real_time={rt}: minuti dei timestamp {minutes}; |media grezzi − valore orario| per finestra: "
                    f"{err.to_dict()}")

    # 2. Profondità dello storico orario per device.
    rows = []
    for i, d in enumerate(devs, 1):
        vals = api.measure(d["device_id"], d["module_id"], "1hour", since_ts)
        first = pd.Timestamp(vals[0][0], unit="s", tz="UTC") if vals else None
        rows.append({"device_id": d["device_id"], "first_hourly": first, "n_first_call": len(vals)})
        if i % 20 == 0:
            logger.info(f"probe {i}/{len(devs)}")
    depth = pd.DataFrame(rows).set_index("device_id")

    print(f"\n== Storico orario disponibile da {since} (primo valore per device) ==")
    for sid, st in stations.items():
        firsts = [depth.first_hourly.get(d["device_id"]) for d in st["devices"]]
        firsts = [f for f in firsts if f is not None]
        earliest = min(firsts).date() if firsts else None
        median = pd.Series(firsts).median().date() if firsts else None
        print(f"st.{sid:>2} {st['name']:<26} device {len(st['devices']):>2} con storico {len(firsts):>2} "
              f"| primo {earliest} | mediana {median}")
    depth.to_csv(WORK_DIR / "probe_depth.csv")
    logger.info(f"{api.calls} chiamate — dettaglio in {WORK_DIR / 'probe_depth.csv'}")


# ─────────────────────────────────────────────────────────────────────────────
# download
# ─────────────────────────────────────────────────────────────────────────────

def download(api: Netatmo, since: str, until: str | None, selected: bool = False) -> None:
    stations = _load_devices(selected)
    devs = _unique_devices(stations)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    # getmeasure aggrega su finestre di 1 h ancorate a date_begin e marca il valore al
    # centro della finestra. Partendo da HH:30:00 ogni valore è la media HH−0:30 → HH+0:30,
    # marcata esattamente a HH:00; ogni pagina riparte dalla fine dell'ultima finestra.
    since_ts = int(pd.Timestamp(since, tz="UTC").floor("h").timestamp()) - HALF_STEP
    end_ts = int(pd.Timestamp(until, tz="UTC").timestamp()) if until else int(time.time())

    failed = []
    for i, d in enumerate(devs, 1):
        path = RAW_DIR / f"{d['device_id'].replace(':', '')}.parquet"
        old = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=["ts", "t", "h"])
        # Riprende dall'ultimo valore già scaricato dentro [since, until), così si può anche
        # riempire un periodo precedente a quello già presente nel file.
        in_range = old.ts[(old.ts >= since_ts) & (old.ts < end_ts)] if len(old) else old.ts
        if len(in_range) and in_range.max() >= end_ts - 3 * 3600:
            continue  # già completo per questo intervallo
        cursor = int(in_range.max()) + HALF_STEP if len(in_range) else since_ts
        new = []
        try:
            while cursor < end_ts:
                vals = api.measure(d["device_id"], d["module_id"], "1hour", cursor, end_ts)
                if not vals:
                    break
                new.extend(vals)
                nxt = vals[-1][0] + HALF_STEP
                if nxt <= cursor:
                    break
                cursor = nxt
                if len(new) >= 8000:  # salva spesso, così un'interruzione non perde lavoro
                    old = _save(path, old, new)
                    new = []
        except RuntimeError as exc:
            logger.error(f"device {d['device_id']} saltato: {exc}")
            failed.append(d["device_id"])
        _save(path, old, new)
        logger.info(f"device {i}/{len(devs)} {d['device_id']} — chiamate totali {api.calls}")
    if failed:
        logger.warning(f"{len(failed)} device non scaricati (rilanciare 'download' per riprovare): {failed}")


def _save(path: Path, old: pd.DataFrame, new: list) -> pd.DataFrame:
    if not new:
        return old
    df = pd.concat([old, pd.DataFrame(new, columns=["ts", "t", "h"])], ignore_index=True)
    df = df.drop_duplicates("ts", keep="last").sort_values("ts")
    df.to_parquet(path, index=False)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# build
# ─────────────────────────────────────────────────────────────────────────────

def build(ts_shift_s: int, selected: bool = False) -> None:
    stations = _load_devices(selected)
    parts = []
    for sid, st in stations.items():
        frames = []
        for d in st["devices"]:
            path = RAW_DIR / f"{d['device_id'].replace(':', '')}.parquet"
            if path.exists():
                f = pd.read_parquet(path)
                frames.append(f.assign(device_id=d["device_id"]))
        if not frames:
            continue
        df = pd.concat(frames, ignore_index=True).dropna(subset=["t"])
        df = df[(df.t > -25) & (df.t < 50)]
        # Il valore orario è centrato sul suo timestamp (vedi 'probe' e 'download'): deve cadere
        # sull'ora esatta. Un valore fuori di più di 5 minuti viene da una finestra disallineata.
        t = pd.to_datetime(df.ts + ts_shift_s, unit="s", utc=True)
        aligned = (t - t.dt.round("h")).abs() <= pd.Timedelta(minutes=5)
        if (~aligned).any():
            logger.warning(f"st.{sid}: scartati {(~aligned).sum()} valori non allineati all'ora "
                           f"({100 * (~aligned).mean():.1f}%)")
        df = df[aligned].assign(recorded_at=t[aligned].dt.round("h"))
        df = df.groupby(["device_id", "recorded_at"], as_index=False)[["t", "h"]].mean()
        g = df.groupby("recorded_at")
        agg = pd.DataFrame({"temperature": g.t.median(), "humidity": g.h.median(), "n_devices": g.device_id.nunique()})
        agg = agg[agg.n_devices >= st["min_cluster"]].reset_index()
        parts.append(agg.assign(station_id=int(sid)))
        logger.info(f"st.{sid:>2} {st['name']:<26} {len(agg):>6} ore "
                    f"({agg.recorded_at.min():%Y-%m-%d} → {agg.recorded_at.max():%Y-%m-%d})" if len(agg) else
                    f"st.{sid:>2} {st['name']:<26} nessuna ora con abbastanza device")
    out = pd.concat(parts, ignore_index=True)
    out.to_parquet(TARGET_FILE, index=False)
    logger.info(f"Salvato {TARGET_FILE}: {len(out):,} righe, {out.station_id.nunique()} stazioni")


# ─────────────────────────────────────────────────────────────────────────────
# validate
# ─────────────────────────────────────────────────────────────────────────────

def validate(since: str, until: str | None, threshold: float, selected: bool = False) -> None:
    """Confronta il target ricostruito con le osservazioni live Netatmo del periodo in comune."""
    stations = _load_devices(selected)
    target = pd.read_parquet(TARGET_FILE)
    target["recorded_at"] = pd.to_datetime(target.recorded_at, utc=True)
    start = pd.Timestamp(since, tz="UTC")
    end = pd.Timestamp(until, tz="UTC") if until else pd.Timestamp.now(tz="UTC")
    client = db.get_client()

    rows = []
    for sid, st in stations.items():
        obs = _paged_obs(client, int(sid), start, end)
        if obs.empty:
            rows.append({"station_id": int(sid), "name": st["name"], "n": 0})
            continue
        # Solo valori vicini all'ora esatta (:02 della produzione), non quelli a :31.
        off = (obs.recorded_at - obs.recorded_at.dt.round("h")).abs()
        obs = obs[off <= pd.Timedelta(minutes=15)].assign(hour=lambda d: d.recorded_at.dt.round("h"))
        obs = obs.groupby("hour").temperature.mean()

        tgt = target[target.station_id == int(sid)].set_index("recorded_at").temperature
        res = {"station_id": int(sid), "name": st["name"]}
        for lag in (-1, 0, 1):
            j = pd.concat([tgt.shift(lag, freq="h").rename("bf"), obs.rename("obs")], axis=1, join="inner").dropna()
            d = j.bf - j.obs
            res[f"mae_lag{lag:+d}"] = d.abs().mean() if len(d) else np.nan
            if lag == 0:
                res.update(n=len(d), bias=d.mean(), mae=d.abs().mean(), p90=d.abs().quantile(0.9) if len(d) else np.nan)
        rows.append(res)

    df = pd.DataFrame(rows).set_index(["station_id", "name"]).sort_index()
    df["lag_migliore"] = df[["mae_lag-1", "mae_lag+0", "mae_lag+1"]].idxmin(axis=1).str.replace("mae_lag", "")
    df["ok"] = (df.mae <= threshold) & (df.bias.abs() <= threshold) & (df.lag_migliore == "+0")
    cols = ["n", "bias", "mae", "p90", "mae_lag-1", "mae_lag+0", "mae_lag+1", "lag_migliore", "ok"]
    print(f"\n== Backfill − osservazioni live Netatmo, {start:%Y-%m-%d} → {end:%Y-%m-%d} (soglia {threshold} °C) ==")
    print(df[cols].round(2).to_string())
    ok = df.ok.sum()
    print(f"\nStazioni entro soglia: {ok}/{len(df)} | MAE mediana {df.mae.median():.2f} °C | "
          f"bias mediano {df.bias.median():+.2f} °C")
    df.round(3).to_csv(WORK_DIR / "validate.csv")


def _paged_obs(client, sid: int, start, end, with_raw: bool = False) -> pd.DataFrame:
    out, offset = [], 0
    while True:
        rows = (client.table("observations").select("recorded_at, temperature, raw_source")
                .eq("station_id", sid).lt("qc_flag", 2)
                .gte("recorded_at", start.isoformat()).lte("recorded_at", end.isoformat())
                .order("recorded_at").range(offset, offset + 999).execute().data)
        out.extend(rows)
        if len(rows) < 1000:
            break
        offset += 1000
    df = pd.DataFrame(out, columns=["recorded_at", "temperature", "raw_source"])
    src = df.raw_source.map(lambda r: (r or {}).get("source") if isinstance(r, dict) else None)
    df = df[src == "netatmo_public"].dropna(subset=["temperature"])
    df["recorded_at"] = pd.to_datetime(df.recorded_at, utc=True, format="ISO8601")
    if with_raw:
        df["n_stations"] = df.raw_source.map(lambda r: r.get("n_stations"))
        df["temps_raw"] = df.raw_source.map(lambda r: r.get("temps_raw") or [])
        return df[["recorded_at", "temperature", "n_stations", "temps_raw"]]
    return df[["recorded_at", "temperature"]]


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill storico orario target Netatmo")
    ap.add_argument("step", choices=["discover", "select", "probe", "download", "build", "validate"])
    ap.add_argument("--selected", action="store_true",
                    help="download/build/validate sui device di 'select' (devices_selected.json)")
    ap.add_argument("--threshold", type=float, default=0.5, help="Soglia di accordo per 'validate' (°C)")
    ap.add_argument("--since", default="2024-03-14", help="Inizio dello storico (default: inizio archivio IFS)")
    ap.add_argument("--until", default=None)
    ap.add_argument("--pause", type=float, default=7.5, help="Secondi tra le chiamate (500/ora → 7.2 s)")
    ap.add_argument("--ts-shift", type=int, default=0,
                    help="Secondi da sommare al timestamp orario Netatmo per ottenere l'ora di riferimento (da 'probe')")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    api = Netatmo(args.pause)
    if args.step == "discover":
        discover(api)
    elif args.step == "probe":
        probe(api, args.since)
    elif args.step == "select":
        select(args.since, args.until)
    elif args.step == "download":
        download(api, args.since, args.until, args.selected)
    elif args.step == "build":
        build(args.ts_shift, args.selected)
    else:
        validate(args.since, args.until, args.threshold, args.selected)


if __name__ == "__main__":
    main()
