"""
rete_ombra.py — pioggia oraria da +1 a +48 h sul Lazio con la U-Net (insieme), in ombra

Dopo ogni corsa ICON-2I (00Z e 12Z, su AWS ~2 h dopo) si costruiscono gli stessi ingressi dell'addestramento e si
salvano le probabilità orarie di ≥ 0,2 / 1 / 4 mm su tutte le celle 0,05°. Nessun effetto sul sito: le previsioni
servono alla verifica dal vivo contro il radar.

Ingressi, come in rete_dataset.py (stesse funzioni):
- ICON-2I: la corsa intera da s3://openmeteo/data_run/italia_meteo_arpae_icon_2i/ (raffiche da m/s a km/h come la
  Previous Runs), il pixel più vicino al centro di ogni cella (la Previous Runs dà il punto di griglia più vicino);
- GFS (gfs_seamless) ai 110 punti 0,25° dall'API Forecast di Open-Meteo (110 chiamate a corsa).
Il lead negli scalari e nella taratura: 1 fino a +35 h dall'inizio della corsa, 2 oltre (in addestramento: giorno 1
≈ 24–35 h, giorno 2 ≈ 48–59 h; le ore più vicine sono più facili di come la rete le tratta).

I modelli stanno in model/pioggia_oraria/ (--impacchetta li copia da logs/pioggia/unet/orario/<nome>/, con
normalizzazione, taratura, statici e celle): in Actions si scaricano dalla release di metek-core.

    python scripts/rete_ombra.py --impacchetta insieme       # una volta, sul Mac
    python scripts/rete_ombra.py [--corsa 2026-11-14T12] [--uscita ombra/]

Con --storia (dal 21/11): le probabilità tarate si correggono con quanto la rete ha sbagliato nei giorni appena
verificati contro il radar (rete_correzione.py); p è la corretta, p_tarata quella di prima, correzione i coefficienti.

Uscita: <uscita>/pioggia_AAAAMMGGHH.npz con p (ore × 3 soglie × 48 × 64, tarata), p_grezza, mm (mm attesi
nell'ora, rete_mm.py, se nel pacchetto c'è mm.json), tempi (inizio dell'ora
prevista, UTC), lead_h, soglie, corsa.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import griglia_lazio as g  # noqa: E402
import rete_dataset as rd  # noqa: E402
import rete_unet as u  # noqa: E402
import rete_unet_orario as o  # noqa: E402

MOD = ROOT / "model" / "pioggia_oraria"
AWS = "https://openmeteo.s3.amazonaws.com/data_run/italia_meteo_arpae_icon_2i"
FORECAST = "https://api.open-meteo.com/v1/forecast"
ORE = 48
UNITA = {"wind_gusts_10m": 3.6}
logger = logging.getLogger("rete_ombra")


# ─────────────────────────────────────────────────────────── pacchetto dei modelli
def impacchetta(nome: str):
    sorg = o.OUT / nome
    if MOD.exists():
        shutil.rmtree(MOD)
    MOD.mkdir(parents=True)
    for n, f in enumerate(sorted(sorg.glob("modello_seme*.pt"))):
        stato = torch.load(f.resolve(), map_location="cpu", weights_only=False)["stato"]   # file nostri
        stato = {k.removeprefix("module."): v for k, v in stato.items() if k != "n_averaged"}
        torch.save(stato, MOD / f"rete{n}.pt")
    meta = json.loads((o.DATI / "cache" / "meta.json").read_text())
    (MOD / "normalizza.json").write_text(json.dumps({k: meta[k] for k in ("canali", "media", "sd")}))
    shutil.copy(sorg / "taratura.json", MOD / "taratura.json")
    if (sorg / "mm" / "mm.json").exists():                                    # rete_mm.py
        shutil.copy(sorg / "mm" / "mm.json", MOD / "mm.json")
    shutil.copy(o.DATI / "statici.npz", MOD / "statici.npz")
    for f in ("descrittori_celle.parquet", "punti_nwp.parquet", "fattore_radar_mnw.parquet"):
        shutil.copy(g.LOCALE / f, MOD / f)
    (MOD / "LEGGIMI.txt").write_text(f"U-Net oraria della pioggia, insieme '{nome}' ({n + 1} reti), "
                                     f"impacchettata il {pd.Timestamp.now():%d/%m/%Y}. Vedi scripts/rete_ombra.py.\n")
    logger.info(f"{n + 1} reti e file di supporto in {MOD}")


# ─────────────────────────────────────────────────────────── ingressi
def ultima_corsa() -> pd.Timestamp:
    """L'ultima corsa ICON-2I completa su AWS (meta.json presente)."""
    adesso = pd.Timestamp.now("UTC").tz_localize(None).floor("12h")
    for k in range(6):
        t = adesso - pd.Timedelta(hours=12 * k)
        if requests.head(f"{AWS}/{t:%Y/%m/%d/%H%M}Z/meta.json", timeout=30).status_code == 200:
            return t
    raise RuntimeError("nessuna corsa ICON-2I su AWS negli ultimi 3 giorni")


def icon_corsa(t0: pd.Timestamp, c: pd.DataFrame, cartella: Path):
    """Un parquet per cella nel formato della Previous Runs (<variabile>_previous_day1, ora = inizio dell'ora)."""
    import omfiles
    G = g.ICON_GRIGLIA
    ii = np.rint((c.lat.to_numpy() - G["lat0"]) / G["dlat"]).astype(int)
    jj = np.rint((c.lon.to_numpy() - G["lon0"]) / G["dlon"]).astype(int)
    base = f"{AWS}/{t0:%Y/%m/%d/%H%M}Z"
    validi = pd.to_datetime(requests.get(f"{base}/meta.json", timeout=60).json()["valid_times"]).tz_localize(None)
    serie = {}
    with tempfile.TemporaryDirectory() as tmp:
        for v in g.VAR_LEAD_ICON:
            r = requests.get(f"{base}/{v}.om", timeout=300)
            r.raise_for_status()
            f = Path(tmp) / f"{v}.om"
            f.write_bytes(r.content)
            a = omfiles.OmFileReader.from_path(str(f))
            i0, i1, j0, j1 = ii.min(), ii.max() + 1, jj.min(), jj.max() + 1
            cubo = a[i0:i1, j0:j1, :]
            t = validi[-cubo.shape[2]:]                    # la pioggia non ha il passo 0
            serie[v] = pd.DataFrame(cubo[ii - i0, jj - j0].T * UNITA.get(v, 1), index=t)
    tempi = serie["precipitation"].index
    cartella.mkdir(parents=True, exist_ok=True)
    for q, x in enumerate(c.itertuples(index=False)):
        f = pd.DataFrame({f"{v}_previous_day1": serie[v][q].reindex(tempi).to_numpy("float32") for v in serie})
        f["ora"] = tempi - pd.Timedelta(hours=1)          # somma dell'ora precedente
        f.to_parquet(cartella / f"{x.i * 1000 + x.j:04d}.parquet", index=False)
    return tempi


def gfs_corsa(t0: pd.Timestamp, cartella: Path):
    """GFS ai punti 0,25° dall'API Forecast (la stessa serie della Previous Runs, alla corsa più recente)."""
    punti = pd.read_parquet(g.LOCALE / "punti_nwp.parquet")
    par = {"latitude": ",".join(f"{a:.4f}" for a in punti.lat), "longitude": ",".join(f"{b:.4f}" for b in punti.lon),
           "hourly": ",".join(g.VAR_LEAD_GFS), "models": "gfs_seamless", "timezone": "UTC",
           "start_date": f"{t0 - pd.Timedelta(days=1):%Y-%m-%d}", "end_date": f"{t0 + pd.Timedelta(days=3):%Y-%m-%d}"}
    for tentativo in range(5):
        try:
            r = requests.get(FORECAST, params=par, timeout=120)
            r.raise_for_status()
            break
        except requests.RequestException as e:
            logger.warning(f"GFS: {e}, riprovo")
            time.sleep(30 * (tentativo + 1))
    else:
        raise RuntimeError("GFS non scaricabile")
    d = r.json()
    cartella.mkdir(parents=True, exist_ok=True)
    for p, dd in zip(punti.punto, d if isinstance(d, list) else [d]):
        f = pd.DataFrame(dd["hourly"])
        f["ora"] = pd.to_datetime(f.pop("time")) - pd.Timedelta(hours=1)
        f = f.rename(columns={v: f"{v}_previous_day1" for v in g.VAR_LEAD_GFS})
        f.to_parquet(cartella / f"{p:04d}.parquet", index=False)


def ingressi(t0: pd.Timestamp, lead_dir: Path) -> tuple[np.ndarray, pd.DatetimeIndex, list[str]]:
    """Ingressi grezzi ore × canali × 48 × 64 per le ore da t0 a t0 + 48 h, con le funzioni di rete_dataset."""
    g.LEAD_DIR = lead_dir
    g.INIZIO_ICON = f"{t0 - pd.Timedelta(days=1):%Y-%m-%d}"
    cfg = rd.configurazione(1)
    c = rd.celle()
    canali = [f"icon_{n}" for n in cfg["ordine_icon"]] + [f"gfs_{n}" for n in cfg["ordine_gfs"]]
    arr, finestre, ids, w = rd.serie_gfs(c, 1, cfg)
    icon = rd.serie_icon(c, 1, finestre, cfg)
    sel = np.where((finestre >= t0) & (finestre < t0 + pd.Timedelta(hours=ORE)))[0]
    x = np.concatenate([rd.icon_mese(c, icon, sel), rd.gfs_mese(c, arr, ids, w, sel)], axis=0)   # C × t × H × W
    return x.transpose(1, 0, 2, 3), finestre[sel], canali


# ─────────────────────────────────────────────────────────── previsione
def prevedi(x: np.ndarray, tempi: pd.DatetimeIndex, t0: pd.Timestamp, canali: list[str]) -> dict:
    nz = json.loads((MOD / "normalizza.json").read_text())
    assert nz["canali"] == canali, "canali diversi da quelli dell'addestramento"
    pioggia = [i for i, n in enumerate(canali) if "_p_" in n or n.endswith("rovesci")]
    xn = np.empty((len(x), len(canali) + 1, *x.shape[2:]), "float32")
    for i in range(len(canali)):
        v = np.log1p(np.clip(x[:, i], 0, None)) if i in pioggia else x[:, i]
        xn[:, i] = np.nan_to_num((v - nz["media"][i]) / nz["sd"][i])
    xn[:, -1] = np.isfinite(x[:, canali.index("icon_p_fin")])
    s = np.load(MOD / "statici.npz")
    st, nomi = s["x"], list(s["canali"])
    extra = np.stack([np.nan_to_num((st[i] - np.nanmean(st[i])) / (np.nanstd(st[i]) + 1e-6)) for i in range(len(nomi) - 1)]
                     + [(st[-1] == 1).astype("float32")]).astype("float32")      # come rete_unet_orario.prepara
    lead_h = ((tempi - t0) / pd.Timedelta(hours=1)).to_numpy() + 1              # fine dell'ora prevista
    k = np.where(lead_h <= 35, 1, 2)
    centro = tempi + pd.Timedelta(minutes=30)
    ora, doy = centro.hour + centro.minute / 60, centro.dayofyear
    scal = np.stack([np.sin(2 * np.pi * ora / 24), np.cos(2 * np.pi * ora / 24), np.sin(2 * np.pi * doy / 365.25),
                     np.cos(2 * np.pi * doy / 365.25), k - 1.5], axis=1).astype("float32")
    e = torch.from_numpy(extra[None])
    idx = np.arange(len(xn))
    p = []
    for f in sorted(MOD.glob("rete*.pt")):
        rete = u.UNet(xn.shape[1] + extra.shape[0], scal.shape[1], dropout=0.2)
        rete.load_state_dict(torch.load(f, map_location="cpu", weights_only=True))
        p.append(u.prevedi(rete, xn, scal, idx, "cpu", e))
    grezza = np.mean(p, axis=0)
    tar = json.loads((MOD / "taratura.json").read_text())["lead"]
    tar = {int(a): {float(s_): v for s_, v in d.items()} for a, d in tar.items()}
    f_cella = o.fasce(rd.distanza_costa(st, nomi))
    tarata = np.empty_like(grezza)
    for kk in (1, 2):
        m = k == kk
        if m.any():
            tarata[m] = o.applica_taratura(grezza[m], f_cella, tar[kk])
    fuori = st[-1] != 1
    tarata[:, :, fuori] = np.nan
    grezza[:, :, fuori] = np.nan
    return {"p": tarata.astype("float16"), "p_grezza": grezza.astype("float16"),
            "tempi": tempi.values.astype("datetime64[m]"), "lead_h": lead_h.astype("int16"),
            "soglie": np.array(o.SOGLIE), "corsa": np.datetime64(t0, "m"), "reti": len(p)}


def stima_mm(p: np.ndarray, lead_h: np.ndarray) -> np.ndarray | None:
    """mm attesi (rete_mm.py): a(p0,2 − p1) + b(p1 − p4) + c·p4, coefficienti per lead e terra / mare."""
    if not (MOD / "mm.json").exists():
        return None
    coef = json.loads((MOD / "mm.json").read_text())["lead"]
    s = np.load(MOD / "statici.npz")
    terra = rd.distanza_costa(s["x"], list(s["canali"])) >= 0
    pm = np.minimum.accumulate(np.clip(p.astype("float32"), 0, 1), axis=1)
    q = np.stack([pm[:, 0] - pm[:, 1], pm[:, 1] - pm[:, 2], pm[:, 2]], axis=1)
    k = np.where(lead_h <= 35, 1, 2)
    mm = np.full((len(q), *q.shape[2:]), np.nan, "float32")
    for kk in (1, 2):
        m = k == kk
        if not m.any():
            continue
        c = coef[str(kk)]
        blocco = np.einsum("nkhw,k->nhw", q[m], np.asarray(c["mare"], "float32"))
        blocco[:, terra] = np.einsum("nkp,k->np", q[m][:, :, terra], np.asarray(c["terra"], "float32"))
        mm[m] = blocco
    return mm.astype("float16")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--impacchetta", metavar="NOME", help="copia i modelli di logs/pioggia/unet/orario/NOME")
    ap.add_argument("--corsa", default=None, help="AAAA-MM-GGTHH (default: l'ultima su AWS)")
    ap.add_argument("--uscita", type=Path, default=ROOT / "ombra")
    ap.add_argument("--storia", type=Path, default=None,
                    help="cartella con ombra/ (corse passate) e radar/ (sri_italia_*.npz) per rete_correzione.py")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if args.impacchetta:
        return impacchetta(args.impacchetta)
    g.LOCALE = MOD                                        # celle e punti dal pacchetto, non da data/
    t0 = pd.Timestamp(args.corsa) if args.corsa else ultima_corsa()
    logger.info(f"corsa ICON-2I {t0:%d/%m/%Y %H}Z")
    with tempfile.TemporaryDirectory() as tmp:
        lead = Path(tmp)
        icon_corsa(t0, rd.celle(), lead / "icon")
        gfs_corsa(t0, lead / "gfs")
        x, tempi, canali = ingressi(t0, lead)
    logger.info(f"ingressi: {x.shape}, ore {tempi[0]:%d/%m %H} – {tempi[-1]:%d/%m %H}, "
                f"ICON presente {np.isfinite(x[:, canali.index('icon_p_fin')]).mean():.0%}")
    ris = prevedi(x, tempi, t0, canali)
    if args.storia:
        import rete_correzione as rc
        s = np.load(MOD / "statici.npz")
        zona = rd.affidabile(s["x"], list(s["canali"]))
        p_corr, info = rc.correggi(ris["p"], t0, args.storia, rd.celle(),
                                   pd.read_parquet(MOD / "fattore_radar_mnw.parquet"), zona, o.SOGLIE)
        ris["p_tarata"], ris["p"] = ris["p"], p_corr.astype("float16")
        ris["correzione"] = np.array(json.dumps(info))
    mm = stima_mm(ris["p"], ris["lead_h"])
    if mm is not None:
        ris["mm"] = mm
    args.uscita.mkdir(parents=True, exist_ok=True)
    f = args.uscita / f"pioggia_{t0:%Y%m%d%H}.npz"
    np.savez_compressed(f, **ris)
    p = ris["p"].astype("float32")
    logger.info(f"{f.name}: {ris['reti']} reti, {f.stat().st_size / 1e6:.1f} MB; p(≥ 0,2 mm) massima per giorno "
                f"{np.nanmax(p[ris['lead_h'] <= 24, 0]):.2f} / {np.nanmax(p[ris['lead_h'] > 24, 0]):.2f}")
    print(f)


if __name__ == "__main__":
    main()
