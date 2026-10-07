"""S7 — Archivio radar italiano IT-DPC-SRI (Zenodo 10.5281/zenodo.18637608).

- Zenodo: un solo file `italian-radar-dpc-sri.zarr.tar.zst` (~50 GB) → nessun accesso lazy da lì.
- Preprint arXiv:2602.15088 §6.2: copia identica su object storage S3 dell'ECMWF European
  Weather Cloud, lettura anonima:
  s3://mlcast-source-datasets/IT-DPC-SRI/v0.1.0/italian-radar-dpc-sri.zarr/
  endpoint https://object-store.os-api.cci2.ecmwf.int (lo stesso che usa `mlcast-datasets`).
Legge in modo lazy un timestep (15/07/2019 12:00 UTC) ritagliato sul Lazio.
MB trasferiti = somma delle dimensioni dei chunk Zarr toccati + metadati consolidati.
"""
import json
import sys
import time
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import PREVIEWS, TEST_BBOXES, write_result

TEST = "s7_it_dpc_sri"
DOMANDA = "Si può leggere un pezzo dell'archivio IT-DPC-SRI senza scaricare i 51 GB?"
ZENODO = "https://zenodo.org/api/records/18637608"
ENDPOINT = "https://object-store.os-api.cci2.ecmwf.int"
ZARR = "mlcast-source-datasets/IT-DPC-SRI/v0.1.0/italian-radar-dpc-sri.zarr"
T_TARGET = "2019-07-15T12:00"


def main():
    import s3fs
    import xarray as xr
    from pyproj import CRS, Transformer

    errori, sec = [], {}
    t0 = time.perf_counter()
    z = requests.get(ZENODO, timeout=60).json()
    files = [{"file": f["key"], "GB": round(f["size"] / 1e9, 2)} for f in z["files"]]
    sec["zenodo_api"] = round(time.perf_counter() - t0, 2)

    fs = s3fs.S3FileSystem(anon=True, client_kwargs={"endpoint_url": ENDPOINT})
    info = {}
    try:
        t0 = time.perf_counter()
        ds = xr.open_zarr(fs.get_mapper(ZARR), consolidated=True)
        sec["apertura_lazy"] = round(time.perf_counter() - t0, 2)
        var = [v for v in ds.data_vars][0] if len(ds.data_vars) == 1 else \
            next(v for v in ds.data_vars if ds[v].ndim == 3)
        da = ds[var]
        info = {"variabili": list(ds.data_vars), "variabile_usata": var, "dims": dict(ds.sizes),
                "coords": list(ds.coords), "chunks": [list(c[:1]) for c in da.chunks],
                "attrs_var": {k: str(v)[:200] for k, v in da.attrs.items()},
                "periodo": [str(ds.time.values[0])[:16], str(ds.time.values[-1])[:16]]}
        print(json.dumps(info, indent=1)[:2000])

        # Proiezione: cerca crs/spatial_ref; altrimenti coordinate lat/lon 2D
        it = int(np.argmin(np.abs(ds.time.values - np.datetime64(T_TARGET))))
        t_letto = str(ds.time.values[it])[:16]
        bb = TEST_BBOXES["lazio"]
        if "lat" in ds.coords and ds["lat"].ndim == 2:
            lat2, lon2 = ds["lat"].values, ds["lon"].values
            m = (lat2 >= bb["lat_min"]) & (lat2 <= bb["lat_max"]) & (lon2 >= bb["lon_min"]) & (lon2 <= bb["lon_max"])
            iy, ix = np.where(m)
            ysl, xsl = slice(iy.min(), iy.max() + 1), slice(ix.min(), ix.max() + 1)
            metodo_crop = "coordinate lat/lon 2D"
        else:
            crs_var = next((c for c in ("spatial_ref", "crs", "transverse_mercator") if c in ds.variables), None)
            wkt = ds[crs_var].attrs.get("crs_wkt") or ds[crs_var].attrs.get("spatial_ref") if crs_var else None
            tr = Transformer.from_crs("EPSG:4326", CRS.from_wkt(wkt) if wkt else CRS.from_proj4(
                "+proj=tmerc +lat_0=42 +lon_0=12.5 +k=1 +x_0=0 +y_0=0 +ellps=WGS84"), always_xy=True)
            xs, ys = tr.transform([bb["lon_min"], bb["lon_max"], bb["lon_min"], bb["lon_max"]],
                                  [bb["lat_min"], bb["lat_min"], bb["lat_max"], bb["lat_max"]])
            xd, yd = [d for d in da.dims if d != "time"][1], [d for d in da.dims if d != "time"][0]
            xv, yv = ds[xd].values, ds[yd].values
            ix = np.where((xv >= min(xs)) & (xv <= max(xs)))[0]
            iy = np.where((yv >= min(ys)) & (yv <= max(ys)))[0]
            ysl, xsl = slice(iy.min(), iy.max() + 1), slice(ix.min(), ix.max() + 1)
            metodo_crop = f"proiezione da {crs_var or 'proj4 tmerc dal record'}"
        t0 = time.perf_counter()
        sub = da.isel(time=it, **{da.dims[1]: ysl, da.dims[2]: xsl}).load()
        sec["lettura_timestep_lazio"] = round(time.perf_counter() - t0, 2)
        v = sub.values

        # byte trasferiti: chunk toccati
        za = json.loads(fs.cat(f"{ZARR}/{var}/.zarray"))
        sep = za.get("dimension_separator", ".")
        ct, cy, cx = za["chunks"]
        keys = [sep.join(map(str, (it // ct, j, i)))
                for j in range(ysl.start // cy, (ysl.stop - 1) // cy + 1)
                for i in range(xsl.start // cx, (xsl.stop - 1) // cx + 1)]
        byt = 0
        for k in keys:
            try:
                byt += fs.size(f"{ZARR}/{var}/{k}")
            except FileNotFoundError:
                pass
        meta = fs.size(f"{ZARR}/.zmetadata") if fs.exists(f"{ZARR}/.zmetadata") else 0
        coord_b = sum(fs.du(f"{ZARR}/{c}") for c in ds.coords if c in ("time", "x", "y", "lat", "lon") and fs.exists(f"{ZARR}/{c}"))
        mb_tr = (byt + meta + coord_b) / 1e6
        info |= {"timestep_letto": t_letto, "metodo_crop": metodo_crop, "shape_ritaglio": list(v.shape),
                 "chunk_zarr": za["chunks"], "compressore": str(za.get("compressor")),
                 "n_chunk_toccati": len(keys), "MB_chunk_dati": round(byt / 1e6, 2),
                 "MB_metadati_e_coordinate": round((meta + coord_b) / 1e6, 2),
                 "pioggia_mm_h": {"max": round(float(np.nanmax(v)), 2), "frac_pioggia>0.1": round(float((v > .1).mean()), 3),
                                  "frac_nan": round(float(np.isnan(v).mean()), 3)}}
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(5, 4.6), dpi=80)
        im = ax.imshow(np.where(v > 0.1, v, np.nan), cmap="turbo", vmin=0, vmax=30, origin="upper"
                       if (sub[da.dims[1]].values[0] > sub[da.dims[1]].values[-1]) else "lower")
        fig.colorbar(im, ax=ax, label="mm/h", shrink=.8)
        ax.set_title(f"IT-DPC-SRI {t_letto} UTC — Lazio", fontsize=9)
        fig.tight_layout()
        fig.savefig(PREVIEWS / "s7_it_dpc_sri_lazio.png")
        plt.close(fig)
        esito = "OK"
        risposta = (f"Sì: Zarr anonimo su EWC (s3 {ENDPOINT}), apertura lazy {sec['apertura_lazy']} s, "
                    f"timestep {t_letto} sul Lazio letto in {sec['lettura_timestep_lazio']} s "
                    f"trasferendo ~{mb_tr:.1f} MB ({len(keys)} chunk); Zenodo ha solo il tar.zst da "
                    f"{files[0]['GB']} GB")
    except Exception as e:  # noqa: BLE001
        errori.append(repr(e)[:1500])
        esito, mb_tr = "PARZIALE", 0
        risposta = f"Accesso lazy non riuscito; unica via certa il download del tar.zst ({files[0]['GB']} GB)"
    write_result(TEST, esito, DOMANDA, risposta,
                 misure={"secondi": sec, "MB_scaricati": round(mb_tr, 2), "MB_conservati": 0,
                         "file_zenodo": files, "dataset": info,
                         "metodo": f"xarray.open_zarr + s3fs anonimo, endpoint {ENDPOINT}, s3://{ZARR}"},
                 errori=errori,
                 note=["Pacchetto mlcast-datasets non necessario: il preprint dà percorso ed endpoint diretti",
                       "Il preprint conferma che l'Italia non partecipa al composito OPERA"])


if __name__ == "__main__":
    main()
