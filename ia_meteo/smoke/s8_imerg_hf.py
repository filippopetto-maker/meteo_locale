"""S8 — IMERG (NASA Earthdata, earthaccess) e Hugging Face (dataset privato).

IMERG: login da ~/.netrc, un file Final V07 half-hourly (GPM_3IMERGHH) del
15/07/2023 12:00 UTC, ritaglio su DOMAIN_BBOX; controllo su CMR se esiste una V08.
Pagina NASA "IMERG V08 Transition Schedule" (28/04/2026): V07 Final termina a
settembre 2025; V08 Final prevista per l'estate 2026, retroprocessata dal 1998.

Hugging Face: crea il dataset privato `ia-meteo-events`, carica
smoke/results/s1_seviri.json, verifica, cancella il file (il dataset resta vuoto).
Il token è quello salvato da `huggingface-cli login` (o HF_TOKEN in .env).
"""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DOMAIN_BBOX, PREVIEWS, RAW, RESULTS, env, mb, rm, write_result

DOMANDA_I = "Il login Earthdata funziona e un file IMERG Final V07 si legge?"
DOMANDA_H = "Possiamo scrivere sul dataset privato Hugging Face?"
V08_URL = "https://gpm.nasa.gov/data/news/imerg-v08-transition-schedule"


def imerg():
    import earthaccess
    import xarray as xr
    sec, errori = {}, []
    if not Path("~/.netrc").expanduser().exists():
        return write_result("s8a_imerg", "SALTATO", DOMANDA_I, "Credenziale assente (~/.netrc)")
    out = RAW / "s8"
    out.mkdir(exist_ok=True)
    mis = {}
    try:
        t0 = time.perf_counter()
        auth = earthaccess.login(strategy="netrc")
        sec["login"] = round(time.perf_counter() - t0, 2)
        mis["login_ok"] = bool(auth and auth.authenticated)
        # stato V08 su CMR
        v08 = {}
        for ver in ("07", "08"):
            try:
                r = earthaccess.search_data(short_name="GPM_3IMERGHH", version=ver,
                                            temporal=("2023-07-15T12:00:00", "2023-07-15T12:29:59"))
                v08[ver] = len(r)
            except Exception as e:  # noqa: BLE001
                v08[ver] = f"errore: {e!r}"[:200]
        mis["granuli_15_07_2023_12UTC_per_versione"] = v08
        try:
            cols = earthaccess.search_datasets(short_name="GPM_3IMERGHH")
            mis["collezioni_GPM_3IMERGHH"] = sorted({c["umm"].get("Version") for c in cols})
        except Exception as e:  # noqa: BLE001
            mis["collezioni_GPM_3IMERGHH"] = f"errore: {e!r}"[:200]
        t0 = time.perf_counter()
        gran = earthaccess.search_data(short_name="GPM_3IMERGHH", version="07",
                                       temporal=("2023-07-15T12:00:00", "2023-07-15T12:29:59"))
        sec["ricerca"] = round(time.perf_counter() - t0, 2)
        t0 = time.perf_counter()
        files = earthaccess.download(gran[:1], str(out))
        sec["download"] = round(time.perf_counter() - t0, 2)
        f = Path(files[0])
        mis["file"] = f.name
        mis["MB"] = round(mb(f), 2)
        ds = xr.open_dataset(f, group="Grid", engine="h5netcdf", decode_timedelta=False)
        var = "precipitation" if "precipitation" in ds else "precipitationCal"
        da = ds[var].isel(time=0)
        sub = da.sel(lat=slice(DOMAIN_BBOX["lat_min"], DOMAIN_BBOX["lat_max"]),
                     lon=slice(DOMAIN_BBOX["lon_min"], DOMAIN_BBOX["lon_max"])).load()
        v = sub.values
        mis |= {"variabile": var, "unita": da.attrs.get("units") or da.attrs.get("Units"),
                "griglia_globale": dict(ds[var].sizes),
                "risoluzione_gradi": round(float(np.diff(ds.lat.values[:2])[0]), 3),
                "shape_ritaglio": list(v.shape), "max_mm_h": round(float(np.nanmax(v)), 2),
                "frac_pioggia>0.1": round(float((v > .1).mean()), 4), "frac_nan": round(float(np.isnan(v).mean()), 4),
                "variabili_Grid": list(ds.data_vars)[:20]}
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8, 4.2), dpi=80)
        im = ax.pcolormesh(sub.lon, sub.lat, np.where(v.T > .1, v.T, np.nan), cmap="turbo", vmin=0, vmax=20)
        fig.colorbar(im, ax=ax, label="mm/h")
        ax.set_title("IMERG Final V07 15/07/2023 12:00-12:30 UTC")
        fig.tight_layout()
        fig.savefig(PREVIEWS / "s8_imerg.png")
        plt.close(fig)
        ds.close()
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
        url = api.create_repo(repo, repo_type="dataset", private=True, exist_ok=True)
        passi["create_repo"] = str(url)
        info = api.repo_info(repo, repo_type="dataset")
        passi["privato"] = bool(info.private)
        src = RESULTS / "s1_seviri.json"
        api.upload_file(path_or_fileobj=str(src), path_in_repo="smoke_test/s1_seviri.json",
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
