# ia_meteo

Ramo sperimentale per il nowcasting convettivo con IA sul dominio 30–50°N / 10°W–30°E
(satellite SEVIRI/FCI, fulmini MTG LI, radar OPERA e IT-DPC-SRI, ambiente da ERA5/Open-Meteo).
Vive solo nel branch `ia-meteo` e non tocca la produzione di `meteo_locale`
(nessun file fuori da questa cartella, nessun workflow GitHub Actions, ambiente conda separato).

**Riferimento:** planning `IA meteo — Planning di avvio` (Project Metek, `claude/IA_METEO_PLANNING.md`).

## Stato delle fasi

| Fase | Contenuto | Stato |
|:--|:--|:--|
| 0 | Ambiente + smoke test delle fonti | in corso: test eseguiti, vedi [`smoke/REPORT.md`](smoke/REPORT.md) |
| 1 | Selezione eventi | da fare |
| 2 | Download e preparazione dataset | da fare |
| 3 | Baseline (pySTEPS) | da fare |
| 4 | Fine-tuning e test sul Lazio | da fare |

## Struttura

```
ia_meteo/
├── environment.yml   # ambiente conda ia_meteo (note di installazione su Apple Silicon in testa)
├── common.py         # DOMAIN_BBOX, TEST_BBOXES, helper .env, timer, dimensioni, write_result
├── archivio.py       # archivio delle finestre su Hugging Face, budget Mac/HF, cache a tetto fisso
├── finestra.py       # costruisce una finestra (tutte le fonti sulla griglia 0,05°) e la carica su HF
├── data/             # staging, lavoro e cache locali (ignorato da git, tetto 12 GB)
├── fonti/            # accesso alle fonti, consolidato dalla Fase 0 (base dei fetch_*.py della Fase 2)
│   ├── eumetsat.py   # SEVIRI, FCI per chunk, LI, Data Tailor
│   ├── opera.py      # compositi OPERA dal bucket S3 storico, lettore ODIM 2.0/2.4, griglia comune
│   ├── era5.py       # fetch_env (ramo ambiente, CAPE/CIN), trattamento CIN mancante
│   ├── openmeteo.py  # Historical Forecast / Forecast API
│   ├── itdpc.py      # IT-DPC-SRI lazy dall'European Weather Cloud
│   └── imerg.py      # IMERG via earthaccess
└── smoke/
    ├── s1_seviri.py … s11_archivio_hf.py  # un test per fonte (S10: CAPE/CIN, S11: archivio HF)
    ├── stima_spazio.py                  # spazio e tempi per i test reali, da misure
    ├── make_report.py                   # genera REPORT.md dai JSON
    ├── results/                         # un JSON per test
    ├── previews/                        # PNG piccoli
    └── raw/                             # file grezzi, ignorato da git
```

## Uso

```bash
conda activate ia_meteo
cd ia_meteo/smoke
python s3_li_flashes.py      # S3 prima di S2/S4: scrive best_hour_utc
python s1_seviri.py          # S1 prima di S9 e di stima_spazio: produce i ritagli in raw/s1
python stima_spazio.py       # dopo S1, S2, S10
python make_report.py
```

## Spazio: Mac a tetto fisso, archivio su Hugging Face

Il Mac ha al più ~20 GB liberi; l'archivio delle finestre sta sul dataset privato
`<utente>/ia-meteo-events` (budget ~95 GB). Il flusso per ogni finestra è:

1. `finestra.py` scarica le fonti un prodotto alla volta (3 in parallelo), ritaglia sulla griglia
   0,05° e cancella subito il grezzo; scrive `data/staging/<id>/` (≈ 0,06–0,17 GB in int16);
2. `archivio.carica_finestra` la carica con un solo commit (aggiorna anche `registro.json`),
   verifica nomi e dimensioni sul repo e cancella la copia locale;
3. per lavorarci, `archivio.scarica_finestra(id, sorgenti=[...])` la riporta in `data/cache/`,
   che non supera 8 GB: oltre, si cancellano le finestre usate meno di recente.

Prima di ogni passo `archivio.controlla_budget` verifica tre limiti e si ferma se uno sfora:

| Limite | Valore | Cosa misura |
|:--|--:|:--|
| `BUDGET_HF_GB` | 95 GB | spazio usato da tutti i repo dell'account HF, cronologia inclusa |
| `LIMITE_LOCALE_GB` | 12 GB | `ia_meteo/data/` (staging + lavoro + cache) |
| `MARGINE_DISCO_GB` | 5 GB | disco libero minimo da lasciare al Mac |

Su HF i file cancellati continuano a occupare spazio nella cronologia finché non si chiama
`archivio.compatta_cronologia()`.

```bash
python finestra.py --id 20190715_06 --inizio 2019-07-15T06:00 --fine 2019-07-15T18:00 --carica
python finestra.py --id 20220812_10 --inizio 2022-08-12T10:00 --fine 2022-08-12T22:00 --lazio --carica
```

Credenziali: `.env` della root (`EUMETSAT_CONSUMER_KEY`, `EUMETSAT_CONSUMER_SECRET`, `HF_TOKEN`),
`~/.cdsapirc`, `~/.netrc`, cache di `huggingface-cli`. Nessuno script le stampa.
Nessuna chiamata Netatmo.
