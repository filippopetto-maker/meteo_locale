"""
griglia_lazio.py — Test su griglia (Fase 6, principio "griglia del territorio"): pioggia sul Lazio allargato

Il dominio è un parametro (DOMINIO): oggi il Lazio allargato di lazio_sri, domani l'Italia. Griglie:
  - modelli: 0,25° (GFS cucito, punti Open-Meteo sul reticolo 0,25°);
  - verità: radar IT-DPC-SRI sulle celle 0,05° di lazio_sri (stessa griglia delle finestre di ia-meteo);
  - descrittori del territorio per cella 0,05° dal DEM Copernicus GLO-90 (AWS, anonimo): quota media e
    dispersione, pendenza, esposizione, posizione rispetto ai dintorni (TPI), frazione di terra,
    distanza dal mare. Niente identità della cella o della stazione: il modello deve poter
    funzionare su celle mai viste.

Passi (riprendibili, ognuno scrive in data/lazio_sri/griglia/):
  gfs     punti GFS 0,25° nel dominio, una chiamata per punto e per anno con la cache di
          pioggia_fattibilita; al limite giornaliero di Open-Meteo aspetta le 00:10 UTC e riprende;
  lead    GFS, vento e ICON-2I ai lead veri (Previous Runs API, giorno 1 e 2) dal 13/04/2025;
  dem     descrittori del territorio per cella;
  radar   verità per cella e finestra di 3 h dai griglia.nc mensili di lazio_sri su HF (scaricati e
          cancellati uno alla volta): mm nella finestra (media dei ratei × 3 h) e massimo;
  test    LightGBM sul target "piove" (≥ 0,2 mm nella cella) con validazione fuori campione nel
          tempo (un anno fuori) e nello spazio (blocchi di celle fuori), confronto con climatologia.

Ambiente: conda ia_meteo per gfs/dem/radar (xarray, rasterio, huggingface_hub), conda meteo per test.

    python scripts/griglia_lazio.py gfs
    python scripts/griglia_lazio.py dem
    python scripts/griglia_lazio.py radar
    python scripts/griglia_lazio.py test
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

LOCALE = ROOT / "data" / "lazio_sri" / "griglia"
DOMINIO = dict(lat_min=40.7, lat_max=43.0, lon_min=11.3, lon_max=14.1)     # = lazio_sri.BBOX_LAZIO
PASSO_NWP = 0.25
PASSO_CELLA = 0.05
INIZIO, FINE = "2021-03-25", "2026-09-30"
VAR_GFS = ["precipitation", "showers", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high", "cape",
           "convective_inhibition", "lifted_index", "total_column_integrated_water_vapour", "boundary_layer_height"]
DEM_URL = "https://copernicus-dem-90m.s3.amazonaws.com/Copernicus_DSM_COG_30_N{lat:02d}_00_E{lon:03d}_00_DEM/Copernicus_DSM_COG_30_N{lat:02d}_00_E{lon:03d}_00_DEM.tif"
SOGLIA_MM = 0.2

logger = logging.getLogger("griglia_lazio")


def punti_nwp() -> pd.DataFrame:
    la = np.arange(np.ceil(DOMINIO["lat_min"] / PASSO_NWP) * PASSO_NWP, DOMINIO["lat_max"] + 1e-9, PASSO_NWP)
    lo = np.arange(np.ceil(DOMINIO["lon_min"] / PASSO_NWP) * PASSO_NWP, DOMINIO["lon_max"] + 1e-9, PASSO_NWP)
    g = pd.DataFrame([(round(a, 2), round(b, 2)) for a in la for b in lo], columns=["lat", "lon"])
    g["punto"] = np.arange(len(g))
    return g


def celle() -> pd.DataFrame:
    import lazio_sri as L
    lat_c, lon_c = L.centri_griglia()
    return pd.DataFrame([(i, j, a, b) for i, a in enumerate(lat_c) for j, b in enumerate(lon_c)],
                        columns=["i", "j", "lat", "lon"])


# ─────────────────────────────────────────────────────────── gfs
VAR_QUOTA = ["wind_speed_850hPa", "wind_direction_850hPa", "wind_speed_700hPa", "wind_direction_700hPa",
             "relative_humidity_850hPa", "relative_humidity_700hPa"]
PASSO_QUOTA = 0.5       # il vento a 850/700 hPa è un campo liscio: basta un reticolo più largo


def passo_gfs():
    punti = punti_nwp()
    scarica_punti(punti, VAR_GFS, "gfs_punti.parquet")
    punti.to_parquet(LOCALE / "punti_nwp.parquet", index=False)


def passo_vento():
    p = punti_nwp()
    p = p[np.isclose((p.lat / PASSO_QUOTA) % 1, 0) & np.isclose((p.lon / PASSO_QUOTA) % 1, 0)].reset_index(drop=True)
    p["punto"] = np.arange(len(p))
    scarica_punti(p, VAR_QUOTA, "vento_punti.parquet")
    p.to_parquet(LOCALE / "punti_vento.parquet", index=False)


INIZIO_ICON = "2025-04-13"   # primo dato ICON-2I nell'archivio (verificato via API il 26/10/2026)


ICON_S3 = "s3://openmeteo/data/italia_meteo_arpae_icon_2i"
ICON_GRIGLIA = dict(lat0=33.7, dlat=0.02, lon0=3.0, dlon=0.025)      # 761 × 761, verificata con HSURF
ICON_VAR = {   # variabile → aggregazioni sulla finestra di 3 h (prima la media sui pixel della cella)
    "precipitation": ("somma", "max_pixel"), "showers": ("somma",), "cloud_cover_low": ("media",),
    "cloud_cover_mid": ("media",), "cape": ("max",), "convective_inhibition": ("media",),
    "lightning_potential": ("max",), "total_column_integrated_water_vapour": ("media",),
    "relative_humidity_850hPa": ("media",), "relative_humidity_700hPa": ("media",),
    "wind_speed_850hPa": ("media",), "wind_direction_850hPa": ("circolare",),
    "vertical_velocity_850hPa": ("media",), "vertical_velocity_700hPa": ("media",),
}


def passo_icon_aws(thread: int = 8):
    """ICON-2I dall'archivio pubblico di Open-Meteo su AWS (s3://openmeteo, file .om a blocchi di 96 h,
    tutta Italia a 2,2 km): si legge solo il ritaglio del dominio, si aggregano i pixel ICON nelle celle
    0,05° (stessa assegnazione di lazio_sri) e le ore nelle finestre di 3 h. Nessuna quota.
    Uscita: griglia/icon_celle/<variabile>[_<aggr>].npy (finestre × lat × lon) + finestre.npy."""
    import fsspec
    import omfiles
    from concurrent.futures import ThreadPoolExecutor
    out = LOCALE / "icon_celle"
    out.mkdir(parents=True, exist_ok=True)
    fs = fsspec.filesystem("s3", anon=True)
    G = ICON_GRIGLIA
    i0 = int(np.floor((DOMINIO["lat_min"] - G["lat0"]) / G["dlat"])) - 1
    i1 = int(np.ceil((DOMINIO["lat_max"] - G["lat0"]) / G["dlat"])) + 2
    j0 = int(np.floor((DOMINIO["lon_min"] - G["lon0"]) / G["dlon"])) - 1
    j1 = int(np.ceil((DOMINIO["lon_max"] - G["lon0"]) / G["dlon"])) + 2
    lat_px = G["lat0"] + np.arange(i0, i1) * G["dlat"]
    lon_px = G["lon0"] + np.arange(j0, j1) * G["dlon"]
    import lazio_sri as L
    lat_c, lon_c = L.centri_griglia()
    r = np.rint((lat_c[0] - lat_px) / PASSO_CELLA).astype(int)
    c = np.rint((lon_px - lon_c[0]) / PASSO_CELLA).astype(int)
    ok_r, ok_c = (r >= 0) & (r < len(lat_c)), (c >= 0) & (c < len(lon_c))
    cella = np.full((len(lat_px), len(lon_px)), -1)
    cella[np.ix_(ok_r, ok_c)] = (r[ok_r][:, None] * len(lon_c) + c[ok_c][None, :])
    n_celle = len(lat_c) * len(lon_c)
    idx = cella.ravel()
    valido = idx >= 0
    conta = np.bincount(idx[valido], minlength=n_celle).astype("float32")

    import requests
    import tempfile
    tmp_dir = LOCALE / "tmp_icon"
    tmp_dir.mkdir(exist_ok=True)

    def chunk(var, n):
        """Il file intero via HTTPS (~9 MB, ~4 s), ritaglio letto in locale, file cancellato. La lettura
        remota con s3fs da più thread si bloccava (26/10/2026)."""
        url = f"https://openmeteo.s3.amazonaws.com/data/italia_meteo_arpae_icon_2i/{var}/chunk_{n}.om"
        for tentativo in range(5):
            try:
                r = requests.get(url, timeout=120)
                r.raise_for_status()
                break
            except requests.RequestException:
                time.sleep(10 * (tentativo + 1))
        else:
            raise RuntimeError(f"{url} non scaricabile")
        with tempfile.NamedTemporaryFile(dir=tmp_dir, suffix=".om", delete=False) as t:
            t.write(r.content)
        try:
            a = omfiles.OmFileReader.from_path(t.name)[i0:i1, j0:j1, :]   # lat × lon × 96 h
        finally:
            Path(t.name).unlink(missing_ok=True)
        return n, a.astype("float32")

    elenco = sorted(int(k.rsplit("_", 1)[1][:-3]) for k in fs.ls(f"{ICON_S3}/precipitation") if k.endswith(".om"))
    inizio = pd.Timestamp(INIZIO_ICON)
    elenco = [n for n in elenco if pd.Timestamp((n + 1) * 96, unit="h") > inizio]
    ore = pd.DatetimeIndex(np.concatenate([pd.Timestamp(n * 96, unit="h") + pd.to_timedelta(np.arange(96), "h") for n in elenco]))
    # ICON (come Open-Meteo): precipitazione all'istante T = somma dell'ora precedente → ora [T−1, T)
    ora_inizio = ore - pd.Timedelta(hours=1)
    finestre = pd.DatetimeIndex(sorted(set(ora_inizio.floor("3h"))))
    fidx = finestre.get_indexer(ora_inizio.floor("3h"))
    np.save(out / "finestre.npy", finestre.to_numpy())
    for var, aggr in ICON_VAR.items():
        if all((out / f"{var}_{a}.npy").exists() for a in aggr):
            continue
        with ThreadPoolExecutor(thread) as pool:
            blocchi = dict(pool.map(lambda n: chunk(var, n), elenco))
        cubo = np.concatenate([blocchi[n] for n in elenco], axis=2)              # lat × lon × ore
        piatto = cubo.reshape(-1, cubo.shape[2])                                  # pixel × ore
        if aggr[0] == "circolare":
            rad = np.radians(piatto)
            serie = {"s": np.sin(rad), "c": np.cos(rad)}
        else:
            serie = {"v": piatto}
        medie = {}
        for k, v in serie.items():
            somma = np.zeros((n_celle, v.shape[1]), "float32")
            np.add.at(somma, idx[valido], np.nan_to_num(v[valido]))
            medie[k] = somma / np.maximum(conta, 1)[:, None]                       # celle × ore
        for a in aggr:
            if a == "max_pixel":
                mx = np.full((n_celle, piatto.shape[1]), -np.inf, "float32")
                np.maximum.at(mx, idx[valido], np.nan_to_num(piatto[valido], nan=-np.inf))
                h = mx
            elif a == "circolare":
                h = None
            else:
                h = medie["v"]
            ris = np.full((len(finestre), n_celle), np.nan, "float32")
            if a == "circolare":
                s = pd.DataFrame(medie["s"].T).groupby(fidx).mean().to_numpy()
                cc = pd.DataFrame(medie["c"].T).groupby(fidx).mean().to_numpy()
                ris = (np.degrees(np.arctan2(s, cc)) % 360).astype("float32")
            else:
                df = pd.DataFrame(h.T).groupby(fidx)
                ris = {"somma": df.sum(min_count=3), "media": df.mean(), "max": df.max(),
                       "max_pixel": df.max()}[a].to_numpy().astype("float32")
            ris = ris.reshape(len(finestre), len(lat_c), len(lon_c))
            np.save(out / f"{var}_{a}.npy", ris)
        logger.info(f"ICON-2I {var}: {len(elenco)} blocchi, {len(finestre)} finestre, aggregazioni {aggr}")
        del cubo, piatto, blocchi


# ─────────────────────────────────────────────────────────── lead veri (Previous Runs API)
PREVIOUS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
VAR_LEAD_ICON = ["precipitation", "showers", "cloud_cover", "relative_humidity_2m", "wind_gusts_10m"]
# Nella Previous Runs (verificato il 27/10/2026) mancano CAPE, fulmini, nubi per strato, CIN, acqua precipitabile
# e tutti i livelli di pressione (anche per il GFS): niente vento a 850/700 hPa ai lead veri.
VAR_LEAD_GFS = ["precipitation", "showers", "cloud_cover", "cape", "lifted_index"]   # nubi medie/alte, CIN, TCWV: vuote nella Previous Runs
LEAD_DIR = LOCALE / "lead"


def con_giorni(variabili: list[str]) -> list[str]:
    """La stessa ora prevista dalla corsa di uno e due giorni prima (≈ lead 24–35 h e 48–59 h per ICON-2I)."""
    return [f"{v}_previous_day{k}" for k in (1, 2) for v in variabili]


def scarica_lead(punti: pd.DataFrame, variabili: list[str], modello: str, nome: str, inizio: str = INIZIO_ICON,
                 fine: str = FINE, base: Path = LEAD_DIR):
    """Un parquet per punto in griglia/lead/<nome>/ (riprendibile: i punti già fatti si saltano). La cache
    JSON di pioggia_fattibilita si cancella dopo ogni punto: per le 1546 celle sarebbe ~1 GB."""
    import hashlib
    import json
    import gfs_stazioni as gs
    import pioggia_fattibilita as pf
    import requests
    out = base / nome
    out.mkdir(parents=True, exist_ok=True)
    da_fare = [p for p in punti.itertuples(index=False) if not (out / f"{p.punto:04d}.parquet").exists()]
    logger.info(f"{nome}: {len(punti)} punti, {len(da_fare)} da scaricare ({modello}, {len(variabili)} variabili)")
    for n, p in enumerate(da_fare, 1):
        parti = []
        for a, b in gs.tratti(inizio, fine):
            par = {"latitude": p.lat, "longitude": p.lon, "hourly": ",".join(variabili), "models": modello,
                   "start_date": a, "end_date": b, "timezone": "UTC"}
            while True:
                try:
                    d = pf._get(PREVIOUS_URL, par)
                    break
                except RuntimeError as e:
                    adesso = datetime.now(timezone.utc)
                    if "giornaliero" in str(e):
                        ripresa = adesso.replace(hour=0, minute=10, second=0) + pd.Timedelta(days=1)
                    elif "orario" in str(e):
                        ripresa = adesso.replace(minute=2, second=0) + pd.Timedelta(hours=1)
                    else:
                        raise
                    logger.warning(f"{e}: riprendo alle {ripresa:%H:%M} UTC del {ripresa:%d/%m}")
                    time.sleep((ripresa - adesso).total_seconds())
                except requests.exceptions.RequestException as e:      # timeout o rete giù: il 30/10 ha fermato tutto
                    logger.warning(f"{type(e).__name__}: riprovo fra 5 minuti")
                    time.sleep(300)
            f = pd.DataFrame(d["hourly"])
            f["ora"] = pd.to_datetime(f.pop("time")) - pd.Timedelta(hours=1)     # somma dell'ora precedente
            parti.append(f)
            (pf.CACHE_OM / (hashlib.sha1(json.dumps([PREVIOUS_URL, par], sort_keys=True).encode()).hexdigest() + ".json")).unlink(missing_ok=True)
        f = pd.concat(parti, ignore_index=True)
        f[variabili] = f[variabili].astype("float32")
        f.to_parquet(out / f"{p.punto:04d}.parquet", index=False)
        if n % 25 == 0 or n == len(da_fare):
            logger.info(f"{nome}: {n}/{len(da_fare)} punti")


def passo_lead():
    """GFS 0,25° ai lead veri (i punti del passo gfs), poi ICON-2I su ogni cella 0,05° di
    terra. Le celle ICON in ordine a reticolo: prima quelle con i e j pari (0,1°), così a metà download c'è
    già un test a 0,1°. Con il limite gratuito (10.000 chiamate pesate al giorno) ~6 giorni in tutto."""
    scarica_lead(pd.read_parquet(LOCALE / "punti_nwp.parquet"), con_giorni(VAR_LEAD_GFS), "gfs_seamless", "gfs")
    c = pd.read_parquet(LOCALE / "descrittori_celle.parquet")
    c = c[c.frac_terra >= 0.5].copy()
    c["punto"] = c.i * 1000 + c.j                       # nome del file: i e j della cella
    c["ordine"] = ((c.i % 2) + (c.j % 2) > 0).astype(int)
    c = c.sort_values(["ordine", "i", "j"])
    scarica_lead(c[["punto", "lat", "lon"]], con_giorni(VAR_LEAD_ICON), "italia_meteo_arpae_icon_2i", "icon")


def passo_lead_mare():
    """ICON-2I ai lead veri anche sulle 1030 celle di mare del dominio (07/11/2026): la pioggia e le nubi in arrivo
    dal Tirreno servono alla rete per vedere la convezione prima che tocchi la costa. Stessa cartella e stessi nomi
    del passo lead, stesso ordine a reticolo (prima 0,1°). ~4 giorni col limite gratuito."""
    c = pd.read_parquet(LOCALE / "descrittori_celle.parquet")
    c = c[c.frac_terra < 0.5].copy()
    c["punto"] = c.i * 1000 + c.j
    c["ordine"] = ((c.i % 2) + (c.j % 2) > 0).astype(int)
    c = c.sort_values(["ordine", "i", "j"])
    scarica_lead(c[["punto", "lat", "lon"]], con_giorni(VAR_LEAD_ICON), "italia_meteo_arpae_icon_2i", "icon")


TEST_DIR = LOCALE / "lead_test"


def passo_lead_test(inizio: str = "2026-10-01", fine: str = "2026-11-01", base: Path | None = None):
    """Come lead + lead_mare ma per un periodo nuovo, mai visto dalla rete (insieme di test, 10/11/2026), in
    griglia/lead_test/: GFS ai punti 0,25°, ICON-2I su tutte le celle (terra e mare). fine = un giorno dopo l'ultimo
    del mese di test, per le ore spostate di ±3 h."""
    scarica_lead(pd.read_parquet(LOCALE / "punti_nwp.parquet"), con_giorni(VAR_LEAD_GFS), "gfs_seamless", "gfs",
                 inizio, fine, base or TEST_DIR)
    c = pd.read_parquet(LOCALE / "descrittori_celle.parquet")
    c["punto"] = c.i * 1000 + c.j
    scarica_lead(c[["punto", "lat", "lon"]], con_giorni(VAR_LEAD_ICON), "italia_meteo_arpae_icon_2i", "icon",
                 inizio, fine, base or TEST_DIR)


def scarica_punti(punti: pd.DataFrame, variabili: list[str], nome_file: str, modello: str = "gfs_seamless",
                  inizio: str = INIZIO):
    import gfs_stazioni as gs
    import pioggia_fattibilita as pf
    LOCALE.mkdir(parents=True, exist_ok=True)
    logger.info(f"{len(punti)} punti GFS nel dominio → {nome_file}")
    parti = []
    for _, p in punti.iterrows():
        for a, b in gs.tratti(inizio, FINE):
            while True:
                try:
                    d = pf._get(pf.HIST_URL, {"latitude": p.lat, "longitude": p.lon, "hourly": ",".join(variabili),
                                              "models": modello, "start_date": a, "end_date": b,
                                              "timezone": "UTC"})
                    break
                except RuntimeError as e:
                    adesso = datetime.now(timezone.utc)
                    if "giornaliero" in str(e):
                        ripresa = adesso.replace(hour=0, minute=10, second=0) + pd.Timedelta(days=1)
                    elif "orario" in str(e):        # ~5.000 chiamate pesate/ora: ~34 punti di 5,5 anni
                        ripresa = adesso.replace(minute=2, second=0) + pd.Timedelta(hours=1)
                    else:
                        raise
                    logger.warning(f"{e}: riprendo alle {ripresa:%H:%M} UTC del {ripresa:%d/%m}")
                    time.sleep((ripresa - adesso).total_seconds())
            f = pd.DataFrame(d["hourly"])
            f["ora"] = pd.to_datetime(f.pop("time")) - pd.Timedelta(hours=1)
            f["punto"] = p.punto
            parti.append(f)
        logger.info(f"punto {p.punto + 1}/{len(punti)} ({p.lat}, {p.lon}) fatto")
    df = pd.concat(parti, ignore_index=True)
    df.to_parquet(LOCALE / nome_file, index=False)
    logger.info(f"{nome_file}: {len(df):,} righe")


# ─────────────────────────────────────────────────────────── dem
def passo_dem():
    import rasterio
    from rasterio.merge import merge
    from scipy import ndimage
    LOCALE.mkdir(parents=True, exist_ok=True)
    lat0, lat1 = int(np.floor(DOMINIO["lat_min"])), int(np.floor(DOMINIO["lat_max"]))
    lon0, lon1 = int(np.floor(DOMINIO["lon_min"])), int(np.floor(DOMINIO["lon_max"]))
    sorgenti = []
    for la in range(lat0, lat1 + 1):
        for lo in range(lon0, lon1 + 1):
            url = DEM_URL.format(lat=la, lon=lo)
            try:
                sorgenti.append(rasterio.open(url))       # i riquadri di solo mare non esistono
            except rasterio.errors.RasterioIOError:
                logger.info(f"nessun riquadro DEM N{la} E{lo} (mare)")
    dem, tr = merge(sorgenti, bounds=(DOMINIO["lon_min"] - 0.3, DOMINIO["lat_min"] - 0.3,
                                      DOMINIO["lon_max"] + 0.3, DOMINIO["lat_max"] + 0.3), nodata=np.nan)
    dem = dem[0].astype("float32")
    # mare = grandi zone contigue a quota ≤ 0 o senza dato: esclude pixel isolati a 0,5 m nell'entroterra
    # (es. 41,80 N 12,41 E) e le bonifiche sotto il livello del mare (Pianura Pontina)
    basso = ~(np.isfinite(dem) & (dem > 0))
    etichette, n = ndimage.label(basso)
    area_px = (abs(tr.e) * 111.32) * (tr.a * 111.32 * np.cos(np.radians(42.0)))
    dimensioni = ndimage.sum(basso, etichette, index=np.arange(1, n + 1))
    mare_id = np.arange(1, n + 1)[dimensioni * area_px >= 500]
    terra = ~np.isin(etichette, mare_id)
    z = np.where(np.isfinite(dem), dem, 0.0)
    ny, nx = z.shape
    res_y, res_x = abs(tr.e), tr.a                          # gradi
    lat_px = tr.f + (np.arange(ny) + 0.5) * tr.e
    lon_px = tr.c + (np.arange(nx) + 0.5) * tr.a
    dy = res_y * 111_320.0
    dx = res_x * 111_320.0 * np.cos(np.radians(lat_px))[:, None]
    gy, gx = np.gradient(z)
    pend = np.degrees(np.arctan(np.hypot(gx / dx, gy / dy)))
    esp_n = -gy / dy / np.maximum(np.hypot(gx / dx, gy / dy), 1e-6)   # componente nord dell'esposizione
    esp_e = -gx / dx / np.maximum(np.hypot(gx / dx, gy / dy), 1e-6)
    tpi5 = z - ndimage.uniform_filter(z, size=int(round(0.05 / res_y)) * 3)   # rispetto a ~15 km
    lat_media = np.radians((DOMINIO["lat_min"] + DOMINIO["lat_max"]) / 2)
    dist_mare = ndimage.distance_transform_edt(                              # km, pixel non quadrati
        terra, sampling=(res_y * 111.32, res_x * 111.32 * np.cos(lat_media)))
    # gradiente del terreno (m/km) verso est e verso nord, fine e smussato (~10 km: il sollevamento
    # orografico lo fanno le dorsali), più il DEM smussato a ~1 km per leggere la quota a monte del vento
    dz_est = np.gradient(z, axis=1) / (dx / 1000)
    dz_nord = -np.gradient(z, axis=0) / (dy / 1000)                  # le righe vanno da nord a sud
    sigma_px = 5.0 / (res_y * 111.32)                                 # σ = 5 km
    zs = ndimage.gaussian_filter(z, sigma_px)
    dzs_est = np.gradient(zs, axis=1) / (dx / 1000)
    dzs_nord = -np.gradient(zs, axis=0) / (dy / 1000)
    passo = int(round(0.01 / res_y))
    np.savez_compressed(LOCALE / "dem_smussato.npz", quota=zs[::passo, ::passo].astype("float32"),
                        lat=lat_px[::passo], lon=lon_px[::passo])
    c = celle()
    righe = []
    for _, r in c.iterrows():
        iy = (lat_px <= r.lat + PASSO_CELLA / 2) & (lat_px > r.lat - PASSO_CELLA / 2)
        ix = (lon_px >= r.lon - PASSO_CELLA / 2) & (lon_px < r.lon + PASSO_CELLA / 2)
        sel = np.ix_(iy, ix)
        t = terra[sel]
        righe.append({"i": r.i, "j": r.j, "frac_terra": t.mean(),
                      "quota_media": float(z[sel][t].mean()) if t.any() else 0.0,
                      "quota_std": float(z[sel][t].std()) if t.any() else 0.0,
                      "quota_max": float(z[sel].max()),
                      "pendenza": float(pend[sel][t].mean()) if t.any() else 0.0,
                      "esp_nord": float(esp_n[sel][t].mean()) if t.any() else 0.0,
                      "esp_est": float(esp_e[sel][t].mean()) if t.any() else 0.0,
                      "tpi_15km": float(tpi5[sel].mean()),
                      "grad_est": float(dz_est[sel].mean()), "grad_nord": float(dz_nord[sel].mean()),
                      "grad10_est": float(dzs_est[sel].mean()), "grad10_nord": float(dzs_nord[sel].mean()),
                      "dist_mare_km": float(dist_mare[sel].mean())})
    d = c.merge(pd.DataFrame(righe), on=["i", "j"])
    d.to_parquet(LOCALE / "descrittori_celle.parquet", index=False)
    logger.info(f"descrittori_celle.parquet: {len(d)} celle, terra {(d.frac_terra > 0.5).sum()}, "
                f"quota max {d.quota_media.max():.0f} m")


# ─────────────────────────────────────────────────────────── radar
def passo_radar(ore: int = 3, dal: str = "sri_2021_03"):
    """Radar in finestre di `ore` ore per cella (mm nella finestra). ore=1 → radar_ore/ (passo radar_ore, dal 04/2025)."""
    import xarray as xr
    from huggingface_hub import hf_hub_download
    sys.path.insert(0, str(ROOT / "ia_meteo"))
    import archivio as ar
    LOCALE.mkdir(parents=True, exist_ok=True)
    dest_dir = LOCALE / ("radar_finestre" if ore == 3 else "radar_ore")
    dest_dir.mkdir(exist_ok=True)
    reg = ar.registro()["lazio_sri"]
    rid = ar.repo_id()
    for chiave, voce in sorted(reg.items()):
        out = dest_dir / f"{chiave}.parquet"
        if out.exists() or chiave < dal:
            continue
        p = next(f for f in voce["file"] if f.endswith("griglia.nc"))
        loc = Path(hf_hub_download(rid, p, repo_type="dataset", local_dir=ROOT / "data" / "lazio_sri" / "hf"))
        with xr.open_dataset(loc) as ds:
            ds = ds.load()
        loc.unlink()
        fin = ds.time.dt.floor(f"{ore}h")
        media = ds.pioggia_media.groupby(fin).mean("time")             # mm/h medi nella finestra
        conta = ds.pioggia_media.notnull().groupby(fin).sum("time")
        massimo = ds.pioggia_max.groupby(fin).max("time")
        f = xr.Dataset({"mm": media * ore, "max_mmh": massimo, "n": conta}).rename({"floor": "finestra"})
        df = f.to_dataframe().reset_index()
        passi_attesi = df.finestra.map(lambda t: 12 * ore)              # passi da 5 min
        df = df[df.n >= 0.75 * passi_attesi].drop(columns="n")
        lat_c, lon_c = ds.lat.values, ds.lon.values
        df["i"] = pd.Index(lat_c).get_indexer(df.lat)
        df["j"] = pd.Index(lon_c).get_indexer(df.lon)
        df[["i", "j"]] = df[["i", "j"]].astype("int16")
        df[["mm", "max_mmh"]] = df[["mm", "max_mmh"]].astype("float32")
        df[["finestra", "i", "j", "mm", "max_mmh"]].to_parquet(out, index=False)
        logger.info(f"{chiave}: {len(df):,} righe cella × finestra")


# ─────────────────────────────────────────────────────────── test
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("passo", choices=["gfs", "vento", "icon_aws", "lead", "lead_mare", "lead_test", "dem", "radar", "radar_ore", "test"])
    ap.add_argument("--inizio", default="2026-10-01", help="lead_test: primo giorno")
    ap.add_argument("--fine", default="2026-11-01", help="lead_test: giorno dopo l'ultimo")
    ap.add_argument("--cartella", type=Path, default=None, help="lead_test: cartella al posto di griglia/lead_test")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for nome in ("httpx", "huggingface_hub", "rasterio"):
        logging.getLogger(nome).setLevel(logging.WARNING)
    if args.passo == "gfs":
        passo_gfs()
    elif args.passo == "vento":
        passo_vento()
    elif args.passo == "icon_aws":
        passo_icon_aws()
    elif args.passo == "lead":
        passo_lead()
    elif args.passo == "lead_mare":
        passo_lead_mare()
    elif args.passo == "lead_test":
        passo_lead_test(args.inizio, args.fine, args.cartella)
    elif args.passo == "dem":
        passo_dem()
    elif args.passo == "radar":
        passo_radar()
    elif args.passo == "radar_ore":
        passo_radar(ore=1, dal="sri_2025_04")
    else:
        import griglia_lazio_test
        griglia_lazio_test.main()


if __name__ == "__main__":
    main()
