"""Costanti e helper condivisi da ia_meteo (Fase 0: smoke test).

Nessun segreto viene mai stampato: gli helper leggono le chiavi da `.env`
(root del repo) o dai file di configurazione standard delle librerie.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent            # ia_meteo/
REPO = ROOT.parent                                # meteo_locale/
SMOKE = ROOT / "smoke"
RAW = SMOKE / "raw"                               # ignorato da git
RESULTS = SMOKE / "results"
PREVIEWS = SMOKE / "previews"

DOMAIN_BBOX = dict(lat_min=30, lat_max=50, lon_min=-10, lon_max=30)
TEST_BBOXES = {               # per misurare la copertura per zona
    "spagna":  dict(lat_min=36, lat_max=43.5, lon_min=-9.5, lon_max=3.5),
    "italia":  dict(lat_min=36.5, lat_max=47, lon_min=6.5, lon_max=18.5),
    "balcani": dict(lat_min=40, lat_max=46.5, lon_min=13.5, lon_max=23),
    "lazio":   dict(lat_min=41.18, lat_max=42.85, lon_min=11.40, lon_max=14.05),
    "tirreno": dict(lat_min=38, lat_max=42, lon_min=10, lon_max=14),
    "nord_africa": dict(lat_min=31, lat_max=37, lon_min=-5, lon_max=11),
}

for _d in (RAW, RESULTS, PREVIEWS):
    _d.mkdir(parents=True, exist_ok=True)


def load_env() -> None:
    """Carica `.env` della root nel processo (senza stamparne il contenuto)."""
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env", override=False)


def env(name: str) -> str | None:
    load_env()
    val = os.environ.get(name)
    return val or None


def mb(path: Path | str) -> float:
    """Dimensione in MB di un file o di una cartella."""
    p = Path(path)
    if not p.exists():
        return 0.0
    if p.is_file():
        return p.stat().st_size / 1e6
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6


def rm(path: Path | str) -> None:
    p = Path(path)
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    elif p.exists():
        p.unlink()


class Timer:
    def __init__(self):
        self.t = {}

    @contextmanager
    def __call__(self, key: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.t[key] = round(self.t.get(key, 0) + time.perf_counter() - t0, 2)


def bbox_mask(lat, lon, bb: dict):
    return (lat >= bb["lat_min"]) & (lat <= bb["lat_max"]) & (lon >= bb["lon_min"]) & (lon <= bb["lon_max"])


def area_def_regular(bb: dict = DOMAIN_BBOX, res: float = 0.05, name: str = "dominio"):
    """AreaDefinition lat/lon regolare (EPSG:4326) sul bbox, passo `res` gradi."""
    from pyresample import create_area_def
    return create_area_def(name, "EPSG:4326",
                           area_extent=(bb["lon_min"], bb["lat_min"], bb["lon_max"], bb["lat_max"]),
                           resolution=res, units="degrees")


def write_result(test: str, esito: str, domanda: str, risposta: str,
                 misure: dict | None = None, errori: list | None = None, **extra) -> Path:
    assert esito in ("OK", "PARZIALE", "KO", "SALTATO"), esito
    out = {
        "test": test,
        "esito": esito,
        "domanda": domanda,
        "risposta": risposta,
        "misure": misure or {},
        "errori": [str(e)[:2000] for e in (errori or [])],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **extra,
    }
    path = RESULTS / f"{test}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[{test}] {esito} → {path.relative_to(ROOT)}")
    return path


def read_result(test: str) -> dict | None:
    p = RESULTS / f"{test}.json"
    return json.loads(p.read_text()) if p.exists() else None
