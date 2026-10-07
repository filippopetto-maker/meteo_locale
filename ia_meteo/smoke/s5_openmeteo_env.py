"""S5 — Ambiente in quota da Open-Meteo Historical Forecast API.

Tre chiamate su Roma Sud (41.73, 12.35), un giorno ciascuna: per ogni modello e
variabile conta le ore non nulle su 24 e il passo temporale effettivo.
Identificativi modello verificati sulla pagina docs il 07/10/2026: `ecmwf_ifs`,
`ecmwf_ifs025`, `ncep_gfs_seamless` (il brief diceva `gfs_seamless`: provato anche quello).
"""
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import write_result

TEST = "s5_openmeteo_env"
DOMANDA = "Quali modelli danno CAPE, CIN, Lifted Index e vento in quota nello storico, e da quando?"
URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
VARS = ["cape", "convective_inhibition", "lifted_index", "freezing_level_height",
        "wind_speed_500hPa", "wind_direction_500hPa", "temperature_850hPa", "relative_humidity_700hPa",
        "temperature_2m"]  # controllo: dice se il modello ha dati quel giorno
CALLS = [("ecmwf_ifs", "2019-07-15"), ("ecmwf_ifs025", "2024-07-15"),
         ("ncep_gfs_seamless", "2022-07-15"), ("gfs_seamless", "2022-07-15"),
         ("ecmwf_ifs", "2024-07-15")]  # extra: IFS 9 km in anni recenti


def main():
    tabella, errori, secondi, kb = {}, [], {}, 0.0
    for model, day in CALLS:
        key = f"{model} {day}"
        t0 = time.perf_counter()
        try:
            r = requests.get(URL, params=dict(latitude=41.73, longitude=12.35, start_date=day, end_date=day,
                                              hourly=",".join(VARS), models=model, timezone="UTC"), timeout=60)
            secondi[key] = round(time.perf_counter() - t0, 2)
            kb += len(r.content) / 1e3
            js = r.json()
            if r.status_code != 200:
                errori.append(f"{key}: HTTP {r.status_code} {js.get('reason')}")
                tabella[key] = {"errore": js.get("reason")}
                continue
            h = js["hourly"]
            row = {v: sum(x is not None for x in h.get(v, [])) for v in VARS}
            # passo effettivo: distanza minima tra ore con CAPE non nullo
            idx = [i for i, x in enumerate(h.get("cape", [])) if x is not None]
            row["passo_h_cape"] = min((b - a for a, b in zip(idx, idx[1:])), default=None)
            idx = [i for i, x in enumerate(h.get("wind_speed_500hPa", [])) if x is not None]
            row["passo_h_w500"] = min((b - a for a, b in zip(idx, idx[1:])), default=None)
            row["cape_max"] = max((x for x in h.get("cape", []) if x is not None), default=None)
            tabella[key] = row
        except Exception as e:  # noqa: BLE001
            errori.append(f"{key}: {e!r}")
        print(key, tabella.get(key))
    risposte = [k for k, v in tabella.items() if "errore" not in v]
    esito = "OK" if all(f"{m} {d}" in risposte for m, d in CALLS[:3]) else ("PARZIALE" if risposte else "KO")
    sintesi = "; ".join(
        f"{k}: " + ("errore " + str(v["errore"]) if "errore" in v else
                    f"CAPE {v['cape']}/24, CIN {v['convective_inhibition']}/24, LI {v['lifted_index']}/24, "
                    f"W500 {v['wind_speed_500hPa']}/24, T2m(controllo) {v['temperature_2m']}/24, passo {v['passo_h_cape']} h")
        for k, v in tabella.items())
    write_result(TEST, esito, DOMANDA, sintesi,
                 misure={"secondi": secondi, "MB_scaricati": round(kb / 1e3, 3), "MB_conservati": 0,
                         "tabella_ore_non_nulle_su_24": tabella},
                 errori=errori,
                 note_doc=["Pagina docs: IFS HRES 9 km orario dal 2017-01-01; IFS 0.25° 3-orario dal 2024-02-03; "
                           "IFS 0.4° dal 2022-11-07; GFS 0.11° e variabili di pressione GFS 0.25° dal 2021-03-23",
                           "ID modello GFS documentato: ncep_gfs_seamless (non gfs_seamless)"])


if __name__ == "__main__":
    main()
