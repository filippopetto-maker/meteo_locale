# IA meteo — Fase 0: smoke test

Generato da `smoke/make_report.py` dai JSON in `smoke/results/`. Esecuzione 07–08/10/2026 sul Mac (Apple Silicon, 8 GB), ambiente conda `ia_meteo`. Nessuna conclusione sulle scelte del planning: solo misure.

## 1. Tabella riassuntiva

| Test | Esito | Tempo (s) | MB scaricati | Risposta |
|:--|:--|--:|--:|:--|
| s1_seviri | **OK** | 74 | 740.6 | 4 prodotti trovati in 1 h; 4/4 scaricati e letti; zip medio 185.2 MB, .nat 271.2 MB, download medio 17.2 s; Tb 217.8–327.0 K; ritaglio 0,05° 1.3 MB |
| s2_fci | **OK** | 401 | 180.4 | Sì: entry scaricabili singolarmente con eumdac (Product.open(entry=...)). Chunk 31–37 + TRAIL = 180 MB contro 1103 MB del disco intero (16%); ritaglio ir_105 Tb 217.6–325.5 K; Data Tailor: DONE |
| s3_li_flashes | **OK** | 85 | 167.5 | 48/48 prodotti da 10 min letti (167.5 MB solo BODY); griglia sparsa su geostazionaria FCI 2 km; best_hour_utc 14:00 (36576 flash·pixel nel dominio) |
| s4_opera | **OK** | 1 | 3.4 | 2019 ODYSSEY: spagna 96.2%, italia 45.7%, balcani 58.7%, lazio 38.6%, tirreno 23.8%, nord_africa 23.7% / 2025 CIRRUS: spagna 91.2%, italia 60.2%, balcani 73.9%, lazio 54.1%, tirreno 23.6%, nord_africa 20.4% |
| s5_openmeteo_env | **OK** | 3 | 0.011 | ecmwf_ifs 2019-07-15: CAPE 0/24, CIN 0/24, LI 0/24, W500 0/24, T2m(controllo) 24/24, passo None h; ecmwf_ifs025 2024-07-15: CAPE 24/24, CIN 0/24, LI 0/24, W500 24/24, T2m(controllo) 24/24, passo 1 h; ncep_gfs_seamless 2022-07-15: CAPE 24/24, CIN 24/24, LI 24/24, W500 24/24, T2m(controllo) 24/24, passo 1 h; gfs_seamless 2022-07-15: CAPE 24/24, CIN 24/24, LI 24/24, W500 24/24, T2m(controllo) 24/24, passo 1 h; ecmwf_ifs 2024-07-15: CAPE 0/24, CIN 0/24, LI 0/24, W500 0/24, T2m(controllo) 24/24, passo None h |
| s6_era5_cds | **OK** | 104 | 0.777 | single-levels: completata, 9/9 var, griglia 81×161, 39.3 s; pressure-levels: completata, 4/4 var, griglia 81×161, 64.4 s |
| s7_it_dpc_sri | **OK** | 4 | 17.9 | Sì: Zarr anonimo su EWC (s3 https://object-store.os-api.cci2.ecmwf.int), apertura lazy 2.07 s, timestep 2019-07-15T12:00 sul Lazio letto in 1.51 s trasferendo ~17.9 MB (1 chunk); Zenodo ha solo il tar.zst da 49.83 GB |
| s8a_imerg | **OK** | 7 | 7.86 | login OK; 3B-HHR.MS.MRG.3IMERG.20230715-S120000-E122959.0720.V07B.HDF5: 7.86 MB, variabile 'precipitation' (mm/hr), 0.1°; V08 su CMR: 0 granuli, versioni collezione ['07'] |
| s8b_huggingface | **OK** | 3 | 0 | dataset privato Fil728/ia-meteo-events: creato=True, privato=True, upload+verifica+cancellazione riusciti; file rimasti: ['.gitattributes'] |
| s9_toolchain | **OK** | 73 | 0 | Sì: LK + estrapolazione +30/+60 min in 0.1 s su [4, 80, 80], processo intero 72.6 s (incl. import e richiesta ERA5), picco RAM 327.4 MB; moto nubi fredde 64.9 km/h da 248°; ERA5 850–500 hPa 30.0 km/h da 269° |

Tempo = somma delle fasi misurate nello script (per S9 il processo intero, inclusa la richiesta ERA5 di confronto).

## 2. Risposte ai "da verificare" del planning

### Quote e rate limit API EUMETSAT

Nessun HTTP 429 in oltre 100 download (4 SEVIRI da 185 MB, 2×48 LI, 2×8 entry FCI, 2×61 richieste Range da 1 byte) né header `X-RateLimit-*` sulle API Data Store (browse/download). Data Tailor: quota disco utente 20 000 MB per gli output delle customizzazioni (endpoint quota, 08/10/2026). Nessun limite giornaliero documentato trovato dall'API stessa: resta da leggere la pagina ufficiale dei limiti.

### FCI: download per chunk e Data Tailor

- **Download per chunk: sì.** Ciclo 2025-07-15 14:00:07 – 2025-07-15 14:09:34. 61 entry (40 BODY, 1 TRAIL, resto quicklook/metadati). Chunk usati 31–37 + TRAIL = **180.4 MB** contro **1102.7 MB** del disco intero. Ritaglio ir_105: Tb 217.6–325.5 K, NaN 0.56% (sottile striscia sul bordo nord-ovest).
- Righe reali (ir_105, contate da sud) dei chunk usati: 31: 4196–4324, 32: 4325–4454, 33: 4455–4593, 34: 4594–4732, 35: 4733–4872, 36: 4873–5011, 37: 5012–5150.
- MB per chunk (1→40): 3.9, 10.2, 15.3, 19.3, 22.1, 24.3, 25.5, 26.9, 27.5, 27.9, 27.9, 29.6, 30.7, 32.5, 33.7, 33.2, 32.9, 32.5, 33.2, 35.0, 36.0, 36.3, 37.8, 37.1, 36.6, 37.7, 35.8, 34.8, 31.9, 29.1, 28.2, 26.4, 26.8, 25.5, 24.9, 24.6, 22.6, 18.3, 13.0, 4.6.
- **Data Tailor su FCI: sì** (prodotto `FCIL1FDHSI`, formato `netcdf4`, ROI dominio, banda `ir_105_effective_radiance`): stato DONE, 244.8 s lato server, output 5.5 MB. `FCIL1FDHSI_NATIVE` è rifiutato.

### Risoluzione della griglia LI

- sparsa (pixels) su griglia geostazionaria FCI 5568×5568 (area satpy mtg_fci_fdss_2km), ≈2 km al nadir, ~3-4 km a 40-45°N. Variabili: auxiliary_dataset_identifier, auxiliary_dataset_status, mtg_geos_projection, accumulation_start_times, accumulation_offsets, x, y, flash_accumulation, l1b_missing_warning, l1b_geolocation_warning, l1b_radiometric_warning, average_flash_qa.
- 20 accumuli per file, durata file 600 s, unità `flashes/pixel`. best_hour_utc (dominio) = **14**.

| Ora UTC | dominio | spagna | italia | balcani |
|:--|--:|--:|--:|--:|
| 10 | 19406.4 | 0.0 | 0.0 | 2132.5 |
| 11 | 34109.5 | 5.1 | 0.0 | 3979.3 |
| 12 | 33386.3 | 0.0 | 0.0 | 5283.5 |
| 13 | 32867.3 | 5.0 | 15.1 | 6593.2 |
| 14 | 36576.3 | 0.0 | 498.2 | 8049.0 |
| 15 | 30561.3 | 4.8 | 30.1 | 5977.3 |
| 16 | 23922.5 | 26.5 | 5.2 | 3039.1 |
| 17 | 24124.3 | 30.1 | 0.0 | 1009.9 |

Valori in flash×pixel per ora (somma di `flash_accumulation`).

### Copertura OPERA per zona (% pixel non `nodata`; `undetect` = valido)

| Composito | spagna | italia | balcani | lazio | tirreno | nord_africa | risoluzione | griglia | MB |
|:--|--:|--:|--:|--:|--:|--:|:--|:--|--:|
| 2019 ODYSSEY (20190715 120000) | 96.2 | 45.7 | 58.7 | 38.6 | 23.8 | 23.7 | 2 km | 1900×2200 | 0.75 |
| 2025 CIRRUS (20250715 140000) | 91.2 | 60.2 | 73.9 | 54.1 | 23.6 | 20.4 | 1 km | 3800×4400 | 2.64 |

Proiezione: `+proj=laea +lat_0=55.0 +lon_0=10.0 +x_0=1950000.0 +y_0=-2100000.0 +units=m +ellps=WGS84`.

### CAPE/CIN/livelli di pressione nello storico (Open-Meteo Historical Forecast)

Ore non nulle su 24, Roma Sud (41.73, 12.35):

| Modello giorno | cape | convective inhibition | lifted index | freezing level height | wind speed 500hPa | temperature 850hPa | relative humidity 700hPa | temperature 2m |
|:--|--:|--:|--:|--:|--:|--:|--:|--:|
| ecmwf_ifs 2019-07-15 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 24 |
| ecmwf_ifs025 2024-07-15 | 24 | 0 | 0 | 0 | 24 | 24 | 24 | 24 |
| ncep_gfs_seamless 2022-07-15 | 24 | 24 | 24 | 24 | 24 | 24 | 24 | 24 |
| gfs_seamless 2022-07-15 | 24 | 24 | 24 | 24 | 24 | 24 | 24 | 24 |
| ecmwf_ifs 2024-07-15 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 24 |

IFS HRES 9 km (`ecmwf_ifs`) nello storico non fornisce né CAPE/CIN né livelli di pressione (nemmeno nel 2024); `temperature_2m` è l'unico controllo pieno.

### `total_column_water_vapour` in ERA5

- Presente: **sì** (variabile `tcwv`).
- Single levels: cp, cape, cin, kx, totalx, tcwv, deg0l, blh, sst; griglia 81×161; coda 27.4 s, elaborazione 11.5 s, totale 39.3 s, 0.293 MB.
- Pressure levels: u, v, t, r a [850.0, 700.0, 500.0, 300.0] hPa; coda 38.8 s, totale 64.4 s, 0.484 MB.

### Accesso lazy a IT-DPC-SRI

- **Sì**: xarray.open_zarr + s3fs anonimo, endpoint https://object-store.os-api.cci2.ecmwf.int, s3://mlcast-source-datasets/IT-DPC-SRI/v0.1.0/italian-radar-dpc-sri.zarr.
- Dims {'time': 1039785, 'y': 1400, 'x': 1200, 'missing_times': 15530}; periodo ['2010-01-01T00:00', '2025-12-31T23:55']; chunk [1, 1400, 1200]; {'blocksize': 0, 'clevel': 9, 'cname': 'zstd', 'id': 'blosc', 'shuffle': 0}.
- Timestep 2019-07-15T12:00 sul Lazio ([187, 222] pixel) in 1.51 s: 0.17 MB di dati + 17.73 MB di coordinate/metadati (una tantum). Pioggia max 17.15 mm/h.

### Stato IMERG V08

- https://gpm.nasa.gov/data/news/imerg-v08-transition-schedule (28/04/2026): V07 Final termina a settembre 2025; V08 Final prevista nell'estate 2026, retroprocessata dal 1998; Late/Early in modalità 'hybrid V07'
- CMR (08/10/2026): granuli 15/07/2023 12 UTC per versione {'07': 1, '08': 0}; versioni della collezione `GPM_3IMERGHH`: ['07'] → **V08 non ancora pubblicata**.
- File V07 letto: `3B-HHR.MS.MRG.3IMERG.20230715-S120000-E122959.0720.V07B.HDF5`, 7.86 MB, variabile `precipitation` (mm/hr), 0.1°, ritaglio [400, 200].

## 3. Stime aggiornate per la Fase 2 (evento da 12 h)

- **SEVIRI** (48 slot da 15 min): scaricati 8.9 GB (zip 185 MB/slot; planning: ~116 MB e ~5,6 GB), conservati 62 MB per canale a 0,05° (1.3 MB/slot float32). Tempo ≈ 15 min in sequenza (17 s download + 1 s lettura per slot). Picco disco: 1 zip + 1 .nat ≈ 0,46 GB se si cancella slot per slot.
- **FCI** (72 cicli da 10 min): disco intero 79 GB; solo chunk 31–37 + TRAIL **13.0 GB**; conservati 94 MB per canale a 0,05°. Tempo ≈ 27 min in sequenza (20 s download + 2 s lettura per ciclo, misurati qui). Picco disco cancellando ciclo per ciclo ≈ 180 MB (sotto i 14 GB di un runner GitHub Actions). Con Data Tailor: ~5.5 MB/ciclo ma ~4 min/ciclo lato server (un canale, in radianza).
- **LI AF**: 167.5 MB per 8 h nel giorno di prova (solo BODY), 82 s.
- Rete: misure prese da una connessione domestica; i tempi su GitHub Actions saranno diversi.

## 4. Problemi incontrati e differenze rispetto alla documentazione

- **Ambiente (B2).** `pysteps` non esiste su conda-forge per osx-arm64: installato via pip. Il wheel va compilato con OpenMP, che il clang di Apple non ha → aggiunti all'ambiente `clang_osx-arm64` e `llvm-openmp` e `CC` puntato a quel clang (procedura in testa a `environment.yml`). pysteps LK richiede anche OpenCV (`py-opencv`), non elencato nel brief. Aggiunto `h5py` per ODIM. `pysteps.__version__` non esiste: versione letta da `importlib.metadata` (1.21.5).
- **SEVIRI.** Lo zip di un disco pesa ~185 MB (il .nat dentro 271 MB), non ~116 MB come nel planning.
- **LI AF.** In archivio i prodotti sono da 10 min (20 accumuli da 30 s), 1 BODY + 1 TRAIL; il BODY pesa da ~0,3 MB (mattina) a ~4 MB (pomeriggio convettivo). Griglia sparsa sulla griglia FCI 2 km. La prima geolocalizzazione (x/y decodificati in radianti + pyproj) dava una mappa specchiata est-ovest; adottata la convenzione del reader satpy `li_l2_nc` (indici interi, origine SW, area `mtg_fci_fdss_2km`). `flash_accumulation` somma flash×pixel, non flash distinti. `area.get_lonlat()` sull'intera griglia 5568² esaurisce gli 8 GB (OOM): si geolocalizzano solo i pixel attivi.
- **FCI.** Il prodotto ha 61 entry, non 41: 40 BODY + 1 TRAIL + 18 quicklook PNG/JPG + manifest + EOPMetadata. Somma entry 1103 MB contro 973 MB dichiarati dalla ricerca (`size`). Chunk numerati da sud (chunk 1) a nord (40); righe reali lette dai file confermano la stima geometrica. `Product.open(entry=...)` di eumdac scarica le singole entry. Il TRAIL non serve a satpy per leggere i BODY. Data Tailor: `FCIL1FDHSI_NATIVE` + `netcdf4` è rifiutato ("no suitable back-end has been found"); `FCIL1FDHSI` + `netcdf4` con banda `ir_105_effective_radiance` e ROI funziona (output in radianza, non Tb).
- **OPERA.** L'API EDR MeteoGate (anonima, 200 richieste/h, header `X-RateLimit-*`) serve solo la cache delle ultime 24 h: per il 15/07/2025 risponde 204. Lo storico è nel bucket S3 pubblico `openradar-archive` (CloudFerro, `--no-sign-request`), stesso schema di chiavi della cache. Nel 2019 il prodotto si chiama `DBZH_QIND` (DBZH + indice di qualità in due dataset ODIM 2.0, `product=COMP`, 15 min, 2 km, 1900×2200); nel 2025 `DBZH` (`product=MAX`, 5 min, 1 km, 3800×4400). Il composito si ferma a ~31,7°N (angolo SW del dominio LAEA). Il preprint IT-DPC-SRI conferma che l'Italia non partecipa al composito OPERA.
- **Open-Meteo.** L'ID documentato per GFS è `ncep_gfs_seamless`; `gfs_seamless` funziona comunque (alias, stessi valori). `ecmwf_ifs` (HRES 9 km) nello storico restituisce solo superficie: CAPE, CIN, LI, quota zero termico e livelli di pressione sono nulli sia nel 2019 sia nel 2024 (controllo: `temperature_2m` 24/24). `ecmwf_ifs025`: CAPE e livelli di pressione sì, CIN/LI/zero termico no; documentato 3-orario ma restituito orario (interpolato).
- **CDS.** Nomi variabili del brief tutti presenti nel `form.json` dei due dataset. La richiesta single-levels torna come zip di 2 netCDF (stepType diversi, per `convective_precipitation`) anche con `download_format=unarchived`. Nomi brevi nel netCDF: cape, cin, kx, totalx, tcwv, deg0l, blh, sst, cp. Il CDS avvisa che i Termini d'uso cambiano il 28/10/2026 (l'uso continuato vale come accettazione).
- **IT-DPC-SRI.** Su Zenodo c'è un solo `italian-radar-dpc-sri.zarr.tar.zst` (49,8 GB): niente lettura parziale da lì. Il preprint (§6.2) dà il percorso S3 anonimo sull'European Weather Cloud; `mlcast-datasets` non serve. Chunk Zarr = 1 timestep × intera Italia (1400×1200), ~0,17 MB compressi; la prima apertura costa ~18 MB (coordinate lat/lon 2D e asse tempo da 1 M valori). Unità `kg m-2 h-1` (= mm/h).
- **IMERG / HF.** La V08 di `GPM_3IMERGHH` non è ancora su CMR (0 granuli, collezione solo `07`). Il dataset HF è stato creato sotto l'utente del token (`Fil728/ia-meteo-events`), privato, ora contiene solo `.gitattributes`.
- **Gemini.** Le tre ricerche di documentazione delegate a Gemini (OPERA, FCI, IT-DPC-SRI) sono andate in timeout; la documentazione è stata letta direttamente.

## Anteprime

![s1_seviri_ir108](previews/s1_seviri_ir108.png)
![s2_fci_ir105](previews/s2_fci_ir105.png)
![s3_li_flashes](previews/s3_li_flashes.png)
![s4_opera_copertura](previews/s4_opera_copertura.png)
![s7_it_dpc_sri_lazio](previews/s7_it_dpc_sri_lazio.png)
![s8_imerg](previews/s8_imerg.png)
![s9_toolchain_moto](previews/s9_toolchain_moto.png)
