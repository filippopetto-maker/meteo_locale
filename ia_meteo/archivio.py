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
SEZIONI = ("finestre", "catalogo", "conferme")

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
def registro(a=None, revision: str | None = None) -> dict:
    """Indice dei pezzi caricati, per sezione (registro.json sul dataset; vuoto se non esiste)."""
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import EntryNotFoundError
    a = a or api()
    try:
        p = hf_hub_download(repo_id(a), "registro.json", repo_type="dataset", local_dir=DATA / "meta",
                            revision=revision)
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
def _commit_con_registro(a, rid, ops, modifica, messaggio, tentativi=8, gia_fatto=None):
    """Commit dei file + registro.json aggiornato con `modifica(reg)`, con controllo ottimistico:
    il commit dichiara la versione del repo su cui si basa (parent_commit); se nel frattempo un altro
    processo ha scritto (es. catalogo.py e conferma.py insieme), si rilegge il registro e si riprova.
    Sugli errori del server (5xx, timeout) si riprova con attesa crescente; prima di riprovare,
    `gia_fatto()` dice se il commit è arrivato comunque (un 504 non garantisce che sia fallito)."""
    from huggingface_hub import CommitOperationAdd
    from huggingface_hub.errors import HfHubHTTPError
    import requests
    for i in range(tentativi):
        try:
            if i and gia_fatto is not None and gia_fatto():
                return None
            base = a.repo_info(rid, repo_type="dataset").sha
            reg = registro(a, revision=base)
            modifica(reg)
            reg["aggiornato_utc"] = _ora()
            op_reg = CommitOperationAdd(path_in_repo="registro.json",
                                        path_or_fileobj=json.dumps(reg, indent=1, ensure_ascii=False).encode())
            return a.create_commit(rid, list(ops) + [op_reg], commit_message=messaggio, repo_type="dataset",
                                   parent_commit=base)
        except HfHubHTTPError as e:
            stato = getattr(e.response, "status_code", None)
            if i == tentativi - 1 or not (stato in (409, 412) or (stato or 0) >= 500):
                raise
            time.sleep(2 + 3 * i if stato in (409, 412) else 30 * (i + 1))
        except (requests.ConnectionError, requests.Timeout):
            if i == tentativi - 1:
                raise
            time.sleep(30 * (i + 1))



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
    ops_file = [CommitOperationAdd(path_in_repo=p, path_or_fileobj=str(f)) for p, f in file.items()]
    t0 = time.perf_counter()
    timbro = _ora()

    def gia_fatto():
        return registro(a).get(sezione, {}).get(chiave, {}).get("caricata_utc") == timbro

    _commit_con_registro(a, rid, ops_file, lambda reg: reg[sezione].__setitem__(
        chiave, (voce or {}) | {"file": attesi, "GB": round(gb, 4), "caricata_utc": timbro}),
        f"{sezione} {chiave}", gia_fatto=gia_fatto)
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
    voce = registro(a)[sezione].get(chiave)
    if voce is None:
        return
    ops = [CommitOperationDelete(path_in_repo=p) for p in voce["file"]]
    _commit_con_registro(a, repo_id(a), ops, lambda reg: reg[sezione].pop(chiave, None),
                         f"elimina {sezione} {chiave}")


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


# ---------------------------------------------------------------- parcheggio e caricamento a lotti
# HF permette 128 commit all'ora per repository (429 "Too Many Requests"): la conferma produce un
# periodo ogni ~15 s, quindi i pezzi piccoli si mettono nel parcheggio (staging/parcheggio/<sezione>/
# <chiave>/) e si caricano a lotti, un commit ogni INTERVALLO_LOTTO_S. Il parcheggio fa anche da
# riserva se HF non risponde: al massimo LIMITE_PARCHEGGIO_GB sul Mac, poi si aspetta.
PARCHEGGIO = STAGING / "parcheggio"
LIMITE_PARCHEGGIO_GB = 5.0
INTERVALLO_LOTTO_S = 600
_ultimo_lotto = [0.0]
_pausa_hf = [0.0]


def gb_parcheggio() -> float:
    if not PARCHEGGIO.exists():
        return 0.0
    return sum(p.stat().st_size for p in PARCHEGGIO.rglob("*") if p.is_file()) / 1e9


def parcheggiati(sezione: str | None = None) -> list[tuple[str, str]]:
    if not PARCHEGGIO.exists():
        return []
    return sorted((m.parent.parent.name, m.parent.name) for m in PARCHEGGIO.glob("*/*/voce.json")
                  if sezione is None or m.parent.parent.name == sezione)


def parcheggia(sezione: str, chiave: str, file: dict[str, Path], voce: dict | None = None, errore: str = "") -> None:
    if sezione not in SEZIONI:
        raise ValueError(f"sezione {sezione!r} non prevista: {SEZIONI}")
    d = PARCHEGGIO / sezione / chiave
    d.mkdir(parents=True, exist_ok=True)
    mappa = {}
    for p, f in file.items():
        dest = d / Path(p).name
        if Path(f) != dest:
            shutil.move(str(f), dest)
        mappa[p] = dest.name
    (d / "voce.json").write_text(json.dumps({"voce": voce or {}, "file": mappa, "errore": errore[:300]}))


def carica_o_parcheggia(sezione: str, chiave: str, file: dict[str, Path], voce: dict | None = None) -> str:
    """Mette il pezzo nel parcheggio e prova a caricare il lotto (al più un commit ogni
    INTERVALLO_LOTTO_S). Ritorna "caricato" se il pezzo è già su HF, altrimenti "in coda"."""
    parcheggia(sezione, chiave, file, voce)
    svuota_parcheggio()
    return "in coda" if (sezione, chiave) in parcheggiati(sezione) else "caricato"


def svuota_parcheggio(forza: bool = False, a=None) -> int:
    """Carica tutto il parcheggio con UN commit (file + registro). Senza `forza` lo fa al più ogni
    INTERVALLO_LOTTO_S, e dopo un errore di HF aspetta (15 min se 429, 5 min altrimenti)."""
    ora = time.time()
    if not forza and (ora - _ultimo_lotto[0] < INTERVALLO_LOTTO_S or ora < _pausa_hf[0]):
        return 0
    if not parcheggiati():
        return 0
    import fcntl
    PARCHEGGIO.mkdir(parents=True, exist_ok=True)
    lucchetto = open(PARCHEGGIO / ".lotto.lock", "w")
    try:
        fcntl.flock(lucchetto, fcntl.LOCK_EX | fcntl.LOCK_NB)   # un solo processo alla volta carica il lotto
    except BlockingIOError:
        lucchetto.close()
        return 0
    try:
        return _carica_lotto(ora, a)
    finally:
        fcntl.flock(lucchetto, fcntl.LOCK_UN)
        lucchetto.close()


def _carica_lotto(ora: float, a=None) -> int:
    from huggingface_hub import CommitOperationAdd
    voci = parcheggiati()
    if not voci:
        return 0
    _ultimo_lotto[0] = ora
    try:
        a = a or api()
        rid = assicura_repo(a)
        ops, attesi, nuove = [], {}, {}
        for sez, chiave in voci:
            d = PARCHEGGIO / sez / chiave
            meta = json.loads((d / "voce.json").read_text())
            file = {p: d / n for p, n in meta["file"].items()}
            sz = {p: f.stat().st_size for p, f in file.items()}
            ops += [CommitOperationAdd(path_in_repo=p, path_or_fileobj=str(f)) for p, f in file.items()]
            attesi |= sz
            nuove[(sez, chiave)] = meta["voce"] | {"file": sz, "GB": round(sum(sz.values()) / 1e9, 4)}
        if nuove:
            controlla_budget(sum(attesi.values()) / 1e9, a=a)
            timbro = _ora()

            def modifica(reg):
                for (sez, chiave), v in nuove.items():
                    reg[sez][chiave] = v | {"caricata_utc": timbro}

            def gia_fatto():
                r = registro(a)
                return all(r.get(s, {}).get(c, {}).get("caricata_utc") == timbro for s, c in nuove)

            _commit_con_registro(a, rid, ops, modifica, f"lotto di {len(nuove)} pezzi", gia_fatto=gia_fatto)
            remoti = {e.path: e.size for e in a.get_paths_info(rid, list(attesi), repo_type="dataset")}
            if remoti != attesi:
                raise RuntimeError(f"verifica del lotto fallita: {len(attesi)} attesi, {len(remoti)} trovati uguali")
        for sez, chiave in voci:
            shutil.rmtree(PARCHEGGIO / sez / chiave, ignore_errors=True)
        if nuove:
            print(f"lotto caricato su HF: {len(nuove)} pezzi in un commit", flush=True)
        return len(nuove)
    except BudgetSuperato:
        raise
    except Exception as e:  # noqa: BLE001
        attesa = 900 if "429" in repr(e) else 300
        _pausa_hf[0] = time.time() + attesa
        print(f"parcheggio: HF non disponibile ({e!r:.140}), riprovo fra {attesa // 60} min "
              f"({len(voci)} pezzi, {gb_parcheggio():.2f} GB in attesa)", flush=True)
        return 0


def attendi_parcheggio(limite_gb: float = LIMITE_PARCHEGGIO_GB, pausa_s: int = 600) -> None:
    """Prova a caricare il lotto; se il parcheggio supera il tetto, aspetta HF prima di produrre altro."""
    svuota_parcheggio()
    while gb_parcheggio() > limite_gb:
        print(f"parcheggio pieno ({gb_parcheggio():.2f} GB): attendo HF", flush=True)
        time.sleep(pausa_s)
        svuota_parcheggio(forza=True)
