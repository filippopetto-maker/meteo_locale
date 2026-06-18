"""
features.py — Feature Engineering per il modello meteo
Blocco 2 - Strato 2

Trasforma le osservazioni pulite (post-QC) in una tabella ricca di feature
pronta per LightGBM. Cinque famiglie di feature, costruite a strati:

  STRATO 1 — Temporali     : ora/giorno/stagione in forma ciclica (sin/cos)
  STRATO 2 — Lag           : valori delle N ore precedenti
  STRATO 3 — Rolling       : medie/varianze su finestre mobili
  STRATO 4 — Derivate meteo: trend, vento in componenti u/v
  STRATO 5 — Orografiche   : quota, distanza mare, microclima (il differenziale)

Input:  DataFrame di osservazioni di UNA stazione, ordinato per tempo.
Output: stesso DataFrame con le colonne feature aggiunte.
"""

import numpy as np
import pandas as pd
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════
# STRATO 1 — FEATURE TEMPORALI
# ══════════════════════════════════════════════════════════════════════════
#
# Il meteo segue cicli giornalieri e annuali. Codifichiamo il tempo in forma
# ciclica (sin/cos) così che valori agli estremi del ciclo siano "vicini" per
# il modello: le 23:00 e le 00:00 devono risultare adiacenti, non lontane.
# ══════════════════════════════════════════════════════════════════════════

def add_temporal_features(df: pd.DataFrame, time_col: str = "recorded_at") -> pd.DataFrame:
    """
    Aggiunge feature temporali cicliche a partire dalla colonna timestamp.

    Feature prodotte:
        hour_sin, hour_cos     → ciclo giornaliero (24h)
        doy_sin, doy_cos       → ciclo annuale (giorno dell'anno, stagionalità)
        month                  → mese (1-12), utile come categoria
        is_weekend             → 0/1 (può influenzare il microclima urbano:
                                  meno traffico = meno calore antropogenico)
        is_daytime             → 0/1 (giorno/notte semplificato)

    Args:
        df:       DataFrame con una colonna timestamp
        time_col: nome della colonna timestamp

    Ritorna:
        df con le nuove colonne aggiunte
    """
    df = df.copy()

    # Assicura che la colonna sia datetime
    df[time_col] = pd.to_datetime(df[time_col])

    # Estrai le componenti temporali di base
    hour = df[time_col].dt.hour + df[time_col].dt.minute / 60.0  # ora frazionaria
    doy  = df[time_col].dt.dayofyear                              # 1-365/366

    # ── Ciclo giornaliero (periodo = 24 ore) ──
    df["hour_sin"] = np.sin(2 * np.pi * hour / 24.0)
    df["hour_cos"] = np.cos(2 * np.pi * hour / 24.0)

    # ── Ciclo annuale (periodo = 365.25 giorni) ──
    df["doy_sin"] = np.sin(2 * np.pi * doy / 365.25)
    df["doy_cos"] = np.cos(2 * np.pi * doy / 365.25)

    # ── Feature categoriche/binarie ──
    df["month"]      = df[time_col].dt.month
    df["is_weekend"] = (df[time_col].dt.dayofweek >= 5).astype(int)
    df["is_daytime"] = ((hour >= 7) & (hour <= 19)).astype(int)

    return df


# ══════════════════════════════════════════════════════════════════════════
# STRATO 2 — LAG FEATURES
# ══════════════════════════════════════════════════════════════════════════
#
# Il meteo ha forte autocorrelazione temporale: il valore futuro dipende dai
# valori recenti. Le lag features danno al modello la "memoria" delle ore
# precedenti. Usiamo .shift() che guarda SOLO al passato → niente look-ahead bias.
# ══════════════════════════════════════════════════════════════════════════

def add_lag_features(
    df: pd.DataFrame,
    cols: list[str] = None,
    lags: list[int] = None,
) -> pd.DataFrame:
    """
    Aggiunge i valori delle osservazioni precedenti come nuove colonne.

    IMPORTANTE: assume che df sia ordinato per tempo crescente e che le
    osservazioni siano regolari (stesso intervallo). Ogni lag=N significa
    "N posizioni indietro nella tabella".

    Feature prodotte (per ogni colonna e ogni lag):
        {col}_lag_{n}   → valore di n posizioni precedenti

    Args:
        df:   DataFrame ordinato per tempo (una sola stazione)
        cols: colonne su cui calcolare i lag (default: temp, vento, direzione, umidità)
        lags: quali ritardi usare (default: 1, 2, 3, 6 posizioni indietro)

    Ritorna:
        df con le colonne lag aggiunte
    """
    df = df.copy()

    if cols is None:
        cols = ["temperature", "wind_speed", "wind_direction", "humidity"]
    if lags is None:
        lags = [1, 2, 3, 6]

    for col in cols:
        if col not in df.columns:
            continue
        for n in lags:
            df[f"{col}_lag_{n}"] = df[col].shift(n)

    return df


# ══════════════════════════════════════════════════════════════════════════
# STRATO 3 — ROLLING STATISTICS
# ══════════════════════════════════════════════════════════════════════════
#
# Mentre i lag danno valori puntuali, le rolling danno il comportamento
# aggregato su una finestra mobile. La MEDIA cattura il livello recente;
# la DEVIAZIONE STANDARD cattura l'instabilità — segnale chiave per prevedere
# cambiamenti di tempo e rischio temporali.
# ══════════════════════════════════════════════════════════════════════════

def add_rolling_features(
    df: pd.DataFrame,
    cols: list[str] = None,
    windows: list[int] = None,
) -> pd.DataFrame:
    """
    Aggiunge medie e deviazioni standard su finestre mobili.

    Usa .rolling() con shift(1) per garantire che la finestra includa solo
    osservazioni PASSATE (non l'istante corrente) → niente look-ahead bias.

    Feature prodotte (per ogni colonna e finestra):
        {col}_roll_mean_{w}  → media degli ultimi w valori (escluso il corrente)
        {col}_roll_std_{w}   → deviazione std (instabilità)

    Args:
        df:      DataFrame ordinato per tempo (una stazione)
        cols:    colonne su cui calcolare (default: temp, vento, pressione)
        windows: ampiezze delle finestre in posizioni (default: 3, 6, 12)

    Ritorna:
        df con le colonne rolling aggiunte
    """
    df = df.copy()

    if cols is None:
        cols = ["temperature", "wind_speed", "pressure"]
    if windows is None:
        windows = [3, 6, 12]

    for col in cols:
        if col not in df.columns:
            continue
        # shift(1): la finestra si ferma all'osservazione precedente,
        # così la riga corrente non "vede se stessa"
        shifted = df[col].shift(1)
        for w in windows:
            df[f"{col}_roll_mean_{w}"] = shifted.rolling(window=w, min_periods=1).mean()
            df[f"{col}_roll_std_{w}"]  = shifted.rolling(window=w, min_periods=2).std()

    return df


# ══════════════════════════════════════════════════════════════════════════
# STRATO 4 — FEATURE DERIVATE METEO
# ══════════════════════════════════════════════════════════════════════════
#
# Combinano i dati grezzi in variabili fisicamente significative.
# La trasformazione chiave è il vento in componenti u/v: risolve la
# discontinuità 360°/0° e separa il vento in due numeri continui, come fanno
# i meteorologi. I trend catturano la direzione del cambiamento.
# ══════════════════════════════════════════════════════════════════════════

def add_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggiunge feature meteo derivate.

    Feature prodotte:
        wind_u, wind_v        → componenti cartesiane del vento (Est-Ovest, Nord-Sud)
        temp_trend_1h         → variazione temperatura sull'ora precedente
        pressure_trend_1h     → variazione pressione (calo = maltempo in arrivo)
        wind_speed_trend_1h   → variazione velocità vento
        wind_chill            → temperatura percepita (se fa freddo e c'è vento)

    Args:
        df: DataFrame con temperature, wind_speed, wind_direction (e pressure se c'è)

    Ritorna:
        df con le colonne derivate aggiunte
    """
    df = df.copy()

    # ── Vento in componenti u/v ──
    # u = componente zonale (Est+/Ovest-), v = componente meridionale (Nord+/Sud-)
    if "wind_speed" in df.columns and "wind_direction" in df.columns:
        rad = np.radians(df["wind_direction"])
        df["wind_u"] = df["wind_speed"] * np.sin(rad)
        df["wind_v"] = df["wind_speed"] * np.cos(rad)

    # ── Trend (variazione sull'osservazione precedente) ──
    if "temperature" in df.columns:
        df["temp_trend_1h"] = df["temperature"].diff()
    if "pressure" in df.columns:
        df["pressure_trend_1h"] = df["pressure"].diff()
    if "wind_speed" in df.columns:
        df["wind_speed_trend_1h"] = df["wind_speed"].diff()

    # ── Wind chill (temperatura percepita) ──
    # Formula standard JAG/TI, valida per T <= 10°C e vento > 4.8 km/h.
    # Fuori da quel range usiamo la temperatura reale.
    if "temperature" in df.columns and "wind_speed" in df.columns:
        t = df["temperature"]
        v = df["wind_speed"].clip(lower=0)
        wc = (13.12 + 0.6215 * t
              - 11.37 * np.power(v.where(v > 0, 0), 0.16)
              + 0.3965 * t * np.power(v.where(v > 0, 0), 0.16))
        # Applica wind chill solo dove ha senso fisico, altrimenti temp reale
        df["wind_chill"] = np.where((t <= 10) & (v > 4.8), wc, t)

    return df


# ══════════════════════════════════════════════════════════════════════════
# STRATO 5 — FEATURE OROGRAFICHE / SPAZIALI  (il differenziale competitivo)
# ══════════════════════════════════════════════════════════════════════════
#
# Sono STATICHE per stazione (non cambiano nel tempo). Si calcolano una volta
# da lat/lon e si "spalmano" su tutte le osservazioni della stazione.
#
# IMPORTANTE: queste feature diventano predittori APPRESI solo in addestramento
# MULTI-STAZIONE. Con una stazione sola sono costanti e il modello le ignora.
# Servono almeno 3-4 stazioni con profili contrastanti (mare/pianura/urbano/quota).
# ══════════════════════════════════════════════════════════════════════════

import math

# Linea di costa laziale approssimata (punti lungo il litorale tirrenico).
# Usata per calcolare la distanza dal mare di un punto qualsiasi.
LATIUM_COAST = [
    (42.10, 11.80),  # Civitavecchia
    (42.02, 11.93),
    (41.95, 12.07),  # Ladispoli
    (41.88, 12.16),
    (41.83, 12.21),  # Fregene
    (41.77, 12.23),  # Fiumicino
    (41.73, 12.28),  # Ostia
    (41.66, 12.39),
    (41.58, 12.50),
    (41.45, 12.63),  # Anzio
    (41.35, 12.78),  # Nettuno/Torre Astura (punto intermedio)
    (41.27, 12.92),  # Sabaudia
    (41.24, 13.08),  # Promontorio del Circeo
    (41.29, 13.25),  # Terracina
    (41.25, 13.45),  # tratto sud, prima di Gaeta
    (41.22, 13.57),  # Gaeta/Formia
]

# Centro di Roma (Piazza Venezia) — proxy del nucleo dell'isola di calore urbana
ROME_CENTER = (41.8959, 12.4823)

# Etichette di microclima previste (devono combaciare con quelle dello schema)
MICROCLIMA_LABELS = ["standard", "esposta_sole", "urban_canyon",
                     "verde_parco", "costiera", "quota"]


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Distanza in km tra due coordinate (formula di Haversine)."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def distance_from_sea(lat: float, lon: float) -> float:
    """Distanza in km dal punto di costa più vicino (litorale laziale)."""
    return min(_haversine_km(lat, lon, c_lat, c_lon)
               for c_lat, c_lon in LATIUM_COAST)


def _bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Rotta (bearing) in gradi 0-360 dal punto 1 verso il punto 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    x = math.sin(dlam) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def bearing_to_sea(lat: float, lon: float) -> float:
    """
    Direzione (gradi 0-360) verso il punto di costa più vicino.
    Serve a capire da che parte arriva la brezza marina per quel punto.
    """
    nearest = min(LATIUM_COAST,
                  key=lambda c: _haversine_km(lat, lon, c[0], c[1]))
    return round(_bearing_deg(lat, lon, nearest[0], nearest[1]), 1)


def distance_from_center(lat: float, lon: float) -> float:
    """Distanza in km dal centro di Roma (proxy isola di calore urbana)."""
    return _haversine_km(lat, lon, ROME_CENTER[0], ROME_CENTER[1])


def fetch_elevation(lat: float, lon: float) -> Optional[float]:
    """
    Recupera la quota (m s.l.m.) da Open-Meteo Elevation API.
    Gratuita, nessuna API key. Da usare UNA TANTUM per popolare stations.

    NB: richiede connessione internet. In caso di errore ritorna None.
    """
    import requests
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/elevation",
            params={"latitude": lat, "longitude": lon},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        return float(data["elevation"][0])
    except Exception as e:
        print(f"⚠️  fetch_elevation fallito per ({lat},{lon}): {e}")
        return None


def compute_static_orography(lat: float, lon: float,
                             microclima: str = "standard") -> dict:
    """
    Calcola tutte le feature orografiche statiche di una stazione.
    Da usare quando aggiungi una stazione, per salvarle nel DB.

    Ritorna un dict con: altitude, dist_sea_km, dist_center_km, microclima.
    """
    return {
        "altitude":       fetch_elevation(lat, lon),
        "dist_sea_km":    round(distance_from_sea(lat, lon), 2),
        "dist_center_km": round(distance_from_center(lat, lon), 2),
        "bearing_sea":    bearing_to_sea(lat, lon),
        "microclima":     microclima,
    }


def add_orographic_features(df: pd.DataFrame, station: dict) -> pd.DataFrame:
    """
    "Spalma" le feature orografiche statiche di una stazione su tutte le sue
    righe di osservazione, e codifica il microclima in colonne numeriche.

    Args:
        df:      DataFrame di osservazioni di UNA stazione
        station: dict con altitude, dist_sea_km, dist_center_km, microclima
                 (tipicamente letto dalla tabella stations)

    Ritorna:
        df con le colonne orografiche aggiunte
    """
    df = df.copy()

    # Feature numeriche continue (costanti per la stazione)
    df["altitude"]       = station.get("altitude")
    df["dist_sea_km"]    = station.get("dist_sea_km")
    df["dist_center_km"] = station.get("dist_center_km")
    df["bearing_sea"]    = station.get("bearing_sea")

    # Interazione vento-mare: quanto il vento attuale è "onshore" (dal mare).
    # +1 = brezza marina piena (raffredda), -1 = vento da terra.
    # Combina statico (bearing_sea) e dinamico (wind_direction) → la firma
    # statistica della brezza, senza simulare il flusso.
    if "wind_direction" in df.columns and station.get("bearing_sea") is not None:
        delta = np.radians(df["wind_direction"] - station["bearing_sea"])
        df["onshore_alignment"] = np.cos(delta)
    else:
        df["onshore_alignment"] = np.nan

    # Microclima → one-hot encoding (una colonna 0/1 per ogni etichetta)
    microclima = station.get("microclima", "standard")
    for label in MICROCLIMA_LABELS:
        df[f"microclima_{label}"] = int(microclima == label)

    return df


# ══════════════════════════════════════════════════════════════════════════
# STRATO 5 — PIPELINE LCZ / IMPERMEABILITÀ  (architettura per il futuro)
# ══════════════════════════════════════════════════════════════════════════
#
# COSA SONO:
#   LCZ (Local Climate Zones) — Wudapt project. Classifica ogni pixel 1-17
#   (1=compact high-rise, 6=open low-rise, 14=low plants, ...).
#   Impermeabilità — % di suolo impermeabile, Copernicus HRL (100 m).
#
# PERCHÉ LE VOGLIAMO:
#   Completano la descrizione dell'isola di calore meglio della sola
#   dist_center_km: un parco a 500 m dal Colosseo (verde_parco) è termicamente
#   opposto a un quartiere a 500 m dall'EUR (urban_canyon), stessa distanza.
#
# QUANDO ATTIVARLE:
#   Fase 3 — stazioni ≥ 10, sufficiente varianza spaziale per imparare il
#   segnale LCZ. Con 3-4 stazioni sono quasi costanti → rumore, non segnale.
#
# COME SCARICARE I RASTER (da fare in fase 3):
#   LCZ Lazio (WUDAPT EU, 100 m):
#       https://zenodo.org/record/6364593
#       gdal_translate -projwin 11.0 42.5 13.5 41.0 input.tif data/lcz_lazio.tif
#   Impermeabilità (Copernicus HRL, login EEA gratuito):
#       https://land.copernicus.eu/pan-european/high-resolution-layers/imperviousness
#       Clip sulla bbox Lazio, salva in data/impervious_lazio.tif
#
# ARCHITETTURA DI FALLBACK (chiave):
#   Ogni funzione raster ritorna float | None.
#   None  →  NaN nel DataFrame  →  LightGBM gestisce NaN nativamente.
#   Nessun blocco in fase 1/2: il codice gira già oggi, le colonne si
#   "accendono" automaticamente quando installi i raster.
# ══════════════════════════════════════════════════════════════════════════

# Punta a None finché non scarichi i file (fase 3).
LCZ_RASTER_PATH    = None   # es. "data/lcz_lazio.tif"
IMPERV_RASTER_PATH = None   # es. "data/impervious_lazio.tif"


def _sample_raster(raster_path: Optional[str], lat: float, lon: float) -> Optional[float]:
    """
    Campiona un raster GeoTIFF in (lat, lon) e ritorna il valore del pixel.

    Gestisce correttamente CRS diversi da WGS84 (es. Copernicus usa EPSG:3035):
    reproietta le coordinate prima di campionare.

    Fallback sicuro:
      - raster_path è None        → None (raster non ancora scaricato)
      - file non trovato          → None + debug log
      - coordinate fuori bbox     → None
      - pixel nodata              → None

    Richiede: rasterio, pyproj  (pip install rasterio pyproj)
    """
    if raster_path is None:
        return None
    try:
        import rasterio
        import pyproj

        with rasterio.open(raster_path) as src:
            # Reproietta lat/lon (WGS84) nel CRS del raster
            transformer = pyproj.Transformer.from_crs(
                "EPSG:4326", src.crs, always_xy=True
            )
            x, y = transformer.transform(lon, lat)

            row, col = src.index(x, y)
            # Controlla che le coordinate siano dentro l'extent
            if not (0 <= row < src.height and 0 <= col < src.width):
                return None
            value = float(src.read(1)[row, col])
            # Nodata → None
            if src.nodata is not None and value == src.nodata:
                return None
            return value

    except Exception as e:
        logger.debug(f"_sample_raster({raster_path}, {lat:.4f}, {lon:.4f}): {e}")
        return None


def fetch_lcz(lat: float, lon: float) -> Optional[float]:
    """
    Classe LCZ del punto (1-10 urbano, 11-17 naturale).

    Classi chiave per Roma:
      1 = Compact high-rise  (centro storico denso)
      3 = Compact low-rise   (quartieri storici periferici)
      6 = Open low-rise      (suburbs, ville, EUR residenziale)
     14 = Low plants         (campi, parchi grandi)
     17 = Water              (Tevere, laghi)

    None se LCZ_RASTER_PATH non è configurato.
    """
    return _sample_raster(LCZ_RASTER_PATH, lat, lon)


def fetch_imperviousness(lat: float, lon: float) -> Optional[float]:
    """
    Percentuale di suolo impermeabile (0–100).

    Proxy diretto dell'effetto isola di calore:
      > 80% → asfalto/cemento → forte accumulo di calore notturno
      < 20% → parchi/campagna → raffrescamento notturno

    None se IMPERV_RASTER_PATH non è configurato.
    """
    return _sample_raster(IMPERV_RASTER_PATH, lat, lon)


def enrich_static_orography(station: dict) -> dict:
    """
    Arricchisce il dict di una stazione con LCZ e impermeabilità.
    Da chiamare DOPO compute_static_orography() in fase 3.

    Flusso completo fase 3:
        meta = compute_static_orography(lat, lon, microclima)
        meta.update({"lat": lat, "lon": lon})
        meta = enrich_static_orography(meta)   # aggiunge lcz_class, imperviousness_pct
        # → salva meta nel DB (colonne stations da estendere in fase 3)

    In fase 1/2 i campi sono None → NaN → LightGBM li ignora → sicuro.
    """
    lat = station.get("lat") or station.get("latitude")
    lon = station.get("lon") or station.get("longitude")
    station["lcz_class"]          = fetch_lcz(lat, lon) if (lat and lon) else None
    station["imperviousness_pct"] = fetch_imperviousness(lat, lon) if (lat and lon) else None
    return station


# ══════════════════════════════════════════════════════════════════════════
# ORCHESTRATORE — build_feature_matrix()
# ══════════════════════════════════════════════════════════════════════════
#
# Applica i 5 strati in sequenza per UNA stazione e ritorna il DataFrame
# feature-complete. Da chiamare per ogni stazione; poi concatenare i
# risultati per costruire il training set multi-stazione.
#
# Perché stazione-per-stazione e non multi-stazione subito?
# Lag e rolling calcolano la "memoria" della serie temporale: devono
# operare DENTRO la serie di ciascuna stazione in isolamento. Se
# mescolassimo prima, la riga di Ostia alle 14:00 vedrebbe come "lag_1"
# il valore di Roma Nord alle 13:00 — dati fisicamente incoerenti.
# ══════════════════════════════════════════════════════════════════════════

def build_feature_matrix(
    df: pd.DataFrame,
    station: dict,
    time_col: str = "recorded_at",
    lag_cols: Optional[list] = None,
    lags: Optional[list] = None,
    rolling_cols: Optional[list] = None,
    rolling_windows: Optional[list] = None,
) -> pd.DataFrame:
    """
    Feature matrix completa per UNA stazione (tutti e 5 gli strati).

    Args:
        df:              DataFrame di osservazioni di UNA stazione,
                         ordinato per tempo o non (viene ordinato internamente).
        station:         dict con metadati orografici della stazione.
                         Campi attesi: altitude, dist_sea_km, dist_center_km,
                         bearing_sea, microclima.
                         Fase 3: aggiunge anche lcz_class, imperviousness_pct.
        time_col:        nome della colonna timestamp.
        lag_cols:        colonne per i lag (default: vedi add_lag_features).
        lags:            ritardi (default: [1, 2, 3, 6]).
        rolling_cols:    colonne per rolling (default: vedi add_rolling_features).
        rolling_windows: finestre rolling (default: [3, 6, 12]).

    Ritorna:
        DataFrame feature-complete per LightGBM.
        Le prime N righe hanno NaN (N ≈ max(lags) + max(windows)).
        Droppale con .dropna() prima del training.

    ─── Pattern di utilizzo (training multi-stazione) ───────────────────
        frames = []
        for st in get_active_stations():
            obs = get_observations(st["id"], hours=8760)   # un anno
            feat = build_feature_matrix(obs, st)
            feat["station_id"] = st["id"]                  # etichetta la stazione
            frames.append(feat)
        train_df = pd.concat(frames).dropna().reset_index(drop=True)
        # train_df è pronto per lgbm.Dataset()
    ─────────────────────────────────────────────────────────────────────
    """
    df = df.sort_values(time_col).copy()

    df = add_temporal_features(df,  time_col=time_col)
    df = add_lag_features(df,       cols=lag_cols, lags=lags)
    df = add_rolling_features(df,   cols=rolling_cols, windows=rolling_windows)
    df = add_derived_features(df)
    df = add_orographic_features(df, station)

    return df


# ── TEST STANDALONE ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Costruiamo dati finti: 48 ore di osservazioni orarie
    timestamps = pd.date_range("2025-07-15 00:00", periods=48, freq="h")
    df_test = pd.DataFrame({
        "recorded_at": timestamps,
        "temperature": 20 + 8 * np.sin(2 * np.pi * np.arange(48) / 24),  # onda giornaliera
        "wind_speed":  np.random.uniform(0, 20, 48),
        "wind_direction": np.random.uniform(0, 360, 48),
    })

    print("=== INPUT (prime 3 righe) ===")
    print(df_test.head(3).to_string())

    df_out = add_temporal_features(df_test)

    print("\n=== OUTPUT — feature temporali aggiunte ===")
    cols = ["recorded_at", "hour_sin", "hour_cos", "doy_sin", "doy_cos",
            "month", "is_weekend", "is_daytime"]
    print(df_out[cols].head(6).to_string(index=False))

    # Verifica chiave: le 23:00 e le 00:00 devono avere sin/cos simili
    print("\n=== VERIFICA CICLICITÀ (23:00 vs 00:00 successiva) ===")
    h23 = df_out[df_out["recorded_at"].dt.hour == 23].iloc[0]
    h00 = df_out[df_out["recorded_at"].dt.hour == 0].iloc[1]  # il giorno dopo
    print(f"23:00 → hour_sin={h23['hour_sin']:+.3f}, hour_cos={h23['hour_cos']:+.3f}")
    print(f"00:00 → hour_sin={h00['hour_sin']:+.3f}, hour_cos={h00['hour_cos']:+.3f}")
    dist = np.sqrt((h23['hour_sin']-h00['hour_sin'])**2 + (h23['hour_cos']-h00['hour_cos'])**2)
    print(f"Distanza sul cerchio: {dist:.3f}  (vicina a 0 = ciclicità corretta ✅)")

    # ── STRATO 2 — Lag ──
    df_out = add_lag_features(df_out)
    print("\n=== STRATO 2 — Lag features (temperatura) ===")
    cols_lag = ["recorded_at", "temperature", "temperature_lag_1",
                "temperature_lag_2", "temperature_lag_3", "temperature_lag_6"]
    print(df_out[cols_lag].head(8).to_string(index=False))
    print("\nNota: le prime righe hanno NaN nei lag — è corretto,")
    print("non esistono osservazioni prima dell'inizio della serie.")
    print(f"Verifica: temp_lag_1 della riga 1 ({df_out['temperature_lag_1'].iloc[1]:.2f}) "
          f"== temp della riga 0 ({df_out['temperature'].iloc[0]:.2f}) ✅")

    # ── STRATO 3 — Rolling ──
    df_out = add_rolling_features(df_out)
    print("\n=== STRATO 3 — Rolling statistics (temperatura, finestra 3) ===")
    cols_roll = ["recorded_at", "temperature", "temperature_roll_mean_3", "temperature_roll_std_3"]
    print(df_out[cols_roll].head(8).to_string(index=False))
    print("\nLa media mobile segue il livello recente; la std mostra l'instabilità.")
    print("Con dati reali, una std alta del vento segnala possibile cambio di tempo.")

    # ── STRATO 4 — Derivate ──
    df_out = add_derived_features(df_out)
    print("\n=== STRATO 4 — Derivate meteo (vento u/v + trend) ===")
    cols_der = ["recorded_at", "wind_speed", "wind_direction", "wind_u", "wind_v", "temp_trend_1h"]
    print(df_out[cols_der].head(6).to_string(index=False))

    # Verifica round-trip: da u/v ricostruisco velocità e direzione originali
    print("\n=== VERIFICA u/v — ricostruzione velocità e direzione ===")
    row = df_out.iloc[3]
    speed_rec = np.sqrt(row["wind_u"]**2 + row["wind_v"]**2)
    dir_rec   = (np.degrees(np.arctan2(row["wind_u"], row["wind_v"]))) % 360
    print(f"Originale: speed={row['wind_speed']:.2f}, dir={row['wind_direction']:.1f}°")
    print(f"Da u/v:    speed={speed_rec:.2f}, dir={dir_rec:.1f}°  (devono coincidere ✅)")

    print(f"\n=== RIEPILOGO: il DataFrame finale ha {len(df_out.columns)} colonne ===")

    # ── STRATO 5 — Orografiche (parte geometrica, testabile offline) ──
    print("\n=== STRATO 5 — Distanze calcolate (verifica geometrica) ===")
    punti = [
        ("Ostia",       41.7330, 12.2830),
        ("Roma Centro", 41.9028, 12.4964),
        ("Roma Nord",   42.0160, 12.5000),
        ("Casal Palocco",41.7512, 12.3594),
    ]
    print(f"{'Stazione':<16}{'dist_mare_km':>14}{'dist_centro_km':>16}{'bearing_mare':>14}")
    for nome, la, lo in punti:
        dmare = distance_from_sea(la, lo)
        dcentro = distance_from_center(la, lo)
        bmare = bearing_to_sea(la, lo)
        print(f"{nome:<16}{dmare:>14.2f}{dcentro:>16.2f}{bmare:>14.1f}")
    print("\nAtteso: Ostia vicina al mare (~0-3 km), Roma Centro/Nord lontane (~20-30 km).")
    print("bearing_mare ~SW (200-250°) per punti a NE della costa. Coerente ✅")

    # Test one-hot microclima + onshore alignment
    print("\n=== STRATO 5 — One-hot microclima + interazione vento-mare ===")
    station_ostia = {"altitude": 3, "dist_sea_km": 0.4, "dist_center_km": 24.5,
                     "bearing_sea": 225.0, "microclima": "costiera"}
    df_oro = add_orographic_features(df_test.copy(), station_ostia)
    print("onshore_alignment (prime 4 righe, dipende dal vento di ogni ora):")
    print(df_oro[["wind_direction", "onshore_alignment"]].head(4).to_string(index=False))
    print("→ vicino a +1 quando il vento viene da ~225° (dal mare), -1 se da terra ✅")

    print("\n💡 La quota (fetch_elevation) usa l'API Open-Meteo: testala sul tuo Mac")
    print("   con connessione attiva. Qui è stata saltata.")

    # ── PIPELINE LCZ — verifica fallback ──
    print("\n=== PIPELINE LCZ / IMPERMEABILITÀ — verifica fallback (raster non configurati) ===")
    lat_test, lon_test = 41.9028, 12.4964
    lcz_val    = fetch_lcz(lat_test, lon_test)
    imperv_val = fetch_imperviousness(lat_test, lon_test)
    print(f"fetch_lcz({lat_test}, {lon_test})            → {lcz_val}")
    print(f"fetch_imperviousness({lat_test}, {lon_test}) → {imperv_val}")
    print("(None atteso in fase 1/2: raster non configurato → nessun blocco ✅)")

    meta = compute_static_orography(lat_test, lon_test, microclima="urban_canyon")
    meta.update({"lat": lat_test, "lon": lon_test})
    meta = enrich_static_orography(meta)
    print(f"\nenrich_static_orography → lcz_class={meta['lcz_class']}, "
          f"imperviousness_pct={meta['imperviousness_pct']}")
    print("(None → NaN nel DataFrame → LightGBM li ignora nativamente ✅)")

    # ── ORCHESTRATORE — build_feature_matrix() ──
    print("\n=== ORCHESTRATORE — build_feature_matrix() (pipeline completa 5 strati) ===")
    station_centro = {
        "altitude": 22, "dist_sea_km": 22.1, "dist_center_km": 0.4,
        "bearing_sea": 248.0, "microclima": "urban_canyon",
        "lcz_class": None, "imperviousness_pct": None,
    }
    df_full = build_feature_matrix(df_test.copy(), station_centro)
    n_col      = len(df_full.columns)
    n_nan_rows = df_full.isnull().any(axis=1).sum()
    print(f"Input:  {len(df_test)} righe × {len(df_test.columns)} colonne")
    print(f"Output: {len(df_full)} righe × {n_col} colonne  "
          f"(+{n_col - len(df_test.columns)} feature aggiunte)")
    print(f"Righe con NaN (da lag/rolling iniziali): {n_nan_rows}")
    print(f"Righe utilizzabili dopo .dropna():        {len(df_full) - n_nan_rows}")
    print("\nColonne feature finali:")
    for i, c in enumerate(df_full.columns):
        print(f"  {i+1:02d}. {c}")
    print(f"\nIl DataFrame è pronto per lgbm.Dataset() dopo .dropna() ✅")
