"""S11 — Modalità d'archivio su Hugging Face con il Mac a tetto fisso (archivio.py, finestra.py).

Due finestre brevi che coprono tutti i percorsi del codice:
- A: 15/07/2019 12:00–13:00, SEVIRI + OPERA 2 km + ERA5 + IMERG + IT-DPC-SRI (Lazio);
- B: 15/07/2025 14:00–14:30, FCI per chunk + OPERA 1 km + ERA5 + IMERG + fulmini LI.
Per ciascuna: costruzione in staging → caricamento su HF (un commit) → verifica → cancellazione
locale → riscaricamento in cache → confronto con quanto caricato. Alla fine le finestre di prova
si tolgono dal dataset e la cronologia viene compattata, così lo spazio HF torna come prima.
"""
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archivio as ar
import finestra as fw
from common import write_result

TEST = "s11_archivio_hf"
DOMANDA = ("Si può lavorare con il Mac a tetto fisso (≤ 20 GB liberi) appoggiando l'archivio delle finestre "
           "sul dataset privato Hugging Face (~95 GB)?")
PROVE = [("prova_20190715T12", datetime(2019, 7, 15, 12), datetime(2019, 7, 15, 13), True),
         ("prova_20250715T14", datetime(2025, 7, 15, 14), datetime(2025, 7, 15, 14, 30), False)]


def impronte(cartella: Path) -> dict:
    return {f.name: hashlib.md5(f.read_bytes()).hexdigest() for f in sorted(cartella.iterdir()) if f.is_file()}


def main():
    a = ar.api()
    errori, ris = [], {}
    spazio_prima = ar.spazio_hf_gb(a)
    locale_prima = ar.spazio_locale_gb()
    for fid, t0, t1, lazio in PROVE:
        r = {}
        try:
            s = time.perf_counter()
            d = fw.costruisci(fid, t0, t1, lazio=lazio)
            r["secondi_costruzione"] = round(time.perf_counter() - s, 1)
            meta = json.loads((d / "meta.json").read_text())
            r["sorgenti"] = meta["file"]
            r["errori_sorgenti"] = meta["errori"]
            r["misure"] = meta["misure"]
            impr = impronte(d)
            r["MB_finestra"] = round(sum(f.stat().st_size for f in d.iterdir()) / 1e6, 2)
            r["picco_locale_GB"] = ar.spazio_locale_gb()
            up = ar.carica_finestra(d)
            r["upload"] = up
            r["staging_cancellato"] = not d.exists()
            s = time.perf_counter()
            c = ar.scarica_finestra(fid)
            r["secondi_riscaricamento"] = round(time.perf_counter() - s, 1)
            r["identica_dopo_riscaricamento"] = impronte(c) == impr
            # prova di lettura
            import xarray as xr
            with xr.open_dataset(c / "satellite.nc") as ds:
                v = list(ds.data_vars)[0]
                r["lettura_satellite"] = {"var": v, "time": int(ds.sizes["time"]),
                                          "min_max_K": [round(float(ds[v].min()), 1), round(float(ds[v].max()), 1)]}
            # scaricamento parziale: solo il radar
            ar._pulisci_cache()
        except Exception as e:  # noqa: BLE001
            errori.append(f"{fid}: {e!r}"[:800])
        ris[fid] = r
        print(fid, json.dumps(r, default=str)[:1500])

    spazio_con_prove = ar.spazio_hf_gb(a)
    reg = ar.registro(a)
    # pulizia: via le finestre di prova, compattazione della cronologia
    for fid, *_ in PROVE:
        try:
            ar.elimina_finestra(fid, a)
        except Exception as e:  # noqa: BLE001
            errori.append(f"elimina {fid}: {e!r}"[:300])
    try:
        ar.compatta_cronologia(a)
    except Exception as e:  # noqa: BLE001
        errori.append(f"compatta: {e!r}"[:300])
    import shutil
    shutil.rmtree(ar.CACHE, ignore_errors=True)
    time.sleep(5)
    spazio_dopo = ar.spazio_hf_gb(a)

    ok = all(r.get("identica_dopo_riscaricamento") and r.get("staging_cancellato") for r in ris.values())
    esito = "OK" if ok and not errori else ("PARZIALE" if any(ris.values()) else "KO")
    mbs = {k: v.get("MB_finestra") for k, v in ris.items()}
    risposta = (f"Sì: {len(ris)} finestre di prova costruite, caricate (1 commit ciascuna), verificate, cancellate dal "
                f"Mac e riscaricate identiche; MB per finestra {mbs}; spazio HF account {spazio_prima['account_GB']} → "
                f"{spazio_con_prove['account_GB']} → {spazio_dopo['account_GB']} GB dopo pulizia e compattazione"
                if ok else "Ciclo non completato")
    write_result(TEST, esito, DOMANDA, risposta,
                 misure={"secondi": {k: v.get("secondi_costruzione") for k, v in ris.items()},
                         "MB_scaricati": round(sum(sum(m.get("MB_scaricati", 0) for m in v.get("misure", {}).values())
                                                   for v in ris.values()), 1),
                         "MB_conservati": 0, "finestre": ris,
                         "registro_con_prove": list(reg.get("finestre", {})),
                         "hf_prima": spazio_prima, "hf_con_prove": spazio_con_prove, "hf_dopo": spazio_dopo,
                         "locale_prima": locale_prima, "locale_dopo": ar.spazio_locale_gb(),
                         "limiti": {"BUDGET_HF_GB": ar.BUDGET_HF_GB, "LIMITE_LOCALE_GB": ar.LIMITE_LOCALE_GB,
                                    "LIMITE_CACHE_GB": ar.LIMITE_CACHE_GB, "MARGINE_DISCO_GB": ar.MARGINE_DISCO_GB}},
                 errori=errori)


if __name__ == "__main__":
    main()
