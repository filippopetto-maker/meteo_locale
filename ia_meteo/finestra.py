"""Costruzione di una finestra (evento) e caricamento su Hugging Face.

Una finestra = intervallo [inizio, fine) sul dominio, con tutte le fonti sulla griglia comune
0,05°. Si scrive in data/staging/<id>/, poi archivio.carica_finestra la porta su HF e la
cancella dal Mac. Il disco del Mac ospita al più: una finestra in staging + WORKER file
grezzi alla volta (~0,5 GB ciascuno per SEVIRI).

Uso:
    python finestra.py --id 20190715_06 --inizio 2019-07-15T06:00 --fine 2019-07-15T18:00 [--lazio] [--carica]
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

import archivio as ar
from common import DOMAIN_BBOX, TEST_BBOXES, area_def_regular, mb, rm
from fonti import era5, eumetsat as eu, imerg, itdpc, opera

WORKER = 3
INIZIO_FCI = datetime(2025, 1, 1)      # planning: FCI solo dal 2025 (e per il live)
INIZIO_CIRRUS = datetime(2024, 7, 1)   # OPERA 1 km / 5 min
INIZIO_LI = datetime(2024, 7, 4)
FINE_IMERG_V07 = datetime(2025, 10, 1)


def _impila(frames: list, tempi: list, lats, lons):
    import xarray as xr
    ds = xr.concat(frames, dim="time")
    return ds.assign_coords(time=np.array(tempi, dtype="datetime64[ns]"), lat=lats, lon=lons)


def _salva(ds, path: Path, tipo: dict[str, str]):
    for v in ds.variables:                       # niente codifiche ereditate dai file d'origine
        ds[v].encoding = {}
    enc = {v: dict(ar.CODIFICA[tipo.get(v, "float")]) for v in ds.data_vars}
    ds.to_netcdf(path, encoding=enc)
    return round(mb(path), 2)


def satellite(t0, t1, area, lavoro: Path, sensore: str, tok) -> tuple:
    """Ritagli SEVIRI o FCI su [t0, t1), WORKER prodotti in parallelo, grezzi cancellati subito."""
    import xarray as xr
    coll = eu.FCI if sensore == "fci" else eu.SEVIRI
    prodotti = eu.cerca(coll, t0, t1, tok)
    lons, lats = area.get_lonlats()

    def uno(i_p):
        i, p = i_p
        d = lavoro / f"w{i % WORKER}"
        d.mkdir(parents=True, exist_ok=True)
        if sensore == "fci":
            ds, m = eu.fci_ritaglio(p, area, d)
        else:
            ds, m = eu.seviri_ritaglio(p, area, d)
        return p.sensing_start, ds.drop_vars(["lat", "lon"]), m

    with ThreadPoolExecutor(WORKER) as ex:
        ris = list(ex.map(uno, enumerate(prodotti)))
    ris.sort(key=lambda r: r[0])
    ds = _impila([r[1] for r in ris], [r[0] for r in ris], lats[:, 0], lons[0])
    ds.attrs = {"sensore": sensore, "collezione": coll, "unita": "K"}
    mis = {"n": len(ris), "MB_scaricati": round(sum(r[2].get("MB_zip", r[2].get("MB_chunk", 0)) for r in ris), 1)}
    return ds, mis


def radar(t0, t1, area, lavoro: Path) -> tuple:
    """Compositi OPERA su [t0, t1) sulla griglia comune, WORKER file in parallelo."""
    import xarray as xr
    passo = timedelta(minutes=5 if t0 >= INIZIO_CIRRUS else 15)
    lons, lats = area.get_lonlats()
    istanti, t = [], t0
    while t < t1:
        istanti.append(t)
        t += passo

    def uno(t):
        try:
            f, _ = opera.scarica_dbzh(t, lavoro / "radar")
        except FileNotFoundError:
            return t, None, 0.0
        peso = mb(f)
        g = opera.su_griglia(opera.leggi_odim(f), area)
        rm(f)
        return t, g, peso

    with ThreadPoolExecutor(WORKER) as ex:
        ris = list(ex.map(uno, istanti))
    ok = [r for r in ris if r[1] is not None]
    ds = _impila([xr.Dataset({"dbz": (("lat", "lon"), r[1])}) for r in ok], [r[0] for r in ok], lats[:, 0], lons[0])
    ds.attrs = {"fonte": "OPERA openradar-archive", "nota": "max per cella; -32 = coperto senza eco; NaN = fuori copertura"}
    return ds, {"n": len(ok), "passo_min": passo.seconds // 60, "mancanti": [r[0].isoformat() for r in ris if r[1] is None],
                "MB_scaricati": round(sum(r[2] for r in ris), 1)}


def fulmini(t0, t1, area, lavoro: Path, tok) -> tuple:
    import xarray as xr
    lons, lats = area.get_lonlats()
    frames, tempi, peso = [], [], 0.0
    for p in eu.cerca(eu.LI_AF, t0, t1, tok):
        f = eu.scarica(p, lavoro, eu.li_body(p))
        peso += mb(f)
        la, lo, w, _ = eu.li_flash(f)
        rm(f)
        frames.append(xr.Dataset({"flash": (("lat", "lon"), eu.li_su_griglia(la, lo, w, area))}))
        tempi.append(p.sensing_start)
    ds = _impila(frames, tempi, lats[:, 0], lons[0])
    ds.attrs = {"fonte": eu.LI_AF, "unita": "flash×pixel per 10 min"}
    return ds, {"n": len(tempi), "MB_scaricati": round(peso, 1)}


def precipitazione_imerg(t0, t1, lavoro: Path) -> tuple:
    import xarray as xr
    imerg.login()
    frames, tempi, peso = [], [], 0.0
    for g in imerg.granuli(t0, t1):
        f = imerg.scarica([g], lavoro)[0]
        peso += mb(f)
        sub, _, _ = imerg.ritaglio(f, DOMAIN_BBOX)
        rm(f)
        frames.append(sub.rename("pioggia").to_dataset())
        tempi.append(datetime.strptime(f.name.split(".")[4][:16], "%Y%m%d-S%H%M%S"))
    ds = xr.concat(frames, dim="time").assign_coords(time=np.array(tempi, dtype="datetime64[ns]"))
    return ds, {"n": len(tempi), "MB_scaricati": round(peso, 1)}


def radar_italia(t0, t1) -> tuple:
    ds = itdpc.apri()
    sub = itdpc.ritaglio(ds, slice(np.datetime64(t0), np.datetime64(t1 - timedelta(seconds=1))),
                         TEST_BBOXES["lazio"]).load()
    out = sub.rename("pioggia").to_dataset()
    return out, {"n": int(sub.sizes["time"])}


def costruisci(fid: str, t0: datetime, t1: datetime, lazio: bool = False, sensore: str | None = None) -> Path:
    """Scrive data/staging/<fid>/ con satellite, radar, ambiente, imerg, [fulmini], [itdpc], meta.json."""
    ar.controlla_budget(ar.STIMA_FINESTRA_GB + 0.5 * WORKER, hf=False)   # finestra + grezzi in volo
    sensore = sensore or ("fci" if t0 >= INIZIO_FCI else "seviri")
    out = ar.STAGING / fid
    lavoro = ar.DATA / "lavoro" / fid
    out.mkdir(parents=True, exist_ok=True)
    area = area_def_regular(DOMAIN_BBOX, 0.05)
    tok = eu.token()
    meta = {"id": fid, "inizio": t0.isoformat(), "fine": t1.isoformat(), "satellite": sensore,
            "griglia": "EPSG:4326 0,05°", "bbox": DOMAIN_BBOX, "lazio": lazio, "misure": {}, "file": {}, "errori": {}}

    passi = [("satellite", lambda: satellite(t0, t1, area, lavoro, sensore, tok), {"IR_108": "Tb", "WV_062": "Tb",
              "WV_073": "Tb", "ir_105": "Tb", "wv_63": "Tb", "wv_73": "Tb"}),
             ("radar", lambda: radar(t0, t1, area, lavoro), {"dbz": "dBZ"}),
             ("ambiente", lambda: era5.fetch_env(t0, t1, lavoro), {})]
    if t0 < FINE_IMERG_V07:
        passi.append(("imerg", lambda: precipitazione_imerg(t0, t1, lavoro), {}))
    if t0 >= INIZIO_LI:
        passi.append(("fulmini", lambda: fulmini(t0, t1, area, lavoro, tok), {}))
    if lazio:
        passi.append(("itdpc", lambda: radar_italia(t0, t1), {}))

    for nome, fn, tipo in passi:
        t_start = time.perf_counter()
        try:
            ds, mis = fn()
            if nome == "ambiente":
                mis = {"MB_scaricati": round(mis, 2), "ore": int(ds.sizes["time"])}
            mis["MB_file"] = _salva(ds, out / f"{nome}.nc", tipo)
            mis["secondi"] = round(time.perf_counter() - t_start, 1)
            meta["misure"][nome] = mis
            meta["file"][nome] = f"{nome}.nc"
            print(f"[{fid}] {nome}: {mis}")
        except Exception as e:  # noqa: BLE001
            meta["errori"][nome] = repr(e)[:500]
            print(f"[{fid}] {nome} ERRORE: {e!r}")
    shutil.rmtree(lavoro, ignore_errors=True)
    (out / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False, default=str))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--inizio", required=True, type=datetime.fromisoformat)
    ap.add_argument("--fine", required=True, type=datetime.fromisoformat)
    ap.add_argument("--lazio", action="store_true")
    ap.add_argument("--sensore", choices=["seviri", "fci"])
    ap.add_argument("--carica", action="store_true", help="carica su HF e cancella dal Mac")
    a = ap.parse_args()
    d = costruisci(a.id, a.inizio, a.fine, a.lazio, a.sensore)
    if a.carica:
        print(ar.carica_finestra(d))


if __name__ == "__main__":
    main()
