"""
rete_dataset.py — Fase 2 del piano della rete (piano_rete_pioggia_nubi.md): dataset di mappe per la U-Net, fonte A

Fonte A = Previous Runs (lead veri giorno 1 e 2, dal 13/04/2025, griglia_lazio.py lead e lead_mare). Un campione
è una finestra di 3 h valida prevista k giorni prima: mappa 48 × 64 (le 46 × 56 celle 0,05° del dominio Lazio,
con un bordo di 1 riga sopra e sotto e 4 colonne ai lati, fuori dal dominio = NaN). Terra e mare insieme: la
pioggia in arrivo dal Tirreno serve a vedere la convezione prima della costa.

Per lead e mese, data/lazio_sri/dataset/lead<k>/AAAA_MM.npz:
  x        (N, C, 48, 64) float16  canali dinamici, valori fisici grezzi (la normalizzazione la fa chi legge,
                                   con le statistiche dei soli mesi di addestramento); NaN dove manca il dato
  canali   (C,)                    nomi dei canali (icon_*, gfs_*)
  y        (N, 48, 64) float16     pioggia della finestra in mm, radar × fattore dei pluviometri MeteoNetwork
                                   (fattore_radar_mnw.parquet); NaN dove il radar non c'è
  tempi    (N,) datetime64[m]      inizio della finestra valida (UTC)
  scalari  (N, 5) float32          ora_sin, ora_cos, doy_sin, doy_cos, lead (giorni): entrano come condizionamento
e una volta sola data/lazio_sri/dataset/statici.npz: territorio (quota media e dispersione, pendenza, esposizione,
TPI, frazione di terra, distanza dal mare) e maschera del dominio, (S, 48, 64).

Canali dinamici (prima la finestra, poi quella prima e quella dopo: l'errore di fase dei modelli):
  ICON-2I sulla cella   p_fin, p_max_ora, p_prima, p_dopo, rovesci, nubi, rh2m, raffiche
  GFS 0,25° bilineare   p_fin, p_max_ora, p_prima, p_dopo, rovesci, nubi, cape_max, li_min
La Previous Runs non ha CAPE di ICON, fulmini, nubi per strato né livelli di pressione: arriveranno con la
fonte B (corse intere su AWS, dal 25/07/2026), nello stesso formato con più canali.

    python scripts/rete_dataset.py [--ore 3|1] [--lead 1 2] [--uscita cartella]     # ambiente meteo, ~1–2 GB di RAM
I modelli si leggono una volta sola per lead; interpolazione GFS, mappe e radar si fanno un mese alla volta.

Con --ore 1 (data/lazio_sri/dataset_1h/): un campione è un'ora, il bersaglio il radar orario (radar_ore/, mm
nell'ora × fattore dei pluviometri); per la pioggia di ICON-2I e GFS entrano l'ora e le 3 ore prima e dopo
(p_m3 … p_p3: l'anticipo o il ritardo dei fronti), poi rovesci, nubi, rh2m, raffiche (ICON) e rovesci, nubi,
cape, lifted index (GFS): 11 + 11 canali.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import griglia_lazio as g  # noqa: E402
import griglia_lazio_test as T  # noqa: E402

USCITA = g.LOCALE.parent / "dataset"
FORMA = (48, 64)
BORDO = (1, 4)                      # righe sopra, colonne a sinistra (le 46 × 56 celle stanno al centro)
ORE = 3
CAMPI_ICON = {"p_fin": ("precipitation", "sum"), "p_max_ora": ("precipitation", "max"), "rovesci": ("showers", "sum"),
              "nubi": ("cloud_cover", "mean"), "rh2m": ("relative_humidity_2m", "mean"), "raffiche": ("wind_gusts_10m", "max")}
CAMPI_GFS = {"p_fin": ("precipitation", "sum"), "p_max_ora": ("precipitation", "max"), "rovesci": ("showers", "sum"),
             "nubi": ("cloud_cover", "mean"), "cape_max": ("cape", "max"), "li_min": ("lifted_index", "min")}
ORDINE_ICON = ["p_fin", "p_max_ora", "p_prima", "p_dopo", "rovesci", "nubi", "rh2m", "raffiche"]
ORDINE_GFS = ["p_fin", "p_max_ora", "p_prima", "p_dopo", "rovesci", "nubi", "cape_max", "li_min"]
SPOSTAMENTI = (-3, -2, -1, 1, 2, 3)
NOMI_SPOST = [f"p_{'m' if s < 0 else 'p'}{abs(s)}" for s in SPOSTAMENTI]
CAMPI_ICON_1H = {"p_fin": ("precipitation", "sum"), "rovesci": ("showers", "sum"), "nubi": ("cloud_cover", "mean"),
                 "rh2m": ("relative_humidity_2m", "mean"), "raffiche": ("wind_gusts_10m", "max")}
CAMPI_GFS_1H = {"p_fin": ("precipitation", "sum"), "rovesci": ("showers", "sum"), "nubi": ("cloud_cover", "mean"),
                "cape_max": ("cape", "max"), "li_min": ("lifted_index", "min")}
ORDINE_ICON_1H = ["p_fin", *NOMI_SPOST, "rovesci", "nubi", "rh2m", "raffiche"]
ORDINE_GFS_1H = ["p_fin", *NOMI_SPOST, "rovesci", "nubi", "cape_max", "li_min"]
STATICI = ["quota_media", "quota_std", "pendenza", "esp_nord", "esp_est", "tpi_15km", "frac_terra", "dist_mare_km"]

logger = logging.getLogger("rete_dataset")


VECCHIA = [f"v_{n}" for n in ["p_fin", *NOMI_SPOST]]       # pioggia della corsa di un giorno prima ancora


def configurazione(ore: int, vecchia: bool = False) -> dict:
    """vecchia (solo orario): per il lead k anche la pioggia della stessa ora (±1–3 h) prevista dalla corsa di k+1
    giorni prima: le due corse insieme dicono quanto la previsione è stabile. Per il lead 2 la corsa di 3 giorni
    prima non è nella Previous Runs scaricata: quei canali restano vuoti."""
    if ore == 3:
        return {"ore": 3, "campi_icon": CAMPI_ICON, "campi_gfs": CAMPI_GFS, "ordine_icon": ORDINE_ICON,
                "ordine_gfs": ORDINE_GFS, "radar": "radar_finestre", "uscita": USCITA}
    v = VECCHIA if vecchia else []
    return {"ore": 1, "campi_icon": CAMPI_ICON_1H, "campi_gfs": CAMPI_GFS_1H, "ordine_icon": ORDINE_ICON_1H + v,
            "ordine_gfs": ORDINE_GFS_1H + v, "radar": "radar_ore", "vecchia": vecchia,
            "uscita": USCITA.parent / ("dataset_1h_v" if vecchia else "dataset_1h")}


def finestre(o: pd.DataFrame, k: int, cfg: dict) -> pd.DataFrame:
    f = T._finestre(o, k, cfg["campi_" + cfg["_modello"]], cfg["ore"])
    if cfg["ore"] == 1:
        f = f.asfreq("h")                                 # ore mancanti come NaN, così lo spostamento è giusto
        for s, n in zip(SPOSTAMENTI, NOMI_SPOST):
            f[n] = f.p_fin.shift(-s)                      # valore all'ora t + s
        if cfg.get("vecchia"):
            col = f"precipitation_previous_day{k + 1}"
            if col in o:
                v = o.groupby(o.ora.dt.floor("h"))[col].sum(min_count=1).asfreq("h").reindex(f.index)
            else:
                v = pd.Series(np.nan, index=f.index)
            f["v_p_fin"] = v
            for s, n in zip(SPOSTAMENTI, NOMI_SPOST):
                f[f"v_{n}"] = v.shift(-s)
    return f[cfg["ordine_" + cfg["_modello"]]]


def distanza_costa(statici: np.ndarray, nomi: list[str]) -> np.ndarray:
    """km dalla costa con segno: positivo sulla terra (distanza dal mare), negativo sul mare (distanza dalla terra
    più vicina; celle 0,05° ≈ 5,6 × 4,1 km a 42° N); NaN fuori dal dominio."""
    from scipy import ndimage
    dominio = statici[nomi.index("dominio")] == 1
    terra = (statici[nomi.index("frac_terra")] >= 0.5) & dominio
    d_mare = ndimage.distance_transform_edt(~terra, sampling=(5.56, 4.13))
    d = np.where(terra, statici[nomi.index("dist_mare_km")], -d_mare)
    return np.where(dominio, d, np.nan)


def affidabile(statici: np.ndarray, nomi: list[str], km_mare: float = 30.0) -> np.ndarray:
    """Dove la previsione è affidabile: terra e mare entro km_mare dalla costa. Oltre, il radar del mare manca nel
    25–45% delle finestre e la rete è più debole (rete_mare.py, 10/11/2026): lì la mappa è «meno affidabile»."""
    d = distanza_costa(statici, nomi)
    return np.isfinite(d) & (d >= -km_mare)


def ruolo(tempi) -> np.ndarray:
    """Divisione a blocchi di 10 giorni (dal 01/01/2025): il quinto di ogni 5 in validazione, gli altri in
    addestramento; i giorni a meno di 2 da un blocco dell'altro ruolo sono "distacco" e non si usano (la stessa
    perturbazione non finisce da entrambe le parti). Il test vero (estate 2027 e un inverno) è a parte."""
    giorno = ((pd.DatetimeIndex(tempi).normalize() - pd.Timestamp("2025-01-01")).days).to_numpy()
    r = lambda d: np.where((d // 10) % 5 == 4, "valid", "train")
    base = r(giorno)
    vicini = np.stack([r(giorno + s) for s in (-2, -1, 1, 2)])
    return np.where((vicini == base).all(0), base, "distacco")


def celle() -> pd.DataFrame:
    c = pd.read_parquet(g.LOCALE / "descrittori_celle.parquet")
    c["r"], c["k"] = c.i + BORDO[0], c.j + BORDO[1]
    return c


def statici(c: pd.DataFrame, uscita: Path = USCITA):
    a = np.full((len(STATICI) + 1, *FORMA), np.nan, "float32")
    for n, s in enumerate(STATICI):
        a[n, c.r, c.k] = c[s]
    a[-1, c.r, c.k] = 1                                   # maschera del dominio
    uscita.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(uscita / "statici.npz", x=a, canali=np.array([*STATICI, "dominio"]))


def serie_icon(c: pd.DataFrame, k: int, tempi: pd.DatetimeIndex, cfg: dict) -> np.ndarray:
    """ICON-2I per cella, letto una volta sola: celle × finestre × campi (float16: ~0,2 GB a 3 h, ~0,8 GB a 1 h)."""
    cfg = {**cfg, "_modello": "icon"}
    serie = np.full((len(c), len(tempi), len(cfg["ordine_icon"])), np.nan, "float16")
    n = 0
    for q, x in enumerate(c.itertuples(index=False)):
        f = g.LEAD_DIR / "icon" / f"{x.i * 1000 + x.j:04d}.parquet"
        if f.exists():
            serie[q] = finestre(pd.read_parquet(f), k, cfg).reindex(tempi).to_numpy()
            n += 1
    logger.info(f"ICON-2I: {n}/{len(c)} celle (terra e mare)")
    return serie


def serie_gfs(c: pd.DataFrame, k: int, cfg: dict):
    """GFS ai punti 0,25° (punti × finestre × campi) e i pesi bilineari celle × 4 punti; l'interpolazione si fa
    un mese alla volta (gfs_mese), perché su tutto il periodo insieme occupa diversi GB."""
    punti = pd.read_parquet(g.LOCALE / "punti_nwp.parquet")
    cfg = {**cfg, "_modello": "gfs"}
    gfs = {p: finestre(pd.read_parquet(g.LEAD_DIR / "gfs" / f"{p:04d}.parquet"), k, cfg) for p in punti.punto}
    tempi = gfs[punti.punto.iloc[0]].index
    tempi = tempi[tempi >= pd.Timestamp(g.INIZIO_ICON)]    # solo il periodo di ICON-2I
    arr = np.stack([gfs[p].reindex(tempi).to_numpy() for p in punti.punto]).astype("float32")
    ids, w = T.pesi_bilineari(c, punti)
    pos = {p: n for n, p in enumerate(punti.punto)}
    return arr, tempi, np.vectorize(pos.get)(ids), w


def gfs_mese(c: pd.DataFrame, arr, ids, w, sel) -> np.ndarray:
    v = arr[:, sel][ids]                                  # celle × 4 × finestre del mese × campi
    ww = np.where(np.isnan(v), 0, w[:, :, None, None])
    den = ww.sum(1)
    interp = np.nansum(v * ww, axis=1) / np.where(den > 0, den, np.nan)   # celle × finestre × campi
    cubo = np.full((arr.shape[2], len(sel), *FORMA), np.nan, "float32")
    cubo[:, :, c.r.to_numpy(), c.k.to_numpy()] = interp.transpose(2, 1, 0)
    return cubo


def icon_mese(c: pd.DataFrame, serie: np.ndarray, sel) -> np.ndarray:
    cubo = np.full((serie.shape[2], len(sel), *FORMA), np.nan, "float32")
    cubo[:, :, c.r.to_numpy(), c.k.to_numpy()] = serie[:, sel].astype("float32").transpose(2, 1, 0)
    return cubo


def radar_mese(m: pd.Period, finestre: pd.DatetimeIndex, cartella: str = "radar_finestre") -> np.ndarray:
    """Pioggia della finestra (radar × fattore dei pluviometri MeteoNetwork); i file radar_finestre sono divisi per
    il mese di inizio della finestra."""
    y = np.full((len(finestre), *FORMA), np.nan, "float32")
    p = g.LOCALE / cartella / f"sri_{m.year}_{m.month:02d}.parquet"
    if not p.exists():
        return y
    f = pd.read_parquet(g.LOCALE / "fattore_radar_mnw.parquet")
    r = pd.read_parquet(p)
    r = r[r.finestra.isin(finestre)]
    if r.empty:
        return y
    r["stagione"] = np.where(r.finestra.dt.month.between(4, 9), "calda", "fredda")
    r = r.merge(f, on=["i", "j", "stagione"], how="left")
    pos = pd.Series(np.arange(len(finestre)), index=finestre)
    y[pos[r.finestra].to_numpy(), r.i.to_numpy() + BORDO[0], r.j.to_numpy() + BORDO[1]] = r.mm * r.fattore.fillna(1.0)
    return y


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lead", type=int, nargs="+", default=[1, 2])
    ap.add_argument("--ore", type=int, choices=[1, 3], default=3)
    ap.add_argument("--uscita", type=Path, default=None, help="cartella del dataset (per le prove)")
    ap.add_argument("--vecchia", action="store_true", help="anche la pioggia della corsa di un giorno prima (solo --ore 1)")
    ap.add_argument("--lead-dir", type=Path, default=None,
                    help="cartella dei lead al posto di griglia/lead (es. griglia/lead_test per l'insieme di test)")
    ap.add_argument("--inizio", default=None, help="prima data (default: inizio di ICON-2I, 13/04/2025)")
    ap.add_argument("--fine", default=None, help="si tengono le ore prima di questa data")
    ap.add_argument("--etichetta-lead", type=float, default=None,
                    help="valore del lead negli scalari (es. 0 per il pre-addestramento a lead corto)")
    args = ap.parse_args()
    if args.lead_dir:
        g.LEAD_DIR = args.lead_dir
    if args.inizio:
        g.INIZIO_ICON = args.inizio
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    cfg = configurazione(args.ore, args.vecchia)
    uscita = args.uscita or cfg["uscita"]
    c = celle()
    statici(c, uscita)
    canali = np.array([f"icon_{n}" for n in cfg["ordine_icon"]] + [f"gfs_{n}" for n in cfg["ordine_gfs"]])
    for k in args.lead:
        arr, finestre, ids, w = serie_gfs(c, k, cfg)
        if args.fine:
            tieni = finestre < pd.Timestamp(args.fine)
            arr, finestre = arr[:, tieni], finestre[tieni]
        icon = serie_icon(c, k, finestre, cfg)
        centro = finestre + pd.Timedelta(hours=cfg["ore"] / 2)
        ora, doy = centro.hour + centro.minute / 60, centro.dayofyear
        scalari = np.stack([np.sin(2 * np.pi * ora / 24), np.cos(2 * np.pi * ora / 24),
                            np.sin(2 * np.pi * doy / 365.25), np.cos(2 * np.pi * doy / 365.25),
                            np.full(len(finestre), k if args.etichetta_lead is None else args.etichetta_lead)],
                           axis=1).astype("float32")
        cartella = uscita / f"lead{k}"
        cartella.mkdir(parents=True, exist_ok=True)
        mesi = finestre.to_period("M")
        for m in mesi.unique():                           # un mese alla volta: memoria ~1–2 GB invece di ~6,6
            sel = np.where(mesi == m)[0]
            x = np.concatenate([icon_mese(c, icon, sel), gfs_mese(c, arr, ids, w, sel)], axis=0)   # C × t × 48 × 64
            y = radar_mese(m, finestre[sel], cfg["radar"])
            # una finestra entra solo se ha il radar e almeno un modello
            ok = np.isfinite(y).any(axis=(1, 2)) & np.isfinite(x).any(axis=(0, 2, 3))
            if not ok.any():
                continue
            f = cartella / f"{m.year}_{m.month:02d}.npz"
            np.savez_compressed(f, x=x[:, ok].transpose(1, 0, 2, 3).astype("float16"), canali=canali,
                                y=y[ok].astype("float16"), tempi=finestre[sel][ok].values.astype("datetime64[m]"),
                                scalari=scalari[sel][ok])
            logger.info(f"lead {k} {m}: {ok.sum()} finestre, {f.stat().st_size / 1e6:.1f} MB")
        del arr, icon


if __name__ == "__main__":
    main()
