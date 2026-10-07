"""S9 — Catena minima sul Mac: 4 frame IR SEVIRI → campo di moto → estrapolazione.

Usa i 4 ritagli di S1 (raw/s1/crop_*.nc), li riduce al box `tirreno`, trasforma
Tb in un campo "nubi fredde alte" = max(0, 260 − Tb) (solo per questo test),
stima il moto con pysteps LK, estrapola a +30 e +60 min (semilagrangiano).
Picco RAM: lo script si rilancia sotto `/usr/bin/time -l` (macOS) e legge
"maximum resident set size". Confronto facoltativo: vento medio 850–500 hPa ERA5
sulla stessa area e ora (richiesta CDS minima), solo da riportare.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import PREVIEWS, RAW, RESULTS, TEST_BBOXES, mb, read_result, rm, write_result

TEST = "s9_toolchain"
DOMANDA = "La catena '4 frame IR → campo di moto → estrapolazione' gira sul Mac da 8 GB, e in quanto tempo?"
PASSO_MIN = 15
BB = TEST_BBOXES["tirreno"]


def direzione(u, v):
    """Direzione meteorologica di provenienza (gradi) da componenti verso est/nord."""
    return float((np.degrees(np.arctan2(-u, -v)) + 360) % 360)


def era5_vento():
    import cdsapi
    import xarray as xr
    out = RAW / "s9_era5.nc"
    req = dict(product_type=["reanalysis"], year=["2019"], month=["07"], day=["15"], time=["12:00"],
               pressure_level=["850", "700", "500"], variable=["u_component_of_wind", "v_component_of_wind"],
               area=[BB["lat_max"], BB["lon_min"], BB["lat_min"], BB["lon_max"]],
               data_format="netcdf", download_format="unarchived")
    t0 = time.perf_counter()
    cdsapi.Client(quiet=True, progress=False).retrieve("reanalysis-era5-pressure-levels", req, str(out))
    sec = round(time.perf_counter() - t0, 1)
    d = xr.open_dataset(out)
    res = {"secondi": sec}
    for lev in (850, 700, 500):
        u = float(d.u.sel(pressure_level=lev).mean()) * 3.6
        v = float(d.v.sel(pressure_level=lev).mean()) * 3.6
        res[f"{lev}hPa"] = {"km_h": round(np.hypot(u, v), 1), "da_gradi": round(direzione(u, v))}
    u = float(d.u.mean()) * 3.6
    v = float(d.v.mean()) * 3.6
    res["media_850_500"] = {"km_h": round(np.hypot(u, v), 1), "da_gradi": round(direzione(u, v))}
    d.close()
    rm(out)
    return res


def catena():
    import xarray as xr
    from pysteps import extrapolation, motion

    T = {}
    files = sorted((RAW / "s1").glob("crop_*.nc"))
    if len(files) < 4:
        raise RuntimeError(f"servono 4 ritagli S1, trovati {len(files)}")
    t0 = time.perf_counter()
    frames, tempi = [], []
    for f in files[:4]:
        d = xr.open_dataset(f)
        sub = d.IR_108.sel(lat=slice(BB["lat_max"], BB["lat_min"]) if d.lat[0] > d.lat[-1] else slice(BB["lat_min"], BB["lat_max"]),
                           lon=slice(BB["lon_min"], BB["lon_max"]))
        frames.append(np.maximum(0, 260 - sub.values))
        tempi.append(d.attrs["start_time"][:16])
        lat, lon = sub.lat.values, sub.lon.values
        tb_last = sub.values
        d.close()
    R = np.stack(frames).astype("float64")
    T["caricamento"] = round(time.perf_counter() - t0, 2)

    t0 = time.perf_counter()
    V = motion.get_method("LK")(R)                       # pixel per passo (15 min), shape (2, ny, nx)
    T["moto_LK"] = round(time.perf_counter() - t0, 2)
    t0 = time.perf_counter()
    fc = extrapolation.get_method("semilagrangian")(R[-1], V, [2, 4])   # +30, +60 min
    T["estrapolazione"] = round(time.perf_counter() - t0, 2)

    lat_c = float(np.mean(lat))
    dx = 0.05 * 111.32 * np.cos(np.radians(lat_c))     # km per pixel in x
    dy = 0.05 * 110.57                                   # km per pixel in y
    sign_y = -1 if lat[0] > lat[-1] else 1               # righe verso sud se lat decrescente
    u = V[0] * dx * (60 / PASSO_MIN)                     # km/h verso est
    v = sign_y * V[1] * dy * (60 / PASSO_MIN)            # km/h verso nord
    m = R[-1] > 5                                        # dove ci sono nubi fredde (Tb < 255 K)
    if m.sum() < 50:
        m = np.ones_like(m, bool)
    um, vm = float(u[m].mean()), float(v[m].mean())
    vel = np.hypot(u, v)[m]

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axs = plt.subplots(1, 3, figsize=(12, 4), dpi=80)
    ext = [lon.min(), lon.max(), lat.min(), lat.max()]
    org = "upper" if lat[0] > lat[-1] else "lower"
    axs[0].imshow(tb_last, cmap="gray_r", vmin=200, vmax=310, extent=ext, origin=org)
    s = 6
    yy, xx = np.meshgrid(lat, lon, indexing="ij")
    axs[0].quiver(xx[::s, ::s], yy[::s, ::s], u[::s, ::s], v[::s, ::s], color="red", scale=1500, width=.004)
    axs[0].set_title(f"Tb {tempi[-1]} + moto LK", fontsize=9)
    for ax, k, lab in ((axs[1], 0, "+30 min"), (axs[2], 1, "+60 min")):
        ax.imshow(fc[k], cmap="Blues", vmin=0, vmax=60, extent=ext, origin=org)
        ax.set_title(f"estrapolazione {lab} (260−Tb)", fontsize=9)
    fig.tight_layout()
    fig.savefig(PREVIEWS / "s9_toolchain_moto.png")
    plt.close(fig)

    return {
        "frame": tempi, "shape_frame": list(R.shape), "secondi": T,
        "moto_medio_nubi_fredde": {"km_h": round(np.hypot(um, vm), 1), "da_gradi": round(direzione(um, vm)),
                                    "u_est_km_h": round(um, 1), "v_nord_km_h": round(vm, 1),
                                    "velocita_mediana_km_h": round(float(np.median(vel)), 1),
                                    "n_pixel": int(m.sum())},
        "frac_nan_forecast": [round(float(np.isnan(fc[k]).mean()), 3) for k in range(2)],
    }


def figlio():
    res = catena()
    try:
        res["era5"] = era5_vento() if (read_result("s6_era5_cds") or {}).get("esito") == "OK" else "S6 non OK: saltato"
    except Exception as e:  # noqa: BLE001
        res["era5"] = f"errore: {e!r}"[:300]
    (RESULTS / ".s9_tmp.json").write_text(json.dumps(res, default=str))


def main():
    t0 = time.perf_counter()
    p = subprocess.run(["/usr/bin/time", "-l", sys.executable, __file__], env=os.environ | {"S9_FIGLIO": "1"},
                       capture_output=True, text=True)
    sec_tot = round(time.perf_counter() - t0, 1)
    m = re.search(r"(\d+)\s+maximum resident set size", p.stderr)
    ram_mb = round(int(m.group(1)) / 1e6, 1) if m else None          # byte su macOS
    tmp = RESULTS / ".s9_tmp.json"
    if p.returncode != 0 or not tmp.exists():
        return write_result(TEST, "KO", DOMANDA, "La catena non è arrivata in fondo",
                            misure={"secondi": {"totale_processo": sec_tot}, "picco_RAM_MB": ram_mb},
                            errori=[p.stderr[-1500:]])
    res = json.loads(tmp.read_text())
    tmp.unlink()
    mm = res["moto_medio_nubi_fredde"]
    e5 = res.get("era5")
    e5s = (f"; ERA5 850–500 hPa {e5['media_850_500']['km_h']} km/h da {e5['media_850_500']['da_gradi']}°"
           if isinstance(e5, dict) else "")
    calcolo = res["secondi"]["moto_LK"] + res["secondi"]["estrapolazione"]
    risposta = (f"Sì: LK + estrapolazione +30/+60 min in {calcolo:.1f} s su {res['shape_frame']}, "
                f"processo intero {sec_tot} s (incl. import e richiesta ERA5), picco RAM {ram_mb} MB; "
                f"moto nubi fredde {mm['km_h']} km/h da {mm['da_gradi']}°{e5s}")
    write_result(TEST, "OK", DOMANDA, risposta,
                 misure={"secondi": res["secondi"] | {"totale_processo": sec_tot}, "MB_scaricati": 0,
                         "MB_conservati": 0, "picco_RAM_MB": ram_mb, **{k: v for k, v in res.items() if k != "secondi"}},
                 note=["Picco RAM = 'maximum resident set size' di /usr/bin/time -l sul processo figlio",
                       "Frecce da verificare a occhio nel PNG: il criterio 'coerenti' non è automatico"])


if __name__ == "__main__":
    figlio() if os.environ.get("S9_FIGLIO") else main()
