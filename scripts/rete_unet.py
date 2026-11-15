"""
rete_unet.py — Fase 3 del piano della rete (piano_rete_pioggia_nubi.md): prima U-Net sul Lazio, fonte A

Legge il dataset di rete_dataset.py (mappe 48 × 64, giorni 1 e 2 insieme, il lead entra come condizionamento),
divide con rete_dataset.ruolo (blocchi di 10 giorni, uno su cinque in validazione), normalizza con le statistiche
dei soli campioni di addestramento e allena una U-Net piccola:
  ingresso  16 canali dei modelli (pioggia in log(1 + mm)) + 8 del territorio + 2 maschere (ICON presente,
            dominio); i NaN, dopo la normalizzazione, valgono 0 e la maschera dice dove il dato manca
  rete      3 livelli (32, 64, 128 filtri), FiLM da ora, giorno dell'anno e lead in ogni blocco
  uscita    probabilità ordinate per costruzione: p(≥ 0,2) = σ(a), p(≥ 2) = p(≥ 0,2)·σ(b), p(≥ 5) = p(≥ 2)·σ(c)
  perdita   entropia binaria sulle tre soglie (mm nella finestra di 3 h), solo dove c'è il radar
Arresto anticipato su un terzo insieme, "arresto": un blocco di addestramento su dieci (con 2 giorni di distacco),
così la validazione non decide quando fermarsi e resta una misura pulita. Più semi (--semi): ogni seme allena una
rete; si riportano media e dispersione e anche la media delle probabilità dei semi ("unet_ens"). Poi la valutazione: sulle celle di terra della validazione,
nello stesso formato di rete_fase1 (--divisione blocchi), così si confronta con clima, ICON calibrato e LightGBM
sulle stesse finestre: BSS, AUC, FSS a 0–60 km.

    KMP_DUPLICATE_LIB_OK=TRUE python scripts/rete_unet.py [--epoche 40] [--semi 0 1 2]   # ambiente meteo, MPS

Uscite in logs/pioggia/unet/: modello_seme<s>.pt, storia.csv, confronto.csv e fss.csv (una riga per seme e
modello), riepilogo.csv (media e deviazione standard sui semi).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")        # torch e numpy portano due OpenMP nell'ambiente meteo

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import rete_dataset as rd  # noqa: E402

OUT = ROOT / "logs" / "pioggia" / "unet"
SOGLIE = [0.2, 2.0, 5.0]

logger = logging.getLogger("rete_unet")


# ─────────────────────────────────────────────────────────── dati
def carica(leads=(1, 2)) -> dict:
    parti = {k: [] for k in ("x", "y", "tempi", "scalari")}
    canali = None
    for k in leads:
        for f in sorted((rd.USCITA / f"lead{k}").glob("*.npz")):
            d = np.load(f)
            canali = list(d["canali"])
            for c in parti:
                parti[c].append(d[c])
    dati = {c: np.concatenate(v) for c, v in parti.items()}
    dati["canali"] = canali
    s = np.load(rd.USCITA / "statici.npz")
    dati["statici"], dati["nomi_statici"] = s["x"], list(s["canali"])
    dati["ruolo"] = ruolo_interno(dati["tempi"], rd.ruolo(dati["tempi"]))
    return dati


def ruolo_interno(tempi, ruolo: np.ndarray) -> np.ndarray:
    """Dall'addestramento si toglie un blocco di 10 giorni su dieci (blocco % 10 == 2) per l'arresto anticipato;
    i giorni di addestramento a meno di 2 da un blocco di arresto (e viceversa) diventano distacco."""
    giorno = ((pd.DatetimeIndex(tempi).normalize() - pd.Timestamp("2025-01-01")).days).to_numpy()
    arr = lambda d: (d // 10) % 10 == 2
    base = arr(giorno)
    vicini_uguali = np.stack([arr(giorno + s) == base for s in (-2, -1, 1, 2)]).all(0)
    out = ruolo.copy()
    tr = ruolo == "train"
    out[tr & base & vicini_uguali] = "arresto"
    out[tr & ~vicini_uguali] = "distacco"
    return out


def normalizza(dati: dict) -> tuple[np.ndarray, np.ndarray]:
    """Ingressi pronti per la rete (float16, N × C × 48 × 64) e bersagli (N × 3 × 48 × 64, NaN dove manca il radar)."""
    x = dati["x"]
    tr = dati["ruolo"] == "train"
    pioggia = [n for n, c in enumerate(dati["canali"]) if "_p_" in c or c.endswith("rovesci")]
    out = np.empty((len(x), x.shape[1] + len(dati["nomi_statici"]) - 1 + 2, *x.shape[2:]), "float16")
    stat = []
    for n in range(x.shape[1]):
        v = x[:, n].astype("float32")
        if n in pioggia:
            v = np.log1p(np.clip(v, 0, None))
        m, sd = np.nanmean(v[tr]), np.nanstd(v[tr]) + 1e-6
        out[:, n] = np.nan_to_num((v - m) / sd)
        stat.append((dati["canali"][n], m, sd))
    st = dati["statici"]
    for n in range(st.shape[0] - 1):                         # l'ultimo è la maschera del dominio
        v = st[n]
        out[:, x.shape[1] + n] = np.nan_to_num((v - np.nanmean(v)) / (np.nanstd(v) + 1e-6))
    icon_ok = np.isfinite(x[:, dati["canali"].index("icon_p_fin")])
    out[:, -2] = icon_ok
    out[:, -1] = st[-1] == 1
    y = dati["y"].astype("float32")
    bersagli = np.stack([np.where(np.isfinite(y), (y >= s).astype("float32"), np.nan) for s in SOGLIE], axis=1)
    dati["stat"] = stat
    return out, bersagli


# ─────────────────────────────────────────────────────────── rete
class FiLM(nn.Module):
    def __init__(self, n_scalari: int, canali: int):
        super().__init__()
        self.lin = nn.Linear(n_scalari, 2 * canali)

    def forward(self, h, s):
        g, b = self.lin(s).chunk(2, dim=1)
        return h * (1 + g[:, :, None, None]) + b[:, :, None, None]


class Blocco(nn.Module):
    def __init__(self, cin: int, cout: int, n_scalari: int):
        super().__init__()
        self.c1 = nn.Conv2d(cin, cout, 3, padding=1)
        self.c2 = nn.Conv2d(cout, cout, 3, padding=1)
        self.n1, self.n2 = nn.GroupNorm(8, cout), nn.GroupNorm(8, cout)
        self.film = FiLM(n_scalari, cout)

    def forward(self, h, s):
        h = F.gelu(self.n1(self.c1(h)))
        h = self.film(self.n2(self.c2(h)), s)
        return F.gelu(h)


class UNet(nn.Module):
    def __init__(self, cin: int, n_scalari: int, larghezze=(32, 64, 128), dropout: float = 0.1):
        super().__init__()
        a, b, c = larghezze
        self.e1, self.e2, self.e3 = Blocco(cin, a, n_scalari), Blocco(a, b, n_scalari), Blocco(b, c, n_scalari)
        self.d2, self.d1 = Blocco(c + b, b, n_scalari), Blocco(b + a, a, n_scalari)
        self.drop = nn.Dropout2d(dropout)
        self.testa = nn.Conv2d(a, len(SOGLIE), 1)

    def forward(self, x, s):
        h1 = self.e1(x, s)                                   # 48 × 64
        h2 = self.e2(F.max_pool2d(h1, 2), s)                 # 24 × 32
        h3 = self.drop(self.e3(F.max_pool2d(h2, 2), s))      # 12 × 16: ~45 km per pixel, vede tutto il Lazio
        u2 = self.d2(torch.cat([F.interpolate(h3, scale_factor=2), h2], 1), s)
        u1 = self.d1(torch.cat([F.interpolate(u2, scale_factor=2), h1], 1), s)
        z = self.testa(u1)
        # soglie ordinate: ogni probabilità è quella della soglia sotto per una condizionata
        lp = torch.cumsum(F.logsigmoid(z), dim=1)
        return lp                                            # log-probabilità, N × 3 × 48 × 64


PESI_SOGLIE: list[float] | None = None   # peso della perdita per soglia (es. [1, 1, 3]: più peso alla pioggia forte)


def perdita(lp, y):
    ok = torch.isfinite(y)
    yy = torch.nan_to_num(y)
    # entropia binaria su log p e log(1 - p) in modo stabile
    l1p = torch.log1p(-torch.exp(lp).clamp(max=1 - 1e-6))
    bce = -(yy * lp + (1 - yy) * l1p)
    if PESI_SOGLIE is not None:
        bce = bce * torch.tensor(PESI_SOGLIE, device=bce.device, dtype=bce.dtype).view(1, -1, 1, 1)
    return (bce * ok).sum() / ok.sum().clamp(min=1)


# ─────────────────────────────────────────────────────────── addestramento
def ingresso(x, idx, extra, dispositivo):
    """Lotto di ingressi: x[idx] (anche da un memmap) più, se c'è, extra (canali fissi S × H × W, uguali per tutti
    i campioni: territorio e maschera del dominio) ripetuto per ogni campione."""
    xb = torch.from_numpy(np.asarray(x[np.sort(idx)] if isinstance(x, np.memmap) else x[idx]).astype("float32"))
    if isinstance(x, np.memmap):                          # l'ordine del memmap è quello di np.sort(idx)
        xb = xb[torch.from_numpy(np.argsort(np.argsort(idx)))]
    xb = xb.to(dispositivo)
    if extra is not None:
        xb = torch.cat([xb, extra.expand(len(idx), -1, -1, -1)], 1)
    return xb


def bersagli_lotto(y: np.ndarray) -> np.ndarray:
    """Da mm (N × H × W, NaN dove manca il radar) a bersagli per soglia (N × 3 × H × W)."""
    y = y.astype("float32")
    return np.stack([np.where(np.isfinite(y), (y >= s).astype("float32"), np.nan) for s in SOGLIE], axis=1)


def allena(x, y, scalari, ruolo, epoche: int, dispositivo: str, seme: int, lr: float = 1e-3, wd: float = 1e-4,
           dropout: float = 0.1, ema: float = 0.0, pazienza: int = 6, extra=None,
           turno: np.ndarray | None = None, iniziale: dict | None = None) -> tuple[UNet, pd.DataFrame]:
    """ema > 0: media mobile esponenziale dei pesi (decadimento per passo); si valuta e si salva la rete mediata,
    che oscilla meno da un'epoca all'altra. y: bersagli N × 3 × H × W, oppure mm N × H × W (i bersagli si fanno
    lotto per lotto: meno memoria per l'orario). extra: canali fissi da aggiungere a ogni campione.
    turno: per campione 0 o 1 (es. ora pari o dispari); se c'è, ogni epoca usa solo l'addestramento di un turno,
    alternando: due ore vicine, quasi uguali, non stanno mai nella stessa epoca.
    iniziale: pesi da cui partire (rifinitura dopo un pre-addestramento)."""
    torch.manual_seed(seme)
    rng = np.random.default_rng(seme)
    i_tr, i_va = np.where(ruolo == "train")[0], np.where(ruolo == "arresto")[0]
    cin = x.shape[1] + (0 if extra is None else extra.shape[1])
    rete = UNet(cin, scalari.shape[1], dropout=dropout).to(dispositivo)
    if iniziale is not None:
        rete.load_state_dict(iniziale)
    media = torch.optim.swa_utils.AveragedModel(
        rete, multi_avg_fn=torch.optim.swa_utils.get_ema_multi_avg_fn(ema)) if ema > 0 else None
    logger.info(f"U-Net: {sum(p.numel() for p in rete.parameters()) / 1e6:.2f} M parametri; "
                f"seme {seme}: campioni {len(i_tr)} addestramento, {len(i_va)} arresto")
    ott = torch.optim.AdamW(rete.parameters(), lr=lr, weight_decay=wd)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(ott, T_max=epoche)
    lotto = lambda idx: (ingresso(x, idx, extra, dispositivo),
                         torch.from_numpy(scalari[idx]).to(dispositivo),
                         torch.from_numpy(bersagli_lotto(y[idx]) if y.ndim == 3 else y[idx]).to(dispositivo))
    storia, migliore, stallo, stato = [], np.inf, 0, None
    for ep in range(epoche):
        t0 = time.time()
        rete.train()
        tot = 0.0
        i_ep = i_tr if turno is None else i_tr[turno[i_tr] == ep % 2]
        for b in np.array_split(rng.permutation(i_ep), max(1, len(i_ep) // 32)):
            xb, sb, yb = lotto(b)
            l = perdita(rete(xb, sb), yb)
            ott.zero_grad(); l.backward(); ott.step()
            if media is not None:
                media.update_parameters(rete)
            tot += l.item() * len(b)
        sched.step()
        valuta = media.module if media is not None else rete
        valuta.eval()
        with torch.no_grad():
            lv = 0.0
            for b in np.array_split(i_va, max(1, len(i_va) // 64)):
                xb, sb, yb = lotto(b)
                lv += perdita(valuta(xb, sb), yb).item() * len(b)
        storia.append({"seme": seme, "epoca": ep + 1, "train": tot / len(i_ep), "arresto": lv / len(i_va),
                       "s": time.time() - t0})
        logger.info(f"seme {seme}, epoca {ep + 1}: train {tot / len(i_ep):.4f}, arresto {lv / len(i_va):.4f} "
                    f"({time.time() - t0:.0f} s)")
        if lv < migliore - 1e-5:
            migliore, stallo = lv, 0
            stato = {k: v.detach().cpu().clone() for k, v in valuta.state_dict().items()}
        else:
            stallo += 1
            if stallo >= pazienza:
                break
    rete.load_state_dict(stato)
    return rete, pd.DataFrame(storia)


def prevedi(rete, x, scalari, idx, dispositivo, extra=None) -> np.ndarray:
    rete.eval()
    out = []
    with torch.no_grad():
        for b in np.array_split(idx, max(1, len(idx) // 64)):
            xb = ingresso(x, b, extra, dispositivo)
            sb = torch.from_numpy(scalari[b]).to(dispositivo)
            out.append(torch.exp(rete(xb, sb)).cpu().numpy())
    return np.concatenate(out)


# ─────────────────────────────────────────────────────────── confronto
def confronto(dati: dict, prev: dict[str, np.ndarray], idx: np.ndarray):
    """U-Net contro clima, ICON calibrato e LightGBM (rete_fase1 --divisione blocchi) sulle stesse celle di terra."""
    import rete_fase1 as f1
    sys.path.insert(0, str(ROOT / "scripts"))
    import ifs_corse_pioggia as ic
    tempi = pd.DatetimeIndex(dati["tempi"][idx])
    lead = dati["scalari"][idx, 4].astype(int)
    metriche, fss = [], []
    for k in sorted(set(lead)):
        sel = np.where(lead == k)[0]
        for n, s in enumerate(SOGLIE):
            rif = pd.read_parquet(f1.OUT / f"previsioni_blocchi_lead{k}_{s}.parquet")
            r, c = rif.i.to_numpy() + rd.BORDO[0], rif.j.to_numpy() + rd.BORDO[1]
            pos = pd.Series(idx[sel], index=tempi[sel])         # posizione nel dataset intero, come p
            ok = rif.finestra.isin(pos.index).to_numpy()
            rif = rif[ok].copy()
            t = pos[rif.finestra].to_numpy()
            for nome, p in prev.items():
                rif[nome] = p[t, n, r[ok], c[ok]]
            rif = rif.dropna(subset=list(prev))
            y = rif.piove.to_numpy().astype(float)
            bc = ((rif.clima - y) ** 2).mean()
            for mod in ["clima", "icon_cal", "lgbm", *prev]:
                metriche.append({"lead": k, "soglia_mm": s, "modello": mod, "n": len(rif), "freq": y.mean(),
                                 "BSS": 1 - ((rif[mod] - y) ** 2).mean() / bc, "AUC": ic._auc(rif[mod].to_numpy(), y.astype(int))})
                for km, v in f1.fss(rif, mod).items():
                    fss.append({"lead": k, "soglia_mm": s, "modello": mod, "scala_km": km, "FSS": v, "FSS_utile": 0.5 + y.mean() / 2})
    return pd.DataFrame(metriche), pd.DataFrame(fss)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epoche", type=int, default=40)
    ap.add_argument("--semi", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--wd", type=float, default=1e-3)
    ap.add_argument("--dropout", type=float, default=0.2)
    ap.add_argument("--ema", type=float, default=0.995, help="decadimento EMA dei pesi per passo, es. 0,995; 0 = no")
    ap.add_argument("--pazienza", type=int, default=8)
    ap.add_argument("--nome", default="", help="sottocartella di logs/pioggia/unet per questa prova")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    global OUT
    OUT = OUT / args.nome if args.nome else OUT
    OUT.mkdir(parents=True, exist_ok=True)
    logger.info(f"prova {args.nome or 'base'}: lr {args.lr}, wd {args.wd}, dropout {args.dropout}, ema {args.ema}")
    disp = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
    dati = carica()
    x, y = normalizza(dati)
    del dati["x"], dati["y"]                                  # restano solo le copie normalizzate
    scal = dati["scalari"].astype("float32").copy()
    scal[:, 4] -= 1.5                                         # lead 1/2 → ±0,5
    logger.info(f"{len(x)} campioni, {x.shape[1]} canali, ruoli {pd.Series(dati['ruolo']).value_counts().to_dict()}, {disp}")
    idx = np.where(dati["ruolo"] == "valid")[0]
    prev, storie = {}, []
    for seme in args.semi:
        rete, storia = allena(x, y, scal, dati["ruolo"], args.epoche, disp, seme, args.lr, args.wd, args.dropout,
                               args.ema, args.pazienza)
        torch.save({"stato": rete.state_dict(), "stat": dati["stat"], "canali": dati["canali"]}, OUT / f"modello_seme{seme}.pt")
        storie.append(storia)
        pp = np.full((len(x), len(SOGLIE), *x.shape[2:]), np.nan, "float32")
        pp[idx] = prevedi(rete, x, scal, idx, disp)
        prev[f"unet_s{seme}"] = pp
        pd.concat(storie).to_csv(OUT / "storia.csv", index=False)
    if len(prev) > 1:
        prev["unet_ens"] = np.mean([v for v in prev.values()], axis=0)
    m, f = confronto(dati, prev, idx)
    m.round(4).to_csv(OUT / "confronto.csv", index=False)
    f.round(4).to_csv(OUT / "fss.csv", index=False)
    # riepilogo: semi singoli → media e deviazione standard
    gruppo = lambda d: d.assign(modello=d.modello.where(~d.modello.str.match(r"unet_s\d"), "unet_semi"))
    chiavi = ["lead", "soglia_mm", "modello"]
    r = gruppo(m).groupby(chiavi)[["BSS", "AUC"]].agg(["mean", "std"])
    r.columns = [f"{a}_{b}" for a, b in r.columns]
    r.round(4).to_csv(OUT / "riepilogo.csv")
    pd.set_option("display.width", 220)
    print(r.round(3))
    print("\nFSS per scala (km), media sui semi:\n",
          gruppo(f).pivot_table(index=chiavi, columns="scala_km", values="FSS", aggfunc="mean").round(3))


if __name__ == "__main__":
    main()
