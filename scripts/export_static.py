"""
export_static.py — Esporta dati da Supabase in JSON statici per il sito.

Output:
  docs/data/latest.json        — stazioni + griglia temperatura
  docs/data/wind_grid.json     — griglia vento formato leaflet-velocity
  docs/data/forecast_48h.json  — previsioni M2 1–48 h (scorrimento orario della mappa)

Dal 31/10/2026 la +1h (latest.json, wind_grid.json) e forecast_48h.json vengono da una sola
lettura di forecasts_48h, ultima emissione: i tre file sono sempre della stessa emissione anche
se produce_m2 scrive mentre l'export gira. Se forecasts_48h è vecchia (oltre MAX_AGE_H, es.
ritorno indietro a inference.yml) la +1h si legge da forecasts e forecast_48h.json non si scrive.

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

from db import get_client, get_active_stations, get_forecasts_48h
import export_forecast_48h  # noqa: E402  (stessa cartella scripts/)
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
            .eq("lead_hours", 1)
            .gte("forecast_at", cutoff)
            .order("forecast_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        if rows:
            result[sid] = rows[0]
    return result


def fetch_m2_emission() -> tuple[dict[int, dict], list[dict]] | None:
    """Ultima emissione di forecasts_48h, se entro MAX_AGE_H: (lead 1 per stazione nel formato di
    forecasts, righe 1–48 h dell'emissione). None se la tabella è vuota o vecchia."""
    rows = get_forecasts_48h()
    if not rows:
        return None
    newest = max(datetime.fromisoformat(r["issue_at"]) for r in rows)
    if datetime.now(timezone.utc) - newest > timedelta(hours=MAX_AGE_H):
        log.warning(f"forecasts_48h vecchia (emissione {newest:%Y-%m-%d %H:%M}Z): +1h da forecasts")
        return None
    # Le ore di un'emissione precedente non ancora cancellate (valid_for = issue_at nuova) si scartano
    rows = [r for r in rows if datetime.fromisoformat(r["issue_at"]) == newest]
    lead1 = {
        r["station_id"]: {
            "station_id": r["station_id"], "forecast_at": r["issue_at"], "valid_for": r["valid_for"],
            "temperature": r["temperature"], "humidity": r["humidity"],
            "wind_speed": r["wind_speed"], "wind_direction": r["wind_direction"],
            "lead_hours": 1, "model_version": r["model_tag"], "corrected": False,
        }
        for r in rows if r["lead_hours"] == 1
    }
    return lead1, rows


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
    humidity_grid_forecast_data: dict | None = None,
    humidity_grid_observed_data: dict | None = None,
    wind_speed_grid_data: dict | None = None,
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
                "humidity": obs.get("humidity"),
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
    if humidity_grid_observed_data:
        payload["humidity_grid_observed"] = humidity_grid_observed_data
    if humidity_grid_forecast_data:
        payload["humidity_grid_forecast"] = humidity_grid_forecast_data
    if wind_speed_grid_data:
        payload["wind_speed_grid"] = wind_speed_grid_data
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
    m2 = fetch_m2_emission()
    if m2:
        forecasts = {sid: fc for sid, fc in m2[0].items() if sid in set(station_ids)}
        log.info(f"  {len(forecasts)}/{len(stations)} stazioni con +1h M2 (emissione {m2[1][0]['issue_at']})")
    else:
        forecasts = fetch_latest_forecasts(station_ids)
        log.info(f"  {len(forecasts)}/{len(stations)} stazioni con forecast recente (tabella forecasts)")

    log.info("Fetch osservazioni recenti...")
    observations = fetch_latest_observations(station_ids)
    log.info(f"  {len(observations)}/{len(stations)} stazioni con observation recente")

    log.info("Calcolo griglia temperatura (ERA5 background + IDW correzioni)...")
    temp_grid_observed = None
    temp_grid_forecast = None
    humidity_grid_observed = None
    humidity_grid_forecast = None

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
        log.info(f"  Fetch ERA5 ({N_BG_LAT * N_BG_LON + len(st_lats)} punti, 3 variabili)...")
        all_lats = bg_lats_flat + st_lats
        all_lons = bg_lons_flat + st_lons
        era5_data = fetch_era5_batch(
            all_lats, all_lons, now_utc,
            variables=["temperature_2m", "relativehumidity_2m", "windspeed_10m"],
        )
        if not era5_data:
            log.warning("ERA5 non disponibile — le griglie useranno IDW puro sui dati stazione")

        fine_lats = np.linspace(LAT_MAX, LAT_MIN, NY)
        fine_lons = np.linspace(LON_MIN, LON_MAX, NX)

        n_bg = len(bg_lats_flat)
        if era5_data:
            era5_temp_flat = np.array(era5_data["temperature_2m"])
            era5_hum_flat  = np.array(era5_data["relativehumidity_2m"])
            era5_ws_flat   = np.array(era5_data["windspeed_10m"])
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

        def _build_hum_grid(mask: np.ndarray, values: np.ndarray) -> dict | None:
            """Costruisce una humidity_grid da un sottoinsieme di stazioni
            (osservata o forecast, indifferente al chiamante).

            Nota deliberata: nessun blend SST sull'umidità. _build_temp_grid applica
            blend_w/sst_grid perché la SST è una temperatura; non esiste un equivalente
            per l'umidità relativa."""
            if mask.sum() < 2:
                return None
            pts = np.array(list(zip(np.array(st_lats)[mask], np.array(st_lons)[mask])))
            if era5_data:
                era5_hum_at_subset = era5_hum_flat[n_bg:][mask]
                corrections = values[mask] - era5_hum_at_subset
                era5_hum_coarse = era5_hum_flat[:n_bg].reshape(N_BG_LAT, N_BG_LON)
                era5_hum_fine = bilinear_to_fine(
                    era5_hum_coarse, bg_lats, bg_lons, fine_lats, fine_lons
                )
                corr_grid = compute_idw_grid(
                    pts, corrections,
                    LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, NX, NY,
                )
                grid = np.clip(era5_hum_fine + corr_grid, 0, 100)
            else:
                grid = np.clip(
                    compute_idw_grid(
                        pts, values[mask],
                        LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, NX, NY,
                    ),
                    0, 100,
                )
            return {
                "lat_min": LAT_MIN, "lat_max": LAT_MAX,
                "lon_min": LON_MIN, "lon_max": LON_MAX,
                "nx": NX, "ny": NY,
                "values": [round(v, 1) for v in grid.flatten().tolist()],
                "h_min": round(float(grid.min()), 1),
                "h_max": round(float(grid.max()), 1),
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

        # ── Umidità: due griglie (osservata + forecast), stesso pattern della temperatura
        hum_obs_mask = np.array([
            st["has_obs"] and (st.get("observation") or {}).get("humidity") is not None
            for st in stations_with_data
        ])
        hum_fc_mask = np.array([
            st["has_fc"] and (st.get("forecast") or {}).get("humidity") is not None
            for st in stations_with_data
        ])
        hum_obs_vals = np.array([
            (st.get("observation") or {}).get("humidity") if m else np.nan
            for st, m in zip(stations_with_data, hum_obs_mask)
        ], dtype=float)
        hum_fc_vals = np.array([
            (st.get("forecast") or {}).get("humidity") if m else np.nan
            for st, m in zip(stations_with_data, hum_fc_mask)
        ], dtype=float)

        humidity_grid_observed = _build_hum_grid(hum_obs_mask, hum_obs_vals)
        humidity_grid_forecast = _build_hum_grid(hum_fc_mask, hum_fc_vals)

        if humidity_grid_observed is None:
            log.warning(f"Solo {int(hum_obs_mask.sum())} stazioni con umidità osservata (< 2) — humidity_grid_observed non incluso")
        if humidity_grid_forecast is None:
            log.warning(f"Solo {int(hum_fc_mask.sum())} stazioni con umidità prevista (< 2) — humidity_grid_forecast non incluso")

        log.info(f"  Umidità: {int(hum_obs_mask.sum())} stazioni osservate, {int(hum_fc_mask.sum())} previste")

        # ── 9. Griglia wind_speed (ERA5 background + IDW correzioni) ────────────
        wind_mask = fc_mask & np.array([
            (st.get("forecast") or {}).get("wind_speed") is not None
            for st in stations_with_data
        ])
        wind_speed_grid_data: dict | None = None
        if wind_mask.sum() >= 2:
            ws_values = np.array([
                st["forecast"]["wind_speed"]
                for st, m in zip(stations_with_data, wind_mask) if m
            ])
            if era5_data and "windspeed_10m" in era5_data:
                era5_ws_at_st  = era5_ws_flat[n_bg:][wind_mask]
                ws_corr        = ws_values - era5_ws_at_st
                era5_ws_coarse = era5_ws_flat[:n_bg].reshape(N_BG_LAT, N_BG_LON)
                era5_ws_fine   = bilinear_to_fine(
                    era5_ws_coarse, bg_lats, bg_lons, fine_lats, fine_lons
                )
                ws_corr_grid = compute_idw_grid(
                    np.array(list(zip(np.array(st_lats)[wind_mask], np.array(st_lons)[wind_mask]))),
                    ws_corr,
                    LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, NX, NY,
                )
                ws_grid = np.clip(era5_ws_fine + ws_corr_grid, 0, None)
            else:
                ws_grid = np.clip(
                    compute_idw_grid(
                        np.array(list(zip(np.array(st_lats)[wind_mask], np.array(st_lons)[wind_mask]))),
                        ws_values,
                        LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, NX, NY,
                    ),
                    0, None,
                )
            wind_speed_grid_data = {
                "lat_min": LAT_MIN, "lat_max": LAT_MAX,
                "lon_min": LON_MIN, "lon_max": LON_MAX,
                "nx": NX, "ny": NY,
                "values": [round(v, 1) for v in ws_grid.flatten().tolist()],
                "ws_min": round(float(ws_grid.min()), 1),
                "ws_max": round(float(ws_grid.max()), 1),
            }
        else:
            log.warning(f"Solo {int(wind_mask.sum())} stazioni valide per wind_speed (< 2) — wind_speed_grid non incluso")
            wind_speed_grid_data = None
    else:
        log.warning(f"Solo {len(stations_with_data)} stazioni con dati validi (< 2) — temp_grid non incluso")
        humidity_grid_observed = None
        humidity_grid_forecast = None
        wind_speed_grid_data = None

    log.info("Calcolo latest.json...")
    latest = build_latest_json(
        stations, forecasts, observations,
        temp_grid_observed, temp_grid_forecast,
        humidity_grid_forecast, humidity_grid_observed,
        wind_speed_grid_data,
    )
    latest_path = DOCS_DATA / "latest.json"
    latest_path.write_text(json.dumps(latest, indent=2, ensure_ascii=False))
    log.info(f"  Scritto {latest_path}")

    log.info("Calcolo wind_grid.json...")
    wind = build_wind_grid_json(stations, forecasts)
    wind_path = DOCS_DATA / "wind_grid.json"
    wind_path.write_text(json.dumps(wind, separators=(",", ":"), ensure_ascii=False))
    log.info(f"  Scritto {wind_path}")

    if m2:
        try:  # facoltativo: un errore qui non deve far fallire latest.json e wind_grid.json
            f48 = export_forecast_48h.build(m2[1], {s["id"]: s["name"] for s in stations})
            f48_path = DOCS_DATA / "forecast_48h.json"
            f48_path.write_text(json.dumps(f48, separators=(",", ":")))
            log.info(f"  Scritto {f48_path} ({len(f48['stations'])} stazioni, emissione {f48['issue_at']})")
        except Exception as exc:
            log.error(f"  forecast_48h.json non scritto ({exc}): resta la copia precedente")

    log.info("Export completato.")


def fetch_dashboard_series(station_ids: list[int], days: int = 7) -> dict:
    """Recupera serie storiche forecast+observed per ogni stazione (ultimi `days` giorni)."""
    client = get_client()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result: dict[str, dict] = {}
    for sid in station_ids:
        fc_rows = (
            client.table("forecasts")
            .select("valid_for, temperature, humidity, wind_speed, forecast_at")
            .eq("station_id", sid)
            .eq("lead_hours", 1)
            .gte("valid_for", since)
            .order("valid_for")
            .execute()
            .data
        )
        # Dedup per valid_for tenendo il forecast_at più recente
        fc_by_valid: dict[str, dict] = {}
        for row in fc_rows:
            vf = row["valid_for"]
            if vf not in fc_by_valid or row["forecast_at"] > fc_by_valid[vf]["forecast_at"]:
                fc_by_valid[vf] = row
        forecast_series = [
            {"t": r["valid_for"], "temp": r["temperature"], "hum": r["humidity"], "ws": r["wind_speed"]}
            for r in sorted(fc_by_valid.values(), key=lambda x: x["valid_for"])
            if r["temperature"] is not None
        ]

        obs_rows = (
            client.table("observations")
            .select("recorded_at, temperature, humidity, wind_speed")
            .eq("station_id", sid)
            .gte("recorded_at", since)
            .lt("qc_flag", 2)
            .order("recorded_at")
            .execute()
            .data
        )
        observed_series = [
            {"t": r["recorded_at"], "temp": r["temperature"], "hum": r["humidity"], "ws": r["wind_speed"]}
            for r in obs_rows
            if r["temperature"] is not None
        ]

        result[str(sid)] = {"forecast": forecast_series, "observed": observed_series}
    return result


def _fetch_valid_window(table: str, columns: str, days: int) -> list[dict]:
    """Righe di `table` con valid_for negli ultimi `days` giorni e già osservabili, paginate a 1000."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    rows, start = [], 0
    while True:
        page = (
            get_client().table(table).select(columns)
            .gte("valid_for", since).lte("valid_for", now.isoformat())
            .order("station_id").order("issue_at").order("lead_hours")
            .range(start, start + 999).execute().data
        )
        rows += page
        if len(page) < 1000:
            return rows
        start += 1000


def fetch_lead_rows(days: int = 7) -> list[dict]:
    """Previsioni 1–48 h per l'errore per anticipo, emissioni delle 09 e 21 UTC, nel formato di
    forecasts_shadow (t_m2/t_ifs, h_m2/h_ifs, w_m2/w_ifs).

    Fonte: forecasts_verifica (la previsione pubblicata, scritta da produce_m2) quando copre tutta la
    finestra, cioè quando la sua emissione più vecchia risale ad almeno days + 2 giorni (il +48 h).
    Fino ad allora temperatura e umidità vengono dallo shadow M2 (stessi modelli) e il vento
    dall'archivio, perché lo shadow non lo scrive più dal 31/10/2026."""
    arch = [
        {"station_id": r["station_id"], "valid_for": r["valid_for"], "lead_hours": r["lead_hours"],
         "t_m2": r["temperature"], "t_ifs": r["t_ifs"], "h_m2": r["humidity"], "h_ifs": r["h_ifs"],
         "w_m2": r["wind_speed"], "w_ifs": r["w_ifs"], "issue_at": r["issue_at"]}
        for r in _fetch_valid_window(
            "forecasts_verifica",
            "station_id, issue_at, valid_for, lead_hours, temperature, t_ifs, humidity, h_ifs, wind_speed, w_ifs",
            days)
    ]
    oldest = min((datetime.fromisoformat(r["issue_at"]) for r in arch), default=None)
    if oldest and oldest <= datetime.now(timezone.utc) - timedelta(days=days + 2):
        return arch
    shadow = _fetch_valid_window(
        "forecasts_shadow", "station_id, valid_for, lead_hours, t_m2, t_ifs, h_m2, h_ifs, w_m2, w_ifs", days)
    return shadow + [{**r, "t_m2": None, "h_m2": None} for r in arch]


# Variabile dashboard → (prefisso colonne forecasts_shadow, chiave delle serie)
LEAD_VARS = {"temp": "t", "hum": "h", "wind": "w"}
SERIES_KEY = {"temp": "temp", "hum": "hum", "wind": "ws"}


def build_lead_errors(series: dict, lead_rows: list[dict]) -> dict:
    """MAE per anticipo (1–48 h) di M2 e di IFS grezzo contro la media oraria osservata (±30 min),
    per stazione e su tutte le stazioni. M2 e IFS sono valutati sulle stesse coppie."""
    obs: dict[tuple[int, datetime], dict[str, list]] = {}
    for sid, s in series.items():
        for o in s.get("observed", []):
            t = datetime.fromisoformat(o["t"].replace("Z", "+00:00")) + timedelta(minutes=30)
            cell = obs.setdefault((int(sid), t.replace(minute=0, second=0, microsecond=0)), {})
            for var, key in SERIES_KEY.items():
                if o.get(key) is not None:
                    cell.setdefault(var, []).append(o[key])

    leads = list(range(1, 49))
    acc: dict[str, dict[str, np.ndarray]] = {}  # scope → var → [somma M2, somma IFS, n] × lead
    for r in lead_rows:
        cell = obs.get((r["station_id"], datetime.fromisoformat(r["valid_for"])))
        if not cell:
            continue
        i = r["lead_hours"] - 1
        for var, p in LEAD_VARS.items():
            m2, ifs, o = r.get(f"{p}_m2"), r.get(f"{p}_ifs"), cell.get(var)
            if m2 is None or ifs is None or not o or not 0 <= i < 48:
                continue
            o = sum(o) / len(o)
            for scope in ("all", str(r["station_id"])):
                a = acc.setdefault(scope, {}).setdefault(var, np.zeros((3, 48)))
                a[:, i] += (abs(m2 - o), abs(ifs - o), 1)

    out: dict = {"leads": leads}
    for scope, by_var in acc.items():
        out[scope] = {}
        for var, a in by_var.items():
            n = a[2]
            with np.errstate(invalid="ignore", divide="ignore"):
                m2, ifs = a[0] / n, a[1] / n
            out[scope][var] = {
                "m2": [None if k == 0 else round(float(v), 3) for v, k in zip(m2, n)],
                "ifs": [None if k == 0 else round(float(v), 3) for v, k in zip(ifs, n)],
                "n": [int(k) for k in n],
            }
    return out


def _mae_pairs(fc_list: list[dict], obs_list: list[dict], key: str) -> list[float]:
    """Errori assoluti previsto − osservato: per ogni osservazione la prima previsione entro ±30 min."""
    pairs = []
    for obs in obs_list:
        if obs.get(key) is None:
            continue
        obs_t = datetime.fromisoformat(obs["t"].replace("Z", "+00:00"))
        for fc in fc_list:
            if fc.get(key) is None:
                continue
            fc_t = datetime.fromisoformat(fc["t"].replace("Z", "+00:00"))
            if abs((fc_t - obs_t).total_seconds()) <= 1800:  # ±30 min
                pairs.append(abs(fc[key] - obs[key]))
                break
    return pairs


def build_dashboard_json(stations: list[dict], series: dict, lead_rows: list[dict] | None = None) -> dict:
    """Calcola MAE forecast vs observed e costruisce il payload dashboard."""
    mae_per_station = []
    for s in stations:
        sid = s["id"]
        fc_list = series.get(str(sid), {}).get("forecast", [])
        obs_list = series.get(str(sid), {}).get("observed", [])

        pairs = _mae_pairs(fc_list, obs_list, "temp")
        pairs_hum = _mae_pairs(fc_list, obs_list, "hum")
        pairs_ws = _mae_pairs(fc_list, obs_list, "ws")

        mae_per_station.append({
            "id": sid,
            "name": s["name"],
            "mae": round(sum(pairs) / len(pairs), 3) if pairs else None,
            "n_pairs": len(pairs),
            "mae_hum": round(sum(pairs_hum) / len(pairs_hum), 3) if pairs_hum else None,
            "n_pairs_hum": len(pairs_hum),
            "mae_ws": round(sum(pairs_ws) / len(pairs_ws), 3) if pairs_ws else None,
            "n_pairs_ws": len(pairs_ws),
        })

    def global_mae(field: str, n_field: str) -> float | None:
        vals = [e[field] for e in mae_per_station if e[n_field] > 0]
        return round(sum(vals) / len(vals), 3) if vals else None

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stations": [
            {"id": s["id"], "name": s["name"], "microclima": s.get("microclima", "standard")}
            for s in stations
        ],
        "series": series,
        "mae_per_station": mae_per_station,
        "mae_global": global_mae("mae", "n_pairs"),
        "mae_global_hum": global_mae("mae_hum", "n_pairs_hum"),
        "mae_global_ws": global_mae("mae_ws", "n_pairs_ws"),
        "lead_errors": build_lead_errors(series, lead_rows) if lead_rows else None,
    }


if __name__ == "__main__":
    if "--dashboard-only" in sys.argv:
        DOCS_DATA.mkdir(parents=True, exist_ok=True)
        stations = get_active_stations()
        station_ids = [s["id"] for s in stations]
        dashboard_series = fetch_dashboard_series(station_ids, days=7)
        try:
            lead_rows = fetch_lead_rows(days=7)
        except Exception as e:  # l'errore per anticipo non deve impedire il resto della dashboard
            log.warning(f"previsioni per anticipo non disponibili: {e}")
            lead_rows = None
        dashboard = build_dashboard_json(stations, dashboard_series, lead_rows)
        dashboard_path = DOCS_DATA / "dashboard_data.json"
        dashboard_path.write_text(json.dumps(dashboard, indent=2, ensure_ascii=False))
        log.info(f"Scritto {dashboard_path}")
    else:
        main()
