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

import altair as alt
import pandas as pd
import streamlit as st

import db


# ─────────────────────────────────────────────────────────────────────────────
# Costanti display
# ─────────────────────────────────────────────────────────────────────────────

# Fuso orario locale: Supabase / Open-Meteo / METAR usano UTC; in dashboard
# convertiamo TUTTI i timestamp a ora italiana per la lettura.
LOCAL_TZ = "Europe/Rome"

# 16 punti cardinali (rosa dei venti italiana). round(°/22.5) % 16
# mappa direttamente sull'indice di questa lista.
WIND_DIRECTIONS_16 = [
    "N",   "NNE", "NE",  "ENE",
    "E",   "ESE", "SE",  "SSE",
    "S",   "SSO", "SO",  "OSO",
    "O",   "ONO", "NO",  "NNO",
]


def to_local(series: pd.Series) -> pd.Series:
    """
    Converte una serie di timestamp a ora locale (Europe/Rome).

    Robusta sia a stringhe ISO con offset (Supabase) sia a Series già
    parsate: forza prima il parsing UTC, poi converte al fuso locale.
    """
    s = pd.to_datetime(series, utc=True, errors="coerce")
    return s.dt.tz_convert(LOCAL_TZ)


def degrees_to_cardinal(deg) -> str:
    """
    Converte gradi (0–360) in punto cardinale a 16 settori.

    Formula: round(deg / 22.5) % 16  →  indice in WIND_DIRECTIONS_16.
    Ritorna stringa vuota per NaN.
    """
    if pd.isna(deg):
        return ""
    return WIND_DIRECTIONS_16[round(float(deg) / 22.5) % 16]


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
    df["forecast_at"] = to_local(df["forecast_at"])
    df["valid_for"]   = to_local(df["valid_for"])
    # Dedup: previsioni multiple emesse per la stessa (stazione, valid_for)
    # — tieni la più recente (forecast_at massimo). Il fix definitivo è un
    # upsert lato inference.py, qui filtriamo a display time.
    df = (
        df.sort_values("forecast_at", ascending=False)
          .drop_duplicates(subset=["station_id", "valid_for"], keep="first")
    )
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
        df["valid_for"]   = to_local(df["valid_for"])
        df["forecast_at"] = to_local(df["forecast_at"])
        # Dedup come in load_latest_forecast_per_station: tieni la previsione
        # più recente (forecast_at massimo) per ogni (station_id, valid_for).
        df = (
            df.sort_values("forecast_at", ascending=False)
              .drop_duplicates(subset=["station_id", "valid_for"], keep="first")
              .sort_values("valid_for")
        )
    return df


@st.cache_data(ttl=60)
def load_recent_observations(station_id: int, hours: int = 48) -> pd.DataFrame:
    rows = db.get_observations(station_id, hours=hours, qc_ok_only=True)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["recorded_at"] = to_local(df["recorded_at"])
    return df


@st.cache_data(ttl=60)
def load_forecast_vs_observed(days: int = 7) -> pd.DataFrame:
    """
    Legge la vista `forecast_vs_observed` per gli ultimi `days` giorni.

    Colonne attese dalla vista: station_id, [station_name], valid_for,
    temp_prevista, temp_osservata (nullable), errore_abs (nullable).
    """
    client = db.get_client()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    res = (
        client.table("forecast_vs_observed")
        .select("*")
        .gte("valid_for", since)
        .order("valid_for")
        .execute()
    )
    df = pd.DataFrame(res.data)
    if not df.empty:
        df["valid_for"] = to_local(df["valid_for"])
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
    df["trained_at"] = to_local(df["trained_at"])
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

    # Punto cardinale a 16 settori accanto ai gradi del vento.
    if "wind_direction" in table.columns:
        table["wind_dir_cardinal"] = table["wind_direction"].apply(degrees_to_cardinal)

    display_cols = [
        "name", "valid_for", "temperature",
        "wind_speed", "wind_direction", "wind_dir_cardinal", "humidity",
        "corrected", "model_version", "forecast_at",
    ]
    display_cols = [c for c in display_cols if c in table.columns]
    table = table[display_cols].sort_values("name").reset_index(drop=True)
    table = table.rename(columns={
        "name":              "Stazione",
        "valid_for":         "Valido per (Italia)",
        "temperature":       "T (°C)",
        "wind_speed":        "Vento (km/h)",
        "wind_direction":    "Direzione (°)",
        "wind_dir_cardinal": "Direzione",
        "humidity":          "Umidità (%)",
        "corrected":         "Corretto",
        "model_version":     "Versione",
        "forecast_at":       "Emessa (Italia)",
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
        "trained_at":     "Trained at (Italia)",
    })
    st.dataframe(display, use_container_width=True, hide_index=True)

    # Riepilogo del modello "principale": temperature, horizon più piccolo.
    primary = metrics_df.sort_values(["target", "horizon_hours"]).iloc[0]
    st.metric(
        label=f"MAE corrente — {primary['target']} (T+{primary['horizon_hours']}h)",
        value=f"{primary['val_mae']:.3f}",
        help=f"Versione: {primary['model_version']}",
    )

# ─── Sezione 4: qualità previsioni dalla vista forecast_vs_observed ──────────
st.header("4. Qualità previsioni — Previsto vs Osservato")

fvo_df = load_forecast_vs_observed(days=7)

if fvo_df.empty:
    st.info(
        "Nessun dato in `forecast_vs_observed`. "
        "La vista si popola man mano che inference.py e mainMETEO.py "
        "accumulano previsioni e osservazioni coincidenti."
    )
else:
    # Risolvi station_name se la vista restituisce solo station_id.
    if "station_name" not in fvo_df.columns:
        fvo_df["station_name"] = (
            fvo_df["station_id"]
            .map(station_name_by_id)
            .fillna(fvo_df["station_id"].astype(str))
        )

    # ── Costruisci DataFrame long per Altair ─────────────────────────────────
    # Linea "Previsto": tutte le righe (incluse quelle senza osservato).
    fc_long = (
        fvo_df[["valid_for", "station_name", "temp_prevista"]]
        .copy()
        .rename(columns={"temp_prevista": "T (°C)"})
    )
    fc_long["Serie"] = "Previsto"

    # Linea "Osservato": solo dove temp_osservata non è NULL.
    obs_long = (
        fvo_df[fvo_df["temp_osservata"].notna()]
        [["valid_for", "station_name", "temp_osservata"]]
        .copy()
        .rename(columns={"temp_osservata": "T (°C)"})
    )
    obs_long["Serie"] = "Osservato"

    plot_df = pd.concat([fc_long, obs_long], ignore_index=True)

    # ── Grafico Altair a due layer: tratteggiato (Previsto) + continuo (Osservato)
    chart = (
        alt.Chart(plot_df)
        .mark_line(point=False)
        .encode(
            x=alt.X("valid_for:T", title="Ora (Italia)"),
            y=alt.Y("T (°C):Q", title="Temperatura (°C)"),
            color=alt.Color("station_name:N", title="Stazione"),
            strokeDash=alt.StrokeDash(
                "Serie:N",
                scale=alt.Scale(
                    domain=["Previsto", "Osservato"],
                    range=[[6, 4], [0, 0]],   # tratteggiata / continua
                ),
                legend=alt.Legend(title="Linea"),
            ),
            opacity=alt.condition(
                alt.datum.Serie == "Osservato",
                alt.value(1.0),
                alt.value(0.65),
            ),
            tooltip=[
                alt.Tooltip("valid_for:T", title="Ora (Italia)", format="%d/%m %H:%M"),
                alt.Tooltip("station_name:N", title="Stazione"),
                alt.Tooltip("Serie:N", title="Serie"),
                alt.Tooltip("T (°C):Q", title="Temperatura (°C)", format=".1f"),
            ],
        )
        .properties(height=380)
    )
    st.altair_chart(chart, use_container_width=True)

    # ── Tabella MAE medio per stazione ───────────────────────────────────────
    if "errore_abs" in fvo_df.columns:
        has_obs = fvo_df["temp_osservata"].notna().any()
        if has_obs:
            mae_table = (
                fvo_df[fvo_df["temp_osservata"].notna()]
                .groupby("station_name", as_index=False)["errore_abs"]
                .mean()
                .rename(columns={
                    "station_name": "Stazione",
                    "errore_abs":   "MAE medio temperatura (°C)",
                })
                .sort_values("Stazione")
                .reset_index(drop=True)
            )
            mae_table["MAE medio temperatura (°C)"] = (
                mae_table["MAE medio temperatura (°C)"].round(3)
            )
            st.dataframe(mae_table, use_container_width=True, hide_index=True)
        else:
            st.info("Nessuna osservazione confrontabile ancora disponibile per calcolare il MAE.")


st.caption("Cache TTL: 60s.  ·  Per aggiornare le previsioni: `python3 model/inference.py`.")
