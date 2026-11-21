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

Fiducia (22/11, rete_prova_prudenza.py): con pochi giorni la correzione piena sbaglia molto (6/10: p 0,15 → 0,49,
osservato 0,15). Ogni giorno passato verificato si corregge come si sarebbe fatto allora (storia fino a 2 giorni
prima) e si guarda se la correzione ha aiutato; logit q = logit p + β·(logit c − logit p) con β separato dove la
correzione alza o abbassa la p e per soglia, dalla massima verosimiglianza su quei giorni, ristretto con l'incertezza
(jackknife sui giorni pesati con i casi di pioggia osservati: un giorno secco non è una prova, un grande evento vale un
giorno; β·β²/(β²+se²), con se² gonfiato a ν/(ν−2) per i pochi gradi di libertà ν = giorni equivalenti − 1: fino a
3 giorni piovosi equivalenti l'errore non si conosce e β = 0, cioè la grezza). Nessuna soglia di giorni o di casi.
Ottobre–novembre e ombra: mai sotto la grezza; 6/10 a 0,144.

Base: la probabilità grezza (dal 22/11; prima la tarata).
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
BETA = np.round(np.arange(-0.5, 2.0001, 0.02), 2)  # griglia su cui cercare il β migliore (solo il modo di cercarlo)
PER_GIORNO = 40_000                                # campioni per giorno per le curve di β (solo velocità)
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
        p = d["p_grezza"]                                                  # la base della correzione
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


def curve(st: dict, soglie: list[float], rng) -> dict:
    """{giorno: (L, E)} per i giorni con una storia fino a 2 giorni prima: L (2 direzioni, soglie, BETA) log-loss del
    giorno per ogni β dove la correzione di allora abbassa (0) o alza (1) la p; E (2, soglie) casi di pioggia osservati."""
    out = {}
    for d in sorted(st):
        prima = {g_: v for g_, v in st.items() if g_ <= d - pd.Timedelta(days=2)}
        if not prima:
            continue
        p, y = st[d]
        i = rng.choice(len(y), min(len(y), PER_GIORNO), replace=False)
        c = applica(p[i], adatta(prima, soglie, rng))
        L, E = np.zeros((2, len(soglie), len(BETA))), np.zeros((2, len(soglie)))
        for k, s in enumerate(soglie):
            lp, lc, ob = logit(p[i, k]), logit(c[:, k]), y[i] >= s
            q = np.clip(1 / (1 + np.exp(-(lp[None] + BETA[:, None] * (lc - lp)[None]))), 1e-6, 1 - 1e-6)
            ll = -np.where(ob[None], np.log(q), np.log(1 - q)) * len(y) / len(i)
            for v, parte in enumerate((lc <= lp, lc > lp)):
                L[v, k] = ll[:, parte].sum(1)
                E[v, k] = ob[parte].sum() * len(y) / len(i)
        out[d] = (L, E)
    return out


def fiducia(C: dict) -> dict:
    """β (2 direzioni × soglie) dalle curve dei giorni verificati, con stima, errore e giorni piovosi equivalenti."""
    n_s = next(iter(C.values()))[0].shape[1] if C else 0
    r = {k: np.zeros((2, n_s)) for k in ("stima", "usato", "neff")}
    r["se"] = np.full((2, n_s), np.nan)
    if not C:
        return r
    M = np.stack([L for L, _ in C.values()])                   # (giorni, 2, soglie, β)
    E = np.stack([e for _, e in C.values()])
    tot = M.sum(0)
    for v in range(2):
        for k in range(n_s):
            e = E[:, v, k]
            if (e > 0).sum() < 2:
                continue
            w = e / e.sum()
            neff = 1 / (w ** 2).sum()
            b = BETA[tot[v, k].argmin()]
            jk = BETA[(tot[v, k][None] - M[:, v, k]).argmin(1)]
            s2 = (neff - 1) * (w * (jk - (w * jk).sum()) ** 2).sum()
            nu = neff - 1                                      # gradi di libertà della stima dell'errore
            s2 = s2 * nu / (nu - 2) if nu > 2 else np.inf      # varianza attesa (chi² inverso): con 2 giorni non si sa
            r["stima"][v, k], r["se"][v, k], r["neff"][v, k] = b, np.sqrt(s2), neff
            r["usato"][v, k] = b ** 3 / (b ** 2 + s2 + 1e-12) if b > 0 else 0.0
    return r


def correggi(p: np.ndarray, t0: pd.Timestamp, cartella: Path, celle: pd.DataFrame, fattore: pd.DataFrame,
             zona: np.ndarray, soglie: list[float]) -> tuple[np.ndarray, dict]:
    """Corregge p (grezza) con la storia in cartella/ombra e cartella/radar, pesata con la fiducia; info per il file."""
    rng = np.random.default_rng(0)
    st = storia(t0, cartella / "ombra", cartella / "radar", celle, fattore, zona)
    modelli = adatta(st, soglie, rng)
    fd = fiducia(curve(st, soglie, rng))
    piena = applica(p, modelli)
    out = np.array(p, dtype="float32", copy=True)
    dentro = np.isfinite(out[:, 0])
    for k in range(p.shape[1]):
        lp, lc = logit(out[:, k][dentro]), logit(piena[:, k][dentro])
        b = np.where(lc > lp, fd["usato"][1, k], fd["usato"][0, k])
        out[:, k][dentro] = 1 / (1 + np.exp(-(lp + b * (lc - lp))))
    out[:, 1] = np.minimum(out[:, 1], out[:, 0])
    out[:, 2] = np.minimum(out[:, 2], out[:, 1])
    info = {"giorni_verificati": len(st), "ultimo_giorno": str(max(st).date()) if st else None, "base": "grezza",
            "memorie": {str(D): [None if m is None else [float(m.intercept_[0]), float(m.coef_[0, 0])] for m in ms]
                        for D, ms in modelli.items()},
            "fiducia": {k: np.round(v, 4).tolist() for k, v in fd.items()}, "fiducia_righe": ["abbassa", "alza"]}
    attive = sum(m is not None for ms in modelli.values() for m in ms)
    logger.info(f"correzione: {len(st)} giorni verificati (ultimo {info['ultimo_giorno']}), "
                f"{attive} logistiche su {len(MEMORIE) * len(soglie)}; fiducia abbassa / alza per soglia "
                f"{np.round(fd['usato'][0], 2).tolist()} / {np.round(fd['usato'][1], 2).tolist()}, "
                f"giorni piovosi equivalenti {np.round(fd['neff'][1], 1).tolist()}")
    return out, info
