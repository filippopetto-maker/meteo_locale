"""S2 — FCI L1c FDHSI (EO:EUM:DAT:0662): si può evitare il disco intero?

1. ciclo da 10 min più vicino a best_hour_utc di S3 (fallback 15/07/2025 14:00);
2. elenco delle entry con dimensione (richiesta Range di 1 byte, senza scaricare);
3. chunk candidati per 30-50°N dalla geometria della griglia FCI 2 km
   (area satpy mtg_fci_fdss_2km, 40 chunk numerati da sud a nord, ~139 righe l'uno);
4. download dei soli chunk candidati (+ TRAIL per misura), lettura satpy fci_l1c_nc,
   ir_105, crop + riproiezione 0,05° su DOMAIN_BBOX; se il ritaglio ha buchi si
   aggiungono i chunk vicini;
5. righe reali di ogni chunk lette dal file (start/end_position_row);
6. facoltativo: Data Tailor (FCIL1FDHSI, netcdf4, ROI dominio, ir_105_effective_radiance) con limite di 15 min.
"""
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DOMAIN_BBOX, PREVIEWS, RAW, Timer, area_def_regular, env, mb, read_result, rm, write_result

TEST = "s2_fci"
DOMANDA = "Si può evitare di scaricare il disco intero FCI (~950 MB per ciclo)?"
COLL = "EO:EUM:DAT:0662"
DT_LIMITE_S = 15 * 60


def entry_sizes(product, token):
    """Dimensione di ogni entry con GET Range: bytes=0-0 (scarica 1 byte)."""
    base = product.metadata["properties"]["links"]["data"][0]["href"]
    h = {"Authorization": f"Bearer {token.access_token}", "Range": "bytes=0-0"}
    out = {}
    for e in product.entries:
        r = requests.get(f"{base}/entry", params={"name": e}, headers=h, timeout=60, stream=True)
        cr = r.headers.get("Content-Range", "")
        out[e] = int(cr.split("/")[-1]) if "/" in cr else (int(r.headers.get("Content-Length", 0)) if r.status_code == 200 else None)
        out[e + "#status"] = r.status_code
        r.close()
    return {k: v for k, v in out.items() if not k.endswith("#status")}, {k: v for k, v in out.items() if k.endswith("#status")}


def chunk_no(name):
    m = re.search(r"CHK-(BODY|TRAIL).*_(\d{4})\.nc$", name)
    return (m.group(1), int(m.group(2))) if m else (None, None)


def download(product, entry, path):
    with product.open(entry=entry) as src, open(path, "wb") as dst:
        while c := src.read(1 << 20):
            dst.write(c)


def candidate_chunks():
    from satpy.area import get_area_def
    a = get_area_def("mtg_fci_fdss_2km")
    nrow = a.shape[0]
    rows = []
    for lat in (DOMAIN_BBOX["lat_min"], DOMAIN_BBOX["lat_max"]):
        for lon in np.linspace(DOMAIN_BBOX["lon_min"], DOMAIN_BBOX["lon_max"], 9):
            _, r = a.get_array_indices_from_lonlat(lon, lat)
            rows.append(nrow - int(r))                         # righe contate da sud
    per = nrow / 40
    return list(range(int(np.ceil(min(rows) / per)), int(np.ceil(max(rows) / per)) + 1))


def leggi_ritaglio(files, area):
    from satpy import Scene
    scn = Scene(reader="fci_l1c_nc", filenames=[str(f) for f in files])
    scn.load(["ir_105"])
    c = scn.crop(ll_bbox=(DOMAIN_BBOX["lon_min"], DOMAIN_BBOX["lat_min"], DOMAIN_BBOX["lon_max"], DOMAIN_BBOX["lat_max"]))
    loc = c.resample(area, resampler="nearest", radius_of_influence=10000)
    return loc["ir_105"].compute()


def righe_chunk(f):
    import netCDF4
    with netCDF4.Dataset(f) as d:
        g = d["data/ir_105/measured"]
        return int(g["start_position_row"][...]), int(g["end_position_row"][...])


def prova_data_tailor(token, product):
    """Ritaglio lato server via Data Tailor. Restituisce dict con esito e tempi."""
    import eumdac
    from eumdac.tailor_models import Chain, RegionOfInterest  # noqa: F401
    dt = eumdac.DataTailor(token)
    # FCIL1FDHSI_NATIVE + netcdf4 → "no suitable back-end has been found" (provato 08/10/2026)
    chain = Chain(product="FCIL1FDHSI", format="netcdf4", filter={"bands": ["ir_105_effective_radiance"]},
                  roi={"NSWE": [DOMAIN_BBOX["lat_max"], DOMAIN_BBOX["lat_min"],
                                DOMAIN_BBOX["lon_min"], DOMAIN_BBOX["lon_max"]]})
    t0 = time.perf_counter()
    cust = dt.new_customisation(product, chain)
    stato = None
    while time.perf_counter() - t0 < DT_LIMITE_S:
        stato = cust.status
        if stato in ("DONE", "FAILED", "KILLED", "INACTIVE"):
            break
        time.sleep(10)
    res = {"stato": stato, "secondi": round(time.perf_counter() - t0, 1)}
    try:
        if stato == "DONE":
            out = RAW / "s2" / "tailor"
            out.mkdir(parents=True, exist_ok=True)
            tot = 0
            for o in cust.outputs:
                p = out / Path(o).name
                with cust.stream_output(o) as src, open(p, "wb") as dst:
                    while c := src.read(1 << 20):
                        dst.write(c)
                tot += mb(p)
            res["MB_output"] = round(tot, 2)
            res["file"] = [Path(o).name for o in cust.outputs]
            rm(out)
        else:
            res["log"] = cust.logfile[-1500:] if hasattr(cust, "logfile") else None
    finally:
        try:
            if stato not in ("DONE", "FAILED", "KILLED", "INACTIVE"):
                cust.kill()
            cust.delete()
        except Exception:  # noqa: BLE001
            pass
    return res


def main():
    key, secret = env("EUMETSAT_CONSUMER_KEY"), env("EUMETSAT_CONSUMER_SECRET")
    if not (key and secret):
        return write_result(TEST, "SALTATO", DOMANDA, "Credenziale assente")
    import eumdac
    import xarray as xr

    T, errori, note = Timer(), [], []
    out = RAW / "s2"
    out.mkdir(exist_ok=True)
    s3 = read_result("s3_li_flashes") or {}
    best = s3.get("best_hour_utc")
    target = datetime(2025, 7, 15, best if best is not None else 14, 0)
    note.append(f"ora target {target:%H:%M} UTC ({'da S3' if best is not None else 'fallback'})")

    token = eumdac.AccessToken((key, secret))
    with T("search"):
        prods = list(eumdac.DataStore(token).get_collection(COLL).search(
            dtstart=target - timedelta(minutes=10), dtend=target + timedelta(minutes=10)))
    p = min(prods, key=lambda x: abs(x.sensing_start - target))
    print("ciclo:", p.sensing_start, p.sensing_end)

    with T("elenco_entry"):
        sizes, status = entry_sizes(p, token)
    body = {chunk_no(e)[1]: e for e in sizes if chunk_no(e)[0] == "BODY"}
    trail = [e for e in sizes if chunk_no(e)[0] == "TRAIL"]
    tot_mb = sum(v for v in sizes.values() if v) / 1e6
    elenco = {
        "n_entry": len(sizes), "n_body": len(body), "n_trail": len(trail),
        "altri": sorted(re.sub(r"_C_EUMT.*", "", e.split("/")[-1])[-40:] for e in sizes
                        if chunk_no(e)[0] is None),
        "MB_disco_intero_somma_entry": round(tot_mb, 1),
        "MB_dichiarati_prodotto": round((p.size or 0) / 1e3, 1),
        "MB_per_chunk": {k: round(sizes[body[k]] / 1e6, 1) for k in sorted(body)},
        "MB_trail": round(sizes[trail[0]] / 1e6, 2) if trail else None,
        "status_http_range": sorted(set(status.values())),
    }
    print(elenco["n_entry"], "entry,", elenco["MB_disco_intero_somma_entry"], "MB")

    cand = candidate_chunks()
    note.append(f"chunk candidati da geometria: {cand}")
    area = area_def_regular(DOMAIN_BBOX, 0.05)
    scaricati, righe = {}, {}
    mb_dl = 0.0
    da = None
    tent = 0
    while tent < 4:
        tent += 1
        for k in cand:
            if k in scaricati or k not in body:
                continue
            f = out / body[k]
            with T("download_chunk"):
                download(p, body[k], f)
            scaricati[k] = f
            mb_dl += mb(f)
            righe[k] = righe_chunk(f)
        if trail and "trail" not in scaricati:
            f = out / trail[0]
            with T("download_chunk"):
                download(p, trail[0], f)
            scaricati["trail"] = f
            mb_dl += mb(f)
        with T("lettura_ritaglio"):
            try:
                da = leggi_ritaglio([v for k, v in scaricati.items() if k != "trail"], area)
            except Exception as e:  # noqa: BLE001
                errori.append(f"satpy: {e!r}")
                break
        v = da.values
        nan_rows = np.isnan(v).all(axis=1)
        frac_nan = float(np.isnan(v).mean())
        print(f"tentativo {tent}: chunk {sorted(k for k in scaricati if k != 'trail')}, frac NaN {frac_nan:.4f}")
        if frac_nan < 0.001:
            break
        lats = area.get_lonlats()[1][:, 0]
        nuovi = []
        if nan_rows[lats > (DOMAIN_BBOX["lat_max"] - 1)].any():
            nuovi.append(max(k for k in scaricati if k != "trail") + 1)
        if nan_rows[lats < (DOMAIN_BBOX["lat_min"] + 1)].any():
            nuovi.append(min(k for k in scaricati if k != "trail") - 1)
        if not nuovi:
            break
        cand = cand + nuovi

    misure_ritaglio = {}
    if da is not None:
        v = da.values
        misure_ritaglio = {"Tb_min_K": round(float(np.nanmin(v)), 1), "Tb_max_K": round(float(np.nanmax(v)), 1),
                           "frac_nan": round(float(np.isnan(v).mean()), 4)}
        lons, lats = area.get_lonlats()
        ds = xr.Dataset({"ir_105": (("lat", "lon"), v.astype("float32"))},
                        coords={"lat": lats[:, 0], "lon": lons[0, :]})
        nc = out / f"crop_fci_{p.sensing_start:%Y%m%dT%H%M}.nc"
        ds.to_netcdf(nc)
        misure_ritaglio["MB_crop"] = round(mb(nc), 2)
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8, 4.2), dpi=90)
        ds.ir_105.plot(ax=ax, cmap="gray_r", vmin=200, vmax=310, cbar_kwargs={"label": "Tb IR 10.5 µm (K)"})
        ax.set_title(f"FCI ir_105 {p.sensing_start:%Y-%m-%d %H:%M} UTC — solo chunk "
                     f"{min(k for k in scaricati if k != 'trail')}–{max(k for k in scaricati if k != 'trail')}")
        fig.tight_layout()
        fig.savefig(PREVIEWS / "s2_fci_ir105.png")
        plt.close(fig)

    usati = sorted(k for k in scaricati if k != "trail")
    mb_needed = sum(sizes[body[k]] for k in usati) / 1e6 + (sizes[trail[0]] / 1e6 if trail else 0)

    dt_res = None
    with T("data_tailor"):
        try:
            dt_res = prova_data_tailor(token, p)
        except Exception as e:  # noqa: BLE001
            dt_res = {"stato": "errore", "errore": repr(e)[:800]}
    print("Data Tailor:", dt_res)

    rm(out)
    ok_crop = da is not None and misure_ritaglio.get("frac_nan", 1) < 0.01 and 180 < misure_ritaglio["Tb_min_K"] < 260
    esito = "OK" if ok_crop else "KO"
    risposta = (f"Sì: entry scaricabili singolarmente con eumdac (Product.open(entry=...)). "
                f"Chunk {usati[0]}–{usati[-1]} + TRAIL = {mb_needed:.0f} MB contro {tot_mb:.0f} MB del disco intero "
                f"({100 * mb_needed / tot_mb:.0f}%); ritaglio ir_105 Tb {misure_ritaglio.get('Tb_min_K')}–"
                f"{misure_ritaglio.get('Tb_max_K')} K; Data Tailor: {dt_res.get('stato') if dt_res else 'n/d'}"
                if ok_crop else "Ritaglio non ottenuto dai soli chunk")
    write_result(TEST, esito, DOMANDA, risposta,
                 misure={"secondi": T.t, "MB_scaricati": round(mb_dl, 1), "MB_conservati": 0,
                         "ciclo": f"{p.sensing_start} – {p.sensing_end}", "prodotto": str(p),
                         "elenco_entry": elenco, "chunk_usati": usati,
                         "righe_reali_chunk_ir105": {str(k): righe[k] for k in sorted(righe)},
                         "MB_chunk_necessari_con_trail": round(mb_needed, 1),
                         "ritaglio": misure_ritaglio, "data_tailor": dt_res},
                 errori=errori, note=note)


if __name__ == "__main__":
    main()
