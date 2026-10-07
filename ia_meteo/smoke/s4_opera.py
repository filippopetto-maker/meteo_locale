"""S4 — Composito radar europeo OPERA (EUMETNET / MeteoGate Open Radar Data).

Documentazione (07/10/2026): l'API MeteoGate (EDR, anonima, 200 richieste/h)
serve solo la cache delle ultime 24 h; lo storico (dal 2012) sta nel bucket S3
pubblico `openradar-archive` su CloudFerro (accesso anonimo, stesso schema di
chiavi della cache: AAAA/MM/GG/OPERA/COMP/OPERA@AAAAMMGGTHHMM@0@<prodotto>.h5).
- 2019 (ODYSSEY, 2 km, 15 min): prodotto `DBZH_QIND` (riflettività max + indice qualità)
- 2025 (CIRRUS, 1 km, 5 min): prodotto `DBZH`

Copertura: pixel validi = non `nodata` (fuori copertura); `undetect` (nessuna eco)
conta come dato valido, come da convenzione ODIM.
"""
import sys
import time
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import PREVIEWS, RAW, TEST_BBOXES, bbox_mask, mb, read_result, rm, write_result

TEST = "s4_opera"
DOMANDA = ("Il composito OPERA copre davvero Spagna, Balcani e mare, sia nell'era ODYSSEY (2 km) "
           "sia in quella CIRRUS (1 km)?")
API = "https://api.meteogate.eu/eu-eumetnet-weather-radar/collections/observations/locations/0-20010-0-OPERA"
S3 = "https://s3.waw3-1.cloudferro.com/openradar-archive"


def leggi_odim(path):
    import h5py
    from pyproj import Proj
    with h5py.File(path, "r") as f:
        where = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in f["where"].attrs.items()}
        what_root = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in f["what"].attrs.items()}
        # ODIM 2.0 (2019): quantity/gain in datasetN/what, DBZH e QIND in dataset separati;
        # ODIM recente (2025): quantity in dataset1/dataM/what
        dec = lambda a: {k: (v.decode() if isinstance(v, bytes) else v) for k, v in a.items()}
        quantita, sel, prod = [], None, ""
        for dsk in sorted(k for k in f if k.startswith("dataset")):
            dsw = dec(f[dsk]["what"].attrs) if "what" in f[dsk] else {}
            for dk in sorted(k for k in f[dsk] if k.startswith("data")):
                g = f[dsk][dk]
                w = dsw | (dec(g["what"].attrs) if "what" in g else {})
                quantita.append(w.get("quantity"))
                if w.get("quantity") == "DBZH" and sel is None:
                    sel, what, prod = g, w, w.get("product", "")
        raw = sel["data"][...]
    p = Proj(where["projdef"])
    ny, nx = raw.shape
    # coordinate di proiezione dei centri pixel dagli angoli UL/LR
    ulx, uly = p(where["UL_lon"], where["UL_lat"])
    x = ulx + (np.arange(nx) + 0.5) * where["xscale"]
    y = uly - (np.arange(ny) + 0.5) * where["yscale"]
    return raw, what, where, what_root, quantita, prod, p, x, y


def copertura(raw, what, p, x, y, passo=4):
    """% di pixel non-nodata per zona, su sottocampione 1 pixel ogni `passo`."""
    xs, ys = np.meshgrid(x[::passo], y[::passo])
    lon, lat = p(xs, ys, inverse=True)
    sub = raw[::passo, ::passo]
    valid = sub != what["nodata"]
    out = {}
    for z, bb in TEST_BBOXES.items():
        m = bbox_mask(lat, lon, bb)
        out[z] = round(100 * float(valid[m].mean()), 1) if m.any() else None
    return out, lon, lat, sub


def main():
    s3 = read_result("s3_li_flashes") or {}
    hh = s3.get("best_hour_utc", 12)
    casi = {
        "2019 ODYSSEY": f"2019/07/15/OPERA/COMP/OPERA@20190715T1200@0@DBZH_QIND.h5",
        "2025 CIRRUS": f"2025/07/15/OPERA/COMP/OPERA@20250715T{hh:02d}00@0@DBZH.h5",
    }
    out = RAW / "s4"
    out.mkdir(exist_ok=True)
    risultati, errori, mb_tot, secondi = {}, [], 0.0, {}

    # controllo API: lo storico non è servito dall'endpoint EDR (una richiesta sola, limite anonimo basso)
    try:
        r = requests.get(API, params=dict(datetime="2025-07-15T12:00Z/2025-07-15T12:10Z", f="CoverageJSON",
                                          standard_name="DBZH", format="ODIM"), timeout=60)
        api_note = (f"API EDR per 15/07/2025: HTTP {r.status_code}; "
                    f"X-RateLimit-Limit {r.headers.get('X-RateLimit-Limit')} "
                    f"(remaining {r.headers.get('X-RateLimit-Remaining')})")
    except Exception as e:  # noqa: BLE001
        api_note = f"API EDR: {e!r}"
    print(api_note)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axs = plt.subplots(1, 2, figsize=(11, 4.6), dpi=80)
    for ax, (nome, key) in zip(axs, casi.items()):
        f = out / Path(key).name
        try:
            t0 = time.perf_counter()
            r = requests.get(f"{S3}/{key}", timeout=120)
            r.raise_for_status()
            f.write_bytes(r.content)
            secondi[nome] = round(time.perf_counter() - t0, 2)
            mb_tot += mb(f)
            raw, what, where, what_root, quantita, prod, p, x, y = leggi_odim(f)
            cov, lon, lat, sub = copertura(raw, what, p, x, y)
            risultati[nome] = {
                "file": key, "MB": round(mb(f), 2), "prodotto_odim": prod, "quantita": quantita,
                "data_ora_odim": f"{what_root.get('date')} {what_root.get('time')}",
                "griglia": f"{raw.shape[1]}×{raw.shape[0]}",
                "risoluzione_m": [float(where["xscale"]), float(where["yscale"])],
                "projdef": where["projdef"],
                "gain_offset_nodata_undetect": [float(what["gain"]), float(what["offset"]),
                                                float(what["nodata"]), float(what["undetect"])],
                "dtype": str(raw.dtype),
                "copertura_pct": cov,
            }
            dbz = np.where((sub == what["nodata"]) | (sub == what["undetect"]), np.nan,
                           sub * what["gain"] + what["offset"])
            ax.set_facecolor("0.85")
            cov_mask = np.where(sub == what["nodata"], np.nan, 0.0)
            ax.pcolormesh(lon, lat, cov_mask, cmap="Greys", vmin=-1, vmax=1, shading="auto")
            im = ax.pcolormesh(lon, lat, dbz, cmap="turbo", vmin=0, vmax=60, shading="auto")
            for z, bb in TEST_BBOXES.items():
                ax.add_patch(plt.Rectangle((bb["lon_min"], bb["lat_min"]), bb["lon_max"] - bb["lon_min"],
                                           bb["lat_max"] - bb["lat_min"], fill=False, ec="k", lw=.8))
            ax.set_xlim(-12, 32); ax.set_ylim(28, 52)
            ax.set_title(f"OPERA {nome} {what_root.get('date')} {str(what_root.get('time'))[:4]} UTC", fontsize=9)
            print(nome, risultati[nome])
        except Exception as e:  # noqa: BLE001
            errori.append(f"{nome}: {e!r}")
    if risultati:
        fig.colorbar(im, ax=axs, label="dBZ (grigio scuro = coperto senza eco, grigio chiaro = fuori copertura)",
                     shrink=.8)
        fig.savefig(PREVIEWS / "s4_opera_copertura.png")
    plt.close(fig)
    rm(out)

    esito = "OK" if len(risultati) == 2 else ("PARZIALE" if risultati else "KO")
    risposta = " | ".join(f"{k}: " + ", ".join(f"{z} {v}%" for z, v in r["copertura_pct"].items())
                          for k, r in risultati.items())
    write_result(TEST, esito, DOMANDA, risposta,
                 misure={"secondi": secondi, "MB_scaricati": round(mb_tot, 2), "MB_conservati": 0,
                         "compositi": risultati},
                 errori=errori,
                 note=[api_note, "Storico solo via bucket S3 openradar-archive (anonimo); "
                       "API EDR MeteoGate = cache 24 h"])


if __name__ == "__main__":
    main()
