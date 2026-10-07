"""S1 — SEVIRI storico (EUMETSAT, EO:EUM:DAT:MSG:HRSEVIRI).

Scarica 4 slot da 15 min del 15/07/2019 12-13 UTC, legge IR_108 con satpy,
ritaglia/riproietta su DOMAIN_BBOX a 0,05°, salva i ritagli netCDF in raw/
(servono a S9) e un PNG in previews/. Cancella zip e .nat dopo la misura.
"""
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import (DOMAIN_BBOX, PREVIEWS, RAW, Timer, area_def_regular, env, mb, rm,
                    write_result)

TEST = "s1_seviri"
DOMANDA = ("L'archivio SEVIRI si scarica con le nostre chiavi, quanto pesa un disco, "
           "e satpy lo legge e ritaglia sul dominio?")
COLL = "EO:EUM:DAT:MSG:HRSEVIRI"


def main():
    key, secret = env("EUMETSAT_CONSUMER_KEY"), env("EUMETSAT_CONSUMER_SECRET")
    if not (key and secret):
        return write_result(TEST, "SALTATO", DOMANDA, "Credenziale assente",
                            errori=["EUMETSAT_CONSUMER_KEY/SECRET mancanti in .env"])
    import eumdac
    import numpy as np
    from satpy import Scene

    T = Timer()
    errori, prodotti, http_note = [], [], []
    out_dir = RAW / "s1"
    out_dir.mkdir(exist_ok=True)
    area = area_def_regular(DOMAIN_BBOX, 0.05)

    with T("auth"):
        token = eumdac.AccessToken((key, secret))
        ds = eumdac.DataStore(token)
        coll = ds.get_collection(COLL)
    with T("search"):
        res = coll.search(dtstart=datetime(2019, 7, 15, 12, 0), dtend=datetime(2019, 7, 15, 13, 0))
        found = sorted(res, key=lambda p: p.sensing_start)
    print(f"trovati {len(found)} prodotti")
    for p in found[:4]:
        rec = {"id": str(p), "sensing_start": str(p.sensing_start), "sensing_end": str(p.sensing_end),
               "size_dichiarata_MB": round((p.size or 0) / 1e3, 1)}  # p.size è in KB
        try:
            zpath = out_dir / f"{p}.zip"
            t0 = time.perf_counter()
            with p.open() as fsrc, open(zpath, "wb") as fdst:
                while chunk := fsrc.read(1 << 20):
                    fdst.write(chunk)
            rec["sec_download"] = round(time.perf_counter() - t0, 1)
            rec["MB_zip"] = round(mb(zpath), 1)
            with zipfile.ZipFile(zpath) as z:
                nat = [n for n in z.namelist() if n.endswith(".nat")][0]
                z.extract(nat, out_dir)
            natp = out_dir / nat
            rec["MB_nat"] = round(mb(natp), 1)
            t0 = time.perf_counter()
            scn = Scene(reader="seviri_l1b_native", filenames=[str(natp)])
            scn.load(["IR_108"])
            loc = scn.resample(area, resampler="nearest", radius_of_influence=10000)
            da = loc["IR_108"].compute()
            rec["sec_lettura_ritaglio"] = round(time.perf_counter() - t0, 1)
            v = da.values
            rec["Tb_min_K"] = round(float(np.nanmin(v)), 1)
            rec["Tb_max_K"] = round(float(np.nanmax(v)), 1)
            rec["frac_nan"] = round(float(np.isnan(v).mean()), 4)
            lons, lats = area.get_lonlats()
            import xarray as xr
            t_nom = da.attrs.get("start_time")
            dsout = xr.Dataset({"IR_108": (("lat", "lon"), v.astype("float32"))},
                               coords={"lat": lats[:, 0], "lon": lons[0, :]},
                               attrs={"start_time": str(t_nom), "units": "K", "fonte": COLL})
            nc = out_dir / f"crop_{t_nom:%Y%m%dT%H%M}.nc"
            dsout.to_netcdf(nc)
            rec["crop"] = nc.name
            rec["MB_crop"] = round(mb(nc), 2)
            rm(zpath)
            rm(natp)
        except Exception as e:  # noqa: BLE001
            msg = repr(e)
            if "429" in msg:
                http_note.append("HTTP 429 ricevuto")
            errori.append(f"{p}: {msg}")
        prodotti.append(rec)
        print(rec)

    ok = [r for r in prodotti if "crop" in r]
    if ok:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import xarray as xr
        last = xr.open_dataset(out_dir / ok[-1]["crop"])
        fig, ax = plt.subplots(figsize=(8, 4.2), dpi=90)
        last.IR_108.plot(ax=ax, cmap="gray_r", vmin=200, vmax=310, cbar_kwargs={"label": "Tb IR 10.8 µm (K)"})
        ax.set_title(f"SEVIRI IR_108 {last.attrs['start_time'][:16]} UTC — ritaglio 0.05°")
        fig.tight_layout()
        fig.savefig(PREVIEWS / "s1_seviri_ir108.png")
        plt.close(fig)
        last.close()

    tutti = len(ok) == 4 and all(180 < r["Tb_min_K"] < 260 and 290 < r["Tb_max_K"] < 340 for r in ok)
    esito = "OK" if tutti else ("PARZIALE" if ok else "KO")
    media = lambda k: round(sum(r[k] for r in ok) / len(ok), 1) if ok else None
    risposta = (f"{len(found)} prodotti trovati in 1 h; {len(ok)}/4 scaricati e letti; "
                f"zip medio {media('MB_zip')} MB, .nat {media('MB_nat')} MB, "
                f"download medio {media('sec_download')} s; Tb {min((r['Tb_min_K'] for r in ok), default=None)}–"
                f"{max((r['Tb_max_K'] for r in ok), default=None)} K; ritaglio 0,05° {ok[0]['MB_crop'] if ok else None} MB")
    write_result(TEST, esito, DOMANDA, risposta,
                 misure={"secondi": T.t | {"download_tot": round(sum(r.get('sec_download', 0) for r in prodotti), 1)},
                         "MB_scaricati": round(sum(r.get("MB_zip", 0) for r in prodotti), 1),
                         "MB_conservati": round(sum(r.get("MB_crop", 0) for r in ok), 2),
                         "prodotti": prodotti, "http": http_note or ["nessun 429 osservato"]},
                 errori=errori)


if __name__ == "__main__":
    main()
