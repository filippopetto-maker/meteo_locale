from dotenv import load_dotenv
load_dotenv()

import os
import logging
from datetime import datetime
from typing import Optional
from supabase import create_client, Client

logger = logging.getLogger(__name__)

_client: Optional[Client] = None

def get_client() -> Client:
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY")
        _client = create_client(url, key)
    return _client

def get_active_stations() -> list[dict]:
    res = get_client().table("stations").select("*").eq("is_active", True).execute()
    return res.data

def insert_observation(station_id, recorded_at, temperature, wind_speed, wind_direction,
                       humidity=None, pressure=None, qc_flag=0, raw_source=None):
    data = {
        "station_id": station_id,
        "recorded_at": recorded_at.isoformat() if hasattr(recorded_at, 'isoformat') else recorded_at,
        "temperature": temperature,
        "wind_speed": wind_speed,
        "wind_direction": wind_direction,
        "humidity": humidity,
        "pressure": pressure,
        "qc_flag": qc_flag,
    }
    res = get_client().table("observations").insert(data).execute()
    return res.data[0]["id"] if res.data else None

def get_observations(station_id, hours=48, qc_ok_only=True):
    from datetime import timezone, timedelta
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    q = get_client().table("observations").select("*")\
        .eq("station_id", station_id)\
        .gte("recorded_at", since)\
        .order("recorded_at")
    if qc_ok_only:
        q = q.lt("qc_flag", 2)
    return q.execute().data

def get_latest_observations() -> list[dict]:
    res = get_client().table("latest_observations").select("*").execute()
    return res.data

def insert_forecast(station_id, forecast_at, valid_for, temperature, wind_speed,
                    wind_direction, humidity=None, model_version="v1", corrected=False):
    data = {
        "station_id": station_id,
        "forecast_at": forecast_at.isoformat() if hasattr(forecast_at, 'isoformat') else forecast_at,
        "valid_for": valid_for.isoformat() if hasattr(valid_for, 'isoformat') else valid_for,
        "temperature": temperature,
        "wind_speed": wind_speed,
        "wind_direction": wind_direction,
        "humidity": humidity,
        "model_version": model_version,
        "corrected": corrected,
    }
    # Upsert su (station_id, valid_for): se la previsione per quella stazione
    # e quell'orario di validità esiste già, viene sovrascritta invece di
    # creare un duplicato. Richiede il vincolo UNIQUE
    # `forecasts_station_valid_unique` su (station_id, valid_for) lato DB.
    res = (
        get_client()
        .table("forecasts")
        .upsert(data, on_conflict="station_id,valid_for")
        .execute()
    )
    return res.data[0]["id"] if res.data else None

def insert_model_metrics(target, horizon_hours, train_mae, train_rmse,
                          val_mae, val_rmse, n_train, n_val,
                          feature_count=None, best_iteration=None,
                          model_version="v1"):
    from datetime import timezone
    data = {
        "target":          target,
        "horizon_hours":   horizon_hours,
        "model_version":   model_version,
        "train_mae":       train_mae,
        "train_rmse":      train_rmse,
        "val_mae":         val_mae,
        "val_rmse":        val_rmse,
        "n_train":         n_train,
        "n_val":           n_val,
        "feature_count":   feature_count,
        "best_iteration":  best_iteration,
        "trained_at":      datetime.now(timezone.utc).isoformat(),
    }
    res = get_client().table("model_metrics").insert(data).execute()
    return res.data[0]["id"] if res.data else None

def health_check() -> bool:
    try:
        get_client().table("stations").select("id").limit(1).execute()
        return True
    except Exception as e:
        logger.error(f"DB health check fallito: {e}")
        return False

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ok = health_check()
    print("✅ Connessione OK" if ok else "❌ Connessione FALLITA")
    if ok:
        stations = get_active_stations()
        print(f"\n📍 Stazioni attive ({len(stations)}):")
        for s in stations:
            print(f"   {s['id']:2d} | {s['name']:<30} | {s['lat']:.4f}, {s['lon']:.4f} | {s['source']}")
