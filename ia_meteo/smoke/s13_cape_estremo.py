"""S13 — Verifica del massimo di CAPE ERA5 nel catalogo di luglio 2019 (9888 J/kg).

Il massimo del mese è il 01/07/2019 18 UTC a 45,0°N 16,5°E (valle della Sava). Controlli:
1. ERA5 profilo verticale (37 livelli: T, q) + superficie (2t, 2d, sp) nel punto e a Zagabria
   (45,75°N 16,0°E), alle 12 e 18 UTC; CAPE ricalcolata con MetPy per particella dalla
   superficie (SB), strato rimescolato 100 hPa (ML) e più instabile (MU), confrontata con la
   CAPE pubblicata da ERA5 (most-unstable, particelle sotto 350 hPa).
3. Radar OPERA 15–21 UTC in un quadrato di 2° attorno al punto: c'è stata convezione?
2. Radiosondaggi osservati di Zagabria (WMO 14240, archivio NOAA IGRA2) del 01/07 00 e 12 UTC
   e del 02/07 00 UTC, stessa procedura MetPy.
Il file IGRA2 della stazione (~33 MB) si scarica in raw/, se ne tiene solo il giorno e si cancella.
"""
import io
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import PREVIEWS, RAW, rm, write_result
from fonti import era5

TEST = "s13_cape_estremo"
DOMANDA = "Il CAPE ERA5 di 9888 J/kg (01/07/2019 18 UTC, 45,0°N 16,5°E) è fisico o un artefatto?"
PUNTO = (45.0, 16.5)
ZAGABRIA = (45.75, 16.0)      # punto ERA5 più vicino alla stazione 14240 (45,82°N 16,03°E)
IGRA = "https://www.ncei.noaa.gov/data/integrated-global-radiosonde-archive/access/data-por/HRM00014240-data.txt.zip"
LIVELLI = ["1000", "975", "950", "925", "900", "875", "850", "825", "800", "775", "750", "700", "650", "600",
           "550", "500", "450", "400", "350", "300", "250", "225", "200", "175", "150", "125", "100"]
BB = dict(lat_min=44.75, lat_max=46.0, lon_min=15.75, lon_max=16.75)


def indici(p_hpa, t_c, td_c):
    """SB/ML/MU CAPE e CIN con MetPy su un profilo ordinato dalla superficie verso l'alto."""
    import metpy.calc as mpc
    from metpy.units import units
    p, t, td = p_hpa * units.hPa, t_c * units.degC, td_c * units.degC
    out = {}
    for nome, fn in (("SB", lambda: mpc.surface_based_cape_cin(p, t, td)),
                     ("ML100", lambda: mpc.mixed_layer_cape_cin(p, t, td, depth=100 * units.hPa)),
                     ("MU", lambda: mpc.most_unstable_cape_cin(p, t, td))):
        try:
            cape, cin = fn()
            out[nome] = {"cape": round(float(cape.m), 0), "cin": round(float(cin.m), 0)}
        except Exception as e:  # noqa: BLE001
            out[nome] = {"errore": repr(e)[:120]}
    return out


def profilo_era5(sl, pl, t, lat, lon):
    import metpy.calc as mpc
    from metpy.units import units
    s = sl.sel(time=t, latitude=lat, longitude=lon)
    q = pl.sel(time=t, latitude=lat, longitude=lon)
    sp = float(s.sp) / 100
    lev = q.pressure_level.values
    sopra = lev < sp - 1
    p = np.r_[sp, lev[sopra]]
    tc = np.r_[float(s.t2m) - 273.15, q.t.values[sopra] - 273.15]
    td_lev = mpc.dewpoint_from_specific_humidity(lev[sopra] * units.hPa, q.q.values[sopra] * units("kg/kg")).m
    td = np.r_[float(s.d2m) - 273.15, td_lev]
    ordine = np.argsort(-p)
    info = {"sp_hPa": round(sp, 1), "t2m_C": round(tc[0], 1), "d2m_C": round(td[0], 1),
            "td_925_C": round(float(np.interp(925, p[::-1], td[::-1])), 1),
            "t_500_C": round(float(np.interp(500, p[::-1], tc[::-1])), 1),
            "cape_era5": round(float(s.cape), 0), "cin_era5": None if np.isnan(float(s.cin)) else round(float(s.cin), 0)}
    return p[ordine], tc[ordine], td[ordine], info


def sondaggi_igra():
    """Profili di Zagabria (01/07 00 e 12, 02/07 00 UTC) dal file IGRA2 della stazione."""
    r = requests.get(IGRA, timeout=300)
    r.raise_for_status()
    blocchi, cur = {}, None
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        nome = z.namelist()[0]
        with z.open(nome) as fh:
            for raw in io.TextIOWrapper(fh, encoding="ascii", errors="ignore"):
                if raw.startswith("#"):
                    data = raw[13:26]
                    cur = data if data in ("2019 07 01 00", "2019 07 01 12", "2019 07 02 00") else None
                    if cur:
                        blocchi[cur] = []
                elif cur:
                    blocchi[cur].append(raw.rstrip("\n"))
    out = {}
    for k, righe in blocchi.items():
        p, t, td = [], [], []
        for l in righe:                                  # formato IGRA2 a colonne fisse
            press, temp, dpdp = int(l[9:15]), int(l[22:27]), int(l[34:39])
            if press <= 0 or temp == -9999 or dpdp in (-9999, -8888):
                continue
            p.append(press / 100); t.append(temp / 10); td.append(temp / 10 - dpdp / 10)
        p, t, td = map(np.array, (p, t, td))
        o = np.argsort(-p)
        out[k] = (p[o], t[o], td[o])
    return out


def radar_attorno(lavoro):
    from datetime import timedelta
    from common import bbox_mask
    from fonti import opera
    bb = dict(lat_min=PUNTO[0] - 1, lat_max=PUNTO[0] + 1, lon_min=PUNTO[1] - 1, lon_max=PUNTO[1] + 1)
    out, t = {}, datetime(2019, 7, 1, 15)
    while t <= datetime(2019, 7, 1, 21):
        f, _ = opera.scarica_dbzh(t, lavoro)
        o = opera.leggi_odim(f)
        rm(f)
        lon, lat = opera.lonlat(o)
        v = opera.dbz(o)[bbox_mask(lat, lon, bb)]
        out[f"{t:%H:%M}"] = {"copertura": round(float(np.isfinite(v).mean()), 2), "max_dBZ": round(float(np.nanmax(v)), 1),
                             "px_>=35dBZ": int((v >= 35).sum())}
        t += timedelta(hours=1)
    return out


def main():
    import xarray as xr
    errori, ris = [], {}
    lavoro = RAW / "s13"
    giorno = datetime(2019, 7, 1)
    sl, _ = era5.richiesta(era5.SINGLE, ["2m_temperature", "2m_dewpoint_temperature", "surface_pressure",
                                         "convective_available_potential_energy", "convective_inhibition"],
                           giorno, [12, 18], lavoro, BB)
    pl, _ = era5.richiesta(era5.PRESSURE, ["temperature", "specific_humidity"], giorno, [12, 18], lavoro, BB,
                           livelli=LIVELLI)
    profili = {}
    for nome, (lat, lon) in (("punto_max", PUNTO), ("zagabria", ZAGABRIA)):
        for h in (12, 18):
            t = np.datetime64(f"2019-07-01T{h:02d}:00")
            try:
                p, tc, td, info = profilo_era5(sl, pl, t, lat, lon)
                ris[f"ERA5 {nome} {h:02d}UTC"] = info | {"metpy": indici(p, tc, td)}
                profili[f"ERA5 {nome} {h:02d}"] = (p, tc, td)
            except Exception as e:  # noqa: BLE001
                errori.append(f"ERA5 {nome} {h}: {e!r}"[:300])
    try:
        for k, (p, tc, td) in sondaggi_igra().items():
            ris[f"Zagabria osservato {k}"] = {"livelli": int(len(p)), "p_sup_hPa": float(p[0]),
                                             "t_sup_C": float(tc[0]), "td_sup_C": round(float(td[0]), 1),
                                             "td_925_C": round(float(np.interp(925, p[::-1], td[::-1])), 1),
                                             "metpy": indici(p, tc, td)}
            profili[f"Oss. Zagabria {k[-5:]}"] = (p, tc, td)
    except Exception as e:  # noqa: BLE001
        errori.append(f"IGRA: {e!r}"[:300])
    try:
        ris["radar OPERA attorno al punto"] = radar_attorno(lavoro)
    except Exception as e:  # noqa: BLE001
        errori.append(f"radar: {e!r}"[:300])
    rm(lavoro)
    for k, v in ris.items():
        print(k, v)

    # emagramma semplice dei profili principali
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6.5, 6), dpi=80)
    for (k, (p, tc, td)), col in zip(profili.items(), plt.cm.tab10.colors):
        ax.plot(tc, p, color=col, label=k)
        ax.plot(td, p, color=col, ls="--")
    ax.set_yscale("log"); ax.set_ylim(1020, 300); ax.set_xlim(-40, 40)
    ax.set_yticks([1000, 925, 850, 700, 500, 400, 300]); ax.set_yticklabels([1000, 925, 850, 700, 500, 400, 300])
    ax.set_xlabel("°C (continua T, tratteggio Td)"); ax.set_ylabel("hPa"); ax.legend(fontsize=7)
    ax.set_title("Profili 01/07/2019: ERA5 nel punto del massimo e a Zagabria, sondaggio osservato", fontsize=8)
    fig.tight_layout()
    fig.savefig(PREVIEWS / "s13_cape_estremo_profili.png")
    plt.close(fig)

    m = ris.get("ERA5 punto_max 18UTC", {})
    o = ris.get("Zagabria osservato 2019 07 01 12", {})
    ez = ris.get("ERA5 zagabria 12UTC", {})
    risposta = (f"Punto max 18 UTC: CAPE ERA5 {m.get('cape_era5')}, MetPy dal profilo ERA5 MU "
                f"{m.get('metpy', {}).get('MU', {}).get('cape')} / ML100 {m.get('metpy', {}).get('ML100', {}).get('cape')} / "
                f"SB {m.get('metpy', {}).get('SB', {}).get('cape')} J/kg, Td 2 m {m.get('d2m_C')} °C; "
                f"Zagabria 12 UTC: ERA5 {ez.get('cape_era5')} (MetPy MU {ez.get('metpy', {}).get('MU', {}).get('cape')}) "
                f"contro osservato MU {o.get('metpy', {}).get('MU', {}).get('cape')} / ML100 "
                f"{o.get('metpy', {}).get('ML100', {}).get('cape')} J/kg, Td superficie {o.get('td_sup_C')} °C; radar OPERA "
                f"15–21 UTC entro 1° dal punto: max "
                f"{max((v['max_dBZ'] for v in ris.get('radar OPERA attorno al punto', {}).values()), default=None)} dBZ")
    esito = "OK" if m and o else ("PARZIALE" if ris else "KO")
    write_result(TEST, esito, DOMANDA, risposta, misure={"secondi": {}, "MB_scaricati": 34, "MB_conservati": 0,
                                                          "profili": ris}, errori=errori)


if __name__ == "__main__":
    main()
