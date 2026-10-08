"""Ambiente in quota da ERA5 (Copernicus CDS, cdsapi ≥ 0.7.7, token in ~/.cdsapirc).

È la fonte di CAPE e CIN per l'addestramento: copre tutti gli anni del planning
(2015–2025) sull'intero dominio, orario, 0,25°. Open-Meteo non basta: IFS HRES 9 km
storico non ha né CAPE né CIN, IFS 0,25° ha CAPE ma non CIN (dal 2024), GFS ha
entrambi ma solo dal 2021 (smoke test S5).

Verificato nella Fase 0:
- nomi variabili del form CDS; nel netCDF diventano cape, cin, kx, totalx, tcwv, deg0l,
  blh, sst, cp, u10, v10, u, v, t, r;
- la single-levels torna come zip di più netCDF (stepType diversi per le variabili
  accumulate come `convective_precipitation`) anche con download_format=unarchived;
- coda tipica 30–40 s per una richiesta piccola.
- CAPE di ERA5 è la massima tra particelle in partenza da tutti i livelli sotto 350 hPa
  (most-unstable, ECMWF param-db 59).
- CIN di ERA5: "A missing value is assigned to CIN for values of CIN > 1000 or where there
  is no cloud base" (ERA5 data documentation, Confluence CKB). NaN = convezione dal basso
  bloccata, non assenza di inibizione: in S10 la CIN è NaN anche dove la CAPE supera
  4000 J/kg. `prepara_cin` porta i NaN a CIN_MANCANTE (1000 J/kg, il tetto documentato)
  e aggiunge la maschera `cin_definita`. Mai riempire con 0.
"""
from __future__ import annotations

import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from common import DOMAIN_BBOX, mb, rm

SINGLE = "reanalysis-era5-single-levels"
PRESSURE = "reanalysis-era5-pressure-levels"

# ramo ambiente del planning (+ vento a 10 m per lo shear 0–6 km)
VAR_SINGLE = ["convective_available_potential_energy", "convective_inhibition", "k_index",
              "total_totals_index", "total_column_water_vapour", "zero_degree_level",
              "boundary_layer_height", "sea_surface_temperature", "convective_precipitation",
              "10m_u_component_of_wind", "10m_v_component_of_wind"]
VAR_PRESSURE = ["u_component_of_wind", "v_component_of_wind", "temperature", "relative_humidity"]
LIVELLI = ["850", "700", "500", "300"]
# campi fissi (una volta sola): quota del terreno e maschera terra/mare per la distanza dalla costa
VAR_FISSE = ["geopotential", "land_sea_mask"]
CIN_MANCANTE = 1000.0   # J/kg: soglia oltre la quale ERA5 non scrive la CIN
# profili per la CAPE dello strato rimescolato (indici.ml_cape_cin): superficie + 23 livelli fino a 150 hPa
VAR_SUPERFICIE_ML = ["2m_temperature", "2m_dewpoint_temperature", "surface_pressure"]
LIVELLI_ML = ["1000", "975", "950", "925", "900", "875", "850", "825", "800", "775", "750", "700", "650",
              "600", "550", "500", "450", "400", "350", "300", "250", "200", "150"]
# catalogo eventi della Fase 1: ore con CAPE alto e precipitazione convettiva (planning)
VAR_CATALOGO = ["convective_available_potential_energy", "convective_inhibition", "convective_precipitation"]


def area_cds(bb: dict = DOMAIN_BBOX) -> list:
    return [bb["lat_max"], bb["lon_min"], bb["lat_min"], bb["lon_max"]]


def _apri(target: Path, cartella: Path):
    """Apre la risposta CDS: netCDF singolo o zip di netCDF (uniti in un Dataset)."""
    import xarray as xr
    if zipfile.is_zipfile(target):
        with zipfile.ZipFile(target) as z:
            z.extractall(cartella)
        parti = [xr.open_dataset(f).load() for f in sorted(cartella.glob("*.nc")) if f != target]
        ds = xr.merge(parti, compat="override", join="outer")
        for p in parti:
            p.close()
    else:
        ds = xr.open_dataset(target).load()
    drop = [v for v in ("expver", "number") if v in ds.variables]
    ds = ds.drop_vars(drop)
    if "valid_time" in ds.dims:
        ds = ds.rename({"valid_time": "time"})
    return ds


def richiesta(dataset: str, variabili: list[str], giorno: datetime, ore: list[int],
              lavoro: Path, bb: dict = DOMAIN_BBOX, livelli: list[str] | None = None,
              anni: list[int] | None = None, quiet: bool = True, giorni: list[int] | None = None):
    """Una richiesta CDS per un giorno (o più giorni dello stesso mese con `giorni`, o lo stesso giorno
    di più anni con `anni`) e un insieme di ore. Ritorna (Dataset in memoria, MB scaricati).
    I file vengono cancellati."""
    import cdsapi
    lavoro.mkdir(parents=True, exist_ok=True)
    req = dict(product_type=["reanalysis"], variable=variabili,
               year=[str(a) for a in (anni or [giorno.year])], month=[f"{giorno.month:02d}"],
               day=[f"{g:02d}" for g in (giorni or [giorno.day])], time=[f"{h:02d}:00" for h in ore],
               area=area_cds(bb), data_format="netcdf", download_format="unarchived")
    if livelli:
        req["pressure_level"] = livelli
    target = lavoro / f"{dataset}_{giorno:%Y%m%d}.nc"
    cdsapi.Client(quiet=quiet, progress=False).retrieve(dataset, req, str(target))
    peso = mb(target)
    sub = lavoro / (target.stem + "_zip")
    try:
        ds = _apri(target, sub)
    finally:
        rm(target)
        rm(sub)
    return ds, peso


def prepara_cin(ds):
    """CIN mancante (CIN > 1000 J/kg o nessuna base della nube) → CIN_MANCANTE, più la maschera
    `cin_definita` (1 dove ERA5 la calcola). Il modello riceve entrambe."""
    if "cin" in ds:
        ds["cin_definita"] = ds["cin"].notnull().astype("int8")
        ds["cin"] = ds["cin"].fillna(CIN_MANCANTE)
    return ds


def fetch_env(t0: datetime, t1: datetime, lavoro: Path, bb: dict = DOMAIN_BBOX, quiet: bool = True):
    """Ambiente orario da t0 a t1 compresi (ore intere) per un evento: single + pressure levels.
    Una coppia di richieste per giorno toccato. Ritorna (Dataset, MB scaricati)."""
    import xarray as xr
    ore_per_giorno: dict[datetime, list[int]] = {}
    t = t0.replace(minute=0, second=0, microsecond=0)
    while t <= t1:
        ore_per_giorno.setdefault(t.replace(hour=0), []).append(t.hour)
        t += timedelta(hours=1)
    parti, peso = [], 0.0
    for g, ore in ore_per_giorno.items():
        s, p1 = richiesta(SINGLE, VAR_SINGLE, g, ore, lavoro, bb, quiet=quiet)
        p, p2 = richiesta(PRESSURE, VAR_PRESSURE, g, ore, lavoro, bb, livelli=LIVELLI, quiet=quiet)
        peso += p1 + p2
        parti.append(xr.merge([s, p], compat="override"))
    ds = xr.concat(parti, dim="time") if len(parti) > 1 else parti[0]
    return prepara_cin(ds), peso


def catalogo_mese(anno: int, mese: int, lavoro: Path, bb: dict = DOMAIN_BBOX, quiet: bool = True) -> tuple[Path, dict]:
    """Un mese di catalogo per la Fase 1 (VAR_CATALOGO, tutte le ore, dominio) in un netCDF compresso
    `lavoro/era5_AAAA_MM.nc`. La CIN resta grezza (NaN = mancante): `prepara_cin` si applica in lettura.
    Una richiesta CDS sola (744 campi per 31 giorni). Ritorna (file, misure)."""
    import calendar
    import time
    giorni = list(range(1, calendar.monthrange(anno, mese)[1] + 1))
    t0 = time.perf_counter()
    ds, peso = richiesta(SINGLE, VAR_CATALOGO, datetime(anno, mese, 1), list(range(24)), lavoro, bb,
                         quiet=quiet, giorni=giorni)
    sec = round(time.perf_counter() - t0, 1)
    for v in ds.variables:
        ds[v].encoding = {}
    out = lavoro / f"era5_{anno:04d}_{mese:02d}.nc"
    ds.attrs |= {"fonte": f"{SINGLE} (Copernicus CDS)", "nota_cin": "NaN = CIN > 1000 J/kg o nessuna base "
                 "della nube (ERA5 data documentation); usare fonti.era5.prepara_cin"}
    ds.to_netcdf(out, encoding={v: {"dtype": "float32", "zlib": True, "complevel": 4} for v in ds.data_vars})
    return out, {"ore": int(ds.sizes["time"]), "variabili": sorted(ds.data_vars), "MB_scaricati": round(peso, 1),
                 "MB_file": round(mb(out), 1), "secondi_cds": sec}
