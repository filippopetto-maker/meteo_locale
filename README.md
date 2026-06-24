# 🌦️ Meteo Locale — Sistema di Previsioni Meteo Iper-Locali per Roma

Sistema di previsione meteo su scala comunale che cala lo stato meteorologico regionale sul singolo punto, catturando i microclimi che i modelli globali non vedono. Accuratezza territoriale superiore alle app mainstream, infrastruttura a costo zero.

**Stato:** Phase 1, 2a, 2b completate e in produzione. Phase 2c parzialmente completata (bias correction ARSIAL attiva). Phase 3 — **mappa interattiva live su GitHub Pages** (Leaflet + leaflet-velocity, heatmap temperatura/umidità + particelle vento). GitHub Actions attivo, inference e ingestion automatica ogni 30 minuti. **32 stazioni attive** su tutto il Lazio (6 Roma metro + 26 espansione Lazio) con copertura Netatmo live e correzione bias ARSIAL data-driven. Mappa con **correzione SST reale sul mare** (Open-Meteo Marine API, blend graduale asimmetrico) e **toggle T / T+1h** (Adesso / +1h). **Dashboard Chart.js live** (`dashboard.html`) con forecast vs observed 7 giorni per stazione, MAE per stazione, switch Temperatura/Umidità.

---

## 📑 Indice

1. [Visione del progetto](#-visione-del-progetto)
2. [Perché questo approccio](#-perché-questo-approccio)
3. [Architettura](#-architettura)
4. [Stack tecnologico](#-stack-tecnologico)
5. [Fonti dati](#-fonti-dati)
6. [Feature orografiche](#-feature-orografiche)
7. [Stato attuale](#-stato-attuale)
8. [Risultati del modello](#-risultati-del-modello)
9. [Struttura del progetto](#-struttura-del-progetto)
10. [Database — schema](#-database--schema)
11. [Setup e installazione](#-setup-e-installazione)
12. [I moduli](#-i-moduli)
13. [Roadmap](#-roadmap)
14. [Diario degli errori risolti](#-diario-degli-errori-risolti)
15. [Differenziali competitivi](#-differenziali-competitivi)
16. [Come riprendere il lavoro](#-come-riprendere-il-lavoro)

---

## 🎯 Visione del progetto

L'obiettivo è costruire un sistema di previsione meteo **iper-locale** sul comune di Roma, capace di battere le principali app meteo sulla **capillarità della conoscenza del territorio**.

Le grandi app usano modelli globali interpolati su griglie larghe (10–25 km), che non catturano i microclimi locali: l'isola di calore urbana del centro storico, la brezza marina di Ostia, l'inversione termica notturna nelle zone basse. Questo sistema parte invece da **dati osservati reali** stazione-per-stazione e impara le correzioni locali che i modelli globali sbagliano.

**Cosa fa la scatola, in una frase:** dato lo stato meteorologico regionale (temperatura, umidità, vento) e il profilo orografico di un punto, restituisce una previsione locale corretta per il microclima specifico.

**Prodotto finale atteso:** un sistema autonomo che raccoglie, analizza e prevede, girando su infrastruttura cloud gratuita, spendibile come progetto di portfolio nel mercato del lavoro data/ML.

---

## 🧭 Perché questo approccio

### Cosa NON facciamo: WRF / NWP completo

Inizialmente valutato un modello numerico di previsione (WRF), poi abbandonato. Un modello fisico integra nel tempo le equazioni della fluidodinamica e pretende in input lo **stato 3D completo dell'atmosfera** su tutta una griglia: non lo si "alimenta" con quattro parametri scalari, e per girare seriamente richiede infrastruttura HPC. Impraticabile su un Mac senza server, e comunque l'attrezzo sbagliato per questo scopo.

### Cosa facciamo: statistical downscaling + ML

Accoppiamo **due fonti diverse** nella tabella di addestramento:

- **Input** = stato regionale grezzo dalla rianalisi storica (ERA5 via Open-Meteo).
- **Target** = cosa è *realmente* successo in un punto preciso, misurato da una stazione vera (METAR aeroportuale).

Il modello impara la **correzione locale**: la differenza tra il grezzo regionale e l'osservazione reale *è* il microclima.

**La trappola della risoluzione (da non dimenticare mai).** Allenare l'ML *solo* sulla rianalisi è inutile: a 25 km, Ostia, Monte Mario, il centro e un parco sono la stessa cella sfocata. Un modello addestrato lì impara a riprodurre ERA5, non a batterlo. Il segnale iper-locale **non è dentro la rianalisi gratuita** — entra solo attraverso i target di stazioni reali. Per questo input e target vengono da fonti diverse.

### Principio chiave: multi-stazione è necessario, non opzionale

Con una sola stazione le feature orografiche (quota, distanza dal mare, esposizione) sono **costanti** → non insegnano nulla, vengono assorbite come offset fisso. Si ottiene solo una correzione di bias *site-specific*: utile, ma non orografia generalizzabile, e cieca su qualsiasi punto nuovo.

Le feature orografiche diventano predittori appresi e generalizzabili **solo addestrando simultaneamente su più stazioni con profili di terreno contrastanti** (costiero, pianura, urbano denso, quota).

**Stazioni attive (32, profili contrastanti):**

| ID | Nome | Fonte | Profilo | Alt | Dist. mare |
|:---|:-----|:------|:--------|:----|:-----------|
| 3 | Roma Sud (Casal Palocco) | METAR + Netatmo | standard | 15 m | 7.0 km |
| 25 | Ostia Lido | Netatmo | costiera | 14 m | 0.4 km |
| 26 | EUR | Netatmo | urban_canyon | 27 m | 19.1 km |
| 27 | Trastevere | Netatmo | urban_canyon | 27 m | 22.5 km |
| 28 | Tivoli | Netatmo | colline_interne | 226 m | 47.7 km |
| 29 | Castelli Romani | Netatmo | quota | 342 m | 29.3 km |
| 33 | Pratica di Mare | METAR + Netatmo | standard | 16 m | 4.9 km |
| 34 | Cerveteri Ladispoli | Netatmo | costiera | 10 m | 0.4 km |
| 35 | Saxa Rubra | Netatmo | standard | 48 m | 30.3 km |
| 36 | Selva Nera | Netatmo | standard | 78 m | 16.6 km |
| 37 | Cisterna Latina | Netatmo | standard | 81 m | 22.6 km |
| 38 | Bracciano | Netatmo | colline_interne | 296 m | 19.1 km |
| 39 | Viterbo | Netatmo | colline_interne | 339 m | 42.1 km |
| 40 | Santa Marinella | Netatmo | costiera | 23 m | 3.9 km |
| 41 | Latina | Netatmo | pianura | 29 m | 21.2 km |
| 42 | Ardea | Netatmo | costiera | 50 m | 8.3 km |
| 43 | Sabaudia | Netatmo | costiera | 24 m | 44.7 km |
| 44 | Ceccano | Netatmo | fondovalle | 205 m | 62.8 km |
| 46 | Labaro | Netatmo | fondovalle | 22 m | 30.4 km |
| 47 | Anagni / Ciociaria alta | Netatmo | colline_interne | 259 m | 42.9 km |
| 48 | Cassino / Liri Sud | Netatmo | fondovalle | 44 m | 100.3 km |
| 49 | Fondi | Netatmo | pianura | 4 m | 67.0 km |
| 50 | Rieti | Netatmo | colline_interne | 393 m | 82.8 km |
| 51 | Fiano Romano | Netatmo | fondovalle | 92 m | 47.7 km |
| 52 | Civitavecchia | Netatmo | brezza_marina | 25 m | 0.8 km |
| 53 | Filettino | Netatmo | alta_quota | 1044 m | 67.2 km |
| 54 | Gaeta | Netatmo | brezza_marina | 12 m | 0.9 km |
| 56 | Rocca Sinibalda | Netatmo | alta_quota, Appennino reatino | 980 m | 80 km |
| 57 | Sigillo | Netatmo | quota, Appennino nord | 648 m | 102 km |
| 58 | Tarquinia | Netatmo | costiera, costa nord Viterbo | 138 m | 0.3 km |
| 59 | Tor Bella Monaca | Netatmo | urban_canyon, periferia est Roma | 70 m | 31 km |
| 60 | Tor Vergata Est | Netatmo | urban_canyon, periferia est Roma | 59 m | 29 km |

*In sospeso: Castelli Romani alta quota (~530m, `quota`, MAC `70:ee:50:2c:be:10`) — offline al 23/06/2026, da aggiungere come id 61 quando torna attiva.*

*Stazioni inattive (storico conservato): id 1 Roma Nord, id 2 Roma Centro (duplicati METAR LIRA), id 4 Ostia (sostituita da Ostia Lido).*

*Nota Filettino (id 53): prima stazione quota elevata dell'Appennino laziale (1044 m). `NETATMO_MIN_CLUSTER` abbassato a 1 per stazioni `quota` in `fetch_netatmo_block.py` perché la zona è scarsamente abitata e non ci sono altre stazioni Netatmo entro 5 km.*

**Gradiente microclima osservato (sera estiva tipica):**
Trastevere 24.8°C → EUR 24.7°C → Roma Sud 24.1°C → Ostia Lido 23.8°C → Tivoli 23.4°C → Castelli Romani 22.3°C — isola di calore, brezza marina e lapse rate altitudinale tutti visibili contemporaneamente.

### Ordine di difficoltà dei target di previsione

```
temperatura  <  direzione vento  ≈  rischio temporali  <  pioggia puntuale (mm)
  (facile)                                                      (più difficile)
```

Sviluppiamo in quest'ordine per costruire risultati e momentum. La pioggia quantitativa in un punto è il problema più duro della meteorologia: da input scalari, aspettarsi al massimo una probabilità grezza, non i millimetri.

### Nota metodologica: evitare il look-ahead bias

Se la scatola deve *prevedere* (non solo diagnosticare il presente), l'input dev'essere lo stato all'ora **T** e il target l'osservazione a **T+N**. Mai mescolare i tempi: altrimenti il modello "bara" guardando il futuro in fase di training e poi crolla nel mondo reale.

Lo split train/val è rigorosamente **temporale** (non random): tutte le osservazioni passate alla stessa data soglia per tutte le stazioni, che riflette lo scenario reale di addestramento su storico e test sul futuro.

---

## 🏗️ Architettura

```
┌───────────────────────────────────────────────────┐
│              LAYER 1 — INGESTION                    │
│  ── Storico (per l'addestramento) ──                │
│  Open-Meteo / ERA5  → input regionale (reanalisi)   │
│  METAR · ARPA       → target storici stazioni       │
│  ── Live (per l'operatività) ──                     │
│  Netatmo API        → 340+ stazioni pubbliche Roma ✅│
│  ARPA Lazio         → dati ufficiali validati[Fase 2]│
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│              LAYER 2 — STORAGE                      │
│  Supabase PostgreSQL (hosted, gratuito)             │
│  stations · observations · forecasts                │
│  qc_log · model_metrics                             │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│              LAYER 3 — PROCESSING                   │
│  QC (range·climatologico·persistenza·spaziale)      │
│  Feature engineering (5 strati, 76 colonne)         │
│  LightGBM (previsione) + RF (correttore residui)    │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│              LAYER 4 — OUTPUT                       │
│  Dashboard (Streamlit) · API REST (FastAPI) [Fase 3]│
│  Mappa interattiva Windy-style (MapLibre GL) [Fase 3]│
│  Campo colorato IDW · Particelle vento WebGL        │
└─────────────────────────────────────────────────────┘

Esecuzione automatica: GitHub Actions (cron ogni 30 min, ubuntu-latest, €0)
```

---

## 🛠️ Stack tecnologico

| Layer | Strumento | Costo |
|:------|:----------|:------|
| Dati storici | Open-Meteo Historical API (ERA5) | €0 |
| Raccolta dati live | Python + GitHub Actions (cron) | €0 |
| Stazioni live dense | Netatmo Public API (OAuth2) | €0 |
| Storage | Supabase PostgreSQL (free tier) | €0 |
| Accesso DB | supabase-py (API REST su HTTPS) | €0 |
| Quality Control | Python (logica custom) | €0 |
| Modello ML | LightGBM + scikit-learn (RandomForest) | €0 |
| Automazione | GitHub Actions (ubuntu-latest, cron 30 min) | €0 |
| Visualizzazione | Streamlit | €0 |
| Versionamento | GitHub | €0 |
| **TOTALE** | | **€0** |

---

## 📡 Fonti dati

### Input — stato regionale storico

- **Open-Meteo Historical Weather API** — basata su ERA5, dati orari dal **1940**, copertura globale senza buchi, gratuita e senza API key, licenza CC BY 4.0. ERA5 a 0,25° (~25 km). Espone le variabili che ci servono come input.

### Target — osservazioni reali (Phase 1)

- **METAR aeroportuali via Iowa State IEM ASOS** (LIRF Fiumicino, LIRA Ciampino) — storico pluridecennale, copertura 2015–2024, frequenza variabile (20–60 min, ricampionato a 1h). Gratuito, no API key, copertura globale.

### Target — osservazioni live (Phase 2a + 2b, attive)

- **METAR live** — IEM ASOS, ultime 2h, stazione Roma Sud (LIRF). Inserite in `observations` ogni 30 min.
- **Netatmo Public API** — rete di stazioni personali pubbliche. 340+ stazioni nel bbox Roma, aggregazione mediana per cluster 5 km, QC a 4 livelli. 6/6 stazioni coperte ogni 30 min. OAuth2 con refresh_token.

### Target — da integrare (Phase 2c)

- **Protezione Civile Lazio / OpenAmbiente** — 238 centraline ufficiali ogni 15 min → `fetch_protezione_civile_lazio()` stub pronto in `mainMETEO.py`.

**Nota CAPE.** CAPE non è attualmente incluso nelle variabili ERA5 scaricate. Servirà nella Fase 3 per il target thunderstorm — aggiungerlo ora richiederebbe rifare historical.py + retraining senza beneficio per i target attuali.

---

## 🏔️ Feature orografiche

Sono il vantaggio competitivo principale: traducono i meccanismi fisici del territorio in colonne della tabella di training.

- **Quota** — come delta rispetto alla cella ERA5. La feature più potente. L'aria si raffredda di ~6,5°C per km; ERA5 spalma la quota su 25 km e sbaglia sistematicamente.
- **Posizione nel terreno** (fondovalle / versante / cresta) — governa la temperatura notturna: l'aria fredda scivola in basso e si accumula nei fondovalle.
- **Esposizione** (pendenza + orientamento del versante) — quanto sole prende il punto; effetto diurno e stagionale.
- **Densità urbana** — isola di calore: asfalto e cemento rilasciano calore di notte (+2/+5°C vs campagna).
- **Distanza dal mare** — brezza marina: di giorno richiama aria fresca e umida dalla costa verso l'interno.
- **Onshore alignment** — quanto il vento attuale è "dal mare": combina il bearing statico verso la costa con la direzione dinamica del vento. +1 = brezza marina piena, -1 = vento da terra.

### Etichette microclima (schema Supabase)

`urban_canyon` · `esposta_sole` · `quota` · `alta_quota` · `costiera` · `colline_interne` · `verde_parco` · `standard`

---

## 📊 Stato attuale

### ✅ Blocco 1 — Storage (COMPLETATO)

- [x] Schema DB progettato e creato su Supabase
- [x] 5 tabelle + 2 viste operative
- [x] `trained_at` aggiunto a `model_metrics` via `ALTER TABLE`
- [x] Modulo `db.py` di connessione (via API REST)
- [x] `.env` configurato con credenziali
- [x] Connessione testata: stazioni caricate

### ✅ Blocco 2 — Modello ML (COMPLETATO)

- [x] `historical.py` — dataset 2015–2024, ~331k righe × 76 colonne, 4 stazioni, formato parquet
- [x] `features.py` — Feature Engineering 5 strati completa
- [x] `forecast.py` — LightGBM, target temperatura val MAE **0.869°C**, convergenza a **643 round**
- [x] `forecast.py` — modelli addestrati anche per wind_speed, wind_direction, humidity
- [x] `model/correttore.py` — RF correttore residui (2° stadio)
- [x] `model/inference.py` — inference operativa, testata con `--dry-run` e run live
- [x] `output/dashboard.py` — Streamlit dashboard read-only live
- [x] `qc.py` — Quality Control a 4 livelli scritto e testato

### ✅ Blocco 3 — Deploy automatico (COMPLETATO)

- [x] Repo GitHub creato: `filippopetto-maker/meteo_locale`
- [x] Secrets Supabase configurati in GitHub (`SUPABASE_URL`, `SUPABASE_KEY`)
- [x] `.github/workflows/inference.yml` — cron ogni 30 min, ubuntu-latest, conda + mamba
- [x] **Prima run manuale completata con successo: 1m 20s** ✅

### ✅ Blocco 4 — Pipeline live Phase 2a (COMPLETATA — giugno 2026)

- [x] `mainMETEO.py` — raccolta METAR live via IEM ASOS (LIRF/LIRA), QC integrato, inserimento in `observations`
- [x] `.github/workflows/ingestion.yml` — cron 30 min, ubuntu-latest, attivo e testato (1m 16s)
- [x] Vista Supabase `forecast_vs_observed` aggiornata con LATERAL JOIN (tolleranza 60 min per disallineamento METAR)
- [x] Dashboard: sezione "Previsto vs Osservato" con grafico Altair + tabella MAE per stazione
- [x] Dashboard: timezone Europe/Rome + direzione vento cardinale
- [x] Upsert su `forecasts` (chiave station_id, valid_for) — no duplicati
- [x] Vincoli UNIQUE su `stations` (lat, lon) e `forecasts` (station_id, valid_for)

### ✅ Blocco 5 — Pipeline live Phase 2b — Netatmo (COMPLETATA — giugno 2026)

- [x] Registrazione dev.netatmo.com → `client_id`, `client_secret`, `refresh_token`
- [x] `fetch_netatmo()` operativa in `mainMETEO.py`: token OAuth2, `getpublicdata` bbox Roma, parsing temperatura/umidità/vento, aggregazione mediana cluster 5 km, QC integrato, insert `observations`
- [x] 340+ stazioni Netatmo pubbliche nel bbox Roma — 6/6 stazioni progetto coperte ogni 30 min
- [x] `db.py`: `raw_source` ora incluso nell'insert `observations`
- [x] `db.py`: upsert `observations` con `ignore_duplicates=True` — fix errore 409 su METAR timestamp fisso
- [x] Schema `stations` arricchito: `microclima`, `dist_sea_km`, `dist_center_km`, `bearing_sea`
- [x] Rete espansa 4 → **6 stazioni attive** Roma metro: Ostia Lido, EUR, Trastevere, Tivoli, Castelli Romani
- [x] `qc.py` `STATION_TYPES` aggiornato con nuovi ID (25–29)
- [x] Secrets `NETATMO_CLIENT_ID`, `NETATMO_CLIENT_SECRET`, `NETATMO_REFRESH_TOKEN` configurati in GitHub Actions

### ✅ Blocco 5b — Espansione rete Lazio (COMPLETATA — giugno 2026)

- [x] Rete espansa da 6 → **32 stazioni attive** su tutto il Lazio (commit `82467c6`)
- [x] LAZIO_BBOXES: 5 sub-bbox sovrapposte con deduplicazione MAC address (sostituisce ROMA_BBOX)
- [x] `min_cluster=1` per stazioni id≥39 o microclima in `quota`/`alta_quota`/`colline_interne`
- [x] Stazioni 56–60 aggiunte il 23/06/2026:
  - 56 Rocca Sinibalda (alta_quota, 980m, Appennino reatino)
  - 57 Sigillo (quota, 648m, Appennino nord)
  - 58 Tarquinia (costiera, 138m, costa nord Viterbo)
  - 59 Tor Bella Monaca (urban_canyon, 70m, periferia est Roma)
  - 60 Tor Vergata Est (urban_canyon, 59m, coordinate approssimative)
- [ ] Castelli Romani alta quota (~530m, MAC `70:ee:50:2c:be:10`) — offline, da aggiungere come id 61

### ✅ Blocco 7 — Dashboard GitHub Pages (COMPLETATO — giugno 2026)

- [x] `docs/dashboard.html` — pagina statica Chart.js, link "📊 Dashboard →" in `#info-panel` di `index.html`
- [x] Switch Temperatura/Umidità — aggiorna entrambi i chart contemporaneamente
- [x] Chart 1: Previsto vs Osservato (7 giorni) per stazione — Chart.js line, asse X `time` con `chartjs-adapter-date-fns`; filtra punti null (umidità spesso assente nelle osservazioni)
- [x] Chart 2: MAE per stazione — barre orizzontali; verde < 1.0°C (< 5.0% umidità), rosso altrimenti; null → barra trasparente "(n/d)"
- [x] `docs/data/dashboard_data.json` — serie storiche 7 giorni (temp + hum) + `mae_per_station` con `mae`, `mae_hum`, `n_pairs`, `n_pairs_hum`; `mae_global`, `mae_global_hum`
- [x] `fetch_dashboard_series()` — query Supabase forecast (dedup `valid_for` per `forecast_at` più recente) + observed QC-ok; ogni punto ha `{"t", "temp", "hum"}`
- [x] `build_dashboard_json()` — coppie forecast/observed entro ±30 min per MAE temperatura e umidità separati
- [x] `scripts/export_static.py --dashboard-only` — genera solo `dashboard_data.json` senza griglie ERA5/IDW
- [x] Job `export-dashboard` in `export.yml` — triggerato 2×/giorno (08:00 e 20:00 UTC) da cron-job.org via `workflow_dispatch`
- [x] `git pull --rebase origin main` prima del push in entrambi i job — fix conflict da run parallele sullo stesso branch

### 🔄 Blocco 6 — Pipeline live Phase 2c (PROSSIMA)

- [ ] Protezione Civile Lazio / OpenAmbiente — 238 centraline ogni 15 min → `fetch_protezione_civile_lazio()` stub già in `mainMETEO.py`

### ⏳ Fase 3 — Output avanzato e nuovi target

1. [ ] **API REST FastAPI** — endpoint `/forecast/latest` e `/forecast/history`
       che espone le previsioni Supabase in JSON pulito per la mappa e per usi esterni
2. [ ] **Mappa interattiva Windy-style** — visualizzazione parametri meteo su Roma
       Stack: MapLibre GL JS + Canvas WebGL + GitHub Pages (zero costi)
       - Campo colorato continuo: interpolazione IDW tra le 6 stazioni su griglia 200×200
         per temperatura, umidità, pioggia (bbox Roma: 41.6–42.1°N / 12.1–12.9°E)
       - Particelle vento animate: leaflet-velocity con componenti U/V dalla previsione
       - Marker stazioni: pallini con tooltip su T, umidità, vento in tempo reale
       - Switcher parametri: Temperatura · Umidità · Vento · Pioggia
       - Tile base: CartoDB Dark (stile simile a Windy)
       Ordine costruzione: FastAPI → prototipo HTML standalone → particelle → GitHub Pages
3. [ ] CAPE da ERA5 → target thunderstorm
4. [ ] Target pioggia puntuale (mm)
5. [ ] LCZ Copernicus per isola di calore
6. [ ] **Dicembre 2026**: retraining completo con Netatmo accumulato + ARSIAL daily
   - ARSIAL daily come **validazione** e cross-check sui bias stagionali
   - Stazioni 56–60 (e 61 se attiva): escono dal cold start, modello impara i gradienti orografici reali
   - Feature aggiuntive già in pipeline: `precipitation`, `cloudcover`, `shortwave_radiation` (ERA5 + NWP)
   - Lag e rolling su `precipitation` e `shortwave_radiation` da aggiungere in `features.py` prima del retraining

---

## 🎯 Risultati del modello

### Dataset di training

| Parametro | Valore |
|:----------|:-------|
| Periodo | 2015–2024 (10 anni) |
| Righe totali | ~331.000 |
| Righe di training | ~264.000 (80%) |
| Righe di validazione | ~67.000 (20%) |
| Colonne feature | 76 |
| Stazioni (training) | 4 (Roma Nord, Centro, Sud, Ostia — schema originale) |
| Stazioni (operative) | 6 (schema espanso Phase 2b) |
| ICAO sorgenti | LIRA (Ciampino), LIRF (Fiumicino) |

*Nota: il modello è stato addestrato sulle 4 stazioni originali. Per le 5 nuove stazioni (id 25–29) opera per estrapolazione sui gradienti orografici appresi. Il retraining con i dati Netatmo accumulati è pianificato per Phase 3.*

### Performance LightGBM (T+1h)

| Target | Val MAE | Note |
|:-------|:--------|:-----|
| temperatura (°C) | **0.869** | Convergenza a 643 round |
| wind_speed (km/h) | — | Addestrato |
| wind_direction (°) | — | Addestrato |
| humidity (%) | — | Addestrato |

### Correttore RF (secondo stadio)

Il RandomForest impara gli **errori sistematici residui** di LightGBM per microzona.

| Target | RF applicato | Motivazione |
|:-------|:-------------|:------------|
| temperatura | ✅ Sì | Residui strutturati per microclima |
| wind_direction | ✅ Sì | Residui strutturati per esposizione |
| wind_speed | ❌ No | Residui non strutturati — rumore puro |
| humidity | ❌ No | Residui non strutturati — rumore puro |

**Iperparametri RF critici:** `max_depth=6, min_samples_leaf=10, n_jobs=-1`.
Senza questi vincoli su ~264k righe il file .pkl esplode a ~4.8 GB e il training dura 10+ minuti invece di ~18 secondi.

### Modelli salvati nel repo

| File | Dimensione |
|:-----|:-----------|
| `model/lgbm_temperature.txt` | 3.6 MB |
| `model/lgbm_wind_speed.txt` | 2.4 MB |
| `model/lgbm_humidity.txt` | 2.0 MB |
| `model/lgbm_wind_direction.txt` | 1.0 MB |
| `model/rf_correttore_temperature.pkl` | 855 KB |
| `model/rf_correttore_wind_direction.pkl` | 1.8 MB |
| **Totale** | **~12 MB** |

### Qualità previsioni per stazione (stato attuale)

Il modello è addestrato sulle 4 stazioni originali. Per le nuove zone la
qualità dipende da quanto il profilo orografico è rappresentato nel training:

| Stazione | Microclima | Qualità previsione attuale | Note |
|:---------|:-----------|:--------------------------|:-----|
| Roma Sud (3) | standard | ✅ Alta | era nel training set |
| Ostia Lido (25) | costiera | 🟡 Buona | microclima `costiera` presente nel training (old Ostia) |
| EUR (26) | urban_canyon | 🟡 Buona | microclima `urban_canyon` presente nel training |
| Trastevere (27) | urban_canyon | 🟡 Discreta | urban_canyon presente, ma zona più centrale |
| Tivoli (28) | quota | 🟠 Approssimata | `quota` **mai vista** nel training — extrapolazione da altitude |
| Castelli Romani (29) | quota | 🟠 Approssimata | quota più alta, massima incertezza sistematica |
| Rocca Sinibalda (56) | alta_quota | 🔵 Cold start | Extrapolazione fino a dic 2026 |
| Sigillo (57) | quota | 🔵 Cold start | Extrapolazione fino a dic 2026 |
| Tarquinia (58) | costiera | 🔵 Cold start | Extrapolazione fino a dic 2026 |
| Tor Bella Monaca (59) | urban_canyon | 🔵 Cold start | Extrapolazione fino a dic 2026 |
| Tor Vergata Est (60) | urban_canyon | 🔵 Cold start | Extrapolazione fino a dic 2026 |

### Nota architetturale — correzione orografica in quota

La griglia IDW in quota (es. area Simbruini/Ernici) appare meno accurata perché le stazioni `alta_quota` (Filettino, Rocca Sinibalda, Sigillo) sono in cold-start. Non applicare correzioni empiriche di lapse rate sulla griglia — violerebbe il principio ERA5-as-background. La copertura migliorerà con il retraining dicembre 2026 quando LightGBM imparerà la relazione quota→temperatura dai dati accumulati.

### Il ciclo virtuoso

Ogni run di `mainMETEO.py` accumula osservazioni Netatmo reali in `observations`
per tutte e 6 le zone. Queste diventano i **target futuri del modello**:

```
Oggi:        ERA5 (input) + METAR 4 stazioni (target storico)
             → previsioni buone per costiera/urban_canyon, approssimate per quota

Ogni 30 min: Netatmo accumula ground truth per 6 zone
             ↓
~6 mesi:     ERA5 (input) + Netatmo 6 stazioni (target live)
             → retraining → il modello impara le correzioni reali per quota,
               Trastevere specifica, Castelli Romani specifica
```

---

## 📁 Struttura del progetto

```
meteo_locale/
│
├── .env                         # credenziali Supabase + Netatmo (NON nel repo — .gitignore)
├── .gitignore
├── README.md
├── requirements.txt
│
├── .github/
│   └── workflows/
│       ├── inference.yml        # GitHub Actions — cron 30 min ✅ ATTIVO
│       ├── ingestion.yml        # GitHub Actions — cron 30 min ✅ ATTIVO
│       └── export.yml           # Job export (30 min) + job export-dashboard (08:00/20:00 UTC) ✅ ATTIVO
│
├── db.py                        # Data Access Layer (connessione Supabase) ✅
├── qc.py                        # Quality Control 4 livelli ✅
├── features.py                  # Feature Engineering 5 strati ✅
├── historical.py                # ERA5 + METAR → parquet training ✅
├── forecast.py                  # Training LightGBM ✅
├── mainMETEO.py                 # Raccolta osservazioni live (METAR + Netatmo) ✅
│
├── model/
│   ├── correttore.py            # RF correttore residui ✅
│   ├── inference.py             # Inference operativa ✅
│   ├── lgbm_temperature.txt     # Modello LightGBM temperatura ✅
│   ├── lgbm_wind_speed.txt      # Modello LightGBM vento ✅
│   ├── lgbm_wind_direction.txt  # Modello LightGBM direzione ✅
│   ├── lgbm_humidity.txt        # Modello LightGBM umidità ✅
│   ├── rf_correttore_temperature.pkl     # RF correttore temperatura ✅
│   ├── rf_correttore_wind_direction.pkl  # RF correttore direzione ✅
│   └── feature_importance_*.json        # Gain per feature (tutti i target)
│
├── data/
│   └── training.parquet         # Dataset storico (NON nel repo — .gitignore)
│
├── output/
│   └── dashboard.py             # Streamlit dashboard read-only ✅
│
└── docs/                        # GitHub Pages (sito statico)
    ├── index.html               # Mappa Leaflet full-screen ✅
    ├── dashboard.html           # Dashboard Chart.js (forecast vs observed, MAE) ✅
    ├── js/
    │   └── app.js               # Logica mappa, popup, legenda ✅
    └── data/
        ├── latest.json          # Stazioni + griglie T/H (aggiornato ogni 30 min)
        ├── wind_grid.json       # Griglia vento U/V per leaflet-velocity
        └── dashboard_data.json  # Serie storiche 7 gg + MAE (aggiornato 2×/giorno)
```

**Nota path:** `correttore.py` e `inference.py` vivono in `model/` con un `sys.path` hack per trovare `forecast.py` e `db.py` nella root. Eseguire sempre dalla root del progetto: `cd ~/Desktop/meteo_locale`.

---

## 🗄️ Database — schema

### `stations` — anagrafica stazioni

| Campo | Tipo | Note |
|:------|:-----|:-----|
| id | SERIAL PK | |
| name | TEXT | |
| lat, lon | DOUBLE | coordinate |
| altitude | DOUBLE | metri s.l.m. (da Open-Meteo Elevation API) |
| source | TEXT | netatmo / arpa / open_meteo |
| microclima | TEXT | urban_canyon / esposta_sole / costiera / verde_parco / quota / standard |
| is_active | BOOLEAN | |
| dist_sea_km | DOUBLE | distanza dal punto costa più vicino (litorale laziale) |
| dist_center_km | DOUBLE | distanza da Piazza Venezia (proxy isola di calore) |
| bearing_sea | DOUBLE | bearing 0–360° verso la costa — usato per onshore_alignment |

*Le colonne orografiche (`dist_sea_km`, `dist_center_km`, `bearing_sea`) si calcolano con `compute_static_orography(lat, lon, microclima)` in `features.py` e si salvano una volta sola al momento dell'inserimento della stazione.*

### `observations` — dati grezzi (serie temporale)

| Campo | Tipo | Note |
|:------|:-----|:-----|
| id | BIGSERIAL PK | |
| station_id | FK → stations | |
| recorded_at | TIMESTAMPTZ | UNIQUE con station_id — upsert idempotente |
| temperature, wind_speed, wind_direction | DOUBLE | |
| humidity, pressure, precipitation | DOUBLE | opzionali / per target pioggia |
| qc_flag | SMALLINT | 0=ok, 1=sospetto, 2=scartato |
| raw_source | JSONB | sorgente e metadati (es. `{"source":"netatmo_public","n_stations":13}`) |

### `qc_log` — log delle anomalie QC

Traccia ogni flag con: check_type, field_name, original_value, reason.

### `forecasts` — previsioni generate

Include `model_version` per confrontare versioni diverse e `corrected` (bool).

### `model_metrics` — performance nel tempo

Storico MAE/RMSE per ogni target, n_samples, periodo, `trained_at`, `model_version`.

### Viste

- `latest_observations` — ultima rilevazione valida per stazione
- `forecast_vs_observed` — confronto automatico previsione vs reale con MAE (LATERAL JOIN, tolleranza 3600s)

---

## ⚙️ Setup e installazione

### 1. Clona il repo e prepara l'ambiente

```bash
git clone https://github.com/filippopetto-maker/meteo_locale.git
cd meteo_locale
conda activate meteo
pip install -r requirements.txt
```

### 2. Configura le credenziali locali

Crea il file `.env` (non è nel repo):

```
SUPABASE_URL=https://xxxxxxxx.supabase.co
SUPABASE_KEY=sb_secret_xxxxxxxxxxxxx
NETATMO_CLIENT_ID=xxxxxxxxxxxxxxxxxxxx
NETATMO_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxx
NETATMO_REFRESH_TOKEN=xxxxxxxxxxxxxxxxxxxx
```

Le chiavi Supabase: Settings → API Keys (usa la **secret key** per gli script backend).
Le chiavi Netatmo: `dev.netatmo.com/apps/` → app `meteo_locale` → Token generator (scope: `read_station`).

### 3. Testa la connessione

```bash
python3 db.py
```

Output atteso: `✅ Connessione OK` + lista delle stazioni.

### Note ambiente

- **Python environment:** Miniforge conda, environment `meteo`, Python 3.12 via conda-forge
- **Attivare sempre** `conda activate meteo` prima di qualsiasi script Python
- **Eseguire sempre dalla root:** `cd ~/Desktop/meteo_locale` — i path relativi `data/` e `model/` dipendono dal cwd
- Connessione via **API REST (HTTPS porta 443)**, non PostgreSQL diretto (porta 5432 spesso bloccata)
- `caffeinate -i python3 ...` per evitare che il Mac vada in sleep durante training lunghi

### GitHub Actions — secrets richiesti

Configurati in: repo → Settings → Secrets and variables → Actions

| Secret | Descrizione |
|:-------|:------------|
| `SUPABASE_URL` | URL del progetto Supabase |
| `SUPABASE_KEY` | Secret key Supabase (service role) |
| `NETATMO_CLIENT_ID` | App ID da dev.netatmo.com |
| `NETATMO_CLIENT_SECRET` | App secret da dev.netatmo.com |
| `NETATMO_REFRESH_TOKEN` | Token generato con scope `read_station` |

---

## 🧩 I moduli

### `db.py` — Data Access Layer ✅

Modulo unico di connessione, importato da tutti gli script. Espone:

- `get_active_stations()` — lista stazioni attive
- `insert_observation(...)` — salva una misurazione (upsert con `ignore_duplicates` su `station_id, recorded_at`)
- `get_observations(station_id, hours)` — storico di una stazione
- `get_latest_observations()` — ultima per stazione
- `insert_forecast(...)` — salva una previsione
- `insert_model_metrics(...)` — salva le performance del modello
- `health_check()` — verifica connessione

**Principio:** se Supabase cambia, si modifica solo `db.py` — gli altri script restano intatti.

### `historical.py` — Costruzione tabella storica ✅

Costruisce il dataset di training per tutte le stazioni:

- Scarica ERA5 orario da Open-Meteo Archive API (gratuita, no API key)
- Scarica METAR storici da Iowa State IEM ASOS (gratuita, no API key, copertura globale)
- Ricampiona METAR a 1h, allinea con ERA5 su timestamp
- Applica feature engineering (5 strati via `features.py`)
- Shift target di `horizon_hours` → garanzia anti look-ahead bias
- Output: parquet multi-stazione (~331k righe × 76 colonne, 2015–2024)

### `qc.py` — Quality Control 4 livelli ✅

Si applica soprattutto ai **dati live** (Netatmo grezzo è rumoroso).

| Livello | Cosa controlla | Azione |
|:--------|:---------------|:-------|
| 1. Range check | Valori fisicamente impossibili | Scarta (flag 2) |
| 2. Climatological | Plausibilità per mese + fascia oraria | Scarta o sospetto |
| 3. Persistence | Sensore bloccato (valore fermo) | Sospetto (flag 1) |
| 4. Spatial | Outlier vs stazioni vicine (z-score) | Scarta o sospetto |

**Climatological check** — usa climatologia Roma aggiornata al trend 2015–2024, con offset per tipo di stazione:

```
esposta_sole: +5°C   urban_canyon: +3°C   standard:  0°C
costiera:     -1°C   verde_parco:  -2°C   quota:    -3°C
```

`STATION_TYPES` mappa station_id → microclima per i threshold del check (id attivi: 3, 25–29).

### `features.py` — Feature Engineering 5 strati ✅

| Strato | Feature | Note |
|:-------|:--------|:-----|
| 1. Temporali | hour_sin/cos, doy_sin/cos, month, is_weekend, is_daytime | Codifica ciclica — 23:00 e 00:00 risultano "vicine" |
| 2. Lag | temperature_lag_1/2/3/6, ecc. | Solo passato → no look-ahead |
| 3. Rolling | roll_mean/std su finestre 3/6/12 | Con shift(1) — no look-ahead |
| 4. Derivate | wind_u/v, temp_trend, pressure_trend, wind_chill | Componenti cartesiane del vento risolvono la discontinuità 360°/0° |
| 5. Orografiche | altitude, dist_sea_km, dist_center_km, bearing_sea, onshore_alignment, microclima_* (one-hot) | Statiche per stazione, attivano l'apprendimento orografico |

`compute_static_orography(lat, lon, microclima)` calcola e restituisce tutti i campi orografici statici da salvare nel DB quando si aggiunge una nuova stazione.

### `forecast.py` — LightGBM ✅

Gradient boosting su feature tabulari. Funzionalità:

- Split temporale rigoroso (no random) con `temporal_split()`
- Feature selection automatica via `get_feature_cols()` (esclude metadati e target_*)
- Early stopping su val-MAE
- Salvataggio modello in formato nativo `.txt` (robusto al cambio versione)
- Feature importance (gain) in JSON
- Insert metriche su Supabase (`model_metrics`)

### `model/correttore.py` — RF Correttore residui ✅

Secondo stadio: impara gli errori sistematici di LightGBM per microzona.

- Importa `temporal_split` e `get_feature_cols` direttamente da `forecast.py` → split identico garantito
- RF applicato solo dove i residui sono strutturati (temperatura, wind_direction)
- RF scartato dove i residui sono rumore puro (wind_speed, humidity)
- **Iperparametri obbligatori:** `max_depth=6, min_samples_leaf=10` — senza questi il file esplode

### `model/inference.py` — Inference operativa ✅

- Scarica l'analisi ERA5 corrente da Open-Meteo
- Applica feature engineering (stessi 5 strati del training)
- Carica LightGBM + RF correttori da file
- Scrive previsioni T+1h su Supabase (`forecasts`) per tutte le stazioni attive (6)
- Supporta `--dry-run` per test senza scrittura DB
- Eseguito automaticamente ogni 30 min da GitHub Actions

### `mainMETEO.py` — Raccolta osservazioni live ✅

Popola la tabella `observations` con dati reali da stazioni fisiche. Ogni run (30 min):

1. **METAR** — IEM ASOS, ultime 2h per LIRA/LIRF, stazione Roma Sud (id=3). Upsert idempotente.
2. **Netatmo** — `fetch_netatmo()`: token OAuth2 refresh → `getpublicdata` bbox Roma → parsing → mediana cluster 5 km → QC → insert per 6 stazioni.

QC a 4 livelli via `qc.run_qc()` — storico ultime 3h da Supabase, neighbors = cluster Netatmo della stazione.
Supporta `--dry-run`. Stub pronto per Phase 2c: `fetch_protezione_civile_lazio()`.

### `output/dashboard.py` — Streamlit ✅

Dashboard read-only. Mostra previsioni correnti, storico temperature, metriche modello, grafico Previsto vs Osservato con MAE per stazione.

### `grid.py` — Griglia spaziale IDW + ERA5 ✅

Due funzioni principali:

- `compute_idw_grid(points, values, ...)` — IDW vettorizzato con numpy broadcasting. Nessun loop Python, istantaneo su 100×100 con 6 stazioni.
- `fetch_era5_batch(lats, lons, target_hour_utc, variables)` — singolo HTTP request batch a Open-Meteo per N punti e M variabili (`temperature_2m`, `relativehumidity_2m`). Ritorna `dict[str, list[float]]`.
- `bilinear_to_fine(coarse, coarse_lats, coarse_lons, fine_lats, fine_lons)` — interpola griglia sparsa ERA5 7×9 a griglia fine 100×100 con `scipy.interpolate.RegularGridInterpolator`.
- `wind_to_uv(speed_ms, direction_deg)` — decomposizione in componenti U/V con convenzione meteo (direction = "da dove arriva").

**Principio architetturale:** la mappa non mostra IDW puro su valori assoluti ma `T_ERA5(x,y) + IDW_correzioni(x,y)`. ERA5 fornisce il campo fisicamente realistico (lapse rate, SST marina, gradiente costa/interno); le 6 stazioni aggiungono la correzione microclima appresa dal modello. Stesso approccio per umidità.

Nuove funzioni aggiunte (giugno 2026): `build_sea_polygon()` — chiude `LATIUM_COAST` in un poligono mare/terra; `is_sea_mask()` — point-in-polygon vettorizzato via `matplotlib.path.Path`; `compute_coast_distance_grid()` — distanza punto-**segmento** (non solo vertice) dalla costa, evita artefatti circolari attorno ai promontori; `compute_sea_blend_weight()` — peso blend SST asimmetrico lato mare (smoothstep su fascia 25 km, `w=0` su tutta la terraferma incluse stazioni costiere).

### `sst.py`

Fetch Sea Surface Temperature da Open-Meteo Marine API (`marine-api.open-meteo.com/v1/marine`, variabile `sea_surface_temperature`). 5 punti offshore lungo la costa laziale (Civitavecchia, Fiumicino, Anzio, Sabaudia, Gaeta). Cache su `data/sst_cache.json` con TTL 4h — committata da `export.yml` così persiste tra run stateless di GitHub Actions. Fallback su cache scaduta se API non disponibile; se nessuna cache, restituisce `None` senza crashare (comportamento legacy).

### `scripts/export_static.py` — Export JSON per GitHub Pages ✅

Eseguito ogni 30 min da `export.yml`. Pipeline:

1. Legge stazioni attive, forecast recenti e osservazioni da Supabase
2. Fetch ERA5 batch: 63 punti griglia coarse + 6 stazioni = 69 punti, 2 variabili (`temperature_2m`, `relativehumidity_2m`) in un unico request
3. Calcola correzioni stazione: `T_modello_i − T_ERA5_i`
4. ERA5 coarse → bilinear → griglia fine 100×100
5. IDW correzioni 100×100
6. Griglia finale = ERA5_fine + IDW_correzioni (con `np.clip([0,100])` per umidità)
7. Scrive `docs/data/latest.json` (stazioni + temp_grid + humidity_grid) e `docs/data/wind_grid.json` (formato nativo leaflet-velocity, componenti U/V)

**Eseguire manualmente:**
```bash
cd ~/Desktop/meteo_locale
conda activate meteo
python3 scripts/export_static.py
```

**Rieseguire quando:** si aggiungono nuove stazioni, si modifica il bounding box griglia, si cambia la lista variabili ERA5.

**Dashboard-only mode:**
```bash
python3 scripts/export_static.py --dashboard-only
```
Genera solo `docs/data/dashboard_data.json` (serie storiche 7 giorni + MAE temperatura e umidità) senza le griglie ERA5/IDW. Usato dal job `export-dashboard` per aggiornarsi 2×/giorno.

### `docs/dashboard.html` — Dashboard Chart.js ✅

Pagina statica accessibile da `filippopetto-maker.github.io/meteo_locale/dashboard.html`. Link "📊 Dashboard →" nell'`#info-panel` di `index.html`.

**Sezioni:**
- **Switch Temperatura/Umidità** — aggiorna entrambi i chart in un click
- **Chart Previsto vs Osservato** (Chart.js line, asse X `time` via `chartjs-adapter-date-fns`): serie 7 giorni per la stazione selezionata; blu = previsto, arancio = osservato; filtra automaticamente i punti null (umidità spesso assente nelle osservazioni storiche)
- **Chart MAE per stazione** (Chart.js bar orizzontale): verde se MAE < 1.0°C (temperatura) o < 5.0% (umidità), rosso altrimenti; stazioni senza coppie → barra trasparente "(n/d)"; questo chart non cambia al cambio stazione

**Dati:** `docs/data/dashboard_data.json` — aggiornato 2×/giorno (08:00 e 20:00 UTC) dal job `export-dashboard` in `export.yml`.

---

## 🗺️ Roadmap

### ✅ Fase 1 — Modello sullo storico (COMPLETATA — giugno 2025)

1. [x] `historical.py`: dataset ERA5 + METAR, 4 stazioni, 2015–2024
2. [x] `features.py`: 5 strati di feature engineering
3. [x] `forecast.py`: LightGBM su tutti i target principali
4. [x] `model/correttore.py`: RF correttore secondo stadio
5. [x] `model/inference.py`: inference operativa testata
6. [x] `output/dashboard.py`: Streamlit live
7. [x] GitHub Actions: cron ogni 30 min, attivo e testato

### ✅ Fase 2a — Pipeline live METAR (COMPLETATA — giugno 2026)

1. [x] `mainMETEO.py`: raccolta METAR live, QC, insert in `observations`
2. [x] `ingestion.yml`: GitHub Actions cron 30 min attivo
3. [x] Dashboard: grafico Previsto vs Osservato + MAE per stazione
4. [x] Vista `forecast_vs_observed` con LATERAL JOIN

### ✅ Fase 2b — Netatmo live + espansione stazioni (COMPLETATA — giugno 2026)

1. [x] Netatmo OAuth2: registrazione dev.netatmo.com → client_id/secret/refresh_token
2. [x] `fetch_netatmo()`: 340+ stazioni pubbliche Roma, mediana cluster 5 km, QC, insert ogni 30 min
3. [x] Schema `stations`: +4 colonne orografiche (`microclima`, `dist_sea_km`, `dist_center_km`, `bearing_sea`)
4. [x] Rete espansa 4 → 6 stazioni attive: Ostia Lido, EUR, Trastevere, Tivoli, Castelli Romani
5. [x] `db.py`: `raw_source` nell'insert + upsert observations con `ignore_duplicates`
6. [x] `qc.py`: `STATION_TYPES` aggiornato con nuovi ID

### 🔄 Fase 2c — Fonti live aggiuntive (PROSSIMA)

1. [ ] Protezione Civile Lazio / OpenAmbiente: 238 centraline, 15 min → `fetch_protezione_civile_lazio()`

### ✅ Fase 3 — Output avanzato (COMPLETATA — giugno 2026)

1. [x] `grid.py` — IDW vettorizzato numpy, `fetch_era5_batch` (batch multi-variabile Open-Meteo), `bilinear_to_fine` (scipy RegularGridInterpolator)
2. [x] `scripts/export_static.py` — ERA5 background 7×9 (69 punti, 2 variabili in un unico request) + IDW correzioni microclima → `docs/data/latest.json` + `docs/data/wind_grid.json`
3. [x] `.github/workflows/export.yml` — commit automatico JSON su GitHub Pages, triggerato da cron-job.org a :05/:35
4. [x] `docs/index.html` + `docs/js/app.js` — mappa Leaflet.js full-screen, heatmap IDW temperatura (ERA5 + correzioni), heatmap umidità (ERA5 + correzioni), toggle layer, particelle vento leaflet-velocity, popup stazioni, click pointer con `lookupGrid` bilineare (valori coerenti con heatmap), legenda, pannello info timestamp
5. [x] GitHub Pages live: `https://filippopetto-maker.github.io/meteo_locale/`
6. [ ] API REST FastAPI — rimandato, sostituito da static JSON su GH Pages
7. [ ] CAPE da ERA5 → target thunderstorm — post-retraining dicembre 2026
8. [ ] Target pioggia puntuale (mm) — post-retraining dicembre 2026
9. [ ] LCZ Copernicus per isola di calore — post-retraining dicembre 2026

### 🔜 Fase 4 — Layer Vento interattivo (PROSSIMA)

1. [ ] Terzo pulsante nel toggle layer, primo a sinistra: `[ 🌬️ Vento ] [ 🌡️ Temperatura ] [ 💧 Umidità ]`; toggle `Adesso/+1h` nascosto quando il layer vento è attivo (il vento non ha griglia T+1 separata)
2. [ ] **Componente 1 — heatmap velocità**: canvas `L.imageOverlay`, stessa architettura di `renderGridLayer()`; palette bianco→ciano→blu→viola per velocità crescente; legenda 3 tick `[vMin, mid, vMax]` in km/h
3. [ ] **Componente 2 — frecce direzionali** (toggle checkbox, default off): griglia fissa di frecce SVG ruotate secondo `atan2(U, V)`; densità adattiva allo zoom (più frecce a zoom alto, fino a un massimo fissato); dimensione e spessore proporzionali alla velocità
4. [ ] Dati: `wind_grid.json` già esistente (36×24 celle, U+V in m/s, aggiornato ogni 30 min) — nessuna modifica al backend necessaria
5. [ ] Click su punto: temperatura + vento IDW + umidità come negli altri layer (già funziona, solo verificare coerenza con `wind_grid.json` attivo)

---

## 🐛 Diario degli errori risolti

| Errore | Causa | Soluzione |
|:-------|:------|:----------|
| `extension "timescaledb" is not available` | Free tier Supabase senza TimescaleDB | PostgreSQL standard + indici ottimizzati |
| `could not translate host name` | Porta 5432 bloccata da rete aziendale | API REST Supabase su HTTPS (porta 443) |
| `Tenant or user not found` | Formato URL pooler errato | Client ufficiale supabase-py con API key |
| `ping timeout` | ICMP bloccato dal router | Falso allarme — internet funzionante |
| `command not found: python` | macOS usa python3 | Uso di `python3` ovunque |
| `.env` non visibile nel Finder | File nascosto (punto iniziale) | Gestione via terminale |
| Coordinate Roma Nord errate | 41.016 invece di 42.016 | Corretto nello schema |
| RF correttore: file da 4.8 GB | `RandomForestRegressor` senza `max_depth` né `min_samples_leaf` su 264k righe | Obbligatorio: `max_depth=6, min_samples_leaf=10, n_jobs=-1` → 18 secondi e ~1 MB |
| `Invalid workflow file: inference.yml#L31` | Il nome dello step conteneva `: ` (due punti + spazio) — YAML lo interpreta come separatore chiave/valore | Aggiungere virgolette attorno al nome: `name: "Setup Miniconda (env: meteo, Python 3.12)"` |
| `refusing to allow a Personal Access Token to create or update workflow` | PAT creato solo con scope `repo`, mancava `workflow` | Rigenerare il PAT aggiungendo lo scope `workflow` nelle impostazioni token GitHub |
| `Authentication failed` con credenziali cached | macOS non aveva ancora salvato il token nel keychain — il fallimento precedente aveva lasciato lo stato inconsistente | Incorporare temporaneamente il token nell'URL remote: `git remote set-url origin https://user:TOKEN@github.com/...`, poi push, poi ripristinare URL pulito |
| Stazioni duplicate (16 invece di 4) | Insert ripetuto della tabella `stations` durante i test | `DELETE FROM stations WHERE id > 4` + `ALTER TABLE stations ADD CONSTRAINT UNIQUE (lat, lon)` |
| `forecast_vs_observed` NULL su tutte le righe | METAR timestamp (es. 21:20) troppo lontano da `valid_for` (22:00) — gap 40 min > finestra 30 min | LATERAL JOIN con tolleranza 3600s che trova l'osservazione più vicina nel tempo |
| `ERROR: cannot drop columns from view` | `CREATE OR REPLACE VIEW` non può rimuovere colonne esistenti | `DROP VIEW IF EXISTS` prima della ricreazione |
| `duplicate key value violates unique constraint "observations_station_id_recorded_at_key"` | METAR riusa il timestamp fisso dell'osservazione aeroportuale — se lo script gira due volte nella stessa mezz'ora, tenta di inserire lo stesso `(station_id, recorded_at)` | `upsert` con `ignore_duplicates=True` su `observations` |
| `column "microclima" of relation "stations" does not exist` | Le colonne orografiche (`microclima`, `dist_sea_km`, `dist_center_km`, `bearing_sea`) non erano nel DDL originale | `ALTER TABLE stations ADD COLUMN IF NOT EXISTS ...` per ciascuna |
| `duplicate key value violates unique constraint "stations_latlon_unique"` (su INSERT nuove stazioni) | Le coordinate della nuova stazione coincidevano con una stazione esistente già inattiva | `UPDATE` della stazione esistente invece di `INSERT`; per le coordinate realmente nuove, `INSERT` funziona |
| `Uncaught TypeError: Cannot read properties of null (reading 'data')` in leaflet-velocity | Header wind_grid.json privo di `parameterCategory: 2` — la libreria identifica U/V via `parameterCategory + "," + parameterNumber` (`"2,2"` e `"2,3"`); senza `parameterCategory` il match fallisce e i component grid restano `null` | Aggiunto `"parameterCategory": 2` a entrambi gli header U e V in `export_static.py` |
| `.env ` (con spazio in coda) committato → GitHub push protection blocca il push | Claude Code ha creato un file `.env ` (trailing space ASCII 32) non coperto dalla regola `.env` in `.gitignore` | `git update-index --force-remove ".env "` + `git commit --amend` + aggiunto `.env\ ` (backslash-space) in `.gitignore` |
| `export.yml` fallisce con exit code 128 | GitHub Actions di default ha permessi read-only; il workflow fa `git push` che richiede write | `Settings → Actions → General → Workflow permissions → Read and write permissions` |
| Conflict su `docs/data/latest.json` durante `git pull --rebase` | Il workflow `export.yml` ha committato i JSON mentre era in corso un push locale | `git checkout --theirs docs/data/latest.json` + `git add` + `git rebase --continue` |
| Mappa umidità fisicamente sbagliata: mare più secco dell'entroterra | IDW puro non ha conoscenza fisica del territorio — interpola geometricamente tra stazioni senza sapere che il mare è sorgente di umidità | Sostituito IDW puro con ERA5 background (`relativehumidity_2m`) + IDW correzioni microclima, identico all'approccio temperatura |
| Click popup mostra valori diversi dal colore heatmap | Popup usava IDW da 6 stazioni (valori assoluti), heatmap usava ERA5+correzioni — due calcoli diversi sullo stesso punto | Sostituito `idwPoint` con `lookupGrid` (lookup bilineare diretto sul grid JSON) — garantisce coerenza esatta tra colore e valore mostrato |
| Particelle vento non visibili, nessun errore apparente | `parameterUnit` assente nell'header leaflet-velocity (necessario per display) | Aggiunto `"parameterUnit": "m.s-1"` agli header U e V |
| Login ARSIAL SIARL non automatizzabile | siarl.arsial.it richiede CIE/SPID (identità digitale nazionale) | Download manuale CSV + bias correction one-shot |
| Temperatura mare gonfiata (31°C su Ostia) | IDW spalma correzione stazioni di terra anche sulle celle di mare; nessuna distinzione terra/mare nella griglia | SST reale da Marine API + maschera `is_sea_mask` + blend graduale asimmetrico in `export_static.py` |
| Bordo netto / arcobaleno lungo la costa | Maschera binaria (`np.where`) + fascia blend troppo stretta (10 km) + distanza da vertice crea cerchi concentrici sui promontori (Circeo) | Distanza punto-segmento + smoothstep su fascia 25 km + blend asimmetrico (w=0 su terra, 0→1 solo verso mare) |
| Riga diagonale artificiale sopra Civitavecchia | `LATIUM_COAST` si fermava a 42.10° (Civitavecchia); il poligono chiudeva dritto all'angolo del bbox classificando Tarquinia/Orbetello come mare | Estesa la coastline a nord fino a (42.85, 10.85) seguendo la costa reale Toscana; il poligono si restringe a zero naturalmente nell'angolo NO |
| `ModuleNotFoundError: No module named 'matplotlib'` | Aggiunto a `requirements.txt` ma non installato nell'ambiente `meteo` locale; il blocco SST in `export_static.py` falliva silenziosamente nel try/except | `pip install matplotlib` nell'ambiente conda `meteo`; aggiunto anche a `pip install` nel workflow |
| Stazioni Tivoli/Filettino/Cassino sempre "osservata: n/d" | Due funzioni `fetch_netatmo()` esistevano in due file diversi (`mainMETEO.py` e `fetch_netatmo_block.py`); solo `mainMETEO.py` è collegata a `ingestion.yml`, l'altra non è mai stata eseguita in produzione nonostante avesse `LAZIO_BBOXES` e la fix `min_cluster` già pronte | Fix applicate sul file giusto (`mainMETEO.py`); `fetch_netatmo_block.py` rinominato `_unused_fetch_netatmo_block.py` per evitare confusione futura |
| `getpublicdata` Netatmo azzera cluster su zone dense (EUR, Trastevere) con bbox esteso a tutto il Lazio | L'API sembra avere un tetto di risultati per chiamata: bbox più ampio non aggiunge stazioni nelle zone dense, le diluisce a favore di copertura geografica più ampia | 5 sotto-bbox (`LAZIO_BBOXES`, margine 0.15° di sovrapposizione) con fetch separato + merge deduplicato su `_id` Netatmo, invece di un singolo bbox per tutto il Lazio |
| IDW usava previsioni LGBM invece di osservazioni Netatmo | Bug logico in export_static.py | Corretto: IDW ora usa dati Netatmo reali per stazioni 33–38 |

**23/06/2026 — Aggiornamenti UI:**
- Toggle unità vento km/h ↔ nodi in `app.js` + `index.html` (radio button sotto checkbox vento)
- Popup stazioni aggiornato in tempo reale al cambio unità via `setPopupContent`
- Popup IDW (click mappa) usa `formatWind()` — aggiornato al click successivo

**Giugno 2026 — Dashboard e fix workflow:**
- Job `export` e `export-dashboard` in conflitto su push: run parallele sullo stesso branch → il secondo trova il remote già avanzato e fallisce con `fetch first` → `git pull --rebase origin main` prima del push in entrambi i job
- Info panel: label "Previsioni per le ore XX:XX" era inline accanto a "Aggiornato:" → aggiunto `<br>` tra i due `<span>`; label si aggiorna anche al toggle Adesso/+1h
- Nominatim restituiva "Municipio Roma XII": `suburb` conteneva il nome del municipio → logica cambiata in `"${city}, ${quarter}"` con `city = a.city||a.town||a.municipality` e `quarter = a.neighbourhood||a.quarter||a.suburb||a.village`; `zoom=10→14`
- Tick legenda sovrapposti con 5 tick su 160 px → ridotti a 3 tick dinamici `[vMin, mid, vMax]` per entrambi i layer
- `displayValues: true` su leaflet-velocity mostrava pannello "Wind Direction / Wind Speed" al movimento del cursore → `displayValues: false`

---

## 🏆 Differenziali competitivi

- **Statistical downscaling ERA5 → stazioni reali** — approccio corretto e sostenibile vs NWP pesante; impara le correzioni che il modello globale sbaglia
- **Rete Netatmo densa** — 340+ stazioni pubbliche nel bbox Roma aggregano il segnale urbano reale ogni 30 min, con QC spaziale integrato su cluster di 5 km
- **Architettura multi-stazione** — 6 stazioni con profili orografici contrastanti (costiera, urbano, quota, pianura) abilitano l'apprendimento dei gradienti territoriali
- **Feature orografiche esplicite** — delta quota vs cella ERA5, onshore alignment, isola di calore, one-hot microclima: il territorio codificato come predittori
- **Modello a due stadi** — LightGBM cattura il segnale principale; RF correttore elimina gli errori sistematici residui per microzona
- **QC climatologico contestuale** — validazione contro climatologia locale per mese e fascia oraria, con offset per tipo di stazione; raro nei tool open source
- **Soglie aggiornate ai cambiamenti climatici** — trend 2015–2024, non medie storiche obsolete
- **Split temporale rigoroso** — nessun leakage tra training e validation; `temporal_split()` condiviso tra `forecast.py` e `correttore.py` garantisce split identico
- **Addestramento immediato sullo storico** — nessuna attesa per accumulare dati live
- **Deploy autonomo a costo zero** — GitHub Actions cron, Supabase free tier, Open-Meteo gratuito, Netatmo pubblico: zero spesa operativa
- **Infrastruttura robusta** — Streamlit dashboard live, metriche su DB, modelli versionati
- **Mappa iperlocale Windy-style** [Fase 3] — visualizzazione del gradiente microclima
  Roma su carta interattiva: il campo colorato mostra le previsioni corrette dal modello
  (non ERA5 grezzo), le particelle animate mostrano il vento iper-locale. Nessuna app
  mainstream mostra la differenza termica Trastevere/Tivoli su una mappa zoomabile.

---

## 📌 Come riprendere il lavoro

```bash
cd ~/Desktop/meteo_locale
conda activate meteo
python3 db.py   # verifica connessione
```

**Riferimento GitHub:** `https://github.com/filippopetto-maker/meteo_locale`

**Stato corrente (giugno 2026):** Phase 1, 2a, 2b, 3 in produzione. Phase 2c parziale (bias correction attiva). Tre GitHub Actions attivi, tutti triggerati da cron-job.org:
- `inference.yml` — previsioni ogni 30 min (LightGBM + RF + ARSIAL bias), trigger :00/:30
- `ingestion.yml` — osservazioni METAR + Netatmo ogni 30 min, trigger :00/:30
- `export.yml` — due job: `export` (JSON statici ogni 30 min, trigger :05/:35) + `export-dashboard` (`dashboard_data.json` 2×/giorno, 08:00/20:00 UTC via cron-job.org `workflow_dispatch`)

**Mappa live:** `https://filippopetto-maker.github.io/meteo_locale/`

**Prossima scadenza fissa: Dicembre 2026** — retraining completo con Netatmo accumulato.

**Completato (giugno 2026):**
- Correzione SST sul mare: `sst.py` + blend graduale asimmetrico in `grid.py` + `export_static.py`; `LATIUM_COAST` estesa da Anzio→Gaeta a sud e fino a (42.85, 10.85) a nord
- Toggle T / T+1h sulla mappa: `temp_grid_observed` + `temp_grid_forecast` in `latest.json`; scala colori unificata tra i due stati
- 4 nuove stazioni (id 51–54): Fiano Romano, Civitavecchia, Filettino 1044m, Gaeta
- Palette umidità ridisegnata per contrasto reale nel range 40-80% (pivot verde)
- Fix copertura Netatmo: `mainMETEO.py` portato da `ROMA_BBOX` singolo a `LAZIO_BBOXES` (5 sotto-zone con dedup), risolve sia i buchi geografici (Cassino, Filettino) sia il "soffocamento" delle zone dense (EUR, Trastevere) causato dal tetto di risultati per chiamata Netatmo
- `min_cluster` rilassato a 1 per id≥39 e per microclima `quota`/`alta_quota`/`colline_interne`
- Pulizia naming: rinominate stazioni con nomi duplicati/imprecisi (Saxa Rubra id46→Labaro, Gaeta/Formia id49→Fondi); riclassificati microclima (Tivoli/Bracciano/Rieti→`colline_interne`, Filettino→`alta_quota`, isolando `quota` alla sola Castelli Romani)

**Dashboard live:** `https://filippopetto-maker.github.io/meteo_locale/dashboard.html`

**Prossimo task:** Layer Vento interattivo (Fase 4) — terzo pulsante toggle `🌬️ Vento`, heatmap velocità `L.imageOverlay` + frecce SVG direzionali (densità adattiva allo zoom). Dati `wind_grid.json` già disponibili, nessuna modifica backend. Vedere roadmap Fase 4 per spec completa.

**Miglioramenti futuri mappa:**
- Più stazioni: settore ovest (Bracciano, Ostia Nord) e nord completamente scoperti dall'IDW — ogni nuova stazione migliora il gradiente senza modifiche al codice
- Upgrade a MapLibre GL JS per qualità visiva superiore (vettoriale, tile più dettagliate)
- FastAPI su Render per query dinamiche (storico per stazione, confronto date)
- Layer vento interattivo (Fase 4): heatmap velocità + frecce SVG direzionali — spec completa in roadmap
- Upgrade `actions/checkout@v4` → `@v5` e `actions/setup-python@v5` → versione corrente (warning Node.js 20 deprecation)

---

*Progetto sviluppato da Filippo · Sistema di previsioni meteo iper-locali · Roma*
