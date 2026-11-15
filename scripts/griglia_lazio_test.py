"""
griglia_lazio_test.py — Modello di test su griglia (passo `test` di griglia_lazio.py)

Una riga = cella 0,05° di terra × finestra di 3 h. Target: piove (≥ 0,2 mm di media nella cella,
radar). Feature:
  - GFS 0,25° interpolato bilineare sulla cella: pioggia (somma, massimo orario, finestre prima e
    dopo, ±6 h), rovesci, nubi basse/medie/alte, CAPE max, CIN, lifted index min, acqua
    precipitabile, strato limite;
  - vicinato: media e massimo della pioggia GFS di finestra sui punti 0,25° entro 30 e 60 km;
  - territorio: quota media e dispersione, pendenza, esposizione, TPI, frazione di terra, distanza
    dal mare (griglia_lazio.py dem);
  - ora della finestra e giorno dell'anno.
Nessuna identità della cella (il test del 26/10 con lat/lon ha dato +0,006: non serve).
  - vento e umidità a 850/700 hPa (GFS 0,5°) e interazioni vento–rilievo (aggiungi_vento).

Validazione, sempre con un anno fuori (addestra sugli altri anni):
  - tempo: tutte le celle in addestramento e in valutazione;
  - tempo + spazio: celle divise in blocchi di 0,5° a scacchiera; si addestra su metà dei blocchi e
    si valuta sull'altra metà. È la prova per il dominio Italia: celle mai viste.
Varianti: "senza territorio" (solo GFS + tempo), "con territorio", "territorio+vento",
"territorio+vento+rilievo" (con gradienti e interazioni vento–rilievo).
Riferimento: climatologia mese × ora della finestra delle celle di addestramento (per il caso spazio
non si può usare quella della cella, che il modello non ha visto) e, nel caso tempo, anche quella
della cella stessa.

Campione: una frazione casuale delle righe (CAMPIONE) per stare nella RAM del Mac.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import griglia_lazio as g  # noqa: E402

OUT = ROOT / "logs" / "pioggia"
CAMPIONE = 0.15
SEME = 42
PARAMETRI = {"objective": "binary", "learning_rate": 0.05, "num_leaves": 63, "min_data_in_leaf": 500,
             "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0,
             "verbose": -1, "seed": SEME, "num_threads": 6}
DESCRITTORI = ["quota_media", "quota_std", "pendenza", "esp_nord", "esp_est", "tpi_15km", "frac_terra", "dist_mare_km"]

logger = logging.getLogger("griglia_lazio_test")


def km(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371 * np.arcsin(np.sqrt(a))


def finestre_punti() -> tuple[np.ndarray, pd.DatetimeIndex, list[str]]:
    """Feature di finestra per ogni punto GFS: array (punti, finestre, feature)."""
    o = pd.read_parquet(g.LOCALE / "gfs_punti.parquet")
    o["finestra"] = o.ora.dt.floor("3h")
    gr = o.groupby(["punto", "finestra"])
    f = pd.DataFrame({"p_fin": gr.precipitation.sum(min_count=3), "p_max_ora": gr.precipitation.max(),
                      "rovesci": gr.showers.sum(min_count=1),
                      "nubi_basse": gr.cloud_cover_low.mean(), "nubi_medie": gr.cloud_cover_mid.mean(),
                      "nubi_alte": gr.cloud_cover_high.mean(), "cape_max": gr.cape.max(),
                      "cin": gr.convective_inhibition.mean(), "li_min": gr.lifted_index.min(),
                      "tcwv": gr.total_column_integrated_water_vapour.mean(), "blh_max": gr.boundary_layer_height.max()})
    f = f.unstack("punto")
    finestre = f.index
    nomi = list(dict.fromkeys(f.columns.get_level_values(0)))
    arr = np.stack([f[n].to_numpy().T for n in nomi], axis=-1).astype("float32")      # punti × finestre × feat
    i = nomi.index("p_fin")
    prima = np.concatenate([np.full_like(arr[:, :1, i], np.nan), arr[:, :-1, i]], axis=1)
    dopo = np.concatenate([arr[:, 1:, i], np.full_like(arr[:, :1, i], np.nan)], axis=1)
    sei = np.nansum(np.stack([prima, arr[:, :, i], dopo]), axis=0)
    arr = np.concatenate([arr, prima[..., None], dopo[..., None], sei[..., None]], axis=-1)
    return arr, pd.DatetimeIndex(finestre), [*nomi, "p_prima", "p_dopo", "p_6h"]


def pesi_bilineari(celle: pd.DataFrame, punti: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    lat_u = np.sort(punti.lat.unique())
    lon_u = np.sort(punti.lon.unique())
    idx = {(round(a, 2), round(b, 2)): p for a, b, p in punti[["lat", "lon", "punto"]].itertuples(index=False)}
    ids = np.zeros((len(celle), 4), int)
    w = np.zeros((len(celle), 4), "float32")
    for n, (la, lo) in enumerate(celle[["lat", "lon"]].itertuples(index=False)):
        i = np.clip(np.searchsorted(lat_u, la) - 1, 0, len(lat_u) - 2)
        j = np.clip(np.searchsorted(lon_u, lo) - 1, 0, len(lon_u) - 2)
        a0, a1, b0, b1 = lat_u[i], lat_u[i + 1], lon_u[j], lon_u[j + 1]
        ta = np.clip((la - a0) / (a1 - a0), 0, 1)
        tb = np.clip((lo - b0) / (b1 - b0), 0, 1)
        for k, (aa, bb, ww) in enumerate(((a0, b0, (1 - ta) * (1 - tb)), (a0, b1, (1 - ta) * tb),
                                          (a1, b0, ta * (1 - tb)), (a1, b1, ta * tb))):
            ids[n, k] = idx[(round(aa, 2), round(bb, 2))]
            w[n, k] = ww
    return ids, w


def costruisci(dal: str | None = None, campione: float = CAMPIONE, icon: bool = False):
    celle = pd.read_parquet(g.LOCALE / "descrittori_celle.parquet")
    celle = celle[celle.frac_terra >= 0.5].reset_index(drop=True)
    celle["cella"] = np.arange(len(celle))
    punti = pd.read_parquet(g.LOCALE / "punti_nwp.parquet")
    arr, finestre, nomi = finestre_punti()
    logger.info(f"GFS: {arr.shape[0]} punti × {arr.shape[1]:,} finestre × {arr.shape[2]} feature")

    radar = pd.concat([pd.read_parquet(f) for f in sorted((g.LOCALE / "radar_finestre").glob("*.parquet"))],
                      ignore_index=True)
    radar = radar.merge(celle[["i", "j", "cella"]], on=["i", "j"])
    if dal:
        radar = radar[radar.finestra >= dal]
    radar = radar.sample(frac=campione, random_state=SEME).reset_index(drop=True)
    radar["w"] = finestre.get_indexer(radar.finestra)
    radar = radar[radar.w >= 0].reset_index(drop=True)
    logger.info(f"campione: {len(radar):,} righe cella × finestra ({campione:.0%})")

    ids, w = pesi_bilineari(celle, punti)
    c, t = radar.cella.to_numpy(), radar.w.to_numpy()
    for k, nome in enumerate(nomi):
        v = arr[ids[c], t[:, None], k]                                   # righe × 4
        ww = np.where(np.isnan(v), 0, w[c])
        radar[f"gfs_{nome}"] = np.where(ww.sum(1) > 0, np.nansum(v * ww, 1) / np.maximum(ww.sum(1), 1e-9), np.nan)
    ip = nomi.index("p_fin")
    for r in (30, 60):
        d = km(celle.lat.to_numpy()[:, None], celle.lon.to_numpy()[:, None], punti.lat.to_numpy()[None], punti.lon.to_numpy()[None])
        vic = [np.where(d[n] <= r)[0] for n in range(len(celle))]
        media = np.full(len(radar), np.nan, "float32")
        massimo = np.full(len(radar), np.nan, "float32")
        for n in range(len(celle)):
            sel = np.where(c == n)[0]
            if len(sel) and len(vic[n]):
                blocco = arr[vic[n]][:, t[sel], ip]
                with np.errstate(all="ignore"):
                    media[sel], massimo[sel] = np.nanmean(blocco, 0), np.nanmax(blocco, 0)
        radar[f"vic{r}_media"], radar[f"vic{r}_max"] = media, massimo
    radar = radar.merge(celle[["cella", "lat", "lon", *DESCRITTORI, *GRADIENTI]], on="cella")
    col_vento = aggiungi_vento(radar, celle, finestre)
    if icon:
        aggiungi_icon(radar, celle, finestre)
    fh = radar.finestra.dt.hour + 1.5
    doy = radar.finestra.dt.dayofyear
    radar["ora_sin"], radar["ora_cos"] = np.sin(2 * np.pi * fh / 24), np.cos(2 * np.pi * fh / 24)
    radar["doy_sin"], radar["doy_cos"] = np.sin(2 * np.pi * doy / 365.25), np.cos(2 * np.pi * doy / 365.25)
    radar["piove"] = (radar.mm >= g.SOGLIA_MM).astype(int)
    radar["anno"], radar["mese"], radar["ora_f"] = radar.finestra.dt.year, radar.finestra.dt.month, radar.finestra.dt.hour
    blocco = (np.floor((radar.lat - g.DOMINIO["lat_min"]) / 0.5) + np.floor((radar.lon - g.DOMINIO["lon_min"]) / 0.5)) % 2
    radar["blocco"] = blocco.astype(int)
    col_gfs = [f"gfs_{n}" for n in nomi] + ["vic30_media", "vic30_max", "vic60_media", "vic60_max"]
    return radar, col_gfs, col_vento


GRADIENTI = ["grad_est", "grad_nord", "grad10_est", "grad10_nord"]
VENTO = ["u850", "v850", "ws850", "u700", "v700", "rh850", "rh700"]
INTERAZIONI = ["salita850", "salita850_fine", "salita700", "flusso_umido", "dz_monte15", "barriera_monte"]


def aggiungi_vento(radar: pd.DataFrame, celle: pd.DataFrame, finestre: pd.DatetimeIndex) -> list[str]:
    """Vento e umidità a 850/700 hPa (GFS 0,5°) interpolati sulla cella, e interazioni vento–rilievo:
      salita850/700  componente del vento verso la salita (vento · gradiente del terreno smussato a 10 km):
                     > 0 il flusso risale il rilievo, < 0 lo discende;
      flusso_umido   salita850 × umidità relativa a 850 hPa;
      dz_monte15     quota della cella − quota 15 km a monte del vento a 850 hPa (> 0 versante sopravvento);
      barriera_monte quota massima a 5–30 km a monte − quota della cella (> 0 cella sottovento a un rilievo)."""
    from scipy.interpolate import RegularGridInterpolator
    o = pd.read_parquet(g.LOCALE / "vento_punti.parquet")
    punti = pd.read_parquet(g.LOCALE / "punti_vento.parquet")
    for liv in (850, 700):
        rad = np.radians(o[f"wind_direction_{liv}hPa"])
        ws = o[f"wind_speed_{liv}hPa"] / 3.6                                     # km/h → m/s
        o[f"u{liv}"], o[f"v{liv}"] = -ws * np.sin(rad), -ws * np.cos(rad)       # direzione di provenienza
    o["ws850"] = o.wind_speed_850hPa / 3.6
    o["rh850"], o["rh700"] = o.relative_humidity_850hPa, o.relative_humidity_700hPa
    o["finestra"] = o.ora.dt.floor("3h")
    f = o.groupby(["punto", "finestra"])[VENTO].mean().unstack("punto").reindex(finestre)
    arr = np.stack([f[n].to_numpy().T for n in VENTO], axis=-1).astype("float32")   # punti × finestre × var
    ids, w = pesi_bilineari(celle, punti)
    c, t = radar.cella.to_numpy(), radar.w.to_numpy()
    for k, nome in enumerate(VENTO):
        v = arr[ids[c], t[:, None], k]
        ww = np.where(np.isnan(v), 0, w[c])
        radar[nome] = np.where(ww.sum(1) > 0, np.nansum(v * ww, 1) / np.maximum(ww.sum(1), 1e-9), np.nan)
    radar["salita850"] = radar.u850 * radar.grad10_est + radar.v850 * radar.grad10_nord
    radar["salita850_fine"] = radar.u850 * radar.grad_est + radar.v850 * radar.grad_nord
    radar["salita700"] = radar.u700 * radar.grad10_est + radar.v700 * radar.grad10_nord
    radar["flusso_umido"] = radar.salita850 * radar.rh850 / 100
    dem = np.load(g.LOCALE / "dem_smussato.npz")
    interp = RegularGridInterpolator((dem["lat"][::-1], dem["lon"]), dem["quota"][::-1], bounds_error=False, fill_value=0.0)
    ws = np.hypot(radar.u850, radar.v850).to_numpy()
    su, sv = -radar.u850.to_numpy() / np.maximum(ws, 0.1), -radar.v850.to_numpy() / np.maximum(ws, 0.1)   # verso monte
    la, lo = radar.lat.to_numpy(), radar.lon.to_numpy()

    def quota_a(km_monte):
        return interp(np.column_stack([la + sv * km_monte / 111.32,
                                       lo + su * km_monte / (111.32 * np.cos(np.radians(la)))]))
    z0 = quota_a(0.0)
    radar["dz_monte15"] = z0 - quota_a(15.0)
    radar["barriera_monte"] = np.max(np.stack([quota_a(k) for k in (5, 10, 20, 30)]), axis=0) - z0
    return [*VENTO, *INTERAZIONI]


ICON = ["icon_p_fin", "icon_p_max_pixel", "icon_p_prima", "icon_p_dopo", "icon_p_6h", "icon_rovesci",
        "icon_nubi_basse", "icon_nubi_medie", "icon_cape_max", "icon_cin", "icon_fulmini", "icon_tcwv",
        "icon_rh850", "icon_rh700", "icon_u850", "icon_v850", "icon_w850", "icon_w700", "icon_salita850",
        "icon_vic15_media", "icon_vic15_max", "icon_vic30_media", "icon_vic30_max"]


def aggiungi_icon(radar: pd.DataFrame, celle: pd.DataFrame, finestre: pd.DatetimeIndex) -> list[str]:
    """ICON-2I (2,2 km) già aggregato per cella 0,05° e finestra di 3 h da griglia_lazio.py icon_aws:
    pioggia della cella (media e pixel massimo), finestre accanto, rovesci, nubi, CAPE, CIN, potenziale di
    fulminazione, acqua precipitabile, umidità e vento a 850/700 hPa, velocità verticale (il sollevamento
    che ICON calcola sulla sua orografia), salita del vento sul rilievo smussato, vicinato a 15 e 30 km."""
    from scipy import ndimage
    d = g.LOCALE / "icon_celle"
    fi = pd.DatetimeIndex(np.load(d / "finestre.npy"))
    carica = lambda n: np.load(d / f"{n}.npy")
    campi = {"p_fin": carica("precipitation_somma"), "p_max_pixel": carica("precipitation_max_pixel"),
             "rovesci": carica("showers_somma"), "nubi_basse": carica("cloud_cover_low_media"),
             "nubi_medie": carica("cloud_cover_mid_media"), "cape_max": carica("cape_max"),
             "cin": carica("convective_inhibition_media"), "fulmini": carica("lightning_potential_max"),
             "tcwv": carica("total_column_integrated_water_vapour_media"), "rh850": carica("relative_humidity_850hPa_media"),
             "rh700": carica("relative_humidity_700hPa_media"), "w850": carica("vertical_velocity_850hPa_media"),
             "w700": carica("vertical_velocity_700hPa_media")}
    ws = carica("wind_speed_850hPa_media") / 3.6
    dr = np.radians(carica("wind_direction_850hPa_circolare"))
    campi["u850"], campi["v850"] = -ws * np.sin(dr), -ws * np.cos(dr)
    p = campi["p_fin"]
    campi["p_prima"] = np.concatenate([np.full_like(p[:1], np.nan), p[:-1]])
    campi["p_dopo"] = np.concatenate([p[1:], np.full_like(p[:1], np.nan)])
    campi["p_6h"] = np.nansum(np.stack([campi["p_prima"], p, campi["p_dopo"]]), 0)
    for r_km, nome in ((15, "vic15"), (30, "vic30")):
        raggio = int(round(r_km / 5))                              # celle da ~5 km
        k = 2 * raggio + 1
        campi[f"{nome}_media"] = ndimage.uniform_filter(np.nan_to_num(p), size=(1, k, k), mode="nearest")
        campi[f"{nome}_max"] = ndimage.maximum_filter(np.nan_to_num(p), size=(1, k, k), mode="nearest")
    t = fi.get_indexer(radar.finestra)
    i, j = radar.i.to_numpy().astype(int), radar.j.to_numpy().astype(int)
    ok = t >= 0
    for nome, arr in campi.items():
        v = np.full(len(radar), np.nan, "float32")
        v[ok] = arr[t[ok], i[ok], j[ok]]
        radar[f"icon_{nome}"] = v
    radar["icon_salita850"] = radar.icon_u850 * radar.grad10_est + radar.icon_v850 * radar.grad10_nord
    return ICON


GFS_LEAD = ["gfs_p_fin", "gfs_p_max_ora", "gfs_rovesci", "gfs_nubi", "gfs_cape_max", "gfs_li_min",
            "gfs_p_prima", "gfs_p_dopo", "gfs_p_6h", "vic30_media", "vic30_max", "vic60_media", "vic60_max"]
ICON_LEAD = ["icon_p_fin", "icon_p_max_ora", "icon_p_prima", "icon_p_dopo", "icon_p_6h", "icon_rovesci", "icon_nubi",
             "icon_rh2m", "icon_raffiche", "icon_vic15_media", "icon_vic15_max", "icon_vic30_media", "icon_vic30_max"]


def _finestre(o: pd.DataFrame, k: int, campi: dict, ore: int = 3) -> pd.DataFrame:
    """Ore → finestre di `ore` ore per un punto della Previous Runs; campi: nome → (variabile, aggregazione).
    Con finestre di 1 h si aggiunge p_7h, la pioggia di ±3 h attorno all'ora (l'errore di fase dei modelli)."""
    gr = o.groupby(o.ora.dt.floor(f"{ore}h"))
    f = pd.DataFrame({n: (gr[f"{v}_previous_day{k}"].sum(min_count=ore) if a == "sum" else gr[f"{v}_previous_day{k}"].agg(a))
                      for n, (v, a) in campi.items()})
    f["p_prima"], f["p_dopo"] = f.p_fin.shift(1), f.p_fin.shift(-1)
    f["p_6h"] = f[["p_prima", "p_fin", "p_dopo"]].sum(axis=1, min_count=1)
    if ore == 1:
        f["p_7h"] = f.p_fin.rolling(7, center=True, min_periods=1).sum()
    return f


def costruisci_lead(k: int, campione: float = 0.5, ore: int = 3):
    """Come costruisci, ma GFS e ICON-2I sono le previsioni emesse k giorni prima (Previous Runs API, griglia_lazio.py
    lead) e c'è solo quello che la Previous Runs dà: niente vento in quota, CAPE ICON, nubi per strato, fulmini."""
    from scipy import ndimage
    celle = pd.read_parquet(g.LOCALE / "descrittori_celle.parquet")
    forma = (celle.i.max() + 1, celle.j.max() + 1)
    celle = celle[celle.frac_terra >= 0.5].reset_index(drop=True)
    celle["cella"] = np.arange(len(celle))
    punti = pd.read_parquet(g.LOCALE / "punti_nwp.parquet")
    campi_gfs = {"p_fin": ("precipitation", "sum"), "p_max_ora": ("precipitation", "max"), "rovesci": ("showers", "sum"),
                 "nubi": ("cloud_cover", "mean"), "cape_max": ("cape", "max"), "li_min": ("lifted_index", "min")}
    gfs = {p: _finestre(pd.read_parquet(g.LEAD_DIR / "gfs" / f"{p:04d}.parquet"), k, campi_gfs, ore) for p in punti.punto}
    finestre = gfs[punti.punto.iloc[0]].index
    nomi = list(gfs[punti.punto.iloc[0]].columns)
    arr = np.stack([gfs[p].reindex(finestre).to_numpy() for p in punti.punto]).astype("float32")   # punti × finestre × feat
    campi_icon = {"p_fin": ("precipitation", "sum"), "p_max_ora": ("precipitation", "max"), "rovesci": ("showers", "sum"),
                  "nubi": ("cloud_cover", "mean"), "rh2m": ("relative_humidity_2m", "mean"), "raffiche": ("wind_gusts_10m", "max")}
    nomi_icon = [*campi_icon, "p_prima", "p_dopo", "p_6h", *(["p_7h"] if ore == 1 else [])]
    cubo = np.full((len(nomi_icon), len(finestre), *forma), np.nan, "float32")
    n_icon = 0
    for c in celle.itertuples(index=False):
        f = g.LEAD_DIR / "icon" / f"{c.i * 1000 + c.j:04d}.parquet"
        if f.exists():
            cubo[:, :, c.i, c.j] = _finestre(pd.read_parquet(f), k, campi_icon, ore).reindex(finestre)[nomi_icon].to_numpy().T
            n_icon += 1
    logger.info(f"lead giorno {k}: GFS {arr.shape[0]} punti × {len(finestre):,} finestre, ICON-2I {n_icon}/{len(celle)} celle")
    # vicinato ICON solo sulle celle con dati (il mare non è scaricato): media = somma / numero di celle valide
    p = cubo[nomi_icon.index("p_fin")]
    valido = ~np.isnan(p)
    vic = {}
    for r_km, nome in ((15, "vic15"), (30, "vic30")):
        kk = 2 * int(round(r_km / 5)) + 1
        n = ndimage.uniform_filter(valido.astype("float32"), size=(1, kk, kk), mode="constant")
        vic[f"{nome}_media"] = np.where(n > 0, ndimage.uniform_filter(np.nan_to_num(p), size=(1, kk, kk), mode="constant")
                                        / np.maximum(n, 1e-6), np.nan)
        vic[f"{nome}_max"] = np.where(n > 0, ndimage.maximum_filter(np.where(valido, p, -1), size=(1, kk, kk), mode="constant"), np.nan)

    radar = pd.concat([pd.read_parquet(f) for f in sorted((g.LOCALE / ("radar_finestre" if ore == 3 else "radar_ore")).glob("*.parquet"))],
                      ignore_index=True)
    radar = radar.merge(celle[["i", "j", "cella"]], on=["i", "j"])
    radar = radar[radar.finestra >= g.INIZIO_ICON]
    radar = radar.sample(frac=campione, random_state=SEME).reset_index(drop=True)
    radar["w"] = finestre.get_indexer(radar.finestra)
    radar = radar[radar.w >= 0].reset_index(drop=True)
    logger.info(f"campione: {len(radar):,} righe cella × finestra ({campione:.0%})")
    ids, w = pesi_bilineari(celle, punti)
    c, t = radar.cella.to_numpy(), radar.w.to_numpy()
    for kf, nome in enumerate(nomi):
        v = arr[ids[c], t[:, None], kf]
        ww = np.where(np.isnan(v), 0, w[c])
        radar[f"gfs_{nome}"] = np.where(ww.sum(1) > 0, np.nansum(v * ww, 1) / np.maximum(ww.sum(1), 1e-9), np.nan)
    ip = nomi.index("p_fin")
    d = km(celle.lat.to_numpy()[:, None], celle.lon.to_numpy()[:, None], punti.lat.to_numpy()[None], punti.lon.to_numpy()[None])
    for r in (30, 60):
        media = np.full(len(radar), np.nan, "float32")
        massimo = np.full(len(radar), np.nan, "float32")
        for n in range(len(celle)):
            sel = np.where(c == n)[0]
            vicini = np.where(d[n] <= r)[0]
            if len(sel) and len(vicini):
                blocco = arr[vicini][:, t[sel], ip]
                with np.errstate(all="ignore"):
                    media[sel], massimo[sel] = np.nanmean(blocco, 0), np.nanmax(blocco, 0)
        radar[f"vic{r}_media"], radar[f"vic{r}_max"] = media, massimo
    i, j = radar.i.to_numpy().astype(int), radar.j.to_numpy().astype(int)
    for kf, nome in enumerate(nomi_icon):
        radar[f"icon_{nome}"] = cubo[kf, t, i, j]
    for nome, a in vic.items():
        radar[f"icon_{nome}"] = a[t, i, j].astype("float32")
    del cubo, vic
    radar = radar.merge(celle[["cella", "lat", "lon", *DESCRITTORI, *GRADIENTI]], on="cella")
    fh = radar.finestra.dt.hour + ore / 2
    doy = radar.finestra.dt.dayofyear
    radar["ora_sin"], radar["ora_cos"] = np.sin(2 * np.pi * fh / 24), np.cos(2 * np.pi * fh / 24)
    radar["doy_sin"], radar["doy_cos"] = np.sin(2 * np.pi * doy / 365.25), np.cos(2 * np.pi * doy / 365.25)
    radar["piove"] = (radar.mm >= g.SOGLIA_MM).astype(int)
    radar["anno"], radar["mese"], radar["ora_f"] = radar.finestra.dt.year, radar.finestra.dt.month, radar.finestra.dt.hour
    blocco = (np.floor((radar.lat - g.DOMINIO["lat_min"]) / 0.5) + np.floor((radar.lon - g.DOMINIO["lon_min"]) / 0.5)) % 2
    radar["blocco"] = blocco.astype(int)
    extra = ["p_7h"] if ore == 1 else []
    return radar, GFS_LEAD + [f"gfs_{n}" for n in extra], ICON_LEAD + [f"icon_{n}" for n in extra]


def addestra(tr: pd.DataFrame, cols: list[str]):
    taglio = tr.finestra.quantile(0.85)
    fit, val = tr[tr.finestra < taglio], tr[tr.finestra >= taglio]
    return lgb.train(PARAMETRI, lgb.Dataset(fit[cols], fit.piove), num_boost_round=3000,
                     valid_sets=[lgb.Dataset(val[cols], val.piove)], callbacks=[lgb.early_stopping(100, verbose=False)])


def main():
    sys.path.insert(0, str(ROOT / "scripts"))
    import ifs_corse_pioggia as ic
    OUT.mkdir(parents=True, exist_ok=True)
    import os
    target = os.environ.get("GRIGLIA_TARGET", "piove")     # "forte": ≥ 5 mm nella finestra di 3 h
    modo_icon = os.environ.get("GRIGLIA_MODO") == "icon"     # periodo ICON-2I (dal 13/04/2025), 2 anni
    lead = int(os.environ.get("GRIGLIA_LEAD", "0"))          # 1 o 2: GFS e ICON-2I emessi 1 o 2 giorni prima
    ore = int(os.environ.get("GRIGLIA_ORE", "3"))            # 1: pioggia oraria (solo con GRIGLIA_LEAD)
    verita = os.environ.get("GRIGLIA_VERITA", "radar")       # "corretta": radar × fattore dei pluviometri
    # fattore dai pluviometri MeteoNetwork controllati (radar_vs_pluviometri.py, 07/11/2026); quello vecchio di
    # radar_corretto.py (Netatmo/ARSIAL, quota e costa) con GRIGLIA_FATTORE=fattore_radar.parquet
    fattore = os.environ.get("GRIGLIA_FATTORE", "fattore_radar_mnw.parquet")
    if lead:
        d, col_gfs, col_icon = costruisci_lead(lead, campione=0.5 if ore == 3 else 0.17, ore=ore)
    elif modo_icon:
        import griglia_lazio as gl
        d, col_gfs, _ = costruisci(dal=gl.INIZIO_ICON, campione=0.5, icon=True)
    else:
        d, col_gfs, _ = costruisci()
    if verita == "corretta":
        f = pd.read_parquet(g.LOCALE / fattore)
        d["stagione"] = np.where(d.finestra.dt.month.between(4, 9), "calda", "fredda")
        d = d.merge(f, on=["i", "j", "stagione"], how="left")
        d["mm"] = d.mm * d.fattore.fillna(1.0)
        d["piove"] = (d.mm >= g.SOGLIA_MM).astype(int)
    if target == "forte":
        d["piove"] = (d.mm >= 5).astype(int)            # stessa colonna, così il resto non cambia
    suffisso = ("" if target == "piove" else f"_{target}") + ("_icon" if modo_icon else "") + ("_corretta" if verita == "corretta" else "") + (f"_lead{lead}" if lead else "") + ("_1h" if ore == 1 else "")
    tempo = ["ora_sin", "ora_cos", "doy_sin", "doy_cos"]
    base = [*col_gfs, *tempo]
    if lead:
        varianti = {"GFS": [*base, *DESCRITTORI], "GFS+ICON": [*base, *DESCRITTORI, *col_icon],
                    "GFS+ICON+rilievo": [*base, *DESCRITTORI, *GRADIENTI, *col_icon]}
    elif modo_icon:
        varianti = {"GFS+vento": [*base, *DESCRITTORI, *VENTO],
                    "GFS+vento+ICON": [*base, *DESCRITTORI, *VENTO, *ICON],
                    "GFS+vento+ICON+rilievo": [*base, *DESCRITTORI, *GRADIENTI, *VENTO, *INTERAZIONI, *ICON]}
    else:
        varianti = {"senza territorio": base, "con territorio": [*base, *DESCRITTORI],
                    "territorio+vento": [*base, *DESCRITTORI, *VENTO],
                    "territorio+vento+rilievo": [*base, *DESCRITTORI, *GRADIENTI, *VENTO, *INTERAZIONI]}
    logger.info(f"{len(d):,} righe, {d.cella.nunique()} celle, anni {sorted(d.anno.unique())}, piove {d.piove.mean():.1%}")
    risultati = []
    for test in sorted(d.anno.unique()):
        for schema in ("tempo", "tempo+spazio"):
            if schema == "tempo":
                tr, te = d[d.anno != test], d[d.anno == test].copy()
            else:
                tr, te = d[(d.anno != test) & (d.blocco == 0)], d[(d.anno == test) & (d.blocco == 1)].copy()
            clima = tr.groupby(["mese", "ora_f"]).piove.mean()
            te["p_clima"] = te.set_index(["mese", "ora_f"]).index.map(clima).to_numpy()
            te["p_clima"] = te.p_clima.fillna(tr.piove.mean())
            if schema == "tempo":
                cc = tr.groupby(["cella", "mese", "ora_f"]).piove.mean()
                te["p_clima_cella"] = te.set_index(["cella", "mese", "ora_f"]).index.map(cc).to_numpy()
                te["p_clima_cella"] = te.p_clima_cella.fillna(te.p_clima)
            for nome, cols in varianti.items():
                m = addestra(tr, cols)
                te[nome] = m.predict(te[cols], num_iteration=m.best_iteration)
            te["schema"] = schema
            risultati.append(te[["anno", "schema", "cella", "mese", "piove", "p_clima", *([] if lead else ["salita850", "barriera_monte"]),
                                 *(["p_clima_cella"] if schema == "tempo" else []), *varianti]])
            if schema == "tempo" and test == sorted(d.anno.unique())[-2]:     # ultima variante, penultimo anno
                imp = pd.Series(m.feature_importance("gain"), index=cols)
            logger.info(f"test {test}, {schema}: fatto ({len(te):,} righe)")
    p = pd.concat(risultati, ignore_index=True)
    p.to_parquet(OUT / f"griglia_lazio_previsioni{suffisso}.parquet", index=False)
    righe = []
    for schema, gr in p.groupby("schema"):
        insiemi = [("tutto", np.ones(len(gr), bool)), ("estati", gr.mese.between(6, 9).to_numpy())]
        if not lead:
            q90 = gr.salita850.quantile(0.9)
            insiemi += [("flusso in salita (10% più forte)", (gr.salita850 >= q90).to_numpy()),
                        ("sottovento a un rilievo (barriera ≥ 300 m)", (gr.barriera_monte >= 300).to_numpy())]
        for nome_ins, sel in insiemi:
            x = gr[sel]
            y = x.piove.to_numpy().astype(float)
            bc = ((x.p_clima - y) ** 2).mean()
            r = {"schema": schema, "insieme": nome_ins, "n": len(x), "freq": y.mean()}
            if schema == "tempo":
                r["BSS clima cella"] = 1 - ((x.p_clima_cella - y) ** 2).mean() / bc
            for v in varianti:
                r[f"BSS {v}"] = 1 - ((x[v] - y) ** 2).mean() / bc
                r[f"AUC {v}"] = ic._auc(x[v].to_numpy(), y.astype(int))
            righe.append(r)
    tab = pd.DataFrame(righe)
    tab.to_csv(OUT / f"griglia_lazio_risultati{suffisso}.csv", index=False)
    pd.set_option("display.width", 250)
    print(f"\nTarget: {target}. Test su griglia 0,05° (celle di terra del Lazio allargato), un anno fuori alla volta, "
          + (f"lead veri giorno {lead}, finestre di {ore} h." if lead else "lead ≈ 0."))
    print("BSS rispetto alla climatologia mese × ora delle celle di addestramento:")
    print(tab.to_string(index=False, float_format="{:.3f}".format))
    imp = imp / imp.sum()
    quota = lambda cols: imp.reindex([c for c in cols if c in imp.index]).sum()
    print(f"\nImportanza, {list(varianti)[-1]} (gain): vento e interazioni {quota([*VENTO, *INTERAZIONI]):.0%}, "
          f"territorio {quota([*DESCRITTORI, *GRADIENTI]):.0%}, ICON-2I {quota([c for c in imp.index if c.startswith("icon_")]):.0%}")
    print(" ".join(f"{k} {v:.3f}" for k, v in imp.sort_values(ascending=False).head(12).items()))
