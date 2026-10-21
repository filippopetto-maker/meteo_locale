"""
m2b.py — M2b (temperatura): M2 senza feature stagionali, con più feature di bias

Addestrato da scripts/train_m2b.py. Previsione = IFS a valid_for + residuo LightGBM con le
feature di produzione (senza doy_sin, doy_cos, month), station_cat, nwp_valid e i riassunti del
bias IFS − Netatmo di bias_features (bias_st_hour, bias_h7, bias_h90, bias_all30, bias_d1,
bias_d3) più, se il modello li usa, bias_lag = giorni tra l'ultimo giorno con dati della tabella
e valid_for (1 per oggi, 2 domani, 3 dopodomani).

correct() applica la correzione sull'errore proprio: previsione − λ · errore medio di M2b per
stazione × ora UTC sui giorni precedenti (funzione SQL m2b_self_bias).

build_m2_frame è la stessa di m2.py, come in training.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import lightgbm as lgb
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (_PROJECT_ROOT, _PROJECT_ROOT / "model"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from m2 import build_m2_frame  # noqa: E402

M2B_DIR = _PROJECT_ROOT / "model" / "m2b"
BIAS_FEATURES = ("bias_st_hour", "bias_h7", "bias_h90", "bias_all30", "bias_d1", "bias_d3")
EXTRA_FEATURES = ("station_cat", "nwp_valid", *BIAS_FEATURES, "bias_lag")


class M2b:
    """Modello congelato in model/m2b/ (lgbm_m2b_temperature.txt.gz + meta.json)."""

    def __init__(self, model_dir: Path = M2B_DIR):
        self.meta = json.loads((model_dir / "meta.json").read_text())
        with gzip.open(model_dir / "lgbm_m2b_temperature.txt.gz", "rt") as f:
            self.booster = lgb.Booster(model_str=f.read())
        self.features = self.meta["features"]
        self.base_features = [c for c in self.features if c not in EXTRA_FEATURES]
        self.tag = self.meta["model_tag"]
        sc = self.meta.get("self_correction", {})
        self.self_lambda = float(sc.get("lambda", 1.0))
        self.self_min_days = int(sc.get("min_days", 7))

    def predict(self, nwp: pd.DataFrame, station: dict, valid_for: list[pd.Timestamp],
                bias: dict[int, dict[str, float | None]], window_end: pd.Timestamp) -> dict[pd.Timestamp, float]:
        """Temperatura M2b per ogni valid_for (UTC aware). `bias`: ora UTC → {feature: valore};
        `window_end`: giorno (00:00 UTC) escluso dalla bias_features, cioè quello in cui è stata calcolata."""
        frame = build_m2_frame(nwp, station, self.base_features)
        frame = frame[frame.valid_for.isin(valid_for)].dropna(subset=["nwp_valid"]).copy()
        if frame.empty:
            return {}
        hours = frame.valid_for.dt.hour
        for name in BIAS_FEATURES:
            frame[name] = pd.to_numeric([bias.get(h, {}).get(name) for h in hours], errors="coerce")
        days = (frame.valid_for.dt.floor("D") - pd.Timestamp(window_end).floor("D")).dt.days + 1
        frame["bias_lag"] = days.clip(lower=1, upper=3)
        pred = frame.nwp_valid.to_numpy() + self.booster.predict(frame[self.features])
        return dict(zip(frame.valid_for, pred))

    def correct(self, pred: float, self_bias: float | None) -> float:
        """Previsione corretta con l'errore medio proprio (nessuna correzione se manca)."""
        return pred if self_bias is None else pred - self.self_lambda * self_bias
