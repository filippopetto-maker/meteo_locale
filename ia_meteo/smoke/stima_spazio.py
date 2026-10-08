"""Stima di spazio e tempi per i test reali (Fasi 1-2), da misure e non da ipotesi.

1. Misura il peso compresso di un frame sulla griglia comune 0,05° per ogni fonte
   (OPERA 2019 e 2025, LI 10 min, IMERG, IT-DPC-SRI Lazio, SEVIRI float32 e int16).
2. Combina con le misure degli smoke test (S1, S2, S10) e con i parametri del planning:
   finestre da 12 h, 150–300 finestre, SEVIRI 2015–2024, FCI solo 2025, Lazio con IT-DPC-SRI.
Scrive results/stima_spazio.json (letto da make_report.py).
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DOMAIN_BBOX, RAW, RESULTS, TEST_BBOXES, area_def_regular, mb, read_result, rm
from fonti import eumetsat as eu, imerg, itdpc, opera

# parametri dal planning (IA meteo — Planning di avvio, 05/10/2026) e scelte esplicite
FINESTRE = (150, 300)          # "obiettivo iniziale 150–300 finestre da 12 ore"
QUOTA_FCI = 0.15               # finestre 2025 (adattamento e test FCI): ipotesi
QUOTA_LAZIO = 0.15             # finestre laziali con IT-DPC-SRI: ipotesi
QUOTA_CIRRUS = 0.10            # finestre SEVIRI dopo 07/2024 con OPERA 1 km/5 min: ipotesi
WORKER = 3                     # download in parallelo
SLOT_SEVIRI, CICLI_FCI, PASSI_RADAR_15, PASSI_RADAR_5 = 48, 72, 48, 144
ORE_ERA5_CATALOGO = 11 * 244 * 24   # Fase 1: aprile–novembre, 2015–2025, orario
CAMPI_CATALOGO = 3                  # cape, cin, cp


def salva(lavoro, arr, lats, lons, scala=None):
    import xarray as xr
    ds = xr.Dataset({"v": (("lat", "lon"), arr.astype("float32"))}, coords={"lat": lats, "lon": lons})
    f = lavoro / "m.nc"
    enc = {"zlib": True, "complevel": 4}
    if scala:
        enc |= {"dtype": "int16", "scale_factor": scala, "add_offset": 0, "_FillValue": -32768}
    ds.to_netcdf(f, encoding={"v": enc})
    out = mb(f)
    rm(f)
    return round(out, 3)


def misura_frame():
    import xarray as xr
    lavoro = RAW / "stima"
    lavoro.mkdir(parents=True, exist_ok=True)
    area = area_def_regular(DOMAIN_BBOX, 0.05)
    lons, lats = area.get_lonlats()
    lats, lons = lats[:, 0], lons[0]
    m = {}
    # SEVIRI: un ritaglio di S1, 3 canali, float32 contro int16 (0,01 K)
    crops = sorted((RAW / "s1").glob("crop_*.nc"))
    if crops:
        with xr.open_dataset(crops[0]) as d:
            f32 = sum(salva(lavoro, d[c].values, lats, lons) for c in eu.CANALI_SEVIRI)
            i16 = sum(salva(lavoro, d[c].values - 250, lats, lons, 0.01) for c in eu.CANALI_SEVIRI)
        m["seviri_3canali_f32"], m["seviri_3canali_i16"] = round(f32, 3), round(i16, 3)
    for t in (datetime(2019, 7, 15, 12), datetime(2025, 7, 15, 14)):
        f, _ = opera.scarica_dbzh(t, lavoro)
        g = opera.su_griglia(opera.leggi_odim(f), area)
        rm(f)
        m[f"opera_{t.year}_f32"] = salva(lavoro, g, lats, lons)
    tok = eu.token()
    p = eu.cerca(eu.LI_AF, datetime(2025, 7, 15, 14), datetime(2025, 7, 15, 14, 10), tok)[0]
    f = eu.scarica(p, lavoro, eu.li_body(p))
    la, lo, w, _ = eu.li_flash(f)
    rm(f)
    m["li_10min_f32"] = salva(lavoro, eu.li_su_griglia(la, lo, w, area), lats, lons)
    imerg.login()
    f = imerg.scarica(imerg.granuli(datetime(2023, 7, 15, 12), datetime(2023, 7, 15, 12, 30))[:1], lavoro)[0]
    sub, _, _ = imerg.ritaglio(f, DOMAIN_BBOX)
    rm(f)
    p1 = lavoro / "im.nc"
    sub.to_dataset(name="p").to_netcdf(p1, encoding={"p": {"zlib": True, "complevel": 4}})
    m["imerg_30min"] = round(mb(p1), 3)
    ds = itdpc.apri()
    s = itdpc.ritaglio(ds, slice("2021-07-15T12:00", "2021-07-15T12:55"), TEST_BBOXES["lazio"]).load()
    s.to_dataset(name="p").to_netcdf(p1, encoding={"p": {"zlib": True, "complevel": 4}})
    m["itdpc_lazio_5min"] = round(mb(p1) / s.sizes["time"], 3)
    rm(lavoro)
    return m


def main():
    fr = misura_frame()
    s1, s2, s10 = read_result("s1_seviri"), read_result("s2_fci"), read_result("s10_cape_cin")
    pr = [p for p in s1["misure"]["prodotti"] if "MB_zip" in p]
    zip_mb = np.mean([p["MB_zip"] for p in pr])
    nat_mb = np.mean([p["MB_nat"] for p in pr])
    sev_dl_s = np.mean([p["sec_download"] for p in pr])
    sev_rd_s = np.median([p["sec_lettura_ritaglio"] for p in pr])
    sev_dt = s1["misure"].get("data_tailor") or {}
    fci = s2["misure"]
    fci_chunk = fci["MB_chunk_necessari"]
    fci_full = fci["elenco_entry"]["MB_disco_intero_somma_entry"]
    fci_s = (fci.get("sec_download_chunk") or 0) + (fci.get("sec_lettura_ritaglio") or 0)
    fci_crop = fci["ritaglio"]["MB_crop_3canali_compresso"]
    era5_ev = s10["misure"]["evento_12h"]["MB_netcdf_compresso"]
    era5_campo_ora = era5_ev / (s10["misure"]["evento_12h"]["ore"] * 27)    # 11 single + 4×4 pressure

    def evento(tipo, int16=False):
        """MB conservati per una finestra da 12 h, compressi."""
        sev = fr["seviri_3canali_i16" if int16 else "seviri_3canali_f32"]
        rap = fr["seviri_3canali_i16"] / fr["seviri_3canali_f32"]
        if tipo == "seviri":
            return SLOT_SEVIRI * sev + PASSI_RADAR_15 * fr["opera_2019_f32"] + 24 * fr["imerg_30min"] + era5_ev
        if tipo == "seviri_cirrus":
            return SLOT_SEVIRI * sev + PASSI_RADAR_5 * fr["opera_2025_f32"] + 24 * fr["imerg_30min"] + era5_ev
        if tipo == "fci":
            return (CICLI_FCI * fci_crop * (rap if int16 else 1) + PASSI_RADAR_5 * fr["opera_2025_f32"]
                    + CICLI_FCI * fr["li_10min_f32"] + 24 * fr["imerg_30min"] + era5_ev)
        if tipo == "lazio_extra":
            return 144 * fr["itdpc_lazio_5min"]

    per_evento = {t: round(evento(t), 1) for t in ("seviri", "seviri_cirrus", "fci", "lazio_extra")}
    per_evento_i16 = {t: round(evento(t, True), 1) for t in ("seviri", "seviri_cirrus", "fci", "lazio_extra")}
    scenari = {}
    for n in FINESTRE:
        n_fci = round(n * QUOTA_FCI)
        n_sev = n - n_fci
        n_cir = round(n_sev * QUOTA_CIRRUS)
        n_laz = round(n * QUOTA_LAZIO)
        cons = lambda pe: (n_sev - n_cir) * pe["seviri"] + n_cir * pe["seviri_cirrus"] + n_fci * pe["fci"] + n_laz * pe["lazio_extra"]
        dl_sev_gb = n_sev * SLOT_SEVIRI * zip_mb / 1000
        dl_fci_gb = n_fci * CICLI_FCI * fci_chunk / 1000
        scenari[str(n)] = {
            "finestre": {"seviri": n_sev, "di_cui_opera_cirrus": n_cir, "fci": n_fci, "lazio": n_laz},
            "GB_conservati_float32": round(cons(per_evento) / 1000, 1),
            "GB_conservati_int16": round(cons(per_evento_i16) / 1000, 1),
            "GB_scaricati_disco_intero_seviri_chunk_fci": round(dl_sev_gb + dl_fci_gb, 0),
            "GB_scaricati_con_data_tailor_seviri": round(n_sev * SLOT_SEVIRI * sev_dt.get("MB_output", 0) / 1000 + dl_fci_gb, 0),
            "ore_download_sequenziali": round((n_sev * SLOT_SEVIRI * (sev_dl_s + sev_rd_s) + n_fci * CICLI_FCI * fci_s) / 3600, 0),
            "ore_download_con_worker": round((n_sev * SLOT_SEVIRI * (sev_dl_s + sev_rd_s) + n_fci * CICLI_FCI * fci_s) / 3600 / WORKER, 0),
        }
    picco_lavoro_gb = WORKER * (zip_mb + nat_mb) / 1000
    s12 = read_result("s12_archivio_catalogo")
    mese = (s12 or {}).get("misure", {}).get("catalogo") or {}
    if mese.get("MB_file"):          # misurato: un mese reale di catalogo (S12)
        catalogo_gb = 11 * 8 * mese["MB_file"] / 1000
        catalogo_ore_cds = round(11 * 8 * mese["secondi_cds"] / 3600, 1)
    else:                            # stima dai campi dell'evento (S10)
        catalogo_gb = ORE_ERA5_CATALOGO * CAMPI_CATALOGO * era5_campo_ora / 1000
        catalogo_ore_cds = None
    out = {
        "parametri": {"finestre": FINESTRE, "quota_fci": QUOTA_FCI, "quota_lazio": QUOTA_LAZIO,
                      "quota_cirrus_su_seviri": QUOTA_CIRRUS, "worker": WORKER,
                      "slot_seviri": SLOT_SEVIRI, "cicli_fci": CICLI_FCI},
        "MB_per_frame_compresso": fr,
        "MB_per_finestra_float32": per_evento, "MB_per_finestra_int16": per_evento_i16,
        "MB_scaricati_per_finestra": {"seviri_disco_intero": round(SLOT_SEVIRI * zip_mb), "seviri_data_tailor":
                                      round(SLOT_SEVIRI * sev_dt.get("MB_output", 0)), "fci_chunk": round(CICLI_FCI * fci_chunk),
                                      "fci_disco_intero": round(CICLI_FCI * fci_full)},
        "scenari": scenari,
        "picco_disco_lavoro_GB": round(picco_lavoro_gb, 2),
        "catalogo_era5_fase1_GB": round(catalogo_gb, 1),
        "catalogo_era5_fase1_ore_cds_in_sequenza": catalogo_ore_cds,
        "ambiente_conda_GB": 1.6,
    }
    (RESULTS / "stima_spazio.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
