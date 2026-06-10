"""
grid.py — IDW interpolation and wind component utilities.
Pure module, no DB dependencies.
"""

import numpy as np


def compute_idw_grid(points, values, lat_min, lat_max, lon_min, lon_max, nx, ny, power=2):
    """
    Inverse Distance Weighting interpolation on a regular grid.

    Parameters
    ----------
    points : array-like, shape (N, 2)  — (lat, lon) of each observation
    values : array-like, shape (N,)    — scalar values at each point
    lat_min, lat_max, lon_min, lon_max : float — bounding box
    nx, ny : int — grid dimensions (columns = nx, rows = ny)
    power  : float — IDW exponent (default 2)

    Returns
    -------
    np.ndarray shape (ny, nx)
        Row 0 = lat_max (north), col 0 = lon_min (west).
    """
    pts = np.asarray(points, dtype=float)   # (N, 2)
    vals = np.asarray(values, dtype=float)  # (N,)

    # lats descending (north→south), lons ascending (west→east)
    lats = np.linspace(lat_max, lat_min, ny)  # (ny,)
    lons = np.linspace(lon_min, lon_max, nx)  # (nx,)

    # grid_lat[i,j], grid_lon[i,j]
    grid_lon, grid_lat = np.meshgrid(lons, lats)  # both (ny, nx)

    # Vectorised IDW: compute all distances at once
    # pts[:,0] = lat, pts[:,1] = lon
    # Expand dims for broadcast: (ny, nx, 1) vs (N,)
    dlat = grid_lat[:, :, np.newaxis] - pts[:, 0]  # (ny, nx, N)
    dlon = grid_lon[:, :, np.newaxis] - pts[:, 1]  # (ny, nx, N)
    dists = np.sqrt(dlat ** 2 + dlon ** 2)          # (ny, nx, N)

    # Handle exact station hits
    exact = dists == 0  # (ny, nx, N)
    hit_cells = exact.any(axis=2)  # (ny, nx)

    weights = np.where(exact, 0.0, 1.0 / np.where(dists == 0, 1.0, dists) ** power)
    w_sum = weights.sum(axis=2)  # (ny, nx)
    grid = np.einsum('ijk,k->ij', weights, vals) / np.where(w_sum == 0, 1.0, w_sum)

    # For exact hits, use the station value directly
    if hit_cells.any():
        hit_idx = np.argmax(exact, axis=2)  # (ny, nx) — index of first hit
        grid = np.where(hit_cells, vals[hit_idx], grid)

    return grid


def wind_to_uv(speed_ms, direction_deg):
    """
    Convert wind speed + meteorological direction to U, V components.

    Meteorological convention: direction is where wind COMES FROM.
      U = -speed * sin(dir_rad)   [positive = wind towards east]
      V = -speed * cos(dir_rad)   [positive = wind towards north]

    Parameters
    ----------
    speed_ms      : float or array-like — wind speed in m/s
    direction_deg : float or array-like — direction in degrees (met. convention)

    Returns
    -------
    (U, V) : tuple of float or ndarray
    """
    dir_rad = np.deg2rad(direction_deg)
    u = -np.asarray(speed_ms) * np.sin(dir_rad)
    v = -np.asarray(speed_ms) * np.cos(dir_rad)
    return u, v


def fetch_era5_batch(
    lats: list,
    lons: list,
    target_hour_utc,
    variables: list | None = None,
) -> dict:
    """
    Fetch ERA5 per una lista di punti e variabili in un singolo request batch.
    Open-Meteo supporta lat/lon multipli separati da virgola.

    Parameters
    ----------
    lats, lons       : coordinate dei punti (stessa lunghezza)
    target_hour_utc  : datetime UTC; si cerca questa ora esatta nel risultato
    variables        : lista di variabili hourly Open-Meteo
                       (default: ["temperature_2m"])

    Returns
    -------
    dict {variable: [float, ...]}  — un valore per punto, stesso ordine di lats/lons
    """
    import requests

    if variables is None:
        variables = ["temperature_2m"]

    params = {
        "latitude":      ",".join(f"{x:.4f}" for x in lats),
        "longitude":     ",".join(f"{x:.4f}" for x in lons),
        "hourly":        ",".join(variables),
        "forecast_days": 1,
        "past_days":     1,
        "timezone":      "UTC",
    }
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params=params, timeout=60,
    )
    r.raise_for_status()
    data = r.json()

    # Un solo punto → Open-Meteo restituisce dict; più punti → lista di dict
    if isinstance(data, dict):
        data = [data]

    target_str = target_hour_utc.strftime("%Y-%m-%dT%H:00")
    results: dict[str, list] = {var: [] for var in variables}
    for loc in data:
        times = loc["hourly"]["time"]
        for var in variables:
            vals = loc["hourly"][var]
            lookup = dict(zip(times, vals))
            if target_str in lookup and lookup[target_str] is not None:
                results[var].append(lookup[target_str])
            else:
                # Fallback: valore non-None più recente
                results[var].append(
                    next((v for v in reversed(vals) if v is not None), 0.0)
                )
    return results


def bilinear_to_fine(
    coarse: np.ndarray,
    coarse_lats: np.ndarray,
    coarse_lons: np.ndarray,
    fine_lats: np.ndarray,
    fine_lons: np.ndarray,
) -> np.ndarray:
    """
    Interpolazione bilineare da griglia coarse a griglia fine.

    Parameters
    ----------
    coarse      : shape (n_lat_coarse, n_lon_coarse), row 0 = lat massima (nord)
    coarse_lats : 1-D array, latitudini coarse (nord→sud, decrescenti)
    coarse_lons : 1-D array, longitudini coarse (ovest→est, crescenti)
    fine_lats   : 1-D array, latitudini griglia fine (nord→sud, decrescenti)
    fine_lons   : 1-D array, longitudini griglia fine (ovest→est, crescenti)

    Returns
    -------
    np.ndarray shape (len(fine_lats), len(fine_lons))
        Row 0 = fine_lats[0] (nord).
    """
    from scipy.interpolate import RegularGridInterpolator

    # RegularGridInterpolator vuole latitudini in ordine crescente
    lats_asc = coarse_lats[::-1]
    grid_asc = coarse[::-1, :]

    interp = RegularGridInterpolator(
        (lats_asc, coarse_lons), grid_asc,
        method="linear", bounds_error=False, fill_value=None,
    )
    pts = np.array([[la, lo] for la in fine_lats for lo in fine_lons])
    return interp(pts).reshape(len(fine_lats), len(fine_lons))
