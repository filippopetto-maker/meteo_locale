"""Genera smoke/REPORT.md leggendo smoke/results/*.json (Fase 0).

Le stime Fase 2 sono calcolate dalle misure dei JSON; le note su problemi e
differenze rispetto alla documentazione sono raccolte durante l'esecuzione
(07-08/10/2026) e stanno in NOTE_ESECUZIONE qui sotto.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import RESULTS, SMOKE

ORDINE = ["s1_seviri", "s2_fci", "s3_li_flashes", "s4_opera", "s5_openmeteo_env", "s6_era5_cds",
          "s7_it_dpc_sri", "s8a_imerg", "s8b_huggingface", "s9_toolchain"]

QUOTA_EUMETSAT = ("Nessun HTTP 429 in oltre 100 download (4 SEVIRI da 185 MB, 2×48 LI, 2×8 entry FCI, "
                  "2×61 richieste Range da 1 byte) né header `X-RateLimit-*` sulle API Data Store "
                  "(browse/download). Data Tailor: quota disco utente 20 000 MB per gli output "
                  "delle customizzazioni (endpoint quota, 08/10/2026). Nessun limite giornaliero "
                  "documentato trovato dall'API stessa: resta da leggere la pagina ufficiale dei limiti.")

NOTE_ESECUZIONE = [
    "**Ambiente (B2).** `pysteps` non esiste su conda-forge per osx-arm64: installato via pip. Il wheel "
    "va compilato con OpenMP, che il clang di Apple non ha → aggiunti all'ambiente `clang_osx-arm64` e `llvm-openmp` "
    "e `CC` puntato a quel clang (procedura in testa a `environment.yml`). "
    "pysteps LK richiede anche OpenCV (`py-opencv`), non elencato nel brief. Aggiunto `h5py` per ODIM. "
    "`pysteps.__version__` non esiste: versione letta da `importlib.metadata` (1.21.5).",
    "**SEVIRI.** Lo zip di un disco pesa ~185 MB (il .nat dentro 271 MB), non ~116 MB come nel planning.",
    "**LI AF.** In archivio i prodotti sono da 10 min (20 accumuli da 30 s), 1 BODY + 1 TRAIL; il BODY "
    "pesa da ~0,3 MB (mattina) a ~4 MB (pomeriggio convettivo). Griglia sparsa sulla griglia FCI 2 km. "
    "La prima geolocalizzazione (x/y decodificati in radianti + pyproj) dava una mappa specchiata "
    "est-ovest; adottata la convenzione del reader satpy `li_l2_nc` (indici interi, origine SW, area "
    "`mtg_fci_fdss_2km`). `flash_accumulation` somma flash×pixel, non flash distinti. "
    "`area.get_lonlat()` sull'intera griglia 5568² esaurisce gli 8 GB (OOM): si geolocalizzano solo i pixel attivi.",
    "**FCI.** Il prodotto ha 61 entry, non 41: 40 BODY + 1 TRAIL + 18 quicklook PNG/JPG + manifest + "
    "EOPMetadata. Somma entry 1103 MB contro 973 MB dichiarati dalla ricerca (`size`). "
    "Chunk numerati da sud (chunk 1) a nord (40); righe reali lette dai file confermano la stima "
    "geometrica. `Product.open(entry=...)` di eumdac scarica le singole entry. "
    "Il TRAIL non serve a satpy per leggere i BODY. Data Tailor: `FCIL1FDHSI_NATIVE` + `netcdf4` è "
    "rifiutato (\"no suitable back-end has been found\"); `FCIL1FDHSI` + `netcdf4` con banda "
    "`ir_105_effective_radiance` e ROI funziona (output in radianza, non Tb).",
    "**OPERA.** L'API EDR MeteoGate (anonima, 200 richieste/h, header `X-RateLimit-*`) serve solo "
    "la cache delle ultime 24 h: per il 15/07/2025 risponde 204. Lo storico è nel bucket S3 pubblico "
    "`openradar-archive` (CloudFerro, `--no-sign-request`), stesso schema di chiavi della cache. "
    "Nel 2019 il prodotto si chiama `DBZH_QIND` (DBZH + indice di qualità in due dataset ODIM 2.0, "
    "`product=COMP`, 15 min, 2 km, 1900×2200); nel 2025 `DBZH` (`product=MAX`, 5 min, 1 km, 3800×4400). "
    "Il composito si ferma a ~31,7°N (angolo SW del dominio LAEA). Il preprint IT-DPC-SRI conferma che "
    "l'Italia non partecipa al composito OPERA.",
    "**Open-Meteo.** L'ID documentato per GFS è `ncep_gfs_seamless`; `gfs_seamless` funziona comunque "
    "(alias, stessi valori). `ecmwf_ifs` (HRES 9 km) nello storico restituisce solo superficie: CAPE, "
    "CIN, LI, quota zero termico e livelli di pressione sono nulli sia nel 2019 sia nel 2024 "
    "(controllo: `temperature_2m` 24/24). `ecmwf_ifs025`: CAPE e livelli di pressione sì, CIN/LI/"
    "zero termico no; documentato 3-orario ma restituito orario (interpolato).",
    "**CDS.** Nomi variabili del brief tutti presenti nel `form.json` dei due dataset. La richiesta "
    "single-levels torna come zip di 2 netCDF (stepType diversi, per `convective_precipitation`) anche con "
    "`download_format=unarchived`. Nomi brevi nel netCDF: cape, cin, kx, totalx, tcwv, deg0l, blh, sst, cp. "
    "Il CDS avvisa che i Termini d'uso cambiano il 28/10/2026 (l'uso continuato vale come accettazione).",
    "**IT-DPC-SRI.** Su Zenodo c'è un solo `italian-radar-dpc-sri.zarr.tar.zst` (49,8 GB): niente lettura "
    "parziale da lì. Il preprint (§6.2) dà il percorso S3 anonimo sull'European Weather Cloud; "
    "`mlcast-datasets` non serve. Chunk Zarr = 1 timestep × intera Italia (1400×1200), ~0,17 MB "
    "compressi; la prima apertura costa ~18 MB (coordinate lat/lon 2D e asse tempo da 1 M valori). "
    "Unità `kg m-2 h-1` (= mm/h).",
    "**IMERG / HF.** La V08 di `GPM_3IMERGHH` non è ancora su CMR (0 granuli, collezione solo `07`). "
    "Il dataset HF è stato creato sotto l'utente del token (`Fil728/ia-meteo-events`), privato, ora "
    "contiene solo `.gitattributes`.",
    "**Gemini.** Le tre ricerche di documentazione delegate a Gemini (OPERA, FCI, IT-DPC-SRI) sono "
    "andate in timeout; la documentazione è stata letta direttamente.",
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
        tot = sum(v for v in s.values() if isinstance(v, (int, float)))
        if "totale_processo" in s:
            tot = s["totale_processo"]
        return f"{tot:.0f}"
    return str(s) if s is not None else "–"


def main():
    R = load()
    L = ["# IA meteo — Fase 0: smoke test", "",
         "Generato da `smoke/make_report.py` dai JSON in `smoke/results/`. "
         "Esecuzione 07–08/10/2026 sul Mac (Apple Silicon, 8 GB), ambiente conda `ia_meteo`. "
         "Nessuna conclusione sulle scelte del planning: solo misure.", "",
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
    L += ["", "Tempo = somma delle fasi misurate nello script (per S9 il processo intero, inclusa la "
          "richiesta ERA5 di confronto).", ""]

    # 2. da verificare
    s1, s2, s3, s4, s5, s6, s7, s8 = (R["s1_seviri"], R["s2_fci"], R["s3_li_flashes"], R["s4_opera"],
                                      R["s5_openmeteo_env"], R["s6_era5_cds"], R["s7_it_dpc_sri"], R["s8a_imerg"])
    L += ["## 2. Risposte ai \"da verificare\" del planning", ""]
    L += ["### Quote e rate limit API EUMETSAT", "", QUOTA_EUMETSAT, ""]
    if s2:
        m = s2["misure"]
        dt = m.get("data_tailor") or {}
        L += ["### FCI: download per chunk e Data Tailor", "",
              f"- **Download per chunk: sì.** Ciclo {m['ciclo']}. {m['elenco_entry']['n_entry']} entry "
              f"({m['elenco_entry']['n_body']} BODY, {m['elenco_entry']['n_trail']} TRAIL, resto quicklook/metadati). "
              f"Chunk usati {m['chunk_usati'][0]}–{m['chunk_usati'][-1]} + TRAIL = "
              f"**{m['MB_chunk_necessari_con_trail']} MB** contro **{m['elenco_entry']['MB_disco_intero_somma_entry']} MB** "
              f"del disco intero. Ritaglio ir_105: Tb {m['ritaglio'].get('Tb_min_K')}–{m['ritaglio'].get('Tb_max_K')} K, "
              f"NaN {100 * m['ritaglio'].get('frac_nan', 0):.2f}% (sottile striscia sul bordo nord-ovest).",
              f"- Righe reali (ir_105, contate da sud) dei chunk usati: "
              + ", ".join(f"{k}: {v[0]}–{v[1]}" for k, v in m["righe_reali_chunk_ir105"].items()) + ".",
              "- MB per chunk (1→40): " + ", ".join(f"{v}" for v in m["elenco_entry"]["MB_per_chunk"].values()) + ".",
              f"- **Data Tailor su FCI: sì** (prodotto `FCIL1FDHSI`, formato `netcdf4`, ROI dominio, banda "
              f"`ir_105_effective_radiance`): stato {dt.get('stato')}, {dt.get('secondi')} s lato server, "
              f"output {dt.get('MB_output')} MB. `FCIL1FDHSI_NATIVE` è rifiutato.", ""]
    if s3:
        sf = s3["misure"].get("struttura_file") or {}
        L += ["### Risoluzione della griglia LI", "",
              f"- {sf.get('griglia')}. Variabili: {', '.join(sf.get('variabili', []))}.",
              f"- {sf.get('accumulazioni_per_file')} accumuli per file, durata file {sf.get('durata_file_s')} s, "
              f"unità `{sf.get('unita')}`. best_hour_utc (dominio) = **{s3.get('best_hour_utc')}**.",
              "", "| Ora UTC | dominio | spagna | italia | balcani |", "|:--|--:|--:|--:|--:|"]
        for h, r in s3["misure"]["conteggi_per_ora_zona"].items():
            L.append(f"| {h} | {r.get('dominio')} | {r.get('spagna')} | {r.get('italia')} | {r.get('balcani')} |")
        L += ["", "Valori in flash×pixel per ora (somma di `flash_accumulation`).", ""]
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
              "| Modello giorno | " + " | ".join(v.replace("_", " ") for v in vs) + " |",
              "|:--|" + "--:|" * len(vs)]
        for k, r in tab.items():
            L.append(f"| {k} | " + " | ".join(str(r.get(v, "–")) for v in vs) + " |")
        L += ["", "IFS HRES 9 km (`ecmwf_ifs`) nello storico non fornisce né CAPE/CIN né livelli di pressione "
              "(nemmeno nel 2024); `temperature_2m` è l'unico controllo pieno.", ""]
    if s6:
        rq = s6["misure"]["richieste"]
        sl = rq.get("reanalysis-era5-single-levels", {})
        pl = rq.get("reanalysis-era5-pressure-levels", {})
        L += ["### `total_column_water_vapour` in ERA5", "",
              f"- Presente: **{'sì' if s6.get('tcwv_presente') else 'no'}** (variabile `tcwv`).",
              f"- Single levels: {', '.join(sl.get('variabili', []))}; griglia "
              f"{sl.get('dimensioni', {}).get('latitude')}×{sl.get('dimensioni', {}).get('longitude')}; "
              f"coda {sl.get('secondi_in_coda')} s, elaborazione {sl.get('secondi_elaborazione')} s, totale "
              f"{sl.get('secondi_totali')} s, {sl.get('MB')} MB.",
              f"- Pressure levels: {', '.join(pl.get('variabili', []))} a {pl.get('pressure_level')} hPa; coda "
              f"{pl.get('secondi_in_coda')} s, totale {pl.get('secondi_totali')} s, {pl.get('MB')} MB.", ""]
    if s7:
        d = s7["misure"]["dataset"]
        L += ["### Accesso lazy a IT-DPC-SRI", "",
              f"- **Sì**: {s7['misure']['metodo']}.",
              f"- Dims {d.get('dims')}; periodo {d.get('periodo')}; chunk {d.get('chunk_zarr')}; {d.get('compressore')}.",
              f"- Timestep {d.get('timestep_letto')} sul Lazio ({d.get('shape_ritaglio')} pixel) in "
              f"{s7['misure']['secondi'].get('lettura_timestep_lazio')} s: {d.get('MB_chunk_dati')} MB di dati "
              f"+ {d.get('MB_metadati_e_coordinate')} MB di coordinate/metadati (una tantum). "
              f"Pioggia max {d.get('pioggia_mm_h', {}).get('max')} mm/h.", ""]
    if s8:
        m = s8["misure"]
        L += ["### Stato IMERG V08", "",
              f"- {s8['note'][0]}",
              f"- CMR (08/10/2026): granuli 15/07/2023 12 UTC per versione {m.get('granuli_15_07_2023_12UTC_per_versione')}; "
              f"versioni della collezione `GPM_3IMERGHH`: {m.get('collezioni_GPM_3IMERGHH')} → **V08 non ancora pubblicata**.",
              f"- File V07 letto: `{m.get('file')}`, {m.get('MB')} MB, variabile `{m.get('variabile')}` "
              f"({m.get('unita')}), {m.get('risoluzione_gradi')}°, ritaglio {m.get('shape_ritaglio')}.", ""]

    # 3. stime Fase 2
    L += ["## 3. Stime aggiornate per la Fase 2 (evento da 12 h)", ""]
    if s1:
        pr = [p for p in s1["misure"]["prodotti"] if "MB_zip" in p]
        zip_mb = sum(p["MB_zip"] for p in pr) / len(pr)
        dl_s = sum(p["sec_download"] for p in pr) / len(pr)
        rd_s = sorted(p["sec_lettura_ritaglio"] for p in pr)[len(pr) // 2]
        crop = pr[0]["MB_crop"]
        n = 48
        L += [f"- **SEVIRI** (48 slot da 15 min): scaricati {n * zip_mb / 1000:.1f} GB "
              f"(zip {zip_mb:.0f} MB/slot; planning: ~116 MB e ~5,6 GB), conservati "
              f"{n * crop:.0f} MB per canale a 0,05° ({crop} MB/slot float32). Tempo ≈ "
              f"{n * (dl_s + rd_s) / 60:.0f} min in sequenza ({dl_s:.0f} s download + {rd_s:.0f} s lettura per slot). "
              "Picco disco: 1 zip + 1 .nat ≈ 0,46 GB se si cancella slot per slot."]
    if s2:
        m = s2["misure"]
        n = 72
        full = m["elenco_entry"]["MB_disco_intero_somma_entry"]
        part = m["MB_chunk_necessari_con_trail"]
        dl = m["secondi"].get("download_chunk", 0)
        rd = m["secondi"].get("lettura_ritaglio", 0)
        crop = m["ritaglio"].get("MB_crop", 0)
        L += [f"- **FCI** (72 cicli da 10 min): disco intero {n * full / 1000:.0f} GB; solo chunk {m['chunk_usati'][0]}–"
              f"{m['chunk_usati'][-1]} + TRAIL **{n * part / 1000:.1f} GB**; conservati {n * crop:.0f} MB per canale "
              f"a 0,05°. Tempo ≈ {n * (dl + rd) / 60:.0f} min in sequenza ({dl:.0f} s download + {rd:.0f} s lettura per ciclo, "
              f"misurati qui). Picco disco cancellando ciclo per ciclo ≈ {part:.0f} MB (sotto i 14 GB di un runner GitHub "
              f"Actions). Con Data Tailor: ~{m['data_tailor'].get('MB_output')} MB/ciclo ma "
              f"~{m['data_tailor'].get('secondi', 0) / 60:.0f} min/ciclo lato server (un canale, in radianza)."]
    if s3:
        L += [f"- **LI AF**: {s3['misure']['MB_scaricati']} MB per 8 h nel giorno di prova (solo BODY), "
              f"{s3['misure']['secondi'].get('download', 0) + s3['misure']['secondi'].get('lettura', 0):.0f} s."]
    L += ["- Rete: misure prese da una connessione domestica; i tempi su GitHub Actions saranno diversi.", ""]

    # 4. problemi
    L += ["## 4. Problemi incontrati e differenze rispetto alla documentazione", ""]
    L += [f"- {n}" for n in NOTE_ESECUZIONE]
    err = [(t, e) for t in ORDINE if R[t] for e in R[t].get("errori", [])]
    if err:
        L += ["", "Errori registrati nei JSON:", ""] + [f"- `{t}`: {e[:300]}" for t, e in err]
    L += ["", "## Anteprime", ""]
    for p in sorted((SMOKE / "previews").glob("*.png")):
        L.append(f"![{p.stem}](previews/{p.name})")
    (SMOKE / "REPORT.md").write_text("\n".join(L) + "\n")
    print("scritto", SMOKE / "REPORT.md")


if __name__ == "__main__":
    main()
