"""S2 — FCI L1c FDHSI (EO:EUM:DAT:0662): si può evitare il disco intero?

1. ciclo da 10 min più vicino a best_hour_utc di S3 (fallback 15/07/2025 14:00);
2. elenco delle entry con dimensione (Range di 1 byte, senza scaricare);
3. chunk del dominio dalla geometria della griglia FCI 2 km (fonti.eumetsat.fci_chunk_per_bbox),
   download dei soli BODY, lettura satpy dei canali del ramo immagine (ir_105, wv_63, wv_73),
   riproiezione 0,05°; se il ritaglio ha righe vuote si riprova con un chunk di margine;
4. righe reali di ogni chunk lette dal file (start/end_position_row);
5. TRAIL scaricato una volta solo per misurarne il peso (satpy non lo usa);
6. facoltativo: Data Tailor (FCIL1FDHSI, netcdf4, ROI dominio, radianza ir_105).
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DOMAIN_BBOX, PREVIEWS, RAW, Timer, area_def_regular, mb, read_result, rm, write_result
from fonti import eumetsat as eu

TEST = "s2_fci"
DOMANDA = "Si può evitare di scaricare il disco intero FCI (~950 MB per ciclo)?"


def main():
    try:
        tok = eu.token()
    except RuntimeError as e:
        return write_result(TEST, "SALTATO", DOMANDA, "Credenziale assente", errori=[e])
    T, errori, note = Timer(), [], []
    out = RAW / "s2"
    s3 = read_result("s3_li_flashes") or {}
    best = s3.get("best_hour_utc")
    target = datetime(2025, 7, 15, best if best is not None else 14, 0)
    note.append(f"ora target {target:%H:%M} UTC ({'da S3' if best is not None else 'fallback'})")
    with T("search"):
        prods = eu.cerca(eu.FCI, target - timedelta(minutes=10), target + timedelta(minutes=10), tok)
    p = min(prods, key=lambda x: abs(x.sensing_start - target))
    print("ciclo:", p.sensing_start, p.sensing_end)

    with T("elenco_entry"):
        sizes = eu.dimensioni_entry(p, tok)
    body = {eu.numero_chunk(e)[1]: e for e in sizes if eu.numero_chunk(e)[0] == "BODY"}
    trail = [e for e in sizes if eu.numero_chunk(e)[0] == "TRAIL"]
    tot_mb = sum(v for v in sizes.values() if v) / 1e6
    elenco = {
        "n_entry": len(sizes), "n_body": len(body), "n_trail": len(trail),
        "n_altri": sum(1 for e in sizes if eu.numero_chunk(e)[0] is None),
        "MB_disco_intero_somma_entry": round(tot_mb, 1),
        "MB_dichiarati_prodotto": round((p.size or 0) / 1e3, 1),
        "MB_per_chunk": {k: round(sizes[body[k]] / 1e6, 1) for k in sorted(body)},
        "MB_trail": round(sizes[trail[0]] / 1e6, 2) if trail else None,
    }
    print(elenco["n_entry"], "entry,", elenco["MB_disco_intero_somma_entry"], "MB")

    area = area_def_regular(DOMAIN_BBOX, 0.05)
    ds, m, mb_dl = None, {}, 0.0
    for margine in (0, 1):
        c = eu.fci_chunk_per_bbox(DOMAIN_BBOX, margine)
        try:
            with T("chunk_download_lettura"):
                ds, m = eu.fci_ritaglio(p, area, out, chunks=c)
            mb_dl += m["MB_chunk"]
        except Exception as e:  # noqa: BLE001
            errori.append(f"fci_ritaglio margine {margine}: {e!r}")
            break
        v = ds["ir_105"].values
        vuote = np.isnan(v).all(axis=1)
        note.append(f"margine {margine}: chunk {c}, NaN {np.isnan(v).mean():.4f}, righe vuote {int(vuote.sum())}")
        print(note[-1])
        if not vuote.any():
            break
    if trail:
        with T("trail_solo_misura"):
            ft = eu.scarica(p, out, trail[0])
            mb_dl += mb(ft)
            rm(ft)

    rit = {}
    if ds is not None:
        for cn in eu.CANALI_FCI:
            vv = ds[cn].values
            rit[cn] = {"min_K": round(float(np.nanmin(vv)), 1), "max_K": round(float(np.nanmax(vv)), 1)}
        rit["frac_nan"] = round(float(np.isnan(ds["ir_105"].values).mean()), 4)
        nc = out / f"crop_fci_{p.sensing_start:%Y%m%dT%H%M}.nc"
        ds.to_netcdf(nc, encoding={v: {"zlib": True, "complevel": 4} for v in ds.data_vars})
        rit["MB_crop_3canali_compresso"] = round(mb(nc), 2)
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axs = plt.subplots(1, 3, figsize=(14, 3.2), dpi=70)
        for ax, cn, (lo, hi) in zip(axs, eu.CANALI_FCI, [(200, 310), (200, 260), (200, 280)]):
            ds[cn].plot(ax=ax, cmap="gray_r", vmin=lo, vmax=hi, cbar_kwargs={"label": "K"})
            ax.set_title(f"FCI {cn} {p.sensing_start:%Y-%m-%d %H:%M} — chunk {m['chunk'][0]}–{m['chunk'][-1]}",
                         fontsize=9)
        fig.tight_layout()
        fig.savefig(PREVIEWS / "s2_fci_ir105.png")
        plt.close(fig)

    usati = m.get("chunk", [])
    mb_needed = sum(sizes[body[k]] for k in usati) / 1e6

    with T("data_tailor"):
        try:
            dt = eu.tailor(p, "FCIL1FDHSI", ["ir_105_effective_radiance"], DOMAIN_BBOX, dest=out / "tailor", tok=tok)
            dt["file"] = [f.name for f in dt.get("file", [])]
        except Exception as e:  # noqa: BLE001
            dt = {"stato": "errore", "errore": repr(e)[:800]}
    print("Data Tailor:", dt)
    rm(out)

    ok_crop = ds is not None and rit.get("frac_nan", 1) < 0.01 and 180 < rit["ir_105"]["min_K"] < 260
    esito = "OK" if ok_crop else "KO"
    risposta = (f"Sì: entry scaricabili singolarmente (Product.open(entry=...)). Chunk {usati[0]}–{usati[-1]} "
                f"= {mb_needed:.0f} MB contro {tot_mb:.0f} MB del disco intero ({100 * mb_needed / tot_mb:.0f}%), "
                f"TRAIL non necessario; ritaglio 3 canali, ir_105 {rit['ir_105']['min_K']}–{rit['ir_105']['max_K']} K; "
                f"Data Tailor: {dt.get('stato')}" if ok_crop else "Ritaglio non ottenuto dai soli chunk")
    write_result(TEST, esito, DOMANDA, risposta,
                 misure={"secondi": T.t, "MB_scaricati": round(mb_dl, 1), "MB_conservati": 0,
                         "ciclo": f"{p.sensing_start} – {p.sensing_end}", "prodotto": str(p),
                         "elenco_entry": elenco, "chunk_usati": usati,
                         "righe_reali_chunk_ir105": {str(k): v for k, v in m.get("righe_chunk", {}).items()},
                         "MB_chunk_necessari": round(mb_needed, 1),
                         "sec_download_chunk": m.get("sec_download"), "sec_lettura_ritaglio": m.get("sec_lettura_ritaglio"),
                         "ritaglio": rit, "data_tailor": dt},
                 errori=errori, note=note)


if __name__ == "__main__":
    main()
