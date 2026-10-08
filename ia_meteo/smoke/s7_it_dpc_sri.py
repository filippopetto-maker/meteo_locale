"""S7 — Archivio radar italiano IT-DPC-SRI (fonti.itdpc).

Elenca i file del record Zenodo, apre lo Zarr in modo lazy dall'European Weather Cloud
(anonimo) e legge un timestep (15/07/2019 12:00 UTC) ritagliato sul Lazio.
MB trasferiti = dimensione dei chunk Zarr toccati + coordinate/metadati.
"""
import json
import sys
import time
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import PREVIEWS, TEST_BBOXES, write_result
from fonti import itdpc

TEST = "s7_it_dpc_sri"
DOMANDA = "Si può leggere un pezzo dell'archivio IT-DPC-SRI senza scaricare i 51 GB?"
T_TARGET = "2019-07-15T12:00"


def main():
    errori, sec, info = [], {}, {}
    z = requests.get(itdpc.ZENODO, timeout=60).json()
    files = [{"file": f["key"], "GB": round(f["size"] / 1e9, 2)} for f in z["files"]]
    fs = itdpc.filesystem()
    try:
        t0 = time.perf_counter()
        ds = itdpc.apri(fs)
        sec["apertura_lazy"] = round(time.perf_counter() - t0, 2)
        var = itdpc.variabile(ds)
        da = ds[var]
        info = {"variabili": list(ds.data_vars), "variabile_usata": var, "dims": dict(ds.sizes),
                "attrs_var": {k: str(v)[:200] for k, v in da.attrs.items()},
                "periodo": [str(ds.time.values[0])[:16], str(ds.time.values[-1])[:16]]}
        t0 = time.perf_counter()
        fin = itdpc.finestra_bbox(ds, TEST_BBOXES["lazio"])
        sec["finestra_lazio_da_latlon"] = round(time.perf_counter() - t0, 2)
        t0 = time.perf_counter()
        sub = itdpc.ritaglio(ds, T_TARGET, TEST_BBOXES["lazio"], fin).load()
        sec["lettura_timestep_lazio"] = round(time.perf_counter() - t0, 2)
        v = sub.values
        it = int(np.argmin(np.abs(ds.time.values - np.datetime64(T_TARGET))))
        za = json.loads(fs.cat(f"{itdpc.ZARR}/{var}/.zarray"))
        sep = za.get("dimension_separator", ".")
        ct, cy, cx = za["chunks"]
        ysl, xsl = fin
        keys = [sep.join(map(str, (it // ct, j, i)))
                for j in range(ysl.start // cy, (ysl.stop - 1) // cy + 1)
                for i in range(xsl.start // cx, (xsl.stop - 1) // cx + 1)]
        byt = sum(fs.size(f"{itdpc.ZARR}/{var}/{k}") for k in keys if fs.exists(f"{itdpc.ZARR}/{var}/{k}"))
        meta = fs.size(f"{itdpc.ZARR}/.zmetadata") if fs.exists(f"{itdpc.ZARR}/.zmetadata") else 0
        coord_b = sum(fs.du(f"{itdpc.ZARR}/{c}") for c in ("time", "lat", "lon", "x", "y") if fs.exists(f"{itdpc.ZARR}/{c}"))
        mb_tr = (byt + meta + coord_b) / 1e6
        info |= {"timestep_letto": str(sub.time.values)[:16], "shape_ritaglio": list(v.shape),
                 "chunk_zarr": za["chunks"], "compressore": str(za.get("compressor")),
                 "n_chunk_toccati": len(keys), "MB_chunk_dati": round(byt / 1e6, 2),
                 "MB_metadati_e_coordinate": round((meta + coord_b) / 1e6, 2),
                 "pioggia_mm_h": {"max": round(float(np.nanmax(v)), 2), "frac_pioggia>0.1": round(float((v > .1).mean()), 3),
                                  "frac_nan": round(float(np.isnan(v).mean()), 3)}}
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(5, 4.6), dpi=80)
        im = ax.pcolormesh(sub.lon.values, sub.lat.values, np.where(v > 0.1, v, np.nan), cmap="turbo", vmin=0, vmax=30)
        fig.colorbar(im, ax=ax, label="mm/h", shrink=.8)
        ax.set_title(f"IT-DPC-SRI {info['timestep_letto']} UTC — Lazio", fontsize=9)
        fig.tight_layout()
        fig.savefig(PREVIEWS / "s7_it_dpc_sri_lazio.png")
        plt.close(fig)
        esito = "OK"
        risposta = (f"Sì: Zarr anonimo su EWC ({itdpc.ENDPOINT}), apertura lazy {sec['apertura_lazy']} s, "
                    f"timestep {info['timestep_letto']} sul Lazio letto in {sec['lettura_timestep_lazio']} s "
                    f"({info['MB_chunk_dati']} MB di dati + {info['MB_metadati_e_coordinate']} MB di coordinate una tantum); "
                    f"Zenodo ha solo il tar.zst da {files[0]['GB']} GB")
    except Exception as e:  # noqa: BLE001
        errori.append(repr(e)[:1500])
        esito, mb_tr = "PARZIALE", 0
        risposta = f"Accesso lazy non riuscito; unica via certa il download del tar.zst ({files[0]['GB']} GB)"
    write_result(TEST, esito, DOMANDA, risposta,
                 misure={"secondi": sec, "MB_scaricati": round(mb_tr, 2), "MB_conservati": 0,
                         "file_zenodo": files, "dataset": info,
                         "metodo": f"xarray.open_zarr + s3fs anonimo, endpoint {itdpc.ENDPOINT}, s3://{itdpc.ZARR}"},
                 errori=errori,
                 note=["Pacchetto mlcast-datasets non necessario: il preprint dà percorso ed endpoint diretti",
                       "Il preprint conferma che l'Italia non partecipa al composito OPERA"])


if __name__ == "__main__":
    main()
