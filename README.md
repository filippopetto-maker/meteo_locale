# 🌦️ Meteo Locale — Sistema di Previsioni Meteo Iper-Locali per Roma e il Lazio

Sistema di previsione meteo su scala comunale che cala lo stato meteorologico regionale sul singolo punto, catturando i microclimi che i modelli globali non vedono. Accuratezza territoriale superiore alle app mainstream, infrastruttura a costo zero.

**Stato:** Phase 1, 2a, 2b completate e in produzione. Phase 2c parzialmente completata (bias correction ARSIAL attiva). Phase 3 — **mappa interattiva live su GitHub Pages** (Leaflet + leaflet-velocity, heatmap temperatura/umidità/vento + particelle/frecce). **Restyling estetico dark theme completato (luglio 2026)**: basemap CartoDB Dark Matter, pannello controlli unificato con segmented control e switch stile iOS, popup e legenda ristilizzati mantenendo invariata la logica di calcolo dati. GitHub Actions attivo, inference e ingestion automatica ogni 30 minuti. **32 stazioni attive** su tutto il Lazio (6 Roma metro + 26 espansione Lazio) con copertura Netatmo live e correzione bias ARSIAL data-driven. Mappa con **correzione SST reale sul mare** (Open-Meteo Marine API, blend graduale asimmetrico) e **toggle T / T+1h** (Adesso / +1h). **Dashboard Chart.js live** (`dashboard.html`) con forecast vs observed 7 giorni per stazione, MAE per stazione, switch Temperatura/Umidità.

**Modello (settembre 2026):** il backtest su tre mesi di run ECMWF archiviati mostra che il modello di produzione peggiora l'IFS grezzo contro il target Netatmo (MAE 2,68 contro 2,13 °C su 1–48 h). Un modello riaddestrato sullo storico Netatmo ricostruito — **M2**: IFS + residuo LightGBM — scende a 0,97 °C ed è **in prova in ombra dal 25/09 al 16/10/2026**, senza effetti sulla mappa (vedi Fase 4a).

---

## 📑 Indice

1. [Visione del progetto](#-visione-del-progetto)
2. [Perché questo approccio](#-perché-questo-approccio)
3. [Architettura](#-architettura)
4. [Stack tecnologico](#-stack-tecnologico)
5. [Fonti dati](#-fonti-dati)
6. [Feature orografiche](#-feature-orografiche)
7. [Stato attuale](#-stato-attuale)
8. [Operazioni da svolgere](#-operazioni-da-svolgere)
9. [Roadmap estesa — Fasi 4–7](#-roadmap-estesa--fasi-47)
10. [Sviluppo a lungo termine](#-sviluppo-a-lungo-termine)
11. [Come riprendere il lavoro](#-come-riprendere-il-lavoro)
12. [Risultati del modello](#-risultati-del-modello)
13. [Struttura del progetto](#-struttura-del-progetto)
14. [Database — schema](#-database--schema)
15. [Setup e installazione](#-setup-e-installazione)
16. [I moduli](#-i-moduli)
17. [Differenziali competitivi](#-differenziali-competitivi)
18. [Diario degli errori risolti](#-diario-degli-errori-risolti)

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
│  Netatmo getmeasure → target storico orario 2024→  │
│  ECMWF IFS (Open-Meteo) → input previsionale 1–48h  │
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
│  bias_table · forecasts_shadow (prova in ombra)     │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│              LAYER 3 — PROCESSING                   │
│  QC (range·climatologico·persistenza·spaziale)      │
│  Feature engineering (5 strati, 76 colonne)         │
│  LightGBM (previsione) + RF (correttore residui)    │
│  M2: IFS + residuo LightGBM (in ombra, 1–48h)       │
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
- **Netatmo Public API** — rete di stazioni personali pubbliche. 340+ stazioni nel bbox Roma, aggregazione mediana per cluster 5 km, QC a 4 livelli. 32/32 stazioni coperte ogni 30 min. OAuth2 con refresh_token.

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

Esecuzione a breve termine, fase per fase: cosa è completato, cosa è in corso, cosa manca. Le fasi completate elencano le componenti principali (non ogni singolo dettaglio implementativo — quelli sono in *I moduli* e in *Database — schema*). Il piano strategico di lungo periodo è nella sezione **Roadmap estesa — Fasi 4–7** che segue.

### ✅ Fase 1 — Modello sullo storico (COMPLETATA — giugno 2025)

1. [x] Schema Supabase (5 tabelle + 2 viste) + `db.py` — layer di connessione via API REST
2. [x] `historical.py` — dataset ERA5 + METAR, 4 stazioni, 2015–2024 (~331k righe × 76 colonne)
3. [x] `features.py` — 5 strati di feature engineering
4. [x] `qc.py` — Quality Control 4 livelli
5. [x] `forecast.py` — LightGBM su tutti i target principali (temperatura, wind_speed, wind_direction, humidity)
6. [x] `model/correttore.py` — RF correttore secondo stadio
7. [x] `model/inference.py` — inference operativa (testata con `--dry-run` e run live)
8. [x] `output/dashboard.py` — Streamlit dashboard live
9. [x] `.github/workflows/inference.yml` — cron ogni 30 min, attivo (prima run manuale: 1m 20s)

### ✅ Fase 2a — Pipeline live METAR (COMPLETATA — giugno 2026)

1. [x] `mainMETEO.py` — raccolta METAR live via IEM ASOS (LIRA/LIRF), QC integrato, insert in `observations` (upsert idempotente)
2. [x] `ingestion.yml` — cron 30 min, attivo e testato (1m 16s)
3. [x] Vista `forecast_vs_observed` — LATERAL JOIN, tolleranza 60 min per disallineamento METAR
4. [x] Dashboard Streamlit: sezione "Previsto vs Osservato" (grafico Altair) + MAE per stazione, timezone Europe/Rome, direzione vento cardinale
5. [x] Vincoli UNIQUE (`stations.lat,lon`; `forecasts.station_id,valid_for`) + upsert su `forecasts`

### ✅ Fase 2b — Netatmo live + espansione stazioni (COMPLETATA — giugno 2026)

1. [x] Netatmo OAuth2 (dev.netatmo.com) + `fetch_netatmo()` in `mainMETEO.py`: 340+ stazioni pubbliche Roma, mediana cluster 5 km, QC, insert ogni 30 min
2. [x] Schema `stations`: +4 colonne orografiche (`microclima`, `dist_sea_km`, `dist_center_km`, `bearing_sea`)
3. [x] `db.py`: `raw_source` nell'insert + upsert `ignore_duplicates=True` (fix 409 su METAR timestamp fisso); `qc.py` `STATION_TYPES` aggiornato con nuovi ID
4. [x] Rete espansa 4 → 6 (+Ostia Lido, EUR, Trastevere, Tivoli, Castelli Romani) → **32 stazioni attive** su tutto il Lazio (commit `82467c6`): `LAZIO_BBOXES` — 5 sub-bbox sovrapposte con dedup MAC (sostituisce `ROMA_BBOX`), `min_cluster=1` per id≥39 o microclima `quota`/`alta_quota`/`colline_interne`
5. [x] Stazioni 56–60 aggiunte 23/06/2026: Rocca Sinibalda (alta_quota, 980m), Sigillo (quota, 648m), Tarquinia (costiera, 138m), Tor Bella Monaca (urban_canyon, 70m), Tor Vergata Est (urban_canyon, 59m)
6. [ ] Castelli Romani alta quota (~530m, MAC `70:ee:50:2c:be:10`) — offline, da aggiungere come id 61

### 🔄 Fase 2c — Fonti live aggiuntive (PROSSIMA)

1. [ ] Protezione Civile Lazio / OpenAmbiente — 238 centraline ogni 15 min → `fetch_protezione_civile_lazio()` (stub già presente in `mainMETEO.py`)

### ✅ Fase 3 — Output avanzato (COMPLETATA — giugno 2026)

1. [x] `grid.py` — IDW vettorizzato numpy, `fetch_era5_batch` (batch multi-variabile Open-Meteo), `bilinear_to_fine` (scipy RegularGridInterpolator)
2. [x] `scripts/export_static.py` — ERA5 background 17×27 (griglia coarse + stazioni attive, 3 variabili in un unico request) + IDW correzioni microclima → `docs/data/latest.json` + `docs/data/wind_grid.json`
3. [x] `.github/workflows/export.yml` — commit automatico JSON su GitHub Pages, trigger esterno via cron-job.org (`workflow_dispatch`, nessuno `schedule:` interno)
4. [x] `docs/index.html` + `docs/js/app.js` — mappa Leaflet.js full-screen, heatmap IDW temperatura e umidità (ERA5 + correzioni), toggle layer, particelle vento leaflet-velocity, popup stazioni, click pointer con `lookupGrid` bilineare (valori coerenti con heatmap), legenda, pannello info timestamp
5. [x] GitHub Pages live: `https://filippopetto-maker.github.io/meteo_locale/`
6. [x] **Carta del vento** — heatmap velocità (ERA5 background + IDW correzioni, scala adattiva `ws_min`/`ws_max`), frecce barbed sinottiche zoom-adaptive (stanghette per intensità, punta direzionale), toggle particelle/frecce, legenda km/h ↔ nodi (`wind_speed_grid` in `latest.json`, `app.js`)
7. [x] **Dashboard GitHub Pages (Chart.js)** — `docs/dashboard.html` + `dashboard_data.json` (serie forecast/observed 7gg, MAE globale e per stazione). Generata da `export_static.py --dashboard-only`, workflow dedicato `export-dashboard.yml` (trigger 8:00/20:00). Sostituisce la dashboard Streamlit. Dettagli tecnici → sezione *I moduli*, `docs/dashboard.html`
8. [ ] API REST FastAPI — rimandato, sostituito da static JSON su GH Pages; resta opzionale in futuro per query dinamiche (storico per stazione, confronto date; ipotesi deploy su Render)

### 🌡️💧 Umidità osservata, temperatura percepita e bulbo umido — settembre 2026

Estensione del layer Umidità alla parità funzionale con Temperatura, seguita da due grandezze derivate — nessuna modifica al training o ai modelli, solo pipeline di export e frontend.

**1. Umidità osservata + toggle Adesso/+1h**

L'umidità osservata era già salvata in `observations` (Netatmo la fornisce sempre) ma scartata in fase di export per una frammentarietà storica dei dati vecchi che non è più presente: verificato sulle serie a 7 giorni, 11.801/11.801 righe `observed` hanno `hum` valorizzato (100%), unica eccezione la stazione id 57 (zero osservazioni in assoluto, muta — non frammentata).

- `scripts/export_static.py`: nuova funzione `_build_hum_grid()` sul modello di `_build_temp_grid()`, genera `humidity_grid_observed` e `humidity_grid_forecast` (prima solo `humidity_grid`, forecast-only). Nessun blend SST sull'umidità (non applicabile, a differenza della temperatura).
- `observation.humidity` ora esposto per stazione in `latest.json`
- `docs/js/app.js`: `#time-toggle` esistente esteso al layer Umidità (nessun controllo duplicato — stesso pattern già in uso per Temperatura)
- **Scala colore umidità passata da fissa (0-100%) a dinamica unificata** tra le due griglie, stesso meccanismo di `globalTMin/globalTMax`. Corregge anche un bug preesistente: la legenda dichiarava `h_min/h_max` dinamici mentre la heatmap renderizzava su 0-100 fisso — valori e colori non corrispondevano

**2. Temperatura percepita (Humidex + Wind Chill) — tab Temperatura**

Switch `Reale / Percepita`. Calcolo interamente client-side: nessuna nuova chiamata backend, deriva da `temp_grid_*` + `humidity_grid_*` + `wind_speed_grid` già presenti in `latest.json`.

Formula a tre rami, non solo Humidex puro — verificato che l'Humidex da solo degenera sotto ~15°C (restituisce valori *inferiori* alla temperatura reale, effetto spurio della formula, non fisiologico):
- T ≥ 20°C → Humidex (Environment Canada)
- T ≤ 10°C e vento > 4,8 km/h → Wind Chill (NWS/EC 2001)
- 10-20°C, o vento insufficiente → temperatura reale

**3. Bulbo umido (Stull 2011) — tab Umidità**

Switch separato `Reale / Bulbo umido`, deliberatamente **non** nella tab Temperatura: il bulbo umido è una grandezza fisica (temperatura minima raggiungibile per evaporazione), quasi sempre *inferiore* alla temperatura dell'aria — l'opposto di ciò che l'utente si aspetta da "percepita". Etichettarlo come tale sarebbe stato fuorviante.

RH clampata a [5, 99] (dominio di validità della formula) prima del calcolo, silenziosamente per cella — a saturazione il bulbo umido coincide comunque con la temperatura, quindi il clamp introduce un errore trascurabile ed evita buchi nella heatmap nelle zone di nebbia/pioggia.

**Comune a entrambe le derivate:**
- 4 griglie derivate (percepita/bulbo umido × osservato/+1h) calcolate una sola volta post-fetch, non ad ogni cambio layer
- Overlay "Dati assenti" quando una griglia sorgente manca (< 2 stazioni valide), invece di un buco silenzioso in mappa
- Popup click mappa: valore derivato mostrato **sempre** accanto alla temperatura reale nella tab pertinente (percepita in Temperatura, bulbo umido in Umidità), letto con `lookupGrid` sulla griglia derivata — mai ricalcolato al volo, per restare coerente al pixel con la heatmap. Vento e Radar invariati, nessun derivato

**Limiti noti, non bloccanti:**
- Wind Chill usa `wind_speed_grid`, disponibile solo in versione forecast — per "Adesso" è un'approssimazione. Interviene solo sotto i 10°C: nullo in estate, da monitorare al primo autunno freddo
- Stull assume pressione al livello del mare — errore di qualche decimo di grado alle quote del Lazio (stazioni `alta_quota`, già in cold-start)

**Verificato prima del deploy:** su dati reali del 7 settembre (55.000 celle), Humidex 28,8-43,1°C contro T 22,9-29,7°C (sale con l'afa, atteso), bulbo umido 19,5-27,4°C (sta sotto, atteso) — le due grandezze si muovono in direzioni opposte, a conferma che tenerle in sezioni separate era la scelta corretta.

Il piano di lungo periodo (previsioni 48h, retraining di dicembre 2026, generalizzazione multi-località, convettività) è nella sezione **Roadmap estesa — Fasi 4–7** più sotto. Prima, le operazioni concrete in sospeso sulla mappa/UI (sezione seguente).

---

## 🔧 Operazioni da svolgere

Task concreti sulla mappa/UI, più vicini nel tempo e più piccoli in scope rispetto alle Fasi 4–7 — non toccano pipeline dati o modello.

| # | Task | Stato | Dettagli |
|:--|:-----|:------|:---------|
| 1 | Modernizzazione legenda/timeline radar | 🔴 Da avviare | Radar RainViewer attuale mostra solo "adesso..." statico. Serve barra timeline scrubbabile con frame passati (e valutare nowcast, oggi esclusa di proposito) con etichette orarie lungo tutta la barra, play/pause. |
| 2 | Immagini satellitari oltre al radar | 🔴 Da avviare | Layer satellite (infrared) RainViewer, stesso pattern a tile del radar attuale — nessun nuovo backend richiesto, coerente col vincolo costo-zero. |

---

## 🧱 Roadmap estesa — Fasi 4–7

Tre obiettivi strategici di lungo periodo, non indipendenti: l'ordine in cui si affrontano cambia il costo totale. Il principio organizzatore è che il retraining di dicembre 2026 è l'operazione più costosa del progetto e va fatta una volta sola con tutte le novità dentro. Tutto ciò che precede dicembre prepara quel retraining; tutto ciò che segue lo sfrutta.

*Aggiornamento 25/09/2026:* il backtest del 24/09 ha anticipato una parte del lavoro. Un modello riaddestrato sul target Netatmo (M2) è in prova in ombra da settembre; dicembre resta il momento del riaddestramento completo (più dati, autunno incluso, altri target).

**I tre obiettivi:**

1. Standardizzazione — da prodotto Roma-specifico a scatola eseguibile per qualsiasi località inserendo solo la posizione.
2. Previsioni orarie fino a 48h — previsioni ora-per-ora sul punto scelto, visualizzate su sito.
3. Convettività — CAPE, soleggiamento, indici convettivi (sempre modulati dall'orografia) → pioggia, perturbazioni, temporali forti.

**Vincolo trasversale** (vale per tutte le fasi): girare senza grandi server esterni. LightGBM/RF restano file da pochi MB; l'inference resta dentro GitHub Actions free tier. L'unico punto da monitorare è la dimensione della nuova tabella di training (vedi Fase 4a).

**Principio metodologico fondante (da non dimenticare mai):** il MOS impara correzioni specifiche delle stazioni su cui è addestrato: il modello non "sa" il meteo, sa quanto ERA5 sbaglia a Trastevere, a Tivoli, a Ostia. Non si sposta il modello — si sposta la fabbrica che produce il modello. Il prodotto generalizzabile non è `lgbm_temperature.txt`, è la pipeline.

Corollario operativo immediato: da ora ogni nuovo pezzo di codice nasce già config-driven (niente nuovi valori Roma hardcoded). Così la generalizzazione (Fase 6) diventa una migrazione del codice vecchio, non una riscrittura del nuovo.

### 🟦 Fase 4a — Infrastruttura 48h e modello M2 in ombra (settembre → ottobre 2026)

**Punto di partenza — verificato sul codice il 22/09/2026.** Le versioni precedenti di
questa sezione davano per da fare una migrazione già avvenuta. `model/inference.py`
NON riceve ERA5: chiama `api.open-meteo.com/v1/forecast` (`past_days=2,
forecast_days=2`), quindi l'input operativo è già NWP previsionale. ERA5
(`archive-api`) sopravvive solo nel training, dentro `historical.py`.

Due conseguenze che cambiano il piano:

- **I lag in produzione sono già previsionali.** `add_lag_features` fa `shift(n)`
  sulla serie che riceve, e in inference quella serie è la catena NWP (passato +
  futuro nella stessa tabella). Il refactor "features.py in due modalità" riguarda
  quindi il *training* di dicembre — dove i lag stanno su ERA5 e vanno spostati
  sulla catena del run archiviato — non l'inference. Vedi *Preparazione al retraining*, R3.
- **Il modello non è vincolato al presente.** `predict_station()` restituisce una
  riga sola perché prende `eligible.iloc[[-1]]`, l'ultima riga ≤ adesso: le ~47 righe
  future scaricate sono input mai interrogato, non previsioni scartate. Il contratto
  appreso è "stato atmosferico a X → temperatura locale a X+1h", e X non deve essere
  adesso. Passando la riga NWP a V−1h si ottiene la previsione per V.

**Quindi il lavoro non è il motore** (una `booster.predict()` su matrice 48×N invece
che 1×N) **ma l'impalcatura attorno**: dimensione lead nel DB, contratto JSON,
difese sui consumer esistenti, workflow dedicato.

**Il limite della versione 0.** Resta l'input distribution mismatch, in una forma
precisa: MOS a lead costante. Il modello tratta ogni riga d'input come se fosse
pulita a lead 0, perché così l'ha vista in training (ERA5 ≈ verità). La riga a V−1h
con V=+48h porta invece 47 ore di errore NWP, di un tipo mai visto in addestramento:
la correzione microclimatica viene applicata correttamente, l'errore NWP passa
intatto. Non può correggere un errore di cui ignora l'esistenza. Criterio di successo
dichiarato: **battere l'NWP grezzo a ogni lead**, non raggiungere un MAE assoluto — alzato il
24/09/2026 a *battere l'IFS meno bias mobile* (opzione E, vedi *Backtest IFS e MOS minimo*).

**Decisioni di schema:**

1. **`lead_hours` in `forecasts`** — ✅ applicata il 23/09/2026. Prima della migrazione
   il vincolo era `UNIQUE (station_id, valid_for)`
   e `db.insert_forecast` faceva upsert su quella chiave: scrivendo 48 lead per run, ogni
   run sovrascrive il precedente sullo stesso `valid_for` e la dimensione lead sparisce
   per sempre — con essa la curva MAE vs lead, irrecuperabile a posteriori. Serve
   colonna `lead_hours SMALLINT`, backfill a 1 sulle righe esistenti, vincolo
   `UNIQUE (station_id, valid_for, lead_hours)`. Colonna esplicita e non `forecast_at`
   nella chiave, perché `lead_hours` è anche la feature di dicembre e il filtro di ogni
   query futura.
2. **DB = registro di validazione, JSON = prodotto** — principio valido, da applicare con il punto 6 del piano. 32 stazioni × 48 lead = 1.536
   righe/run; alla cadenza attuale di 30 min sono ~74.000 righe/giorno, ~2,2 M/mese su
   un free tier da 500 MB. Il prodotto ha bisogno delle 48 ore piene solo nel JSON; il
   DB solo di ciò che serve a validare. Si persistono i lead {1, 3, 6, 12, 24, 36, 48}
   su un workflow 48h a cadenza bassa (3-oraria), mentre `inference.yml` resta a T+1h
   ogni 30 minuti.
3. **Copertura temporale della fetch** — ✅ applicata il 24/09/2026 (commit `56137d31`). Open-Meteo ancora l'orario a mezzanotte del
   giorno corrente: con `forecast_days=2`, alle 21:00 UTC restano ~27 ore di futuro,
   non 48. Serve `forecast_days=4` con taglio a 48 righe da adesso. `past_days=2` resta
   corretto per il warm-up (max lag 6 + max rolling 12).
4. **Bias ARSIAL indicizzato su `valid_for`** — ✅ applicata il 24/09/2026 (commit `56137d31`).
   Prima la correzione usava `datetime.now().month`: su una serie di 48h a cavallo di fine mese applica il bias
   del mese sbagliato alle ultime ore.

**Stato del DB (aggiornato al 25/09/2026), migrazioni Supabase in ordine:**

- `forecasts_add_lead_hours` (23/09) — colonna `lead_hours SMALLINT NOT NULL DEFAULT 1` (le
  76.186 righe esistenti → 1), vincolo `UNIQUE (station_id, valid_for, lead_hours)`.
  Rimossi i due vincoli precedenti: `(station_id, valid_for)` e un
  `(station_id, valid_for, model_version)` mai documentato, scoperto leggendo lo schema live
- `model_metrics_align_to_training_code` (23/09) — fix del bug per cui nessuna metrica di
  training è mai stata salvata (vedi *Diario degli errori*)
- `forecasts_bridge_legacy_unique` (23/09) — **ponte temporaneo, ancora attivo**: ripristina
  `UNIQUE (station_id, valid_for)` accanto al nuovo. Innocuo finché si scrive solo lead 1;
  **va rimosso prima della prima riga con lead > 1** in `forecasts`. `inference.py` rifiuta
  (exit 2) le scritture con lead > 1 senza `--allow-multi-lead`
- Colonne `nwp_temperature`, `nwp_humidity`, `nwp_run_at` in `forecasts` (24/09) — valore NWP
  grezzo a `valid_for` accanto alla previsione (decisione B)
- Colonne `temperature_bc`, `bc_bias` in `forecasts` e tabella `bias_table` (24/09) — create per
  l'ombra dell'opzione E dentro `inference.py`. Il 25/09 si è deciso di non toccare
  l'inference: `bias_table` è usata dalla prova in ombra di M2, le due colonne restano vuote
- `forecasts_shadow_m2` (25/09) — tabella `forecasts_shadow` e vista `shadow_vs_observed` per la
  prova in ombra (vedi *Prova in ombra M2*)

**Piano operativo Fase 4a:**

1. [x] SQL su Supabase: `lead_hours` + backfill a 1 + sostituzione del vincolo UNIQUE
   (23/09/2026)
2. [x] `db.py`: `insert_forecast(lead_hours=1)` con `on_conflict` a tre colonne
   (commit `81f0c832`, 23/09/2026)
   - [ ] resta: versione batch (una chiamata REST per stazione, non 48)
3. [ ] `DROP CONSTRAINT forecasts_station_valid_unique` (il ponte) — **sospeso** insieme al
   punto 6
4. [x] `model/inference.py` multi-lead (commit `56137d31`, 24/09/2026): `predict_series()` /
   `predict_from_df()` a N righe con riga NWP a V−1h, flag `--horizon` / `--leads` /
   `--max-lead`, `--db-leads`, `--nwp-model`, `--json-out`; `forecast_days=4`; bias ARSIAL sul
   mese di `valid_for`; `nwp_temperature`/`nwp_humidity` salvati. Lead 1 identico al
   centesimo alla versione precedente su 32 stazioni
5. [x] `scripts/export_static.py`: `.eq("lead_hours", 1)` in `fetch_latest_forecasts()` e
   `fetch_dashboard_series()` — stesso commit del punto 4
6. [ ] `docs/data/forecast_48h.json` + workflow `inference-48h.yml` — **sospeso (24/09/2026)**:
   il backtest mostra che il modello attuale peggiora l'IFS grezzo, quindi le sue 48 ore non si
   pubblicano. Si riprende con il modello che esce dalla *Prova in ombra M2*
7. [ ] Curva MAE per lead in produzione: estendere `forecast_vs_observed` con `lead_hours` e
   renderla interrogabile per finestre (oggi un `count(*)` sull'intera view va in timeout).
   Per la prova in ombra la curva c'è già in `shadow_vs_observed`
8. [x] ~~Verificare su Historical Forecast API variabili e profondità d'archivio~~ —
   risolto il 23/09/2026 con esito diverso dal previsto: vedi *Preparazione al
   retraining*, decisione A

#### 🎯 Preparazione al retraining

Tutto ciò che va pronto **prima** di dicembre perché il retraining sia un'esecuzione e
non un cantiere. Il retraining (Fase 5) consuma questi risultati; qui si costruiscono.
L'ordine conta: la decisione A condiziona R2–R4.

**La scoperta del 23/09/2026 — la fonte prevista non esiste nella forma prevista.**
Le versioni precedenti del piano indicavano la *Historical Forecast API* di Open-Meteo
come archivio di "cosa il modello NWP prevedeva per quell'ora con quel lead time". Non
lo è. Dalla documentazione: *"Each run's first few hours are stitched into a continuous
hourly timeseries"*. Cuce le prime ore di ogni run: è una quasi-analisi a lead ≈ 0,
concettualmente una seconda ERA5. Ottima per le variabili (CAPE, lifted index, CIN,
shortwave, cloud cover su 4 livelli, pressione), inutile per imparare come cresce
l'errore NWP col lead.

Le fonti che conservano davvero il lead time:

| Fonte | Storico | Lead | Pro | Contro |
|:--|:--|:--|:--|:--|
| Open-Meteo **Single Runs API**, GFS/ICON | da apr 2026 (~8 mesi a dicembre) | orario esatto, parametro `&run=` | REST/JSON, stesso fornitore dell'inference | nessun ciclo stagionale completo, niente inverno |
| Open-Meteo **Single Runs API**, ECMWF IFS HRES 9 km | da 14 mar 2024 | orario, orizzonte 15 giorni | archivio vero, modello di qualità | obbliga ad allineare `&models=` anche in inference |
| Open-Meteo **Previous Runs API** | da gen 2024 (GFS da mar 2021) | solo step di 24h (`_previous_day1`…`7`) | profondità discreta | granularità insufficiente per lead 1–23h |
| **NCAR/UCAR RDA** — GFS 0.25° | dal 15 gen 2015 | tutti gli step, 3h da 0 a 240h | 11 anni, gratis, nessuna registrazione, CC-BY 4.0 | GRIB2 da scaricare e interpolare sul punto; aggiornamento dell'archivio fermato nel 2026 |
| **Google Earth Engine** — `NOAA/GFS0P25` | dal 1 lug 2015 | 1h fino a 120h, poi 3h; ogni immagine ha `creation_time` + `forecast_hours` | 11 anni, lead orario, query per punto via API | account Earth Engine (gratis per uso non commerciale), nuova dipendenza |

Scartate: DWD opendata (ICON) e NOMADS tengono solo una finestra rotante di run
recenti; il bucket AWS `noaa-gfs-bdp-pds` è gratuito ma la profondità non è verificata.
Strumento utile se si sceglie la via GRIB2: pacchetto Python `herbie-data`.

**Target storico Netatmo — risolto (25/09/2026).** La Decisione A era bloccata dal timore
che il target osservato (tabella `observations`, da giugno 2026) rendesse inutile uno storico
NWP profondo. Un primo test di fattibilità (24/09, un device per stazione trovato per
prossimità) aveva trovato storico pluriennale su 29/31 stazioni via Netatmo `getmeasure`.
Il backfill vero, con gli stessi device che la produzione aggrega, è descritto in *Retraining
sul target Netatmo*: 105 device, storico orario dal 14/03/2024 (inizio dell'archivio IFS) per
tutte le 32 stazioni. `getmeasure` fornisce `temperature` e `humidity`; nessun device del
campione ha vento o pioggia.

**Decisione vento — asimmetria accettata (24/09/2026).** Nessuna fonte esterna testata
copre il vento storico. ARSIAL (`data/arsial_roma_2023_2025.parquet`) conferma solo
`temp_min/med/max`, `humidity_med`, `precip_mm` — **niente vento** (ma `precip_mm` è un
bonus non pianificato, utile per un futuro target pioggia, Fase 7). Due reti regionali
restano candidate non sfruttate, non bloccanti: ARPA Lazio (rete micrometeorologica, 8
stazioni, storico CSV 2013–2024 scaricabile) e il Centro Funzionale Regionale — Protezione
Civile Lazio (232 stazioni, 23 con sensore vento, copertura diretta anche a Cassino/
Filettino/Sigillo/Fiano Romano non verificata; il portale è orientato al realtime,
accesso allo storico non trovato in una prima ricerca). **Decisione:** il training del
vento usa solo le osservazioni dirette del progetto da giugno 2026, con copertura molto
diseguale tra le 32 stazioni (verificato via query `observations`: 13 stazioni >90%
copertura, 6 a ~0%, il resto parziale). Confermato che `mainMETEO.py` (riga ~314) è
corretto — aggrega mediana/media circolare su tutti i device Netatmo pubblici nel raggio
di 5 km che riportano vento, `None` se nessuno lo riporta — la copertura scarsa è un
limite hardware reale (pochi Netatmo privati hanno l'anemometro), non un bug.

**Decisioni (aperte e chiuse):**

- **A — Fonte dei dati di training multi-lead. RISOLTA (24/09/2026): ECMWF IFS HRES 9 km via
  Open-Meteo.** Single Runs API per i run archiviati (backtest), Historical Forecast API per la
  serie cucita a lead ≈ 0 (training di M2). Il target storico c'è per tutte le stazioni: vedi
  *Retraining sul target Netatmo*.
- **A-bis — Stesso modello NWP in training e in inference.** Parzialmente risolta: M2 è
  addestrato su IFS e la prova in ombra lo alimenta con IFS (`models=ecmwf_ifs`). La produzione
  (`inference.yml`) usa ancora il blend di default di Open-Meteo; si chiude con la decisione su M2.
- **B — Salvare il valore NWP grezzo in `forecasts`. RISOLTA (24/09/2026):** colonne
  `nwp_temperature`/`nwp_humidity`, scritte da `inference.py` (commit `56137d31`).
- **C — Granularità dei lead. RISOLTA:** IFS da Open-Meteo è orario (Single Runs e Historical
  Forecast), nessuna interpolazione.
- **D — Colonne storiche di `model_metrics`** (`mae_temperature`, `period_start/end`,
  `n_samples`...): mai usate. Lasciarle, rimuoverle, o usarle per snapshot periodici
  del MAE operativo per lead (sovrapposto al punto 7 del piano operativo).

**Operazioni:**

1. [x] **R1 — Decisione A** (24/09/2026: ECMWF IFS HRES)
2. [x] **R2 — Prototipo di estrazione** — superato dal backtest: 178 run × 32 stazioni dalla
   Single Runs API, con cache locale (`scripts/backtest_ifs_lead.py`)
3. [~] **R3 — Tabella di training:** fatta per il contratto lead 1 (M2: 635k righe, input IFS
   cucito, lag calcolati sulla catena NWP e mai su osservato — nessun leakage). Applicato a
   tutti i lead come `predict_from_df`, M2 perde solo ~0,1 °C tra lead 1 e 48. La tabella su
   run archiviati per ogni lead resta un'opzione, da valutare con la curva per lead
   dell'ombra: il guadagno atteso è piccolo e il costo alto (giorni di chiamate Single Runs)
4. [x] **R4 — Stima dimensione:** M2 si addestra sul Mac in circa un minuto (635k righe × 70
   colonne). Una tabella per lead su run archiviati sarebbe ~×48: solo se R3 lo richiede
5. [ ] **R5 — Feature per orizzonti lunghi:** l'esperimento T+24h mostra che il feature
   set T+1h porta il modello a reggersi su persistenza e stagionalità (147 alberi contro
   643). Rilevante solo se si passa al training per lead (R3)
6. [ ] **R6 — Variabili convettive in input:** CAPE, shortwave, copertura nuvolosa
   multi-livello, eventualmente 500 hPa. Presupposto del target pioggia sì/no (Fase 7)
7. [ ] **R7 — Nuovi CSV ARSIAL 2026:** download manuale (CIE/SPID), rieseguire
   `arsial_bias_correction.py`. Serve solo al modello attuale: M2 non usa la correzione ARSIAL
8. [ ] **R8 — Protocollo di validazione del correttore RF per orizzonte:** a T+24h
   peggiorava il val MAE (1.6123 → 1.6331). M2 non ha correttore RF; resta aperto solo per il
   modello attuale
9. [ ] **R9 — Verifica `model_metrics`:** un training con insert attivo, controllare
   che la riga arrivi. Esclude che oltre allo schema ci fosse anche un problema di
   RLS o credenziali. Senza questo, il confronto pre/post di dicembre torna a mano

Non richiede azioni: le osservazioni Netatmo live si accumulano da sole ogni 30 minuti; lo
storico orario si estende con `scripts/netatmo_backfill.py download --selected` prima di ogni
riaddestramento.

#### 🔬 Backtest IFS e MOS minimo — 24/09/2026

**Setup.** 178 run ECMWF IFS HRES (00Z e 12Z, Single Runs API) dal 25/06 al 21/09/2026, 32 stazioni,
lead 1–48 h, circa 266.000 previsioni (emissione = inizio run + 9 h, il ritardo reale di
disponibilità). Target: osservazioni Netatmo, abbinate come in produzione (la più vicina entro
60 min). Il modello di produzione, addestrato prima del 6 giugno, è interamente out-of-sample.
Script `scripts/backtest_ifs_lead.py` e `scripts/experiment_bias_hour.py` (committati); output in
`logs/backtest/` (git-ignored).

**Risultato 1 — il modello attuale peggiora l'IFS grezzo, a ogni lead.** MAE (°C) contro Netatmo:

| Lead | Modello su IFS | IFS grezzo | Produzione (input default) |
|:--|:--|:--|:--|
| 1 | 2.34 | 1.96 | 2.27 |
| 6 | 3.08 | 2.33 | — |
| 24 | 2.47 | 2.11 | — |
| 48 | 2.48 | 2.10 | — |

- La causa non è l'input: il modello su IFS fa 2.34 contro 2.27 della produzione a lead 1.
- Al lead 1 il modello batte l'IFS solo in 6 stazioni su 32: Tivoli, Selva Nera, Cisterna Latina,
  Rieti, Filettino, Rocca Sinibalda. Nelle altre peggiora, di più su costa e pianura.
- L'errore non cresce col lead: tra i lead 12, 24, 36 e 48 il MAE del modello è 2.50, 2.47, 2.50,
  2.48 e quello dell'IFS 2.13, 2.11, 2.11, 2.10. Con due soli run al giorno ogni lead cade su due
  sole ore del giorno (i lead k e k+12 coincidono): il MAE per lead riflette l'ora, e il confronto
  tra lead è pulito solo tra multipli di 12.

**Risultato 2 — l'errore è un bias che dipende dall'ora.** Contro Netatmo il bias è vicino a zero
al mattino (06–09 UTC) e diventa negativo dal pomeriggio alla notte (12–00 UTC): circa −1.8/−3.5 °C
per il modello e −0.8/−2.6 °C per l'IFS grezzo, quindi il modello aggiunge circa 1 °C di freddo.
Stabile da giugno a settembre (tabella per ora UTC dal backtest).

**Diagnosi — è un problema di riferimento (target), non di input.** Roma Sud è l'unica stazione con
sia METAR (LIRF) sia Netatmo. Sugli ultimi 60 giorni, con le stesse previsioni a lead 1 (query su
`forecasts` e `observations`):

| Previsioni a lead 1 confrontate con | Bias | MAE | Coppie |
|:--|:--|:--|:--|
| solo METAR | −0.60 °C | 1.10 | 1.416 |
| solo Netatmo | −2.17 °C | 2.86 | 1.426 |

Confronto diretto tra le due reti: Netatmo − METAR = +1.60 °C di media e +2.00 °C di mediana
(dev. std 2.10) su 3.540 coppie. Il modello è stato addestrato su METAR ed ERA5 (stazioni
aeroportuali/aperte); le Netatmo sono stazioni domestiche, per lo più urbane, che di sera e di
notte trattengono calore. Non si separa quanto sia deriva dei sensori (sole, muri) e quanto
differenza reale tra aeroporto e cluster entro 5 km. Verificato solo su Roma Sud.

**MOS minimo — esperimento.** Bias stimati su 25/06–31/08, test su settembre (mai usato per
stimarli). MAE medio su tutti i lead:

| Variante | MAE (°C) |
|:--|:--|
| A — IFS grezzo | 2.16 |
| B — modello attuale su IFS | 2.52 |
| C — IFS meno bias di stazione | 1.51 |
| D — IFS meno bias stazione × ora UTC (stimato una volta su giu–ago) | 1.04 |
| E — come D, con bias sui 30 giorni precedenti l'emissione (calcolabile in produzione) | 0.96 |
| F — modello attuale meno bias stazione × ora UTC | 1.13 |

- D ed E sono piatte col lead: 0.96–1.18 e 0.89–1.05 da lead 1 a 48. E è la migliore in 20 stazioni su
  32. Nessuna perdita di informazione nello script: E usa solo osservazioni con `valid_for`
  precedente all'emissione, D stima su un periodo che non contiene il test.
- Anche dopo la correzione del bias il modello (F, 1.13) resta peggio dell'IFS corretto (D, 1.04):
  la correzione appresa non aggiunge valore oltre a una tabella di bias. F è la migliore solo in
  4 stazioni interne (Selva Nera, Viterbo, Cassino, Rieti).
- Limiti: un solo mese di test (settembre, in prevalenza tempo stabile); il bias estivo non è detto
  che valga in inverno (la variante E si adatta); il risultato è una calibrazione sulla rete
  Netatmo, non sulla temperatura "vera".

**Conseguenze (aggiornate al 25/09/2026).**

- Deciso: il `forecast_48h.json` del modello attuale non si pubblica (24/09/2026).
- Il riferimento da battere diventa l'opzione E (IFS meno bias mobile), non l'IFS grezzo: un
  modello appreso deve dimostrare valore oltre a una tabella di bias. Raggiunto da M2 (sezione
  seguente).
- Target: si resta sulla mediana Netatmo, con gli stessi device che usa la produzione — è ciò
  che mostra la mappa. Allineamento al METAR scartato: coprirebbe una sola stazione.
- L'ombra dell'opzione E dentro `inference.py` (colonna `temperature_bc`, `inference.yml` su
  IFS) è sostituita da una prova in ombra separata, che non tocca la produzione (25/09/2026).
- Fase 4a punto 6 (`inference-48h.yml`) e rimozione del ponte restano sospesi fino alla
  decisione su M2.

#### 🧬 Retraining sul target Netatmo — 25/09/2026

Il backtest dice che il problema è il target di addestramento (METAR/ERA5), non l'input. Qui si
ricostruisce lo storico del target che la mappa mostra davvero e si riaddestra su quello.

**1. Storico orario del target** — `scripts/netatmo_backfill.py` (commit `6792022c`).

- Passi: `discover` (device candidati da `getpublicdata`) → `download` (`getmeasure` orario,
  paginato, un parquet per device) → `select` → `build` (mediana oraria per stazione, stesso
  `min_cluster` della produzione) → `validate` (confronto con le osservazioni live).
- **`select` — scegliere i device della produzione, non tutti quelli entro 5 km.**
  `getpublicdata` su un'area grande restituisce solo una parte dei device, e la mappa fa la
  mediana di quel sottoinsieme (Trastevere: 3 device in produzione, 139 candidati). Ogni
  osservazione live salva i valori dei device usati (`raw_source.temps_raw`) e quanti erano
  (`n_stations`): per ogni stazione si tengono gli N device che coincidono più spesso con
  quei valori (entro 0,25 °C), N = mediana di `n_stations`. Con tutti i candidati il MAE
  mediano contro le osservazioni live era 0,61 °C; con la selezione 0,38.
- Risultato: **105 device, 657.532 righe orarie, 32 stazioni, 14/03/2024 → 25/09/2026**
  (`data/netatmo_hourly_target.parquet`, git-ignored; device in
  `logs/netatmo_backfill/devices_selected.json`). Circa 2.400 chiamate `getmeasure`,
  ~5 ore al limite di 500 chiamate/ora.
- **Validazione** contro `observations` (10/06–25/09/2026, stesse ore): **25/32 stazioni
  entro 0,5 °C, MAE mediano 0,38 °C, bias mediano −0,03 °C, nessuno sfasamento orario**
  (lag migliore +0 ovunque). Fuori soglia: Pratica di Mare 0,76, Civitavecchia 0,70, Castelli
  Romani 0,65, Ceccano 0,65, Rieti 0,60, Cassino 0,56, Gaeta 0,53 (la produzione usa 4 device,
  se ne trovano 3). Lo scarto residuo di 0,1–0,4 °C non è recuperabile: il backfill è una
  media oraria, la produzione legge valori istantanei fino a 90 minuti vecchi.
- **Sigillo (id 57) esclusa** dal training e dalla valutazione: un solo device e poche
  osservazioni live.

**2. Modelli** — `scripts/experiment_retrain_netatmo.py` (commit `d0519229`). Input: ECMWF IFS
cucito dalla Historical Forecast API; stesso contratto della produzione (riga NWP a X →
temperatura a X+1h); split temporale, validazione solo per l'early stopping.

- **M1** — feature di produzione + stazione (categorica), target = temperatura.
- **M2** — M1 + IFS a `valid_for` + bias IFS−Netatmo per stazione × ora UTC sui 30 giorni
  precedenti (l'opzione E come feature), target = residuo osservato − IFS. La previsione è
  IFS + residuo.

**3. Risultati a lead 1** (run IFS archiviati del backtest, emissioni 09Z/21Z, verità =
osservazioni live), MAE °C:

| Variante | Train fino a lug 2026 (val. agosto), test settembre | Train fino a ott 2025 (val. novembre), test 25/06–21/09/2026 |
|:--|:--|:--|
| IFS grezzo | 1.92 | 1.96 |
| E (IFS − bias 30 gg) | 1.01 | 1.07 |
| Modello attuale | 2.20 | 2.34 |
| M1 | 0.85 | 1.03 |
| **M2** | **0.80** | **0.93** |

La seconda colonna è la prova severa: nessun dato 2026 in training. Sull'inverno 2025–26
(dicembre–febbraio, input cucito, verità = storico ricostruito) M2 fa 0,79 contro 1,04 di E.
Qui E usa il bias calcolato dallo storico ricostruito; nell'esperimento del backtest (0,96)
lo calcolava dalle osservazioni live.

**4. Da lead 1 a 48** — `scripts/experiment_retrain_multilead.py` (commit `00e931af`). I
modelli lead 1 applicati a ogni lead come `predict_from_df` (riga a V−1h dell'input visto
all'emissione); bias preso dal giorno di emissione, nessun dato successivo. 178 run, ~263.000
previsioni, 31 stazioni, train fino a ott 2025:

| Lead (h) | IFS grezzo | E | Modello attuale | M1 | **M2** |
|:--|:--|:--|:--|:--|:--|
| 1–6 | 2.07 | 1.07 | 2.69 | 1.05 | **0.93** |
| 7–12 | 2.17 | 1.11 | 2.64 | 1.05 | **0.95** |
| 13–24 | 2.14 | 1.10 | 2.68 | 1.06 | **0.95** |
| 25–36 | 2.13 | 1.12 | 2.67 | 1.08 | **0.97** |
| 37–48 | 2.15 | 1.14 | 2.70 | 1.11 | **1.00** |
| **1–48** | 2.13 | 1.11 | 2.68 | 1.08 | **0.97** |

- M2 migliore in 19 stazioni su 31, M1 in 11, E in 1. Con il modello addestrato fino a luglio
  (validazione agosto), test settembre: M2 0,88 su 1–48 h (0,80 → 0,93).
- L'errore cresce di ~0,1 °C in 48 ore: buona parte dell'errore IFS era sistematico e M2 lo
  toglie a ogni lead. Il dente di sega del modello attuale (lead 6, 18, 30, 42 = 15 e 03 UTC,
  le ore del bias freddo serale) sparisce.
- Bias calcolato dalle osservazioni live invece che dallo storico (come potrà fare la
  produzione): M2 0,943 invece di 0,967 e 0,847 invece di 0,879 — nessun peggioramento
  (commit `4c4ad571`).

**5. Modello congelato per la prova** — `scripts/train_m2.py` (commit `dbd8b2eb`): M2 su tutti i
dati (14/03/2024 → 22/09/2026, 635.071 righe, 31 stazioni), 2012 iterazioni (il best iteration
del run validato su agosto), seed 42. `model/m2/lgbm_m2_temperature.txt.gz` (4,3 MB compresso)
+ `model/m2/meta.json` (feature, parametri del bias, periodo), tag `m2-20260922`.

**Limiti.** M2 è addestrato sul contratto lead 1 e applicato a tutti i lead; il target è la
mediana della rete Netatmo (sere urbane calde comprese), non la temperatura "vera"; il test
2026 copre un'estate e un inizio d'autunno prevalentemente stabili — da qui la prova in ombra.

#### 🌗 Prova in ombra M2 — 25/09 → 16/10/2026

**Obiettivo.** Verificare sul campo, per tre settimane d'autunno, che i numeri del backtest
reggano, prima di decidere qualsiasi sostituzione. Brief di riferimento (locale, non
committato): `brief_fase4a_m2_shadow.md`.

**Cosa gira** (commit `f03f1dec`, `0bf8cb86`):

| Workflow | Trigger (cron-job.org, UTC) | Script | Scrive |
|:--|:--|:--|:--|
| `bias-table.yml` | ogni giorno 05:00 | `scripts/update_bias_table.py` | `bias_table` — bias IFS − Netatmo per stazione × ora UTC, 30 giorni precedenti |
| `shadow-m2.yml` | ogni giorno 09:15 e 21:15 | `model/shadow_m2.py` | `forecasts_shadow` — 31 stazioni × lead 1–48 per emissione |

Ogni emissione calcola, con lo stesso input IFS (Forecast API, `models=ecmwf_ifs`):

| Colonna | Variante | Atteso dal backtest (MAE 1–48 h) |
|:--|:--|:--|
| `t_ifs` | IFS grezzo a `valid_for` | 2.13 |
| `t_e` | opzione E: `t_ifs − bias` (ripiego `station_bias`) | 1.10 |
| `t_v1` | modello attuale (LGBM + RF + ARSIAL) su input IFS | 2.68 |
| `t_m2` | M2 congelato (`m2-20260922`) | 0.94 |

**Garanzie.** Nessuna modifica a `inference.yml`, `model/inference.py`, `forecasts`,
`export_static.py`, `docs/`; il job ombra non chiama Netatmo (un refresh del token
invaliderebbe quello dell'ingestion). Tabelle con RLS attiva, nessun accesso `anon`/
`authenticated`. Volume: 2.976 righe/giorno, ~90.000/mese.

**Verifiche fatte prima dell'avvio (25/09):**

- `bias_table` ricalcolata al 20/07 coincide con quella dell'esperimento (743 celle, |Δ| max
  0,0005 °C); primo calcolo: 744 righe, bias medio da +0,5 °C alle 08–09 UTC a −2,5 °C alle 00 UTC
- `t_v1` e `t_ifs` a lead 1 identici a `inference.py --horizon 1 --nwp-model ecmwf_ifs
  --dry-run` sulla stessa ora (31 stazioni, Δ max 0,000)
- `t_m2` ricalcolato a mano su 3 stazioni: Δ ≤ 0,004 (arrotondamento)
- Primo run reale: 1.488 righe, nessun NULL, righe di `forecasts` invariate; primi run da
  GitHub Actions via cron-job.org riusciti

**Valutazione** (via SQL su `shadow_vs_observed`, solo emissioni 09Z e 21Z — quelle del 25/09
alle 07Z e 08Z sono di prova e si escludono):

| Data | Giorni | MAE 1–48 h IFS | E | Modello attuale | M2 | Note |
|:--|:--|:--|:--|:--|:--|:--|
| 02/10/2026 | 7 | | | | | |
| 09/10/2026 | 14 | | | | | |
| 16/10/2026 | 21 | | | | | decisione |

Per ogni valutazione: MAE per fascia di lead e per stazione, bias per ora UTC, giorni perturbati
(errore medio IFS > 2,5 °C o pioggia) a parte, confronto con `forecasts` lead 1 (la mappa).

**Criteri per proporre il passaggio in produzione** (la decisione resta a Filippo):

- M2 su 1–48 h ≤ 1,15 °C e sotto E di almeno 0,05 °C
- nessuna stazione con M2 peggiore dell'IFS grezzo di oltre 0,3 °C
- bias medio di M2 per ora UTC entro ±0,5 °C
- nei giorni perturbati M2 non peggiore di E

Se un criterio fallisce si riporta, senza ritoccare il modello durante la prova.

**Se M2 passa:** M2 in `inference.py` al posto del modello attuale (input IFS, chiude A-bis);
poi rimozione del ponte e ripresa del punto 6 (48 ore sulla mappa, Fase 4b).
**Rollback:** disattivare i job su cron-job.org; nient'altro dipende dall'ombra.

### 🧪 Esperimento — TimesFM-3 (zero-shot) vs MOS attuale — Roma Sud, T+1h e T+24h

**Data:** 01/09/2026
**Setup:** confronto isolato (script `benchmark_timesfm3_vs_mos*.py`, non in produzione) su Roma Sud (id=3), usando TimesFM-3 (Google, 330M par., licenza non-commerciale) zero-shot con la serie osservata reale come contesto e la temperatura ERA5 come covariata nota ("past-future covariate").

**Risultati MAE (°C):**

| Orizzonte | Split | LGBM solo | LGBM + RF | MOS scelto (baseline) | TimesFM-3 zero-shot |
|:-------|:-------|:-------|:-------|:-------|:-------|
| T+1h | full val (16.974 righe) | — | 0.8447 | 0.8447 | — |
| T+1h | sottocampione 300 pt | — | — | 0.8823 | 1.1689 |
| T+24h | full val (16.903 righe) | 1.6123 (train 1.3136) | 1.6331 (train 1.2446) | **1.6123 (LGBM solo)** | — |
| T+24h | sottocampione 300 pt | — | — | 1.6420 | 2.0197 |

Gap relativo TimesFM-3 vs MOS: **+32% a T+1h, +23% a T+24h** (si restringe con l'orizzonte, ma il MOS resta avanti in entrambi i casi).

**Cosa abbiamo imparato:**

1. **Il MOS vince su entrambi gli orizzonti testati.** TimesFM-3 parcheggiato, non scartato: da
   rivalutare solo come secondo parere in un ensemble, non come sostituto.
2. **Il correttore RF va validato per orizzonte, non applicato per default.** A T+24h peggiora
   il val MAE (1.6123 → 1.6331) pur migliorando il train (1.3136 → 1.2446): overfitting sui
   residui, meno strutturati a 24h che a 1h.
3. **Le feature T+1h non bastano per orizzonti lunghi:** il modello T+24h si regge su
   persistenza (`temperature`, `temperature_lag_1`) e stagionalità (`doy_sin/cos`) e si ferma
   a 147 alberi contro 643.

Le implicazioni sono in *Preparazione al retraining* (R3, R5, R8). Script di benchmark
`benchmark_timesfm3_vs_mos.py` (non in produzione); gli artefatti T+24h non sono conservati e si
rigenerano dal codice.

### 🟦 Fase 4b — Dashboard GitHub Pages (parallela)

Assorbe il task Chart.js già pianificato in Fase 3 ed estende la dashboard con il meteogramma orario 48h per stazione. Zero infrastruttura nuova: `export_static.py` produce `forecast_48h.json`, la pagina GitHub Pages lo rende con Chart.js. Coerente col vincolo costo-zero.

**Stato:** la dashboard Chart.js di base è già stata completata a giugno 2026 (`dashboard_data.json`, `dashboard.html`, auto-update via `export-dashboard.yml` — vedi Fase 3 in *Stato attuale*). Resta da fare solo il meteogramma 48h: dipende dalla Fase 4a e dal modello che esce dalla *Prova in ombra M2* (le 48 ore del modello attuale non si pubblicano).

1. [x] `dashboard_data.json` (forecast_vs_observed, MAE per stazione, ultime osservazioni) — completato in Fase 3
2. [ ] `forecast_48h.json` per stazione (meteogramma) — dipende dalla Fase 4a (punto 6) e dalla decisione su M2 del 16/10/2026
3. [x] `dashboard.html` con Chart.js: tabelle, "Previsto vs Osservato" — completato in Fase 3; resta da aggiungere il meteogramma 48h
4. [x] Auto-update via workflow dedicato (`export-dashboard.yml`) — completato in Fase 3

### 🟪 Fase 5 — Retraining "grande" (Dicembre 2026)

Un unico retraining che incorpora simultaneamente tutto ciò che è maturato. Nota stagionale: l'estate è la stagione convettiva — i temporali di luglio–settembre 2026 sono dati preziosi da non perdere.

Qui c'è solo l'**esecuzione**. Tutto il lavoro preparatorio sta in **Fase 4a → Preparazione
al retraining**. Punto di partenza cambiato a settembre: la ricetta esiste già (M2, target
Netatmo storico, input IFS) ed è in prova in ombra; dicembre la estende invece di inventarla.

1. [ ] Estendere lo storico target fino a novembre (`scripts/netatmo_backfill.py download
   --selected`, rieseguire `select` sulle osservazioni più recenti) e riaddestrare M2 con
   `scripts/train_m2.py`: il primo autunno completo entra nel training
2. [ ] Decidere tra contratto lead 1 applicato a tutti i lead (come oggi) e training per lead su
   run archiviati (R3), in base alla curva MAE per lead della prova in ombra
3. [ ] Umidità con lo stesso approccio (IFS + residuo, target Netatmo: già nello storico
   ricostruito); vento escluso, lo storico Netatmo non lo ha
4. [ ] Primo target di classificazione: pioggia sì/no orario (vedi Fase 7 per la metodologia)
5. [ ] Confronto pre/post tracciato in `model_metrics` (R9): curva MAE vs lead contro il modello
   in produzione, l'IFS grezzo e l'opzione E
6. [ ] Se il modello attuale resta in produzione per qualche target: correttore RF secondo il
   protocollo per orizzonte (R8) e CSV ARSIAL 2026 (R7). M2 non usa né RF né ARSIAL

### 🟫 Fase 6 — Generalizzazione multi-località (post-retraining)

Per ultima, deliberatamente: parametrizzare una metodologia ancora in evoluzione significa rifattorizzare due volte. Tre livelli:

**Livello 1 — Configurazione esplicita.** Censire tutte le costanti Roma-specifiche sparse nel codice (ID stazioni hardcoded, `ARSIAL_PROXY`, `dist_center_km` da Piazza Venezia, `bearing_sea` costa laziale, bbox Netatmo, `STATION_TYPES`) e spostarle in un unico `config.yaml` di deployment: nome località, bbox, centroide urbano, lista stazioni, fonti dati attive/disattive (ARSIAL diventa un plugin opzionale, non un componente strutturale). Il codice diventa identico per ogni località; cambia solo il config.

**Livello 2 — Orografia automatica da sole coordinate.** Sostituire le assegnazioni manuali (`microclima`, `dist_sea_km`, `bearing_sea`) con fonti calcolabili: altitudine da Open-Meteo Elevation API (già così), distanza/bearing costa da coastline globale (Natural Earth / OSM), microclima da LCZ Copernicus invece che etichetta manuale. `compute_static_orography()` diventa veramente universale.

**Livello 3 — Ciclo di vita del cold start.** Una località nuova non ha 6 mesi di Netatmo. Documentare il ciclo di vita esplicito (è la storia già vissuta con Roma):

- Giorno 0: bootstrap con METAR storico (IEM ASOS è globale, quasi ovunque c'è un aeroporto entro 30–50 km) + ERA5 → modello "stazione standard" subito operativo.
- Mesi 1–6: accumulo Netatmo sulle micro-zone, bias correction climatologica provvisoria (il ruolo che ARSIAL ha avuto per Roma).
- Mese 6+: retraining completo con target iper-locali.

Promessa onesta: non "iper-locale ovunque dal giorno 0", ma "operativo dal giorno 0, iper-locale dopo l'accumulo" — la traiettoria che Roma ha dimostrato fattibile.

1. [ ] `config.yaml` di deployment + refactor delle costanti
2. [ ] Orografia automatica da coordinate (coastline + LCZ Copernicus)
3. [ ] Ciclo di vita cold-start documentato
4. [ ] Comando `bootstrap_location.py --lat --lon` che genera config + bootstrap

### 🟥 Fase 7 — Convettività e target difficili (continuativa, post-storico)

Rispettare la gerarchia di difficoltà: `temperatura < direzione vento ≈ rischio temporali < pioggia puntuale (mm)`.

Feature convettive = incroci con l'orografia. Gli indici convettivi sono a griglia larga: un CAPE di 1500 J/kg sulla cella di Roma non dice dove scoppia il temporale. È l'orografia che modula il triggering (i Castelli innescano convezione che il litorale non vede). Feature interessanti: CAPE × altitudine, CAPE × allineamento vento-rilievo, radiazione × esposizione del versante.

**Gerarchia dei target, in ordine:**

1. [ ] Probabilità di precipitazione (sì/no orario) — classificazione binaria, target giusto per iniziare (pluviometri Netatmo + METAR precip + ARSIAL daily per cross-check). Metriche: Brier score, ROC-AUC (non MAE). Gestire lo sbilanciamento di classe (a Roma piove in una piccola frazione delle ore). → già avviato in Fase 5.
2. [ ] Rischio temporale — classificazione su soglie CAPE+shear con correzione locale appresa. Eventi rari: serve più storico (temporali forti su una zona = decine/anno, non migliaia).
3. [ ] Pioggia puntuale in mm — il più difficile, distribuzione zero-inflated. Approccio a due stadi: classificatore di occorrenza + regressore di quantità addestrato solo sulle ore piovose. Per ultimo, con aspettative calibrate (anche i servizi nazionali la sbagliano alla scala puntuale).

**Soleggiamento — caso speciale.** Gran parte del downscaling della radiazione è geometria, non statistica: pendenza, esposizione, orizzonte topografico si calcolano deterministicamente dal DEM. → modulo geometrico esplicito accanto al ML, non un target appreso.

---

## 🚀 Sviluppo a lungo termine

Lavoro sul frontend/mappa che procede in parallelo alla Roadmap estesa (Fasi 4–7) ma non ne fa parte concettualmente — non tocca pipeline dati, modello o storage.

### 🟦 Radar temporali live (stile Windy, storico 1h)

**Obiettivo:** sezione dedicata sulla mappa con overlay radar precipitazioni in tempo reale, slider temporale su ~1h di storico + nowcast breve, animazione automatica stile Windy.

**Fonte dati:** RainViewer API (gratuita, no key richiesta)
- Endpoint: `https://api.rainviewer.com/public/weather-maps.json`
- Risposta contiene `radar.past` (frame storici, ~2h, ogni 10 min) e `radar.nowcast` (~30 min avanti)
- Ogni frame è un path tile da comporre in URL standard: `https://tilecache.rainviewer.com{frame.path}/256/{z}/{x}/{y}/{color}/1_1.png`
- Nessun costo, nessuna chiamata da GitHub Actions: è client-side puro, il browser scarica i tile direttamente dal provider — zero impatto su Supabase/inference/export esistenti

**Implementazione (frontend, nessuna modifica a `db.py`/`inference.py`/`export_static.py`):**
1. Nuovo file `docs/js/radar.js` (separato da `app.js` per non appesantirlo):
   - `fetchRadarFrames()`: GET a `weather-maps.json`, parsing di `past` + `nowcast`
   - Layer Leaflet: un `L.tileLayer` per frame, sostituito sulla mappa in base al frame selezionato (pattern identico al layer heatmap esistente, non a leaflet-velocity che è vettoriale)
2. UI: sezione "Radar" nel menu layer esistente (accanto a Vento/Temp/Umidità), con:
   - slider temporale sotto la mappa (frame past + nowcast, timestamp leggibile)
   - play/pause per animazione loop automatica (`setInterval`, ~500ms/frame, stile Windy)
   - opacity fissa ragionevole (es. 0.6) per non coprire lo sfondo mappa
3. Refresh: richiamare `fetchRadarFrames()` ogni 10 min (nuovo frame disponibile lato RainViewer) per tenere la sezione aggiornata senza dover ricaricare la pagina

**Limiti da comunicare in UI (onestà del prodotto, come già fatto per altre feature):**
- Risoluzione radar aggregata ~1-2 km, non è output del modello proprio — è un dato di osservazione esterno, non previsione MOS-corretta
- Copertura Italia buona ma non garantita quanto un radar nazionale dedicato

**Alternative scartate:** radar Protezione Civile Nazionale (no API pubblica stabile, stesso problema di affidabilità già visto con OpenAmbiente offline), embed iframe Windy (non è una sezione propria del prodotto)

- [ ] `docs/js/radar.js` — fetch frame + layer management
- [ ] UI slider/play-pause nella sezione mappa
- [ ] Refresh automatico ogni 10 min
- [ ] Nota limiti in UI/tooltip

### 🎨 Restyling mappa — luglio 2026

Restyling puramente estetico/CSS della mappa live, senza alcuna modifica alla logica di calcolo, ai dati, o alla pipeline di inferenza/export.

**Modifiche applicate:**
- Basemap sostituita da OpenStreetMap chiaro a **CartoDB Dark Matter** (`{s}.basemaps.cartocdn.com/dark_all`), gratuito, nessuna chiave richiesta
- **Pannello controlli unificato** (`#control-panel`): fusi i due pannelli separati precedenti (top-left layer/tempo + bottom-left vento/unità/dashboard) in un solo contenitore con sezione condizionale in base al layer attivo
- **Segmented control a pillole** per Vento/Temperatura/Umidità e per Adesso/+1h, sostituendo i bottoni piatti precedenti
- **Checkbox/radio nativi → switch e pillole stilizzate**: "Mostra vento", "Frecce direzionali" (switch stile iOS), km/h↔nodi (pillole) — gli input reali restano nel DOM (nascosti via CSS), nessuna modifica alla logica degli event listener esistenti
- **Legenda**: contenitore ristilizzato (card scura, bordo sottile, radius 10px, tipografia più leggera) — gradiente, calcolo tick e valori numerici invariati, per non compromettere la precisione del dato mostrato
- **Popup stazioni Leaflet**: wrapper, tip e pulsante di chiusura ristilizzati in tema scuro (default bianco di Leaflet completamente sostituito)
- **Opacità heatmap ridotta del 17%** su tutti e tre i layer (temperatura, umidità, vento) per lasciare più leggibile la basemap sottostante: alpha temperatura 153→127, alpha umidità/vento 179→149 (valori canvas 0-255)

**Deviazione consapevole dal brief iniziale:** il comportamento funzionale dei controlli condizionali (quali toggle sono visibili su quale layer) è stato mantenuto identico a prima del restyling, non riorganizzato come inizialmente ipotizzato — per non introdurre regressioni non richieste.

**Non toccato in questo restyling (noto, rimandato):**
- Pannello ora vive in `<body>` invece che dentro `#map` (fix necessario per evitare conflitti di click con il popup IDW della mappa)
- Bug preesistente non risolto: riferimento a un elemento `#updated-at` mai esistito nel markup, in un blocco `catch` — da investigare separatamente

### 📱 Creazione app — PWA installabile su iPhone (luglio 2026)

Modifica puramente additiva, nessun impatto su chi apre il sito da browser senza installarlo. Target: iPhone iOS precedente alla 26 (17/18), dove l'apertura in standalone da Home Screen non è automatica e va dichiarata esplicitamente via meta tag.

**File aggiunti:**
- `docs/manifest.json` — `start_url`/`scope` relativi (`./`) per il sottopercorso GitHub Pages `/meteo_locale/`; `display: "standalone"`; icone 192/512
- `docs/icons/` — `icon-192.png`, `icon-512.png`, `apple-touch-icon.png` (180×180, no alpha)
- `docs/sw.js` — service worker **nella root di `docs/`** (non in sottocartelle, altrimenti lo scope si restringe e non intercetta le richieste della pagina)

**Meta tag aggiunti in `docs/index.html`** (`<head>`, nessun'altra modifica): `apple-mobile-web-app-capable`, `apple-mobile-web-app-status-bar-style`, `apple-mobile-web-app-title`, `apple-touch-icon`, `theme-color`, `viewport-fit=cover` aggiunto al viewport esistente.

**Registrazione SW:** poche righe in coda a `init()` in `docs/js/app.js`, path relativo `./sw.js`, `.catch()` silenzioso — se la registrazione fallisce il sito funziona identico a prima.

**Strategia cache (cuore del service worker):**
- **Cache-first** — asset statici (`index.html`, `dashboard.html`, `app.js`, `radar.js`, manifest, icone)
- **Network-first con fallback su cache** — `data/latest.json`, `data/wind_grid.json`, `data/dashboard_data.json`: si tenta sempre la rete per primo, la cache serve solo da fallback offline. Servirli da cache come prima scelta mostrerebbe previsioni vecchie spacciate per attuali — inaccettabile per un prodotto meteo
- **Nessuna intercettazione** — tile RainViewer, `weather-maps.json`, basemap CartoDB: dominio esterno, esclusi esplicitamente (`url.origin !== self.location.origin`). Ogni frame radar ha un URL con timestamp diverso: cacharli farebbe crescere la cache senza limite, rischio eviction su iOS dove le quote storage sono più strette

**⚠️ Cache-busting:** `CACHE_VERSION` in cima a `docs/sw.js` va **incrementata ad ogni deploy che tocca HTML/CSS/JS**, altrimenti le modifiche non compaiono sui dispositivi già installati (restano serviti gli asset vecchi da cache-first). Sono previste iterazioni estetiche frequenti — attenzione a non dimenticarlo. Automazione valutata e scartata per ora: richiederebbe toccare i workflow GitHub Actions, fuori dal vincolo additivo di questa fase.

**Limite noto (non risolto, di natura simile alla pausa Supabase dopo inattività prolungata):** iOS può svuotare la cache di una PWA rimasta inutilizzata a lungo. Impatto minimo qui — la strategia network-first sui dati significa che al riavvio si scaricano comunque dati freschi; nel peggiore dei casi si perde solo il fallback offline.

**Fuori scopo (deciso, non da implementare):** push notification (inaffidabili su iOS, non richieste da questo caso d'uso), background sync (non disponibile su iOS), app nativa/App Store (richiede account developer a pagamento), prompt di installazione automatico (non esiste su iOS — gesto manuale Safari → Condividi → Aggiungi a Home).

---

## 📌 Come riprendere il lavoro

```bash
cd ~/Desktop/meteo_locale
conda activate meteo
python3 db.py   # verifica connessione
```

**Riferimento GitHub:** `https://github.com/filippopetto-maker/meteo_locale`

**Stato corrente (settembre 2026):** Fase 1, 2a, 2b, 3 in produzione (Fase 3 include carta del vento e dashboard Chart.js). Fase 2c parziale (bias correction ARSIAL attiva, Protezione Civile Lazio ancora da integrare). **Fase 4a in corso:** inference multi-lead pronta ma non pubblicata, modello M2 in prova in ombra fino al 16/10/2026. Radar RainViewer in corso (vedi *Sviluppo a lungo termine*). Roadmap strategica di lungo periodo (48h, retraining dicembre, generalizzazione, convettività) → [Roadmap estesa — Fasi 4–7](#-roadmap-estesa--fasi-47). GitHub Actions attivi. Trigger primario per tutti i workflow: cron-job.org (`workflow_dispatch`), l'unico che non ha mai saltato un run. I due workflow a 30 minuti hanno in più uno `schedule:` interno GitHub come rete di sicurezza (inaffidabile da solo su repo a bassa attività, ma innocuo come doppione):
- `inference.yml` — previsioni, ogni 30 min (cron-job.org + `schedule:` interno di riserva)
- `ingestion.yml` — osservazioni METAR + Netatmo, ogni 30 min (cron-job.org + `schedule:` interno di riserva)
- `export.yml` — export griglia statica (`latest.json`, `wind_grid.json`), ogni ora (solo cron-job.org)
- `export-dashboard.yml` — export `dashboard_data.json`, 2×/giorno (8:00, 20:00) (solo cron-job.org)
- `bias-table.yml` — bias IFS − Netatmo per stazione × ora in `bias_table`, ogni giorno 05:00 UTC (solo cron-job.org, prova in ombra)
- `shadow-m2.yml` — previsioni in ombra 1–48 h in `forecasts_shadow`, 09:15 e 21:15 UTC (solo cron-job.org, prova in ombra)

**Mappa live:** `https://filippopetto-maker.github.io/meteo_locale/`

**Prossime scadenze:** valutazioni della prova in ombra M2 il **2, 9 e 16 ottobre 2026** (decisione il 16/10, vedi Fase 4a); **dicembre 2026** — retraining completo (Fase 5).

**Completato (giugno 2026):**
- Correzione SST sul mare: `sst.py` + blend graduale asimmetrico in `grid.py` + `export_static.py`; `LATIUM_COAST` estesa da Anzio→Gaeta a sud e fino a (42.85, 10.85) a nord
- Toggle T / T+1h sulla mappa: `temp_grid_observed` + `temp_grid_forecast` in `latest.json`; scala colori unificata tra i due stati
- 4 nuove stazioni (id 51–54): Fiano Romano, Civitavecchia, Filettino 1044m, Gaeta
- Palette umidità ridisegnata per contrasto reale nel range 40-80% (pivot verde)
- Fix copertura Netatmo: `mainMETEO.py` portato da `ROMA_BBOX` singolo a `LAZIO_BBOXES` (5 sotto-zone con dedup), risolve sia i buchi geografici (Cassino, Filettino) sia il "soffocamento" delle zone dense (EUR, Trastevere) causato dal tetto di risultati per chiamata Netatmo
- `min_cluster` rilassato a 1 per id≥39 e per microclima `quota`/`alta_quota`/`colline_interne`
- Pulizia naming: rinominate stazioni con nomi duplicati/imprecisi (Saxa Rubra id46→Labaro, Gaeta/Formia id49→Fondi); riclassificati microclima (Tivoli/Bracciano/Rieti→`colline_interne`, Filettino→`alta_quota`, isolando `quota` alla sola Castelli Romani)

**Dashboard live:** `https://filippopetto-maker.github.io/meteo_locale/dashboard.html`

**Prossimo task immediato:** seguire la *Prova in ombra M2* (Fase 4a) — valutazioni SQL su
`shadow_vs_observed` alle tre scadenze, poi decisione sul passaggio in produzione. In sospeso
fino ad allora: Fase 4a punto 6 (48 ore sulla mappa) e rimozione del vincolo ponte.

**Miglioramenti futuri mappa:**
- Più stazioni: settore ovest (Bracciano, Ostia Nord) e nord completamente scoperti dall'IDW — ogni nuova stazione migliora il gradiente senza modifiche al codice
- Upgrade a MapLibre GL JS per qualità visiva superiore (vettoriale, tile più dettagliate)
- Upgrade `actions/checkout@v4` → `@v5` e `actions/setup-python@v5` → versione corrente (warning Node.js 20 deprecation)

---

## 🎯 Risultati del modello

Due modelli: quello **di produzione** (v1, addestrato a giugno 2026 su ERA5 → METAR, sulla mappa) e **M2** (IFS + residuo LightGBM su target Netatmo, in prova in ombra dal 25/09/2026). Le metriche di validazione di v1 qui sotto sono contro METAR; contro il target Netatmo che la mappa mostra, v1 fa peggio dell'IFS grezzo (vedi sotto e Fase 4a).

### Modello di produzione (v1) — dataset di training

| Parametro | Valore |
|:----------|:-------|
| Periodo | 2015–2024 (10 anni) |
| Righe totali | ~331.000 |
| Righe di training | ~264.000 (80%) |
| Righe di validazione | ~67.000 (20%) |
| Colonne feature | 76 |
| Stazioni (training) | 4 (Roma Nord, Centro, Sud, Ostia — schema originale) |
| Stazioni (operative) | 32 (schema espanso Lazio, Phase 2c/3) |
| ICAO sorgenti | LIRA (Ciampino), LIRF (Fiumicino) |

*Nota: il modello è stato addestrato sulle 4 stazioni originali; per le altre 28 opera per estrapolazione sui gradienti orografici appresi.*

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
| `model/m2/lgbm_m2_temperature.txt.gz` | 4.3 MB (M2, compresso) |
| **Totale** | **~16 MB** |

### Valutazione operativa contro il target Netatmo (backtest 25/06–21/09/2026)

MAE °C su 1–48 h, run ECMWF IFS archiviati, verità = osservazioni Netatmo live, 31 stazioni.
Dettagli e metodo in Fase 4a (*Backtest IFS e MOS minimo*, *Retraining sul target Netatmo*).

| Modello | MAE 1–48 h | Note |
|:--|:--|:--|
| v1 (produzione) su input IFS | 2.68 | peggiora l'IFS in 26 stazioni su 32 a lead 1; bias freddo serale fino a −3,5 °C |
| IFS grezzo | 2.13 | |
| Opzione E (IFS − bias staz×ora 30 gg) | 1.11 | |
| **M2** (addestrato fino a ott 2025) | **0.97** | 0,93 a lead 1, 1,03 a lead 48; migliore in 19 stazioni su 31 |

L'errore di v1 non dipende dalla quota o dal microclima della stazione come si ipotizzava a giugno:
è un bias di riferimento (METAR/ERA5 contro rete Netatmo urbana), sistematico per ora del giorno.

### Nota architetturale — correzione orografica in quota

La griglia IDW in quota (es. area Simbruini/Ernici) appare meno accurata perché le stazioni `alta_quota` (Filettino, Rocca Sinibalda, Sigillo) sono in cold-start. Non applicare correzioni empiriche di lapse rate sulla griglia — violerebbe il principio ERA5-as-background. La copertura migliorerà con il retraining dicembre 2026 quando LightGBM imparerà la relazione quota→temperatura dai dati accumulati.

### Il ciclo virtuoso

Ogni run di `mainMETEO.py` accumula osservazioni Netatmo reali in `observations` per tutte le 32
zone; `scripts/netatmo_backfill.py` ricostruisce lo stesso target all'indietro fino a marzo 2024:

```
Giugno 2026:   ERA5 (input) + METAR 4 stazioni (target storico) → v1
Settembre:     IFS (input) + Netatmo 31 stazioni, storico 2024→ ricostruito (target)
               → M2, in prova in ombra
Ogni 30 min:   Netatmo accumula ground truth live; bias_table si aggiorna ogni giorno
Dicembre:      M2 riaddestrato con l'autunno + nuovi target (Fase 5)
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
│       ├── inference.yml            # previsioni, cron-job.org + schedule: interno di riserva ✅ ATTIVO
│       ├── ingestion.yml            # osservazioni live, cron-job.org + schedule: interno di riserva ✅ ATTIVO
│       ├── export.yml               # export griglie mappa, trigger esterno ✅ ATTIVO
│       ├── export-dashboard.yml     # export dashboard_data.json, trigger esterno 8:00/20:00 ✅ ATTIVO
│       ├── bias-table.yml           # bias_table IFS − Netatmo, cron-job.org 05:00 UTC ✅ ATTIVO (ombra)
│       └── shadow-m2.yml            # prova in ombra 1–48 h, cron-job.org 09:15/21:15 UTC ✅ ATTIVO (ombra)
│
├── db.py                        # Data Access Layer (connessione Supabase) ✅
├── qc.py                        # Quality Control 4 livelli ✅
├── features.py                  # Feature Engineering 5 strati ✅
├── historical.py                # ERA5 + METAR → parquet training ✅
├── forecast.py                  # Training LightGBM ✅
├── mainMETEO.py                 # Raccolta osservazioni live (METAR + Netatmo) ✅
├── sst.py                       # Sea Surface Temperature (Open-Meteo Marine API), blend costiero asimmetrico 25 km ✅
│
├── model/
│   ├── correttore.py            # RF correttore residui ✅
│   ├── inference.py             # Inference operativa ✅
│   ├── m2.py                    # Feature e previsione di M2 (condivise da esperimenti e ombra) ✅
│   ├── shadow_m2.py             # Job della prova in ombra → forecasts_shadow ✅
│   ├── m2/                      # M2 congelato: lgbm_m2_temperature.txt.gz + meta.json ✅
│   ├── lgbm_temperature.txt     # Modello LightGBM temperatura ✅
│   ├── lgbm_wind_speed.txt      # Modello LightGBM vento ✅
│   ├── lgbm_wind_direction.txt  # Modello LightGBM direzione ✅
│   ├── lgbm_humidity.txt        # Modello LightGBM umidità ✅
│   ├── rf_correttore_temperature.pkl     # RF correttore temperatura ✅
│   ├── rf_correttore_wind_direction.pkl  # RF correttore direzione ✅
│   └── feature_importance_*.json        # Gain per feature (tutti i target)
│
├── data/
│   ├── training.parquet         # Dataset storico (NON nel repo — .gitignore)
│   └── netatmo_hourly_target.parquet  # Target Netatmo orario 2024→ (NON nel repo)
│
├── output/
│   └── dashboard.py             # Streamlit dashboard read-only ✅
│
├── scripts/
│   ├── export_static.py         # export griglie + dashboard (flag --dashboard-only) ✅
│   ├── update_bias_table.py     # bias_table giornaliera (prova in ombra) ✅
│   ├── netatmo_backfill.py      # storico orario del target Netatmo (discover/select/download/build/validate) ✅
│   ├── train_m2.py              # addestra e congela M2 in model/m2/ ✅
│   ├── backtest_ifs_lead.py     # backtest multi-lead su run IFS archiviati ✅
│   └── experiment_*.py          # esperimenti: bias staz×ora, retraining M1/M2, multi-lead ✅
│
├── logs/                        # output di backtest, backfill ed esperimenti, cache Open-Meteo (NON nel repo)
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

Una riga per `(station_id, valid_for, lead_hours)` (più il vincolo ponte `(station_id, valid_for)`,
vedi Fase 4a). Colonne: `forecast_at`, `valid_for`, `lead_hours`, `temperature`, `humidity`,
`wind_speed`, `wind_direction`, `model_version`, `corrected`, `nwp_temperature`, `nwp_humidity`,
`nwp_run_at` (NWP grezzo a `valid_for`), `temperature_bc` / `bc_bias` (create per l'opzione E,
oggi vuote).

### `bias_table` — bias IFS − Netatmo per stazione × ora UTC

PK `(station_id, hour_utc)`. `bias` = media dei valori giornalieri sui 30 giorni precedenti
(NULL sotto 10 giorni), `station_bias` = ripiego per l'opzione E, `n_samples`, `n_station`,
`window_days`, `window_end`, `nwp_model`, `computed_at`. Riscritta ogni giorno da
`scripts/update_bias_table.py`.

### `forecasts_shadow` — prova in ombra

PK `(station_id, issue_at, lead_hours)`; `valid_for`, `nwp_run_at`, `t_ifs`, `t_e`, `t_v1`,
`t_m2`, `bias`, `model_tag`. Scritta da `model/shadow_m2.py`, mai letta dalla mappa.

### `model_metrics` — performance nel tempo

Storico MAE/RMSE per ogni target, n_samples, periodo, `trained_at`, `model_version`.

### Viste

- `latest_observations` — ultima rilevazione valida per stazione
- `forecast_vs_observed` — confronto automatico previsione vs reale con MAE (LATERAL JOIN, tolleranza 3600s)
- `shadow_vs_observed` — `forecasts_shadow` + osservazione Netatmo (QC < 2) più vicina entro ±15 min, con `err_ifs`, `err_e`, `err_v1`, `err_m2` (`security_invoker`)

Tutte le tabelle hanno RLS attiva e nessun accesso `anon`/`authenticated`: il sito statico non legge Supabase, legge i JSON in `docs/data/`.

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

### cron-job.org — trigger dei workflow

Ogni workflow con `workflow_dispatch` si avvia da un job su cron-job.org. Il modo più semplice è
clonare un job esistente e cambiare titolo, URL e orario.

| Campo | Valore |
|:--|:--|
| URL | `https://api.github.com/repos/filippopetto-maker/meteo_locale/actions/workflows/<file>.yml/dispatches` |
| Metodo | `POST` |
| Corpo | `{"ref":"main"}` |
| Header `Authorization` | `Bearer <token>` — Personal Access Token GitHub (classic) con scope `repo` e `workflow` |
| Header `Accept` | `application/vnd.github+json` |
| Fuso orario del job | **UTC** (gli orari dei run IFS sono in UTC; con Europe/Rome si spostano al cambio d'ora) |
| Pianificazione | espressione cron, es. `15 9,21 * * *` = 09:15 e 21:15 ogni giorno |

Il token si legge negli header di un job esistente; GitHub non lo mostra più dopo la creazione (se
perso, generarne uno nuovo e aggiornare tutti i job). Non vanno usate le credenziali Netatmo
(`client id`/`client secret`). "Test run" su cron-job.org avvia davvero il workflow: risposta
attesa **204**, poi il run compare nella scheda Actions.

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
- `get_netatmo_observations(...)`, `upsert_bias_table(...)`, `get_bias_table()`, `upsert_forecasts_shadow(...)` — prova in ombra
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

- Scarica la previsione NWP oraria da Open-Meteo Forecast API (`past_days=2`, `forecast_days=4`;
  blend di default, oppure un modello con `--nwp-model`, es. `ecmwf_ifs`)
- Applica feature engineering (stessi 5 strati del training) e, per ogni lead L, usa la riga NWP
  a `valid_for − 1h` (contratto appreso X → X+1h)
- Carica LightGBM + RF correttori da file, applica il bias ARSIAL sul mese di `valid_for`
- Scrive previsioni T+1h su Supabase (`forecasts`) per tutte le stazioni attive, con
  `nwp_temperature`/`nwp_humidity`. Multi-lead disponibile (`--leads`, `--max-lead`,
  `--json-out`); su DB oltre lead 1 solo con `--allow-multi-lead`
- Supporta `--dry-run` per test senza scrittura DB
- Eseguito automaticamente ogni 30 min da GitHub Actions (`--horizon 1`)

### `model/m2.py` e `model/shadow_m2.py` — M2 e prova in ombra ✅

- `m2.py`: `build_m2_frame()` costruisce le feature di M2 (le stesse di produzione + stazione,
  IFS a `valid_for`, bias staz×ora); la classe `M2` carica `model/m2/` e prevede IFS + residuo.
  Usato sia dagli script di esperimento sia dal job in ombra: training e produzione condividono
  lo stesso codice
- `shadow_m2.py`: un'emissione per run, 31 stazioni × lead 1–48, quattro varianti (`t_ifs`, `t_e`,
  `t_v1`, `t_m2`) → `forecasts_shadow`. Non scrive su `forecasts`, non chiama Netatmo.
  `--dry-run`, `--max-lead`, `--json-out`

### `scripts/netatmo_backfill.py` — Storico del target Netatmo ✅

Ricostruisce la mediana oraria Netatmo per stazione con gli stessi device della produzione:
`discover` → `download` (paginato, riprende da dove era) → `select` → `build` → `validate`.
Limite Netatmo ~500 chiamate/ora (`--pause 7.5`). Un solo processo alla volta: ogni refresh del
token Netatmo invalida quello precedente.

### `scripts/update_bias_table.py`, `scripts/train_m2.py` ✅

- `update_bias_table.py`: bias IFS − Netatmo per stazione × ora sui 30 giorni precedenti, dalle
  osservazioni live e dalla Historical Forecast API; `--dry-run`, `--window-end` per ricalcolare
  una data passata
- `train_m2.py`: addestra M2 su tutto lo storico con iterazioni fisse e lo congela in `model/m2/`

### Script di valutazione ✅

`backtest_ifs_lead.py` (backtest multi-lead su run IFS archiviati, cache in `logs/backtest/`),
`experiment_bias_hour.py` (varianti A–F del MOS minimo), `experiment_retrain_netatmo.py`
(M1/M2 a lead 1), `experiment_retrain_multilead.py` (M1/M2 da lead 1 a 48, `--bias-source`).

### `mainMETEO.py` — Raccolta osservazioni live ✅

Popola la tabella `observations` con dati reali da stazioni fisiche. Ogni run (30 min):

1. **METAR** — IEM ASOS, ultime 2h per LIRA/LIRF, stazione Roma Sud (id=3). Upsert idempotente.
2. **Netatmo** — `fetch_netatmo()`: token OAuth2 refresh → `getpublicdata` bbox Roma → parsing → mediana cluster 5 km → QC → insert per 32 stazioni.

QC a 4 livelli via `qc.run_qc()` — storico ultime 3h da Supabase, neighbors = cluster Netatmo della stazione.
Supporta `--dry-run`. Stub pronto per Phase 2c: `fetch_protezione_civile_lazio()`.

### `output/dashboard.py` — Streamlit ✅

Dashboard read-only. Mostra previsioni correnti, storico temperature, metriche modello, grafico Previsto vs Osservato con MAE per stazione.

### `grid.py` — Griglia spaziale IDW + ERA5 ✅

Due funzioni principali:

- `compute_idw_grid(points, values, ...)` — IDW vettorizzato con numpy broadcasting. Nessun loop Python, istantaneo su 100×100 con 32 stazioni.
- `fetch_era5_batch(lats, lons, target_hour_utc, variables)` — singolo HTTP request batch a Open-Meteo per N punti e M variabili (`temperature_2m`, `relativehumidity_2m`). Ritorna `dict[str, list[float]]`.
- `bilinear_to_fine(coarse, coarse_lats, coarse_lons, fine_lats, fine_lons)` — interpola griglia sparsa ERA5 7×9 a griglia fine 100×100 con `scipy.interpolate.RegularGridInterpolator`.
- `wind_to_uv(speed_ms, direction_deg)` — decomposizione in componenti U/V con convenzione meteo (direction = "da dove arriva").

**Principio architetturale:** la mappa non mostra IDW puro su valori assoluti ma `T_ERA5(x,y) + IDW_correzioni(x,y)`. ERA5 fornisce il campo fisicamente realistico (lapse rate, SST marina, gradiente costa/interno); le 32 stazioni aggiungono la correzione microclima appresa dal modello. Stesso approccio per umidità.

Nuove funzioni aggiunte (giugno 2026): `build_sea_polygon()` — chiude `LATIUM_COAST` in un poligono mare/terra; `is_sea_mask()` — point-in-polygon vettorizzato via `matplotlib.path.Path`; `compute_coast_distance_grid()` — distanza punto-**segmento** (non solo vertice) dalla costa, evita artefatti circolari attorno ai promontori; `compute_sea_blend_weight()` — peso blend SST asimmetrico lato mare (smoothstep su fascia 25 km, `w=0` su tutta la terraferma incluse stazioni costiere).

### `sst.py`

Fetch Sea Surface Temperature da Open-Meteo Marine API (`marine-api.open-meteo.com/v1/marine`, variabile `sea_surface_temperature`). 5 punti offshore lungo la costa laziale (Civitavecchia, Fiumicino, Anzio, Sabaudia, Gaeta). Cache su `data/sst_cache.json` con TTL 4h — committata da `export.yml` così persiste tra run stateless di GitHub Actions. Fallback su cache scaduta se API non disponibile; se nessuna cache, restituisce `None` senza crashare (comportamento legacy).

### `scripts/export_static.py` — Export JSON per GitHub Pages ✅

Eseguito ogni ora da `export.yml`; il ramo `--dashboard-only` è eseguito separatamente 2×/giorno (8:00, 20:00) da `export-dashboard.yml`. Pipeline principale:

1. Legge stazioni attive, forecast recenti e osservazioni da Supabase
2. Fetch ERA5 batch: griglia coarse 17×27 + stazioni attive, 3 variabili (`temperature_2m`, `relativehumidity_2m`, `windspeed_10m`) in un unico request
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
Genera solo `docs/data/dashboard_data.json` (serie storiche 7 giorni + MAE temperatura e umidità) senza le griglie ERA5/IDW. Usato dal workflow dedicato `export-dashboard.yml` per aggiornarsi 2×/giorno (8:00, 20:00).

### `docs/dashboard.html` — Dashboard Chart.js ✅

Pagina statica accessibile da `filippopetto-maker.github.io/meteo_locale/dashboard.html`. Link "📊 Dashboard →" nell'`#info-panel` di `index.html`.

**Sezioni:**
- **Switch Temperatura/Umidità** — aggiorna entrambi i chart in un click
- **Chart Previsto vs Osservato** (Chart.js line, asse X `time` via `chartjs-adapter-date-fns`): serie 7 giorni per la stazione selezionata; blu = previsto, arancio = osservato; filtra automaticamente i punti null (umidità spesso assente nelle osservazioni storiche)
- **Chart MAE per stazione** (Chart.js bar orizzontale): verde se MAE < 1.0°C (temperatura) o < 5.0% (umidità), rosso altrimenti; stazioni senza coppie → barra trasparente "(n/d)"; questo chart non cambia al cambio stazione

**Dati:** `docs/data/dashboard_data.json` — aggiornato 2×/giorno (08:00 e 20:00 UTC) dal workflow dedicato `export-dashboard.yml`.


---

## 🏆 Differenziali competitivi

- **Statistical downscaling ERA5 → stazioni reali** — approccio corretto e sostenibile vs NWP pesante; impara le correzioni che il modello globale sbaglia
- **Rete Netatmo densa** — 340+ stazioni pubbliche nel bbox Roma aggregano il segnale urbano reale ogni 30 min, con QC spaziale integrato su cluster di 5 km
- **Architettura multi-stazione** — 32 stazioni con profili orografici contrastanti (costiera, urbano, quota, pianura) abilitano l'apprendimento dei gradienti territoriali
- **Carta del vento dedicata** — heatmap velocità ERA5-corretta + frecce barbed sinottiche zoom-adaptive con intensità codificata dalle stanghette; toggle automatico particelle/frecce
- **Feature orografiche esplicite** — delta quota vs cella ERA5, onshore alignment, isola di calore, one-hot microclima: il territorio codificato come predittori
- **Modello a due stadi** — LightGBM cattura il segnale principale; RF correttore elimina gli errori sistematici residui per microzona
- **QC climatologico contestuale** — validazione contro climatologia locale per mese e fascia oraria, con offset per tipo di stazione; raro nei tool open source
- **Soglie aggiornate ai cambiamenti climatici** — trend 2015–2024, non medie storiche obsolete
- **Split temporale rigoroso** — nessun leakage tra training e validation; `temporal_split()` condiviso tra `forecast.py` e `correttore.py` garantisce split identico
- **Addestramento immediato sullo storico** — nessuna attesa per accumulare dati live
- **Deploy autonomo a costo zero** — GitHub Actions cron, Supabase free tier, Open-Meteo gratuito, Netatmo pubblico: zero spesa operativa
- **Infrastruttura robusta** — Streamlit dashboard live, metriche su DB, modelli versionati
- **Validazione prima del deploy** — backtest su run NWP archiviati con emissione realistica e prova in ombra in produzione prima di sostituire un modello
- **Mappa iperlocale Windy-style** [Fase 3] — visualizzazione del gradiente microclima
  Roma su carta interattiva: il campo colorato mostra le previsioni corrette dal modello
  (non ERA5 grezzo), le particelle animate mostrano il vento iper-locale. Nessuna app
  mainstream mostra la differenza termica Trastevere/Tivoli su una mappa zoomabile.

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
| `ReferenceError: Cannot access 'stationMarkers' before initialization` | `const stationMarkers` dichiarato dentro `init()` con closure di `switchLayer` che vi accedeva prima dell'esecuzione della riga `const` (Temporal Dead Zone) — le branch temperatura/umidità di `switchLayer` chiamavano `showStations(map, stationMarkers)` prima che la variabile fosse inizializzata | Spostare `let stationMarkers = []` a livello di modulo (fuori da `init()`), assegnare dentro `init()` senza `const`/`let`; `switchLayer` legge così la variabile già popolata |
| `ModuleNotFoundError: No module named 'matplotlib'` | Aggiunto a `requirements.txt` ma non installato nell'ambiente `meteo` locale; il blocco SST in `export_static.py` falliva silenziosamente nel try/except | `pip install matplotlib` nell'ambiente conda `meteo`; aggiunto anche a `pip install` nel workflow |
| Stazioni Tivoli/Filettino/Cassino sempre "osservata: n/d" | Due funzioni `fetch_netatmo()` esistevano in due file diversi (`mainMETEO.py` e `fetch_netatmo_block.py`); solo `mainMETEO.py` è collegata a `ingestion.yml`, l'altra non è mai stata eseguita in produzione nonostante avesse `LAZIO_BBOXES` e la fix `min_cluster` già pronte | Fix applicate sul file giusto (`mainMETEO.py`); `fetch_netatmo_block.py` rinominato `_unused_fetch_netatmo_block.py` per evitare confusione futura |
| `getpublicdata` Netatmo azzera cluster su zone dense (EUR, Trastevere) con bbox esteso a tutto il Lazio | L'API sembra avere un tetto di risultati per chiamata: bbox più ampio non aggiunge stazioni nelle zone dense, le diluisce a favore di copertura geografica più ampia | 5 sotto-bbox (`LAZIO_BBOXES`, margine 0.15° di sovrapposizione) con fetch separato + merge deduplicato su `_id` Netatmo, invece di un singolo bbox per tutto il Lazio |
| IDW usava previsioni LGBM invece di osservazioni Netatmo | Bug logico in export_static.py | Corretto: IDW ora usa dati Netatmo reali per stazioni 33–38 |
| Legenda vento mostrava km/h anche in modalità nodi | `updateLegend()` chiamata con wsMin/wsMax sempre in km/h; il toggle unità aggiornava solo il titolo, non i tick | Nuova `updateWindLegend()` che ricalcola vMin/vMax con fattore di conversione (0.539957) prima di chiamare `updateLegend()` |
| Titolo legenda vento con doppio spazio (`Velocità vento ( km/h)`) | Unità formattata con spazio iniziale nel fix precedente | `unit.trim()` applicato solo alla stringa del titolo |
| `export` job: `! [rejected] main -> main (stale info)` ~7-8x/giorno | `git push --force-with-lease` senza `pull --rebase` prima, race con altri push su main | `git pull --rebase origin main` + retry×3 prima del push |
| `dashboard_data.json` ricalcolato 2 volte per ciclo (dentro `export` e dentro `export-dashboard`) | Blocco dashboard lasciato per errore anche dentro `main()` di `export_static.py`, oltre che nel ramo `--dashboard-only` | Rimosso da `main()`, resta solo nel ramo `--dashboard-only` |
| GitHub Actions `schedule:` interno non affidabile (run saltati/ritardati) | Scheduler nativo GitHub degrada su repo a bassa attività | Trigger primario via cron-job.org (`workflow_dispatch`) su tutti i workflow. `inference.yml` e `ingestion.yml` mantengono anche lo `schedule:` interno come riserva, per scelta: un run doppio è innocuo (upsert idempotenti), uno saltato no |
| `model_metrics` sempre vuota (0 righe) | `db.insert_model_metrics()` scriveva colonne (`target`, `horizon_hours`, `train_mae`, `val_mae`, `n_train`, `best_iteration`...) inesistenti nella tabella, che aveva uno schema diverso mai cablato; il `try/except` non bloccante in `forecast.py` nascondeva l'errore a ogni training | Migrazione `model_metrics_align_to_training_code` (23/09/2026): aggiunte le colonne scritte dal codice. Le vecchie colonne non referenziate restano intatte. Il confronto operativo previsto vs osservato (`forecast_vs_observed`) non era toccato dal bug |
| Insert `forecasts` falliti dopo la migrazione `lead_hours` | L'upsert di `db.py` usava `on_conflict="station_id,valid_for"`, vincolo appena sostituito da `(station_id, valid_for, lead_hours)` → PostgREST rifiuta un `on_conflict` senza vincolo corrispondente | `db.py` portato su `on_conflict="station_id,valid_for,lead_hours"` con `lead_hours=1` di default (commit `81f0c832`). Nel frattempo ripristinato il vecchio vincolo come ponte (`forecasts_bridge_legacy_unique`), da rimuovere prima dei lead > 1. Lezione: una migrazione che cambia un vincolo usato da `on_conflict` va sincronizzata col deploy del codice, o accompagnata da un ponte |
| Stato del DB diverso da quello descritto nel codice | Su `forecasts` esisteva un secondo vincolo `UNIQUE (station_id, valid_for, model_version)`, mai documentato né usato | Rimosso nella migrazione `forecasts_add_lead_hours`. Lezione: prima di una migrazione, leggere lo schema live (`pg_constraint`), non solo `db.py` |
| Backfill storico Netatmo sottostimato (tutte le stazioni "finivano" mesi/anni fa) | `getmeasure` a scala `1day` non restituisce un punto per chiave: ogni blocco (`beg_time`) contiene un array `value` di giorni consecutivi a passo `step_time`; il parsing iniziale leggeva solo `beg_time` come singolo giorno, ignorando `len(value)` | Calcolo corretto: `ultimo_giorno = beg_time_ultimo_blocco + (len(valori)-1) × step_time`. Inoltre `getmeasure` limita ~1024 valori per chiamata: serve paginazione reale (richieste successive con `date_begin` = timestamp dell'ultimo valore ricevuto + `step_time`) per coprire storici pluriennali |
| Backfill Netatmo con metà delle ore sfasate di 30 minuti e ~500 ore duplicate per device | `getmeasure` a scala `1hour` aggrega su finestre ancorate a `date_begin` e marca il valore al centro: ripartendo ogni pagina dall'ultimo timestamp + 1 s le finestre si alternavano tra :00 e :30 | Partenza a HH:30 (`since − 1800 s`) e ogni pagina da ultimo timestamp + 1800 s: ogni valore è la media HH−0:30 → HH+0:30 marcata a HH:00. `build` scarta i valori a più di 5 min dall'ora (25/09/2026) |
| Target storico Netatmo diverso dalla mappa (MAE 0,61 °C contro le osservazioni live) | Presi tutti i device entro 5 km: `getpublicdata` su aree grandi ne restituisce solo una parte, e la mappa fa la mediana di quel sottoinsieme | Passo `select`: gli N device che coincidono più spesso con `raw_source.temps_raw` delle osservazioni live → MAE 0,38 °C (25/09/2026) |
| Download Netatmo rallentato, token rinnovato ogni 30 s | Due processi di backfill in parallelo: ogni refresh del token invalida l'access token dell'altro (e può far perdere un ciclo all'ingestion) | Un solo processo Netatmo alla volta; il job in ombra non chiama Netatmo |
| Netatmo HTTP 503 `code=27` ripetuti su un device | Servizio temporaneamente non disponibile, a volte persistente per singolo device | Massimo 3 tentativi, poi device saltato e segnalato; rilanciare `download` riprende solo i mancanti |
| Esperimento retraining: opzione E identica all'IFS grezzo sull'estate 2026 | L'input IFS veniva scaricato solo fino a `--test-end`: fuori da quel periodo il bias staz×ora era NaN | Opzione `--data-end` separata dalla fine del test (25/09/2026) |
| Molte stazioni Netatmo pubbliche mostravano identica data di inizio storico (2020-09-26) | Non è la data di installazione del device: è il floor di retention dell'API `getmeasure` per dispositivi non di proprietà (~6 anni indietro dalla data della richiesta) | Nessun fix lato nostro; oltre questa soglia lo storico non è recuperabile da questo endpoint, qualunque sia l'età reale del dispositivo |

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

*Progetto sviluppato da Filippo · Sistema di previsioni meteo iper-locali · Roma*
