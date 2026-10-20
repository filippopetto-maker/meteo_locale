"""Fase 2 — costruzione di tutte le finestre delle due rose, in ordine d'importanza, verso HF.

Elenco: rosa larga ERA5 (1200, selezione.py --rosa) + rosa radar dei temporali mancati da ERA5
(400, selezione.py --rosa-radar), con le conferme osservate (conferma.py --riepilogo). Ordine:
in ogni rosa i periodi con più convezione osservata e più intensa vengono prima, confrontati dentro
lo stesso anno (percentile del punteggio nell'anno: i fulmini, solo dal 2024, e la diversa qualità
del radar negli anni non devono spostare tutto su certi anni); le due rose si alternano 3:1 (le
proporzioni delle rose). Così dopo poche centinaia di finestre c'è già un nucleo con tutti gli anni.

Due "corsie" in parallelo, due processi che si prenotano le finestre (data/prenotazioni/):
- `--via completo`: satellite scaricato intero (8 GB a finestra, satura la linea);
- `--via tailor`: satellite ritagliato dal Data Tailor (0,35 GB, 3 alla volta per limite EUMETSAT),
  solo finestre SEVIRI (fino al 2024); usa poca banda e si somma alla prima (~+30%).
Le finestre vanno nel parcheggio e su HF a lotti (archivio.carica_o_parcheggia, max 5 GB sul Mac).
Una finestra senza satellite o senza radar non si carica e si riprova (al più 2 volte).

Uso:
    caffeinate -i python fase2.py --via completo
    caffeinate -i python fase2.py --via tailor
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

import archivio as ar
import finestra as fi
from catalogo import libera, prenota

ORDINE = ar.DATA / "finestre_ordine.parquet"
TENTATIVI = 2
LIMITE_GB = 90.0               # ci si ferma a ~90 GB su HF (~1000 finestre): prima la curva di apprendimento,
                               # poi si decide se le altre ~600 aggiungono valore (decisione del 19/10/2026)
# eventi documentati usati per scegliere la rosa (selezione.py): le loro finestre vanno per prime
EVENTI = ["2017-09-10", "2018-08-20", "2018-09-28", "2019-07-10", "2019-09-12", "2021-10-25", "2022-08-18",
          "2022-09-15", "2023-07-19", "2023-07-21", "2023-07-24", "2023-08-04", "2023-09-05", "2023-09-10",
          "2024-10-29"]
ESSENZIALI = ("satellite", "radar")
AMBIENTE_V = 2      # 2 = + t2m, d2m, sp, msl, q ai 4 livelli, ML-CAPE/CIN dai 23 livelli (20/10/2026)


def _punteggio(riep: pd.DataFrame) -> pd.Series:
    """Convezione osservata del periodo: riquadri convettivi (confermati + mancati da ERA5) e intensità
    radar (≥ 55 dBZ, ≥ 50 mm/h). Niente fulmini: ci sono solo dal 2024."""
    conv = riep.etichetta.isin(["confermato", "mancato_dal_modello"])
    g = riep.assign(conv=conv, f55=riep.opera_frac55.where(conv, 0).clip(upper=0.02),
                    f50=riep.itdpc_frac50.fillna(0).where(conv, 0).clip(upper=0.02)).groupby("periodo")
    return g.conv.sum() + 200 * g.f55.sum() + 200 * g.f50.sum()


QUOTE = {"positiva": 770, "negativa": 550, "casuale": 200}   # entro ~90 GB (19/10/2026; +40 casuali 2022 il 20/10;
# 20/10: 550 candidati negativi perché ~1/4 si scarta al controllo radar completo → ~400 negative, 1 ogni 2 positive)
COPERTURA_PULITO = 0.8      # con 0,5 passavano riquadri della costa algerina con temporali nell'entroterra non coperto
                            # (visti dai fulmini LI: 5 riquadri su 31 nell'agosto 2024)
SCARTATE = ar.DATA / "negativi_scartati"      # un file per finestra negativa scartata (condiviso fra le corsie)


def _positive() -> pd.DataFrame:
    """Rosa ERA5 e rosa radar alternate 3:1, ciascuna per percentile della convezione osservata nell'anno."""
    righe = []
    for rosa, riep in (("era5", "riepilogo_rosa"), ("radar", "riepilogo_radar")):
        sel = pd.read_parquet(ar.DATA / f"{'rosa_larga' if rosa == 'era5' else 'rosa_radar'}.parquet")
        p = _punteggio(pd.read_parquet(ar.DATA / f"{riep}.parquet"))
        sel = sel.assign(rosa=rosa, punteggio_oss=sel.id.map(p).fillna(0))
        sel["priorita"] = sel.groupby(sel.inizio.dt.year).punteggio_oss.rank(pct=True)
        sel = sel.sort_values(["priorita", "punteggio_oss"], ascending=False)
        righe.append(sel[["id", "inizio", "fine", "insieme", "satellite", "rosa", "priorita"]].reset_index(drop=True))
    era5, radar = righe
    out, i, j = [], 0, 0
    while i < len(era5) or j < len(radar):            # 3 ERA5 : 1 radar
        for _ in range(3):
            if i < len(era5):
                out.append(era5.iloc[i]); i += 1
        if j < len(radar):
            out.append(radar.iloc[j]); j += 1
    return pd.DataFrame(out).assign(componente="positiva")


def ordine() -> pd.DataFrame:
    """Positive, negative (planning: 1 ogni 2 positive) e casuali (valutazione 'giorno qualsiasi')
    mescolate in proporzione alle QUOTE: la k-esima di una componente va alla posizione relativa
    k/quota, così anche un arresto anticipato rispetta i rapporti. In testa le finestre degli eventi."""
    pos = _positive()
    neg = pd.read_parquet(ar.DATA / "rosa_negativa.parquet")
    neg["priorita"] = neg.groupby(neg.inizio.dt.year).n_negativi_oss.rank(pct=True)
    neg = neg.sort_values(["soglia_mlcape", "priorita", "n_negativi_oss"], ascending=False).assign(componente="negativa")
    cas = pd.read_parquet(ar.DATA / "casuali.parquet")
    cas = cas.sample(frac=1, random_state=1).assign(componente="casuale", priorita=0.0)   # ordine casuale
    parti = []
    for comp, df in (("positiva", pos), ("negativa", neg), ("casuale", cas)):
        df = df[["id", "inizio", "fine", "insieme", "satellite", "rosa", "priorita", "componente"]].reset_index(drop=True)
        df["chiave"] = (np.arange(len(df)) + 0.5) / QUOTE[comp]
        parti.append(df)
    df = pd.concat(parti).sort_values("chiave", kind="stable")
    df = df.drop_duplicates("id").reset_index(drop=True)          # una casuale può coincidere con un'altra finestra
    giorni = [pd.Timestamp(d) for d in EVENTI]
    evento = df.apply(lambda r: r.componente == "positiva" and
                      any(r.inizio < g + pd.Timedelta("1D") and r.fine > g for g in giorni), axis=1)
    df = pd.concat([df[evento], df[~evento]], ignore_index=True)
    df["posizione"] = np.arange(len(df))
    return df.drop(columns="chiave")


def verifica_negativa(fid: str, t0, t1, riquadri: list[str]) -> tuple[list[str], dict]:
    """Prima di costruire una finestra negativa: radar completo (OPERA ogni 5–15 min e, dove vede, il radar
    italiano) sui riquadri negativi. Pulito = coperto (≥ COPERTURA_PULITO) e nessun pixel ≥ 40 dBZ / ≥ 10 mm/h in nessuna
    immagine. La scansione ogni 3 h lasciava passare celle brevi: 16 riquadri su 17 nelle prime 3 negative."""
    import zone
    from common import DOMAIN_BBOX, area_def_regular
    area = area_def_regular(DOMAIN_BBOX, 0.05)
    riq = zone.riquadri()
    imp = zone.carica()["impronta_radar_italia"]
    lavoro = ar.DATA / "lavoro" / f"verifica_{fid}"
    try:
        op = fi.radar(t0, t1, area, lavoro)[0].dbz.values
        it = None
        if t0 < fi.FINE_ITDPC and any(imp[riq[k]].mean() > 0.2 for k in riquadri):
            it = fi.radar_italia(t0, t1, area)[0].pioggia_max.values
    finally:
        shutil.rmtree(lavoro, ignore_errors=True)
    puliti, det = [], {}
    for k in riquadri:
        r, c = riq[k]
        b = op[:, r, c]
        cop_op = float(np.isfinite(b).mean())
        mx_op = float(np.nanmax(b)) if np.isfinite(b).any() else None
        cop_it, mx_it = 0.0, None
        if it is not None:
            bi = it[:, r, c]
            cop_it = float(np.isfinite(bi).mean())
            mx_it = float(np.nanmax(bi)) if np.isfinite(bi).any() else None
        coperto = max(cop_op, cop_it) >= COPERTURA_PULITO
        pulito = coperto and (mx_op is None or mx_op < 40) and (mx_it is None or mx_it < 10)
        det[k] = {"copertura_opera": round(cop_op, 3), "max_dbz": mx_op, "copertura_italia": round(cop_it, 3),
                  "max_mmh": mx_it, "pulito": pulito}
        if pulito:
            puliti.append(k)
    return puliti, det


def costruisci_e_carica(r, via: str, extra: dict | None = None) -> str:
    d = fi.costruisci(r.id, r.inizio.to_pydatetime(), r.fine.to_pydatetime(), via=via)
    meta = json.loads((d / "meta.json").read_text())
    mancano = [k for k in ESSENZIALI if k in meta["errori"] or k not in meta["file"]]
    if mancano:
        shutil.rmtree(d, ignore_errors=True)
        raise RuntimeError(f"mancano {mancano}: {meta['errori']}")
    file = {f"finestre/{r.id}/{f.name}": f for f in sorted(d.iterdir()) if f.is_file()}
    voce = {"inizio": meta["inizio"], "fine": meta["fine"], "satellite": meta["satellite"], "rosa": r.rosa,
            "ambiente_v": AMBIENTE_V if _ambiente_v2(d / "ambiente.nc") else 0,
            "componente": r.componente,
            "insieme": r.insieme, "priorita": float(r.priorita), "via": via,
            "sorgenti": sorted(meta["file"]), "errori": meta["errori"]} | (extra or {})
    esito = ar.carica_o_parcheggia("finestre", r.id, file, voce)
    shutil.rmtree(d, ignore_errors=True)
    return esito


def _ambiente_v2(f) -> bool:
    if not f.exists():
        return False
    try:
        import xarray as xr
        with xr.open_dataset(f) as ds:
            return all(v in ds for v in ("mlcape", "mlcin", "msl", "t2m", "d2m", "sp", "q"))
    except Exception:  # noqa: BLE001
        return False


def completa_ambiente(ogni: int = 10) -> int:
    """Finestre caricate prima del 20/10/2026: si rifà ambiente.nc con superficie, pressione al livello
    del mare e ML-CAPE/CIN dai profili completi (stessa funzione e stesso percorso delle nuove) e si
    sostituisce su HF, un commit ogni `ogni` finestre. Riprendibile: il registro segna `ambiente_v`
    e i file rifatti ma non ancora caricati restano in staging e si riusano."""
    from huggingface_hub import CommitOperationAdd
    from fonti import era5
    a = ar.api()
    rid = ar.assicura_repo(a)
    da_fare = [k for k, v in ar.registro(a)["finestre"].items() if v.get("ambiente_v") != AMBIENTE_V]
    print(f"ambiente da completare: {len(da_fare)} finestre", flush=True)
    fatte, lotto = 0, {}

    def commit(lotto):
        ops = [CommitOperationAdd(path_in_repo=f"finestre/{k}/ambiente.nc", path_or_fileobj=str(f)) for k, f in lotto.items()]

        def modifica(reg):
            for k, f in lotto.items():
                v = reg["finestre"][k]
                v["file"][f"finestre/{k}/ambiente.nc"] = f.stat().st_size
                v["GB"] = round(sum(v["file"].values()) / 1e9, 4)
                v["ambiente_v"] = AMBIENTE_V
        ar._commit_con_registro(a, rid, ops, modifica, f"ambiente con ML-CAPE per {len(lotto)} finestre")
        for f in lotto.values():
            shutil.rmtree(f.parent, ignore_errors=True)
        print(f"caricati {len(lotto)} ambienti", flush=True)

    for k in da_fare:
        v = ar.registro(a)["finestre"][k]
        t0, t1 = pd.Timestamp(v["inizio"]).to_pydatetime(), pd.Timestamp(v["fine"]).to_pydatetime()
        d = ar.STAGING / f"ambiente_{k}"
        d.mkdir(parents=True, exist_ok=True)
        if _ambiente_v2(d / "ambiente.nc"):          # già rifatto in una sessione interrotta prima del commit
            lotto[k] = d / "ambiente.nc"
            fatte += 1
            continue
        try:
            ds, _ = era5.fetch_env(t0, t1, ar.DATA / "lavoro" / f"ambiente_{k}")
            fi._salva(ds, d / "ambiente.nc", {})
        except Exception as e:  # noqa: BLE001
            print(f"{k}: ERRORE {e!r:.200}", flush=True)
            continue
        finally:
            shutil.rmtree(ar.DATA / "lavoro" / f"ambiente_{k}", ignore_errors=True)
        lotto[k] = d / "ambiente.nc"
        fatte += 1
        if len(lotto) >= ogni:
            commit(lotto)
            lotto = {}
    if lotto:
        commit(lotto)
    return fatte


def completa_sorgenti(ogni: int = 10) -> int:
    """Finestre già caricate a cui manca una sorgente non essenziale (IMERG, fulmini, radar italiano:
    di solito un guasto temporaneo del servizio): si calcola, si aggiunge con meta.json aggiornato e si
    aggiorna il registro, un commit ogni `ogni` finestre. Riprendibile come completa_ambiente."""
    from huggingface_hub import CommitOperationAdd
    a = ar.api()
    rid = ar.assicura_repo(a)
    reg = ar.registro(a)["finestre"]
    da_fare = []
    for k, v in reg.items():
        t0 = pd.Timestamp(v["inizio"]).to_pydatetime()
        for nome in sorted(fi.sorgenti_attese(t0) - set(v["sorgenti"]) - set(ESSENZIALI) - {"ambiente"}):
            da_fare.append((k, nome))
    print(f"sorgenti da completare: {len(da_fare)} {sorted({n for _, n in da_fare})}", flush=True)
    fatte, lotto = 0, []

    def commit(lotto):
        ops, nuove = [], {}
        for k, nome, f, mis in lotto:
            ops.append(CommitOperationAdd(path_in_repo=f"finestre/{k}/{nome}.nc", path_or_fileobj=str(f)))
            nuove.setdefault(k, []).append((nome, f, mis))
        for k, voci in nuove.items():                   # meta.json della finestra allineato al registro
            m = json.loads(Path(ar.scarica("finestre", k, lambda p: p.endswith("meta.json"), a)[0]).read_text())
            for nome, f, mis in voci:
                m["errori"].pop(nome, None)
                m["file"][nome] = f"{nome}.nc"
                m["misure"][nome] = mis | {"completata_dopo": True}
            mj = ar.STAGING / f"sorgenti_{k}" / "meta.json"
            mj.parent.mkdir(parents=True, exist_ok=True)
            mj.write_text(json.dumps(m, indent=1, ensure_ascii=False, default=str))
            ops.append(CommitOperationAdd(path_in_repo=f"finestre/{k}/meta.json", path_or_fileobj=str(mj)))

        def modifica(reg):
            for k, voci in nuove.items():
                v = reg["finestre"][k]
                for nome, f, _ in voci:
                    v["file"][f"finestre/{k}/{nome}.nc"] = f.stat().st_size
                    v["errori"].pop(nome, None)
                    v["sorgenti"] = sorted(set(v["sorgenti"]) | {nome})
                v["file"][f"finestre/{k}/meta.json"] = (ar.STAGING / f"sorgenti_{k}" / "meta.json").stat().st_size
                v["GB"] = round(sum(v["file"].values()) / 1e9, 4)
        ar._commit_con_registro(a, rid, ops, modifica, f"sorgenti completate per {len(nuove)} finestre")
        for k in nuove:
            shutil.rmtree(ar.STAGING / f"sorgenti_{k}", ignore_errors=True)
        print(f"caricate {len(lotto)} sorgenti in {len(nuove)} finestre", flush=True)

    for k, nome in da_fare:
        v = reg[k]
        t0, t1 = pd.Timestamp(v["inizio"]).to_pydatetime(), pd.Timestamp(v["fine"]).to_pydatetime()
        d = ar.STAGING / f"sorgenti_{k}"
        d.mkdir(parents=True, exist_ok=True)
        f, mf = d / f"{nome}.nc", d / f"{nome}.json"
        if f.exists() and mf.exists():                  # già calcolata in una sessione interrotta
            lotto.append((k, nome, f, json.loads(mf.read_text())))
            fatte += 1
            continue
        lavoro = ar.DATA / "lavoro" / f"sorgenti_{k}"
        try:
            ds, mis, tipo = fi.passo_singolo(nome, t0, t1, lavoro)
            mis["MB_file"] = fi._salva(ds, f, tipo)
            mf.write_text(json.dumps(mis, default=str))
        except Exception as e:  # noqa: BLE001
            print(f"{k} {nome}: ERRORE {e!r:.200}", flush=True)
            continue
        finally:
            shutil.rmtree(lavoro, ignore_errors=True)
        lotto.append((k, nome, f, mis))
        fatte += 1
        print(f"{k} {nome}: {mis}", flush=True)
        if len(lotto) >= ogni:
            commit(lotto)
            lotto = []
    if lotto:
        commit(lotto)
    return fatte


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--via", choices=["completo", "tailor"], default="completo")
    ap.add_argument("--max", type=int, default=None, help="al più N finestre in questa sessione")
    ap.add_argument("--completa-ambiente", action="store_true", help="rifà ambiente.nc delle finestre già caricate")
    ap.add_argument("--completa-sorgenti", action="store_true", help="aggiunge le sorgenti non essenziali mancanti")
    a = ap.parse_args()
    if a.completa_sorgenti:
        print(f"fine: {completa_sorgenti()} sorgenti completate", flush=True)
        return
    if a.completa_ambiente:
        print(f"fine: {completa_ambiente()} finestre completate", flush=True)
        return
    if not ORDINE.exists():
        ordine().to_parquet(ORDINE, index=False)

    def leggi_coda():
        lista = pd.read_parquet(ORDINE)
        fatte = set(ar.registro()["finestre"]) | {k for s, k in ar.parcheggiati("finestre")} | \
            {f.stem for f in SCARTATE.glob("*.json")}
        coda = [r for r in lista.itertuples() if r.id not in fatte and (a.via == "completo" or r.satellite == "seviri")]
        print(f"{len(lista)} finestre in elenco, {len(fatte)} già fatte, {len(coda)} candidate per la corsia {a.via}", flush=True)
        return coda

    def negativi() -> dict:
        return pd.read_parquet(ar.DATA / "rosa_negativa.parquet").set_index("id").riquadri_negativi_oss.map(list).to_dict()

    coda, versione = leggi_coda(), ORDINE.stat().st_mtime
    tentativi, n = {}, 0
    while coda:
        if ORDINE.stat().st_mtime != versione:          # ordine rigenerato: si riparte dal nuovo, senza riavviare
            coda, versione = leggi_coda(), ORDINE.stat().st_mtime
            continue
        r = coda.pop(0)
        chiave = f"finestra_{r.id}"
        try:
            ar.attendi_parcheggio()
        except ar.BudgetSuperato as e:
            print(f"STOP, budget HF: {e}", flush=True)
            break
        occupati = ar.gb_registro(ar.registro()) + ar.gb_parcheggio()
        if occupati >= LIMITE_GB:
            print(f"STOP: {occupati:.1f} GB su HF + parcheggio, limite di questa fase {LIMITE_GB} GB", flush=True)
            break
        if r.id in ar.registro()["finestre"] or ("finestre", r.id) in ar.parcheggiati("finestre"):
            continue
        if not prenota(chiave, a.via):
            continue
        t = time.perf_counter()
        try:
            extra = None
            if r.componente == "negativa":
                puliti, det = verifica_negativa(r.id, r.inizio.to_pydatetime(), r.fine.to_pydatetime(),
                                                negativi().get(r.id, []))
                if not puliti:
                    SCARTATE.mkdir(parents=True, exist_ok=True)
                    (SCARTATE / f"{r.id}.json").write_text(json.dumps(det, indent=1))
                    print(f"{r.id} [negativa, pos {r.posizione}]: scartata, nessun riquadro pulito col radar completo "
                          f"({(time.perf_counter() - t) / 60:.1f} min)", flush=True)
                    continue
                extra = {"riquadri_negativi_verificati": puliti, "verifica_negativa": det}
            esito = costruisci_e_carica(r, a.via, extra)
            n += 1
            print(f"{r.id} [{r.componente}/{r.rosa}, pos {r.posizione}]: {esito} in {(time.perf_counter() - t) / 60:.1f} min "
                  f"(sessione: {n})", flush=True)
        except ar.BudgetSuperato as e:
            print(f"STOP, budget HF: {e}", flush=True)
            break
        except Exception as e:  # noqa: BLE001
            tentativi[r.id] = tentativi.get(r.id, 0) + 1
            print(f"{r.id}: ERRORE {e!r:.300}", flush=True)
            traceback.print_exc()
            if tentativi[r.id] < TENTATIVI:
                coda.append(r)
        finally:
            libera(chiave)
        if a.max and n >= a.max:
            break
    while ar.parcheggiati():
        if ar.svuota_parcheggio(forza=True) == 0 and ar.parcheggiati():
            time.sleep(600)
    print("fine sessione", flush=True)


if __name__ == "__main__":
    main()
