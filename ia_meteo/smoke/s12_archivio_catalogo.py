"""S12 — Archivio generico su HF: catalogo ERA5 della Fase 1 e ripresa dei lavori a sessioni.

1. Un mese di catalogo (luglio 2019: CAPE, CIN grezza, precipitazione convettiva, orari, dominio)
   con fonti.era5.catalogo_mese → archivio.carica_catalogo → verifica → file locale cancellato.
2. `presente()` lo riconosce come già fatto (così un lavoro a sessioni lo salta).
3. Riscaricamento in cache e confronto byte per byte; lettura e CIN con prepara_cin.
4. Una mini-finestra (15/07/2019 12:00–12:15) con il percorso finestre rifattorizzato, poi tolta.
5. Cache LRU: con il tetto abbassato a quasi zero il pezzo usato meno di recente viene cancellato.
Il mese di catalogo resta su HF: è il primo mese reale della Fase 1.
"""
import hashlib
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archivio as ar
import finestra as fw
from common import write_result
from fonti import era5

TEST = "s12_archivio_catalogo"
DOMANDA = ("Lo stesso archivio HF regge anche il catalogo ERA5 della Fase 1 (caricamento, verifica, "
           "ripresa a sessioni, cache a tetto fisso)?")
ANNO, MESE = 2019, 7
FID = "prova_s12_20190715T12"


def md5(f: Path) -> str:
    return hashlib.md5(f.read_bytes()).hexdigest()


def main():
    import xarray as xr
    a = ar.api()
    errori, mis, sec = [], {}, {}
    reg = ar.registro(a)
    chiave = ar.chiave_catalogo(ANNO, MESE)
    mis["gia_presente_prima"] = ar.presente("catalogo", chiave, reg)

    # 1. catalogo
    try:
        if not mis["gia_presente_prima"]:
            t0 = time.perf_counter()
            f, m = era5.catalogo_mese(ANNO, MESE, ar.STAGING / "catalogo", quiet=False)
            sec["catalogo_mese"] = round(time.perf_counter() - t0, 1)
            impronta = md5(f)
            mis["catalogo"] = m
            mis["upload_catalogo"] = ar.carica_catalogo(f, ANNO, MESE, voce=m, a=a)
            mis["catalogo_locale_cancellato"] = not f.exists()
        else:                                   # ripresa: misure dal registro, niente nuova richiesta CDS
            impronta = None
            mis["catalogo"] = {k: v for k, v in reg["catalogo"][chiave].items() if k not in ("file",)}
        # 2. ripresa
        mis["presente_dopo"] = ar.presente("catalogo", chiave)
        # 3. riscaricamento
        t0 = time.perf_counter()
        c = ar.scarica_catalogo(ANNO, MESE, a)
        sec["riscaricamento_catalogo"] = round(time.perf_counter() - t0, 1)
        mis["identico_dopo_riscaricamento"] = (md5(c) == impronta) if impronta else "già presente: confronto non fatto"
        with xr.open_dataset(c) as ds:
            ds = ds.load()
        mis["lettura"] = {"ore": int(ds.sizes["time"]), "variabili": sorted(ds.data_vars),
                          "primo_ultimo": [str(ds.time.values[0])[:13], str(ds.time.values[-1])[:13]],
                          "cape_max": round(float(ds.cape.max()), 0),
                          "cin_frac_definita": round(float(ds.cin.notnull().mean()), 3)}
        ds = era5.prepara_cin(ds)
        mis["dopo_prepara_cin"] = {"cin_nan": int(ds.cin.isnull().sum()), "cin_max": float(ds.cin.max())}
    except Exception as e:  # noqa: BLE001
        errori.append(f"catalogo: {e!r}"[:800])

    # 4. mini-finestra
    try:
        t0 = time.perf_counter()
        d = fw.costruisci(FID, datetime(2019, 7, 15, 12), datetime(2019, 7, 15, 12, 15))
        impr = {p.name: md5(p) for p in d.iterdir()}
        up = ar.carica_finestra(d, a=a)
        # scaricamento parziale su cache vuota: devono arrivare solo radar + meta
        shutil.rmtree(ar.CACHE, ignore_errors=True)
        ar.scarica_catalogo(ANNO, MESE, a)      # in cache prima della finestra: per la LRU è il più vecchio
        time.sleep(1)
        parziale = sorted(Path(x).name for x in ar.scarica("finestre", FID, lambda q: q.endswith(("radar.nc", "meta.json")), a))
        presenti = sorted(x.name for x in (ar.CACHE / "finestre" / FID).iterdir())
        cart = ar.scarica_finestra(FID, a=a)
        mis["finestra"] = {"upload": up, "staging_cancellato": not d.exists(),
                           "identica": {p.name: md5(p) for p in cart.iterdir()} == impr,
                           "solo_radar": {"restituiti": parziale, "in_cache": presenti},
                           "secondi": round(time.perf_counter() - t0, 1)}
    except Exception as e:  # noqa: BLE001
        errori.append(f"finestra: {e!r}"[:800])

    # 5. LRU: il catalogo è stato usato prima della finestra → è il primo a uscire
    tetto = ar.LIMITE_CACHE_GB
    try:
        ar.LIMITE_CACHE_GB = 0.005
        tolte = ar._pulisci_cache()
        mis["lru"] = {"tetto_prova_GB": ar.LIMITE_CACHE_GB, "tolte_in_ordine": tolte,
                      "cache_dopo_GB": ar.spazio_locale_gb()["cache_GB"]}
    finally:
        ar.LIMITE_CACHE_GB = tetto

    # pulizia: via la mini-finestra, compattazione; il catalogo resta
    try:
        ar.elimina_finestra(FID, a)
        ar.compatta_cronologia(a)
    except Exception as e:  # noqa: BLE001
        errori.append(f"pulizia: {e!r}"[:300])
    shutil.rmtree(ar.CACHE, ignore_errors=True)
    reg = ar.registro(a)
    mis["registro_finale"] = {s: list(reg[s]) for s in ar.SEZIONI}
    mis["spazio_hf"] = ar.spazio_hf_gb(a)
    mis["spazio_locale"] = ar.spazio_locale_gb()

    ok = (mis.get("presente_dopo") and mis.get("identico_dopo_riscaricamento") in (True, "già presente: confronto non fatto")
          and mis.get("finestra", {}).get("identica")
          and mis.get("finestra", {}).get("solo_radar", {}).get("in_cache") == ["meta.json", "radar.nc"] and not errori)
    esito = "OK" if ok else ("PARZIALE" if mis.get("presente_dopo") else "KO")
    c = mis.get("catalogo", {})
    stato_cat = ("già su HF da una sessione precedente: ripreso senza nuova richiesta CDS"
                 if mis["gia_presente_prima"] else "caricato, verificato, cancellato dal Mac e riscaricato identico")
    lru = (mis.get("lru", {}).get("tolte_in_ordine") or ["-"])[0]
    risposta = (f"Sì: catalogo {chiave} ({c.get('ore')} ore, {c.get('MB_file')} MB, CDS {c.get('secondi_cds')} s) "
                f"{stato_cat}; presente() lo riconosce; mini-finestra caricata e riscaricata identica (anche solo "
                f"radar); LRU ha tolto prima {lru}") if ok else "Ciclo non completato"
    write_result(TEST, esito, DOMANDA, risposta,
                 misure={"secondi": sec, "MB_scaricati": c.get("MB_scaricati", 0), "MB_conservati": 0, **mis},
                 errori=errori)


if __name__ == "__main__":
    main()
