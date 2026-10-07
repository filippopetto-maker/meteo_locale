"""S3 — Fulmini MTG LI Accumulated Flashes (EO:EUM:DAT:0686).

Scarica i prodotti AF del 15/07/2025 10-18 UTC (uno ogni 10 min), scarica solo
l'entry BODY (il TRAIL non serve per leggere), converte x/y (angoli di scansione
(griglia geostazionaria FCI 2 km) in lat/lon (convenzione satpy), somma
`flash_accumulation` per ora e per zona.
Geolocalizzazione: stessa convenzione del reader satpy `li_l2_nc` (indici interi
x/y → area `mtg_fci_fdss_2km`). Un primo tentativo con x/y in radianti + pyproj
dava una mappa specchiata est-ovest (Atlante orientato NO-SE): scartato. Scrive `best_hour_utc` per S2/S4.

Nota: `flash_accumulation` è "per area accumulation of flashes" (flash per pixel):
la somma su un'area conta pixel×flash, non flash distinti (un flash copre più pixel).
"""
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DOMAIN_BBOX, PREVIEWS, RAW, TEST_BBOXES, Timer, bbox_mask, env, mb, rm, write_result

TEST = "s3_li_flashes"
DOMANDA = "I file LI si leggono, e possiamo usarli per trovare automaticamente le ore convettive?"
COLL = "EO:EUM:DAT:0686"
ZONE = ["spagna", "italia", "balcani"]


def main():
    key, secret = env("EUMETSAT_CONSUMER_KEY"), env("EUMETSAT_CONSUMER_SECRET")
    if not (key and secret):
        return write_result(TEST, "SALTATO", DOMANDA, "Credenziale assente")
    import eumdac
    import numpy as np
    import xarray as xr
    from pyproj import Transformer
    from satpy.area import get_area_def

    T, errori = Timer(), []
    out = RAW / "s3"
    out.mkdir(exist_ok=True)
    with T("search"):
        ds = eumdac.DataStore(eumdac.AccessToken((key, secret)))
        prods = sorted(ds.get_collection(COLL).search(dtstart=datetime(2025, 7, 15, 10, 0),
                                                       dtend=datetime(2025, 7, 15, 18, 0)),
                       key=lambda p: p.sensing_start)
    prods = [p for p in prods if datetime(2025, 7, 15, 10) <= p.sensing_start < datetime(2025, 7, 15, 18)]
    print(f"{len(prods)} prodotti")

    conteggi = defaultdict(lambda: defaultdict(float))   # ora -> zona -> somma
    mb_tot, struttura, n_ok = 0.0, None, 0
    all_lat, all_lon, all_w = [], [], []
    area = None
    for p in prods:
        try:
            body = [e for e in p.entries if "BODY" in e and e.endswith(".nc")][0]
            f = out / body
            with T("download"):
                with p.open(entry=body) as src, open(f, "wb") as dst:
                    while c := src.read(1 << 20):
                        dst.write(c)
            mb_tot += mb(f)
            with T("lettura"):
                d = xr.open_dataset(f)
                draw = xr.open_dataset(f, mask_and_scale=False)
                if area is None:
                    # stessa convenzione del reader satpy li_l2_nc: x/y interi 1..5568 con origine
                    # in basso a sinistra (SW) → righe capovolte, poi area mtg_fci_fdss_2km
                    area = get_area_def("mtg_fci_fdss_2km")
                    tr = Transformer.from_crs(area.crs, "EPSG:4326", always_xy=True)
                    struttura = {
                        "variabili": list(d.data_vars),
                        "dimensioni": dict(d.sizes),
                        "griglia": "sparsa (pixels) su griglia geostazionaria FCI 5568×5568 (area satpy "
                                   "mtg_fci_fdss_2km), ≈2 km al nadir, ~3-4 km a 40-45°N",
                        "accumulazioni_per_file": int(d.sizes["accumulations"]),
                        "durata_file_s": d.attrs.get("time_coverage_duration"),
                        "unita": d.flash_accumulation.attrs.get("units"),
                    }
                rows = 5568 - draw.y.values.astype(int)
                cols = draw.x.values.astype(int) - 1
                # solo i pixel attivi (get_lonlat sull'intera griglia satura gli 8 GB)
                x0, _, _, y1 = area.area_extent
                xm = x0 + (cols + 0.5) * area.pixel_size_x
                ym = y1 - (rows + 0.5) * area.pixel_size_y
                lon, lat = tr.transform(xm, ym)
                draw.close()
                w = d.flash_accumulation.values
                ok = np.isfinite(lon) & np.isfinite(lat) & (np.abs(lon) < 1e3)
                lat, lon, w = lat[ok], lon[ok], w[ok]
                hr = p.sensing_start.hour
                conteggi[hr]["dominio"] += float(w[bbox_mask(lat, lon, DOMAIN_BBOX)].sum())
                for z in ZONE:
                    conteggi[hr][z] += float(w[bbox_mask(lat, lon, TEST_BBOXES[z])].sum())
                m = bbox_mask(lat, lon, DOMAIN_BBOX)
                all_lat.append(lat[m]); all_lon.append(lon[m]); all_w.append(w[m])
                d.close()
            n_ok += 1
            rm(f)
        except Exception as e:  # noqa: BLE001
            errori.append(f"{p}: {e!r}")
    tab = {f"{h:02d}": {k: round(v, 1) for k, v in conteggi[h].items()} for h in sorted(conteggi)}
    best = max(conteggi, key=lambda h: conteggi[h]["dominio"]) if conteggi else None
    for h, r in tab.items():
        print(h, r)

    if all_lat:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        lat, lon, w = map(np.concatenate, (all_lat, all_lon, all_w))
        H, xe, ye = np.histogram2d(lon, lat, bins=[200, 100], weights=w,
                                   range=[[DOMAIN_BBOX["lon_min"], DOMAIN_BBOX["lon_max"]],
                                          [DOMAIN_BBOX["lat_min"], DOMAIN_BBOX["lat_max"]]])
        fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 3.8), dpi=80, gridspec_kw={"width_ratios": [2, 1]})
        im = a1.pcolormesh(xe, ye, np.log10(H.T + 1), cmap="magma_r")
        for z in ZONE:
            b = TEST_BBOXES[z]
            a1.add_patch(plt.Rectangle((b["lon_min"], b["lat_min"]), b["lon_max"] - b["lon_min"],
                                       b["lat_max"] - b["lat_min"], fill=False, ec="tab:blue", lw=1))
            a1.text(b["lon_min"] + .2, b["lat_max"] - .8, z, color="tab:blue", fontsize=8)
        fig.colorbar(im, ax=a1, label="log10(flash·pixel + 1)")
        a1.set_title("MTG LI AF 15/07/2025 10-18 UTC (0,2°)")
        hrs = sorted(conteggi)
        for z in ZONE:
            a2.plot(hrs, [conteggi[h][z] for h in hrs], marker="o", label=z)
        a2.set_xlabel("ora UTC"); a2.set_ylabel("flash·pixel / h"); a2.legend(fontsize=8)
        a2.set_title(f"best_hour_utc dominio = {best}")
        fig.tight_layout()
        fig.savefig(PREVIEWS / "s3_li_flashes.png")
        plt.close(fig)

    non_nulli = any(conteggi[h][z] > 0 for h in conteggi for z in ZONE)
    esito = "OK" if n_ok == len(prods) and non_nulli else ("PARZIALE" if n_ok and non_nulli else "KO")
    risposta = (f"{n_ok}/{len(prods)} prodotti da 10 min letti ({round(mb_tot, 1)} MB solo BODY); "
                f"griglia sparsa su geostazionaria FCI 2 km; best_hour_utc {best}:00 "
                f"({round(conteggi[best]['dominio']) if best is not None else 0} flash·pixel nel dominio)")
    write_result(TEST, esito, DOMANDA, risposta,
                 misure={"secondi": T.t, "MB_scaricati": round(mb_tot, 1), "MB_conservati": 0,
                         "n_prodotti": len(prods), "struttura_file": struttura,
                         "conteggi_per_ora_zona": tab},
                 errori=errori, best_hour_utc=best)


if __name__ == "__main__":
    main()
