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
└── smoke/
    ├── s1_seviri.py … s9_toolchain.py   # un test per fonte
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
python s1_seviri.py          # S1 prima di S9: produce i ritagli in raw/s1
python make_report.py
```

Credenziali: `.env` della root (`EUMETSAT_CONSUMER_KEY`, `EUMETSAT_CONSUMER_SECRET`, `HF_TOKEN`),
`~/.cdsapirc`, `~/.netrc`, cache di `huggingface-cli`. Nessuno script le stampa.
Nessuna chiamata Netatmo.
