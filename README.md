# meteo_locale

Previsione meteo locale per l'area di Roma — modello MOS (Model Output
Statistics) a due stadi: LightGBM addestrato su ERA5/METAR + Random Forest
correttore dei residui.

## Architettura

```
ERA5 (Open-Meteo Archive)  ──┐
                              ├──► historical.py ──► training set parquet
METAR (Iowa State IEM)    ──┘                                │
                                                              ▼
                                       forecast.py (LightGBM, 5 strati feature)
                                                              │
                                                              ▼
                                       model/correttore.py (RF sui residui)
                                                              │
                                                              ▼
Open-Meteo Forecast API  ──► model/inference.py ──► Supabase (tabella forecasts)
                                                              │
                                                              ▼
                                                  output/dashboard.py (Streamlit)
```

## Stack

- **Python 3.12** (conda env `meteo`, installer Miniforge)
- **Supabase** (PostgreSQL gestito) — tabelle: `stations`, `observations`,
  `forecasts`, `model_metrics`, vista `latest_observations`
- **LightGBM** — primo stadio, salvato in formato nativo `.txt`
- **scikit-learn RandomForest** — secondo stadio (correttore residui)
- **Open-Meteo** API gratuite — archive ERA5 (training) + forecast (inference)
- **Iowa State IEM ASOS** — METAR storico (truth label) gratuito
- **Streamlit** — dashboard read-only
- **GitHub Actions** — scheduling inference operativa

Dipendenze pinnate in `requirements.txt`.

## Stazioni attive

| id | Nome                     | ICAO METAR | Microclima      |
|----|--------------------------|------------|-----------------|
| 1  | Roma Nord                | LIRA       | standard        |
| 2  | Roma Centro              | LIRA       | urban_canyon    |
| 3  | Roma Sud (Casal Palocco) | LIRF       | verde_parco     |
| 4  | Ostia                    | LIRF       | costiera        |

Coordinate e metadati orografici in tabella `stations` su Supabase.

## Stato attuale

### Fase 1 — Modello sullo storico  · **85%**

- ✅ `historical.py` — ERA5 + METAR → parquet (10 anni, 331k righe)
- ✅ `features.py` — 5 strati di feature engineering (67 feature totali)
- ✅ `forecast.py` — LightGBM addestrato su 4 target:
  - `temperature` — val MAE **0.869 °C**
  - `wind_speed` — val MAE **2.717 km/h**
  - `wind_direction` — val MAE **44.47 °**
  - `humidity` — val MAE **5.73 %**
- ✅ `correttore.py` — RF correttore attivo per **temperature** e **wind_direction**;
  `wind_speed` e `humidity` usano LGBM puro (RF non migliora su residui non strutturati,
  vedi tabella in [RF correttore (stadio 2)](#rf-correttore-stadio-2))
- ❌ rischio temporali e pioggia puntuale — rimandato alla Fase 3

### Fase 2 — Pipeline live  · **20%**

- ✅ `inference.py` — genera previsioni live per le 4 stazioni, scrive su Supabase
- ✅ `.github/workflows/inference.yml` — workflow cron ogni 30 min creato,
  **da attivare con push del repo + configurazione secrets**
- ❌ `mainMETEO.py` — raccolta dati live Netatmo/ARPA non ancora implementata
- ❌ Tabella `observations` su Supabase ancora vuota (nessun loop osservato vs previsto)

### Fase 3 — Output  · **30%**

- ✅ `output/dashboard.py` — Streamlit con 3 sezioni:
  ultime previsioni, grafico previsto vs osservato 48h, metriche correnti
- ❌ API FastAPI per esporre le previsioni
- ❌ Mappa Cartopy con interpolazione spaziale
- ❌ Tuning iperparametri LGBM/RF
- ❌ Espansione a >4 stazioni (richiesta per attivare LCZ + impermeabilità)

## Struttura del progetto

```
meteo_locale/
├── db.py                    # client Supabase + CRUD
├── qc.py                    # QC 4 livelli (range/clima/persistence/spatial)
├── features.py              # feature engineering 5 strati
├── historical.py            # builder training set ERA5 + METAR
├── forecast.py              # trainer LightGBM (stadio 1)
├── model/
│   ├── correttore.py        # trainer RF residui (stadio 2)
│   ├── inference.py         # previsione operativa real-time
│   ├── lgbm_{target}.txt    # modelli LGBM nativi (artefatti)
│   ├── rf_correttore_{target}.pkl  # modelli RF (artefatti)
│   └── feature_importance_{target}.json
├── output/
│   └── dashboard.py         # dashboard Streamlit
├── data/
│   ├── training_10y_h1.parquet     # training set 10 anni, horizon T+1h
│   └── training.parquet
├── logs/
│   └── training_log.txt
├── .github/workflows/
│   └── inference.yml        # cron 30 min su GitHub Actions
└── requirements.txt
```

## Feature engineering (5 strati, `features.py`)

1. **Temporali**: `hour_sin/cos`, `doy_sin/cos`, `month`, `is_weekend`, `is_daytime`
2. **Lag**: `{col}_lag_{1,2,3,6}` per `temperature`, `wind_speed`, `wind_direction`, `humidity`
3. **Rolling**: `{col}_roll_mean_{3,6,12}`, `{col}_roll_std_{3,6,12}` con `shift(1)` (no look-ahead)
4. **Derivate**: `wind_u`, `wind_v`, `temp_trend_1h`, `pressure_trend_1h`, `wind_speed_trend_1h`, `wind_chill`
5. **Orografiche** (statiche per stazione): `altitude`, `dist_sea_km`, `dist_center_km`, `bearing_sea`, `onshore_alignment`, `microclima_*` one-hot

Totale: **67 feature** dopo il merge ERA5 + 5 strati.

### Look-ahead bias prevention
- ERA5 a tempo T = reanalisi, nessuna info dal futuro
- `shift(n≥1)` per lag/rolling → solo passato
- Target prodotto con `shift(-horizon_hours)` ma è label, non entra in X

## Modelli addestrati

Dataset: `data/training_10y_h1.parquet` (10 anni, 331,170 righe, 76 colonne).
Split temporale 80/20 (no shuffle): train 2015-01-01 → 2023-01-07,
val 2023-01-07 → 2024-12-30.

### LightGBM (stadio 1) — metriche val

| Target           | MAE val   | RMSE val   | Best iter | Top-3 feature              |
|------------------|-----------|------------|-----------|----------------------------|
| `temperature`    | (trained) | (trained)  | —         | —                          |
| `wind_speed`     | **2.7169 km/h**  | 3.7128 km/h | 432 / 1000 | wind_speed, wind_speed_lag_1, shortwave_radiation |
| `wind_direction` | **44.4718°**     | 66.9540°    | 186 / 1000 | wind_u, wind_direction, shortwave_radiation |
| `humidity`       | **5.7258 %**     | 7.4342 %    | 354 / 1000 | humidity, humidity_lag_1, shortwave_radiation |

> `wind_direction` MAE elevato è atteso: la grandezza è circolare e non
> gestita con perdita angolare dedicata in questa fase. La feature `wind_u`
> domina l'importance proprio perché linearizza la componente est/ovest del
> vettore.

### RF correttore (stadio 2)

Architettura:

```python
RandomForestRegressor(
    n_estimators=200,
    max_depth=6,          # alberi corti: i residui LGBM sono già piccoli
    min_samples_leaf=10,  # regolarizzazione contro overfit sul rumore
    n_jobs=-1,            # parallelismo multicore (critico su >100k righe)
    random_state=42,
)
```

Addestrato sui residui `y_train - lgbm_pred_train`, con `lgbm_pred` come
feature aggiuntiva. Stesso split temporale di forecast.py (split row-identical
garantito riusando `temporal_split`, `get_feature_cols` da `forecast.py`).

Modelli salvati: `model/rf_correttore_{target}.pkl`.

#### Metriche val (delta vs LGBM puro)

| Target | LGBM MAE | LGBM+RF MAE | Δ MAE | LGBM RMSE | LGBM+RF RMSE | Δ RMSE | File `.pkl` | Decisione |
|---|---|---|---|---|---|---|---|---|
| `temperature` | (in training_log) | — | — | — | — | — | 875 KB | usato a inference |
| `wind_speed` | 2.7169 km/h | 2.7416 km/h | **+0.025** ❌ | 3.7128 km/h | 3.7658 km/h | +0.053 ❌ | **4.8 GB** ⚠️ | **scartato**: peggiora e file oversized |
| `wind_direction` | 44.4718° | 43.5161° | **−0.956** ✅ | 66.9540° | 67.1875° | +0.234 | 1.8 MB | **usato a inference** (riaddestrato con `max_depth=6`, train ~1m46s) |
| `humidity` | 5.7258% | 5.7297% | +0.004 ≈ | 7.4342% | 7.4597% | +0.026 ❌ | 1.8 MB | **scartato**: nessun miglioramento |

> Il file `rf_correttore_wind_speed.pkl` da 4.8 GB è il sintomo dell'errore
> #12 (parametri "minimal" prima del fix): alberi cresciuti completi su 264k
> righe con residui di vento molto rumorosi → modello esploso. Il fix del
> codice è già applicato; i file vecchi vanno cancellati e i correttori
> riaddestrati selettivamente solo dove portano valore (`wind_direction`).

## Quickstart

```bash
# 1. Setup env
conda activate meteo
pip install -r requirements.txt

# 2. Verifica DB
python3 db.py

# 3. Costruisci training set (10 anni, T+1h)
python3 historical.py --start 2015-01-01 --end 2024-12-30 --horizon 1 \
    --out data/training_10y_h1.parquet

# 4. Addestra LGBM per target (esempi)
python3 forecast.py --data data/training_10y_h1.parquet --target temperature
python3 forecast.py --data data/training_10y_h1.parquet --target wind_speed

# 5. Addestra RF correttore (richiede LGBM già pronto)
python3 model/correttore.py --data data/training_10y_h1.parquet --target temperature

# 6. Inference (dry-run prima, poi reale)
python3 model/inference.py --dry-run --horizon 1
python3 model/inference.py --horizon 1

# 7. Dashboard
streamlit run output/dashboard.py
```

Tutti gli script supportano `--help`. Quelli di training accettano `--no-db`
per saltare la scrittura su `model_metrics`.

## Inference operativa (GitHub Actions)

`.github/workflows/inference.yml` lancia `python3 model/inference.py --horizon 1`
ogni 30 minuti (cron `0,30 * * * *` UTC) su `ubuntu-latest` con
`conda-incubator/setup-miniconda@v3`, env `meteo`, Python 3.12.

**Setup richiesto su GitHub**:
1. Push del repo
2. Settings → Secrets and variables → Actions → New repository secret:
   - `SUPABASE_URL`
   - `SUPABASE_KEY`

Il workflow ha `timeout-minutes: 15` e `workflow_dispatch` per trigger manuale.

## Cronologia degli interventi

### Step 1 — fondamenta (pre-esistente)
- `db.py`: client Supabase + CRUD per `stations`/`observations`
- `qc.py`: QC 4 livelli (range, climatologico, persistence, spatial), flag 0/1/2
- `features.py`: 5 strati di feature engineering, entry point `build_feature_matrix(df, station)`

### Step 2 — historical training set
- Creato `historical.py`: scarica ERA5 da Open-Meteo Archive + METAR da Iowa State IEM ASOS, applica `build_feature_matrix()`, produce parquet con colonne `target_*` shiftate di `-horizon_hours`
- Verificato look-ahead bias: ERA5 a T = reanalisi, target da `shift(-N)` non entra in X

### Step 3 — trainer LightGBM (`forecast.py`)
- Split temporale 80/20 con `temporal_split()` (sort + cut, no shuffle)
- `train_lgbm()` con early stopping su val-MAE
- Salvataggio importance `feature_importance_{target}.json`
- Logging metriche su `model_metrics` (`db.insert_model_metrics()`)
- Auto-versioning `model_version = datetime.utcnow().strftime("v%Y%m%d_%H%M")`

### Step 4 — RF correttore (`model/correttore.py`)
- Spostato da root a `model/`, aggiunto sys.path hack per importare `forecast`/`db`
- Riusa `load_dataset`, `temporal_split`, `get_feature_cols` da `forecast.py` → split row-identical, zero leakage
- Feature matrix RF = feature ERA5 + colonna `lgbm_pred`
- Training su `residui_train = y_train - lgbm_pred_train`
- Pred corretta: `corrected = lgbm_pred + rf.predict(X_rf)`
- Refactor finale: `RandomForestRegressor(n_estimators=200, random_state=42)` semplice, `pathlib.Path` ovunque

### Step 5 — inference operativa (`model/inference.py`)
- Endpoint Open-Meteo **Forecast** (`api.open-meteo.com/v1/forecast`), non Archive
- `past_days=2` per warm-up lag/rolling, `forecast_days=2`
- Loop su `db.get_active_stations()`, applica `build_feature_matrix()` per stazione
- Carica `model/lgbm_{target}.txt` via `lgb.Booster(model_file=…)`
- Se esiste `model/rf_correttore_{target}.pkl` → applica, `corrected=True`; altrimenti LGBM puro, `corrected=False`
- Cache booster/RF per evitare ricarichi tra stazioni
- `wind_speed`/`wind_direction`/`humidity` pass-through NWP per le righe non ancora modellate
- Flags: `--horizon` (default 1), `--target`, `--dry-run`, `--model-version`
- Lazy import di `db` dentro `run()` → `--help` funziona senza `dotenv`/`supabase`
- Aggiunto `warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")` in cima al file

### Step 6 — dashboard (`output/dashboard.py`)
- Streamlit read-only, 3 sezioni:
  1. Ultima previsione per stazione attiva (tabella, groupby+first su `forecasts`)
  2. Previsto vs Osservato ultime 48h (line chart, selectbox stazione + variabile)
  3. Metriche correnti da `model_metrics` (latest per `(target, horizon_hours)`) + metric box MAE primario
- Tutti i loader con `@st.cache_data(ttl=60)`

### Step 7 — dipendenze e CI
- Creato `requirements.txt`: `pandas`, `numpy`, `pyarrow`, `scikit-learn`, `lightgbm`, `requests`, `python-dotenv`, `supabase`, `streamlit`
- Installati nel conda env `meteo`: `streamlit 1.58.0`, `supabase 2.31.0`, `python-dotenv 1.2.2` (+ transitive)
- Creato `.github/workflows/inference.yml` (cron 30 min, secrets, timeout 15 min)

### Step 8 — training pipeline batch (wind_speed/wind_direction/humidity)
- Creata `logs/`
- Eseguiti in sequenza 3 trainings LGBM + 3 trainings RF correttore con `--no-db`
- Output completo in `logs/training_log.txt`

## Errori riscontrati e fix applicati

| # | Errore | Causa | Fix |
|---|--------|-------|-----|
| 1 | LightGBM rifiuta colonne `object` da parquet | Colonne orografiche (`dist_sea_km`, `bearing_sea`, …) serializzate come `object` | Aggiunto coercion `pd.to_numeric(errors="coerce")` in `load_dataset()` per il branch parquet |
| 2 | Pickle rotto al cambio versione LightGBM | Pickle non è formato cross-version | Switch a formato nativo: `booster.save_model(path)` → `.txt`, ricarico `lgb.Booster(model_file=…)` |
| 3 | `model_version="v1"` hardcoded confondeva versioni | Stesso identificatore per training diversi | Auto-versioning `datetime.utcnow().strftime("v%Y%m%d_%H%M")` |
| 4 | `python3 model/correttore.py`: file not found | Lo script era stato creato in root, non in `model/` | Spostato con `mv`, aggiunto `sys.path.insert(0, _PROJECT_ROOT)` per importare `forecast` e `db` |
| 5 | Correttore non conforme alla spec utente | Implementazione iniziale con `max_depth`, `min_samples_leaf`, `max_features`, `n_jobs` e `os.path` | Refactor: RF inline minimale, `pathlib.Path` ovunque (vedi #12 per il ripristino dei parametri di performance) |
| 12 | RF correttore lentissimo (~10-20 min per target invece di ~18 s), CPU al 100% single-core | Il refactor #5 aveva rimosso `n_jobs=-1` (single-core) e `max_depth=6` (alberi cresciuti completi su 264k righe). Spec minimale non sostenibile in produzione. | Ripristinati `n_jobs=-1, max_depth=6, min_samples_leaf=10` mantenendo `pathlib.Path`. Job già in corso lasciato terminare (Python ha già il modulo in memoria, la modifica del file non lo impatta) |
| 6 | `python3 model/inference.py --help` falliva: `No module named 'dotenv'` | Import di `db` a livello modulo tirava dentro `dotenv`/`supabase` | Lazy import di `db` dentro `run()` |
| 7 | `streamlit`, `supabase`, `dotenv` mancanti nell'env conda `meteo` | Mai installati | `pip install -r requirements.txt`; salvato file `requirements.txt` |
| 8 | `libomp.dylib` not loaded in Python 3.12 di sistema | OpenMP non disponibile fuori da conda | Eseguire SEMPRE nell'env `meteo` (`conda activate meteo`) |
| 9 | Warning sklearn rumorosi a inference (mismatch nome feature tra training pandas e pred numpy/pandas) | sklearn emette `UserWarning` quando i `feature_names_in_` non combaciano | Aggiunto `warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")` in cima a `inference.py` |
| 10 | `DeprecationWarning: datetime.utcnow()` (Python 3.12+) in `forecast.py` e `correttore.py` | API deprecata in Python 3.13 | **Aperto** — non bloccante. Migrazione futura a `datetime.now(timezone.utc)` |
| 11 | `Pandas4Warning` su `select_dtypes(include="object")` in `forecast.py:81` | Pandas 4 cambierà semantica di `object` | **Aperto** — non bloccante. Migrazione futura a `include="object"`/`"str"` esplicito |

## Convenzioni

- **Unità**: `temperature` °C, `wind_speed` km/h, `wind_direction` gradi (0–360), `humidity` %, `pressure` hPa
- **Timezone**: tutto UTC nei timestamp DB e Open-Meteo (parametro `timezone=UTC`)
- **Pathing**: `pathlib.Path` ovunque, mai stringhe hardcoded
- **Versioning modello**:
  - LGBM: `v%Y%m%d_%H%M`
  - RF correttore: `rf_v%Y%m%d_%H%M`
  - Inference: `inference_v%Y%m%d_%H%M`

## Come riprendere il lavoro

I prossimi step per chiudere la pipeline live e iniziare a misurare la qualità
del modello in produzione.

### 1. Attivare l'inference cron su GitHub Actions

```bash
# Dalla root del repo
git init                                  # se non già git repo
git add .
git commit -m "Pipeline meteo locale completa (training + inference + dashboard)"
git branch -M main
git remote add origin <URL_REPO_REMOTO>
git push -u origin main
```

Poi su GitHub:
- Settings → Secrets and variables → Actions → **New repository secret**:
  - `SUPABASE_URL`
  - `SUPABASE_KEY`
- Actions tab → workflow "Inference operativa (T+1h)" → "Enable workflow"
- Trigger manuale con "Run workflow" per smoke test, poi il cron `0,30 * * * *`
  parte automaticamente.

Verifica: dopo ~30 min vedere nuove righe nella tabella `forecasts` (Supabase).

### 2. Chiudere il loop osservato vs previsto — `mainMETEO.py`

Senza osservazioni reali in DB, la Sezione 2 della dashboard mostra solo le
previsioni. Per misurare l'errore in produzione serve un ingestor live:

- Sorgenti: API Netatmo (per le 4 stazioni private) + ARPA Lazio (open data)
- Frequenza: ogni 10–15 min via cron analogo a `inference.yml`
- Schema: scrive su `observations` con `qc_flag` valorizzato da `qc.py`
- Output atteso: ogni `forecast_at` → confronto con `observations` allo stesso `valid_for`

Una volta acceso `mainMETEO.py`, la dashboard Sezione 2 si popola e si può
iniziare a calcolare MAE *in produzione* (diverso dal MAE val storico).

### 3. Backlog modellistico (rimandabile)

- [ ] Migrazione `datetime.utcnow()` → `datetime.now(timezone.utc)` (Python 3.13)
- [ ] Loss circolare per `wind_direction`: predire `wind_u`/`wind_v` separatamente, ricomporre
- [ ] LCZ + impermeabilità (rasters Wudapt/Copernicus) — già predisposto fallback `None` in `features.py`
- [ ] Modelli MOS per `wind_speed`/`humidity` con architettura diversa (es. boosting su feature dedicate)
- [ ] Test multi-orizzonte (T+3h, T+6h, T+12h) → file parquet separati per horizon
- [ ] API FastAPI per esporre le previsioni come JSON
- [ ] Mappa Cartopy con interpolazione spaziale sulle 4 stazioni
