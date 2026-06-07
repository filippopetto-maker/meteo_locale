"""
dashboard.py — Dashboard Streamlit read-only del modello meteo locale
Blocco 3 - Fase 5

Tre sezioni:
  1. Previsioni più recenti — una riga per stazione attiva.
  2. Previsto vs Osservato (ultime 48h) — line chart per stazione + target.
  3. Metriche modello correnti — ultima riga di model_metrics.

Read-only: nessuna scrittura su Supabase, nessun training, nessun fetch NWP.
Tutti i dati provengono dalle tabelle del DB.

Avvio:
    streamlit run output/dashboard.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# output/ è una subdir: aggiungi project root a sys.path per importare db.py
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import streamlit as st

import db


# ─────────────────────────────────────────────────────────────────────────────
# Helpers cache (TTL breve: dashboard quasi real-time)
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_stations() -> pd.DataFrame:
    rows = db.get_active_stations()
    return pd.DataFrame(rows)


@st.cache_data(ttl=60)
def load_latest_forecast_per_station() -> pd.DataFrame:
    """
    Ultima previsione (forecast_at max) per ogni stazione attiva.
    Usa la tabella forecasts: estrae le righe più recenti.
    """
    client = db.get_client()
    res = (
        client.table("forecasts")
        .select("*")
        .order("forecast_at", desc=True)
        .limit(500)
        .execute()
    )
    df = pd.DataFrame(res.data)
    if df.empty:
        return df
    df["forecast_at"] = pd.to_datetime(df["forecast_at"])
    df["valid_for"]   = pd.to_datetime(df["valid_for"])
    # Una riga per stazione: la più recente per forecast_at.
    df = (
        df.sort_values("forecast_at", ascending=False)
          .groupby("station_id", as_index=False)
          .first()
    )
    return df


@st.cache_data(ttl=60)
def load_recent_forecasts(hours: int = 48) -> pd.DataFrame:
    """Forecast con valid_for nelle ultime `hours` (o future entro la finestra)."""
    client = db.get_client()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    res = (
        client.table("forecasts")
        .select("*")
        .gte("valid_for", since)
        .order("valid_for")
        .execute()
    )
    df = pd.DataFrame(res.data)
    if not df.empty:
        df["valid_for"]   = pd.to_datetime(df["valid_for"])
        df["forecast_at"] = pd.to_datetime(df["forecast_at"])
    return df


@st.cache_data(ttl=60)
def load_recent_observations(station_id: int, hours: int = 48) -> pd.DataFrame:
    rows = db.get_observations(station_id, hours=hours, qc_ok_only=True)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["recorded_at"] = pd.to_datetime(df["recorded_at"])
    return df


@st.cache_data(ttl=60)
def load_latest_metrics() -> pd.DataFrame:
    """
    Una riga per (target, horizon_hours) — la versione più recente.
    """
    client = db.get_client()
    res = (
        client.table("model_metrics")
        .select("*")
        .order("trained_at", desc=True)
        .limit(200)
        .execute()
    )
    df = pd.DataFrame(res.data)
    if df.empty:
        return df
    df["trained_at"] = pd.to_datetime(df["trained_at"])
    df = (
        df.sort_values("trained_at", ascending=False)
          .groupby(["target", "horizon_hours"], as_index=False)
          .first()
    )
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Layout
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Meteo Locale — Dashboard",
    page_icon="🌤️",
    layout="wide",
)

st.title("Meteo Locale — Dashboard")
st.caption("Sistema MOS locale per l'area di Roma. Read-only.")

stations_df = load_stations()
if stations_df.empty:
    st.error("Nessuna stazione attiva trovata in DB.")
    st.stop()

station_name_by_id = dict(zip(stations_df["id"], stations_df["name"]))

# ─── Sezione 1: previsioni più recenti ───────────────────────────────────────
st.header("1. Previsioni più recenti")

latest_fc = load_latest_forecast_per_station()
if latest_fc.empty:
    st.info("Nessuna previsione disponibile. Esegui `python3 model/inference.py`.")
else:
    table = latest_fc.merge(
        stations_df[["id", "name"]].rename(columns={"id": "station_id"}),
        on="station_id",
        how="left",
    )
    display_cols = [
        "name", "valid_for", "temperature",
        "wind_speed", "wind_direction", "humidity",
        "corrected", "model_version", "forecast_at",
    ]
    display_cols = [c for c in display_cols if c in table.columns]
    table = table[display_cols].sort_values("name").reset_index(drop=True)
    table = table.rename(columns={
        "name":           "Stazione",
        "valid_for":      "Valido per (UTC)",
        "temperature":    "T (°C)",
        "wind_speed":     "Vento (km/h)",
        "wind_direction": "Direzione (°)",
        "humidity":       "Umidità (%)",
        "corrected":      "Corretto",
        "model_version":  "Versione",
        "forecast_at":    "Emessa (UTC)",
    })
    st.dataframe(table, use_container_width=True, hide_index=True)


# ─── Sezione 2: previsto vs osservato — ultime 48h ───────────────────────────
st.header("2. Previsto vs Osservato — ultime 48h")

col_station, col_target = st.columns([2, 1])
with col_station:
    station_id = st.selectbox(
        "Stazione",
        options=stations_df["id"].tolist(),
        format_func=lambda i: station_name_by_id.get(i, f"st.{i}"),
    )
with col_target:
    target = st.selectbox(
        "Variabile",
        options=["temperature", "wind_speed", "humidity"],
        index=0,
    )

forecasts_df = load_recent_forecasts(hours=48)
obs_df       = load_recent_observations(station_id, hours=48)

forecasts_st = forecasts_df[forecasts_df["station_id"] == station_id] if not forecasts_df.empty else forecasts_df

if obs_df.empty and forecasts_st.empty:
    st.info("Nessun dato disponibile nelle ultime 48h.")
else:
    parts = []
    if not forecasts_st.empty and target in forecasts_st.columns:
        f = forecasts_st[["valid_for", target]].rename(
            columns={"valid_for": "time", target: "Previsto"}
        ).set_index("time")
        parts.append(f)
    if not obs_df.empty and target in obs_df.columns:
        o = obs_df[["recorded_at", target]].rename(
            columns={"recorded_at": "time", target: "Osservato"}
        ).set_index("time")
        parts.append(o)

    if parts:
        chart_df = pd.concat(parts, axis=1).sort_index()
        st.line_chart(chart_df)
    else:
        st.info(f"Nessun valore di '{target}' disponibile.")


# ─── Sezione 3: metriche modello correnti ────────────────────────────────────
st.header("3. Metriche modello correnti")

metrics_df = load_latest_metrics()
if metrics_df.empty:
    st.info("Nessuna metrica in `model_metrics`. Addestra un modello con `forecast.py`.")
else:
    display = metrics_df[[
        "target", "horizon_hours", "model_version",
        "val_mae", "val_rmse", "train_mae", "train_rmse",
        "n_train", "n_val", "trained_at",
    ]].rename(columns={
        "target":         "Target",
        "horizon_hours":  "Orizzonte (h)",
        "model_version":  "Versione",
        "val_mae":        "MAE val",
        "val_rmse":       "RMSE val",
        "train_mae":      "MAE train",
        "train_rmse":     "RMSE train",
        "n_train":        "N train",
        "n_val":          "N val",
        "trained_at":     "Trained at (UTC)",
    })
    st.dataframe(display, use_container_width=True, hide_index=True)

    # Riepilogo del modello "principale": temperature, horizon più piccolo.
    primary = metrics_df.sort_values(["target", "horizon_hours"]).iloc[0]
    st.metric(
        label=f"MAE corrente — {primary['target']} (T+{primary['horizon_hours']}h)",
        value=f"{primary['val_mae']:.3f}",
        help=f"Versione: {primary['model_version']}",
    )

st.caption("Cache TTL: 60s.  ·  Per aggiornare le previsioni: `python3 model/inference.py`.")
