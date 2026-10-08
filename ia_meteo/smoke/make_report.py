"""Genera smoke/REPORT.md leggendo smoke/results/*.json (Fase 0).

Le stime per i test reali vengono da results/stima_spazio.json (stima_spazio.py);
le note su problemi e differenze rispetto alla documentazione sono raccolte durante
l'esecuzione (07-08/10/2026) e stanno in NOTE_ESECUZIONE.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import RESULTS, SMOKE

ORDINE = ["s1_seviri", "s2_fci", "s3_li_flashes", "s4_opera", "s5_openmeteo_env", "s6_era5_cds",
          "s7_it_dpc_sri", "s8a_imerg", "s8b_huggingface", "s9_toolchain", "s10_cape_cin",
          "s11_archivio_hf", "s12_archivio_catalogo", "s13_cape_estremo",
          "s14_mlcape"]

QUOTA_EUMETSAT = ("Nessun HTTP 429 in oltre 200 download (SEVIRI, LI, entry FCI, richieste Range da 1 byte) "
                  "né header `X-RateLimit-*` sulle API Data Store. Data Tailor: quota disco utente 20 000 MB per "
                  "gli output delle customizzazioni (endpoint quota, 08/10/2026); 3 customizzazioni SEVIRI in "
                  "parallelo accettate (145 s in tutto). Un limite giornaliero ufficiale non è esposto dall'API.")

NOTE_ESECUZIONE = [
    "**Codice consolidato.** Tutte le soluzioni trovate stanno nel pacchetto `ia_meteo/fonti/` "
    "(`eumetsat`, `opera`, `era5`, `openmeteo`, `itdpc`, `imerg`); gli smoke test lo usano, e "
    "lo useranno i `fetch_*.py` della Fase 2. Dopo il consolidamento tutti i test sono stati rieseguiti "
    "con gli stessi risultati (08/10/2026).",
    "**Ambiente (B2).** `pysteps` non esiste su conda-forge per osx-arm64: installato via pip. Il wheel "
    "va compilato con OpenMP, che il clang di Apple non ha → aggiunti all'ambiente `clang_osx-arm64` e "
    "`llvm-openmp` e `CC` puntato a quel clang (procedura in testa a `environment.yml`). "
    "pysteps LK richiede anche OpenCV (`py-opencv`), non elencato nel brief. Aggiunto `h5py` per ODIM.",
    "**SEVIRI.** Lo zip di un disco pesa ~185 MB (il .nat dentro 271 MB), non ~116 MB come nel planning. "
    "Data Tailor su SEVIRI funziona (`HRSEVIRI`, `channel_5/6/9`, ROI dominio): 7,3 MB in ~55 s invece "
    "di 185 MB, ma l'output è in **radianza** e in proiezione geostazionaria: la conversione in Tb "
    "(calibrazione SEVIRI) va scritta e verificata contro satpy prima di usarlo.",
    "**LI AF.** In archivio i prodotti sono da 10 min (20 accumuli da 30 s), 1 BODY + 1 TRAIL; il BODY "
    "pesa da ~0,3 MB (mattina) a ~4 MB (pomeriggio convettivo). La prima geolocalizzazione (x/y in "
    "radianti + pyproj) dava una mappa specchiata est-ovest; adottata la convenzione del reader satpy "
    "`li_l2_nc`. `flash_accumulation` somma flash×pixel, non flash distinti. `area.get_lonlat()` "
    "sull'intera griglia 5568² esaurisce gli 8 GB: si geolocalizzano solo i pixel attivi.",
    "**FCI.** 61 entry per ciclo, non 41: 40 BODY + 1 TRAIL + 18 quicklook + manifest + EOPMetadata. "
    "Somma entry 1103 MB contro 973 MB dichiarati. Chunk numerati da sud (1) a nord (40); le righe "
    "reali lette dai file confermano la stima geometrica. Il TRAIL non serve a satpy. Data Tailor: "
    "`FCIL1FDHSI_NATIVE` + `netcdf4` rifiutato; `FCIL1FDHSI` + `netcdf4` + `ir_105_effective_radiance` "
    "funziona (radianza, ~4–5 min lato server).",
    "**OPERA.** L'API EDR MeteoGate (anonima, 200 richieste/h) serve solo la cache di 24 h (204 per il "
    "2025; il 08/10 ha risposto 429 a limite esaurito). Lo storico è nel bucket S3 pubblico "
    "`openradar-archive` (CloudFerro). 2019: `DBZH_QIND` (ODIM 2.0, DBZH e QIND in dataset separati, "
    "15 min, 2 km); 2025: `DBZH` (5 min, 1 km). Il composito si ferma a ~31,7°N. L'Italia non partecipa "
    "a OPERA (preprint IT-DPC-SRI). Sulla griglia comune si usa il massimo per cella (non la media).",
    "**Open-Meteo.** ID GFS documentato `ncep_gfs_seamless` (`gfs_seamless` è un alias). `ecmwf_ifs` "
    "storico: solo superficie (niente CAPE/CIN né livelli di pressione, anche nel 2024). `ecmwf_ifs025`: "
    "CAPE e livelli sì, CIN/LI/zero termico no.",
    "**ERA5 / CIN.** Documentazione ERA5: \"A missing value is assigned to CIN for values of CIN > 1000 "
    "or where there is no cloud base\". In S10 la CIN manca nel 46% dei punti con CAPE > 500 J/kg (fino "
    "a CAPE 4568 J/kg): il NaN non è \"niente inibizione\". Prima versione di `prepara_cin` riempiva con 0 "
    "(errato, corretto): ora NaN → 1000 J/kg + maschera `cin_definita`. La CAPE ERA5 è la most-unstable "
    "(particelle sotto 350 hPa). La single-levels torna come zip di 2 netCDF anche con "
    "`download_format=unarchived`. Il CDS cambia i Termini d'uso il 28/10/2026.",
    "**IT-DPC-SRI.** Su Zenodo c'è un solo tar.zst da 49,8 GB. Il preprint (§6.2) dà il percorso S3 "
    "anonimo sull'European Weather Cloud; `mlcast-datasets` non serve. Chunk = 1 timestep × intera "
    "Italia (~0,17 MB); la prima apertura costa ~18 MB di coordinate.",
    "**IMERG / HF.** V08 di `GPM_3IMERGHH` non ancora su CMR. Dataset HF `Fil728/ia-meteo-events` "
    "privato, contiene solo `.gitattributes`.",
    "**Gemini.** Le tre ricerche di documentazione delegate a Gemini sono andate in timeout; la "
    "documentazione è stata letta direttamente.",
]


def load():
    out = {}
    for t in ORDINE:
        p = RESULTS / f"{t}.json"
        out[t] = json.loads(p.read_text()) if p.exists() else None
    return out


def fmt_s(m):
    s = m.get("secondi")
    if isinstance(s, dict):
        if "totale_processo" in s:
            return f"{s['totale_processo']:.0f}"
        return f"{sum(v for v in s.values() if isinstance(v, (int, float))):.0f}"
    return str(s) if s is not None else "–"


def main():
    R = load()
    L = ["# IA meteo — Fase 0: smoke test", "",
         "Generato da `smoke/make_report.py` dai JSON in `smoke/results/`. Esecuzione 07–08/10/2026 "
         "sul Mac (Apple Silicon, 8 GB), ambiente conda `ia_meteo`, codice delle fonti in `ia_meteo/fonti/`. "
         "Nessuna conclusione sulle scelte del planning: solo misure e decisioni aperte.", "",
         "## 1. Tabella riassuntiva", "",
         "| Test | Esito | Tempo (s) | MB scaricati | Risposta |", "|:--|:--|--:|--:|:--|"]
    for t in ORDINE:
        r = R[t]
        if r is None:
            L.append(f"| {t} | NON ESEGUITO | – | – | – |")
            continue
        m = r["misure"]
        L.append(f"| {t} | **{r['esito']}** | {fmt_s(m)} | {m.get('MB_scaricati', '–')} | "
                 f"{r['risposta'].replace('|', '/')} |")
    L += ["", "Tempo = somma delle fasi misurate (S9: processo intero). Le richieste CDS ripetute possono "
          "uscire dalla cache del CDS e risultare più veloci della prima esecuzione.", ""]

    s1, s2, s3, s4, s5, s6, s7, s8, s10 = (R[k] for k in ("s1_seviri", "s2_fci", "s3_li_flashes", "s4_opera",
                                                           "s5_openmeteo_env", "s6_era5_cds", "s7_it_dpc_sri",
                                                           "s8a_imerg", "s10_cape_cin"))
    L += ["## 2. Risposte ai \"da verificare\" del planning", "",
          "### Quote e rate limit API EUMETSAT", "", QUOTA_EUMETSAT, ""]
    if s2:
        m = s2["misure"]
        dt = m.get("data_tailor") or {}
        L += ["### FCI: download per chunk e Data Tailor", "",
              f"- **Download per chunk: sì.** Ciclo {m['ciclo']}. {m['elenco_entry']['n_entry']} entry "
              f"({m['elenco_entry']['n_body']} BODY, {m['elenco_entry']['n_trail']} TRAIL, {m['elenco_entry']['n_altri']} "
              f"quicklook/metadati). Chunk {m['chunk_usati'][0]}–{m['chunk_usati'][-1]} = **{m['MB_chunk_necessari']} MB** "
              f"contro **{m['elenco_entry']['MB_disco_intero_somma_entry']} MB** del disco intero. Ritaglio 3 canali "
              f"(ir_105 {m['ritaglio']['ir_105']['min_K']}–{m['ritaglio']['ir_105']['max_K']} K), NaN "
              f"{100 * m['ritaglio']['frac_nan']:.2f}% (sottile striscia sul bordo nord-ovest), "
              f"{m['ritaglio']['MB_crop_3canali_compresso']} MB compresso.",
              "- Righe reali (ir_105, da sud): " + ", ".join(f"{k}: {v[0]}–{v[1]}" for k, v in m["righe_reali_chunk_ir105"].items()) + ".",
              "- MB per chunk (1→40): " + ", ".join(f"{v}" for v in m["elenco_entry"]["MB_per_chunk"].values()) + ".",
              f"- **Data Tailor su FCI: sì** (`FCIL1FDHSI`, `netcdf4`, ROI dominio, radianza ir_105): "
              f"{dt.get('stato')}, {dt.get('secondi')} s lato server, {dt.get('MB_output')} MB.", ""]
    if s1:
        dt = s1["misure"].get("data_tailor") or {}
        L += ["### Data Tailor su SEVIRI (non chiesto dal brief, utile per i volumi)", "",
              f"- `HRSEVIRI`, canali 5/6/9 (WV 6,2, WV 7,3, IR 10,8), ROI dominio: {dt.get('stato')}, "
              f"{dt.get('secondi')} s, **{dt.get('MB_output')} MB** contro ~185 MB dello zip. Output in radianza, "
              f"griglia geostazionaria {dt.get('dims')}. Conversione in Tb da implementare.", ""]
    if s3:
        sf = s3["misure"].get("struttura_file") or {}
        L += ["### Risoluzione della griglia LI", "",
              f"- {sf.get('griglia')}.",
              f"- {sf.get('accumulazioni_per_file')} accumuli per file, durata {sf.get('durata_file_s')} s, unità "
              f"`{sf.get('unita')}`. best_hour_utc = **{s3.get('best_hour_utc')}**. Sulla griglia 0,05° si "
              f"conserva il {100 * (s3['misure'].get('rapporto_griglia_su_totale') or 0):.2f}% dei flash del dominio.",
              "", "| Ora UTC | dominio | spagna | italia | balcani |", "|:--|--:|--:|--:|--:|"]
        for h, r in s3["misure"]["conteggi_per_ora_zona"].items():
            L.append(f"| {h} | {r.get('dominio')} | {r.get('spagna')} | {r.get('italia')} | {r.get('balcani')} |")
        L += ["", "Valori in flash×pixel per ora.", ""]
    if s4:
        c = s4["misure"]["compositi"]
        zone = list(next(iter(c.values()))["copertura_pct"])
        L += ["### Copertura OPERA per zona (% pixel non `nodata`; `undetect` = valido)", "",
              "| Composito | " + " | ".join(zone) + " | risoluzione | griglia | MB |",
              "|:--|" + "--:|" * len(zone) + ":--|:--|--:|"]
        for k, r in c.items():
            L.append(f"| {k} ({r['data_ora_odim']}) | " + " | ".join(str(r["copertura_pct"][z]) for z in zone)
                     + f" | {r['risoluzione_m'][0] / 1000:.0f} km | {r['griglia']} | {r['MB']} |")
        L += ["", f"Proiezione: `{next(iter(c.values()))['projdef']}`.", ""]
    if s5:
        tab = s5["misure"]["tabella_ore_non_nulle_su_24"]
        vs = ["cape", "convective_inhibition", "lifted_index", "freezing_level_height", "wind_speed_500hPa",
              "temperature_850hPa", "relative_humidity_700hPa", "temperature_2m"]
        L += ["### CAPE/CIN/livelli di pressione nello storico (Open-Meteo Historical Forecast)", "",
              "Ore non nulle su 24, Roma Sud (41.73, 12.35):", "",
              "| Modello giorno | " + " | ".join(v.replace("_", " ") for v in vs) + " |", "|:--|" + "--:|" * len(vs)]
        for k, r in tab.items():
            L.append(f"| {k} | " + " | ".join(str(r.get(v, "–")) for v in vs) + " |")
        L += [""]
    if s6:
        m = s6["misure"]
        L += ["### `total_column_water_vapour` in ERA5", "",
              f"- Presente: **{'sì' if s6.get('tcwv_presente') else 'no'}** (`tcwv`). Variabili di `fonti.era5.fetch_env`: "
              f"{', '.join(m.get('variabili', []))}; griglia {m.get('dimensioni')}.", ""]
    if s10:
        m = s10["misure"]
        L += ["### CAPE e CIN per l'addestramento (S10)", "",
              f"- {s10['risposta']}.",
              f"- {m.get('nota_cin_era5')}",
              "", "| Anno ora | CAPE max | CAPE>500 (% dominio) | CIN definita (% dominio) | CIN mancante dove CAPE>500 | CAPE max dove CIN manca |",
              "|:--|--:|--:|--:|--:|--:|"]
        for k, v in m["per_anno_ora"].items():
            L.append(f"| {k} | {v['cape_max']:.0f} | {100 * v['cape_frac>500']:.0f} | {100 * v['cin_frac_definita']:.0f} | "
                     f"{100 * v['cin_NaN_frac_dove_cape>500']:.0f}% | {v['cape_max_dove_cin_NaN']:.0f} |")
        L += ["", "Disponibilità in tempo reale (Forecast API, ore non nulle nelle prossime 24 h su Roma Sud):", "",
              "| Modello | CAPE | CIN |", "|:--|--:|--:|"]
        for k, v in m["forecast_24h_ore_non_nulle_roma"].items():
            L.append(f"| {k} | {v.get('cape', v.get('errore', '–'))} | {v.get('convective_inhibition', '–')} |")
        L += ["", "Convenzioni, Roma Sud 15/07 12 UTC (ERA5 most-unstable, CIN positiva o mancante; GFS "
              "Open-Meteo, CIN negativa):", "", "| Anno | ERA5 CAPE | GFS CAPE | ERA5 CIN | GFS CIN |", "|:--|--:|--:|--:|--:|"]
        for a, v in m["roma_12utc_ERA5_vs_GFS"].items():
            L.append(f"| {a} | {v.get('ERA5_cape')} | {v.get('GFS_cape')} | {v.get('ERA5_cin', '–') or 'mancante'} | {v.get('GFS_cin')} |")
        L += [""]
    s13 = R.get("s13_cape_estremo")
    if s13:
        pr = s13["misure"]["profili"]
        L += ["### Il massimo di CAPE del catalogo di luglio 2019 (S13)", "",
              f"- {s13['risposta']}.", "",
              "| Profilo | CAPE ERA5 | MetPy SB | MetPy ML100 | MetPy MU | Td 2 m / superficie (°C) | Td 925 hPa (°C) |",
              "|:--|--:|--:|--:|--:|--:|--:|"]
        for k, v in pr.items():
            if "metpy" not in v:
                continue
            mp = v["metpy"]
            L.append(f"| {k} | {v.get('cape_era5', '–')} | {mp['SB'].get('cape')} | {mp['ML100'].get('cape')} | "
                     f"{mp['MU'].get('cape')} | {v.get('d2m_C', v.get('td_sup_C'))} | {v.get('td_925_C')} |")
        rad = pr.get("radar OPERA attorno al punto", {})
        L += ["", "Radar OPERA entro 1° dal punto: " + ", ".join(f"{h} max {r['max_dBZ']} dBZ" for h, r in rad.items()) + ".", ""]
    s14 = R.get("s14_mlcape")
    if s14:
        stt = s14["misure"]["statistiche"]
        L += ["### CAPE dello strato rimescolato vettoriale (S14)", "",
              f"- {s14['risposta']}.",
              f"- Concordanza sul segno (CAPE > 0): {100 * stt['concordanza_cape>0']:.1f}%; errore CAPE assoluto mediano "
              f"{stt['cape_errore_assoluto_mediano']} J/kg (p95 {stt['cape_errore_assoluto_p95']}); CIN p95 "
              f"{stt['cin_errore_assoluto_p95']} J/kg. Leggera sovrastima sistematica (~2–3%).", ""]
    if s7:
        d = s7["misure"]["dataset"]
        L += ["### Accesso lazy a IT-DPC-SRI", "", f"- **Sì**: {s7['misure']['metodo']}.",
              f"- Dims {d.get('dims')}; periodo {d.get('periodo')}; chunk {d.get('chunk_zarr')}.",
              f"- Timestep {d.get('timestep_letto')} sul Lazio ({d.get('shape_ritaglio')} px) in "
              f"{s7['misure']['secondi'].get('lettura_timestep_lazio')} s: {d.get('MB_chunk_dati')} MB di dati "
              f"+ {d.get('MB_metadati_e_coordinate')} MB di coordinate (una tantum).", ""]
    if s8:
        m = s8["misure"]
        L += ["### Stato IMERG V08", "", f"- {s8['note'][0]}",
              f"- CMR (08/10/2026): granuli per versione {m.get('granuli_15_07_2023_12UTC_per_versione')}; versioni "
              f"collezione {m.get('collezioni_GPM_3IMERGHH')} → **V08 non ancora pubblicata**.", ""]

    # 3. stima di spazio
    st = json.loads((RESULTS / "stima_spazio.json").read_text()) if (RESULTS / "stima_spazio.json").exists() else None
    if st:
        p, fr = st["parametri"], st["MB_per_frame_compresso"]
        L += ["## 3. Spazio e tempi per i test reali (da `stima_spazio.py`)", "",
              f"Parametri del planning: finestre da 12 h, {p['finestre'][0]}–{p['finestre'][1]} finestre, griglia 0,05°, "
              f"canali IR 10,8 + WV 6,2/7,3. Ipotesi esplicite: {100 * p['quota_fci']:.0f}% finestre FCI (2025), "
              f"{100 * p['quota_lazio']:.0f}% laziali con IT-DPC-SRI, {p['worker']} download in parallelo.", "",
              "**Peso compresso di un frame sulla griglia comune** (MB): "
              + ", ".join(f"{k} {v}" for k, v in fr.items()) + ".", "",
              "| Per finestra da 12 h | conservati float32 (MB) | conservati int16 (MB) |", "|:--|--:|--:|"]
        for k in st["MB_per_finestra_float32"]:
            L.append(f"| {k} | {st['MB_per_finestra_float32'][k]} | {st['MB_per_finestra_int16'][k]} |")
        dl = st["MB_scaricati_per_finestra"]
        L += ["", f"Scaricati per finestra: SEVIRI disco intero {dl['seviri_disco_intero']} MB "
              f"(planning: ~5 600), SEVIRI via Data Tailor {dl['seviri_data_tailor']} MB, FCI solo chunk "
              f"{dl['fci_chunk']} MB, FCI disco intero {dl['fci_disco_intero']} MB.", "",
              "| Scenario | finestre SEVIRI / FCI / Lazio | conservati float32 | conservati int16 | scaricati (rete) | "
              "scaricati con Data Tailor SEVIRI | ore di download (3 worker) |", "|:--|:--|--:|--:|--:|--:|--:|"]
        for n, s in st["scenari"].items():
            f = s["finestre"]
            L.append(f"| {n} finestre | {f['seviri']} / {f['fci']} / {f['lazio']} | {s['GB_conservati_float32']} GB | "
                     f"{s['GB_conservati_int16']} GB | {s['GB_scaricati_disco_intero_seviri_chunk_fci']:.0f} GB | "
                     f"{s['GB_scaricati_con_data_tailor_seviri']:.0f} GB | {s['ore_download_con_worker']:.0f} h |")
        s300 = st["scenari"]["300"]
        L += ["", f"Le ore con {p['worker']} worker presuppongono che la linea regga {p['worker']} download "
              f"insieme (~9 MB/s ciascuno misurati qui); in sequenza sono "
              f"{st['scenari']['150']['ore_download_sequenziali']:.0f} h (150) e "
              f"{s300['ore_download_sequenziali']:.0f} h (300). Il Data Tailor SEVIRI riduce la rete di ~25 volte "
              "ma costa ~50 s di elaborazione lato server per slot.", "", "**Spazio sul Mac** (i dati scaricati non restano: ogni slot si ritaglia e si cancella):", "",
              f"- Lavoro durante il download: ~{st['picco_disco_lavoro_GB']} GB ({p['worker']} worker × zip+.nat SEVIRI).",
              f"- Catalogo eventi ERA5 della Fase 1 (CAPE, CIN, precipitazione convettiva, orari, apr–nov 2015–2025): "
              f"~{st['catalogo_era5_fase1_GB']} GB (88 mesi; misurato su un mese reale in S12), da tenere su HF: sul Mac "
              f"c'è al più un mese. Tempo CDS in sequenza ~{st.get('catalogo_era5_fase1_ore_cds_in_sequenza')} h.",
              f"- Ambiente conda: ~{st['ambiente_conda_GB']} GB (già installato).",
              f"- Dataset ritagliato: da {st['scenari']['150']['GB_conservati_int16']} GB (150 finestre, int16) a "
              f"{s300['GB_conservati_float32']} GB (300 finestre, float32) se resta tutto in locale; "
              "~0 se ogni finestra va su Hugging Face (privato, limite 100 GB) e si cancella dal Mac.",
              "", ""]

    s11 = R.get("s11_archivio_hf")
    if s11:
        m = s11["misure"]
        lim = m["limiti"]
        L += ["## 3b. Modalità d'archivio: Mac a tetto fisso, finestre su Hugging Face (S11)", "",
              "`finestra.py` costruisce la finestra in `data/staging/<id>/` scaricando un prodotto alla volta e "
              "cancellando il grezzo; `archivio.carica_finestra` la carica con un commit (più `registro.json`), "
              "verifica nomi e dimensioni sul repo e cancella la copia locale; `archivio.scarica_finestra` la "
              "riporta in una cache locale a tetto fisso. Limiti controllati prima di ogni passo: HF "
              f"{lim['BUDGET_HF_GB']} GB (account, cronologia inclusa), `ia_meteo/data/` {lim['LIMITE_LOCALE_GB']} GB, "
              f"cache {lim['LIMITE_CACHE_GB']} GB, disco libero minimo {lim['MARGINE_DISCO_GB']} GB.", "",
              f"- Esito: **{s11['esito']}**. {s11['risposta']}.", "",
              "| Finestra | sorgenti | MB su HF | costruzione (s) | upload (s) | riscaricata identica |",
              "|:--|:--|--:|--:|--:|:--|"]
        for k, v in m["finestre"].items():
            L.append(f"| {k} | {', '.join(v.get('sorgenti', {}))} | {v.get('MB_finestra')} | "
                     f"{v.get('secondi_costruzione')} | {(v.get('upload') or {}).get('secondi_upload')} | "
                     f"{v.get('identica_dopo_riscaricamento')} |")
        L += ["", "Dettaglio per sorgente (n frame, MB scaricati, MB nel file, secondi):", ""]
        for k, v in m["finestre"].items():
            L.append(f"- `{k}`: " + "; ".join(f"{sn} {mm.get('n', mm.get('ore'))} / {mm.get('MB_scaricati')} / "
                                             f"{mm.get('MB_file')} / {mm.get('secondi')}" for sn, mm in v.get("misure", {}).items())
                     + (f"; errori: {v['errori_sorgenti']}" if v.get("errori_sorgenti") else ""))
        L += ["", "Su HF i file eliminati restano nella cronologia: `archivio.compatta_cronologia()` la riduce a "
              "un commit. Il conteggio `used_storage` di HF si aggiorna in differita: durante S11 è rimasto a "
              f"{m['hf_dopo']['account_GB']} GB sia con le finestre caricate sia dopo la pulizia. Per questo "
              "`controlla_budget` usa il massimo tra il valore HF e la somma delle voci nel registro.", ""]
    s12 = R.get("s12_archivio_catalogo")
    if s12:
        m = s12["misure"]
        L += ["### Catalogo della Fase 1 sullo stesso archivio (S12)", "",
              f"- Esito: **{s12['esito']}**. {s12['risposta']}.",
              f"- Lettura: {m.get('lettura')}; dopo `prepara_cin`: {m.get('dopo_prepara_cin')}.",
              f"- Cache LRU con tetto di prova {m.get('lru', {}).get('tetto_prova_GB')} GB: tolti in ordine "
              f"{m.get('lru', {}).get('tolte_in_ordine')}.",
              f"- Registro finale: {m.get('registro_finale')}.", ""]

    L += ["## 4. Problemi incontrati e differenze rispetto alla documentazione", ""]
    L += [f"- {n}" for n in NOTE_ESECUZIONE]
    err = [(t, e) for t in ORDINE if R[t] for e in R[t].get("errori", [])]
    if err:
        L += ["", "Errori registrati nei JSON:", ""] + [f"- `{t}`: {e[:300]}" for t, e in err]
    L += ["", "## Anteprime", ""]
    for p_ in sorted((SMOKE / "previews").glob("*.png")):
        L.append(f"![{p_.stem}](previews/{p_.name})")
    (SMOKE / "REPORT.md").write_text("\n".join(L) + "\n")
    print("scritto", SMOKE / "REPORT.md")


if __name__ == "__main__":
    main()
