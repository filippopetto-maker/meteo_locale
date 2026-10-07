"""S6 — ERA5 da Copernicus CDS (cdsapi, credenziali in ~/.cdsapirc).

Due richieste da un'ora sola (15/07/2019 12 UTC) su area [50, -10, 30, 30], netCDF.
Nomi variabili verificati sul form.json del CDS il 07/10/2026 (tutti identici al brief).
Tempo in coda ricavato dai log di cdsapi (accepted → running → successful).
Limite: 30 minuti per richiesta, poi PARZIALE.
"""
import logging
import sys
import threading
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import RAW, mb, rm, write_result

TEST = "s6_era5_cds"
DOMANDA = "La richiesta CDS funziona e contiene tutte le variabili convettive del ramo ambiente?"
LIMITE_S = 30 * 60
BASE = dict(product_type=["reanalysis"], year=["2019"], month=["07"], day=["15"], time=["12:00"],
            area=[50, -10, 30, 30], data_format="netcdf", download_format="unarchived")
RICHIESTE = {
    "reanalysis-era5-single-levels": dict(variable=[
        "convective_available_potential_energy", "convective_inhibition", "k_index", "total_totals_index",
        "total_column_water_vapour", "zero_degree_level", "boundary_layer_height",
        "sea_surface_temperature", "convective_precipitation"]),
    "reanalysis-era5-pressure-levels": dict(variable=["u_component_of_wind", "v_component_of_wind",
                                                      "temperature", "relative_humidity"],
                                            pressure_level=["850", "700", "500", "300"]),
}


class LogTimes(logging.Handler):
    def __init__(self):
        super().__init__()
        self.ev = []

    def emit(self, record):
        self.ev.append((time.perf_counter(), record.getMessage()[:200]))


def main():
    if not Path("~/.cdsapirc").expanduser().exists():
        return write_result(TEST, "SALTATO", DOMANDA, "Credenziale assente (~/.cdsapirc)")
    import cdsapi
    import xarray as xr

    out = RAW / "s6"
    out.mkdir(exist_ok=True)
    risultati, errori, mb_tot, completate = {}, [], 0.0, 0
    for ds, extra in RICHIESTE.items():
        h = LogTimes()
        for name in ("cdsapi", "ecmwf.datastores", "datapi", ""):
            logging.getLogger(name).addHandler(h)
        logging.getLogger().setLevel(logging.INFO)
        target = out / f"{ds}.nc"
        res, box = {}, {}

        def run():
            try:
                cdsapi.Client(quiet=False, progress=False).retrieve(ds, BASE | extra, str(target))
            except Exception as e:  # noqa: BLE001
                box["err"] = e

        t0 = time.perf_counter()
        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(LIMITE_S)
        res["secondi_totali"] = round(time.perf_counter() - t0, 1)
        t_run = next((t for t, m in h.ev if "running" in m.lower()), None)
        t_ok = next((t for t, m in h.ev if "successful" in m.lower() or "completed" in m.lower()), None)
        res["secondi_in_coda"] = round(t_run - t0, 1) if t_run else None
        res["secondi_elaborazione"] = round(t_ok - t_run, 1) if (t_ok and t_run) else None
        res["log_stati"] = [f"+{t - t0:.0f}s {m}" for t, m in h.ev][:15]
        for name in ("cdsapi", "ecmwf.datastores", "datapi", ""):
            logging.getLogger(name).removeHandler(h)
        if th.is_alive():
            res["stato"] = f"interrotta dopo {LIMITE_S // 60} min in coda/elaborazione"
            errori.append(f"{ds}: oltre {LIMITE_S // 60} min")
        elif "err" in box:
            res["stato"] = "errore"
            errori.append(f"{ds}: {box['err']!r}")
        else:
            res["MB"] = round(mb(target), 3)
            mb_tot += mb(target)
            files = [target]
            if zipfile.is_zipfile(target):          # più stepType → zip di netCDF
                with zipfile.ZipFile(target) as z:
                    z.extractall(out / ds)
                files = sorted((out / ds).glob("*.nc"))
                res["nota"] = f"risposta zip con {len(files)} netCDF nonostante download_format=unarchived"
            vars_, dims = [], {}
            for f in files:
                d = xr.open_dataset(f)
                vars_ += [v for v in d.data_vars if v not in ("expver", "number")]
                dims |= {k: int(v) for k, v in d.sizes.items()}
                if "pressure_level" in d.coords:
                    res["pressure_level"] = [float(x) for x in d.pressure_level.values]
                d.close()
            res["variabili"] = vars_
            res["dimensioni"] = dims
            res["n_variabili_attese"] = len(extra["variable"])
            res["stato"] = "completata"
            completate += 1
        risultati[ds] = res
        print(ds, res)
    rm(out)

    sl = risultati.get("reanalysis-era5-single-levels", {})
    tutte = completate == 2 and all(r.get("n_variabili_attese") == len(r.get("variabili", [])) for r in risultati.values())
    esito = "OK" if tutte else ("PARZIALE" if completate or any("interrotta" in r.get("stato", "") for r in risultati.values()) else "KO")
    risposta = "; ".join(f"{k.split('-')[-2]}-levels: {v.get('stato')}, {len(v.get('variabili', []))}/{v.get('n_variabili_attese', '?')} var, "
                         f"griglia {v.get('dimensioni', {}).get('latitude')}×{v.get('dimensioni', {}).get('longitude')}, "
                         f"{v.get('secondi_totali')} s" for k, v in risultati.items())
    write_result(TEST, esito, DOMANDA, risposta,
                 misure={"secondi": {k: v.get("secondi_totali") for k, v in risultati.items()},
                         "MB_scaricati": round(mb_tot, 3), "MB_conservati": 0, "richieste": risultati},
                 errori=errori,
                 tcwv_presente=any(v in sl.get("variabili", []) for v in ("tcwv", "total_column_water_vapour")))


if __name__ == "__main__":
    main()
