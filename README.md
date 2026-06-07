# 🌦️ Meteo Locale — Sistema di Previsioni Meteo Iper-Locali per Roma

Sistema di previsione meteo su scala comunale che cala lo stato meteorologico regionale sul singolo punto, catturando i microclimi che i modelli globali non vedono. Accuratezza territoriale superiore alle app mainstream, infrastruttura a costo zero.

---

## 📑 Indice

1. [Visione del progetto](#-visione-del-progetto)  
2. [Perché questo approccio](#-perché-questo-approccio)  
3. [Architettura](#-architettura)  
4. [Stack tecnologico](#-stack-tecnologico)  
5. [Fonti dati](#-fonti-dati)  
6. [Feature orografiche](#-feature-orografiche)  
7. [Stato attuale](#-stato-attuale)  
8. [Struttura del progetto](#-struttura-del-progetto)  
9. [Database — schema](#-database--schema)  
10. [Setup e installazione](#-setup-e-installazione)  
11. [I moduli](#-i-moduli)  
12. [Roadmap](#-roadmap)  
13. [Diario degli errori risolti](#-diario-degli-errori-risolti)  
14. [Differenziali competitivi](#-differenziali-competitivi)  
15. [Come riprendere il lavoro](#-come-riprendere-il-lavoro)

---

## 🎯 Visione del progetto

L'obiettivo è costruire un sistema di previsione meteo **iper-locale** sul comune di Roma, capace di battere le principali app meteo sulla **capillarità della conoscenza del territorio**.

Le grandi app usano modelli globali interpolati su griglie larghe (10–25 km), che non catturano i microclimi locali: l'isola di calore urbana del centro storico, la brezza marina di Ostia, l'inversione termica notturna nelle zone basse. Questo sistema parte invece da **dati osservati reali** stazione-per-stazione e impara le correzioni locali che i modelli globali sbagliano.

**Cosa fa la scatola, in una frase:** dato lo stato meteorologico regionale (temperatura, umidità, CAPE, vento) e il profilo orografico di un punto, restituisce una previsione locale — rischio temporale, pioggia attesa, temperatura e vento con direzione.

**Prodotto finale atteso:** un sistema autonomo che raccoglie, analizza e prevede, girando su infrastruttura cloud gratuita, spendibile come progetto di portfolio nel mercato del lavoro data/ML.

---

## 🧭 Perché questo approccio

### Cosa NON facciamo: WRF / NWP completo

Inizialmente valutato un modello numerico di previsione (WRF), poi abbandonato. Un modello fisico integra nel tempo le equazioni della fluidodinamica e pretende in input lo **stato 3D completo dell'atmosfera** su tutta una griglia: non lo si "alimenta" con quattro parametri scalari, e per girare seriamente richiede infrastruttura HPC. Impraticabile su un Mac senza server, e comunque l'attrezzo sbagliato per questo scopo.

### Cosa facciamo: statistical downscaling \+ ML

Accoppiamo **due fonti diverse** nella tabella di addestramento:

- **Input** \= stato regionale grezzo dalla rianalisi storica (ERA5 via Open-Meteo).  
- **Target** \= cosa è *realmente* successo in un punto preciso, misurato da una stazione vera (METAR, ARPA, Netatmo).

Il modello impara la **correzione locale**: la differenza tra il grezzo regionale e l'osservazione reale *è* il microclima.

**La trappola della risoluzione (da non dimenticare mai).** Allenare l'ML *solo* sulla rianalisi è inutile: a 25 km, Ostia, Monte Mario, il centro e un parco sono la stessa cella sfocata. Un modello addestrato lì impara a riprodurre ERA5, non a batterlo. Il segnale iper-locale **non è dentro la rianalisi gratuita** — entra solo attraverso i target di stazioni reali. Per questo input e target vengono da fonti diverse.

### Principio chiave: multi-stazione è necessario, non opzionale

Con una sola stazione le feature orografiche (quota, distanza dal mare, esposizione) sono **costanti** → non insegnano nulla, vengono assorbite come offset fisso. Si ottiene solo una correzione di bias *site-specific*: utile, ma non orografia generalizzabile, e cieca su qualsiasi punto nuovo.

Le feature orografiche diventano predittori appresi e generalizzabili **solo addestrando simultaneamente su più stazioni con profili di terreno contrastanti** (costiero, pianura, urbano denso, quota). Solo confrontandoli il modello impara la regola fisica generale. → Non si sceglie *un* punto, si sceglie un **set di 3–4 punti** che coprano l'arco orografico.

### Ordine di difficoltà dei target di previsione

temperatura  \<  direzione vento  ≈  rischio temporali  \<  pioggia puntuale (mm)

  (facile)                                                      (più difficile)

Sviluppiamo in quest'ordine per costruire risultati e momentum. La pioggia quantitativa in un punto è il problema più duro della meteorologia: da input scalari, aspettarsi al massimo una probabilità grezza, non i millimetri.

### Nota metodologica: evitare il look-ahead bias

Se la scatola deve *prevedere* (non solo diagnosticare il presente), l'input dev'essere lo stato all'ora **T** e il target l'osservazione a **T+N**. Mai mescolare i tempi: altrimenti il modello "bara" guardando il futuro in fase di training e poi crolla nel mondo reale.

---

## 🏗️ Architettura

┌───────────────────────────────────────────────────┐

│              LAYER 1 — INGESTION                    │

│  ── Storico (per l'addestramento) ──                │

│  Open-Meteo / ERA5  → input regionale (reanalisi)   │

│  METAR · ARPA       → target storici stazioni       │

│  ── Live (per l'operatività) ──                     │

│  Netatmo API        → stazioni reali dense          │

│  ARPA Lazio         → dati ufficiali validati       │

└────────────────────┬────────────────────────────────┘

                     │

┌────────────────────▼────────────────────────────────┐

│              LAYER 2 — STORAGE                      │

│  Supabase PostgreSQL (hosted, gratuito)             │

│  stations · observations · forecasts                │

│  qc\_log · model\_metrics                             │

└────────────────────┬────────────────────────────────┘

                     │

┌────────────────────▼────────────────────────────────┐

│              LAYER 3 — PROCESSING                   │

│  QC (range·climatologico·persistenza·spaziale)      │

│  Feature engineering (temporali·orografiche·derivate)│

│  LightGBM (previsione) \+ RF (correttore)            │

└────────────────────┬────────────────────────────────┘

                     │

┌────────────────────▼────────────────────────────────┐

│              LAYER 4 — OUTPUT                       │

│  Dashboard (Streamlit) · API (FastAPI)              │

│  Mappa (Cartopy)                                    │

└─────────────────────────────────────────────────────┘

Esecuzione automatica (live): GitHub Actions (cron ogni 30 min, gratuito)

**Due percorsi dati.** Il percorso *storico* (ERA5 \+ storico stazioni) serve ad allenare il modello SUBITO, offline, senza attese. Il percorso *live* (Netatmo \+ ARPA via GitHub Actions → Supabase) serve a coprire i punti senza storico e ad alimentare le previsioni operative una volta che il modello è addestrato.

---

## 🛠️ Stack tecnologico

| Layer | Strumento | Costo |
| :---- | :---- | :---- |
| Dati storici | Open-Meteo Historical API (ERA5) | €0 |
| Raccolta dati live | Python \+ GitHub Actions (cron) | €0 |
| Storage | Supabase PostgreSQL (free tier) | €0 |
| Accesso DB | supabase-py (API REST su HTTPS) | €0 |
| Quality Control | Python (logica custom) | €0 |
| Modello ML | LightGBM \+ scikit-learn (RandomForest) | €0 |
| Visualizzazione | Streamlit / Grafana / Cartopy | €0 |
| Versionamento | GitHub | €0 |
| **TOTALE** |  | **€0** |

---

## 📡 Fonti dati

### Input — stato regionale storico

- **Open-Meteo Historical Weather API** — basata su ERA5, dati orari dal **1940**, copertura globale **senza buchi**, **gratuita e senza API key**, licenza CC BY 4.0. ERA5 a 0,25° (\~25 km); ERA5-Land a 0,1° (\~9 km). Espone le variabili che ci servono come input, **incluso CAPE**. Scaricabile anche in locale via AWS Open Data per grandi volumi.

### Target — osservazioni reali

- **METAR aeroportuali** (Ciampino, Fiumicino, Roma Urbe) — storico pluridecennale, ideale per il primo addestramento.  
- **ARPA Lazio** — dati ufficiali validati.  
- **Netatmo** — rete densa di stazioni personali (ottima copertura urbana, ma dato grezzo rumoroso → motivo in più per un QC robusto, vedi sotto).

**CAPE è un input, non un'osservazione di stazione.** Le stazioni misurano temperatura, vento, umidità, pioggia; CAPE e profili d'instabilità arrivano dalla rianalisi ERA5. Per il target "pioggia" lo schema `observations` andrà esteso con un campo di precipitazione.

---

## 🏔️ Feature orografiche

Sono il vero vantaggio competitivo: traducono i meccanismi fisici del territorio in colonne della tabella di training. In ordine di importanza:

- **Quota — come delta rispetto alla cella ERA5.** La feature più potente. Non conta la quota assoluta ma di quanto il punto reale si scosta dalla quota "spalmata" che ERA5 assume per quella cella. È il correttore di temperatura più grosso (l'aria si raffredda di \~6,5 °C per km).  
- **Posizione nel terreno** (fondovalle / versante / cresta). Governa la temperatura notturna: di notte l'aria fredda scivola in basso e si accumula nei fondovalle (inversioni, sacche di freddo); le creste restano più calde ma ventose.  
- **Esposizione** (pendenza \+ orientamento del versante). Quanto sole prende il punto; effetto diurno e stagionale (versanti a sud più caldi).  
- **Densità urbana.** Isola di calore: asfalto e cemento rilasciano calore di notte (+2/+5 °C vs campagna). Si descrive con superficie impermeabile, densità di edificato, indice di vegetazione, geometria street-canyon.  
- **Distanza dal mare / acqua.** Brezza di mare: di giorno richiama aria fresca e umida dalla costa verso l'interno. Forte e regolare d'estate. Analogo minore per Tevere e laghi (Bracciano, Albano).  
- **Esposizione al vento / sollevamento orografico.** Incanalamento lungo le valli, sollevamento sui versanti sopravento → più pioggia sopravento, ombra pluviometrica sottovento. Rilevante soprattutto per il target pioggia.

### Mappatura sulle etichette Supabase esistenti

Le etichette di microclima già nello schema sono il vocabolario orografico di partenza: `urban_canyon`, `esposta_sole`, `quota`, `costiera`, `verde_parco`.

---

## 📊 Stato attuale

### ✅ Blocco 1 — Storage (COMPLETATO)

- [x] Schema DB progettato e creato su Supabase  
- [x] 5 tabelle \+ 2 viste operative  
- [x] Modulo `db.py` di connessione (via API REST)  
- [x] `.env` configurato con credenziali  
- [x] Connessione testata: 4 stazioni iniziali caricate

### 🔄 Blocco 2 — Modello ML (IN CORSO)

- [ ] `historical.py` — costruzione tabella storica (ERA5 input \+ target stazioni)  
- [ ] `features.py` — Feature Engineering (incl. feature orografiche)  
- [ ] `forecast.py` — Modello LightGBM  
- [ ] `correttore.py` — RF Correttore  
- [x] `qc.py` — Quality Control a 4 livelli (scritto)  
- [ ] `qc.py` — installato e testato in locale

### ⏳ Blocco 3 — Pipeline live (DA FARE)

- [ ] `mainMETEO.py` — riscrittura con fonti multi-stazione reali  
- [ ] Integrazione fonti Netatmo \+ ARPA  
- [ ] GitHub Actions per esecuzione automatica

---

## 📁 Struttura del progetto

meteo\_locale/

│

├── .env                  \# credenziali Supabase (NON committare)

├── .gitignore            \# include .env

├── README.md             \# questo file

├── requirements.txt      \# dipendenze Python

│

├── db.py                 \# modulo connessione DB (CRUD \+ health check)

├── schema.sql            \# schema database (già eseguito su Supabase)

│

├── data/

│   └── historical.py     \# scarica ERA5 \+ storico stazioni → tabella training (DA FARE)

│

├── collect/

│   └── mainMETEO.py      \# raccolta dati live (DA RISCRIVERE)

│

├── model/

│   ├── qc.py             \# quality control 4 livelli ✅

│   ├── features.py       \# feature engineering \+ orografiche (DA FARE)

│   ├── forecast.py       \# LightGBM (DA FARE)

│   └── correttore.py     \# RF correttore (DA FARE)

│

└── output/

    ├── mappa\_meteo.py    \# mappa Cartopy (da adattare al DB)

    └── dashboard.py      \# Streamlit (DA FARE)

---

## 🗄️ Database — schema

### `stations` — anagrafica stazioni

| Campo | Tipo | Note |
| :---- | :---- | :---- |
| id | SERIAL PK |  |
| name | TEXT |  |
| lat, lon | DOUBLE | coordinate |
| altitude | DOUBLE | metri s.l.m. |
| source | TEXT | netatmo / arpa / open\_meteo |
| microclima | TEXT | urban\_canyon / esposta\_sole / costiera / verde\_parco / quota |
| is\_active | BOOLEAN |  |

### `observations` — dati grezzi (serie temporale)

| Campo | Tipo | Note |
| :---- | :---- | :---- |
| id | BIGSERIAL PK |  |
| station\_id | FK → stations |  |
| recorded\_at | TIMESTAMPTZ |  |
| temperature, wind\_speed, wind\_direction | DOUBLE |  |
| humidity, pressure, precipitation | DOUBLE | opzionali / per target pioggia |
| qc\_flag | SMALLINT | 0=ok, 1=sospetto, 2=scartato |
| raw\_source | JSONB | risposta API originale |

### `qc_log` — log delle anomalie QC

Traccia ogni flag con: check\_type, field\_name, original\_value, reason.

### `forecasts` — previsioni generate

Include `model_version` per confrontare versioni diverse e `corrected` (bool).

### `model_metrics` — performance nel tempo

Storico MAE per temperatura/vento/direzione, n\_samples, periodo.

### Viste

- `latest_observations` — ultima rilevazione valida per stazione  
- `forecast_vs_observed` — confronto automatico previsione vs reale con MAE

---

## ⚙️ Setup e installazione

### 1\. Clona e prepara l'ambiente

cd \~/Desktop/meteo\_locale

pip install \-r requirements.txt

### 2\. Configura le credenziali

Crea il file `.env`:

SUPABASE\_URL=https://xxxxxxxx.supabase.co

SUPABASE\_KEY=sb\_secret\_xxxxxxxxxxxxx

Le chiavi si trovano su Supabase → Settings → API Keys. Usa la **secret key** per gli script backend.

### 3\. Crea lo schema DB

Esegui `schema.sql` nell'SQL Editor di Supabase.

### 4\. Testa la connessione

python3 db.py

Output atteso: `✅ Connessione OK` \+ lista delle stazioni.

### Note ambiente

- macOS: usare sempre `python3`, non `python`  
- Connessione via **API REST (HTTPS porta 443\)**, non PostgreSQL diretto (porta 5432 spesso bloccata da firewall aziendali)

---

## 🧩 I moduli

### `db.py` — Data Access Layer

Modulo unico di connessione, importato da tutti gli script. Espone:

- `get_active_stations()` — lista stazioni attive  
- `insert_observation(...)` — salva una misurazione  
- `get_observations(station_id, hours)` — storico di una stazione  
- `get_latest_observations()` — ultima per stazione  
- `insert_forecast(...)` — salva una previsione  
- `health_check()` — verifica connessione

**Principio:** se Supabase cambia, si modifica solo `db.py` — gli altri script restano intatti (separation of concerns).

### `historical.py` — Costruzione tabella storica (da fare)

Il modulo che abilita la **Fase 1**. Per ogni punto target:

- scarica da Open-Meteo la serie ERA5 (input regionale: T, umidità, CAPE, vento)  
- recupera lo storico osservato della stazione (target reale)  
- allinea i tempi rispettando lo sfasamento T → T+N (no look-ahead bias)  
- produce la tabella di training (input \+ feature orografiche \+ target)

### `qc.py` — Quality Control (4 livelli)

Si applica soprattutto ai **dati live** (Netatmo grezzo è rumoroso). I dati storici ERA5 sono già consistenti, e METAR/ARPA sono validati.

| Livello | Cosa controlla | Azione |
| :---- | :---- | :---- |
| 1\. Range check | Valori fisicamente impossibili | Scarta (flag 2\) |
| 2\. Climatological | Plausibilità per mese \+ fascia oraria | Scarta o sospetto |
| 3\. Persistence | Sensore bloccato (valore fermo) | Sospetto (flag 1\) |
| 4\. Spatial | Outlier vs stazioni vicine (z-score) | Scarta o sospetto |

**Climatological check** — usa medie storiche di Roma aggiornate ai cambiamenti climatici (trend 2015–2024), con **offset per tipo di stazione**:

esposta\_sole: \+5°C   urban\_canyon: \+3°C   standard:  0°C

costiera:     \-1°C   verde\_parco:  \-2°C   quota:    \-3°C

Evita di scartare dati reali validi da stazioni esposte (es. un tetto a luglio può legittimamente segnare 48°C).

### `features.py` — Feature Engineering (da fare)

Trasforma i dati grezzi in feature per il modello:

- **Temporali:** ora del giorno (sin/cos), giorno dell'anno, trend  
- **Lag:** valori delle ore precedenti  
- **Rolling:** medie/varianze su finestre mobili  
- **Orografiche:** delta quota vs cella ERA5, posizione nel terreno, esposizione, densità urbana, distanza dal mare → vedi sezione [Feature orografiche](#-feature-orografiche)

### `forecast.py` — LightGBM (da fare)

Gradient boosting su feature tabulari, standard industriale per serie temporali strutturate. Produce la previsione grezza.

### `correttore.py` — RF Correttore (da fare)

Approccio a due stadi: LightGBM fa la previsione grezza, il RandomForest impara gli errori sistematici per microzona e li compensa.

---

## 🗺️ Roadmap

Una sola macchina che evolve in tre fasi. La Fase 1 NON aspetta la raccolta dati: parte subito dallo storico.

### Fase 1 — Modello sullo storico (SUBITO)

1. `historical.py`: costruire la tabella di training (ERA5 input \+ storico stazioni target)  
2. Partire da **un** punto con storico lungo e pulito (es. aeroporto) per collaudare la pipeline end-to-end  
3. Aggiungere **3–4 punti contrastanti** (costiero / pianura / urbano / quota) per attivare le feature orografiche  
4. `features.py` → `forecast.py` (LightGBM) → `correttore.py` (RF)  
5. Target in ordine di difficoltà: temperatura → vento/direzione → rischio temporali → pioggia

### Fase 2 — Pipeline live in parallelo

1. Installare e testare `qc.py`  
2. Riscrivere `mainMETEO.py` con fonti reali multi-stazione (Netatmo denso \+ ARPA)  
3. GitHub Actions (cron ogni 30 min) → sistema autonomo, indipendente dal PC locale  
4. **Scopo:** coprire i punti *senza* storico (valore unico) e alimentare le previsioni operative

### Fase 3 — Output e ottimizzazione

1. Dashboard Streamlit interattiva \+ API REST con FastAPI \+ mappa Cartopy  
2. Tuning iperparametri per massima accuratezza  
3. Espansione capillare delle stazioni  
4. Consolidamento dei target più difficili (rischio temporali, pioggia puntuale)

---

## 🐛 Diario degli errori risolti

| Errore | Causa | Soluzione |
| :---- | :---- | :---- |
| `extension "timescaledb" is not available` | Free tier Supabase senza TimescaleDB | PostgreSQL standard \+ indici ottimizzati |
| `could not translate host name` | Porta 5432 bloccata da rete aziendale | API REST Supabase su HTTPS (porta 443\) |
| `Tenant or user not found` | Formato URL pooler errato | Client ufficiale supabase-py con API key |
| `ping timeout` | ICMP bloccato dal router | Falso allarme — internet funzionante |
| `command not found: python` | macOS usa python3 | Uso di `python3` ovunque |
| `.env` non visibile nel Finder | File nascosto (punto iniziale) | Gestione via terminale |
| Coordinate Roma Nord errate | 41.016 invece di 42.016 | Corretto nello schema |

---

## 🏆 Differenziali competitivi

- **Statistical downscaling ERA5 → stazioni reali** — approccio corretto e sostenibile vs NWP pesante; impara le correzioni che il modello globale sbaglia  
- **Architettura multi-stazione** — abilita l'apprendimento delle feature orografiche, il vero vantaggio sulla concorrenza  
- **Feature orografiche esplicite** — delta quota vs cella ERA5, fondovalle, esposizione, isola di calore: il microclima codificato come predittori  
- **QC climatologico contestuale** — validazione contro la climatologia locale per mese e fascia oraria, raro nei tool open source  
- **Offset per tipo di stazione** — gestione esplicita del microclima urbano già nel quality control  
- **Soglie aggiornate ai cambiamenti climatici** — non medie storiche obsolete  
- **Addestramento immediato sullo storico** — nessuna attesa per accumulare dati  
- **Storage strutturato con storico illimitato** — vs CSV fragile  
- **Infrastruttura a costo zero** — interamente deployabile gratis

---

## 📌 Come riprendere il lavoro

1. Apri il terminale: `cd ~/Desktop/meteo_locale`  
2. Verifica la connessione: `python3 db.py`  
3. **Prossima decisione aperta:** scegliere il set di 3–4 punti target contrastanti in base alla disponibilità reale di storico (ERA5 sul punto \+ record lungo stazione)  
4. **Prossimo task di codice:** `historical.py` per costruire la prima tabella di training, partendo da un punto con storico lungo  
5. Consulta la [Roadmap](#-roadmap) per il quadro completo

---

*Progetto sviluppato da Filippo · Sistema di previsioni meteo iper-locali · Roma*  
