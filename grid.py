"""
grid.py — IDW interpolation and wind component utilities.
Pure module, no DB dependencies.
"""

import numpy as np
import time


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
    chunk_size: int = 20,
    max_retries: int = 3,
) -> dict:
    """
    Fetch ERA5 da archive-api.open-meteo.com per una lista di coordinate.

    Chunking (max chunk_size punti per request) + retry con backoff esponenziale.
    In caso di errore non recuperabile restituisce {} invece di crashare.

    Args:
        lats, lons        : sequenze di float (stessa lunghezza)
        target_hour_utc   : datetime UTC di riferimento (arrotondato all'ora)
        variables         : lista variabili ERA5 (default: ["temperature_2m"])
        chunk_size        : max coordinate per singola request (default 20)
        max_retries       : tentativi per chunk prima di propagare l'eccezione

    Returns:
        dict[str, list[float]] — una lista di valori per ogni variabile,
        nell'ordine corrispondente a lats/lons.
        Ritorna {} se l'intera fetch fallisce (fallback graceful).
    """
    import requests
    from datetime import timedelta

    if variables is None:
        variables = ["temperature_2m"]

    lat_list   = list(lats)
    lon_list   = list(lons)
    target_str = target_hour_utc.strftime("%Y-%m-%dT%H:00")
    start_date = (target_hour_utc.date() - timedelta(days=1)).isoformat()
    end_date   = target_hour_utc.date().isoformat()
    n_chunks   = (len(lat_list) + chunk_size - 1) // chunk_size
    results: dict = {var: [] for var in variables}

    try:
        for chunk_idx, chunk_start in enumerate(range(0, len(lat_list), chunk_size)):
            c_lats = lat_list[chunk_start: chunk_start + chunk_size]
            c_lons = lon_list[chunk_start: chunk_start + chunk_size]
            last_exc = None
            for attempt in range(max_retries):
                try:
                    r = requests.get(
                        "https://archive-api.open-meteo.com/v1/era5",
                        params={
                            "latitude":   ",".join(f"{x:.4f}" for x in c_lats),
                            "longitude":  ",".join(f"{x:.4f}" for x in c_lons),
                            "hourly":     ",".join(variables),
                            "start_date": start_date,
                            "end_date":   end_date,
                            "timezone":   "UTC",
                        },
                        timeout=30,
                    )
                    r.raise_for_status()
                    chunk_data = r.json()
                    if isinstance(chunk_data, dict):
                        chunk_data = [chunk_data]
                    for loc in chunk_data:
                        times = loc["hourly"]["time"]
                        for var in variables:
                            vals = loc["hourly"][var]
                            lookup = dict(zip(times, vals))
                            if target_str in lookup and lookup[target_str] is not None:
                                results[var].append(lookup[target_str])
                            else:
                                results[var].append(
                                    next((v for v in reversed(vals) if v is not None), 0.0)
                                )
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt < max_retries - 1:
                        wait = 10 * (attempt + 1)
                        print(
                            f"  [ERA5] chunk {chunk_idx+1}/{n_chunks} "
                            f"attempt {attempt+1}/{max_retries} fallito, "
                            f"retry in {wait}s — {type(exc).__name__}: {exc}",
                            flush=True,
                        )
                        time.sleep(wait)
                    else:
                        print(
                            f"  [ERA5] chunk {chunk_idx+1}/{n_chunks} "
                            f"fallito dopo {max_retries} tentativi — {exc}",
                            flush=True,
                        )
                        raise last_exc
            if chunk_start + chunk_size < len(lat_list):
                time.sleep(1)
        return results
    except Exception as exc:
        print(
            f"  [ERA5] fetch fallito, fallback a griglia vuota — "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return {}


def build_sea_polygon(lon_min: float, lat_min: float, lat_max: float) -> list:
    """
    Poligono che racchiude il mare: bordo ovest del bbox + coastline (sud→nord).
    Punti in ordine (lat, lon) per restare coerenti col resto del modulo.
    """
    from features import LATIUM_COAST
    coast_sorted = sorted(LATIUM_COAST, key=lambda c: c[0])  # sud → nord
    polygon = [(lat_min, lon_min), (lat_max, lon_min)]
    polygon += [(lat, lon) for lat, lon in reversed(coast_sorted)]  # nord → sud
    polygon.append((lat_min, lon_min))  # chiude il poligono
    return polygon


def is_sea_mask(lat_min, lat_max, lon_min, lon_max, nx, ny) -> np.ndarray:
    """
    Maschera booleana (ny, nx) — True dove la cella è mare.
    Stessa convenzione di compute_idw_grid: riga 0 = lat_max (nord), col 0 = lon_min (ovest).
    """
    from matplotlib.path import Path

    polygon = build_sea_polygon(lon_min, lat_min, lat_max)
    poly_path = Path([(lo, la) for la, lo in polygon])  # Path vuole (x, y) = (lon, lat)

    lats = np.linspace(lat_max, lat_min, ny)
    lons = np.linspace(lon_min, lon_max, nx)
    grid_lon, grid_lat = np.meshgrid(lons, lats)
    points = np.column_stack([grid_lon.ravel(), grid_lat.ravel()])

    mask = poly_path.contains_points(points).reshape(ny, nx)
    return mask


SEA_BLEND_BAND_KM = 10.0  # ampiezza fascia di transizione, SOLO lato mare


def compute_coast_distance_grid(lat_min, lat_max, lon_min, lon_max, nx, ny) -> np.ndarray:
    """
    Distanza in km (non firmata) dal punto di LATIUM_COAST più vicino,
    per ogni cella della griglia fine. Stessa convenzione riga/colonna
    di compute_idw_grid (riga 0 = lat_max nord, col 0 = lon_min ovest).
    """
    from features import LATIUM_COAST

    lats = np.linspace(lat_max, lat_min, ny)
    lons = np.linspace(lon_min, lon_max, nx)
    grid_lon, grid_lat = np.meshgrid(lons, lats)  # (ny, nx)

    coast = np.array(LATIUM_COAST)  # (M, 2) — lat, lon
    R = 6371.0
    lat1 = np.radians(grid_lat[:, :, np.newaxis])
    lon1 = np.radians(grid_lon[:, :, np.newaxis])
    lat2 = np.radians(coast[:, 0])
    lon2 = np.radians(coast[:, 1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    dist = R * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))  # (ny, nx, M)
    return dist.min(axis=2)  # (ny, nx)


def compute_sea_blend_weight(lat_min, lat_max, lon_min, lon_max, nx, ny,
                              band_km: float = SEA_BLEND_BAND_KM) -> np.ndarray:
    """
    Peso w (ny, nx) in [0, 1] per il blend SST.

    w = 0 su tutta la terraferma (nessuna diluizione delle stazioni, anche
    quelle a pochi metri dalla costa). w cresce da 0 a 1 SOLO andando
    verso mare aperto, su una fascia di band_km — continuità garantita
    esattamente sulla linea di costa (a distanza 0 il blend coincide col
    valore di terra).
    """
    dist_km = compute_coast_distance_grid(lat_min, lat_max, lon_min, lon_max, nx, ny)
    sea = is_sea_mask(lat_min, lat_max, lon_min, lon_max, nx, ny)

    signed = np.where(sea, dist_km, -dist_km)
    t = np.clip(signed / band_km, 0.0, 1.0)
    w = t * t * (3 - 2 * t)  # smoothstep
    return w


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
