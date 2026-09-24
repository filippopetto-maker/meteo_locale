"""
inference.py — Previsione operativa real-time
Blocco 3 - Fase 4

Pipeline operativa:
  1. Per ciascuna stazione attiva, scarica le previsioni Open-Meteo
     (past_days=2 per warm-up di lag/rolling, forecast_days=4 per il futuro).
  2. Applica build_feature_matrix() come in training (stesse colonne, stesso ordine).
  3. Predice i lead richiesti con LightGBM (lgbm_temperature.txt): per
     valid_for = V usa la riga NWP a V−1h (contratto appreso: X → X+1h).
  4. Se esiste rf_correttore_temperature.pkl, applica la correzione RF
     (corrected=True nel DB). Altrimenti, salva la previsione LGBM grezza
     (corrected=False).
  5. Inserisce la previsione su Supabase via db.insert_forecast().

Le previsioni di wind_speed / wind_direction sono pass-through dell'NWP
finché i modelli MOS dedicati non sono addestrati.

Utilizzo:
    python3 model/inference.py
    python3 model/inference.py --horizon 3
    python3 model/inference.py --dry-run
    python3 model/inference.py --dry-run --max-lead 48 --json-out /tmp/f48.json
    python3 model/inference.py --max-lead 48 --db-leads 3,6,12 --allow-multi-lead
"""

from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")

import argparse
import json
import logging
import pickle
import sys
import time as _time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# Lo script vive in model/, ma forecast.py / db.py / features.py / historical.py
# sono nella project root un livello sopra. Aggiunge la root a sys.path.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import lightgbm as lgb
import pandas as pd
import requests

from features import build_feature_matrix
from forecast import get_feature_cols
from historical import ERA5_VARIABLES, ERA5_RENAME

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configurazione
# ─────────────────────────────────────────────────────────────────────────────

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Directory dei modelli (relativa alla project root).
MODEL_DIR = _PROJECT_ROOT / "model"


# ─────────────────────────────────────────────────────────────────────────────
# Fetch Open-Meteo Forecast
# ─────────────────────────────────────────────────────────────────────────────

def fetch_forecast(
    lat: float,
    lon: float,
    past_days: int = 2,
    forecast_days: int = 4,
    retries: int = 3,
    model: Optional[str] = None,
) -> pd.DataFrame:
    """
    Scarica previsioni Open-Meteo (forecast API, non archive).

    past_days serve a popolare le feature lag/rolling che richiedono
    osservazioni delle ore precedenti (max lag = 6h, max rolling = 12h).

    Args:
        lat, lon:      coordinate della stazione.
        past_days:     giorni di storico (default 2).
        forecast_days: giorni di previsione futura (default 4). Open-Meteo
                       parte dalla mezzanotte UTC del giorno corrente: con 2
                       giorni alle 21 UTC resterebbero ~27 h di futuro, con 4
                       ne restano sempre almeno 48.
        model:         modello NWP Open-Meteo (es. "ecmwf_ifs"); None = blend
                       di default.

    Returns:
        DataFrame con colonna 'recorded_at' (UTC naive) e le variabili
        rinominate secondo lo schema progetto (stessi nomi di historical.py).
    """
    params = {
        "latitude":      lat,
        "longitude":     lon,
        "hourly":        ",".join(ERA5_VARIABLES),
        "past_days":     past_days,
        "forecast_days": forecast_days,
        "timezone":      "UTC",
    }
    if model:
        params["models"] = model

    last_exc: Exception = RuntimeError("Nessun tentativo eseguito")
    for attempt in range(retries):
        try:
            r = requests.get(OPEN_METEO_FORECAST_URL, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            break
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries - 1:
                wait = 2 ** attempt
                logger.warning(
                    f"Open-Meteo tentativo {attempt + 1}/{retries} fallito: {exc}. "
                    f"Riprovo in {wait}s"
                )
                _time.sleep(wait)
    else:
        raise last_exc

    hourly = data["hourly"]
    df = pd.DataFrame({"recorded_at": pd.to_datetime(hourly["time"])})
    for var in ERA5_VARIABLES:
        if var in hourly:
            df[var] = hourly[var]

    df = df.rename(columns={k: v for k, v in ERA5_RENAME.items() if k in df.columns})
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Caricamento modelli (cache per evitare ricarichi in loop multi-stazione)
# ─────────────────────────────────────────────────────────────────────────────

_BOOSTER_CACHE: dict[Path, lgb.Booster] = {}
_RF_CACHE:      dict[Path, object]      = {}
_ARSIAL_BIAS:   dict | None             = None

# Mapping stazione progetto → stazione proxy ARSIAL per correzione bias mensile.
# Stazione id=3 (Roma Sud) esclusa: era nel training set, nessuna correzione.
ARSIAL_PROXY: dict[int, str] = {
    25: "FIUMICINO T. Lepre",        # Ostia Lido
    26: "ROMA P. Nona",              # EUR
    27: "ROMA Lanciani",             # Trastevere
    28: "S. GREGORIO DA SASSOLA",    # Tivoli
    29: "MONTECOMPATRI C. Mattia",   # Castelli Romani
    33: "ROMA Capocotta",            # Pratica di Mare
    34: "LADISPOLI",                 # Cerveteri Ladispoli
    35: "MONTEROTONDO G. Marozza",   # Saxa Rubra
    36: "FORMELLO",                  # Selva Nera
    37: "VELLETRI P. Lungo",         # Cisterna Latina
    38: "FORMELLO",                  # Bracciano (condiviso con Selva Nera)
}


def load_arsial_bias() -> dict:
    """
    Carica data/arsial_bias_table.json con i bias mensili ARSIAL–ERA5.
    Usa cache modulo-level per evitare ricarichi in loop multi-stazione.
    Ritorna dict vuoto se il file manca (fallback silenzioso — no crash).
    """
    global _ARSIAL_BIAS
    if _ARSIAL_BIAS is not None:
        return _ARSIAL_BIAS
    bias_path = _PROJECT_ROOT / "data" / "arsial_bias_table.json"
    try:
        with bias_path.open() as f:
            _ARSIAL_BIAS = json.load(f)
        logger.info(f"ARSIAL bias table caricata: {len(_ARSIAL_BIAS)} stazioni proxy")
    except Exception as exc:
        logger.warning(f"ARSIAL bias table non disponibile ({exc}) — nessuna correzione")
        _ARSIAL_BIAS = {}
    return _ARSIAL_BIAS


def load_lgbm(target: str) -> lgb.Booster:
    """Carica il booster LightGBM nativo (.txt) dalla cache o da disco."""
    path = MODEL_DIR / f"lgbm_{target}.txt"
    if path not in _BOOSTER_CACHE:
        if not path.exists():
            raise FileNotFoundError(
                f"Modello LGBM non trovato: {path}\n"
                f"Esegui prima: python3 forecast.py --target {target}"
            )
        _BOOSTER_CACHE[path] = lgb.Booster(model_file=str(path))
        logger.info(f"LGBM caricato: {path}")
    return _BOOSTER_CACHE[path]


def load_rf(target: str) -> Optional[object]:
    """
    Carica il correttore RF se esiste, altrimenti None (fallback a LGBM solo).
    """
    path = MODEL_DIR / f"rf_correttore_{target}.pkl"
    if path not in _RF_CACHE:
        if not path.exists():
            logger.info(f"RF correttore assente ({path.name}) — fallback LGBM solo")
            _RF_CACHE[path] = None
        else:
            with path.open("rb") as f:
                _RF_CACHE[path] = pickle.load(f)
            logger.info(f"RF correttore caricato: {path}")
    return _RF_CACHE[path]


# ─────────────────────────────────────────────────────────────────────────────
# Predizione per UNA stazione, N lead
# ─────────────────────────────────────────────────────────────────────────────

def _num(v) -> float:
    return float(v) if pd.notna(v) else float("nan")


def predict_series(
    station: dict,
    leads: list[int],
    target: str = "temperature",
    nwp_model: Optional[str] = None,
) -> list[dict]:
    """
    Genera le previsioni per i lead richiesti con una sola fetch e una sola
    predict. Contratto del modello: riga NWP all'ora X → temperatura a X+1h,
    quindi per valid_for = V si usa la riga a V−1h (per il lead 1 è la riga
    "adesso", identica al comportamento storico T+1h).

    Returns:
        Lista di dict (uno per lead calcolato) pronti per db.insert_forecast().
        Un lead senza riga di input viene saltato con un warning.
    """
    df = fetch_forecast(station["lat"], station["lon"], model=nwp_model)
    if df.empty:
        logger.warning(f"[st.{station['id']}] Open-Meteo vuoto, skip")
        return []
    return predict_from_df(df, station, leads, target=target)


def predict_from_df(
    df: pd.DataFrame,
    station: dict,
    leads: list[int],
    target: str = "temperature",
    now_utc: Optional[datetime] = None,
) -> list[dict]:
    """
    Come predict_series ma su una serie NWP già scaricata. now_utc (aware,
    default: ora corrente troncata all'ora) permette di simulare un'emissione
    passata nel backtest.
    """
    sid = station["id"]

    # ── 2. Feature engineering (stessi 5 strati di training) ─────────────────
    feat_df = build_feature_matrix(df, station)

    # ── 3. Allinea le colonne a quelle attese dal LGBM ───────────────────────
    booster      = load_lgbm(target)
    feature_cols = booster.feature_name()

    # Aggiungi colonne mancanti come NaN (es. microclima_* assenti su questa
    # stazione). LightGBM gestisce i NaN nativamente.
    for col in feature_cols:
        if col not in feat_df.columns:
            feat_df[col] = pd.NA

    # ── 4. Per ogni lead: valid_for = now + L, riga di input a valid_for − 1h ─
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    now_utc   = now_utc.replace(minute=0, second=0, microsecond=0)
    now_naive = now_utc.replace(tzinfo=None)

    feat_df = feat_df.sort_values("recorded_at").reset_index(drop=True)
    row_idx = pd.Series(feat_df.index, index=feat_df["recorded_at"])
    row_idx = row_idx[~row_idx.index.duplicated(keep="last")]

    idxs, used_leads, skipped = [], [], []
    for L in leads:
        input_at = now_naive + timedelta(hours=L - 1)
        if input_at in row_idx.index:
            idxs.append(int(row_idx[input_at]))
            used_leads.append(L)
            continue
        if L == 1:
            # Fallback storico del job T+1h: ultima riga ≤ adesso.
            eligible = feat_df[feat_df["recorded_at"] <= now_naive]
            if not eligible.empty:
                idxs.append(int(eligible.index[-1]))
                used_leads.append(L)
                continue
        skipped.append(L)
    if skipped:
        logger.warning(f"[st.{sid}] Lead senza riga di input, saltati: {skipped}")
    if not idxs:
        logger.warning(
            f"[st.{sid}] Nessuna riga di input utilizzabile, "
            f"range disponibile: {feat_df['recorded_at'].min()} → {feat_df['recorded_at'].max()}"
        )
        return []

    X = feat_df.loc[idxs, feature_cols].apply(pd.to_numeric, errors="coerce")

    # ── 5. LGBM predict ──────────────────────────────────────────────────────
    lgbm_pred = booster.predict(X)

    # ── 6. Correttore RF se disponibile ──────────────────────────────────────
    rf = load_rf(target)
    if rf is not None:
        final_pred = lgbm_pred + rf.predict(X.assign(lgbm_pred=lgbm_pred))
        corrected  = True
    else:
        final_pred = lgbm_pred.copy()
        corrected  = False

    valid_fors = [now_utc + timedelta(hours=L) for L in used_leads]

    # ── 6b. Correzione ARSIAL bias mensile, sul mese di valid_for ────────────
    # bias_temp_med = ARSIAL − ERA5: positivo → zona più calda di ERA5.
    # Sommiamo il bias a final_pred perché il modello, addestrato su stazioni
    # pianura/costiere, sottostima sistematicamente le zone non nel training.
    if target == "temperature":
        arsial_proxy = ARSIAL_PROXY.get(sid)
        if arsial_proxy is not None:
            applied = {}
            for i, vf in enumerate(valid_fors):
                month_key = str(vf.month)
                try:
                    bias = load_arsial_bias()[arsial_proxy]["monthly"][month_key]["bias_temp_med"]
                except (KeyError, TypeError):
                    continue  # JSON mancante o chiave assente — fallback silenzioso
                final_pred[i] += bias
                applied[month_key] = bias
            if applied:
                desc = ", ".join(f"mese {m} {b:+.3f}°C" for m, b in applied.items())
                logger.info(f"[ARSIAL bias] st.{sid} {desc} (stazione {arsial_proxy})")

    # ── 7. Pass-through NWP sulla riga valid_for ─────────────────────────────
    nwp_by_time = df.drop_duplicates("recorded_at", keep="last").set_index("recorded_at")

    out = []
    for i, (L, vf) in enumerate(zip(used_leads, valid_fors)):
        key = vf.replace(tzinfo=None)
        nwp = nwp_by_time.loc[key] if key in nwp_by_time.index else None
        get = (lambda c: _num(nwp.get(c))) if nwp is not None else (lambda c: float("nan"))
        ws, wd, rh, t_nwp = get("wind_speed"), get("wind_direction"), get("humidity"), get("temperature")
        out.append({
            "forecast_at":     now_utc,
            "valid_for":       vf,
            "lead_hours":      L,
            "temperature":     round(float(final_pred[i]), 2) if target == "temperature" else float("nan"),
            "wind_speed":      round(ws, 2)    if pd.notna(ws)    else None,
            "wind_direction":  round(wd, 1)    if pd.notna(wd)    else None,
            "humidity":        round(rh, 1)    if pd.notna(rh)    else None,
            "nwp_temperature": round(t_nwp, 2) if pd.notna(t_nwp) else None,
            "nwp_humidity":    round(rh, 1)    if pd.notna(rh)    else None,
            "corrected":       corrected,
            "lgbm_pred":       round(float(lgbm_pred[i]), 2),  # solo per logging dry-run
        })
    return out


def predict_station(
    station: dict,
    horizon_hours: int,
    target: str = "temperature",
) -> Optional[dict]:
    """Wrapper retrocompatibile: un solo lead, None se non calcolabile."""
    preds = predict_series(station, [horizon_hours], target=target)
    return preds[0] if preds else None


# ─────────────────────────────────────────────────────────────────────────────
# Orchestratore
# ─────────────────────────────────────────────────────────────────────────────

def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_num(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else v


def write_series_json(path: str, results: list[dict], stations: list[dict],
                      leads: list[int], nwp_model: Optional[str],
                      model_version: str) -> None:
    names = {st["id"]: st.get("name", "") for st in stations}
    by_station: dict[str, dict] = {}
    for p in results:
        entry = by_station.setdefault(
            str(p["station_id"]), {"name": names.get(p["station_id"], ""), "series": []}
        )
        entry["series"].append({
            "valid_for": _iso_z(p["valid_for"]),
            "lead":      p["lead_hours"],
            "t":         _json_num(p["temperature"]),
            "t_nwp":     _json_num(p["nwp_temperature"]),
            "rh":        _json_num(p["humidity"]),
            "ws":        _json_num(p["wind_speed"]),
            "wd":        _json_num(p["wind_direction"]),
            "corrected": p["corrected"],
        })
    forecast_at = results[0]["forecast_at"] if results else None
    payload = {
        "generated_at":  _iso_z(datetime.now(timezone.utc)),
        "forecast_at":   _iso_z(forecast_at) if forecast_at else None,
        "nwp_model":     nwp_model or "default",
        "model_version": model_version,
        "leads":         leads,
        "stations":      by_station,
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    logger.info(f"JSON serie scritto: {out} ({len(results)} righe)")


def run(
    leads: Optional[list[int]] = None,
    db_leads: Optional[list[int]] = None,
    target: str = "temperature",
    dry_run: bool = False,
    model_version: Optional[str] = None,
    nwp_model: Optional[str] = None,
    json_out: Optional[str] = None,
    allow_multi_lead: bool = False,
) -> list[dict]:
    """
    Esegue l'inference su tutte le stazioni attive.

    Args:
        leads:            lead in ore da calcolare (default [1]).
        db_leads:         sottoinsieme di leads da salvare su DB (None = tutti).
        target:           variabile target del modello LGBM (default "temperature").
        dry_run:          se True, stampa le previsioni senza scriverle su DB.
        model_version:    versione da loggare; se None usa "inference_v%Y%m%d_%H%M".
        nwp_model:        modello NWP Open-Meteo (None = blend di default).
        json_out:         se valorizzato, scrive la serie completa in JSON.
        allow_multi_lead: consente scritture su DB con lead > 1.

    Returns:
        Lista dei dict di previsione (uno per stazione e lead calcolato).
    """
    leads = sorted(set(leads or [1]))
    db_leads = leads if db_leads is None else sorted(set(db_leads) & set(leads))

    # Finché esiste il vincolo ponte UNIQUE (station_id, valid_for), una riga
    # con lead > 1 blocca l'upsert successivo del lead 1 sullo stesso
    # valid_for, e l'insert fallisce in silenzio (try/except non bloccante).
    if not dry_run and db_leads and max(db_leads) > 1 and not allow_multi_lead:
        logger.error(
            "Scrittura su DB di lead > 1 rifiutata: finché esiste il vincolo ponte "
            "forecasts_station_valid_unique (station_id, valid_for), un lead > 1 "
            "impedirebbe al job da 30 min di scrivere il lead 1 sullo stesso valid_for. "
            "Usa --dry-run, --db-leads 1, oppure --allow-multi-lead dopo aver rimosso il ponte."
        )
        sys.exit(2)

    if model_version is None:
        model_version = "inference_" + datetime.utcnow().strftime("v%Y%m%d_%H%M")

    # Lazy import: evita di richiedere dotenv/supabase solo per --help
    import db
    stations = db.get_active_stations()
    if not stations:
        logger.error("Nessuna stazione attiva in DB")
        return []

    single = len(leads) == 1
    lead_desc = f"T+{leads[0]}h" if single else f"{len(leads)} lead ({leads[0]}…{leads[-1]}h)"
    sep = "=" * 70
    print(f"\n{sep}")
    print(f"Inference   : {lead_desc}  |  target={target}  |  "
          f"{'DRY-RUN' if dry_run else 'DB INSERT'}")
    if nwp_model:
        print(f"Modello NWP : {nwp_model}")
    print(f"Stazioni    : {len(stations)} attive")
    print(f"Versione    : {model_version}")
    print(f"{sep}")

    results: list[dict] = []
    n_ok = 0
    for st in stations:
        sid  = st["id"]
        name = st.get("name", "")
        try:
            preds = predict_series(st, leads, target=target, nwp_model=nwp_model)
        except Exception as exc:
            logger.error(f"[st.{sid} {name}] Errore: {exc}")
            continue
        if not preds:
            continue
        n_ok += 1

        if single:
            pred = preds[0]
            corr_tag = "RF" if pred["corrected"] else "LGBM only"
            print(
                f"st.{sid:>2} {name:<24} | "
                f"valid {pred['valid_for'].strftime('%Y-%m-%d %H:%M UTC')} | "
                f"T={pred['temperature']:>5.2f}°C  ({corr_tag}, lgbm_raw={pred['lgbm_pred']:.2f})"
            )
        else:
            by_lead = {p["lead_hours"]: p for p in preds}
            snap = "  ".join(
                f"+{L}h={by_lead[L]['temperature']:.2f}" for L in (1, 24, 48) if L in by_lead
            )
            missing = sorted(set(leads) - set(by_lead))
            print(
                f"st.{sid:>2} {name:<24} | {len(preds)}/{len(leads)} lead | {snap}"
                + (f" | saltati {missing}" if missing else "")
            )

        if not dry_run:
            for pred in preds:
                if pred["lead_hours"] not in db_leads:
                    continue
                try:
                    fid = db.insert_forecast(
                        station_id      = sid,
                        forecast_at     = pred["forecast_at"],
                        valid_for       = pred["valid_for"],
                        temperature     = pred["temperature"],
                        wind_speed      = pred["wind_speed"],
                        wind_direction  = pred["wind_direction"],
                        humidity        = pred["humidity"],
                        model_version   = model_version,
                        corrected       = pred["corrected"],
                        lead_hours      = pred["lead_hours"],
                        nwp_temperature = pred["nwp_temperature"],
                        nwp_humidity    = pred["nwp_humidity"],
                    )
                    if single:
                        print(f"    └─ Supabase id={fid}")
                except Exception as exc:
                    logger.warning(
                        f"[st.{sid}] Insert lead {pred['lead_hours']} fallito (non bloccante): {exc}"
                    )

        results.extend({**p, "station_id": sid} for p in preds)

    if json_out:
        write_series_json(json_out, results, stations, leads, nwp_model, model_version)

    print(f"{sep}")
    print(f"Completate  : {n_ok}/{len(stations)} stazioni")
    print(f"{sep}\n")
    return results


def _parse_leads(s: str) -> list[int]:
    try:
        leads = [int(x) for x in s.split(",") if x.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"lista di lead non valida: {s!r}")
    if not leads or min(leads) < 1:
        raise argparse.ArgumentTypeError(f"i lead devono essere interi ≥ 1: {s!r}")
    return leads


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    ap = argparse.ArgumentParser(
        description="Previsione operativa real-time (LGBM + opzionale correttore RF)"
    )
    lead_group = ap.add_mutually_exclusive_group()
    lead_group.add_argument("--horizon", type=int, default=None,
                            help="Orizzonte previsionale in ore (default: 1); equivale a --leads N")
    lead_group.add_argument("--leads", type=_parse_leads, default=None,
                            help="Lead da calcolare, es. 1,3,6")
    lead_group.add_argument("--max-lead", type=int, default=None,
                            help="Calcola i lead 1…N")
    ap.add_argument("--db-leads", type=_parse_leads, default=None,
                    help="Sottoinsieme dei lead da salvare su DB (default: tutti)")
    ap.add_argument("--allow-multi-lead", action="store_true",
                    help="Consente di scrivere su DB lead > 1 (solo dopo la rimozione del vincolo ponte)")
    ap.add_argument("--nwp-model", default=None,
                    help="Modello NWP Open-Meteo, es. ecmwf_ifs (default: blend di Open-Meteo)")
    ap.add_argument("--json-out", default=None,
                    help="Scrive la serie completa in questo file JSON (non sotto docs/)")
    ap.add_argument("--target",  default="temperature",
                    choices=["temperature", "wind_speed", "wind_direction",
                             "humidity", "pressure"],
                    help="Variabile target del modello LGBM (default: temperature)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Stampa le previsioni senza scriverle su Supabase")
    ap.add_argument("--model-version", default=None,
                    help="Versione da loggare in forecasts (default: timestamp automatico)")
    args = ap.parse_args()

    if args.leads:
        leads = args.leads
    elif args.max_lead:
        if args.max_lead < 1:
            ap.error("--max-lead deve essere ≥ 1")
        leads = list(range(1, args.max_lead + 1))
    else:
        leads = [args.horizon or 1]

    run(
        leads=leads,
        db_leads=args.db_leads,
        target=args.target,
        dry_run=args.dry_run,
        model_version=args.model_version,
        nwp_model=args.nwp_model,
        json_out=args.json_out,
        allow_multi_lead=args.allow_multi_lead,
    )
