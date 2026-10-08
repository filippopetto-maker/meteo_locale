"""EUMETSAT Data Store e Data Tailor: SEVIRI, FCI, LI.

Soluzioni verificate negli smoke test della Fase 0 (07-08/10/2026):
- SEVIRI HRSEVIRI: un prodotto = zip (~185 MB) con un .nat (~271 MB); reader satpy
  `seviri_l1b_native`.
- FCI FDHSI: 61 entry per ciclo (40 BODY + TRAIL + quicklook/metadati); i BODY sono
  fasce di latitudine numerate da sud (1) a nord (40) e si scaricano singolarmente con
  `Product.open(entry=...)`. Per 30–50°N bastano i chunk 31–37 (~180 MB contro ~1100).
  Il TRAIL non serve a satpy (`fci_l1c_nc`).
- LI AF: un prodotto ogni 10 min (20 accumuli da 30 s), griglia sparsa sulla griglia FCI
  2 km. Geolocalizzazione con la convenzione del reader satpy `li_l2_nc`: indici interi
  x/y (1..5568, origine SW) → area `mtg_fci_fdss_2km`. Mai `get_lonlat()` sull'intera
  griglia (5568² satura 8 GB): si convertono solo i pixel attivi.
- Data Tailor: FCI con `FCIL1FDHSI` + `netcdf4` + bande `*_effective_radiance`
  (`FCIL1FDHSI_NATIVE` è rifiutato); SEVIRI con `HRSEVIRI` + `channel_N`.
"""
from __future__ import annotations

import re
import time
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np

from common import DOMAIN_BBOX, env, mb

SEVIRI = "EO:EUM:DAT:MSG:HRSEVIRI"
FCI = "EO:EUM:DAT:0662"
LI_AF = "EO:EUM:DAT:0686"

# canali del ramo immagine (planning): IR 10,8 / 10,5 µm e vapore acqueo 6,2 e 7,3 µm
CANALI_SEVIRI = ["IR_108", "WV_062", "WV_073"]
CANALI_FCI = ["ir_105", "wv_63", "wv_73"]
# numerazione Data Tailor HRSEVIRI (canali SEVIRI 1..11): 5=WV 6,2  6=WV 7,3  9=IR 10,8
DT_CANALI_SEVIRI = {"WV_062": "channel_5", "WV_073": "channel_6", "IR_108": "channel_9"}
FCI_RIGHE = 5568
FCI_N_CHUNK = 40


# ---------------------------------------------------------------- accesso
def token():
    import eumdac
    key, secret = env("EUMETSAT_CONSUMER_KEY"), env("EUMETSAT_CONSUMER_SECRET")
    if not (key and secret):
        raise RuntimeError("EUMETSAT_CONSUMER_KEY/SECRET mancanti in .env")
    return eumdac.AccessToken((key, secret))


def cerca(collezione: str, t0: datetime, t1: datetime, tok=None) -> list:
    """Prodotti con sensing_start in [t0, t1), ordinati nel tempo."""
    import eumdac
    tok = tok or token()
    res = eumdac.DataStore(tok).get_collection(collezione).search(dtstart=t0, dtend=t1)
    return sorted((p for p in res if t0 <= p.sensing_start < t1), key=lambda p: p.sensing_start)


def scarica(prodotto, dest: Path, entry: str | None = None) -> Path:
    """Scarica il prodotto intero (zip) o una sola entry in `dest` (cartella)."""
    dest.mkdir(parents=True, exist_ok=True)
    f = dest / (entry if entry else f"{prodotto}.zip")
    with prodotto.open(entry=entry) as src, open(f, "wb") as dst:
        while c := src.read(1 << 20):
            dst.write(c)
    return f


def dimensioni_entry(prodotto, tok) -> dict[str, int | None]:
    """Byte di ogni entry senza scaricarla (GET con Range: bytes=0-0 → Content-Range)."""
    import requests
    base = prodotto.metadata["properties"]["links"]["data"][0]["href"]
    h = {"Authorization": f"Bearer {tok.access_token}", "Range": "bytes=0-0"}
    out = {}
    for e in prodotto.entries:
        r = requests.get(f"{base}/entry", params={"name": e}, headers=h, timeout=60, stream=True)
        cr = r.headers.get("Content-Range", "")
        out[e] = int(cr.split("/")[-1]) if "/" in cr else None
        r.close()
    return out


def _salva_ritaglio(ds, path: Path | None):
    if path is not None:
        enc = {v: {"zlib": True, "complevel": 4} for v in ds.data_vars}
        ds.to_netcdf(path, encoding=enc)
    return ds


def _a_dataset(scn, canali, area, attrs):
    import xarray as xr
    lons, lats = area.get_lonlats()
    data = {c: (("lat", "lon"), scn[c].values.astype("float32")) for c in canali}
    return xr.Dataset(data, coords={"lat": lats[:, 0], "lon": lons[0, :]}, attrs=attrs)


# ---------------------------------------------------------------- SEVIRI
def seviri_ritaglio(prodotto, area, lavoro: Path, canali=CANALI_SEVIRI, out: Path | None = None,
                    tieni_grezzi: bool = False):
    """Zip → .nat → satpy → griglia comune. Cancella zip e .nat. Ritorna (Dataset, misure)."""
    from satpy import Scene
    m = {}
    t0 = time.perf_counter()
    z = scarica(prodotto, lavoro)
    m["sec_download"], m["MB_zip"] = round(time.perf_counter() - t0, 1), round(mb(z), 1)
    with zipfile.ZipFile(z) as zz:
        nat = [n for n in zz.namelist() if n.endswith(".nat")][0]
        zz.extract(nat, lavoro)
    natp = lavoro / nat
    m["MB_nat"] = round(mb(natp), 1)
    t0 = time.perf_counter()
    scn = Scene(reader="seviri_l1b_native", filenames=[str(natp)])
    scn.load(canali)
    loc = scn.resample(area, resampler="nearest", radius_of_influence=10000)
    t_nom = scn[canali[0]].attrs.get("start_time")
    ds = _a_dataset(loc, canali, area, {"start_time": str(t_nom), "units": "K", "fonte": SEVIRI,
                                        "prodotto": str(prodotto)})
    m["sec_lettura_ritaglio"] = round(time.perf_counter() - t0, 1)
    if not tieni_grezzi:
        z.unlink(missing_ok=True)
        natp.unlink(missing_ok=True)
    return _salva_ritaglio(ds, out), m


# ---------------------------------------------------------------- FCI
def numero_chunk(nome: str) -> tuple[str | None, int | None]:
    m = re.search(r"CHK-(BODY|TRAIL).*_(\d{4})\.nc$", nome)
    return (m.group(1), int(m.group(2))) if m else (None, None)


def fci_chunk_per_bbox(bb: dict = DOMAIN_BBOX, margine: int = 0) -> list[int]:
    """Chunk FDHSI che coprono il bbox, dalla geometria della griglia 2 km (righe da sud)."""
    from satpy.area import get_area_def
    a = get_area_def("mtg_fci_fdss_2km")
    righe = []
    for lat in (bb["lat_min"], bb["lat_max"]):
        for lon in np.linspace(bb["lon_min"], bb["lon_max"], 9):
            _, r = a.get_array_indices_from_lonlat(lon, lat)
            righe.append(FCI_RIGHE - int(r))
    per = FCI_RIGHE / FCI_N_CHUNK
    lo = max(1, int(np.ceil(min(righe) / per)) - margine)
    hi = min(FCI_N_CHUNK, int(np.ceil(max(righe) / per)) + margine)
    return list(range(lo, hi + 1))


def fci_entry(prodotto, chunks: list[int], trail: bool = False) -> list[str]:
    sel = []
    for e in prodotto.entries:
        tipo, n = numero_chunk(e)
        if (tipo == "BODY" and n in chunks) or (trail and tipo == "TRAIL"):
            sel.append(e)
    return sorted(sel)


def fci_righe_chunk(f: Path, canale: str = "ir_105") -> tuple[int, int]:
    import netCDF4
    with netCDF4.Dataset(f) as d:
        g = d[f"data/{canale}/measured"]
        return int(g["start_position_row"][...]), int(g["end_position_row"][...])


def fci_ritaglio(prodotto, area, lavoro: Path, canali=CANALI_FCI, bb: dict = DOMAIN_BBOX,
                 chunks: list[int] | None = None, out: Path | None = None, tieni_grezzi: bool = False):
    """Scarica solo i chunk BODY del bbox, legge con satpy, ritaglia sulla griglia comune."""
    from satpy import Scene
    chunks = chunks or fci_chunk_per_bbox(bb)
    m = {"chunk": chunks}
    t0 = time.perf_counter()
    files = [scarica(prodotto, lavoro, e) for e in fci_entry(prodotto, chunks)]
    m["sec_download"] = round(time.perf_counter() - t0, 1)
    m["MB_chunk"] = round(sum(mb(f) for f in files), 1)
    m["righe_chunk"] = {numero_chunk(f.name)[1]: fci_righe_chunk(f) for f in files}
    t0 = time.perf_counter()
    scn = Scene(reader="fci_l1c_nc", filenames=[str(f) for f in files])
    scn.load(canali)
    c = scn.crop(ll_bbox=(bb["lon_min"], bb["lat_min"], bb["lon_max"], bb["lat_max"]))
    loc = c.resample(area, resampler="nearest", radius_of_influence=10000)
    ds = _a_dataset(loc, canali, area, {"start_time": str(prodotto.sensing_start), "units": "K",
                                        "fonte": FCI, "prodotto": str(prodotto)})
    m["sec_lettura_ritaglio"] = round(time.perf_counter() - t0, 1)
    if not tieni_grezzi:
        for f in files:
            f.unlink(missing_ok=True)
    return _salva_ritaglio(ds, out), m


# ---------------------------------------------------------------- LI
def li_body(prodotto) -> str:
    return [e for e in prodotto.entries if "BODY" in e and e.endswith(".nc")][0]


def li_flash(f: Path):
    """lat, lon, flash_accumulation dei soli pixel attivi di un file LI AF (BODY)."""
    import xarray as xr
    from pyproj import Transformer
    from satpy.area import get_area_def
    area = get_area_def("mtg_fci_fdss_2km")
    tr = Transformer.from_crs(area.crs, "EPSG:4326", always_xy=True)
    with xr.open_dataset(f) as d, xr.open_dataset(f, mask_and_scale=False) as draw:
        rows = FCI_RIGHE - draw.y.values.astype(int)       # origine SW → righe dall'alto
        cols = draw.x.values.astype(int) - 1
        w = d.flash_accumulation.values
        attrs = {"start": d.attrs.get("time_coverage_start"), "end": d.attrs.get("time_coverage_end"),
                 "n_accumulazioni": int(d.sizes["accumulations"])}
    x0, _, _, y1 = area.area_extent
    lon, lat = tr.transform(x0 + (cols + 0.5) * area.pixel_size_x, y1 - (rows + 0.5) * area.pixel_size_y)
    ok = np.isfinite(lon) & np.isfinite(lat) & (np.abs(lon) < 1e3)
    return lat[ok], lon[ok], w[ok], attrs


def li_su_griglia(lat, lon, w, area):
    """Somma flash×pixel nelle celle della griglia comune (lat decrescente come gli altri ritagli)."""
    lons, lats = area.get_lonlats()
    lon_e = np.r_[lons[0, :] - area.pixel_size_x / 2, lons[0, -1] + area.pixel_size_x / 2]
    lat_c = lats[:, 0][::-1]
    lat_e = np.r_[lat_c - area.pixel_size_y / 2, lat_c[-1] + area.pixel_size_y / 2]
    H, _, _ = np.histogram2d(lat, lon, bins=[lat_e, lon_e], weights=w)
    return H[::-1].astype("float32")


# ---------------------------------------------------------------- Data Tailor
def tailor(prodotto, prodotto_dt: str, bande: list[str], bb: dict = DOMAIN_BBOX, formato: str = "netcdf4",
           dest: Path | None = None, limite_s: int = 900, tok=None) -> dict:
    """Customizzazione Data Tailor con ROI e filtro bande; scarica l'output in `dest`, poi la cancella."""
    import eumdac
    from eumdac.tailor_models import Chain
    dt = eumdac.DataTailor(tok or token())
    chain = Chain(product=prodotto_dt, format=formato, filter={"bands": bande},
                  roi={"NSWE": [bb["lat_max"], bb["lat_min"], bb["lon_min"], bb["lon_max"]]})
    t0 = time.perf_counter()
    cust = dt.new_customisation(prodotto, chain)
    stato = None
    try:
        while time.perf_counter() - t0 < limite_s:
            stato = cust.status
            if stato in ("DONE", "FAILED", "KILLED", "INACTIVE"):
                break
            time.sleep(10)
        res = {"stato": stato, "secondi": round(time.perf_counter() - t0, 1)}
        if stato == "DONE" and dest is not None:
            dest.mkdir(parents=True, exist_ok=True)
            files = []
            for o in cust.outputs:
                p = dest / Path(o).name
                with cust.stream_output(o) as src, open(p, "wb") as dst:
                    while c := src.read(1 << 20):
                        dst.write(c)
                files.append(p)
            res["file"] = files
            res["MB_output"] = round(sum(mb(f) for f in files), 2)
        elif stato != "DONE":
            res["log"] = cust.logfile[-1500:]
        return res
    finally:
        try:
            if stato not in ("DONE", "FAILED", "KILLED", "INACTIVE"):
                cust.kill()
            cust.delete()
        except Exception:  # noqa: BLE001
            pass


def tailor_quota(tok=None) -> dict:
    import eumdac
    q = eumdac.DataTailor(tok or token()).quota["data"]
    return next(iter(q.values()))
