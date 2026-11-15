"""
rete_unet_orario.py — la U-Net a passo orario, con la taratura per distanza dalla costa (terra e mare)

Dataset: rete_dataset.py --ore 1 (data/lazio_sri/dataset_1h/: un campione = un'ora, 22 canali dei modelli con le
3 ore prima e dopo). Stessa rete, stessa divisione (blocchi di 10 giorni; arresto = un blocco di addestramento su
dieci) e stessa configurazione stabile di rete_unet.py. Soglie orarie: 0,2, 1 e 4 mm nell'ora (frequenza sul Lazio
~4,9%, 2,1%, 0,4%).

Memoria: gli ingressi normalizzati (~2,8 GB) stanno su disco in un memmap (data/lazio_sri/dataset_1h/cache/), il
territorio si aggiunge lotto per lotto, i bersagli si fanno dai mm lotto per lotto.

Taratura: regressione logistica su logit(p), fascia di distanza dalla costa e logit(p) × fascia, adattata
sull'insieme di arresto (mai usato per misurare), per lead e soglia; fasce sulla terra 0–5, 5–15, 15–30, oltre 30 km
dal mare; sul mare 0–5, 5–10, 10–20, 20–30, oltre 30 km dalla terra («meno affidabile», rete_dataset.affidabile).
Regola: la soglia di 4 mm sulla terra resta grezza (la taratura lì abbassava BSS e FSS, prova del 10/11); sul mare e
per 0,2 e 1 mm si tara ovunque. Dopo la taratura le soglie si riordinano. Coefficienti in taratura.json.

Misura sulla validazione, per zona: clima (mese × ora × zona, dall'addestramento), ICON tarato (isotonica della
pioggia oraria ICON-2I), U-Net grezza (media dei semi) e tarata: BSS, AUC; FSS 0–60 km sulla zona affidabile.
LightGBM a passo orario sulla divisione a blocchi non c'è: il confronto è con ICON tarato e con la versione a 3 h
per abilità relativa.

    python scripts/rete_unet_orario.py [--semi 0 1 2] [--epoche 40] [--alterna --nome alterna]   # meteo, MPS, ~1 h
    python scripts/rete_unet_orario.py --rimisura      # modelli già salvati: solo taratura e misura, ~15 min

--alterna: ogni epoca usa solo le ore pari o solo le dispari, alternando (le ore vicine sono quasi uguali e la
rete le imparava a memoria già all'epoca 2–3).

Uscite in logs/pioggia/unet/orario/: modello_seme<s>.pt, storia.csv, taratura.json, metriche.csv, fss.csv.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import rete_dataset as rd  # noqa: E402
import rete_unet as u  # noqa: E402

u.SOGLIE = [0.2, 1.0, 4.0]                                # mm nell'ora (rete_unet le usa per i bersagli)
SOGLIE = u.SOGLIE
DATI = rd.configurazione(1)["uscita"]
OUT = u.OUT / "orario"
FASCE_TERRA = [0, 5, 15, 30, np.inf]
FASCE_MARE = [0, 5, 10, 20, 30, np.inf]
NOMI_FASCE = ([f"terra {a:g}-{b:g}" for a, b in zip(FASCE_TERRA[:-1], FASCE_TERRA[1:])]
              + [f"mare {a:g}-{b:g}" for a, b in zip(FASCE_MARE[:-1], FASCE_MARE[1:])])
RIF = 3                                                   # fascia di riferimento della logistica: terra oltre 30 km
GREZZA = {4.0: [0, 1, 2, 3]}                              # soglia → fasce dove la taratura non si applica
ZONE = {"terra costa 0-15": [0, 1], "terra interno": [2, 3], "mare 0-10": [4, 5], "mare 10-30": [6, 7],
        "mare oltre 30": [8]}
logger = logging.getLogger("rete_unet_orario")


# ─────────────────────────────────────────────────────────── dati
def prepara() -> dict:
    file = [(k, f) for k in (1, 2) for f in sorted((DATI / f"lead{k}").glob("*.npz"))]
    cache = DATI / "cache"
    cache.mkdir(exist_ok=True)
    firma = [[k, f.name, f.stat().st_mtime] for k, f in file]
    tempi, scal, y, icon, ruolo = [], [], [], [], []
    canali = None
    for k, f in file:
        d = np.load(f)
        canali = list(d["canali"])
        tempi.append(d["tempi"]); scal.append(d["scalari"]); y.append(d["y"])
        icon.append(d["x"][:, canali.index("icon_p_fin")])
    dati = {"tempi": np.concatenate(tempi), "scalari": np.concatenate(scal), "y": np.concatenate(y),
            "icon_p_fin": np.concatenate(icon), "canali": canali}
    dati["ruolo"] = u.ruolo_interno(dati["tempi"], rd.ruolo(dati["tempi"]))
    n, c = len(dati["tempi"]), len(canali)
    meta_f = cache / "meta.json"
    meta = json.loads(meta_f.read_text()) if meta_f.exists() else {}
    if meta.get("firma") != firma:
        pioggia = [i for i, nome in enumerate(canali) if "_p_" in nome or nome.endswith("rovesci")]
        somma, quad, conta = np.zeros(c), np.zeros(c), np.zeros(c)
        trasf = lambda v, i: np.log1p(np.clip(v, 0, None)) if i in pioggia else v
        for k, f in file:                                 # 1° passo: statistiche sui soli campioni di addestramento
            d = np.load(f)
            tr = u.ruolo_interno(d["tempi"], rd.ruolo(d["tempi"])) == "train"
            x = d["x"][tr].astype("float32")
            for i in range(c):
                v = trasf(x[:, i], i)
                v = v[np.isfinite(v)]
                somma[i] += v.sum(dtype="float64"); quad[i] += (v.astype("float64") ** 2).sum(); conta[i] += v.size
        media = somma / conta
        sd = np.sqrt(np.maximum(quad / conta - media ** 2, 0)) + 1e-6
        mm = np.lib.format.open_memmap(cache / "x.npy", mode="w+", dtype="float16", shape=(n, c + 1, *rd.FORMA))
        pos = 0
        for k, f in file:                                 # 2° passo: ingressi normalizzati su disco
            x = np.load(f)["x"].astype("float32")
            for i in range(c):
                mm[pos:pos + len(x), i] = np.nan_to_num((trasf(x[:, i], i) - media[i]) / sd[i])
            mm[pos:pos + len(x), c] = np.isfinite(x[:, canali.index("icon_p_fin")])
            pos += len(x)
            logger.info(f"normalizzato {f.parent.name}/{f.name}")
        mm.flush()
        del mm
        meta = {"firma": firma, "media": media.tolist(), "sd": sd.tolist(), "canali": canali}
        meta_f.write_text(json.dumps(meta))
    dati["x"] = np.load(cache / "x.npy", mmap_mode="r")
    dati["stat"] = list(zip(canali, meta["media"], meta["sd"]))
    s = np.load(DATI / "statici.npz")
    st, nomi = s["x"], list(s["canali"])
    extra = np.stack([np.nan_to_num((st[i] - np.nanmean(st[i])) / (np.nanstd(st[i]) + 1e-6)) for i in range(len(nomi) - 1)]
                     + [(st[-1] == 1).astype("float32")]).astype("float32")
    dati["extra"], dati["statici"], dati["nomi_statici"] = extra, st, nomi
    dati["dist"] = rd.distanza_costa(st, nomi)
    return dati


def fasce(dist: np.ndarray) -> np.ndarray:
    """Indice di fascia (0–8, vedi NOMI_FASCE) per ogni cella; −1 fuori dal dominio."""
    f = np.full(dist.shape, -1)
    t, m = dist >= 0, dist < 0
    f[t] = np.digitize(dist[t], FASCE_TERRA[1:-1])
    f[m] = 4 + np.digitize(-dist[m], FASCE_MARE[1:-1])
    return f


# ─────────────────────────────────────────────────────────── taratura
def caratteristiche(p, f):
    q = np.clip(p, 1e-4, 1 - 1e-4)
    lg = np.log(q / (1 - q))
    oh = np.eye(len(NOMI_FASCE))[f]
    oh = np.delete(oh, RIF, axis=1)
    return np.column_stack([lg, oh, oh * lg[:, None]])


def adatta_taratura(p, y, f_cella, rng, massimo=2_000_000) -> dict:
    """p: campioni × soglie × H × W (media dei semi sull'arresto), y: mm. Una logistica per soglia."""
    ok = np.isfinite(y) & (f_cella[None] >= 0)
    t_, r_, c_ = np.nonzero(ok)
    if len(t_) > massimo:
        s = rng.choice(len(t_), massimo, replace=False)
        t_, r_, c_ = t_[s], r_[s], c_[s]
    ris = {}
    for n, s in enumerate(SOGLIE):
        lr = LogisticRegression(C=10, max_iter=2000).fit(caratteristiche(p[t_, n, r_, c_], f_cella[r_, c_]),
                                                         (y[t_, r_, c_] >= s).astype(int))
        ris[s] = {"coef": lr.coef_[0].tolist(), "intercetta": float(lr.intercept_[0])}
    return ris


def applica_taratura(p, f_cella, tar: dict) -> np.ndarray:
    """p: campioni × soglie × H × W → tarate e di nuovo ordinate (p(≥ 4) ≤ p(≥ 1) ≤ p(≥ 0,2))."""
    out = np.array(p, copy=True)
    dentro = f_cella >= 0
    r_, c_ = np.nonzero(dentro)
    for n, s in enumerate(SOGLIE):
        par = tar[s]
        v = p[:, n, r_, c_]                               # campioni × celle
        z = caratteristiche(v.ravel(), np.tile(f_cella[r_, c_], len(p))) @ np.array(par["coef"]) + par["intercetta"]
        q = (1 / (1 + np.exp(-z))).reshape(v.shape)
        resta = np.isin(f_cella[r_, c_], GREZZA.get(s, []))
        out[:, n, r_, c_] = np.where(resta[None], v, q)
    out[:, 1] = np.minimum(out[:, 1], out[:, 0])
    out[:, 2] = np.minimum(out[:, 2], out[:, 1])
    return out


# ─────────────────────────────────────────────────────────── misura
def climatologia(dati, tr, f_cella, s) -> pd.Series:
    """Frequenza di pioggia ≥ s per mese × ora × zona, dai campioni di addestramento."""
    tempi = pd.DatetimeIndex(dati["tempi"][tr])
    righe = []
    for a in range(0, len(tr), 1000):
        yy = dati["y"][tr[a:a + 1000]].astype("float32")
        for z, ff in ZONE.items():
            m = np.isin(f_cella, ff)
            v = yy[:, m]
            righe.append(pd.DataFrame({"mese": tempi.month[a:a + 1000], "ora": tempi.hour[a:a + 1000], "zona": z,
                                       "si": (v >= s).sum(1), "tot": np.isfinite(v).sum(1)}))
    d = pd.concat(righe).groupby(["mese", "ora", "zona"])[["si", "tot"]].sum()
    return d.si / d.tot.clip(lower=1)


def icon_tarato(dati, tr, f_cella, s, rng, massimo=3_000_000) -> IsotonicRegression:
    scelti = rng.choice(tr, min(len(tr), 3000), replace=False)
    yy = dati["y"][scelti].astype("float32")
    ii = dati["icon_p_fin"][scelti].astype("float32")
    ok = np.isfinite(yy) & np.isfinite(ii) & (f_cella[None] >= 0)
    v, t = ii[ok], (yy[ok] >= s).astype(int)
    if len(v) > massimo:
        sel = rng.choice(len(v), massimo, replace=False)
        v, t = v[sel], t[sel]
    return IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit(v, t)


def misura(dati, iv, p_semi, p_tar, f_cella, rng):
    import rete_fase1 as f1
    import ifs_corse_pioggia as ic
    lead = dati["scalari"][:, 4].astype(int)
    itr = np.where(dati["ruolo"] == "train")[0]
    zona_cella = np.full(f_cella.shape, "", object)
    for z, ff in ZONE.items():
        zona_cella[np.isin(f_cella, ff)] = z
    aff = rd.affidabile(dati["statici"], dati["nomi_statici"])
    metr, tab_fss = [], []
    for k in (1, 2):
        tr = itr[lead[itr] == k]
        sv = np.where(lead[iv] == k)[0]
        tempi = pd.DatetimeIndex(dati["tempi"][iv[sv]])
        yv = dati["y"][iv[sv]].astype("float32")
        iconv = dati["icon_p_fin"][iv[sv]].astype("float32")
        ok = np.isfinite(yv) & (f_cella[None] >= 0)
        t_, r_, c_ = np.nonzero(ok)
        for n, s in enumerate(SOGLIE):
            clima = climatologia(dati, tr, f_cella, s)
            iso = icon_tarato(dati, tr, f_cella, s, rng)
            d = pd.DataFrame({"finestra": tempi[t_], "i": r_, "j": c_, "zona": zona_cella[r_, c_],
                              "affidabile": aff[r_, c_], "piove": (yv[t_, r_, c_] >= s).astype(int),
                              "grezza": p_semi[:, sv][:, t_, n, r_, c_].mean(0), "tarata": p_tar[sv][t_, n, r_, c_]})
            for q in range(len(p_semi)):
                d[f"s{q}"] = p_semi[q, sv][t_, n, r_, c_]
            d["clima"] = pd.MultiIndex.from_arrays([d.finestra.dt.month, d.finestra.dt.hour, d.zona]).map(clima).to_numpy()
            d["clima"] = d.clima.fillna(d.piove.mean())
            iv_ = iconv[t_, r_, c_]
            d["icon_cal"] = np.where(np.isfinite(iv_), iso.predict(np.nan_to_num(iv_)), d.clima)
            gruppi = [("affidabile", d[d.affidabile]), *[(z, d[d.zona == z]) for z in ZONE]]
            for nome, g in gruppi:
                yy = g.piove.to_numpy().astype(float)
                c2 = ((g.clima - yy) ** 2).mean()
                riga = {"lead": k, "soglia_mm": s, "zona": nome, "n": len(g), "freq": yy.mean()}
                for m in ["icon_cal", "grezza", "tarata"]:
                    riga[f"BSS_{m}"] = 1 - ((g[m] - yy) ** 2).mean() / c2
                    riga[f"AUC_{m}"] = ic._auc(g[m].to_numpy(), yy.astype(int))
                semi = [1 - ((g[f"s{q}"] - yy) ** 2).mean() / c2 for q in range(len(p_semi))]
                riga["BSS_semi_media"], riga["BSS_semi_dev"] = np.mean(semi), np.std(semi, ddof=1) if len(semi) > 1 else np.nan
                metr.append(riga)
            a = d[d.affidabile]
            for m in ["icon_cal", "grezza", "tarata"]:
                for km, v in f1.fss(a, m).items():
                    tab_fss.append({"lead": k, "soglia_mm": s, "modello": m, "scala_km": km, "FSS": v,
                                    "FSS_utile": 0.5 + a.piove.mean() / 2})
            logger.info(f"misura lead {k}, soglia {s}: fatto")
    return pd.DataFrame(metr), pd.DataFrame(tab_fss)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epoche", type=int, default=40)
    ap.add_argument("--semi", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--alterna", action="store_true", help="ogni epoca solo le ore pari o solo le dispari, alternando")
    ap.add_argument("--nome", default="", help="sottocartella di logs/pioggia/unet/orario per questa prova")
    ap.add_argument("--pazienza", type=int, default=8)
    ap.add_argument("--rimisura", action="store_true", help="non allena: carica modello_seme*.pt, tara e misura")
    ap.add_argument("--vecchia", action="store_true",
                    help="dataset_1h_v: anche la pioggia della corsa di un giorno prima (rete_dataset.py --vecchia)")
    ap.add_argument("--frazione", type=float, default=1.0,
                    help="quota dei blocchi di addestramento da usare (curva di apprendimento; blocchi interi, annidati)")
    args = ap.parse_args()
    global OUT, DATI
    OUT = OUT / args.nome if args.nome else OUT
    if args.vecchia:
        DATI = rd.configurazione(1, True)["uscita"]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    OUT.mkdir(parents=True, exist_ok=True)
    disp = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
    dati = prepara()
    scal = dati["scalari"].astype("float32").copy()
    scal[:, 4] -= 1.5
    extra = torch.from_numpy(dati["extra"][None]).to(disp)
    ruolo = dati["ruolo"]
    if args.frazione < 1:
        # blocchi di 10 giorni interi, in un ordine fisso: la quota 0,5 è contenuta nella 0,75
        blocco = (pd.DatetimeIndex(dati["tempi"]).normalize() - pd.Timestamp("2025-01-01")).days.to_numpy() // 10
        tutti = np.unique(blocco[ruolo == "train"])
        tenuti = np.random.default_rng(0).permutation(tutti)[:int(round(args.frazione * len(tutti)))]
        ruolo = np.where((ruolo == "train") & ~np.isin(blocco, tenuti), "escluso", ruolo)
        # dati["ruolo"] resta intero: clima e ICON tarato della misura uguali per tutte le quote
    logger.info(f"{len(scal)} ore, {dati['x'].shape[1]} + {dati['extra'].shape[0]} canali, "
                f"ruoli {pd.Series(ruolo).value_counts().to_dict()}, {disp}")
    ia, iv = np.where(ruolo == "arresto")[0], np.where(ruolo == "valid")[0]
    pa, pv, storie = [], [], []
    for seme in args.semi:
        if args.rimisura:
            stato = torch.load(OUT / f"modello_seme{seme}.pt", map_location=disp, weights_only=False)["stato"]  # file nostri
            stato = {k.removeprefix("module."): v for k, v in stato.items() if k != "n_averaged"}
            rete = u.UNet(dati["x"].shape[1] + extra.shape[1], scal.shape[1], dropout=0.2).to(disp)
            rete.load_state_dict(stato)
            pa.append(u.prevedi(rete, dati["x"], scal, ia, disp, extra))
            pv.append(u.prevedi(rete, dati["x"], scal, iv, disp, extra))
            logger.info(f"seme {seme}: previsioni dal modello salvato")
            continue
        rete, storia = u.allena(dati["x"], dati["y"], scal, ruolo, args.epoche, disp, seme, lr=3e-4, wd=1e-3,
                                dropout=0.2, ema=0.995, pazienza=args.pazienza, extra=extra,
                                turno=(pd.DatetimeIndex(dati["tempi"]).hour.to_numpy() % 2) if args.alterna else None)
        torch.save({"stato": rete.state_dict(), "stat": [(c, float(m), float(s)) for c, m, s in dati["stat"]],
                    "canali": dati["canali"], "soglie": SOGLIE}, OUT / f"modello_seme{seme}.pt")
        storie.append(storia)
        pd.concat(storie).to_csv(OUT / "storia.csv", index=False)
        pa.append(u.prevedi(rete, dati["x"], scal, ia, disp, extra))
        pv.append(u.prevedi(rete, dati["x"], scal, iv, disp, extra))
    pa, pv = np.stack(pa), np.stack(pv)
    f_cella = fasce(dati["dist"])
    rng = np.random.default_rng(0)
    lead = scal[:, 4] + 1.5
    tar, p_tar = {}, np.empty(pv.shape[1:], "float32")
    for k in (1, 2):
        sa, sv = lead[ia] == k, lead[iv] == k
        tar[k] = adatta_taratura(pa[:, sa].mean(0), dati["y"][ia[sa]].astype("float32"), f_cella, rng)
        p_tar[sv] = applica_taratura(pv[:, sv].mean(0), f_cella, tar[k])
    (OUT / "taratura.json").write_text(json.dumps({"soglie": SOGLIE, "fasce": NOMI_FASCE, "riferimento": NOMI_FASCE[RIF],
                                                   "lead": {str(k): {str(s): v for s, v in t.items()} for k, t in tar.items()}},
                                                  indent=1))
    m, f = misura(dati, iv, pv, p_tar, f_cella, rng)
    m.round(4).to_csv(OUT / "metriche.csv", index=False)
    f.round(4).to_csv(OUT / "fss.csv", index=False)
    pd.set_option("display.width", 250)
    print(m.round(3).to_string(index=False))
    print("\nFSS sulla zona affidabile:\n", f.pivot_table(index=["lead", "soglia_mm", "modello"], columns="scala_km",
                                                          values="FSS").round(3))


if __name__ == "__main__":
    main()
