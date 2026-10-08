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
rappresentate, più un piccolo peso per i negativi. Senza l'intensità vincevano i giorni con tanti
temporali deboli sparsi: il 10/07/2019 (grandinata di Pescara) finiva 59° su 245 nel mese pilota. Si scelgono i periodi migliori senza sovrapposizioni, con un
massimo per mese per non concentrare tutto in pochi episodi, e si rispetta la divisione per anni del
planning (train 2015–2021, validazione 2022, test 2023–2024, FCI 2025).
La conferma con il radar OPERA (e i fulmini LI dal 2024) si fa dopo, solo sulle finestre scelte.

Uso:
    python selezione.py --n 300            # usa tutti i mesi del catalogo presenti su HF
Scrive data/events_candidati.parquet (bozza) e stampa il riepilogo.
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
MAX_PER_MESE = 6
PESO_NEGATIVI = 0.02           # solo spareggio: i negativi stanno comunque dentro le finestre
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
                      "intensita": intens, "n_conv": len(conv), "n_neg": len(neg)})
    return pd.DataFrame(righe)


def scegli(per: pd.DataFrame, n: int) -> pd.DataFrame:
    """Periodi migliori senza sovrapposizioni, al più MAX_PER_MESE per mese, pesando le zone rare."""
    freq = pd.Series([r for rr in per.riquadri_convettivi for r in rr]).value_counts()
    peso_zona = np.sqrt(freq.median() / freq).to_dict()          # ≈ 1 per una zona tipica
    per = per.copy()
    per["punteggio"] = [sum(peso_zona.get(r, 0) * v for r, v in it.items()) + PESO_NEGATIVI * nn
                        for it, nn in zip(per.intensita, per.n_neg)]
    per = per[per.n_conv > 0].sort_values("punteggio", ascending=False)
    scelti, occupati, per_mese = [], [], {}
    for _, r in per.iterrows():
        mese = r.inizio.strftime("%Y-%m")
        if per_mese.get(mese, 0) >= MAX_PER_MESE:
            continue
        if any(not (r.fine <= a or r.inizio >= b) for a, b in occupati):
            continue
        scelti.append(r)
        occupati.append((r.inizio, r.fine))
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
    a = ap.parse_args()
    df = classifica(carica_candidati())
    per = periodi(df)
    sel = scegli(per, a.n)
    ar.DATA.mkdir(parents=True, exist_ok=True)
    f = ar.DATA / "events_candidati.parquet"
    sel.to_parquet(f, index=False)
    print(f"{df.time.dt.strftime('%Y-%m').nunique()} mesi, {len(per)} periodi da 12 h valutati, {len(sel)} scelti → {f}")
    print(sel[["id", "insieme", "satellite", "n_conv", "n_neg", "punteggio"]].to_string(index=False))


if __name__ == "__main__":
    main()
