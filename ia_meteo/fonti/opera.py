"""Compositi radar EUMETNET OPERA (Open Radar Data).

Verificato il 08/10/2026:
- L'API EDR MeteoGate (anonima, 200 richieste/h) serve solo la cache delle ultime 24 h.
- Lo storico (dal 2012) è nel bucket S3 pubblico `openradar-archive` su CloudFerro,
  accesso anonimo via HTTPS. Chiave: AAAA/MM/GG/OPERA/COMP/OPERA@AAAAMMGGTHHMM@0@<prodotto>.h5
- ODYSSEY (fino a 10/2024): 2 km, 15 min, prodotto `DBZH_QIND` (ODIM 2.0: DBZH e QIND in
  dataset separati, `quantity` in datasetN/what, product=COMP).
- CIRRUS (da 07/2024): 1 km, 5 min, prodotto `DBZH` (quantity in dataset1/dataM/what, product=MAX).
  Nel periodo di sovrapposizione esistono entrambi: si preferisce CIRRUS.
- Proiezione LAEA (lat_0=55, lon_0=10); il composito si ferma a ~31,7°N.
- nodata = fuori copertura; undetect = coperto senza eco (dato valido).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import requests

S3 = "https://s3.waw3-1.cloudferro.com/openradar-archive"
API = "https://api.meteogate.eu/eu-eumetnet-weather-radar/collections/observations/locations/0-20010-0-OPERA"
PRODOTTI_DBZH = ("DBZH", "DBZH_QIND")   # CIRRUS, ODYSSEY


def chiave(t: datetime, prodotto: str) -> str:
    return f"{t:%Y/%m/%d}/OPERA/COMP/OPERA@{t:%Y%m%dT%H%M}@0@{prodotto}.h5"


def elenco_giorno(giorno: datetime) -> dict[str, int]:
    """Chiavi e dimensioni dei compositi di un giorno (listing S3 anonimo)."""
    import re
    out, token = {}, None
    while True:
        params = {"list-type": "2", "prefix": f"{giorno:%Y/%m/%d}/OPERA/COMP/", "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        t = requests.get(f"{S3}/", params=params, timeout=60).text
        for k, s in re.findall(r"<Key>([^<]*)</Key>.*?<Size>(\d+)</Size>", t):
            out[k] = int(s)
        m = re.search(r"<NextContinuationToken>([^<]*)</NextContinuationToken>", t)
        if not m:
            return out
        token = m.group(1)


def scarica_dbzh(t: datetime, dest: Path) -> tuple[Path, str]:
    """Scarica il composito di riflettività massima all'istante t (CIRRUS se c'è, altrimenti ODYSSEY)."""
    dest.mkdir(parents=True, exist_ok=True)
    for prod in PRODOTTI_DBZH:
        r = requests.get(f"{S3}/{chiave(t, prod)}", timeout=120)
        if r.status_code == 200:
            f = dest / Path(chiave(t, prod)).name
            f.write_bytes(r.content)
            return f, prod
    raise FileNotFoundError(f"nessun composito DBZH per {t:%Y-%m-%d %H:%M}")


def leggi_odim(path: Path) -> dict:
    """Legge il primo DBZH di un composito ODIM (2.0 o recente). Valori grezzi + metadati."""
    import h5py
    from pyproj import Proj
    dec = lambda a: {k: (v.decode() if isinstance(v, bytes) else v) for k, v in a.items()}
    with h5py.File(path, "r") as f:
        where, what_root = dec(f["where"].attrs), dec(f["what"].attrs)
        quantita, sel, what = [], None, None
        for dsk in sorted(k for k in f if k.startswith("dataset")):
            dsw = dec(f[dsk]["what"].attrs) if "what" in f[dsk] else {}
            for dk in sorted(k for k in f[dsk] if k.startswith("data")):
                g = f[dsk][dk]
                w = dsw | (dec(g["what"].attrs) if "what" in g else {})
                quantita.append(w.get("quantity"))
                if w.get("quantity") == "DBZH" and sel is None:
                    sel, what = g, w
        raw = sel["data"][...]
    p = Proj(where["projdef"])
    ny, nx = raw.shape
    ulx, uly = p(where["UL_lon"], where["UL_lat"])
    x = ulx + (np.arange(nx) + 0.5) * where["xscale"]
    y = uly - (np.arange(ny) + 0.5) * where["yscale"]
    return {"raw": raw, "what": what, "where": where, "what_root": what_root, "quantita": quantita,
            "prodotto": what.get("product", ""), "proj": p, "x": x, "y": y}


def dbz(o: dict) -> np.ndarray:
    """dBZ con NaN fuori copertura e −32 dove coperto senza eco (undetect)."""
    w, raw = o["what"], o["raw"]
    v = raw * w["gain"] + w["offset"]
    v = np.where(raw == w["undetect"], -32.0, v)
    return np.where(raw == w["nodata"], np.nan, v).astype("float32")


def lonlat(o: dict, passo: int = 1):
    xs, ys = np.meshgrid(o["x"][::passo], o["y"][::passo])
    return o["proj"](xs, ys, inverse=True)


def copertura(o: dict, bboxes: dict, passo: int = 4) -> dict:
    """% di pixel non-nodata per zona (sottocampione 1 ogni `passo`)."""
    from common import bbox_mask
    lon, lat = lonlat(o, passo)
    valid = o["raw"][::passo, ::passo] != o["what"]["nodata"]
    out = {}
    for z, bb in bboxes.items():
        m = bbox_mask(lat, lon, bb)
        out[z] = round(100 * float(valid[m].mean()), 1) if m.any() else None
    return out


def su_griglia(o: dict, area) -> np.ndarray:
    """Riflettività massima per cella della griglia comune (max pooling, non media: i nuclei
    convettivi non vanno diluiti). NaN dove la cella è fuori copertura."""
    import dask.array as da
    from pyresample.bucket import BucketResampler
    v = dbz(o)
    lon, lat = lonlat(o)
    br = BucketResampler(area, da.from_array(lon), da.from_array(lat))
    out = br.get_max(da.from_array(np.where(np.isnan(v), -999.0, v))).compute()
    return np.where(out <= -998, np.nan, out).astype("float32")
