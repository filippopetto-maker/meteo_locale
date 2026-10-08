"""NASA GPM IMERG via earthaccess (login da ~/.netrc).

Final V07 half-hourly = collezione `GPM_3IMERGHH`, versione "07", 0,1°, ~8 MB per file
globale, variabile `precipitation` (mm/hr) nel gruppo `Grid` (dimensioni time, lon, lat).
V07 Final termina a settembre 2025; al 08/10/2026 la V08 non è ancora su CMR.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

SHORT_NAME = "GPM_3IMERGHH"


def login():
    import earthaccess
    return earthaccess.login(strategy="netrc")


def granuli(t0: datetime, t1: datetime, versione: str = "07"):
    import earthaccess
    return earthaccess.search_data(short_name=SHORT_NAME, version=versione,
                                   temporal=(t0.isoformat(), (t1 - timedelta(seconds=1)).isoformat()))


def scarica(gran, dest: Path) -> list[Path]:
    import earthaccess
    dest.mkdir(parents=True, exist_ok=True)
    return [Path(f) for f in earthaccess.download(gran, str(dest))]


def ritaglio(f: Path, bb: dict):
    """DataArray (lat, lon) della precipitazione sul bbox, primo istante del file."""
    import xarray as xr
    with xr.open_dataset(f, group="Grid", engine="h5netcdf", decode_timedelta=False) as ds:
        var = "precipitation" if "precipitation" in ds else "precipitationCal"
        da = ds[var].isel(time=0).sel(lat=slice(bb["lat_min"], bb["lat_max"]),
                                      lon=slice(bb["lon_min"], bb["lon_max"]))
        return da.transpose("lat", "lon").load(), var, dict(ds[var].sizes)
