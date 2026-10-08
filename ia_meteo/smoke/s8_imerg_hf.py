"""S8 — IMERG (fonti.imerg, NASA Earthdata) e Hugging Face (dataset privato).

IMERG: login da ~/.netrc, un file Final V07 half-hourly (GPM_3IMERGHH) del
15/07/2023 12:00 UTC, ritaglio su DOMAIN_BBOX; controllo su CMR se esiste una V08.
Pagina NASA "IMERG V08 Transition Schedule" (28/04/2026): V07 Final termina a
settembre 2025; V08 Final prevista per l'estate 2026, retroprocessata dal 1998.

Hugging Face: crea (o ritrova) il dataset privato `ia-meteo-events`, carica
smoke/results/s1_seviri.json, verifica, cancella il file (il dataset resta vuoto).
"""
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DOMAIN_BBOX, PREVIEWS, RAW, RESULTS, env, mb, rm, write_result
from fonti import imerg as im

DOMANDA_I = "Il login Earthdata funziona e un file IMERG Final V07 si legge?"
DOMANDA_H = "Possiamo scrivere sul dataset privato Hugging Face?"
V08_URL = "https://gpm.nasa.gov/data/news/imerg-v08-transition-schedule"
T0, T1 = datetime(2023, 7, 15, 12, 0), datetime(2023, 7, 15, 12, 30)


def imerg():
    import earthaccess
    sec, errori, mis = {}, [], {}
    if not Path("~/.netrc").expanduser().exists():
        return write_result("s8a_imerg", "SALTATO", DOMANDA_I, "Credenziale assente (~/.netrc)")
    out = RAW / "s8"
    try:
        t0 = time.perf_counter()
        auth = im.login()
        sec["login"] = round(time.perf_counter() - t0, 2)
        mis["login_ok"] = bool(auth and auth.authenticated)
        mis["granuli_15_07_2023_12UTC_per_versione"] = {v: len(im.granuli(T0, T1, v)) for v in ("07", "08")}
        try:
            cols = earthaccess.search_datasets(short_name=im.SHORT_NAME)
            mis["collezioni_GPM_3IMERGHH"] = sorted({c["umm"].get("Version") for c in cols})
        except Exception as e:  # noqa: BLE001
            mis["collezioni_GPM_3IMERGHH"] = f"errore: {e!r}"[:200]
        t0 = time.perf_counter()
        f = im.scarica(im.granuli(T0, T1, "07")[:1], out)[0]
        sec["download"] = round(time.perf_counter() - t0, 2)
        sub, var, sizes = im.ritaglio(f, DOMAIN_BBOX)
        v = sub.values
        mis |= {"file": f.name, "MB": round(mb(f), 2), "variabile": var,
                "unita": sub.attrs.get("units") or sub.attrs.get("Units"), "griglia_globale": sizes,
                "risoluzione_gradi": round(float(np.diff(sub.lat.values[:2])[0]), 3),
                "shape_ritaglio": list(v.shape), "max_mm_h": round(float(np.nanmax(v)), 2),
                "frac_pioggia>0.1": round(float((v > .1).mean()), 4), "frac_nan": round(float(np.isnan(v).mean()), 4)}
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8, 4.2), dpi=80)
        im_ = ax.pcolormesh(sub.lon, sub.lat, np.where(v > .1, v, np.nan), cmap="turbo", vmin=0, vmax=20)
        fig.colorbar(im_, ax=ax, label="mm/h")
        ax.set_title("IMERG Final V07 15/07/2023 12:00-12:30 UTC")
        fig.tight_layout()
        fig.savefig(PREVIEWS / "s8_imerg.png")
        plt.close(fig)
        esito = "OK"
    except Exception as e:  # noqa: BLE001
        errori.append(repr(e)[:1500])
        esito = "KO"
    rm(out)
    risposta = (f"login {'OK' if mis.get('login_ok') else 'KO'}; {mis.get('file', '-')}: {mis.get('MB')} MB, "
                f"variabile '{mis.get('variabile')}' ({mis.get('unita')}), {mis.get('risoluzione_gradi')}°; "
                f"V08 su CMR: {mis.get('granuli_15_07_2023_12UTC_per_versione', {}).get('08')} granuli, "
                f"versioni collezione {mis.get('collezioni_GPM_3IMERGHH')}")
    write_result("s8a_imerg", esito, DOMANDA_I, risposta,
                 misure={"secondi": sec, "MB_scaricati": mis.get("MB", 0), "MB_conservati": 0, **mis},
                 errori=errori,
                 note=[f"{V08_URL} (28/04/2026): V07 Final termina a settembre 2025; V08 Final prevista "
                       "nell'estate 2026, retroprocessata dal 1998; Late/Early in modalità 'hybrid V07'"])


def huggingface():
    from huggingface_hub import HfApi
    from huggingface_hub.utils import get_token
    sec, errori, passi = {}, [], {}
    token = get_token() or env("HF_TOKEN")
    if not token:
        return write_result("s8b_huggingface", "SALTATO", DOMANDA_H, "Credenziale assente")
    api = HfApi(token=token)
    try:
        t0 = time.perf_counter()
        user = api.whoami()["name"]
        repo = f"{user}/ia-meteo-events"
        passi["utente"] = user
        passi["create_repo"] = str(api.create_repo(repo, repo_type="dataset", private=True, exist_ok=True))
        passi["privato"] = bool(api.repo_info(repo, repo_type="dataset").private)
        api.upload_file(path_or_fileobj=str(RESULTS / "s1_seviri.json"), path_in_repo="smoke_test/s1_seviri.json",
                        repo_id=repo, repo_type="dataset", commit_message="smoke test ia_meteo")
        passi["file_dopo_upload"] = api.list_repo_files(repo, repo_type="dataset")
        api.delete_file("smoke_test/s1_seviri.json", repo_id=repo, repo_type="dataset",
                        commit_message="rimozione smoke test")
        passi["file_dopo_delete"] = api.list_repo_files(repo, repo_type="dataset")
        sec["totale"] = round(time.perf_counter() - t0, 2)
        ok = ("smoke_test/s1_seviri.json" in passi["file_dopo_upload"]
              and "smoke_test/s1_seviri.json" not in passi["file_dopo_delete"] and passi["privato"])
        esito = "OK" if ok else "PARZIALE"
    except Exception as e:  # noqa: BLE001
        errori.append(repr(e)[:1500])
        esito = "KO"
    risposta = (f"dataset privato {passi.get('utente')}/ia-meteo-events: creato={'create_repo' in passi}, "
                f"privato={passi.get('privato')}, upload+verifica+cancellazione "
                f"{'riusciti' if esito == 'OK' else 'non completati'}; file rimasti: {passi.get('file_dopo_delete')}")
    write_result("s8b_huggingface", esito, DOMANDA_H, risposta,
                 misure={"secondi": sec, "MB_scaricati": 0, "MB_conservati": 0, "passi": passi},
                 errori=errori)


if __name__ == "__main__":
    imerg()
    huggingface()
