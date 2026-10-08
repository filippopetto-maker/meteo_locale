"""Archivio delle finestre su Hugging Face, con il Mac come area di lavoro a tetto fisso.

Modalità di lavoro (Mac ≤ 20 GB liberi, HF privato ~95 GB):
- ogni finestra si costruisce in `data/staging/<id>/` (finestra.py), si carica su HF con un
  solo commit, si verifica (nomi e dimensioni) e si cancella dal Mac;
- per usarla (baseline, controlli) si riscarica in `data/cache/`, cache con tetto
  LIMITE_CACHE_GB: oltre il tetto si cancellano le finestre usate meno di recente;
- prima di ogni passo si controllano tre budget: spazio HF dell'account (BUDGET_HF_GB,
  cronologia inclusa), dati locali di ia_meteo (LIMITE_LOCALE_GB) e disco libero del Mac
  (MARGINE_DISCO_GB). Se uno sfora, ci si ferma con un errore, senza scrivere niente.

Struttura del dataset `<utente>/ia-meteo-events` (privato):
    registro.json                         # indice delle finestre caricate
    finestre/<id>/meta.json               # finestra, fonti, versioni, misure
    finestre/<id>/satellite.nc            # SEVIRI o FCI, 3 canali, int16 (0,01 K)
    finestre/<id>/radar.nc                # OPERA dBZ max per cella, int16 (0,01 dBZ)
    finestre/<id>/ambiente.nc             # ERA5 (fonti.era5.fetch_env)
    finestre/<id>/imerg.nc, fulmini.nc, itdpc.nc   # se disponibili
Pochi file grandi per finestra: HF chiede < 10 000 file per cartella e regge male gli Zarr a
molti chunk. Nota: su HF i file cancellati restano nella cronologia e contano nello spazio
finché non si esegue `super_squash_history` (vedi `compatta_cronologia`).
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from common import ROOT, mb

DATA = ROOT / "data"                 # ignorato da git (ia_meteo/.gitignore)
STAGING = DATA / "staging"
CACHE = DATA / "cache"
NOME_REPO = "ia-meteo-events"

BUDGET_HF_GB = 95.0                  # spazio privato HF che ci concediamo (account intero)
LIMITE_LOCALE_GB = 12.0              # tetto per data/ (staging + cache) sul Mac
LIMITE_CACHE_GB = 8.0                # tetto della sola cache delle finestre riscaricate
MARGINE_DISCO_GB = 5.0               # disco libero minimo da lasciare al Mac
STIMA_FINESTRA_GB = 0.25             # tetto prudente per una finestra (misurato: 0,06–0,17 GB in int16)

# codifica su disco: int16 compresso per i campi grandi, float32 compresso per gli altri
CODIFICA = {
    "Tb": {"dtype": "int16", "scale_factor": 0.01, "add_offset": 250.0, "_FillValue": -32768,
           "zlib": True, "complevel": 4},
    "dBZ": {"dtype": "int16", "scale_factor": 0.01, "add_offset": 0.0, "_FillValue": -32768,
            "zlib": True, "complevel": 4},
    "float": {"dtype": "float32", "zlib": True, "complevel": 4},
}


class BudgetSuperato(RuntimeError):
    pass


# ---------------------------------------------------------------- HF
def api():
    from huggingface_hub import HfApi
    return HfApi()


def repo_id(a=None) -> str:
    a = a or api()
    return f"{a.whoami()['name']}/{NOME_REPO}"


def assicura_repo(a=None) -> str:
    a = a or api()
    rid = repo_id(a)
    a.create_repo(rid, repo_type="dataset", private=True, exist_ok=True)
    return rid


def spazio_hf_gb(a=None) -> dict:
    """Spazio usato su HF: questo dataset e tutti i repo dell'account (cronologia inclusa)."""
    a = a or api()
    user = a.whoami()["name"]
    tot, dettaglio = 0, {}
    for kind, lister in (("model", a.list_models), ("dataset", a.list_datasets), ("space", a.list_spaces)):
        for r in lister(author=user):
            info = a.repo_info(r.id, repo_type=kind)
            u = getattr(info, "used_storage", 0) or 0
            dettaglio[f"{kind}:{r.id}"] = round(u / 1e9, 3)
            tot += u
    return {"account_GB": round(tot / 1e9, 3), "repo": dettaglio}


# ---------------------------------------------------------------- Mac
def spazio_locale_gb() -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    return {"data_GB": round(mb(DATA) / 1e3, 3), "cache_GB": round(mb(CACHE) / 1e3, 3),
            "staging_GB": round(mb(STAGING) / 1e3, 3),
            "disco_libero_GB": round(shutil.disk_usage(DATA).free / 1e9, 2)}


def controlla_budget(gb_nuovi: float = STIMA_FINESTRA_GB, hf: bool = True, a=None) -> dict:
    """Solleva BudgetSuperato se aggiungere `gb_nuovi` sfora uno dei tre limiti."""
    loc = spazio_locale_gb()
    if loc["disco_libero_GB"] - gb_nuovi < MARGINE_DISCO_GB:
        raise BudgetSuperato(f"disco Mac: liberi {loc['disco_libero_GB']} GB, margine {MARGINE_DISCO_GB} GB")
    if loc["data_GB"] + gb_nuovi > LIMITE_LOCALE_GB:
        raise BudgetSuperato(f"data/ locale: {loc['data_GB']} GB + {gb_nuovi} > {LIMITE_LOCALE_GB} GB")
    out = {"locale": loc}
    if hf:
        # used_storage di HF si aggiorna in differita: si usa il massimo con la somma del registro
        h = spazio_hf_gb(a)
        reg_gb = sum(f["GB"] for f in registro(a)["finestre"].values())
        occupato = max(h["account_GB"], reg_gb)
        if occupato + gb_nuovi > BUDGET_HF_GB:
            raise BudgetSuperato(f"HF: {occupato:.2f} GB + {gb_nuovi} > {BUDGET_HF_GB} GB")
        out["hf"] = h | {"registro_GB": round(reg_gb, 3), "occupato_GB": round(occupato, 3)}
    return out


# ---------------------------------------------------------------- registro
def registro(a=None) -> dict:
    """Indice delle finestre caricate (registro.json sul dataset; vuoto se non esiste)."""
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import EntryNotFoundError
    a = a or api()
    try:
        p = hf_hub_download(repo_id(a), "registro.json", repo_type="dataset", local_dir=DATA / "meta")
        return json.loads(Path(p).read_text())
    except EntryNotFoundError:
        return {"finestre": {}}


# ---------------------------------------------------------------- carica / scarica
def carica_finestra(cartella: Path, cancella_locale: bool = True, a=None) -> dict:
    """Carica `cartella` (= data/staging/<id>) in finestre/<id>/ con un solo commit che aggiorna
    anche registro.json; verifica nomi e dimensioni; poi cancella la copia locale."""
    from huggingface_hub import CommitOperationAdd
    a = a or api()
    rid = assicura_repo(a)
    fid = cartella.name
    files = sorted(f for f in cartella.iterdir() if f.is_file())
    gb = sum(f.stat().st_size for f in files) / 1e9
    controlla_budget(gb, a=a)
    reg = registro(a)
    meta = json.loads((cartella / "meta.json").read_text()) if (cartella / "meta.json").exists() else {}
    reg["finestre"][fid] = {"file": {f.name: f.stat().st_size for f in files}, "GB": round(gb, 4),
                            "caricata_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "inizio": meta.get("inizio"), "fine": meta.get("fine"),
                            "satellite": meta.get("satellite"), "sorgenti": sorted(meta.get("file", {}))}
    reg["aggiornato_utc"] = reg["finestre"][fid]["caricata_utc"]
    ops = [CommitOperationAdd(path_in_repo=f"finestre/{fid}/{f.name}", path_or_fileobj=str(f)) for f in files]
    ops.append(CommitOperationAdd(path_in_repo="registro.json",
                                  path_or_fileobj=json.dumps(reg, indent=1, ensure_ascii=False).encode()))
    t0 = time.perf_counter()
    a.create_commit(rid, ops, commit_message=f"finestra {fid}", repo_type="dataset")
    sec = round(time.perf_counter() - t0, 1)
    # verifica: tutti i file presenti con la stessa dimensione
    remoti = {e.path.split("/")[-1]: e.size for e in a.list_repo_tree(rid, path_in_repo=f"finestre/{fid}",
                                                                      repo_type="dataset")}
    attesi = {f.name: f.stat().st_size for f in files}
    if remoti != attesi:
        raise RuntimeError(f"verifica fallita per {fid}: attesi {attesi}, trovati {remoti}")
    if cancella_locale:
        shutil.rmtree(cartella)
    return {"id": fid, "GB": round(gb, 4), "secondi_upload": sec, "file": len(files)}


def _pulisci_cache():
    """Cancella le finestre in cache usate meno di recente finché la cache sta sotto il tetto."""
    base = CACHE / "finestre"
    if not base.exists():
        return []
    cartelle = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.stat().st_atime)
    tolte = []
    while cartelle and mb(CACHE) / 1e3 > LIMITE_CACHE_GB:
        d = cartelle.pop(0)
        shutil.rmtree(d)
        tolte.append(d.name)
    return tolte


def scarica_finestra(fid: str, sorgenti: list[str] | None = None, a=None) -> Path:
    """Porta in cache finestre/<fid>/ (tutte le sorgenti o solo quelle indicate, es. ["radar"])."""
    from huggingface_hub import snapshot_download
    a = a or api()
    reg = registro(a)["finestre"].get(fid)
    if reg is None:
        raise KeyError(f"finestra {fid} non è nel registro")
    nomi = [n for n in reg["file"] if sorgenti is None or n.split(".")[0] in sorgenti or n == "meta.json"]
    gb = sum(reg["file"][n] for n in nomi) / 1e9
    _pulisci_cache()
    controlla_budget(gb, hf=False)
    snapshot_download(repo_id(a), repo_type="dataset", local_dir=CACHE,
                      allow_patterns=[f"finestre/{fid}/{n}" for n in nomi])
    d = CACHE / "finestre" / fid
    d.touch()                                    # aggiorna l'accesso per la politica LRU
    _pulisci_cache()
    return d


def elimina_finestra(fid: str, a=None) -> None:
    """Toglie una finestra dal dataset e dal registro (lo spazio si libera solo con compatta_cronologia)."""
    from huggingface_hub import CommitOperationAdd, CommitOperationDelete
    a = a or api()
    rid = repo_id(a)
    reg = registro(a)
    reg["finestre"].pop(fid, None)
    reg["aggiornato_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    a.create_commit(rid, [CommitOperationDelete(path_in_repo=f"finestre/{fid}/"),
                          CommitOperationAdd(path_in_repo="registro.json",
                                             path_or_fileobj=json.dumps(reg, indent=1).encode())],
                    commit_message=f"elimina finestra {fid}", repo_type="dataset")


def compatta_cronologia(a=None) -> None:
    """Riduce la cronologia del dataset a un solo commit: libera lo spazio dei file cancellati."""
    a = a or api()
    a.super_squash_history(repo_id(a), repo_type="dataset", commit_message="compattazione cronologia")
