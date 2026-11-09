"""
archivio_radar.py — Archivio giornaliero del radar DPC (SRI) sull'Italia, sulle celle 0,05° (verità della pioggia)

L'archivio IT-DPC-SRI finisce il 31/12/2025; dal 2026 la fonte è l'API pubblica della piattaforma radar DPC
(https://dpc-radar.readthedocs.io/it/latest/api.html, licenza CC-BY-SA): un GeoTIFF SRI ogni 5 min, 1400×1200
a 1 km, sulla stessa griglia dell'archivio. L'API conserva circa 5–6 mesi a scorrere: quello che non si
archivia si perde. Qui per ogni giorno completo non ancora archiviato si scaricano i 288 passi, si riducono
sulle celle 0,05° dell'Italia (stessa assegnazione pixel → cella di archivio_corse.indici_cella,
coordinate dei pixel dal GeoTIFF) e si salva `sri_italia_AAAAMMGG.npz`:
  tempi                (passi,)            istanti dei passi disponibili (UTC)
  lat, lon             centri delle celle (lat decrescenti, lon crescenti)
  pioggia_media/_max   (passi, lat, lon)   media e massimo dei pixel della cella, mm/h × 100 in int16,
                                           -1 dove non c'è dato (fuori copertura o pixel mancanti)
I .npz vanno negli asset della release del mese (radar-AAAA-MM), come le corse dei modelli.

Il firewall dell'API blocca chi chiede troppo (23/10/2026, 4 thread): un thread e una pausa fra le richieste,
circa 7 minuti per giorno.

    python scripts/archivio_radar.py --uscita /tmp/radar [--dal 2026-10-01] [--salta-esistenti elenco.txt]

Senza --dal parte dal primo giorno ancora nello storico dell'API. Stampa i file nuovi, uno per riga, su stdout.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

API = "https://radar-api.protezionecivile.it"
ORIGIN = {"origin": "https://radar.protezionecivile.it", "Content-Type": "application/json"}
ITALIA = dict(lat_min=35.5, lat_max=47.5, lon_min=6.0, lon_max=19.0)       # = archivio_corse.ITALIA
PASSO_CELLA = 0.05
PAUSA_S = 0.5
ATTESA_BLOCCO_S = 900

logger = logging.getLogger("archivio_radar")
_sessione = requests.Session()


def centri_celle():
    lat_c = np.round(np.arange(50 - PASSO_CELLA / 2, 30, -PASSO_CELLA), 3)
    lon_c = np.round(np.arange(-10 + PASSO_CELLA / 2, 30, PASSO_CELLA), 3)
    lat_c = lat_c[(lat_c > ITALIA["lat_min"]) & (lat_c < ITALIA["lat_max"])]
    lon_c = lon_c[(lon_c > ITALIA["lon_min"]) & (lon_c < ITALIA["lon_max"])]
    return lat_c, lon_c


def richiesta(t: pd.Timestamp) -> requests.Response:
    ms = int(t.tz_localize("UTC").timestamp() * 1000)
    for tentativo in range(3):
        time.sleep(PAUSA_S)
        r = _sessione.post(f"{API}/downloadProduct", headers=ORIGIN,
                           data=json.dumps({"productType": "SRI", "productDate": ms}), timeout=60)
        if r.status_code != 403:
            return r
        # blocco del firewall, non prodotto mancante: si aspetta a lungo, poi ci si ferma
        logger.warning(f"{t}: 403 dal firewall, attendo {ATTESA_BLOCCO_S // 60} min")
        time.sleep(ATTESA_BLOCCO_S)
    raise RuntimeError("radar-api: 403 ripetuti, probabile blocco per troppe richieste")


def scarica_passo(t: pd.Timestamp):
    """GeoTIFF SRI all'istante t (UTC): (mm/h con NaN fuori copertura, sorgente aperta per le coordinate) o None."""
    from rasterio.io import MemoryFile
    for tentativo in range(5):
        try:
            r = richiesta(t)
            # prodotto assente o già uscito dallo storico: 404, oppure 500 con "S3 error ... HeadObject"
            if r.status_code == 404 or "HeadObject" in r.text or (r.ok and "error" in r.json()):
                return None
            r.raise_for_status()
            f = _sessione.get(r.json()["url"], timeout=60)
            f.raise_for_status()
            with MemoryFile(f.content) as mf, mf.open() as src:
                a = src.read(1).astype("float32")
                geo = (src.crs, src.transform, src.shape)
            a[a < 0] = np.nan
            return a, geo
        except (requests.RequestException, ValueError, KeyError) as e:
            attesa = 10 * (tentativo + 1)
            logger.warning(f"{t}: {str(e)[:100]}, riprovo tra {attesa} s")
            time.sleep(attesa)
    return None


def disponibile(t: pd.Timestamp) -> bool:
    r = richiesta(t)
    return r.ok and "url" in r.text


def primo_giorno(t0: pd.Timestamp, t1: pd.Timestamp) -> pd.Timestamp:
    """Primo giorno in [t0, t1) ancora nello storico dell'API (ricerca binaria su mezzogiorno)."""
    giorni = pd.date_range(t0.normalize(), t1, freq="D", inclusive="left")
    lo, hi = 0, len(giorni)
    while lo < hi:
        m = (lo + hi) // 2
        if disponibile(giorni[m] + pd.Timedelta(hours=12)):
            hi = m
        else:
            lo = m + 1
    return giorni[lo] if lo < len(giorni) else t1


def geometria(crs, transform, forma) -> dict:
    """Coordinate dei pixel dal GeoTIFF e assegnazione pixel → cella 0,05° (matrice di somma, ordine per il massimo)."""
    from rasterio.warp import transform as trasforma
    from scipy import sparse
    righe, colonne = np.mgrid[0:forma[0], 0:forma[1]]
    xs, ys = transform * (colonne.ravel() + 0.5, righe.ravel() + 0.5)
    lo, la = map(np.asarray, trasforma(crs, "EPSG:4326", xs, ys))
    lat_c, lon_c = centri_celle()
    # come archivio_corse.indici_cella: intervalli semiaperti di una griglia fissa, i bordi vanno a sud e a est
    r = np.floor((50 - la) / PASSO_CELLA + 1e-6).astype(int) - int(round((50 - lat_c[0]) / PASSO_CELLA - 0.5))
    c = np.floor((lo + 10) / PASSO_CELLA + 1e-6).astype(int) - int(round((lon_c[0] + 10) / PASSO_CELLA - 0.5))
    ok = (r >= 0) & (r < len(lat_c)) & (c >= 0) & (c < len(lon_c))
    cella = np.where(ok, r * len(lon_c) + c, -1)
    n_celle = len(lat_c) * len(lon_c)
    pix = np.where(ok)[0]
    ordine = pix[np.argsort(cella[pix], kind="stable")]
    inizi = np.r_[0, np.where(np.diff(cella[ordine]))[0] + 1]
    somma = sparse.csr_matrix((np.ones(len(pix), "float32"), (cella[pix], pix)), shape=(n_celle, len(cella)))
    return dict(lat_c=lat_c, lon_c=lon_c, n_celle=n_celle, ordine=ordine, inizi=inizi,
                presenti=cella[ordine][inizi], somma=somma)


def su_celle(blocco: np.ndarray, g: dict) -> tuple[np.ndarray, np.ndarray]:
    """blocco (passi, pixel) → media e massimo per cella (passi, lat, lon), NaN dove non c'è dato."""
    finito = np.isfinite(blocco)
    s = (g["somma"] @ np.where(finito, blocco, 0).T).T
    n = (g["somma"] @ finito.T.astype("float32")).T
    with np.errstate(all="ignore"):
        media = np.where(n > 0, s / n, np.nan)
        mx = np.full((len(blocco), g["n_celle"]), np.nan, "float32")
        mx[:, g["presenti"]] = np.fmax.reduceat(blocco[:, g["ordine"]], g["inizi"], axis=1)
    forma = (len(blocco), len(g["lat_c"]), len(g["lon_c"]))
    return media.reshape(forma), mx.reshape(forma)


def in_int16(a: np.ndarray) -> np.ndarray:
    return np.where(np.isfinite(a), np.rint(np.clip(a, 0, 327) * 100), -1).astype("int16")


def giorno(d: pd.Timestamp, uscita: Path, stato: dict) -> Path | None:
    inizio = time.time()
    tempi, pixel = [], []
    for t in pd.date_range(d, d + pd.Timedelta(days=1), freq="5min", inclusive="left"):
        x = scarica_passo(t)
        if x is None:
            continue
        a, geo = x
        if "geo" not in stato:
            stato["geo"] = geometria(*geo)
        tempi.append(t.to_datetime64())
        pixel.append(a.ravel())
    if not tempi:
        logger.warning(f"{d:%Y-%m-%d}: nessun passo disponibile")
        return None
    g = stato["geo"]
    media, mx = su_celle(np.stack(pixel), g)
    f = uscita / f"sri_italia_{d:%Y%m%d}.npz"
    np.savez_compressed(f, tempi=np.array(tempi, dtype="datetime64[m]"), lat=g["lat_c"], lon=g["lon_c"],
                        pioggia_media=in_int16(media), pioggia_max=in_int16(mx))
    logger.info(f"{f.name}: {len(tempi)}/288 passi, max {np.nanmax(mx):.1f} mm/h, {f.stat().st_size / 1e6:.1f} MB, "
                f"{(time.time() - inizio) / 60:.1f} min")
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uscita", type=Path, required=True)
    ap.add_argument("--dal", help="primo giorno (AAAA-MM-GG); senza, il primo ancora nello storico dell'API")
    ap.add_argument("--al", help="ultimo giorno (compreso); senza, ieri")
    ap.add_argument("--max-giorni", type=int, default=25, help="giorni per giro (~7 min l'uno)")
    ap.add_argument("--salta-esistenti", type=Path, help="file con i nomi già archiviati, uno per riga")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("rasterio").setLevel(logging.WARNING)
    args.uscita.mkdir(parents=True, exist_ok=True)
    fatti = set(args.salta_esistenti.read_text().split()) if args.salta_esistenti and args.salta_esistenti.exists() else set()
    oggi = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()
    fine = pd.Timestamp(args.al) + pd.Timedelta(days=1) if args.al else oggi
    dal = pd.Timestamp(args.dal) if args.dal else oggi - pd.Timedelta(days=200)
    giorni = [d for d in pd.date_range(dal, fine, freq="D", inclusive="left")
              if f"sri_italia_{d:%Y%m%d}.npz" not in fatti]
    if giorni:
        primo = primo_giorno(giorni[0], fine)
        if primo > giorni[0]:
            logger.info(f"storico dell'API dal {primo:%d/%m/%Y}: salto i giorni precedenti")
        # il giorno prima del primo intero può avere le ultime ore ancora disponibili: lo si lascia perdere,
        # un giorno parziale archiviato non verrebbe più ripreso
        giorni = [d for d in giorni if d >= primo]
    stato = {}
    for d in giorni[:args.max_giorni]:
        f = giorno(d, args.uscita, stato)
        if f:
            print(f, flush=True)


if __name__ == "__main__":
    main()
