"""
archivio_corse.py — Archivio delle corse vere dei modelli sul Lazio allargato (da Open-Meteo su AWS)

Open-Meteo pubblica le corse intere dei modelli su S3 (s3://openmeteo/data_run/<modello>/AAAA/MM/GG/HHMMZ/,
licenza CC-BY 4.0, senza quote). Qui, per ogni corsa non ancora archiviata, si leggono solo i pezzi dei file
.om che coprono il dominio (lettura parziale) e si salva un .npz per modello e corsa:
  - modelli a passo < 0,05° (ICON-2I, AROME HD): media sui pixel di ogni cella 0,05° delle finestre radar
    (lazio_sri.centri_griglia), più il massimo sui pixel per pioggia, CAPE e fulmini; la pioggia ICON-2I
    anche ai pixel nativi (per la rete neurale);
  - modelli più larghi (GFS, IFS, ICON-EU): i pixel nativi del ritaglio con un margine di 0,5°;
  - ensemble IFS (O1280, 51 membri): i punti della griglia gaussiana nel ritaglio, primi 72 h.
Vento come componenti u/v (m/s). Valori float16. L'asse del tempo di ogni campo parte da `tempi[0]` (l'istante
della corsa; la pioggia lì è vuota) e può avere un passo in meno di `tempi` alla fine. I .npz vanno negli asset di una release GitHub per mese
(corse-AAAA-MM), così l'archivio non pesa sul repository né sul Mac.

    python scripts/archivio_corse.py --uscita /tmp/corse [--dal 2026-07-25] [--salta-esistenti elenco.txt]

Senza --dal guarda le corse degli ultimi 2 giorni. Stampa i file nuovi, uno per riga, su stdout.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

BUCKET = "openmeteo/data_run"
DOMINIO = dict(lat_min=40.7, lat_max=43.0, lon_min=11.3, lon_max=14.1)     # = lazio_sri.BBOX_LAZIO
MARGINE = 0.5
PASSO_CELLA = 0.05
ORE_ENS = 72

PRESSIONE = [850, 700, 500]
MODELLI = {
    # nome: (cartella su S3, corse UTC, a celle?, variabili). Le variabili wind_speed/direction diventano u/v.
    "icon2i": ("italia_meteo_arpae_icon_2i", (0, 12), True,
               ["precipitation", "showers", "rain", "cape", "convective_inhibition", "lightning_potential",
                "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
                "total_column_integrated_water_vapour", "freezing_level_height", "pressure_msl",
                "relative_humidity_2m", "temperature_2m", "wind_gusts_10m", "shortwave_radiation", "direct_radiation",
                "geopotential_height_500hPa", "wind_10m",
                *[f"{v}_{p}hPa" for p in PRESSIONE for v in ("relative_humidity", "temperature", "vertical_velocity")],
                *[f"wind_{p}hPa" for p in PRESSIONE]]),
    "arome_hd": ("meteofrance_arome_france_hd", (0, 6, 12, 18), True,
                 ["precipitation", "cape", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
                  "relative_humidity_2m", "temperature_2m", "wind_gusts_10m", "wind_uv_10m"]),
    "gfs013": ("ncep_gfs013", (0, 6, 12, 18), False,
               ["precipitation", "showers", "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
                "total_column_integrated_water_vapour", "boundary_layer_height", "relative_humidity_2m", "wind_uv_10m"]),
    "gfs025": ("ncep_gfs025", (0, 6, 12, 18), False,
               ["cape", "convective_inhibition", "lifted_index", "freezing_level_height", "pressure_msl",
                *[f"{v}_{p}hPa" for p in PRESSIONE for v in ("relative_humidity", "vertical_velocity")],
                *[f"wind_uv_{p}hPa" for p in PRESSIONE]]),
    "ifs025": ("ecmwf_ifs025", (0, 6, 12, 18), False,
               ["precipitation", "showers", "cape", "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
                "total_column_integrated_water_vapour", "relative_humidity_2m", "pressure_msl", "wind_uv_10m",
                *[f"{v}_{p}hPa" for p in PRESSIONE for v in ("relative_humidity", "vertical_velocity")],
                *[f"wind_uv_{p}hPa" for p in PRESSIONE]]),
    "icon_eu": ("dwd_icon_eu", (0, 6, 12, 18), False,
                ["precipitation", "showers", "cape", "convective_inhibition", "cloud_cover_low", "cloud_cover_mid",
                 "cloud_cover_high", "convective_cloud_top", "freezing_level_height", "relative_humidity_2m",
                 "wind_gusts_10m", "wind_uv_10m"]),
    "ifs_ens": ("ecmwf_ifs_europe_ensemble", (0, 12), False, ["precipitation", "cloud_cover"]),
}
MASSIMO = {"precipitation", "cape", "lightning_potential", "showers"}

logger = logging.getLogger("archivio_corse")


def centri_celle():
    """Centri delle celle 0,05° di lazio_sri.centri_griglia (lat decrescenti, lon crescenti)."""
    lat_c = np.round(np.arange(50 - PASSO_CELLA / 2, 30, -PASSO_CELLA), 3)
    lon_c = np.round(np.arange(-10 + PASSO_CELLA / 2, 30, PASSO_CELLA), 3)
    lat_c = lat_c[(lat_c > DOMINIO["lat_min"]) & (lat_c < DOMINIO["lat_max"])]
    lon_c = lon_c[(lon_c > DOMINIO["lon_min"]) & (lon_c < DOMINIO["lon_max"])]
    return lat_c, lon_c


def griglia_regolare(meta: dict, forma: tuple[int, int]):
    """lat/lon dei pixel dal BBOX del crs_wkt (griglie regolari; la gaussiana del GFS 0,13° è quasi regolare)."""
    bb = meta["crs_wkt"].split("BBOX[")[1].split("]")[0]
    la0, lo0, la1, lo1 = map(float, bb.split(","))
    return np.linspace(la0, la1, forma[0]), np.linspace(lo0, lo1, forma[1])


def punti_o1280(meta: dict):
    """Punti della griglia gaussiana ridotta O1280 nel BBOX, nell'ordine dei file (da nord, da ovest)."""
    bb = meta["crs_wkt"].split("BBOX[")[1].split("]")[0]
    la0, lo0, la1, lo1 = map(float, bb.split(","))
    n = 1280
    x, _ = np.polynomial.legendre.leggauss(2 * n)
    lat = np.degrees(np.arcsin(x))[::-1]
    per_riga = np.concatenate([4 * np.arange(1, n + 1) + 16, (4 * np.arange(1, n + 1) + 16)[::-1]])
    la, lo = [], []
    for l, k in zip(lat, per_riga):
        if not la0 - 1e-3 <= l <= la1 + 1e-3:
            continue
        s = np.arange(k) * 360 / k
        s = np.sort(np.where(s > 180, s - 360, s))
        s = s[(s >= lo0 - 1e-6) & (s <= lo1 + 1e-6)]
        la += [l] * len(s)
        lo += list(s)
    return np.array(la), np.array(lo)


INTERO_MB = 50     # sotto questa taglia il file si scarica intero: più veloce di centinaia di letture a pezzi


def leggi(fs, cartella: str, var: str, sel):
    import tempfile
    import omfiles
    p = f"{BUCKET}/{cartella}/{var}.om"
    if fs.size(p) > INTERO_MB * 1e6:
        return np.asarray(omfiles.OmFileReader.from_fsspec(fs, p)[sel], dtype="float32")
    with tempfile.NamedTemporaryFile(suffix=".om") as t:
        t.write(fs.cat_file(p))
        t.flush()
        r = omfiles.OmFileReader.from_path(t.name)
        a = np.asarray(r[sel], dtype="float32")
        r.close()
        return a


def componenti(fs, cartella, var, sel, presenti):
    """wind_10m / wind_850hPa (velocità e direzione) o wind_uv_* (componenti) → (u, v) in m/s."""
    liv = var.split("_", 2)[-1] if var.startswith("wind_uv_") else var.split("_", 1)[1]
    if var.startswith("wind_uv_"):
        u, v = f"wind_u_component_{liv}", f"wind_v_component_{liv}"
        if u not in presenti:
            return None
        return {f"u_{liv}": leggi(fs, cartella, u, sel), f"v_{liv}": leggi(fs, cartella, v, sel)}
    s, d = f"wind_speed_{liv}", f"wind_direction_{liv}"
    if s not in presenti:
        return None
    ws = leggi(fs, cartella, s, sel)                                       # nei .om è già in m/s
    dr = np.radians(leggi(fs, cartella, d, sel))
    return {f"u_{liv}": -ws * np.sin(dr), f"v_{liv}": -ws * np.cos(dr)}   # direzione di provenienza


def archivia(fs, nome: str, corsa: datetime, uscita: Path) -> Path | None:
    cartella_s3, _, a_celle, variabili = MODELLI[nome]
    cartella = f"{cartella_s3}/{corsa:%Y/%m/%d/%H%M}Z"
    try:
        meta = json.loads(fs.cat_file(f"{BUCKET}/{cartella}/meta.json"))
    except FileNotFoundError:
        return None
    presenti = {Path(p).stem for p in fs.ls(f"{BUCKET}/{cartella}", detail=False)}
    tempi = np.array([t.replace("Z", "") for t in meta["valid_times"]], dtype="datetime64[m]")
    import omfiles
    campione = next(v for v in ("precipitation", "cape", "cloud_cover") if v in presenti)
    forma = omfiles.OmFileReader.from_fsspec(fs, f"{BUCKET}/{cartella}/{campione}.om").shape
    out = {"tempi": tempi, "corsa": np.datetime64(corsa.replace(tzinfo=None), "m")}
    if nome == "ifs_ens":
        la, lo = punti_o1280(meta)
        assert len(la) == forma[1], f"O1280: {len(la)} punti calcolati, {forma[1]} nel file"
        k = np.where((la >= DOMINIO["lat_min"] - MARGINE) & (la <= DOMINIO["lat_max"] + MARGINE)
                     & (lo >= DOMINIO["lon_min"] - MARGINE) & (lo <= DOMINIO["lon_max"] + MARGINE))[0]
        nt = min(ORE_ENS + 1, forma[3])
        out["lat"], out["lon"], out["tempi"] = la[k], lo[k], tempi[:nt]
        tagli = np.split(k, np.where(np.diff(k) > 1)[0] + 1)               # un segmento contiguo per riga
        import omfiles
        for v in variabili:
            if v in presenti:
                r = omfiles.OmFileReader.from_fsspec(fs, f"{BUCKET}/{cartella}/{v}.om")
                out[v] = np.concatenate([np.asarray(r[0, s[0]:s[-1] + 1, :, :nt], dtype="float32")
                                         for s in tagli]).astype("float16")      # punti × membri × ore
    else:
        lat, lon = griglia_regolare(meta, forma[:2])
        m = 0.0 if a_celle else MARGINE
        ri = np.where((lat >= DOMINIO["lat_min"] - m - 0.05) & (lat <= DOMINIO["lat_max"] + m + 0.05))[0]
        ci = np.where((lon >= DOMINIO["lon_min"] - m - 0.05) & (lon <= DOMINIO["lon_max"] + m + 0.05))[0]
        sel = (slice(ri.min(), ri.max() + 1), slice(ci.min(), ci.max() + 1), slice(None))
        la, lo = lat[ri.min():ri.max() + 1], lon[ci.min():ci.max() + 1]
        campi = {}
        for v in variabili:
            if v.startswith("wind_"):
                c = componenti(fs, cartella, v, sel, presenti)
                if c:
                    campi.update(c)
            elif v in presenti:
                campi[v] = leggi(fs, cartella, v, sel)
        if a_celle:
            lat_c, lon_c = centri_celle()
            r = np.rint((lat_c[0] - la) / PASSO_CELLA).astype(int)
            c = np.rint((lo - lon_c[0]) / PASSO_CELLA).astype(int)
            ok_r, ok_c = (r >= 0) & (r < len(lat_c)), (c >= 0) & (c < len(lon_c))
            idx = np.full((len(la), len(lo)), -1)
            idx[np.ix_(ok_r, ok_c)] = r[ok_r][:, None] * len(lon_c) + c[ok_c][None, :]
            idx = idx.ravel()
            valido = idx >= 0
            n_celle = len(lat_c) * len(lon_c)
            conta = np.bincount(idx[valido], minlength=n_celle).astype("float32")
            out["lat"], out["lon"] = lat_c, lon_c
            for v, a in campi.items():
                piatto = a.reshape(-1, a.shape[-1])[valido]
                vuoto = np.isnan(piatto)
                s = np.zeros((n_celle, a.shape[-1]), "float32")
                np.add.at(s, idx[valido], np.nan_to_num(piatto))
                n = np.zeros((n_celle, a.shape[-1]), "float32")
                np.add.at(n, idx[valido], (~vuoto).astype("float32"))
                media = np.where(n > 0, s / np.maximum(n, 1), np.nan)
                out[v] = media.reshape(len(lat_c), len(lon_c), -1).astype("float16")
                if v in MASSIMO:
                    mx = np.full((n_celle, a.shape[-1]), -np.inf, "float32")
                    np.maximum.at(mx, idx[valido], np.where(vuoto, -np.inf, piatto))
                    out[f"{v}_max"] = np.where(np.isfinite(mx), mx, np.nan).reshape(len(lat_c), len(lon_c), -1).astype("float16")
            if nome == "icon2i" and "precipitation" in campi:
                out["precipitation_nativa"] = campi["precipitation"].astype("float16")
                out["lat_nativa"], out["lon_nativa"] = la, lo
            del conta
        else:
            out["lat"], out["lon"] = la, lo
            for v, a in campi.items():
                out[v] = a.astype("float16")
    f = uscita / f"{nome}_{corsa:%Y%m%d%H}.npz"
    np.savez_compressed(f, **out)
    logger.info(f"{f.name}: {len([k for k in out if k not in ('tempi', 'corsa', 'lat', 'lon')])} campi, "
                f"{f.stat().st_size / 1e6:.1f} MB")
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uscita", type=Path, required=True)
    ap.add_argument("--dal", help="prima data da archiviare (AAAA-MM-GG); senza, gli ultimi 2 giorni")
    ap.add_argument("--al", help="ultima data (compresa)")
    ap.add_argument("--salta-esistenti", type=Path, help="file con i nomi già archiviati, uno per riga")
    ap.add_argument("--modelli", default=",".join(MODELLI))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    import s3fs
    fs = s3fs.S3FileSystem(anon=True)
    args.uscita.mkdir(parents=True, exist_ok=True)
    fatti = set(args.salta_esistenti.read_text().split()) if args.salta_esistenti and args.salta_esistenti.exists() else set()
    adesso = datetime.now(timezone.utc)
    inizio = datetime.fromisoformat(args.dal).replace(tzinfo=timezone.utc) if args.dal else adesso - timedelta(days=2)
    fine = (datetime.fromisoformat(args.al).replace(tzinfo=timezone.utc) + timedelta(days=1)) if args.al else adesso
    giorno = inizio.replace(hour=0, minute=0, second=0, microsecond=0)
    while giorno < fine:
        for nome in args.modelli.split(","):
            for h in MODELLI[nome][1]:
                corsa = giorno.replace(hour=h)
                if corsa > adesso or f"{nome}_{corsa:%Y%m%d%H}.npz" in fatti:
                    continue
                try:
                    f = archivia(fs, nome, corsa, args.uscita)
                except Exception as e:                        # una corsa rotta non deve fermare le altre
                    logger.warning(f"{nome} {corsa:%Y-%m-%d %HZ}: {type(e).__name__}: {e}")
                    continue
                if f:
                    print(f, flush=True)
        giorno += timedelta(days=1)


if __name__ == "__main__":
    main()
