"""S1 — SEVIRI storico (EUMETSAT, EO:EUM:DAT:MSG:HRSEVIRI).

Scarica 4 slot da 15 min del 15/07/2019 12-13 UTC, legge i canali del ramo immagine
(IR_108, WV_062, WV_073) con satpy, riproietta su DOMAIN_BBOX a 0,05° e salva i ritagli
(netCDF compresso) in raw/s1 per S9; PNG in previews/. Zip e .nat cancellati slot per slot.
Facoltativo: lo stesso slot via Data Tailor (HRSEVIRI, canali 5/6/9, ROI dominio).
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DOMAIN_BBOX, PREVIEWS, RAW, Timer, area_def_regular, mb, rm, write_result
from fonti import eumetsat as eu

TEST = "s1_seviri"
DOMANDA = ("L'archivio SEVIRI si scarica con le nostre chiavi, quanto pesa un disco, "
           "e satpy lo legge e ritaglia sul dominio?")


def main():
    import numpy as np
    try:
        tok = eu.token()
    except RuntimeError as e:
        return write_result(TEST, "SALTATO", DOMANDA, "Credenziale assente", errori=[e])
    T, errori, prodotti = Timer(), [], []
    out_dir = RAW / "s1"
    out_dir.mkdir(exist_ok=True)
    area = area_def_regular(DOMAIN_BBOX, 0.05)
    with T("search"):
        found = eu.cerca(eu.SEVIRI, datetime(2019, 7, 15, 12), datetime(2019, 7, 15, 13), tok)
    print(f"trovati {len(found)} prodotti")
    for p in found[:4]:
        rec = {"id": str(p), "sensing_start": str(p.sensing_start), "size_dichiarata_MB": round((p.size or 0) / 1e3, 1)}
        try:
            nc = out_dir / f"crop_{p.sensing_start:%Y%m%dT%H%M}.nc"
            ds, m = eu.seviri_ritaglio(p, area, out_dir, out=nc)
            rec |= m
            for c in eu.CANALI_SEVIRI:
                rec[f"{c}_min_max_K"] = [round(float(np.nanmin(ds[c])), 1), round(float(np.nanmax(ds[c])), 1)]
            rec["frac_nan"] = round(float(np.isnan(ds["IR_108"].values).mean()), 4)
            rec["crop"], rec["MB_crop"] = nc.name, round(mb(nc), 2)
        except Exception as e:  # noqa: BLE001
            errori.append(f"{p}: {e!r}" + (" [HTTP 429]" if "429" in repr(e) else ""))
        prodotti.append(rec)
        print(rec)

    # Data Tailor sullo stesso primo slot: 3 canali, ROI dominio
    dt = None
    if found:
        with T("data_tailor"):
            try:
                dt = eu.tailor(found[0], "HRSEVIRI", list(eu.DT_CANALI_SEVIRI.values()), DOMAIN_BBOX,
                               dest=out_dir / "tailor", tok=tok)
                if dt.get("file"):
                    import xarray as xr
                    with xr.open_dataset(dt["file"][0]) as d:
                        dt["variabili"] = list(d.data_vars)[:12]
                        dt["dims"] = dict(d.sizes)
                    dt["file"] = [f.name for f in dt["file"]]
                rm(out_dir / "tailor")
            except Exception as e:  # noqa: BLE001
                dt = {"stato": "errore", "errore": repr(e)[:600]}
        print("Data Tailor:", dt)

    ok = [r for r in prodotti if "crop" in r]
    if ok:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import xarray as xr
        last = xr.open_dataset(out_dir / ok[-1]["crop"])
        fig, axs = plt.subplots(1, 3, figsize=(14, 3.2), dpi=70)
        for ax, c, (lo, hi) in zip(axs, eu.CANALI_SEVIRI, [(200, 310), (200, 260), (200, 280)]):
            last[c].plot(ax=ax, cmap="gray_r", vmin=lo, vmax=hi, cbar_kwargs={"label": "K"})
            ax.set_title(f"{c} {last.attrs['start_time'][:16]}", fontsize=9)
        fig.tight_layout()
        fig.savefig(PREVIEWS / "s1_seviri_ir108.png")
        plt.close(fig)
        last.close()

    tutti = len(ok) == 4 and all(180 < r["IR_108_min_max_K"][0] < 260 and 290 < r["IR_108_min_max_K"][1] < 340 for r in ok)
    esito = "OK" if tutti else ("PARZIALE" if ok else "KO")
    media = lambda k: round(sum(r[k] for r in ok) / len(ok), 1) if ok else None
    risposta = (f"{len(found)} prodotti in 1 h; {len(ok)}/4 scaricati e letti; zip medio {media('MB_zip')} MB, "
                f".nat {media('MB_nat')} MB, download medio {media('sec_download')} s; IR_108 "
                f"{min((r['IR_108_min_max_K'][0] for r in ok), default=None)}–"
                f"{max((r['IR_108_min_max_K'][1] for r in ok), default=None)} K; ritaglio 3 canali 0,05° "
                f"compresso {ok[0]['MB_crop'] if ok else None} MB; Data Tailor: {dt.get('stato') if dt else 'n/d'}"
                + (f" ({dt.get('secondi')} s, {dt.get('MB_output')} MB)" if dt and dt.get('stato') == 'DONE' else ""))
    write_result(TEST, esito, DOMANDA, risposta,
                 misure={"secondi": T.t | {"download_tot": round(sum(r.get('sec_download', 0) for r in prodotti), 1)},
                         "MB_scaricati": round(sum(r.get("MB_zip", 0) for r in prodotti), 1),
                         "MB_conservati": round(sum(r.get("MB_crop", 0) for r in ok), 2),
                         "prodotti": prodotti, "canali": eu.CANALI_SEVIRI, "data_tailor": dt,
                         "http": ["nessun 429 osservato"] if not any("429" in e for e in errori) else ["HTTP 429"]},
                 errori=errori)


if __name__ == "__main__":
    main()
