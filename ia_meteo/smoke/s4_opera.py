"""S4 — Composito radar europeo OPERA (EUMETNET / MeteoGate Open Radar Data).

Lo storico si legge dal bucket S3 pubblico `openradar-archive` (fonti.opera): l'API EDR
MeteoGate serve solo le ultime 24 h (una richiesta di controllo, limite anonimo basso).
- 2019 (ODYSSEY, 2 km, 15 min): `DBZH_QIND`; 2025 (CIRRUS, 1 km, 5 min): `DBZH`.
Copertura per zona = pixel non `nodata` (`undetect` = coperto senza eco = valido).
In più: composito sulla griglia comune 0,05° (massimo per cella), come servirà in Fase 2.
"""
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DOMAIN_BBOX, PREVIEWS, RAW, TEST_BBOXES, area_def_regular, mb, read_result, rm, write_result
from fonti import opera

TEST = "s4_opera"
DOMANDA = ("Il composito OPERA copre davvero Spagna, Balcani e mare, sia nell'era ODYSSEY (2 km) "
           "sia in quella CIRRUS (1 km)?")


def main():
    hh = (read_result("s3_li_flashes") or {}).get("best_hour_utc", 12)
    casi = {"2019 ODYSSEY": datetime(2019, 7, 15, 12), "2025 CIRRUS": datetime(2025, 7, 15, hh)}
    out = RAW / "s4"
    area = area_def_regular(DOMAIN_BBOX, 0.05)
    risultati, errori, mb_tot, secondi = {}, [], 0.0, {}
    try:
        r = requests.get(opera.API, params=dict(datetime="2025-07-15T12:00Z/2025-07-15T12:10Z", f="CoverageJSON",
                                                standard_name="DBZH", format="ODIM"), timeout=60)
        api_note = (f"API EDR per 15/07/2025: HTTP {r.status_code}; X-RateLimit-Limit "
                    f"{r.headers.get('X-RateLimit-Limit')} (remaining {r.headers.get('X-RateLimit-Remaining')})")
    except Exception as e:  # noqa: BLE001
        api_note = f"API EDR: {e!r}"
    print(api_note)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axs = plt.subplots(2, 2, figsize=(11, 8), dpi=70)
    lons, lats = area.get_lonlats()
    for col, (nome, t) in enumerate(casi.items()):
        try:
            t0 = time.perf_counter()
            f, prod = opera.scarica_dbzh(t, out)
            secondi[nome] = round(time.perf_counter() - t0, 2)
            mb_tot += mb(f)
            o = opera.leggi_odim(f)
            cov = opera.copertura(o, TEST_BBOXES)
            t0 = time.perf_counter()
            g = opera.su_griglia(o, area)
            secondi[f"{nome} griglia_0.05"] = round(time.perf_counter() - t0, 2)
            w = o["what"]
            risultati[nome] = {
                "file": opera.chiave(t, prod), "MB": round(mb(f), 2), "prodotto_odim": o["prodotto"],
                "quantita": o["quantita"], "data_ora_odim": f"{o['what_root'].get('date')} {o['what_root'].get('time')}",
                "griglia": f"{o['raw'].shape[1]}×{o['raw'].shape[0]}",
                "risoluzione_m": [float(o["where"]["xscale"]), float(o["where"]["yscale"])],
                "projdef": o["where"]["projdef"],
                "gain_offset_nodata_undetect": [float(w["gain"]), float(w["offset"]), float(w["nodata"]), float(w["undetect"])],
                "copertura_pct": cov,
                "griglia_0.05": {"frac_coperta": round(float(np.isfinite(g).mean()), 3),
                                 "max_dBZ": round(float(np.nanmax(g)), 1),
                                 "frac_eco>=35dBZ": round(float((g >= 35).mean()), 4)},
            }
            # in alto: copertura nativa; in basso: griglia comune
            lon, lat = opera.lonlat(o, 4)
            v = opera.dbz(o)[::4, ::4]
            ax = axs[0, col]
            ax.set_facecolor("0.85")
            ax.pcolormesh(lon, lat, np.where(np.isnan(v), np.nan, 0.0), cmap="Greys", vmin=-1, vmax=1, shading="auto")
            im = ax.pcolormesh(lon, lat, np.where(v > 0, v, np.nan), cmap="turbo", vmin=0, vmax=60, shading="auto")
            for bb in TEST_BBOXES.values():
                ax.add_patch(plt.Rectangle((bb["lon_min"], bb["lat_min"]), bb["lon_max"] - bb["lon_min"],
                                           bb["lat_max"] - bb["lat_min"], fill=False, ec="k", lw=.8))
            ax.set_xlim(-12, 32); ax.set_ylim(28, 52)
            ax.set_title(f"OPERA {nome} {t:%Y-%m-%d %H:%M} nativo", fontsize=9)
            ax = axs[1, col]
            ax.set_facecolor("0.85")
            ax.pcolormesh(lons[0], lats[:, 0], np.where(np.isnan(g), np.nan, 0.0), cmap="Greys", vmin=-1, vmax=1)
            ax.pcolormesh(lons[0], lats[:, 0], np.where(g > 0, g, np.nan), cmap="turbo", vmin=0, vmax=60)
            ax.set_title(f"{nome} su griglia 0,05° (max per cella)", fontsize=9)
            print(nome, {k: v for k, v in risultati[nome].items() if k != "projdef"})
        except Exception as e:  # noqa: BLE001
            errori.append(f"{nome}: {e!r}")
    if risultati:
        fig.colorbar(im, ax=axs, label="dBZ (grigio scuro = coperto senza eco, grigio chiaro = fuori copertura)",
                     shrink=.6)
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
                 note=[api_note, "Storico solo via bucket S3 openradar-archive (anonimo); API EDR MeteoGate = cache 24 h"])


if __name__ == "__main__":
    main()
