"""S14 — CAPE dello strato rimescolato (ML100) vettoriale: validazione contro MetPy e tempi.

Profili ERA5 sul dominio (23 livelli isobarici T e q, più 2t, 2d, sp) del 01/07/2019 12 e 18 UTC.
`indici.ml_cape_cin` su tutte le colonne in un colpo; `metpy.calc.mixed_layer_cape_cin`
colonna per colonna su 300 colonne casuali (metà con CAPE ERA5 > 500) più il punto del massimo
di S13 (45,0°N 16,5°E). Misure: errore assoluto e relativo, correlazione, tempi.
"""
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import indici
from common import PREVIEWS, RAW, rm, write_result
from fonti import era5

TEST = "s14_mlcape"
DOMANDA = "La CAPE ML100 calcolata in forma vettoriale coincide con MetPy, e quanto costa sull'intero dominio?"
N_CAMPIONE = 300


def metpy_ml(ps, t2, d2, livelli, T, q):
    import metpy.calc as mpc
    from metpy.units import units
    sopra = livelli < ps - 1
    p = np.r_[ps, livelli[sopra]]
    t = np.r_[t2, T[sopra]] - 273.15
    td_l = mpc.dewpoint_from_specific_humidity(livelli[sopra] * units.hPa, q[sopra] * units("kg/kg")).m
    td = np.r_[d2 - 273.15, td_l]
    cape, cin = mpc.mixed_layer_cape_cin(p * units.hPa, t * units.degC, td * units.degC, depth=100 * units.hPa)
    return float(cape.m), float(cin.m)


def main():
    lavoro = RAW / "s14"
    giorno = datetime(2019, 7, 1)
    t0 = time.perf_counter()
    sl, mb1 = era5.richiesta(era5.SINGLE, era5.VAR_SUPERFICIE_ML + ["convective_available_potential_energy"],
                             giorno, [12, 18], lavoro)
    pl, mb2 = era5.richiesta(era5.PRESSURE, ["temperature", "specific_humidity"], giorno, [12, 18], lavoro,
                             livelli=era5.LIVELLI_ML)
    sec_cds = round(time.perf_counter() - t0, 1)
    livelli = pl.pressure_level.values.astype(float)
    ordine = np.argsort(-livelli)
    livelli = livelli[ordine]
    rng = np.random.default_rng(0)
    confronto, tempi_vett, errori = [], [], []
    campi = {}
    for t in sl.time.values:
        s, p = sl.sel(time=t), pl.sel(time=t).isel(pressure_level=ordine)
        ny, nx = s.sp.shape
        ps = s.sp.values.ravel() / 100
        t2, d2 = s.t2m.values.ravel(), s.d2m.values.ravel()
        T = p.t.values.reshape(len(livelli), -1).T
        q = p.q.values.reshape(len(livelli), -1).T
        tt = time.perf_counter()
        r = indici.ml_cape_cin(ps, t2, d2, livelli, T, q)
        tempi_vett.append(round(time.perf_counter() - tt, 2))
        campi[str(t)[:13]] = (r["cape"].reshape(ny, nx), s.cape.values, s.latitude.values, s.longitude.values)
        cape_era5 = s.cape.values.ravel()
        alti = np.flatnonzero(cape_era5 > 500)
        scelte = np.r_[rng.choice(alti, min(N_CAMPIONE // 4, len(alti)), replace=False),
                       rng.choice(len(ps), N_CAMPIONE // 4, replace=False)]
        lat, lon = np.meshgrid(s.latitude.values, s.longitude.values, indexing="ij")
        ip = np.flatnonzero((lat.ravel() == 45.0) & (lon.ravel() == 16.5))
        scelte = np.r_[scelte, ip]
        tm = time.perf_counter()
        for i in scelte:
            try:
                c_m, n_m = metpy_ml(ps[i], t2[i], d2[i], livelli, T[i], q[i])
                confronto.append({"t": str(t)[:13], "lat": float(lat.ravel()[i]), "lon": float(lon.ravel()[i]),
                                  "metpy_cape": c_m, "vett_cape": float(r["cape"][i]),
                                  "metpy_cin": n_m, "vett_cin": float(r["cin"][i]), "era5_cape": float(cape_era5[i])})
            except Exception as e:  # noqa: BLE001
                errori.append(f"metpy col {i}: {e!r}"[:200])
        tempi_vett.append(f"metpy {len(scelte)} colonne: {time.perf_counter() - tm:.1f} s")
    rm(lavoro)

    mc = np.array([c["metpy_cape"] for c in confronto])
    vc = np.array([c["vett_cape"] for c in confronto])
    mn = np.array([c["metpy_cin"] for c in confronto])
    vn = np.array([c["vett_cin"] for c in confronto])
    sig = mc > 100
    stat = {
        "n_colonne": int(len(mc)),
        "cape_errore_assoluto_mediano": round(float(np.median(np.abs(vc - mc))), 1),
        "cape_errore_assoluto_p95": round(float(np.percentile(np.abs(vc - mc), 95)), 1),
        "cape_errore_relativo_mediano_dove_>100": round(float(np.median(np.abs(vc - mc)[sig] / mc[sig])), 4),
        "cape_errore_relativo_p95_dove_>100": round(float(np.percentile(np.abs(vc - mc)[sig] / mc[sig], 95)), 4),
        "cape_correlazione": round(float(np.corrcoef(mc, vc)[0, 1]), 5),
        "cin_errore_assoluto_mediano": round(float(np.median(np.abs(vn - mn))), 1),
        "cin_errore_assoluto_p95": round(float(np.percentile(np.abs(vn - mn), 95)), 1),
        "concordanza_cape>0": round(float(((mc > 0) == (vc > 0)).mean()), 4),
        "peggiori_5": sorted(confronto, key=lambda c: -abs(c["vett_cape"] - c["metpy_cape"]))[:5],
        "punto_s13": [c for c in confronto if c["lat"] == 45.0 and c["lon"] == 16.5],
    }
    print({k: v for k, v in stat.items() if k not in ("peggiori_5",)})

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axs = plt.subplots(1, 3, figsize=(15, 4), dpi=70)
    axs[0].scatter(mc, vc, s=6)
    axs[0].plot([0, mc.max()], [0, mc.max()], "k--", lw=.8)
    axs[0].set_xlabel("MetPy ML100 CAPE (J/kg)"); axs[0].set_ylabel("vettoriale (J/kg)")
    axs[0].set_title(f"{len(mc)} colonne, r = {stat['cape_correlazione']}", fontsize=9)
    k = "2019-07-01T18"
    ml, mu, la, lo = campi[k]
    for ax, f, tit in ((axs[1], mu, "ERA5 CAPE (most-unstable)"), (axs[2], ml, "ML100 CAPE vettoriale")):
        im = ax.pcolormesh(lo, la, f, cmap="YlOrRd", vmin=0, vmax=4000)
        ax.set_title(f"{tit} 01/07/2019 18 UTC", fontsize=9)
    fig.colorbar(im, ax=axs[1:], label="J/kg")
    fig.savefig(PREVIEWS / "s14_mlcape.png")
    plt.close(fig)

    ok = stat["cape_errore_relativo_mediano_dove_>100"] < 0.05 and stat["cape_correlazione"] > 0.99
    esito = "OK" if ok else "PARZIALE"
    risposta = (f"Errore relativo CAPE mediano {100 * stat['cape_errore_relativo_mediano_dove_>100']:.1f}% "
                f"(p95 {100 * stat['cape_errore_relativo_p95_dove_>100']:.1f}%), r = {stat['cape_correlazione']}, "
                f"CIN errore mediano {stat['cin_errore_assoluto_mediano']} J/kg; dominio intero (13 041 colonne) in "
                f"{tempi_vett[0]} s per ora contro {tempi_vett[1]}; punto S13 18 UTC: "
                f"{[(round(c['metpy_cape']), round(c['vett_cape'])) for c in stat['punto_s13'] if c['t'].endswith('18')]}")
    write_result(TEST, esito, DOMANDA, risposta,
                 misure={"secondi": {"cds": sec_cds}, "MB_scaricati": round(mb1 + mb2, 2), "MB_conservati": 0,
                         "tempi": tempi_vett, "livelli": livelli.tolist(), "statistiche": stat},
                 errori=errori)


if __name__ == "__main__":
    main()
