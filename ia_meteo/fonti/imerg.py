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


ARCHIVIO = "https://data.gesdisc.earthdata.nasa.gov/data/GPM_L3/GPM_3IMERGHH.07"


def url_diretti(t0: datetime, t1: datetime) -> list[str]:
    """Indirizzi dei file Final V07 costruiti dal nome (anno/giorno dell'anno/inizio-fine della mezz'ora/minuti
    dall'inizio del giorno): servono quando la ricerca CMR non risponde (500 il 20/10/2026)."""
    out, t = [], t0.replace(minute=0 if t0.minute < 30 else 30, second=0, microsecond=0)
    while t < t1:
        fine = t + timedelta(minutes=29, seconds=59)
        nome = (f"3B-HHR.MS.MRG.3IMERG.{t:%Y%m%d}-S{t:%H%M%S}-E{fine:%H%M%S}."
                f"{t.hour * 60 + t.minute:04d}.V07B.HDF5")
        out.append(f"{ARCHIVIO}/{t:%Y}/{t.timetuple().tm_yday:03d}/{nome}")
        t += timedelta(minutes=30)
    return out


def granuli(t0: datetime, t1: datetime, versione: str = "07"):
    """Granuli dalla ricerca CMR; se la ricerca è in errore, gli indirizzi diretti (solo V07)."""
    import earthaccess
    try:
        return earthaccess.search_data(short_name=SHORT_NAME, version=versione,
                                       temporal=(t0.isoformat(), (t1 - timedelta(seconds=1)).isoformat()))
    except Exception:  # noqa: BLE001
        if versione != "07":
            raise
        return url_diretti(t0, t1)


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
