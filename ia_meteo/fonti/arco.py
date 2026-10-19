"""ERA5 da ARCO-ERA5 (Google Cloud, pubblico, senza coda): alternativa al CDS per il catalogo.

Archivio Zarr v2 `full_37-1h-0p25deg-chunk-1.zarr-v3`: ogni blocco è un'ora × tutto il globo
(× 37 livelli per le variabili in quota), compresso Blosc/LZ4. Un blocco di T o q pesa ~85–100 MB:
non si può leggere solo il nostro riquadro, quindi si scarica il blocco in memoria, si ritaglia il
dominio e si butta il resto (sul disco non passa nulla). Un mese di catalogo ≈ 50 GB di rete.
Valori identici al CDS (stessi GRIB ERA5): verificato su luglio 2019, ML-CAPE ±6 J/kg, CAPE/CIN ±0,1.
"""
from __future__ import annotations

import time
from datetime import datetime

import numpy as np
import requests
import xarray as xr
from numcodecs import Blosc

from common import DOMAIN_BBOX

URL = "https://storage.googleapis.com/gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
ORIGINE = datetime(1900, 1, 1)               # indice temporale = ore dal 1900-01-01
NY, NX = 721, 1440                           # 90 → −90, 0 → 359,75
LIVELLI = np.array([1, 2, 3, 5, 7, 10, 20, 30, 50, 70, 100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450,
                    500, 550, 600, 650, 700, 750, 775, 800, 825, 850, 875, 900, 925, 950, 975, 1000])
# nome ARCO → nome dei file CDS (così il resto del catalogo non cambia)
NOMI = {"2m_temperature": "t2m", "2m_dewpoint_temperature": "d2m", "surface_pressure": "sp",
        "convective_available_potential_energy": "cape", "convective_inhibition": "cin",
        "convective_precipitation": "cp", "temperature": "t", "specific_humidity": "q",
        "k_index": "kx", "total_totals_index": "totalx", "total_column_water_vapour": "tcwv",
        "zero_degree_level": "deg0l", "boundary_layer_height": "blh", "sea_surface_temperature": "sst",
        "10m_u_component_of_wind": "u10", "10m_v_component_of_wind": "v10", "mean_sea_level_pressure": "msl",
        "u_component_of_wind": "u", "v_component_of_wind": "v"}
_BLOSC = Blosc()


def griglia(bb=DOMAIN_BBOX, passo=0.25):
    """Latitudini e longitudini come le restituisce il CDS per `area` (lat decrescenti, lon −180…180)."""
    lat = np.round(np.arange(bb["lat_max"], bb["lat_min"] - passo / 2, -passo), 2)
    lon = np.round(np.arange(bb["lon_min"], bb["lon_max"] + passo / 2, passo), 2)
    return lat, lon


def _indici(lat, lon):
    iy = np.rint((90 - lat) / 0.25).astype(int)
    ix = np.rint(np.mod(lon, 360) / 0.25).astype(int)
    return iy, ix


def indice_ora(t: datetime) -> int:
    return int((t - ORIGINE).total_seconds() // 3600)


def blocco(sessione, variabile: str, t: datetime, in_quota: bool, tentativi=6) -> np.ndarray:
    """Un'ora globale decompressa; riprova con attesa crescente sugli errori di rete."""
    chiave = f"{indice_ora(t)}.0.0.0" if in_quota else f"{indice_ora(t)}.0.0"
    for k in range(tentativi):
        try:
            r = sessione.get(f"{URL}/{variabile}/{chiave}", timeout=120)
            r.raise_for_status()
            a = np.frombuffer(_BLOSC.decode(r.content), dtype="<f4")
            return a.reshape((len(LIVELLI), NY, NX) if in_quota else (NY, NX)), len(r.content)
        except Exception:  # noqa: BLE001
            if k == tentativi - 1:
                raise
            time.sleep(10 * 2 ** k)


def ritaglio(sessione, variabile, t, lat, lon, livelli=None):
    """Blocco ritagliato sul dominio (e sui livelli richiesti, nell'ordine dato) + byte scaricati."""
    iy, ix = _indici(lat, lon)
    a, n = blocco(sessione, variabile, t, livelli is not None)
    if livelli is None:
        return a[np.ix_(iy, ix)].copy(), n
    il = [int(np.where(LIVELLI == int(v))[0][0]) for v in livelli]
    return a[np.ix_(il, iy, ix)].copy(), n


def dataset(variabili, tempi, livelli=None, bb=DOMAIN_BBOX, flussi=4):
    """Dataset come quelli del CDS (nomi corti, coordinate time/latitude/longitude[/pressure_level]).
    Restituisce (ds, GB scaricati). I blocchi si scaricano in parallelo su `flussi` connessioni."""
    from concurrent.futures import ThreadPoolExecutor
    lat, lon = griglia(bb)
    sessione = requests.Session()
    sessione.mount("https://", requests.adapters.HTTPAdapter(pool_connections=flussi, pool_maxsize=flussi))
    lavori = [(v, t) for v in variabili for t in tempi]
    with ThreadPoolExecutor(flussi) as ex:
        ris = list(ex.map(lambda vt: ritaglio(sessione, vt[0], vt[1], lat, lon, livelli), lavori))
    byte = sum(n for _, n in ris)
    dati = {}
    for j, v in enumerate(variabili):
        pila = np.stack([ris[j * len(tempi) + i][0] for i in range(len(tempi))])
        dims = ("time", "pressure_level", "latitude", "longitude") if livelli is not None else ("time", "latitude", "longitude")
        dati[NOMI.get(v, v)] = (dims, pila)
    coords = {"time": np.array(tempi, dtype="datetime64[ns]"), "latitude": lat, "longitude": lon}
    if livelli is not None:
        coords["pressure_level"] = np.array([float(x) for x in livelli])
    return xr.Dataset(dati, coords=coords), byte / 1e9


def umidita_relativa(t, q, p_hpa):
    """Umidità relativa (%) come la calcola l'IFS per il parametro `r` di ERA5 (ARCO non ce l'ha):
    saturazione sull'acqua sopra 0 °C, sul ghiaccio sotto −23 °C, miscela quadratica in mezzo
    (IFS Documentation CY41R2, Part IV, §7.2.1)."""
    T0, TICE = 273.16, 250.16
    esw = 611.21 * np.exp(17.502 * (t - T0) / (t - 32.19))
    esi = 611.21 * np.exp(22.587 * (t - T0) / (t + 0.7))
    alfa = np.clip((t - TICE) / (T0 - TICE), 0, 1) ** 2
    es = alfa * esw + (1 - alfa) * esi
    e = p_hpa * 100 * q / (0.621981 + 0.378019 * q)
    return 100 * e / es
