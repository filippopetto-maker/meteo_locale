"""
export_static.py — Esporta dati da Supabase in JSON statici per il sito.

Output:
  docs/data/latest.json    — stazioni + griglia temperatura
  docs/data/wind_grid.json — griglia vento formato leaflet-velocity

Utilizzo:
  python3 scripts/export_static.py
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from db import get_client, get_active_stations
from grid import compute_idw_grid, wind_to_uv, fetch_era5_batch, bilinear_to_fine, compute_sea_blend_weight, SEA_BLEND_BAND_KM
from sst import get_sst_values, SST_POINTS

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Bounding box Lazio completo
LAT_MIN, LAT_MAX = 41.18, 42.85
LON_MIN, LON_MAX = 11.40, 14.05

# Dimensioni griglia temperatura fine (output)
# Risoluzione invariata (~0.0077° lat, ~0.0107° lon) su area più grande
NX, NY = 250, 220

# Griglia di sfondo ERA5 (coarse, poi interpolata a NX×NY; spacing ~0.1°)
N_BG_LAT = 17   # (42.85 - 41.18) / 0.1 ≈ 17 righe
N_BG_LON = 27   # (14.05 - 11.40) / 0.1 ≈ 27 colonne  (17×27 = 459 punti)

# Bbox griglia vento — esclude mare aperto tirrenico a ovest (lon_sw=11.55)
WIND_LAT_MIN = 41.18
WIND_LAT_MAX = 42.85
WIND_LON_MIN = 11.55   # costa più occidentale del Lazio (S.Marinella/Civitavecchia)
WIND_LON_MAX = 14.05
WIND_NY = 24   # ~0.07° spacing su 1.67° lat
WIND_NX = 36   # ~0.07° spacing su 2.50° lon

DOCS_DATA = _PROJECT_ROOT / "docs" / "data"
MAX_AGE_H = 2  # dati oltre questa soglia sono esclusi dalla griglia IDW


def fetch_latest_forecasts(station_ids: list[int]) -> dict[int, dict]:
    """Ritorna l'ultima previsione (entro MAX_AGE_H) per ogni stazione."""
    client = get_client()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_H)).isoformat()
    result: dict[int, dict] = {}
    for sid in station_ids:
        rows = (
            client.table("forecasts")
            .select("*")
            .eq("station_id", sid)
            .gte("forecast_at", cutoff)
            .order("forecast_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        if rows:
            result[sid] = rows[0]
    return result


def fetch_latest_observations(station_ids: list[int]) -> dict[int, dict]:
    """Ritorna l'ultima osservazione QC-ok (entro MAX_AGE_H) per ogni stazione."""
    client = get_client()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_H)).isoformat()
    result: dict[int, dict] = {}
    for sid in station_ids:
        rows = (
            client.table("observations")
            .select("*")
            .eq("station_id", sid)
            .lt("qc_flag", 2)
            .gte("recorded_at", cutoff)
            .order("recorded_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        if rows:
            result[sid] = rows[0]
    return result


def build_latest_json(
    stations: list[dict],
    forecasts: dict,
    observations: dict,
    temp_grid_observed_data: dict | None = None,
    temp_grid_forecast_data: dict | None = None,
    humidity_grid_data: dict | None = None,
) -> dict:
    station_list = []
    for s in stations:
        sid = s["id"]
        fc = forecasts.get(sid)
        obs = observations.get(sid)
        entry: dict = {
            "id": sid,
            "name": s["name"],
            "lat": s["lat"],
            "lon": s["lon"],
            "microclima": s.get("microclima", "standard"),
        }
        if fc:
            entry["forecast"] = {
                "temperature": fc.get("temperature"),
                "wind_speed": fc.get("wind_speed"),
                "wind_direction": fc.get("wind_direction"),
                "humidity": fc.get("humidity"),
                "valid_for": fc.get("valid_for"),
            }
        if obs:
            entry["observation"] = {
                "temperature": obs.get("temperature"),
                "recorded_at": obs.get("recorded_at"),
            }
        station_list.append(entry)

    payload: dict = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stations": station_list,
    }
    if temp_grid_observed_data:
        payload["temp_grid_observed"] = temp_grid_observed_data
    if temp_grid_forecast_data:
        payload["temp_grid_forecast"] = temp_grid_forecast_data
    if humidity_grid_data:
        payload["humidity_grid"] = humidity_grid_data
    return payload


def build_wind_grid_json(stations: list[dict], forecasts: dict) -> list:
    NX, NY = WIND_NX, WIND_NY
    dx = round((WIND_LON_MAX - WIND_LON_MIN) / (NX - 1), 5)
    dy = round((WIND_LAT_MAX - WIND_LAT_MIN) / (NY - 1), 5)

    wind_points, u_values, v_values = [], [], []
    for s in stations:
        fc = forecasts.get(s["id"])
        if (
            fc
            and fc.get("wind_speed") is not None
            and fc.get("wind_direction") is not None
        ):
            speed_ms = fc["wind_speed"] / 3.6  # km/h → m/s
            u, v = wind_to_uv(speed_ms, fc["wind_direction"])
            wind_points.append([s["lat"], s["lon"]])
            u_values.append(float(u))
            v_values.append(float(v))

    if len(wind_points) < 2:
        log.warning(
            f"Solo {len(wind_points)} stazioni valide per vento "
            "(< 2) — wind_grid.json con array vuoti"
        )
        u_flat: list = []
        v_flat: list = []
    else:
        u_grid = compute_idw_grid(
            wind_points, u_values,
            WIND_LAT_MIN, WIND_LAT_MAX, WIND_LON_MIN, WIND_LON_MAX,
            nx=NX, ny=NY,
        )
        v_grid = compute_idw_grid(
            wind_points, v_values,
            WIND_LAT_MIN, WIND_LAT_MAX, WIND_LON_MIN, WIND_LON_MAX,
            nx=NX, ny=NY,
        )
        u_flat = [round(v, 4) for v in u_grid.flatten().tolist()]
        v_flat = [round(v, 4) for v in v_grid.flatten().tolist()]

    base_header = {
        "la1": WIND_LAT_MAX,
        "la2": WIND_LAT_MIN,
        "lo1": WIND_LON_MIN,
        "lo2": WIND_LON_MAX,
        "nx": NX,
        "ny": NY,
        "dx": dx,
        "dy": dy,
    }
    return [
        {
            "header": {
                **base_header,
                "parameterCategory": 2,
                "parameterNumber": 2,
                "parameterNumberName": "eastward_wind",
                "parameterUnit": "m.s-1",
            },
            "data": u_flat,
        },
        {
            "header": {
                **base_header,
                "parameterCategory": 2,
                "parameterNumber": 3,
                "parameterNumberName": "northward_wind",
                "parameterUnit": "m.s-1",
            },
            "data": v_flat,
        },
    ]


def main() -> None:
    DOCS_DATA.mkdir(parents=True, exist_ok=True)

    log.info("Caricamento stazioni attive...")
    stations = get_active_stations()
    if not stations:
        log.error("Nessuna stazione attiva trovata")
        sys.exit(1)
    log.info(f"  {len(stations)} stazioni attive")

    station_ids = [s["id"] for s in stations]

    log.info("Fetch previsioni recenti...")
    forecasts = fetch_latest_forecasts(station_ids)
    log.info(f"  {len(forecasts)}/{len(stations)} stazioni con forecast recente")

    log.info("Fetch osservazioni recenti...")
    observations = fetch_latest_observations(station_ids)
    log.info(f"  {len(observations)}/{len(stations)} stazioni con observation recente")

    log.info("Calcolo griglia temperatura (ERA5 background + IDW correzioni)...")
    temp_grid_observed = None
    temp_grid_forecast = None

    # Stazioni con observation e/o forecast di temperatura valido (unione, non intersezione)
    stations_with_data = []
    for s in stations:
        fc  = forecasts.get(s["id"])
        obs = observations.get(s["id"])
        has_fc  = fc  is not None and fc.get("temperature")  is not None
        has_obs = obs is not None and obs.get("temperature") is not None
        if has_fc or has_obs:
            stations_with_data.append({
                **s,
                "forecast":    fc  if has_fc  else None,
                "observation": obs if has_obs else None,
                "has_fc":      has_fc,
                "has_obs":     has_obs,
            })

    if len(stations_with_data) >= 2:
        now_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

        # ── 1. Griglia coarse ERA5 ──────────────────────────────────────────
        bg_lats = np.linspace(LAT_MAX, LAT_MIN, N_BG_LAT)
        bg_lons = np.linspace(LON_MIN, LON_MAX, N_BG_LON)
        bg_lat_grid, bg_lon_grid = np.meshgrid(bg_lats, bg_lons, indexing="ij")
        bg_lats_flat = bg_lat_grid.flatten().tolist()
        bg_lons_flat = bg_lon_grid.flatten().tolist()

        # ── 2. Coordinate stazioni (unione obs+forecast) ─────────────────────
        st_lats = [st["lat"] for st in stations_with_data]
        st_lons = [st["lon"] for st in stations_with_data]

        # ── 3. Fetch ERA5 batch ────────────────────────────────────────────────
        log.info(f"  Fetch ERA5 ({N_BG_LAT * N_BG_LON + len(st_lats)} punti, 2 variabili)...")
        all_lats = bg_lats_flat + st_lats
        all_lons = bg_lons_flat + st_lons
        era5_data = fetch_era5_batch(
            all_lats, all_lons, now_utc,
            variables=["temperature_2m", "relativehumidity_2m"],
        )
        if not era5_data:
            log.warning("ERA5 non disponibile — le griglie useranno IDW puro sui dati stazione")

        fine_lats = np.linspace(LAT_MAX, LAT_MIN, NY)
        fine_lons = np.linspace(LON_MIN, LON_MAX, NX)

        n_bg = len(bg_lats_flat)
        if era5_data:
            era5_temp_flat = np.array(era5_data["temperature_2m"])
            era5_hum_flat  = np.array(era5_data["relativehumidity_2m"])
            era5_bg_flat   = era5_temp_flat[:n_bg]
            era5_coarse    = era5_bg_flat.reshape(N_BG_LAT, N_BG_LON)
            era5_fine      = bilinear_to_fine(era5_coarse, bg_lats, bg_lons, fine_lats, fine_lons)

        # ── Blend mare (SST), condiviso da entrambe le griglie ────────────────
        sst_values = get_sst_values()
        blend_w  = None
        sst_grid = None
        if sst_values and len(sst_values) >= 2:
            sst_points_arr = np.array([[p["lat"], p["lon"]] for p in SST_POINTS if p["name"] in sst_values])
            sst_vals_arr   = np.array([sst_values[p["name"]] for p in SST_POINTS if p["name"] in sst_values])
            sst_grid = compute_idw_grid(
                sst_points_arr, sst_vals_arr,
                LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, NX, NY,
            )
            blend_w = compute_sea_blend_weight(LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, NX, NY)
        else:
            log.warning("SST non disponibile — griglie senza blend mare (comportamento legacy)")

        def _build_temp_grid(mask: np.ndarray, values: np.ndarray) -> dict | None:
            """Costruisce un temp_grid_data da un sottoinsieme di stazioni
            (osservato o forecast, indifferente al chiamante)."""
            if mask.sum() < 2:
                return None
            pts = np.array(list(zip(np.array(st_lats)[mask], np.array(st_lons)[mask])))
            if era5_data:
                era5_at_subset = era5_temp_flat[n_bg:][mask]
                corrections = values[mask] - era5_at_subset
                corr_grid = compute_idw_grid(
                    pts, corrections,
                    LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, NX, NY,
                )
                grid = era5_fine + corr_grid
            else:
                grid = compute_idw_grid(
                    pts, values[mask],
                    LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, NX, NY,
                )
            if blend_w is not None and sst_grid is not None:
                grid = blend_w * sst_grid + (1 - blend_w) * grid
            return {
                "lat_min": LAT_MIN, "lat_max": LAT_MAX,
                "lon_min": LON_MIN, "lon_max": LON_MAX,
                "nx": NX, "ny": NY,
                "values": [round(v, 2) for v in grid.flatten().tolist()],
                "t_min": round(float(grid.min()), 2),
                "t_max": round(float(grid.max()), 2),
            }

        obs_mask = np.array([st["has_obs"] for st in stations_with_data])
        fc_mask  = np.array([st["has_fc"]  for st in stations_with_data])
        obs_vals = np.array([
            st["observation"]["temperature"] if st["has_obs"] else np.nan
            for st in stations_with_data
        ])
        fc_vals  = np.array([
            st["forecast"]["temperature"] if st["has_fc"] else np.nan
            for st in stations_with_data
        ])

        temp_grid_observed = _build_temp_grid(obs_mask, obs_vals)
        temp_grid_forecast = _build_temp_grid(fc_mask,  fc_vals)

        if temp_grid_observed is None:
            log.warning(f"Solo {int(obs_mask.sum())} stazioni con osservazione valida (< 2) — temp_grid_observed non incluso")
        if temp_grid_forecast is None:
            log.warning(f"Solo {int(fc_mask.sum())} stazioni con forecast valido (< 2) — temp_grid_forecast non incluso")

        # ── Umidità (resta solo forecast, nessun "osservato" per ora) ────────
        hum_mask = fc_mask & np.array([
            (st.get("forecast") or {}).get("humidity") is not None
            for st in stations_with_data
        ])
        if hum_mask.sum() >= 2:
            hum_values = np.array([
                st["forecast"]["humidity"]
                for st, m in zip(stations_with_data, hum_mask) if m
            ])
            if era5_data:
                era5_hum_at_st  = era5_hum_flat[n_bg:][hum_mask]
                hum_corr        = hum_values - era5_hum_at_st
                era5_hum_coarse = era5_hum_flat[:n_bg].reshape(N_BG_LAT, N_BG_LON)
                era5_hum_fine   = bilinear_to_fine(
                    era5_hum_coarse, bg_lats, bg_lons, fine_lats, fine_lons
                )
                hum_corr_grid = compute_idw_grid(
                    np.array(list(zip(np.array(st_lats)[hum_mask], np.array(st_lons)[hum_mask]))), hum_corr,
                    LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, NX, NY,
                )
                hum_grid = np.clip(era5_hum_fine + hum_corr_grid, 0, 100)
            else:
                hum_grid = np.clip(
                    compute_idw_grid(
                        np.array(list(zip(np.array(st_lats)[hum_mask], np.array(st_lons)[hum_mask]))), hum_values,
                        LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, NX, NY,
                    ),
                    0, 100,
                )
            humidity_grid_data = {
                "lat_min": LAT_MIN, "lat_max": LAT_MAX,
                "lon_min": LON_MIN, "lon_max": LON_MAX,
                "nx": NX, "ny": NY,
                "values": [round(v, 1) for v in hum_grid.flatten().tolist()],
                "h_min": round(float(hum_grid.min()), 1),
                "h_max": round(float(hum_grid.max()), 1),
            }
        else:
            log.warning(f"Solo {int(hum_mask.sum())} stazioni valide per umidità (< 2) — humidity_grid non incluso")
            humidity_grid_data = None
    else:
        log.warning(f"Solo {len(stations_with_data)} stazioni con dati validi (< 2) — temp_grid non incluso")
        humidity_grid_data = None

    log.info("Calcolo latest.json...")
    latest = build_latest_json(
        stations, forecasts, observations,
        temp_grid_observed, temp_grid_forecast, humidity_grid_data,
    )
    latest_path = DOCS_DATA / "latest.json"
    latest_path.write_text(json.dumps(latest, indent=2, ensure_ascii=False))
    log.info(f"  Scritto {latest_path}")

    log.info("Calcolo wind_grid.json...")
    wind = build_wind_grid_json(stations, forecasts)
    wind_path = DOCS_DATA / "wind_grid.json"
    wind_path.write_text(json.dumps(wind, separators=(",", ":"), ensure_ascii=False))
    log.info(f"  Scritto {wind_path}")

    log.info("Export completato.")


if __name__ == "__main__":
    main()
