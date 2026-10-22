"""
m2.py — Feature e previsione di M2 (temperatura, IFS + residuo LightGBM)

M2 è addestrato da scripts/train_m2.py sul contratto di produzione: la riga NWP
a X dà la temperatura a X+1h. La previsione è nwp_valid (IFS a valid_for) più il
residuo predetto da LightGBM con:
  - le feature di produzione (build_feature_matrix, stesse colonne di lgbm_temperature);
  - station_cat (id stazione, categorica);
  - nwp_valid;
  - bias_st_hour: bias IFS − Netatmo della stazione all'ora UTC di valid_for,
    media dei 30 giorni precedenti (bias_table); NaN se manca.

build_m2_frame è usata sia dagli esperimenti sia dalla prova in ombra, così
training e produzione costruiscono le feature con lo stesso codice.
"""

from __future__ import annotations

import gzip
import json
import sys
from datetime import timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from features import build_feature_matrix  # noqa: E402

M2_DIR = _PROJECT_ROOT / "model" / "m2"
M2_HUM_DIR = _PROJECT_ROOT / "model" / "m2_hum"
M2_WIND_DIR = _PROJECT_ROOT / "model" / "m2_wind"
M2_WIND10_DIR = _PROJECT_ROOT / "model" / "m2_wind10"
EXTRA_FEATURES = ("station_cat", "nwp_valid", "bias_st_hour")
EXTRA_FEATURES_HUM = ("station_cat", "nwp_valid", "nwp_rh_valid", "bias_rh_hour")
EXTRA_FEATURES_WIND = ("station_cat", "nwp_ws_valid", "nwp_wd_sin", "nwp_wd_cos", "bias_ws_hour")


def build_m2_frame(nwp: pd.DataFrame, station: dict, feature_cols: list[str]) -> pd.DataFrame:
    """Una riga per ora NWP X: feature a X, valid_for = X+1h, nwp_valid = IFS a valid_for."""
    feat = build_feature_matrix(nwp.copy(), station)
    for c in feature_cols:
        if c not in feat.columns:
            feat[c] = np.nan
    feat = feat[["recorded_at", *feature_cols]].copy()
    feat[feature_cols] = feat[feature_cols].apply(pd.to_numeric, errors="coerce")
    feat["recorded_at"] = pd.to_datetime(feat.recorded_at).dt.tz_localize("UTC")
    feat["valid_for"] = feat.recorded_at + timedelta(hours=1)

    nwp_t = nwp[["recorded_at", "temperature"]].copy()
    nwp_t["recorded_at"] = pd.to_datetime(nwp_t.recorded_at).dt.tz_localize("UTC")
    feat = feat.merge(nwp_t.rename(columns={"recorded_at": "valid_for", "temperature": "nwp_valid"}),
                      on="valid_for", how="left")
    feat["station_cat"] = station["id"]
    return feat


class M2:
    """Modello congelato in model/m2/ (lgbm_m2_temperature.txt.gz + meta.json)."""

    def __init__(self, model_dir: Path = M2_DIR):
        self.meta = json.loads((model_dir / "meta.json").read_text())
        with gzip.open(model_dir / "lgbm_m2_temperature.txt.gz", "rt") as f:
            self.booster = lgb.Booster(model_str=f.read())
        self.features = self.meta["features"]
        self.base_features = [c for c in self.features if c not in EXTRA_FEATURES]
        self.tag = self.meta["model_tag"]

    def predict(self, nwp: pd.DataFrame, station: dict, valid_for: list[pd.Timestamp],
                bias_by_hour: dict[int, float | None]) -> dict[pd.Timestamp, float]:
        """Temperatura M2 per ogni valid_for (UTC aware); le ore senza riga di input sono omesse."""
        frame = build_m2_frame(nwp, station, self.base_features)
        frame = frame[frame.valid_for.isin(valid_for)].dropna(subset=["nwp_valid"]).copy()
        if frame.empty:
            return {}
        frame["bias_st_hour"] = [bias_by_hour.get(h) for h in frame.valid_for.dt.hour]
        frame["bias_st_hour"] = pd.to_numeric(frame.bias_st_hour, errors="coerce")
        pred = frame.nwp_valid.to_numpy() + self.booster.predict(frame[self.features])
        return dict(zip(frame.valid_for, pred))


class M2Humidity:
    """M2 umidità (H1: residuo diretto su RH), congelato in model/m2_hum/.

    previsione = RH dell'IFS a valid_for + residuo LightGBM, limitata a [0, 100].
    Le feature sono quelle di M2 temperatura (build_m2_frame) più nwp_rh_valid e
    bias_rh_hour (bias RH IFS − Netatmo della stazione all'ora UTC di valid_for,
    media dei 30 giorni precedenti, da bias_table.bias_rh)."""

    def __init__(self, model_dir: Path = M2_HUM_DIR):
        self.meta = json.loads((model_dir / "meta.json").read_text())
        with gzip.open(model_dir / "lgbm_m2_humidity.txt.gz", "rt") as f:
            self.booster = lgb.Booster(model_str=f.read())
        self.features = self.meta["features"]
        self.base_features = [c for c in self.features if c not in EXTRA_FEATURES_HUM]
        self.tag = self.meta["model_tag"]

    def predict(self, nwp: pd.DataFrame, station: dict, valid_for: list[pd.Timestamp],
                bias_rh_by_hour: dict[int, float | None]) -> dict[pd.Timestamp, float]:
        """Umidità relativa M2 per ogni valid_for (UTC aware); le ore senza riga di input sono omesse."""
        frame = build_m2_frame(nwp, station, self.base_features)
        nwp_h = nwp[["recorded_at", "humidity"]].copy()
        nwp_h["recorded_at"] = pd.to_datetime(nwp_h.recorded_at).dt.tz_localize("UTC")
        frame = frame.merge(nwp_h.rename(columns={"recorded_at": "valid_for", "humidity": "nwp_rh_valid"}),
                            on="valid_for", how="left")
        frame = frame[frame.valid_for.isin(valid_for)].dropna(subset=["nwp_valid", "nwp_rh_valid"]).copy()
        if frame.empty:
            return {}
        frame["bias_rh_hour"] = pd.to_numeric([bias_rh_by_hour.get(h) for h in frame.valid_for.dt.hour],
                                              errors="coerce")
        pred = np.clip(frame.nwp_rh_valid.to_numpy() + self.booster.predict(frame[self.features]), 0.0, 100.0)
        return dict(zip(frame.valid_for, pred))


class M2Wind:
    """M2 vento (velocità), congelato in model/m2_wind/.

    previsione = expm1(log1p(velocità IFS a valid_for) + residuo LightGBM), km/h, ≥ 0.
    Feature: quelle di build_m2_frame + nwp_ws_valid, direzione IFS (seno e coseno) e
    bias_ws_hour (media dei 30 giorni precedenti di log1p(IFS) − log1p(Netatmo) per stazione
    e ora UTC, da bias_table.bias_ws). La direzione non è modellata. Le stazioni fuori dal
    training (meta["train_stations"]) non hanno previsione."""

    def __init__(self, model_dir: Path = M2_WIND_DIR):
        self.meta = json.loads((model_dir / "meta.json").read_text())
        with gzip.open(model_dir / "lgbm_m2_wind.txt.gz", "rt") as f:
            self.booster = lgb.Booster(model_str=f.read())
        self.features = self.meta["features"]
        self.base_features = [c for c in self.features if c not in EXTRA_FEATURES_WIND]
        self.tag = self.meta["model_tag"]
        self.stations = set(self.meta["train_stations"])

    def predict(self, nwp: pd.DataFrame, station: dict, valid_for: list[pd.Timestamp],
                bias_ws_by_hour: dict[int, float | None]) -> dict[pd.Timestamp, float]:
        """Velocità del vento M2 (km/h) per ogni valid_for (UTC aware); {} se la stazione non è nel training."""
        if station["id"] not in self.stations:
            return {}
        frame = build_m2_frame(nwp, station, self.base_features)
        w = nwp[["recorded_at", "wind_speed", "wind_direction"]].copy()
        w["recorded_at"] = pd.to_datetime(w.recorded_at).dt.tz_localize("UTC")
        w["nwp_wd_sin"] = np.sin(np.deg2rad(w.wind_direction))
        w["nwp_wd_cos"] = np.cos(np.deg2rad(w.wind_direction))
        frame = frame.merge(w.rename(columns={"recorded_at": "valid_for", "wind_speed": "nwp_ws_valid"})
                            [["valid_for", "nwp_ws_valid", "nwp_wd_sin", "nwp_wd_cos"]], on="valid_for", how="left")
        frame = frame[frame.valid_for.isin(valid_for)].dropna(subset=["nwp_ws_valid"]).copy()
        if frame.empty:
            return {}
        frame["bias_ws_hour"] = pd.to_numeric([bias_ws_by_hour.get(h) for h in frame.valid_for.dt.hour],
                                              errors="coerce")
        base = np.log1p(frame.nwp_ws_valid.clip(lower=0).to_numpy())
        pred = np.clip(np.expm1(base + self.booster.predict(frame[self.features])), 0.0, None)
        return dict(zip(frame.valid_for, pred))


class M2Wind10:
    """M2 vento a 10 m (verità METAR), congelato in model/m2_wind10/.

    Velocità (km/h, residuo su log1p della velocità IFS) e direzione (angolo da seno e coseno previsti,
    pesati con la velocità in training). Nessuna identità di stazione né bias: si applica a qualunque punto
    con i descrittori statici (altitude, dist_sea_km, dist_center_km, bearing_sea, microclima).
    Attesa (meta["expected"]): su siti già visti 2,73 km/h e 29,4°; su siti mai visti 3,19 km/h e
    nessun guadagno sulla direzione (36,5° come l'IFS). La direzione ha senso con vento ≥ 3 km/h."""

    def __init__(self, model_dir: Path = M2_WIND10_DIR):
        self.meta = json.loads((model_dir / "meta.json").read_text())
        self.features = self.meta["features"]
        self.base_features = [c for c in self.features if c not in ("nwp_ws_valid", "nwp_wd_sin", "nwp_wd_cos")]
        self.tag = self.meta["model_tag"]
        self.airports = self.meta["airports"]
        self.boosters = {}
        for key, name in (("speed", "lgbm_speed"), ("dir_sin", "lgbm_dir_sin"), ("dir_cos", "lgbm_dir_cos")):
            with gzip.open(model_dir / f"{name}.txt.gz", "rt") as f:
                self.boosters[key] = lgb.Booster(model_str=f.read())

    def predict(self, nwp: pd.DataFrame, point: dict, valid_for: list[pd.Timestamp]) -> pd.DataFrame:
        """DataFrame (valid_for, w_ifs, w_m2, d_ifs, d_m2) per i valid_for con riga di input."""
        frame = build_m2_frame(nwp, point, self.base_features)
        w = nwp[["recorded_at", "wind_speed", "wind_direction"]].copy()
        w["recorded_at"] = pd.to_datetime(w.recorded_at).dt.tz_localize("UTC")
        w["nwp_wd_sin"] = np.sin(np.deg2rad(w.wind_direction))
        w["nwp_wd_cos"] = np.cos(np.deg2rad(w.wind_direction))
        frame = frame.merge(w.rename(columns={"recorded_at": "valid_for", "wind_speed": "nwp_ws_valid",
                                              "wind_direction": "nwp_wd_valid"})
                            [["valid_for", "nwp_ws_valid", "nwp_wd_valid", "nwp_wd_sin", "nwp_wd_cos"]],
                            on="valid_for", how="left")
        frame = frame[frame.valid_for.isin(valid_for)].dropna(subset=["nwp_ws_valid", "nwp_wd_valid"]).copy()
        if frame.empty:
            return pd.DataFrame(columns=["valid_for", "w_ifs", "w_m2", "d_ifs", "d_m2"])
        X = frame[self.features]
        speed = np.clip(np.expm1(np.log1p(frame.nwp_ws_valid.clip(lower=0).to_numpy())
                                 + self.boosters["speed"].predict(X)), 0.0, None)
        direction = np.rad2deg(np.arctan2(self.boosters["dir_sin"].predict(X), self.boosters["dir_cos"].predict(X))) % 360
        return pd.DataFrame({"valid_for": frame.valid_for.to_numpy(), "w_ifs": frame.nwp_ws_valid.to_numpy(),
                             "w_m2": speed, "d_ifs": frame.nwp_wd_valid.to_numpy(), "d_m2": direction})
