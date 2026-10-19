"""Archivio radar italiano IT-DPC-SRI (Franch et al. 2026, Zenodo 10.5281/zenodo.18637608).

Su Zenodo c'è un solo tar.zst da ~50 GB (niente lettura parziale). La copia identica
sull'object storage S3 dell'ECMWF European Weather Cloud si legge in modo lazy e anonimo
(preprint arXiv:2602.15088 §6.2). Chunk Zarr = 1 timestep × intera Italia (1400×1200,
~0,17 MB compressi); la prima apertura scarica ~18 MB di coordinate (lat/lon 2D, tempo).
Variabile `precipitation` in kg m-2 h-1 (= mm/h); passo 15/10/5 min (2010-14/2014-20/2020-25).
"""
from __future__ import annotations

import numpy as np

ENDPOINT = "https://object-store.os-api.cci2.ecmwf.int"
ZARR = "mlcast-source-datasets/IT-DPC-SRI/v0.1.0/italian-radar-dpc-sri.zarr"
ZENODO = "https://zenodo.org/api/records/18637608"


def filesystem():
    import s3fs
    return s3fs.S3FileSystem(anon=True, client_kwargs={"endpoint_url": ENDPOINT})


def apri(fs=None):
    import xarray as xr
    fs = fs or filesystem()
    return xr.open_zarr(fs.get_mapper(ZARR), consolidated=True)


def variabile(ds) -> str:
    return next(v for v in ds.data_vars if ds[v].ndim == 3)


def finestra_bbox(ds, bb: dict) -> tuple[slice, slice]:
    """Slice (y, x) del rettangolo che contiene il bbox, dalle coordinate lat/lon 2D."""
    lat2, lon2 = ds["lat"].values, ds["lon"].values
    m = (lat2 >= bb["lat_min"]) & (lat2 <= bb["lat_max"]) & (lon2 >= bb["lon_min"]) & (lon2 <= bb["lon_max"])
    iy, ix = np.where(m)
    return slice(iy.min(), iy.max() + 1), slice(ix.min(), ix.max() + 1)


def ritaglio(ds, t, bb: dict, finestra=None):
    """DataArray del timestep più vicino a t (o di un intervallo slice) sul bbox."""
    var = variabile(ds)
    da = ds[var]
    ysl, xsl = finestra or finestra_bbox(ds, bb)
    if isinstance(t, slice):
        sel = da.sel(time=t)
    else:
        sel = da.isel(time=int(np.argmin(np.abs(ds.time.values - np.datetime64(t)))))
    return sel.isel({da.dims[1]: ysl, da.dims[2]: xsl})


def su_griglia(ds, t0, t1, lat_c, lon_c):
    """Pioggia IT-DPC-SRI su [t0, t1) aggregata sulla griglia regolare (centri lat_c decrescenti,
    lon_c crescenti): massimo e media dei pixel nativi (~1 km) di ogni cella. NaN fuori copertura.
    Ritorna (tempi, massimo, media) con forma (n, len(lat_c), len(lon_c))."""
    var = variabile(ds)
    passo = float(lat_c[0] - lat_c[1])
    lat2, lon2 = ds["lat"].values, ds["lon"].values
    r = np.rint((lat_c[0] - lat2) / passo).astype(int)
    c = np.rint((lon2 - lon_c[0]) / passo).astype(int)
    ok = (r >= 0) & (r < len(lat_c)) & (c >= 0) & (c < len(lon_c))
    cella = np.where(ok, r * len(lon_c) + c, -1).ravel()
    n = len(lat_c) * len(lon_c)
    sub = ds[var].sel(time=slice(np.datetime64(t0), np.datetime64(t1) - np.timedelta64(1, "s")))
    tempi, massimi, medie = [], [], []
    for i in range(sub.sizes["time"]):
        v = sub.isel(time=i).values.ravel()
        m = (cella >= 0) & np.isfinite(v)
        mx = np.full(n, -np.inf)
        np.maximum.at(mx, cella[m], v[m])
        conta = np.bincount(cella[m], minlength=n)
        somma = np.bincount(cella[m], weights=v[m], minlength=n)
        with np.errstate(all="ignore"):
            media = np.where(conta > 0, somma / conta, np.nan)
        massimi.append(np.where(np.isfinite(mx), mx, np.nan).reshape(len(lat_c), len(lon_c)).astype("float32"))
        medie.append(media.reshape(len(lat_c), len(lon_c)).astype("float32"))
        tempi.append(sub.time.values[i])
    return np.array(tempi), np.stack(massimi) if massimi else None, np.stack(medie) if medie else None
