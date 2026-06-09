"""
arsial_bias_correction.py
═══════════════════════════════════════════════════════════════════════
Calcola il bias sistematico ERA5 vs ARSIAL per le 19 stazioni Roma.

Cosa fa:
  1. Carica data/arsial_roma_2023_2025.parquet (dati ARSIAL reali)
  2. Scarica ERA5 giornaliero da Open-Meteo per le stesse coordinate
  3. Calcola bias = ARSIAL − ERA5 per mese e stazione
  4. Salva data/arsial_bias_table.json → pronto per inference

Uso:
    cd ~/Desktop/meteo_locale
    conda activate meteo
    python3 arsial_bias_correction.py

Output:
    data/arsial_bias_table.json   — bias mensile per stazione
    data/arsial_era5_merged.parquet — dati allineati per audit
═══════════════════════════════════════════════════════════════════════
"""

import json
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Coordinate approssimate delle stazioni ARSIAL ────────────────────────────
# Fonte: posizione geografica nota dei comuni/frazioni
# Per coordinate precise: siarl.arsial.it → Stazioni → Localizzazione su mappa
STATION_COORDS = {
    "ROMA P. Nona":           {"lat": 41.872, "lon": 12.660, "zona": "EUR"},
    "MONTECOMPATRI C. Mattia":{"lat": 41.798, "lon": 12.778, "zona": "Castelli_Romani"},
    "S. GREGORIO DA SASSOLA": {"lat": 41.900, "lon": 12.878, "zona": "Tivoli_direzione"},
    "VELLETRI P. Lungo":      {"lat": 41.691, "lon": 12.780, "zona": "Castelli_Romani_sud"},
    "FIUMICINO T. Lepre":     {"lat": 41.766, "lon": 12.185, "zona": "Ostia_Lido"},
    "MONTEROTONDO G. Marozza":{"lat": 42.052, "lon": 12.641, "zona": "Nord_Roma"},
    "ROMA Capocotta":         {"lat": 41.720, "lon": 12.430, "zona": "Roma_sud_costiera"},
    "ROMA Lanciani":          {"lat": 41.908, "lon": 12.514, "zona": "Roma_centro"},
    "FRASCATI Prata":         {"lat": 41.821, "lon": 12.667, "zona": "Castelli_Frascati"},
    "GROTTAFERRATA":          {"lat": 41.792, "lon": 12.673, "zona": "Castelli_Romani"},
    "MONTEPORZIO":            {"lat": 41.820, "lon": 12.712, "zona": "Castelli_Romani"},
    "MARINO":                 {"lat": 41.774, "lon": 12.659, "zona": "Castelli_Romani"},
    "ZAGAROLO":               {"lat": 41.833, "lon": 12.832, "zona": "Est_Roma"},
    "FORMELLO":               {"lat": 42.088, "lon": 12.395, "zona": "Nord_ovest_Roma"},
    "MARCELLINA":             {"lat": 41.975, "lon": 12.787, "zona": "NordEst_Roma_Tivoli"},
    "FIUMICINO Maccarese":    {"lat": 41.805, "lon": 12.207, "zona": "Costiera_Fiumicino"},
    "MONTELIBRETTI":          {"lat": 42.098, "lon": 12.735, "zona": "NordEst_Roma"},
    "LADISPOLI":              {"lat": 41.954, "lon": 12.070, "zona": "Costiera_nord"},
    "GENAZZANO":              {"lat": 41.826, "lon": 12.968, "zona": "Est_Roma"},
}

ERA5_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# ── Funzioni ─────────────────────────────────────────────────────────────────

def fetch_era5_daily(lat: float, lon: float,
                     start: str = "2023-01-01",
                     end: str   = "2025-12-31") -> pd.DataFrame:
    """
    Scarica temperatura oraria ERA5 da Open-Meteo e aggrega a giornaliero.
    Ritorna DataFrame con colonne: date, era5_temp_min, era5_temp_med, era5_temp_max.
    """
    params = {
        "latitude":   lat,
        "longitude":  lon,
        "hourly":     "temperature_2m,relative_humidity_2m",
        "start_date": start,
        "end_date":   end,
        "timezone":   "UTC",
    }
    for attempt in range(3):
        try:
            r = requests.get(ERA5_ARCHIVE_URL, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            if attempt < 2:
                logger.warning(f"  Tentativo {attempt+1}/3 fallito: {e} — riprovo...")
                time.sleep(2 ** attempt)
            else:
                raise

    hourly = data["hourly"]
    df = pd.DataFrame({
        "datetime":   pd.to_datetime(hourly["time"]),
        "temp":       hourly["temperature_2m"],
        "humidity":   hourly["relative_humidity_2m"],
    })
    df["date"] = df["datetime"].dt.normalize()

    daily = df.groupby("date").agg(
        era5_temp_min =("temp",     "min"),
        era5_temp_med =("temp",     "mean"),
        era5_temp_max =("temp",     "max"),
        era5_hum_med  =("humidity", "mean"),
    ).reset_index()
    daily["era5_temp_med"] = daily["era5_temp_med"].round(2)
    daily["era5_hum_med"]  = daily["era5_hum_med"].round(1)
    return daily


def compute_bias_table(merged: pd.DataFrame) -> dict:
    """
    Calcola il bias mensile: ARSIAL − ERA5 per temperatura media.

    Struttura output:
      {
        "ROMA P. Nona": {
          "1": {"bias_temp_med": 1.2, "bias_temp_min": 0.8, "n_days": 28},
          "2": {...},
          ...
          "12": {...}
        },
        ...
      }
    """
    merged["month"] = merged["date"].dt.month
    bias = {}

    for station, grp in merged.groupby("Stazione"):
        station_bias = {}
        for month, mgrp in grp.groupby("month"):
            valid = mgrp.dropna(subset=["temp_med", "era5_temp_med"])
            n = len(valid)
            if n < 5:
                continue
            station_bias[str(month)] = {
                "bias_temp_med": round(float((valid["temp_med"] - valid["era5_temp_med"]).mean()), 3),
                "bias_temp_min": round(float((valid["temp_min"] - valid["era5_temp_min"]).mean()), 3),
                "bias_temp_max": round(float((valid["temp_max"] - valid["era5_temp_max"]).mean()), 3),
                "bias_hum_med":  round(float((valid["humidity_med"] - valid["era5_hum_med"]).mean()), 2)
                                  if valid["humidity_med"].notna().sum() > 5 else None,
                "n_days":        int(n),
                "mae_temp_med":  round(float((valid["temp_med"] - valid["era5_temp_med"]).abs().mean()), 3),
            }
        if station_bias:
            zona = STATION_COORDS.get(station, {}).get("zona", "unknown")
            bias[station] = {"zona": zona, "monthly": station_bias}

    return bias


def print_summary(bias_table: dict):
    """Stampa riepilogo bias annuale per stazione."""
    print("\n" + "="*75)
    print(f"  {'Stazione':<28} {'Zona':<22} {'Bias T_med':>10} {'MAE':>7} {'Mesi':>5}")
    print("="*75)
    for station, info in sorted(bias_table.items()):
        monthly = info["monthly"]
        biases = [v["bias_temp_med"] for v in monthly.values()]
        maes   = [v["mae_temp_med"]  for v in monthly.values()]
        mean_bias = round(sum(biases)/len(biases), 2)
        mean_mae  = round(sum(maes)/len(maes), 2)
        print(f"  {station:<28} {info['zona']:<22} {mean_bias:>+9.2f}°C {mean_mae:>6.2f}°C {len(monthly):>4}")
    print("="*75)
    print()
    print("  Bias positivo = ARSIAL più calda di ERA5 (effetto UHI / quota più bassa)")
    print("  Bias negativo = ARSIAL più fredda di ERA5 (effetto quota / esposizione)")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    project_root = Path(__file__).resolve().parent
    arsial_path  = project_root / "data" / "arsial_roma_2023_2025.parquet"
    out_bias     = project_root / "data" / "arsial_bias_table.json"
    out_merged   = project_root / "data" / "arsial_era5_merged.parquet"

    if not arsial_path.exists():
        raise FileNotFoundError(
            f"File ARSIAL non trovato: {arsial_path}\n"
            "Copia arsial_roma_2023_2025.parquet in data/"
        )

    # ── 1. Carica ARSIAL ──────────────────────────────────────────────────────
    arsial = pd.read_parquet(arsial_path)
    arsial["date"] = pd.to_datetime(arsial["date"])
    logger.info(f"ARSIAL caricato: {len(arsial):,} righe, "
                f"{arsial['Stazione'].nunique()} stazioni, "
                f"{arsial['date'].min().date()} → {arsial['date'].max().date()}")

    # ── 2. Scarica ERA5 per ogni stazione ─────────────────────────────────────
    era5_frames = []
    stations = [s for s in arsial["Stazione"].unique() if s in STATION_COORDS]
    logger.info(f"Scarico ERA5 per {len(stations)} stazioni...")

    for i, station in enumerate(stations, 1):
        coords = STATION_COORDS[station]
        logger.info(f"  [{i:02d}/{len(stations)}] {station} ({coords['lat']:.3f}, {coords['lon']:.3f})")
        try:
            era5 = fetch_era5_daily(coords["lat"], coords["lon"])
            era5["Stazione"] = station
            era5_frames.append(era5)
            time.sleep(0.3)  # rispetta rate limit Open-Meteo
        except Exception as e:
            logger.error(f"  ❌ {station}: {e}")

    era5_all = pd.concat(era5_frames, ignore_index=True)
    logger.info(f"ERA5 scaricato: {len(era5_all):,} righe")

    # ── 3. Merge ARSIAL + ERA5 ────────────────────────────────────────────────
    merged = arsial.merge(era5_all, on=["Stazione", "date"], how="inner")
    logger.info(f"Merge completato: {len(merged):,} giorni confrontabili")

    # ── 4. Calcola bias mensile ───────────────────────────────────────────────
    bias_table = compute_bias_table(merged)
    logger.info(f"Bias calcolato per {len(bias_table)} stazioni")
    print_summary(bias_table)

    # ── 5. Salva output ───────────────────────────────────────────────────────
    out_bias.parent.mkdir(exist_ok=True)

    with open(out_bias, "w", encoding="utf-8") as f:
        json.dump(bias_table, f, ensure_ascii=False, indent=2)
    logger.info(f"✅ Bias table salvata: {out_bias}")

    merged.to_parquet(out_merged, index=False)
    logger.info(f"✅ Dati allineati salvati: {out_merged}")

    # ── 6. Stampa i bias per le zone del progetto ─────────────────────────────
    print("\n📍 Bias per le stazioni più vicine alle zone progetto:\n")
    zone_priority = {
        "EUR":                "ROMA P. Nona",
        "Castelli_Romani":    "MONTECOMPATRI C. Mattia",
        "Tivoli_direzione":   "S. GREGORIO DA SASSOLA",
        "Ostia_Lido":         "FIUMICINO T. Lepre",
        "Nord_Roma":          "MONTEROTONDO G. Marozza",
        "Roma_sud_costiera":  "ROMA Capocotta",
    }
    for zona, station in zone_priority.items():
        if station not in bias_table:
            continue
        monthly = bias_table[station]["monthly"]
        summer = [monthly.get(str(m), {}).get("bias_temp_med", float("nan")) for m in [6,7,8]]
        winter = [monthly.get(str(m), {}).get("bias_temp_med", float("nan")) for m in [12,1,2]]
        b_sum = round(sum(x for x in summer if not pd.isna(x)) / max(1, sum(1 for x in summer if not pd.isna(x))), 2)
        b_win = round(sum(x for x in winter if not pd.isna(x)) / max(1, sum(1 for x in winter if not pd.isna(x))), 2)
        print(f"  {zona:<22} ({station:<28}) → estate: {b_sum:+.2f}°C  inverno: {b_win:+.2f}°C")

    print(f"\nProssimo step: usa arsial_bias_table.json in inference.py per correggere")
    print("sistematicamente le previsioni per le zone non nel training set originale.")


if __name__ == "__main__":
    main()
