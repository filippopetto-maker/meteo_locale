"""
verifica_pioggia_ombra.py — le previsioni della pioggia in ombra (rete_ombra.py) contro il radar DPC

Previsioni: asset pioggia_AAAAMMGGHH.npz delle release pioggia-ombra-AAAA-MM di metek-core.
Verità: asset sri_italia_AAAAMMGG.npz delle release radar-AAAA-MM (archivio_radar.py, passi da 5 min sulle celle
0,05°), come il bersaglio dell'addestramento: media dei passi nell'ora (almeno 9 su 12), × fattore dei pluviometri
MeteoNetwork per cella e stagione (fattore_radar_mnw.parquet; 1 se manca).

Zona affidabile (terra e mare entro 30 km), per fascia di anticipo (1–12, 13–24, 25–36, 37–48 h) e in tutto:
- per soglia (0,2 / 1 / 4 mm): frequenza osservata, probabilità media, Brier, BSS sul clima del periodo (la frequenza
  osservata nel periodo stesso: riferimento comodo ma ottimista per il clima), AUC;
- mm: media osservata e prevista, correlazione oraria; totali di 24 h (giorno UTC, corse con il giorno intero).

I file scaricati restano in data/lazio_sri/verifica_ombra/ (si riscaricano solo i nuovi); i risultati in
logs/pioggia/verifica_ombra/.

    python scripts/verifica_pioggia_ombra.py [--dal 2026-11-15] [--al 2026-11-30]      # ambiente meteo
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import griglia_lazio as g  # noqa: E402
import rete_dataset as rd  # noqa: E402

REPO = "filippopetto-maker/metek-core"
CACHE = ROOT / "data" / "lazio_sri" / "verifica_ombra"
OUT = ROOT / "logs" / "pioggia" / "verifica_ombra"
MOD = ROOT / "model" / "pioggia_oraria"
SOGLIE = [0.2, 1.0, 4.0]
FASCE = [(1, 12), (13, 24), (25, 36), (37, 48)]
logger = logging.getLogger("verifica_pioggia_ombra")


def scarica(tag: str, schema: str, cartella: Path) -> list[Path]:
    """Gli asset della release che corrispondono allo schema, solo quelli non ancora in cartella."""
    cartella.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["gh", "release", "view", tag, "-R", REPO, "--json", "assets", "-q", ".assets[].name"],
                       capture_output=True, text=True)
    if r.returncode:
        mese = tag[-7:].replace("-", "")                                       # AAAAMM della release
        locali = sorted(f for f in cartella.glob(schema) if mese in f.name)
        logger.warning(f"{tag}: elenco non disponibile ({r.stderr.strip()[:120]}), uso i {len(locali)} file locali")
        return locali
    nomi = [n for n in r.stdout.split() if Path(n).match(schema)]
    mancanti = [n for n in nomi if not (cartella / n).exists()]
    for n in mancanti:
        subprocess.run(["gh", "release", "download", tag, "-R", REPO, "-p", n, "--dir", str(cartella)], check=True)
    if mancanti:
        logger.info(f"{tag}: scaricati {len(mancanti)} file")
    return sorted(cartella / n for n in nomi)


def radar_orario(file: Path, celle: pd.DataFrame, fattore: pd.DataFrame) -> tuple[pd.DatetimeIndex, np.ndarray]:
    """(ore, 48, 64) mm nell'ora sulla griglia della rete; NaN dove manca."""
    d = np.load(file)
    ii = pd.Index(np.round(d["lat"], 3)).get_indexer(np.round(celle.lat, 3))
    jj = pd.Index(np.round(d["lon"], 3)).get_indexer(np.round(celle.lon, 3))
    assert (ii >= 0).all() and (jj >= 0).all(), "celle del Lazio fuori dalla griglia del radar"
    v = d["pioggia_media"][:, ii, jj].astype("float32")              # passi × celle, mm/h × 100
    v[v < 0] = np.nan
    v /= 100
    ora = pd.DatetimeIndex(d["tempi"]).floor("h")
    df = pd.DataFrame(v).groupby(ora)
    media, conta = df.mean().to_numpy().copy(), df.count().to_numpy()
    media[conta < 9] = np.nan
    ore = pd.DatetimeIndex(sorted(set(ora)))
    stagione = np.where(ore.month.isin(range(4, 10)), "calda", "fredda")
    f = fattore.set_index(["i", "j", "stagione"]).fattore
    y = np.full((len(ore), *rd.FORMA), np.nan, "float32")
    R, C = celle.i.to_numpy() + rd.BORDO[0], celle.j.to_numpy() + rd.BORDO[1]
    for s in np.unique(stagione):
        k = stagione == s
        fc = f.reindex(pd.MultiIndex.from_arrays([celle.i, celle.j, np.full(len(celle), s)])).fillna(1.0).to_numpy()
        blocco = y[k]
        blocco[:, R, C] = media[k] * fc
        y[k] = blocco
    return ore, y


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dal", default="2026-11-15")
    ap.add_argument("--al", default=None, help="ultimo giorno di corse (default: tutte)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    dal = pd.Timestamp(args.dal)
    al = pd.Timestamp(args.al) + pd.Timedelta(days=1) if args.al else pd.Timestamp.now("UTC").tz_localize(None)
    mesi = pd.period_range(dal, al + pd.Timedelta(days=2), freq="M")
    prev = [f for m in mesi for f in scarica(f"pioggia-ombra-{m}", "pioggia_*.npz", CACHE / "ombra")]
    radar = [f for m in mesi for f in scarica(f"radar-{m}", "sri_italia_*.npz", CACHE / "radar")]
    celle = g.celle()
    fattore = pd.read_parquet(g.LOCALE / "fattore_radar_mnw.parquet")
    oss = {}
    for f in radar:
        if pd.Timestamp(f.stem[-8:]) < dal:
            continue
        ore, y = radar_orario(f, celle, fattore)
        oss.update({t: y[n] for n, t in enumerate(ore)})
    logger.info(f"radar: {len(oss)} ore, {min(oss):%d/%m %H} – {max(oss):%d/%m %H}")
    s = np.load(MOD / "statici.npz")
    zona = rd.affidabile(s["x"], list(s["canali"]))

    righe_p, righe_y, totali = [], [], []
    for f in prev:
        corsa = pd.Timestamp(f.stem[-10:-2] + "T" + f.stem[-2:])
        if not dal <= corsa < al:
            continue
        d = np.load(f)
        tempi = pd.DatetimeIndex(d["tempi"])
        ok = np.array([t in oss for t in tempi])
        if not ok.any():
            continue
        y = np.stack([oss[t] for t in tempi[ok]])
        p = d["p"][ok].astype("float32")
        mm = d["mm"][ok].astype("float32") if "mm" in d else np.full(y.shape, np.nan, "float32")
        lead = d["lead_h"][ok]
        m = np.isfinite(y) & zona & np.isfinite(p[:, 0])
        for n in range(len(y)):
            v = m[n]
            righe_p.append(pd.DataFrame({"corsa": corsa, "lead": int(lead[n]), "y": y[n][v],
                                         **{f"p{k}": p[n, k][v] for k in range(3)}, "mm": mm[n][v]}))
        giorno = tempi[ok].normalize()
        for gg in np.unique(giorno):
            k = giorno == gg
            if k.sum() == 24:
                v = m[k].all(0)
                totali.append(pd.DataFrame({"corsa": corsa, "giorno": gg, "lead_medio": float(lead[k].mean()),
                                            "oss": y[k].sum(0)[v], "prev": mm[k].sum(0)[v]}))
        logger.info(f"corsa {corsa:%d/%m %HZ}: {ok.sum()} ore verificabili su {len(tempi)}")
    if not righe_p:
        logger.warning("nessuna ora verificabile")
        return
    t = pd.concat(righe_p, ignore_index=True)
    t["fascia"] = pd.cut(t.lead, [0, 12, 24, 36, 48], labels=[f"{a}–{b} h" for a, b in FASCE])

    def misura(x: pd.DataFrame) -> dict:
        r = {"n": len(x), "corse": x.corsa.nunique()}
        for k, sg in enumerate(SOGLIE):
            o = (x.y >= sg).astype("float32")
            p = x[f"p{k}"]
            bs, bs_c = ((p - o) ** 2).mean(), o.mean() * (1 - o.mean())
            r.update({f"freq_{sg}": o.mean(), f"p_media_{sg}": p.mean(), f"BSS_{sg}": 1 - bs / bs_c if bs_c else np.nan,
                      f"AUC_{sg}": roc_auc_score(o, p) if 0 < o.sum() < len(o) else np.nan})
        r.update({"mm_oss": x.y.mean(), "mm_prev": x.mm.mean(), "mm_corr": x[["y", "mm"]].corr().iloc[0, 1]})
        return r

    tab = pd.DataFrame([{"fascia": "tutte", **misura(t)}] +
                       [{"fascia": str(fa), **misura(x)} for fa, x in t.groupby("fascia", observed=True)])
    OUT.mkdir(parents=True, exist_ok=True)
    fine = t.corsa.max()
    tab.round(4).to_csv(OUT / f"metriche_{dal:%Y%m%d}_{fine:%Y%m%d%H}.csv", index=False)
    pd.set_option("display.width", 250)
    print(f"\ncorse {dal:%d/%m} – {fine:%d/%m %HZ}, zona affidabile")
    print(tab.round(3).to_string(index=False))
    if totali:
        tt = pd.concat(totali, ignore_index=True)
        per = tt.groupby("giorno").agg(oss=("oss", "mean"), prev=("prev", "mean"), corse=("corsa", "nunique"))
        print("\ntotali di 24 h (media sulle celle):")
        print(per.round(2).to_string())
        print(f"in tutto: oss {tt.oss.mean():.2f} mm, prev {tt.prev.mean():.2f} mm, "
              f"correlazione per cella {np.corrcoef(tt.oss, tt.prev)[0, 1]:.2f}")


if __name__ == "__main__":
    main()
