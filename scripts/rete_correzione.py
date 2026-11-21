"""
rete_correzione.py — correzione della pioggia in ombra appresa dai giorni appena verificati

Perché (21/11/2026, rete_diagnosi_sottostima.py): la rete tende alla media. Nei periodi piovosi (ottobre e novembre
2026, ma anche agosto 2025 e 2026) le probabilità stanno sotto la frequenza osservata (−23 / −37 / −46 % a
0,2 / 1 / 4 mm nell'ombra 15–20/11), nei periodi secchi sopra. Non è la taratura (la grezza è già bassa) né un cambio
della verità (radar contro pluviometri Netatmo: nessuno stacco tra l'archivio 2025 e l'API 2026). L'errore però dura
qualche giorno (autocorrelazione 0,49 a un giorno, 0,32 a due): i giorni appena passati dicono quanto la rete sta
sbagliando adesso.

Come: per ogni soglia, una logistica logit(p corretta) = a + b·logit(p tarata) adattata sulle coppie previsione /
radar degli ultimi D giorni già verificati (tutte le corse in ombra, zona affidabile). Niente D scelto a mano: si
adatta per tutte le memorie da 3 a 25 giorni e si fa la media delle probabilità corrette (una memoria con meno di 50
casi di pioggia, o senza giorni secchi, lascia la p tarata). Simulazione su ottobre e novembre 2026 (storia fino a
due giorni prima del giorno previsto): probabilità media 0,112 contro frequenza 0,109 a 0,2 mm (tarata 0,081); Brier
sul clima del giorno da 0,044 a 0,116 (0,2 mm), da 0,023 a 0,093 (1 mm), da 0,011 a 0,033 (4 mm); a novembre da
negativo a positivo su tutte e tre. Scegliere ogni giorno la memoria migliore fin lì o correggere solo lo
scostamento rendeva meno.

Verità come in verifica_pioggia_ombra.py: radar DPC orario × fattore dei pluviometri MeteoNetwork.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import rete_dataset as rd  # noqa: E402
import verifica_pioggia_ombra as vo  # noqa: E402

MEMORIE = (3, 5, 7, 10, 15, 25)                    # giorni; si mediano tutte, nessuna è scelta
MIN_PIOGGIA = 50                                   # casi di pioggia sotto cui una logistica non si adatta
CAMPIONI = 150_000
logger = logging.getLogger("rete_correzione")


def logit(p: np.ndarray) -> np.ndarray:
    q = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(q / (1 - q))


def storia(t0: pd.Timestamp, cartella_ombra: Path, cartella_radar: Path, celle: pd.DataFrame,
           fattore: pd.DataFrame, zona: np.ndarray) -> dict:
    """{giorno: (p (n, 3), y (n,))} dalle corse in ombra prima di t0, sulle ore con il radar, zona affidabile."""
    oss = {}
    inizio = t0.normalize() - pd.Timedelta(days=max(MEMORIE) + 3)
    for f in sorted(cartella_radar.glob("sri_italia_*.npz")):
        if inizio <= pd.Timestamp(f.stem[-8:]) < t0.normalize():
            ore, y = vo.radar_orario(f, celle, fattore)
            oss.update({t: y[n] for n, t in enumerate(ore)})
    per_giorno: dict = {}
    for f in sorted(cartella_ombra.glob("pioggia_*.npz")):
        corsa = pd.Timestamp(f.stem[-10:-2] + "T" + f.stem[-2:])
        if not inizio - pd.Timedelta(days=2) <= corsa < t0:
            continue
        d = np.load(f)
        p = d["p_tarata"] if "p_tarata" in d else d["p"]                  # sempre la p prima della correzione
        for n, t in enumerate(pd.DatetimeIndex(d["tempi"])):
            if t not in oss:
                continue
            m = zona & np.isfinite(oss[t]) & np.isfinite(p[n, 0])
            per_giorno.setdefault(t.normalize(), []).append((p[n][:, m].T.astype("float32"), oss[t][m]))
    return {g_: (np.concatenate([a for a, _ in v]), np.concatenate([b for _, b in v])) for g_, v in per_giorno.items()}


def adatta(st: dict, soglie: list[float], rng=None) -> dict:
    """{D: [logistica o None per soglia]} sugli ultimi D giorni verificati."""
    rng = rng or np.random.default_rng(0)
    if not st:
        return {}
    ultimo = max(st)
    modelli = {}
    for D in MEMORIE:
        giorni = [g_ for g_ in st if g_ > ultimo - pd.Timedelta(days=D)]
        p = np.concatenate([st[g_][0] for g_ in giorni])
        y = np.concatenate([st[g_][1] for g_ in giorni])
        if len(y) > CAMPIONI:
            i = rng.choice(len(y), CAMPIONI, replace=False)
            p, y = p[i], y[i]
        modelli[D] = []
        for k, s in enumerate(soglie):
            o = (y >= s).astype(int)
            if o.sum() < MIN_PIOGGIA or o.sum() == len(o):
                modelli[D].append(None)
                continue
            modelli[D].append(LogisticRegression(C=1.0).fit(logit(p[:, k])[:, None], o))
    return modelli


def applica(p: np.ndarray, modelli: dict) -> np.ndarray:
    """p (ore, soglie, H, W) tarata → corretta: media delle memorie, poi di nuovo ordinate."""
    out = np.array(p, dtype="float32", copy=True)
    if not modelli:
        return out
    dentro = np.isfinite(out[:, 0])
    for k in range(p.shape[1]):
        v = out[:, k][dentro]
        q = [v if m[k] is None else m[k].predict_proba(logit(v)[:, None])[:, 1] for m in modelli.values()]
        out[:, k][dentro] = np.mean(q, axis=0)
    out[:, 1] = np.minimum(out[:, 1], out[:, 0])
    out[:, 2] = np.minimum(out[:, 2], out[:, 1])
    return out


def correggi(p: np.ndarray, t0: pd.Timestamp, cartella: Path, celle: pd.DataFrame, fattore: pd.DataFrame,
             zona: np.ndarray, soglie: list[float]) -> tuple[np.ndarray, dict]:
    """Corregge p (tarata) con la storia in cartella/ombra e cartella/radar; info per il file d'uscita."""
    st = storia(t0, cartella / "ombra", cartella / "radar", celle, fattore, zona)
    modelli = adatta(st, soglie)
    info = {"giorni_verificati": len(st), "ultimo_giorno": str(max(st).date()) if st else None,
            "memorie": {str(D): [None if m is None else [float(m.intercept_[0]), float(m.coef_[0, 0])] for m in ms]
                        for D, ms in modelli.items()}}
    attive = sum(m is not None for ms in modelli.values() for m in ms)
    logger.info(f"correzione: {len(st)} giorni verificati (ultimo {info['ultimo_giorno']}), "
                f"{attive} logistiche su {len(MEMORIE) * len(soglie)}")
    return applica(p, modelli), info
