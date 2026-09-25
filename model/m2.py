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
EXTRA_FEATURES = ("station_cat", "nwp_valid", "bias_st_hour")


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
