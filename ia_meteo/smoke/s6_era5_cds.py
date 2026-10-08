"""S6 — ERA5 da Copernicus CDS (fonti.era5, credenziali in ~/.cdsapirc).

Ambiente completo del ramo "ambiente" del planning per un'ora (15/07/2019 12 UTC) sul
dominio: single levels (CAPE, CIN, K, TT, TCWV, zero termico, PBL, SST, precipitazione
convettiva, vento 10 m) + pressure levels (u, v, T, RH a 850/700/500/300 hPa), via
`fonti.era5.fetch_env`. Tempo in coda ricavato dai log di cdsapi. Limite 30 min.
"""
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import RAW, write_result
from fonti import era5

TEST = "s6_era5_cds"
DOMANDA = "La richiesta CDS funziona e contiene tutte le variabili convettive del ramo ambiente?"
LIMITE_S = 30 * 60
ATTESE = ["cape", "cin", "kx", "totalx", "tcwv", "deg0l", "blh", "sst", "cp", "u10", "v10", "u", "v", "t", "r"]


class LogTimes(logging.Handler):
    def __init__(self):
        super().__init__()
        self.ev = []

    def emit(self, record):
        self.ev.append((time.perf_counter(), record.getMessage()[:120]))


def main():
    if not Path("~/.cdsapirc").expanduser().exists():
        return write_result(TEST, "SALTATO", DOMANDA, "Credenziale assente (~/.cdsapirc)")
    h = LogTimes()
    for name in ("cdsapi", "ecmwf.datastores", "datapi", ""):
        logging.getLogger(name).addHandler(h)
    logging.getLogger().setLevel(logging.INFO)
    box, errori = {}, []
    t0 = time.perf_counter()

    def run():
        try:
            box["ds"], box["MB"] = era5.fetch_env(datetime(2019, 7, 15, 12), datetime(2019, 7, 15, 12), RAW / "s6",
                                                     quiet=False)
        except Exception as e:  # noqa: BLE001
            box["err"] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(LIMITE_S)
    sec = round(time.perf_counter() - t0, 1)
    stati = [(t - t0, m) for t, m in h.ev if "status has been updated" in m]
    accettate = [t for t, m in stati if "accepted" in m]
    running = [t for t, m in stati if "running" in m]
    code = [round(r - a, 1) for a, r in zip(accettate, running)]
    misure = {"secondi": {"totale": sec}, "secondi_in_coda_per_richiesta": code,
              "log_stati": [f"+{t:.0f}s {m}" for t, m in stati]}
    if th.is_alive():
        return write_result(TEST, "PARZIALE", DOMANDA, f"Interrotto dopo {LIMITE_S // 60} min in coda",
                            misure=misure, errori=[f"oltre {LIMITE_S // 60} min"])
    if "err" in box:
        return write_result(TEST, "KO", DOMANDA, "Richiesta fallita", misure=misure, errori=[repr(box["err"])])
    ds = box["ds"]
    presenti = [v for v in ATTESE if v in ds]
    mancanti = [v for v in ATTESE if v not in ds]
    misure |= {"MB_scaricati": round(box["MB"], 3), "MB_conservati": 0,
               "variabili": sorted(ds.data_vars), "dimensioni": {k: int(v) for k, v in ds.sizes.items()},
               "pressure_level": [float(x) for x in ds.pressure_level.values] if "pressure_level" in ds.coords else None,
               "cape_max": round(float(ds.cape.max()), 1),
               "cin_frac_definita": round(float(ds.cin_definita.mean()), 3)}
    esito = "OK" if not mancanti else "PARZIALE"
    risposta = (f"{len(presenti)}/{len(ATTESE)} variabili ({', '.join(presenti)}), griglia "
                f"{ds.sizes.get('latitude')}×{ds.sizes.get('longitude')}, {sec} s totali "
                + (f"(coda {', '.join(map(str, code))} s)" if code else "(nessuna coda: risultato già in cache CDS)")
                + f", {box['MB']:.2f} MB")
    write_result(TEST, esito, DOMANDA, risposta, misure=misure,
                 errori=[f"mancanti: {mancanti}"] if mancanti else [],
                 tcwv_presente="tcwv" in ds)


if __name__ == "__main__":
    main()
