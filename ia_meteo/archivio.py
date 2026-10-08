"""Archivio su Hugging Face, con il Mac come area di lavoro a tetto fisso.

Modalità di lavoro (Mac ≤ 20 GB liberi, HF privato ~95 GB), uguale per ogni tipo di dato:
- si produce un pezzo in locale (una finestra in `data/staging/<id>/`, un mese di catalogo
  ERA5 in `data/staging/catalogo/`), lo si carica su HF con un solo commit che aggiorna anche
  `registro.json`, si verifica sul repo (percorsi e dimensioni) e si cancella dal Mac;
- `presente(sezione, chiave)` dice se un pezzo è già su HF: i lavori lunghi a sessioni
  (download delle finestre, catalogo mese per mese) saltano ciò che è già fatto e riprendono;
- per usarlo si riscarica in `data/cache/`, con tetto LIMITE_CACHE_GB: oltre il tetto si
  cancellano i pezzi usati meno di recente;
- prima di ogni passo si controllano tre budget: spazio HF dell'account (BUDGET_HF_GB,
  cronologia inclusa), dati locali di ia_meteo (LIMITE_LOCALE_GB) e disco libero del Mac
  (MARGINE_DISCO_GB). Se uno sfora, ci si ferma con un errore, senza scrivere niente.

Struttura del dataset `<utente>/ia-meteo-events` (privato):
    registro.json                         # indice per sezione: {"finestre": {...}, "catalogo": {...}}
    finestre/<id>/meta.json               # finestra, fonti, versioni, misure
    finestre/<id>/satellite.nc            # SEVIRI o FCI, 3 canali, int16 (0,01 K)
    finestre/<id>/radar.nc                # OPERA dBZ max per cella, int16 (0,01 dBZ)
    finestre/<id>/ambiente.nc             # ERA5 (fonti.era5.fetch_env)
    finestre/<id>/imerg.nc, fulmini.nc, itdpc.nc   # se disponibili
    catalogo/era5_AAAA_MM.nc              # Fase 1: CAPE, CIN (grezza), precipitazione convettiva, orari
Ogni voce del registro ha `file` = {percorso nel repo: byte}, `GB`, `caricata_utc` e metadati
propri della sezione. Pochi file grandi: HF chiede < 10 000 file per cartella e regge male gli
Zarr a molti chunk. Su HF i file cancellati restano nella cronologia e contano nello spazio
finché non si esegue `compatta_cronologia`; il conteggio `used_storage` si aggiorna in differita.
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
SEZIONI = ("finestre", "catalogo")

BUDGET_HF_GB = 95.0                  # spazio privato HF che ci concediamo (account intero)
LIMITE_LOCALE_GB = 12.0              # tetto per data/ (staging + lavoro + cache) sul Mac
LIMITE_CACHE_GB = 8.0                # tetto della sola cache dei pezzi riscaricati
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


def _ora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    """Spazio usato su HF: tutti i repo dell'account (cronologia inclusa)."""
    a = a or api()
    user = a.whoami()["name"]
    tot, dettaglio = 0, {}
    for kind, lister in (("model", a.list_models), ("dataset", a.list_datasets), ("space", a.list_spaces)):
        for r in lister(author=user):
            u = getattr(a.repo_info(r.id, repo_type=kind), "used_storage", 0) or 0
            dettaglio[f"{kind}:{r.id}"] = round(u / 1e9, 3)
            tot += u
    return {"account_GB": round(tot / 1e9, 3), "repo": dettaglio}


# ---------------------------------------------------------------- Mac
def spazio_locale_gb() -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    return {"data_GB": round(mb(DATA) / 1e3, 3), "cache_GB": round(mb(CACHE) / 1e3, 3),
            "staging_GB": round(mb(STAGING) / 1e3, 3),
            "disco_libero_GB": round(shutil.disk_usage(DATA).free / 1e9, 2)}


# ---------------------------------------------------------------- registro
def registro(a=None) -> dict:
    """Indice dei pezzi caricati, per sezione (registro.json sul dataset; vuoto se non esiste)."""
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import EntryNotFoundError
    a = a or api()
    try:
        p = hf_hub_download(repo_id(a), "registro.json", repo_type="dataset", local_dir=DATA / "meta")
        reg = json.loads(Path(p).read_text())
    except EntryNotFoundError:
        reg = {}
    for s in SEZIONI:
        reg.setdefault(s, {})
    return reg


def presente(sezione: str, chiave: str, reg: dict | None = None) -> bool:
    """True se il pezzo è già su HF (per riprendere un lavoro a sessioni saltando il fatto)."""
    return chiave in (reg or registro()).get(sezione, {})


def gb_registro(reg: dict) -> float:
    return sum(v.get("GB", 0) for s in SEZIONI for v in reg.get(s, {}).values())


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
        reg_gb = gb_registro(registro(a))
        occupato = max(h["account_GB"], reg_gb)
        if occupato + gb_nuovi > BUDGET_HF_GB:
            raise BudgetSuperato(f"HF: {occupato:.2f} GB + {gb_nuovi} > {BUDGET_HF_GB} GB")
        out["hf"] = h | {"registro_GB": round(reg_gb, 3), "occupato_GB": round(occupato, 3)}
    return out


# ---------------------------------------------------------------- caricamento generico
def carica(sezione: str, chiave: str, file: dict[str, Path], voce: dict | None = None,
           cancella_locale: bool = True, a=None) -> dict:
    """Carica `file` ({percorso nel repo: file locale}) con un solo commit che registra la voce
    `registro[sezione][chiave]`; verifica percorsi e dimensioni sul repo; cancella i file locali."""
    from huggingface_hub import CommitOperationAdd
    if sezione not in SEZIONI:
        raise ValueError(f"sezione {sezione!r} non prevista: {SEZIONI}")
    a = a or api()
    rid = assicura_repo(a)
    attesi = {p: f.stat().st_size for p, f in file.items()}
    gb = sum(attesi.values()) / 1e9
    controlla_budget(gb, a=a)
    reg = registro(a)
    reg[sezione][chiave] = (voce or {}) | {"file": attesi, "GB": round(gb, 4), "caricata_utc": _ora()}
    reg["aggiornato_utc"] = reg[sezione][chiave]["caricata_utc"]
    ops = [CommitOperationAdd(path_in_repo=p, path_or_fileobj=str(f)) for p, f in file.items()]
    ops.append(CommitOperationAdd(path_in_repo="registro.json",
                                  path_or_fileobj=json.dumps(reg, indent=1, ensure_ascii=False).encode()))
    t0 = time.perf_counter()
    a.create_commit(rid, ops, commit_message=f"{sezione} {chiave}", repo_type="dataset")
    sec = round(time.perf_counter() - t0, 1)
    remoti = {e.path: e.size for e in a.get_paths_info(rid, list(attesi), repo_type="dataset")}
    if remoti != attesi:
        raise RuntimeError(f"verifica fallita per {sezione}/{chiave}: attesi {attesi}, trovati {remoti}")
    if cancella_locale:
        for f in file.values():
            f.unlink(missing_ok=True)
    return {"sezione": sezione, "chiave": chiave, "GB": round(gb, 4), "secondi_upload": sec, "file": len(file)}


# ---------------------------------------------------------------- cache e scaricamento generico
def _unita_cache() -> list[Path]:
    """Unità della cache per la politica LRU: una cartella per finestra, un file per il resto."""
    out = []
    if (CACHE / "finestre").exists():
        out += [d for d in (CACHE / "finestre").iterdir() if d.is_dir()]
    for s in SEZIONI:
        if s != "finestre" and (CACHE / s).exists():
            out += [f for f in (CACHE / s).rglob("*") if f.is_file()]
    return out


def _pulisci_cache() -> list[str]:
    """Cancella i pezzi in cache usati meno di recente finché la cache sta sotto il tetto."""
    unita = sorted(_unita_cache(), key=lambda p: p.stat().st_atime)
    tolte = []
    while unita and mb(CACHE) / 1e3 > LIMITE_CACHE_GB:
        u = unita.pop(0)
        shutil.rmtree(u) if u.is_dir() else u.unlink()
        tolte.append(str(u.relative_to(CACHE)))
    return tolte


def scarica(sezione: str, chiave: str, filtro=None, a=None) -> list[Path]:
    """Porta in cache i file della voce `registro[sezione][chiave]` (tutti, o quelli per cui
    filtro(percorso) è vero). Ritorna i percorsi locali."""
    from huggingface_hub import snapshot_download
    a = a or api()
    voce = registro(a)[sezione].get(chiave)
    if voce is None:
        raise KeyError(f"{sezione}/{chiave} non è nel registro")
    percorsi = [p for p in voce["file"] if filtro is None or filtro(p)]
    gb = sum(voce["file"][p] for p in percorsi) / 1e9
    _pulisci_cache()
    controlla_budget(gb, hf=False)
    snapshot_download(repo_id(a), repo_type="dataset", local_dir=CACHE, allow_patterns=percorsi)
    locali = [CACHE / p for p in percorsi]
    for f in locali:                       # aggiorna l'accesso per la politica LRU
        f.touch()
        if sezione == "finestre":
            f.parent.touch()
    _pulisci_cache()
    return locali


def elimina(sezione: str, chiave: str, a=None) -> None:
    """Toglie i file di una voce dal dataset e dal registro (lo spazio si libera con compatta_cronologia)."""
    from huggingface_hub import CommitOperationAdd, CommitOperationDelete
    a = a or api()
    reg = registro(a)
    voce = reg[sezione].pop(chiave, None)
    if voce is None:
        return
    reg["aggiornato_utc"] = _ora()
    ops = [CommitOperationDelete(path_in_repo=p) for p in voce["file"]]
    ops.append(CommitOperationAdd(path_in_repo="registro.json", path_or_fileobj=json.dumps(reg, indent=1).encode()))
    a.create_commit(repo_id(a), ops, commit_message=f"elimina {sezione} {chiave}", repo_type="dataset")


def compatta_cronologia(a=None) -> None:
    """Riduce la cronologia del dataset a un solo commit: libera lo spazio dei file cancellati."""
    a = a or api()
    a.super_squash_history(repo_id(a), repo_type="dataset", commit_message="compattazione cronologia")


# ---------------------------------------------------------------- finestre
def carica_finestra(cartella: Path, cancella_locale: bool = True, a=None) -> dict:
    """Carica data/staging/<id>/ in finestre/<id>/ e cancella la cartella locale."""
    fid = cartella.name
    meta = json.loads((cartella / "meta.json").read_text()) if (cartella / "meta.json").exists() else {}
    file = {f"finestre/{fid}/{f.name}": f for f in sorted(cartella.iterdir()) if f.is_file()}
    voce = {"inizio": meta.get("inizio"), "fine": meta.get("fine"), "satellite": meta.get("satellite"),
            "sorgenti": sorted(meta.get("file", {})), "errori": meta.get("errori", {})}
    out = carica("finestre", fid, file, voce, cancella_locale, a)
    if cancella_locale:
        shutil.rmtree(cartella, ignore_errors=True)
    return out


def scarica_finestra(fid: str, sorgenti: list[str] | None = None, a=None) -> Path:
    """Porta in cache finestre/<fid>/ (tutte le sorgenti o solo quelle indicate, es. ["radar"])."""
    filtro = None if sorgenti is None else (lambda p: Path(p).stem in sorgenti or p.endswith("meta.json"))
    scarica("finestre", fid, filtro, a)
    return CACHE / "finestre" / fid


def elimina_finestra(fid: str, a=None) -> None:
    elimina("finestre", fid, a)


# ---------------------------------------------------------------- catalogo (Fase 1)
def chiave_catalogo(anno: int, mese: int) -> str:
    return f"era5_{anno:04d}_{mese:02d}"


def carica_catalogo(f: Path, anno: int, mese: int, voce: dict | None = None, a=None) -> dict:
    """Carica un mese di catalogo ERA5 in catalogo/<nome file> e cancella il file locale."""
    return carica("catalogo", chiave_catalogo(anno, mese), {f"catalogo/{f.name}": f},
                  (voce or {}) | {"anno": anno, "mese": mese}, True, a)


def scarica_catalogo(anno: int, mese: int, a=None) -> Path:
    return scarica("catalogo", chiave_catalogo(anno, mese), a=a)[0]
