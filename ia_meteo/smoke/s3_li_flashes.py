"""S3 — Fulmini MTG LI Accumulated Flashes (EO:EUM:DAT:0686).

Scarica i prodotti AF del 15/07/2025 10-18 UTC (uno ogni 10 min, solo l'entry BODY),
geolocalizza i pixel attivi (fonti.eumetsat.li_flash, convenzione satpy li_l2_nc),
somma `flash_accumulation` per ora e zona e porta i flash sulla griglia comune 0,05°.
Scrive `best_hour_utc` per S2/S4.

`flash_accumulation` è "per area accumulation of flashes" (flash per pixel):
la somma su un'area conta pixel×flash, non flash distinti.
"""
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DOMAIN_BBOX, PREVIEWS, RAW, TEST_BBOXES, Timer, area_def_regular, bbox_mask, mb, rm, write_result
from fonti import eumetsat as eu

TEST = "s3_li_flashes"
DOMANDA = "I file LI si leggono, e possiamo usarli per trovare automaticamente le ore convettive?"
ZONE = ["spagna", "italia", "balcani"]


def main():
    import numpy as np
    import xarray as xr
    try:
        tok = eu.token()
    except RuntimeError as e:
        return write_result(TEST, "SALTATO", DOMANDA, "Credenziale assente", errori=[e])
    T, errori = Timer(), []
    out = RAW / "s3"
    area = area_def_regular(DOMAIN_BBOX, 0.05)
    with T("search"):
        prods = eu.cerca(eu.LI_AF, datetime(2025, 7, 15, 10), datetime(2025, 7, 15, 18), tok)
    print(f"{len(prods)} prodotti")

    conteggi = defaultdict(lambda: defaultdict(float))
    griglia = np.zeros(area.shape, "float32")
    mb_tot, struttura, n_ok = 0.0, None, 0
    for p in prods:
        try:
            with T("download"):
                f = eu.scarica(p, out, eu.li_body(p))
            mb_tot += mb(f)
            with T("lettura"):
                if struttura is None:
                    with xr.open_dataset(f) as d:
                        struttura = {
                            "variabili": list(d.data_vars), "dimensioni": dict(d.sizes),
                            "griglia": "sparsa (pixels) su griglia geostazionaria FCI 5568×5568 (area satpy "
                                       "mtg_fci_fdss_2km), ≈2 km al nadir, ~3-4 km a 40-45°N",
                            "accumulazioni_per_file": int(d.sizes["accumulations"]),
                            "durata_file_s": d.attrs.get("time_coverage_duration"),
                            "unita": d.flash_accumulation.attrs.get("units")}
                lat, lon, w, _ = eu.li_flash(f)
                hr = p.sensing_start.hour
                conteggi[hr]["dominio"] += float(w[bbox_mask(lat, lon, DOMAIN_BBOX)].sum())
                for z in ZONE:
                    conteggi[hr][z] += float(w[bbox_mask(lat, lon, TEST_BBOXES[z])].sum())
            with T("griglia_0.05"):
                griglia += eu.li_su_griglia(lat, lon, w, area)
            n_ok += 1
            rm(f)
        except Exception as e:  # noqa: BLE001
            errori.append(f"{p}: {e!r}")
    tab = {f"{h:02d}": {k: round(v, 1) for k, v in conteggi[h].items()} for h in sorted(conteggi)}
    best = max(conteggi, key=lambda h: conteggi[h]["dominio"]) if conteggi else None
    for h, r in tab.items():
        print(h, r)
    # controllo di coerenza: la griglia deve conservare il totale nel dominio
    tot_dom = sum(conteggi[h]["dominio"] for h in conteggi)
    coerenza = round(float(griglia.sum()) / tot_dom, 4) if tot_dom else None

    if n_ok:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        lons, lats = area.get_lonlats()
        fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 3.8), dpi=80, gridspec_kw={"width_ratios": [2, 1]})
        im = a1.pcolormesh(lons[0], lats[:, 0], np.log10(griglia + 1), cmap="magma_r")
        for z in ZONE:
            b = TEST_BBOXES[z]
            a1.add_patch(plt.Rectangle((b["lon_min"], b["lat_min"]), b["lon_max"] - b["lon_min"],
                                       b["lat_max"] - b["lat_min"], fill=False, ec="tab:blue", lw=1))
            a1.text(b["lon_min"] + .2, b["lat_max"] - .8, z, color="tab:blue", fontsize=8)
        fig.colorbar(im, ax=a1, label="log10(flash·pixel + 1)")
        a1.set_title("MTG LI AF 15/07/2025 10-18 UTC, griglia comune 0,05°")
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
                f"({round(conteggi[best]['dominio']) if best is not None else 0} flash·pixel nel dominio)"
                + (f"; griglia 0,05° conserva il {100 * coerenza:.2f}% del totale" if coerenza else ""))
    write_result(TEST, esito, DOMANDA, risposta,
                 misure={"secondi": T.t, "MB_scaricati": round(mb_tot, 1), "MB_conservati": 0,
                         "n_prodotti": len(prods), "struttura_file": struttura,
                         "conteggi_per_ora_zona": tab, "rapporto_griglia_su_totale": coerenza},
                 errori=errori, best_hour_utc=best)


if __name__ == "__main__":
    main()
