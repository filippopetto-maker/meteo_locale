"""S10 — CAPE e CIN per l'addestramento: ci sono per tutti gli anni, e come vanno trattati?

1. ERA5 single levels CAPE+CIN del 15/07 alle 00 e 12 UTC per ogni anno 2015–2025
   (una richiesta) sul dominio: presenza per anno, massimi, frazione di CIN definita,
   relazione tra CIN indefinita e CAPE.
2. Evento completo da 12 h (15/07/2019 06–18 UTC) con fonti.era5.fetch_env: tempo e MB reali
   per la stima della Fase 2.
3. Inferenza (Fase 5): ore con CAPE e CIN non nulli nelle prossime 24 h su Roma Sud,
   Open-Meteo Forecast API, per modello.
4. Convenzioni: ERA5 contro GFS storico (Open-Meteo) su Roma Sud alle 12 UTC, 2021–2025.
"""
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DOMAIN_BBOX, PREVIEWS, RAW, write_result
from fonti import era5, openmeteo as om

TEST = "s10_cape_cin"
DOMANDA = ("CAPE e CIN sono disponibili per tutti gli anni di training e test (2015–2025) sul dominio, "
           "e in tempo reale per l'inferenza?")
ANNI = list(range(2015, 2026))
ROMA = (41.73, 12.35)
MODELLI_LIVE = ["ncep_gfs_seamless", "ecmwf_ifs025", "ecmwf_ifs", "icon_seamless", "meteofrance_seamless",
                "italia_meteo_arpae_icon_2i"]


def main():
    errori, sec, mis = [], {}, {}
    lavoro = RAW / "s10"

    # 1. tutti gli anni
    t0 = time.perf_counter()
    ds, peso = era5.richiesta(era5.SINGLE, ["convective_available_potential_energy", "convective_inhibition"],
                              datetime(2015, 7, 15), [0, 12], lavoro, anni=ANNI)
    sec["anni_2015_2025"] = round(time.perf_counter() - t0, 1)
    raw_cin = ds["cin"]
    per_anno = {}
    for t in ds.time.values:
        c, n = ds.cape.sel(time=t), raw_cin.sel(time=t)
        definita = n.notnull()
        per_anno[str(t)[:13]] = {
            "cape_max": round(float(c.max()), 0),
            "cape_frac>500": round(float((c > 500).mean()), 3),
            "cin_frac_definita": round(float(definita.mean()), 3),
            "cin_mediana_definita": round(float(n.where(definita).median()), 1) if definita.any() else None,
            "cin_max": round(float(n.max()), 0) if definita.any() else None,
            # dove CIN non è definita, quanto vale CAPE?
            "cape_max_dove_cin_NaN": round(float(c.where(~definita).max()), 1),
            "cape_frac>0_dove_cin_NaN": round(float((c.where(~definita) > 0).sum() / max(int((~definita).sum()), 1)), 3),
            # dei punti con CAPE > 500 J/kg, quanti hanno CIN mancante?
            "cin_NaN_frac_dove_cape>500": round(float((~definita & (c > 500)).sum() / max(int((c > 500).sum()), 1)), 3),
        }
    anni_ok = sorted({k[:4] for k, v in per_anno.items() if v["cape_max"] is not None})
    mis["per_anno_ora"] = per_anno
    mis["MB_anni"] = round(peso, 2)
    print({k: (v["cape_max"], v["cin_frac_definita"], v["cape_max_dove_cin_NaN"]) for k, v in per_anno.items()})

    # mappa per l'anteprima (2019 12 UTC)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    t_map = np.datetime64("2019-07-15T12:00")
    fig, axs = plt.subplots(1, 3, figsize=(14, 3.4), dpi=70)
    c = ds.cape.sel(time=t_map)
    n = raw_cin.sel(time=t_map)
    c.plot(ax=axs[0], cmap="YlOrRd", vmin=0, vmax=2500, cbar_kwargs={"label": "J/kg"})
    axs[0].set_title("ERA5 CAPE 15/07/2019 12 UTC", fontsize=9)
    n.plot(ax=axs[1], cmap="Blues", vmin=0, vmax=300, cbar_kwargs={"label": "J/kg"})
    axs[1].set_title("ERA5 CIN (bianco = non definita)", fontsize=9)
    n.notnull().astype(int).plot(ax=axs[2], cmap="Greys", vmin=0, vmax=1, add_colorbar=False)
    axs[2].set_title(f"CIN definita: {100 * float(n.notnull().mean()):.0f}% del dominio", fontsize=9)
    fig.tight_layout()
    fig.savefig(PREVIEWS / "s10_cape_cin.png")
    plt.close(fig)

    # 2. evento completo da 12 h
    try:
        t0 = time.perf_counter()
        ev, peso_ev = era5.fetch_env(datetime(2019, 7, 15, 6), datetime(2019, 7, 15, 18), lavoro)
        sec["evento_12h_fetch_env"] = round(time.perf_counter() - t0, 1)
        nc = lavoro / "evento.nc"
        lavoro.mkdir(parents=True, exist_ok=True)
        ev.to_netcdf(nc, encoding={v: {"zlib": True, "complevel": 4} for v in ev.data_vars})
        from common import mb, rm
        mis["evento_12h"] = {"ore": int(ev.sizes["time"]), "variabili": sorted(ev.data_vars),
                             "MB_scaricati": round(peso_ev, 2), "MB_netcdf_compresso": round(mb(nc), 2),
                             "cin_frac_definita_media": round(float(ev.cin_definita.mean()), 3)}
        rm(lavoro)
    except Exception as e:  # noqa: BLE001
        errori.append(f"evento 12h: {e!r}"[:600])

    # 3. disponibilità in tempo reale (Forecast API)
    live = {}
    for m in MODELLI_LIVE:
        try:
            js = om.orario(*ROMA, ["cape", "convective_inhibition"], m, storico=False, forecast_days=1)
            live[m] = om.ore_non_nulle(js, ["cape", "convective_inhibition"])
        except Exception as e:  # noqa: BLE001
            live[m] = {"errore": str(e)[:150]}
    mis["forecast_24h_ore_non_nulle_roma"] = live
    print(live)

    # 4. convenzioni ERA5 vs GFS storico su Roma Sud alle 12 UTC
    confronto = {}
    for a in range(2021, 2026):
        try:
            js = om.orario(*ROMA, ["cape", "convective_inhibition"], "ncep_gfs_seamless", f"{a}-07-15")
            g_cape, g_cin = js["hourly"]["cape"][12], js["hourly"]["convective_inhibition"][12]
            t = np.datetime64(f"{a}-07-15T12:00")
            e_cape = float(ds.cape.sel(time=t).sel(latitude=ROMA[0], longitude=ROMA[1], method="nearest"))
            e_cin = raw_cin.sel(time=t).sel(latitude=ROMA[0], longitude=ROMA[1], method="nearest")
            confronto[str(a)] = {"ERA5_cape": round(e_cape, 1), "GFS_cape": g_cape,
                                 "ERA5_cin": None if np.isnan(float(e_cin)) else round(float(e_cin), 1),
                                 "GFS_cin": g_cin}
        except Exception as e:  # noqa: BLE001
            confronto[str(a)] = {"errore": str(e)[:150]}
    mis["roma_12utc_ERA5_vs_GFS"] = confronto
    print(confronto)

    tutti_anni = anni_ok == [str(a) for a in ANNI]
    def_media = float(np.mean([v["cin_frac_definita"] for v in per_anno.values()]))
    cape_dove_nan = max(v["cape_max_dove_cin_NaN"] for v in per_anno.values())
    live_cin = [m for m, v in live.items() if isinstance(v, dict) and v.get("convective_inhibition", 0) > 0]
    esito = "OK" if tutti_anni and "evento_12h" in mis else ("PARZIALE" if anni_ok else "KO")
    nan_cape = float(np.mean([v["cin_NaN_frac_dove_cape>500"] for v in per_anno.values()]))
    mis["nota_cin_era5"] = ("ERA5 data documentation: 'A missing value is assigned to CIN for values of CIN > 1000 "
                            "or where there is no cloud base'. Trattamento: NaN → 1000 J/kg + maschera cin_definita "
                            "(fonti.era5.prepara_cin).")
    risposta = (f"ERA5: CAPE e CIN presenti in tutti gli anni {ANNI[0]}–{ANNI[-1]} ({len(anni_ok)}/11); "
                f"CIN definita in media sul {100 * def_media:.0f}% del dominio (NaN altrove, CAPE max dove CIN è NaN "
                f"= {cape_dove_nan:.0f} J/kg; CIN mancante nel {100 * nan_cape:.0f}% dei punti con CAPE > 500); evento 12 h: {mis.get('evento_12h', {}).get('MB_netcdf_compresso')} MB "
                f"compressi in {sec.get('evento_12h_fetch_env')} s; CIN in tempo reale da: {', '.join(live_cin) or 'nessuno'}")
    write_result(TEST, esito, DOMANDA, risposta,
                 misure={"secondi": sec, "MB_scaricati": round(peso + mis.get("evento_12h", {}).get("MB_scaricati", 0), 2),
                         "MB_conservati": 0, **mis},
                 errori=errori)


if __name__ == "__main__":
    main()
