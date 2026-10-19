"""Fase 1 — selezione delle finestre da 12 h dai candidati del catalogo (catalogo.py).

Una finestra copre l'intero dominio per 12 ore: si scelgono periodi, non singoli temporali.
Dentro ogni periodo i riquadri 5°×5° si classificano così (soglie provvisorie, da rivedere sul
catalogo completo prima della selezione definitiva):
- CONVETTIVO: ML-CAPE p90 ≥ SOGLIA_MLCAPE e almeno FRAZ_CP del riquadro con ≥ 1 mm di
  precipitazione convettiva ERA5 in 3 h;
- NEGATIVO:  ML-CAPE p90 ≥ SOGLIA_MLCAPE_NEG ma precipitazione convettiva quasi assente
  (instabile senza convezione, il "caso negativo" del planning);
- il resto non conta.
Punteggio di un periodo: somma sui riquadri convettivi dell'intensità (massimo nel periodo di
frazione con ≥ 1 mm convettivi in 3 h × ML-CAPE p90 / 1000, con tetto 3), pesata per le zone poco
rappresentate, più BETA_LOCALE × il picco locale (massimo cp3h_mm_max dei riquadri convettivi),
entrambi divisi per la loro mediana, più un piccolo peso per i negativi. Il picco locale serve ai
temporali violenti ma piccoli: senza, il 24/07/2023 (grandine record Lombardia-Friuli) era 1088°. Senza l'intensità vincevano i giorni con tanti
temporali deboli sparsi: il 10/07/2019 (grandinata di Pescara) finiva 59° su 245 nel mese pilota. Si scelgono i periodi migliori senza sovrapposizioni, con un
massimo per mese per non concentrare tutto in pochi episodi, e si rispetta la divisione per anni del
planning (train 2015–2021, validazione 2022, test 2023–2024, FCI 2025).
La conferma con il radar OPERA (e i fulmini LI dal 2024) si fa dopo, solo sulle finestre scelte.

Rosa larga per la conferma radar (step 1, ottobre 2026): N_ROSA periodi con al più MAX_PER_MESE_ROSA
per mese. Scelta con prove sul catalogo completo (88 mesi) contro 15 eventi documentati e i picchi
locali di ogni riquadro: 1200 / 25 / BETA 1 prende 15/15 eventi e l'82% dei picchi; 6 per mese ne
prendeva 6/15; oltre 1200 il guadagno cala (1500: 84%, 2000: 85%) e la conferma costa di più.

Uso:
    python selezione.py --rosa             # rosa larga → data/rosa_larga.parquet
    python selezione.py --n 300            # bozza finale → data/events_candidati.parquet
Usa tutti i mesi del catalogo presenti su HF.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

import archivio as ar

SOGLIA_MLCAPE = 500.0         # J/kg, p90 del riquadro
FRAZ_CP = 0.05                # frazione del riquadro con ≥ 1 mm convettivi in 3 h
SOGLIA_MLCAPE_NEG = 1000.0
FRAZ_CP_NEG = 0.005
DURATA_H = 12
MAX_PER_MESE = 6               # selezione finale (provvisorio: si rivede con la conferma radar)
PESO_NEGATIVI = 0.02           # solo spareggio: i negativi stanno comunque dentro le finestre
BETA_LOCALE = 1.0              # peso del picco locale di pioggia convettiva
N_ROSA = 1200
MAX_PER_MESE_ROSA = 25
INSIEMI = {"train": range(2015, 2022), "validazione": [2022], "test": [2023, 2024], "fci": [2025]}


def insieme(anno: int) -> str:
    return next(k for k, v in INSIEMI.items() if anno in v)


def carica_candidati() -> pd.DataFrame:
    reg = ar.registro()
    parti = []
    for chiave, voce in sorted(reg["catalogo"].items()):
        pq = [p for p in voce["file"] if p.endswith(".parquet")]
        if not pq:
            continue
        f = ar.scarica("catalogo", chiave, lambda p: p.endswith(".parquet"))[0]
        parti.append(pd.read_parquet(f))
    if not parti:
        raise RuntimeError("nessun mese di catalogo con candidati su HF: lanciare catalogo.py")
    return pd.concat(parti, ignore_index=True)


def classifica(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["convettivo"] = (df.mlcape_p90 >= SOGLIA_MLCAPE) & (df.frac_cp3h_1mm >= FRAZ_CP)
    df["negativo"] = (df.mlcape_p90 >= SOGLIA_MLCAPE_NEG) & (df.frac_cp3h_1mm < FRAZ_CP_NEG)
    return df


def periodi(df: pd.DataFrame) -> pd.DataFrame:
    """Punteggio di ogni periodo di 12 h che inizia a un istante del catalogo (ogni 3 h)."""
    passi = DURATA_H // 3
    per_t = df.groupby("time").agg(conv=("convettivo", "sum"), neg=("negativo", "sum")).sort_index()
    # riquadri convettivi distinti nel periodo (un riquadro conta una volta anche se attivo più istanti)
    df = df.assign(intensita=df.frac_cp3h_1mm * np.minimum(df.mlcape_p90 / 1000.0, 3.0))
    att = df[df.convettivo].groupby("time")["riquadro"].apply(set).reindex(per_t.index)
    inten = df[df.convettivo].groupby("time").apply(lambda g: dict(zip(g.riquadro, g.intensita))).reindex(per_t.index)
    negs = df[df.negativo].groupby("time")["riquadro"].apply(set).reindex(per_t.index)
    picco = df[df.convettivo].groupby("time").cp3h_mm_max.max().reindex(per_t.index)
    righe = []
    tempi = per_t.index
    for i in range(len(tempi) - passi + 1):
        blocco = tempi[i:i + passi]
        if (blocco[-1] - blocco[0]) != pd.Timedelta(hours=DURATA_H - 3):
            continue                                   # buco nel catalogo (fine mese)
        conv = set().union(*[s for s in att.loc[blocco] if isinstance(s, set)])
        intens = {}
        for d in inten.loc[blocco]:
            if isinstance(d, dict):
                for r, v in d.items():
                    intens[r] = max(intens.get(r, 0.0), v)
        neg = set().union(*[s for s in negs.loc[blocco] if isinstance(s, set)]) - conv
        righe.append({"inizio": blocco[0], "fine": blocco[0] + pd.Timedelta(hours=DURATA_H),
                      "riquadri_convettivi": sorted(conv), "riquadri_negativi": sorted(neg),
                      "intensita": intens, "n_conv": len(conv), "n_neg": len(neg),
                      "picco_locale": float(np.nan_to_num(picco.loc[blocco].max()))})
    return pd.DataFrame(righe)


def punteggio(per: pd.DataFrame, beta: float = BETA_LOCALE) -> np.ndarray:
    freq = pd.Series([r for rr in per.riquadri_convettivi for r in rr]).value_counts()
    peso_zona = np.sqrt(freq.median() / freq).to_dict()          # ≈ 1 per una zona tipica
    esteso = np.array([sum(peso_zona.get(r, 0) * v for r, v in it.items()) for it in per.intensita])
    esteso = esteso / np.median(esteso[esteso > 0])
    locale = per.picco_locale.to_numpy() / np.median(per.picco_locale[per.picco_locale > 0])
    return esteso + beta * locale + PESO_NEGATIVI * per.n_neg.to_numpy()


def scegli(per: pd.DataFrame, n: int, max_mese: int = MAX_PER_MESE, beta: float = BETA_LOCALE) -> pd.DataFrame:
    """Periodi migliori senza sovrapposizioni, al più `max_mese` per mese, pesando le zone rare."""
    per = per.assign(punteggio=punteggio(per, beta))
    per = per[per.n_conv > 0].sort_values("punteggio", ascending=False)
    passi = DURATA_H // 3
    scelti, occupati, per_mese = [], set(), {}
    for _, r in per.iterrows():
        mese = r.inizio.strftime("%Y-%m")
        if per_mese.get(mese, 0) >= max_mese:
            continue
        slot = [r.inizio + pd.Timedelta(hours=3 * k) for k in range(passi)]   # istanti a 3 h coperti
        if any(x in occupati for x in slot):
            continue
        scelti.append(r)
        occupati.update(slot)
        per_mese[mese] = per_mese.get(mese, 0) + 1
        if len(scelti) >= n:
            break
    out = pd.DataFrame(scelti).sort_values("inizio").reset_index(drop=True)
    out["id"] = out.inizio.dt.strftime("%Y%m%d_%H")
    out["intensita"] = out.intensita.apply(lambda d: {k: round(v, 3) for k, v in d.items()})
    out["insieme"] = [insieme(t.year) for t in out.inizio]
    out["satellite"] = np.where(out.inizio >= pd.Timestamp("2025-01-01"), "fci", "seviri")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--rosa", action="store_true", help=f"rosa larga per la conferma ({N_ROSA}, {MAX_PER_MESE_ROSA}/mese)")
    ap.add_argument("--rosa-radar", action="store_true", help="temporali mancati da ERA5, dalla scansione radar")
    ap.add_argument("--negativi", action="store_true", help="casi negativi (instabile, nessuna convezione osservata)")
    ap.add_argument("--casuali", action="store_true", help="periodi a caso per la valutazione 'giorno qualsiasi'")
    a = ap.parse_args()
    if a.negativi or a.casuali:
        sel = rosa_negativa() if a.negativi else casuali()
        f = ar.DATA / ("rosa_negativa.parquet" if a.negativi else "casuali.parquet")
        sel.to_parquet(f, index=False)
        print(f"{len(sel)} periodi → {f}")
        print(sel.groupby("insieme").size().to_dict(), sel.groupby(sel.inizio.dt.year).size().to_dict())
        return
    if a.rosa_radar:
        sel = rosa_radar()
        f = ar.DATA / "rosa_radar.parquet"
        sel.to_parquet(f, index=False)
        print(f"{len(sel)} periodi → {f}")
        print(sel.groupby("insieme").size().to_dict(), sel.groupby(sel.inizio.dt.month).size().to_dict())
        return
    df = classifica(carica_candidati())
    per = periodi(df)
    sel = scegli(per, N_ROSA, MAX_PER_MESE_ROSA) if a.rosa else scegli(per, a.n)
    ar.DATA.mkdir(parents=True, exist_ok=True)
    f = ar.DATA / ("rosa_larga.parquet" if a.rosa else "events_candidati.parquet")
    sel.to_parquet(f, index=False)
    print(f"{df.time.dt.strftime('%Y-%m').nunique()} mesi, {len(per)} periodi da 12 h valutati, {len(sel)} scelti → {f}")
    print(sel[["id", "insieme", "satellite", "n_conv", "n_neg", "punteggio"]].to_string(index=False))



# ---------------------------------------------------------------- rosa radar (temporali mancati da ERA5)
N_ROSA_RADAR = 400
MAX_PER_MESE_RADAR = 8


def carica_scansione() -> pd.DataFrame:
    """Scansione radar ogni 3 h di tutti i mesi (conferma.py --scansione), da HF."""
    from huggingface_hub import snapshot_download
    a = ar.api()
    chiavi = [k for k in ar.registro(a)["conferme"] if k.startswith("scansione_")]
    snapshot_download(ar.repo_id(a), repo_type="dataset", local_dir=ar.CACHE,
                      allow_patterns=[f"conferme/{k}.parquet" for k in chiavi])
    return pd.concat([pd.read_parquet(ar.CACHE / "conferme" / f"{k}.parquet") for k in sorted(chiavi)],
                     ignore_index=True)


def mancati_radar(scan: pd.DataFrame, cand: pd.DataFrame) -> pd.DataFrame:
    """Riquadri-istante con convezione per il radar (criterio calibrato sui fulmini) che ERA5 non
    classifica convettivi. ML-CAPE: massimo fra l'istante e quelli a ±3 h."""
    import conferma as co
    d = scan.merge(cand[["time", "riquadro", "convettivo", "mlcape_p90"]], on=["time", "riquadro"], how="left")
    d = d.sort_values(["riquadro", "time"])
    g = d.groupby("riquadro").mlcape_p90
    d["mlcape"] = pd.concat([g.shift(1), d.mlcape_p90, g.shift(-1)], axis=1).max(axis=1).fillna(0)
    d["radar_conv"] = co.radar_convettivo(d.opera_copertura, d.opera_frac45, d.itdpc_copertura.fillna(0),
                                          d.itdpc_frac50.fillna(0), d.mlcape, d.opera_frac55)
    m = d[d.radar_conv & ~d.convettivo.fillna(False).astype(bool)].copy()
    # intensità osservata: area con eco forte, con più peso a grandine (≥ 55 dBZ) e ≥ 50 mm/h
    f45 = m.opera_frac45.where(~co.artefatto_opera(m.opera_frac45, m.opera_frac55), 0.0)
    f55 = m.opera_frac55.where(~co.artefatto_opera(m.opera_frac45, m.opera_frac55), 0.0)
    m["intensita_radar"] = f45 + 3 * f55 + 4 * m.itdpc_frac50.fillna(0)
    return m


def rosa_radar(n=N_ROSA_RADAR, max_mese=MAX_PER_MESE_RADAR) -> pd.DataFrame:
    """Periodi da 12 h con i temporali mancati da ERA5 più intensi, fuori dalle finestre della rosa
    larga e senza sovrapposizioni, al più `max_mese` per mese, pesando le zone rare."""
    cand = classifica(carica_candidati())
    m = mancati_radar(carica_scansione(), cand)
    rosa = pd.read_parquet(ar.DATA / "rosa_larga.parquet")
    passi = DURATA_H // 3
    occupati = {t + pd.Timedelta(hours=3 * k) for t in rosa.inizio for k in range(passi)}
    freq = m.riquadro.value_counts()
    peso = np.sqrt(freq.median() / freq)
    m["v"] = m.intensita_radar / m.intensita_radar.median() * m.riquadro.map(peso)
    per_t = m.groupby(["time", "riquadro"]).v.max().unstack(fill_value=0.0).sort_index()
    tempi = pd.DatetimeIndex(sorted(cand.time.unique()))
    per_t = per_t.reindex(tempi, fill_value=0.0)
    # punteggio di un periodo: somma sui riquadri del massimo nelle 4 istanti
    mx = per_t.rolling(passi).max().shift(-(passi - 1))
    punteggio = mx.sum(axis=1)
    validi = [t for t in tempi if (t + pd.Timedelta(hours=DURATA_H - 3)) in per_t.index
              and punteggio.get(t, 0) > 0]
    cand_per = periodi(cand).set_index("inizio")
    scelti, per_mese = [], {}
    for t in sorted(validi, key=lambda x: -punteggio[x]):
        mese = t.strftime("%Y-%m")
        slot = [t + pd.Timedelta(hours=3 * k) for k in range(passi)]
        if per_mese.get(mese, 0) >= max_mese or any(x in occupati for x in slot) or t not in cand_per.index:
            continue
        r = cand_per.loc[t]
        riq = mx.loc[t]
        scelti.append({"inizio": t, "fine": t + pd.Timedelta(hours=DURATA_H), "punteggio_radar": float(punteggio[t]),
                       "riquadri_mancati": sorted(riq[riq > 0].index), "n_mancati": int((riq > 0).sum()),
                       "riquadri_convettivi": r.riquadri_convettivi, "riquadri_negativi": r.riquadri_negativi,
                       "n_conv": r.n_conv, "n_neg": r.n_neg})
        occupati.update(slot)
        per_mese[mese] = per_mese.get(mese, 0) + 1
        if len(scelti) >= n:
            break
    out = pd.DataFrame(scelti).sort_values("inizio").reset_index(drop=True)
    out["id"] = out.inizio.dt.strftime("%Y%m%d_%H")
    out["insieme"] = [insieme(t.year) for t in out.inizio]
    out["satellite"] = np.where(out.inizio >= pd.Timestamp("2025-01-01"), "fci", "seviri")
    out["rosa"] = "radar"
    return out



# ---------------------------------------------------------------- casi negativi e valutazione "giorno qualsiasi"
N_NEGATIVE = 400               # planning: 1 negativa ogni 2 positive (~770 positive entro 90 GB)
MLCAPE_NEGATIVO = 1000.0
CASUALI = {2022: 40, 2023: 40, 2024: 40, 2025: 40}   # validazione, test, FCI: periodi estratti a caso
SEME_CASUALI = 20261019


def _finestre_occupate(*nomi) -> set:
    passi = DURATA_H // 3
    occ = set()
    for n in nomi:
        f = ar.DATA / f"{n}.parquet"
        if f.exists():
            occ |= {t + pd.Timedelta(hours=3 * k) for t in pd.read_parquet(f).inizio for k in range(passi)}
    return occ


def rosa_negativa(n=N_NEGATIVE) -> pd.DataFrame:
    """Planning: "ambiente instabile ma nessuna convezione osservata". Per ogni periodo da 12 h si
    contano i riquadri con ML-CAPE p90 ≥ 1000 in qualche istante, coperti dal radar (≥ 50%) in tutti
    gli istanti e senza convezione radar (criterio calibrato sui fulmini) in nessuno; si scelgono i
    periodi con più riquadri così, fuori dalle altre rose e senza sovrapposizioni."""
    cand = classifica(carica_candidati())
    scan = carica_scansione()
    import conferma as co
    d = scan.merge(cand[["time", "riquadro", "mlcape_p90"]], on=["time", "riquadro"], how="left")
    d = d.sort_values(["riquadro", "time"])
    g = d.groupby("riquadro").mlcape_p90
    d["mlcape"] = pd.concat([g.shift(1), d.mlcape_p90, g.shift(-1)], axis=1).max(axis=1).fillna(0)
    d["conv"] = co.radar_convettivo(d.opera_copertura, d.opera_frac45, d.itdpc_copertura.fillna(0),
                                    d.itdpc_frac50.fillna(0), d.mlcape, d.opera_frac55)
    d["oss"] = (d.opera_copertura >= 0.5) | (d.itdpc_copertura.fillna(0) >= 0.5)
    passi = DURATA_H // 3
    piv = lambda c, f: d.pivot_table(index="time", columns="riquadro", values=c, aggfunc="max").sort_index() \
        .astype(float).rolling(passi).agg(f).shift(-(passi - 1))
    inst = piv("mlcape", "max") >= MLCAPE_NEGATIVO
    conv = piv("conv", "max") > 0
    oss = piv("oss", "min") > 0
    neg = (inst & ~conv & oss)
    n_neg = neg.sum(axis=1)
    occ = _finestre_occupate("rosa_larga", "rosa_radar")
    cand_per = periodi(cand).set_index("inizio")
    scelti = []
    for t, v in n_neg[n_neg > 0].sort_values(ascending=False).items():
        slot = [t + pd.Timedelta(hours=3 * k) for k in range(passi)]
        if any(x in occ for x in slot) or t not in cand_per.index:
            continue
        r = cand_per.loc[t]
        scelti.append({"inizio": t, "fine": t + pd.Timedelta(hours=DURATA_H), "n_negativi_oss": int(v),
                       "riquadri_negativi_oss": sorted(neg.columns[neg.loc[t].values]),
                       "riquadri_convettivi": r.riquadri_convettivi, "riquadri_negativi": r.riquadri_negativi})
        occ.update(slot)
        if len(scelti) >= n:
            break
    out = pd.DataFrame(scelti).sort_values("inizio").reset_index(drop=True)
    out["id"] = out.inizio.dt.strftime("%Y%m%d_%H")
    out["insieme"] = [insieme(t.year) for t in out.inizio]
    out["satellite"] = np.where(out.inizio >= pd.Timestamp("2025-01-01"), "fci", "seviri")
    out["rosa"] = "negativa"
    return out


def casuali(quanti=CASUALI, seme=SEME_CASUALI) -> pd.DataFrame:
    """Periodi da 12 h estratti a caso (aprile–novembre, stratificati per mese, inizio a ore multiple di 3),
    senza guardare la convezione: misurano FAR e CSI su un giorno qualsiasi. Possono coincidere con
    finestre delle altre rose (in quel caso si riusa la stessa finestra)."""
    rng = np.random.default_rng(seme)
    righe = []
    for anno, n in quanti.items():
        mesi_ = list(range(4, 12))
        per_mese = np.full(len(mesi_), n // len(mesi_))
        per_mese[: n % len(mesi_)] += 1
        for m, k in zip(mesi_, per_mese):
            giorni = pd.date_range(f"{anno}-{m:02d}-01", periods=pd.Period(f"{anno}-{m:02d}").days_in_month, freq="D")
            for g in rng.choice(len(giorni), size=k, replace=False):
                t = giorni[g] + pd.Timedelta(hours=int(rng.integers(0, 8)) * 3)
                righe.append({"inizio": t, "fine": t + pd.Timedelta(hours=DURATA_H)})
    out = pd.DataFrame(righe).sort_values("inizio").reset_index(drop=True)
    out["id"] = out.inizio.dt.strftime("%Y%m%d_%H")
    out["insieme"] = [insieme(t.year) for t in out.inizio]
    out["satellite"] = np.where(out.inizio >= pd.Timestamp("2025-01-01"), "fci", "seviri")
    out["rosa"] = "casuale"
    return out


if __name__ == "__main__":
    main()
