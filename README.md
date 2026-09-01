# 🌦️ Meteo Locale — Sistema di Previsioni Meteo Iper-Locali per Roma e il Lazio

Sistema di previsione meteo su scala comunale che cala lo stato meteorologico regionale sul singolo punto, catturando i microclimi che i modelli globali non vedono. Accuratezza territoriale superiore alle app mainstream, infrastruttura a costo zero.

**Stato:** Phase 1, 2a, 2b completate e in produzione. Phase 2c parzialmente completata (bias correction ARSIAL attiva). Phase 3 — **mappa interattiva live su GitHub Pages** (Leaflet + leaflet-velocity, heatmap temperatura/umidità/vento + particelle/frecce). **Restyling estetico dark theme completato (luglio 2026)**: basemap CartoDB Dark Matter, pannello controlli unificato con segmented control e switch stile iOS, popup e legenda ristilizzati mantenendo invariata la logica di calcolo dati. GitHub Actions attivo, inference e ingestion automatica ogni 30 minuti. **32 stazioni attive** su tutto il Lazio (6 Roma metro + 26 espansione Lazio) con copertura Netatmo live e correzione bias ARSIAL data-driven. Mappa con **correzione SST reale sul mare** (Open-Meteo Marine API, blend graduale asimmetrico) e **toggle T / T+1h** (Adesso / +1h). **Dashboard Chart.js live** (`dashboard.html`) con forecast vs observed 7 giorni per stazione, MAE per stazione, switch Temperatura/Umidità.

---

---

## 📑 Indice

1. [Visione del progetto](#-visione-del-progetto)
2. [Perché questo approccio](#-perché-questo-approccio)
3. [Architettura](#-architettura)
4. [Stack tecnologico](#-stack-tecnologico)
5. [Fonti dati](#-fonti-dati)
6. [Feature orografiche](#-feature-orografiche)
7. [Stato attuale](#-stato-attuale)
8. [Roadmap estesa — Fasi 4–7](#-roadmap-estesa--fasi-47)
9. [Sviluppo a lungo termine](#-sviluppo-a-lungo-termine)
10. [Come riprendere il lavoro](#-come-riprendere-il-lavoro)
11. [Risultati del modello](#-risultati-del-modello)
12. [Struttura del progetto](#-struttura-del-progetto)
13. [Database — schema](#-database--schema)
14. [Setup e installazione](#-setup-e-installazione)
15. [I moduli](#-i-moduli)
16. [Differenziali competitivi](#-differenziali-competitivi)
17. [Diario degli errori risolti](#-diario-degli-errori-risolti)

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

Il piano di lungo periodo (previsioni 48h, retraining di dicembre 2026, generalizzazione multi-località, convettività) è nella sezione **Roadmap estesa — Fasi 4–7** che segue.

---

## 🧱 Roadmap estesa — Fasi 4–7

Tre obiettivi strategici di lungo periodo, non indipendenti: l'ordine in cui si affrontano cambia il costo totale. Il principio organizzatore è che il retraining di dicembre 2026 è l'operazione più costosa del progetto e va fatta una volta sola con tutte le novità dentro. Tutto ciò che precede dicembre prepara quel retraining; tutto ciò che segue lo sfrutta.

**I tre obiettivi:**

1. Standardizzazione — da prodotto Roma-specifico a scatola eseguibile per qualsiasi località inserendo solo la posizione.
2. Previsioni orarie fino a 48h — previsioni ora-per-ora sul punto scelto, visualizzate su sito.
3. Convettività — CAPE, soleggiamento, indici convettivi (sempre modulati dall'orografia) → pioggia, perturbazioni, temporali forti.

**Vincolo trasversale** (vale per tutte le fasi): girare senza grandi server esterni. LightGBM/RF restano file da pochi MB; l'inference resta dentro GitHub Actions free tier. L'unico punto da monitorare è la dimensione della nuova tabella di training (vedi Fase 4a).

**Principio metodologico fondante (da non dimenticare mai):** il MOS impara correzioni specifiche delle stazioni su cui è addestrato: il modello non "sa" il meteo, sa quanto ERA5 sbaglia a Trastevere, a Tivoli, a Ostia. Non si sposta il modello — si sposta la fabbrica che produce il modello. Il prodotto generalizzabile non è `lgbm_temperature.txt`, è la pipeline.

Corollario operativo immediato: da ora ogni nuovo pezzo di codice nasce già config-driven (niente nuovi valori Roma hardcoded). Così la generalizzazione (Fase 6) diventa una migrazione del codice vecchio, non una riscrittura del nuovo.

### 🟦 Fase 4a — Infrastruttura 48h (subito → autunno 2026), senza riaddestrare

Il problema dell'input (cuore di tutto l'obiettivo 48h). Oggi il modello è addestrato su ERA5 (rianalisi) e in inference riceve ERA5, disponibile solo per passato/presente. Per prevedere a +48h l'input deve diventare un modello previsionale NWP (Open-Meteo Forecast API: ICON, GFS, ECMWF — gratuiti, stesse variabili, orari fino a 16 giorni). Ma un MOS addestrato su rianalisi e fatto girare su previsioni soffre di input distribution mismatch: la rianalisi è "perfetta" rispetto a una previsione a +36h, quindi in produzione il modello vede errori di input mai visti in training.

La soluzione corretta (MOS classico): addestrare sulle previsioni archiviate, non sulla rianalisi. Open-Meteo offre una Historical Forecast API che archivia i run previsionali passati (dal ~2022). Nuova tabella di training: input = cosa il modello NWP prevedeva per quell'ora con quel lead time; target = cosa è realmente successo alla stazione. Così il modello impara a correggere sia il microclima sia gli errori sistematici del modello previsionale al crescere del lead — esattamente ciò che fanno i MOS operativi dei servizi nazionali.

Il lead time come dimensione. Due strategie: un modello per lead (48 modelli, pesante) oppure un modello unico con `lead_time` come feature. → Scelta: modello unico con feature `lead_time`. LightGBM gestisce bene l'interazione lead × altre feature e rispetta il vincolo "niente grandi server".

Il problema dei lag. A +30h non ci sono osservazioni: i lag vanno calcolati sulla catena previsionale stessa (il lag-3h della previsione a +30h è il valore previsto a +27h), non sulle osservazioni. Refactor concettuale di `features.py`: stessa funzione in due modalità — "storica" (lag su osservato) e "previsionale" (lag su catena NWP). Punto critico anti-leakage: anche il training deve usare i lag previsionali, altrimenti si addestra su informazioni non disponibili in produzione.

Validazione. La metrica diventa una curva: MAE in funzione del lead (+1h, +6h, +12h, +24h, +48h). Degradazione monotona attesa. L'obiettivo non è MAE 0.87°C a +48h (impossibile) ma battere il modello NWP grezzo a ogni lead. Estendere `forecast_vs_observed` per tracciare il lead.

**Piano operativo Fase 4a:**

1. [ ] Pipeline Open-Meteo Forecast API → modello esistente → `forecast_48h` in DB + JSON statico (= versione 0, accetta il mismatch rianalisi/previsione, documentato come provvisorio)
2. [ ] Verificare su Open-Meteo Historical Forecast API quali variabili e che profondità d'archivio sono disponibili per Roma — dato che condiziona lo schema della nuova tabella di training
3. [ ] Costruire la nuova tabella di training dall'archivio previsionale (input previsto + lead_time + lag previsionali)
4. [ ] Stimare la dimensione della nuova tabella (archivio × 48 lead × stazioni ≫ 331k righe attuali) e verificare che il training resti fattibile in locale sul Mac

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

**Top-10 feature importance (gain) del modello T+24h addestrato ad-hoc:** temperature, wind_chill, temperature_lag_1, shortwave_radiation, doy_cos, doy_sin, hour_cos, pressure, wind_u, temperature_roll_mean_6. Best iteration: 147/1000 (contro 643/1000 del modello T+1h).

**Cosa abbiamo imparato:**

1. **Il MOS vince su entrambi gli orizzonti testati.** Nessun elemento per integrare TimesFM-3 in pipeline, ora o dopo dicembre. Parcheggiato, non scartato: da rivalutare solo se emergono use case specifici (es. come secondo parere in un ensemble, non come sostituto).
2. **Il correttore RF non è universalmente utile — va validato per orizzonte, non applicato per default.** A T+1h migliora il val MAE; a T+24h lo *peggiora* (1.6123 → 1.6331) pur migliorando molto il train (1.3136 → 1.2446): overfitting sui residui, perché a 24h i residui del LightGBM sono meno strutturati (più vicini a rumore) che a 1h.
3. **Segnale strutturale importante per il redesign multi-horizon:** il modello T+24h, ottenuto semplicemente riapplicando il feature set pensato per T+1h a un target shiftato di 24h, si appoggia quasi interamente su persistenza (`temperature`, `temperature_lag_1`) e stagionalità (`doy_sin/cos`), non su un vero pattern predittivo a lungo raggio. Il modello "rinuncia prima" (147 alberi contro 643) perché il feature set non gli offre altro segnale da sfruttare oltre quello. Questo NON è un limite di LightGBM in sé, è un limite di riusare feature T+1h-centriche su orizzonti lunghi senza ridisegnarle.

**Implicazioni:** i punti chiave di questo esperimento — feature `lead_time` esplicita, input dall'archivio previsionale invece che da ERA5 per gli orizzonti lunghi, feature dedicate a orizzonti lunghi, validazione del correttore RF per-orizzonte prima del retraining — sono già incorporati nel piano operativo della Fase 4a sopra e nel retraining di Fase 5 sotto, non ripetuti qui.

**Riferimenti — dove trovare codice e artefatti di questo esperimento:**

- **Script di benchmark T+1h** (branch `claude/timesfm3-mos-benchmark-fkq48z`, repo `meteo_locale`): `benchmark_timesfm3_vs_mos.py`, gira dentro `meteo_locale/` (usa direttamente `data/training_10y_h1.parquet` e `model/` di produzione, solo in lettura)
- **Esperimento T+24h (script + dataset + modello) — cartella isolata, FUORI dal repo git**, mai committata: `~/Desktop/timesfm_h24_experiment/` (locale, solo sulla macchina di sviluppo). Include anche lo script di confronto T+24h (equivalente locale di `benchmark_timesfm3_vs_mos.py` ma non versionato)
  - Contiene copie di `historical.py`, `features.py`, `db.py`, `forecast.py`, `correttore.py` + `.env`, usate per generare un modello LightGBM+RF ad-hoc per T+24h (non esiste in produzione, che copre solo T+1h)
  - Dataset generato: `data/roma_sud_h24.parquet` (Roma Sud, 2015–2024, 84.514 righe utilizzabili)
  - Modello generato: `model_experimental_h24/lgbm_temperature.txt` + `rf_correttore_temperature.pkl`
  - Questi artefatti (cartella, script, dataset, modello) sono riproducibili dal codice e non sono conservati: se servono di nuovo, si rigenerano con gli stessi comandi.

### 🟦 Fase 4b — Dashboard GitHub Pages (parallela)

Assorbe il task Chart.js già pianificato in Fase 3 ed estende la dashboard con il meteogramma orario 48h per stazione. Zero infrastruttura nuova: `export_static.py` produce `forecast_48h.json`, la pagina GitHub Pages lo rende con Chart.js. Coerente col vincolo costo-zero.

**Stato:** la dashboard Chart.js di base è già stata completata a giugno 2026 (`dashboard_data.json`, `dashboard.html`, auto-update via `export-dashboard.yml` — vedi Fase 3 in *Stato attuale*). Resta da fare solo il meteogramma 48h, che dipende dalla pipeline previsionale della Fase 4a.

1. [x] `dashboard_data.json` (forecast_vs_observed, MAE per stazione, ultime osservazioni) — completato in Fase 3
2. [ ] `forecast_48h.json` per stazione (meteogramma) — dipende dalla Fase 4a
3. [x] `dashboard.html` con Chart.js: tabelle, "Previsto vs Osservato" — completato in Fase 3; resta da aggiungere il meteogramma 48h
4. [x] Auto-update via workflow dedicato (`export-dashboard.yml`) — completato in Fase 3

### 🟪 Fase 5 — Retraining "grande" (Dicembre 2026)

Un unico retraining che incorpora simultaneamente tutto ciò che è maturato. Definire ora lo schema della nuova tabella così che l'accumulo estivo-autunnale sia già nella forma giusta. Nota stagionale: l'estate è la stagione convettiva — i temporali di luglio–settembre 2026 sono dati preziosi da non perdere, motivo in più per fissare lo schema in anticipo.

1. [ ] Scaricare nuovi CSV ARSIAL 2026, rieseguire `arsial_bias_correction.py`
2. [ ] Input da archivio previsionale (Historical Forecast API) invece che ERA5 puro
3. [ ] Feature `lead_time` integrata
4. [ ] Target Netatmo orario accumulato (giugno–dicembre 2026) per tutte e 6 le zone
5. [ ] Nuove variabili convettive in input: CAPE, radiazione shortwave, copertura nuvolosa multi-livello, eventualmente 500 hPa
6. [ ] Primo target di classificazione: pioggia sì/no orario (vedi Fase 7 per la metodologia)
7. [ ] Riaddestrare RF corrector sulle nuove stazioni
8. [ ] Confrontare MAE pre/post per Tivoli e Castelli Romani; curva MAE vs lead
9. [ ] Rimuovere la correzione ARSIAL post-hoc (incorporata nel modello)

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

**Stato corrente (settembre 2026):** Fase 1, 2a, 2b, 3 in produzione (Fase 3 include carta del vento e dashboard Chart.js). Fase 2c parziale (bias correction ARSIAL attiva, Protezione Civile Lazio ancora da integrare). Radar RainViewer in corso (vedi *Sviluppo a lungo termine*). Roadmap strategica di lungo periodo (48h, retraining dicembre, generalizzazione, convettività) → [Roadmap estesa — Fasi 4–7](#-roadmap-estesa--fasi-47). GitHub Actions attivi, tutti triggerati esternamente via cron-job.org (nessuno `schedule:` interno ai workflow — inaffidabile su repo a bassa attività):
- `inference.yml` — previsioni, ogni 30 min
- `ingestion.yml` — osservazioni METAR + Netatmo, ogni 30 min
- `export.yml` — export griglia statica (`latest.json`, `wind_grid.json`), ogni ora
- `export-dashboard.yml` — export `dashboard_data.json`, 2×/giorno (8:00, 20:00)

**Mappa live:** `https://filippopetto-maker.github.io/meteo_locale/`

**Prossima scadenza fissa: Dicembre 2026** — retraining completo con Netatmo accumulato (Fase 5 della Roadmap estesa).

**Completato (giugno 2026):**
- Correzione SST sul mare: `sst.py` + blend graduale asimmetrico in `grid.py` + `export_static.py`; `LATIUM_COAST` estesa da Anzio→Gaeta a sud e fino a (42.85, 10.85) a nord
- Toggle T / T+1h sulla mappa: `temp_grid_observed` + `temp_grid_forecast` in `latest.json`; scala colori unificata tra i due stati
- 4 nuove stazioni (id 51–54): Fiano Romano, Civitavecchia, Filettino 1044m, Gaeta
- Palette umidità ridisegnata per contrasto reale nel range 40-80% (pivot verde)
- Fix copertura Netatmo: `mainMETEO.py` portato da `ROMA_BBOX` singolo a `LAZIO_BBOXES` (5 sotto-zone con dedup), risolve sia i buchi geografici (Cassino, Filettino) sia il "soffocamento" delle zone dense (EUR, Trastevere) causato dal tetto di risultati per chiamata Netatmo
- `min_cluster` rilassato a 1 per id≥39 e per microclima `quota`/`alta_quota`/`colline_interne`
- Pulizia naming: rinominate stazioni con nomi duplicati/imprecisi (Saxa Rubra id46→Labaro, Gaeta/Formia id49→Fondi); riclassificati microclima (Tivoli/Bracciano/Rieti→`colline_interne`, Filettino→`alta_quota`, isolando `quota` alla sola Castelli Romani)

**Dashboard live:** `https://filippopetto-maker.github.io/meteo_locale/dashboard.html`

**Prossimo task immediato:** Radar RainViewer (vedi *Sviluppo a lungo termine*). A seguire: Fase 4a della Roadmap estesa — Infrastruttura 48h.

~~Fix legenda nodi~~ — **RISOLTO** (vedi Diario degli errori risolti): la scala del gradiente ora converte correttamente `ws_min`/`ws_max` da km/h a nodi anche per i colori della heatmap tramite `updateWindLegend()`.

**Miglioramenti futuri mappa:**
- Più stazioni: settore ovest (Bracciano, Ostia Nord) e nord completamente scoperti dall'IDW — ogni nuova stazione migliora il gradiente senza modifiche al codice
- Upgrade a MapLibre GL JS per qualità visiva superiore (vettoriale, tile più dettagliate)
- Upgrade `actions/checkout@v4` → `@v5` e `actions/setup-python@v5` → versione corrente (warning Node.js 20 deprecation)

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
| Stazioni (operative) | 32 (schema espanso Lazio, Phase 2c/3) |
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
per tutte le 32 zone. Queste diventano i **target futuri del modello**:

```
Oggi:        ERA5 (input) + METAR 4 stazioni (target storico)
             → previsioni buone per costiera/urban_canyon, approssimate per quota

Ogni 30 min: Netatmo accumula ground truth per 32 zone
             ↓
~6 mesi:     ERA5 (input) + Netatmo 32 stazioni (target live)
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
│       ├── inference.yml            # previsioni, trigger esterno (cron-job.org) ✅ ATTIVO
│       ├── ingestion.yml            # osservazioni live, trigger esterno (cron-job.org) ✅ ATTIVO
│       ├── export.yml               # export griglie mappa, trigger esterno ✅ ATTIVO
│       └── export-dashboard.yml     # export dashboard_data.json, trigger esterno 8:00/20:00 ✅ ATTIVO
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
├── scripts/
│   └── export_static.py         # export griglie + dashboard (flag --dashboard-only) ✅
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
- Scrive previsioni T+1h su Supabase (`forecasts`) per tutte le stazioni attive (32)
- Supporta `--dry-run` per test senza scrittura DB
- Eseguito automaticamente ogni 30 min da GitHub Actions

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
| GitHub Actions `schedule:` interno non affidabile (run saltati/ritardati) | Scheduler nativo GitHub degrada su repo a bassa attività | Trigger esclusivamente esterno via cron-job.org (workflow_dispatch), nessuno `schedule:` nei workflow file |

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
