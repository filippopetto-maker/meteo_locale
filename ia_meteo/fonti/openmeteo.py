"""Open-Meteo: Historical Forecast (storico dei modelli) e Forecast (operativo, Fase 5).

Identificativi modello documentati (pagina docs, 07/10/2026): `ecmwf_ifs` (HRES 9 km,
dal 2017, solo superficie nello storico), `ecmwf_ifs025` (dal 2024-02-03, CAPE e livelli
di pressione ma non CIN/LI), `ncep_gfs_seamless` (dal 2021-03-23, CAPE/CIN/LI e livelli).
`gfs_seamless` è un alias funzionante.
"""
from __future__ import annotations

import requests

STORICO = "https://historical-forecast-api.open-meteo.com/v1/forecast"
PREVISIONE = "https://api.open-meteo.com/v1/forecast"
VAR_AMBIENTE = ["cape", "convective_inhibition", "lifted_index", "freezing_level_height",
                "wind_speed_500hPa", "wind_direction_500hPa", "temperature_850hPa",
                "relative_humidity_700hPa"]


def orario(lat: float, lon: float, variabili: list[str], modello: str, start: str | None = None,
           end: str | None = None, storico: bool = True, **extra) -> dict:
    """Serie oraria su un punto. Con `storico=False` usa la Forecast API (oggi + giorni futuri)."""
    params = dict(latitude=lat, longitude=lon, hourly=",".join(variabili), models=modello, timezone="UTC",
                  **extra)
    if start:
        params |= dict(start_date=start, end_date=end or start)
    r = requests.get(STORICO if storico else PREVISIONE, params=params, timeout=60)
    js = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"Open-Meteo {r.status_code}: {js.get('reason')}")
    return js


def ore_non_nulle(js: dict, variabili: list[str]) -> dict[str, int]:
    h = js.get("hourly", {})
    return {v: sum(x is not None for x in h.get(v, [])) for v in variabili}
