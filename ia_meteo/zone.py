"""Zone fisse sulla griglia comune 0,05° (400 × 800, righe da nord): terra, Italia, Balcani, riquadri 5°.

Balcani (planning: esclusi dal train, inclusi nel test storico), fissati il 20/10/2026 prima di
qualsiasi risultato e da non cambiare: terra e mare a est dell'asse dell'Adriatico (retta da
45,8°N 13,0°E a 39,8°N 19,0°E, che lascia all'Italia Gargano, Puglia e Salento e ai Balcani la costa
dalmata e le isole croate) e, più a sud, a est di 19,5°E (Grecia ionica ed Egea), fra 34,5°N e 47°N.
Trieste e il Carso, a est della retta, restano esclusi dai Balcani perché nell'area del radar italiano.
"italia" è la terra vista dal radar italiano fuori dai Balcani: Italia più Corsica, Malta, Canton Ticino
e la costa di Nizza (per l'Italia in senso stretto, o il Lazio, servirà un confine amministrativo).

Uso:
    python zone.py          # ricalcola data/zone_005.npz e stampa le aree
"""
from __future__ import annotations

import numpy as np

import archivio as ar

FILE = ar.DATA / "zone_005.npz"
TERRA_SHP = "/Users/filippo/.local/share/cartopy/shapefiles/natural_earth/physical/ne_10m_land.shp"
NY, NX, PASSO = 400, 800, 0.05
LAT = 50 - PASSO / 2 - PASSO * np.arange(NY)          # centri di cella, da nord
LON = -10 + PASSO / 2 + PASSO * np.arange(NX)
FINESTRA_IMPRONTA_ITALIA = "20230724_12"                # una finestra qualsiasi con radar_italia


def riquadri() -> dict[str, tuple[slice, slice]]:
    """Riquadri 5° del catalogo ('+40.0+010.0' = angolo sud-ovest) → (righe, colonne) della griglia."""
    out = {}
    for la in (30, 35, 40, 45):
        for lo in range(-10, 30, 5):
            r = np.flatnonzero((LAT >= la) & (LAT < la + 5))
            c = np.flatnonzero((LON >= lo) & (LON < lo + 5))
            out[f"{la:+05.1f}{lo:+06.1f}"] = (slice(r[0], r[-1] + 1), slice(c[0], c[-1] + 1))
    return out


def _terra() -> np.ndarray:
    import shapefile
    from matplotlib.path import Path as MPath
    LO, LA = np.meshgrid(LON, LAT)
    pts = np.c_[LO.ravel(), LA.ravel()]
    m = np.zeros(len(pts), bool)
    for sh in shapefile.Reader(TERRA_SHP).shapes():
        x0, y0, x1, y1 = sh.bbox
        if x1 < -10 or x0 > 30 or y1 < 30 or y0 > 50:
            continue
        parti = list(sh.parts) + [len(sh.points)]
        for i in range(len(parti) - 1):
            p = np.array(sh.points[parti[i]:parti[i + 1]])
            bb = (pts[:, 0] >= p[:, 0].min()) & (pts[:, 0] <= p[:, 0].max()) & \
                 (pts[:, 1] >= p[:, 1].min()) & (pts[:, 1] <= p[:, 1].max())
            if bb.any():
                m[bb] ^= MPath(p).contains_points(pts[bb])      # xor: i laghi si tolgono
    return m.reshape(NY, NX)


def _impronta_radar_italia() -> np.ndarray:
    import xarray as xr
    f = ar.scarica("finestre", FINESTRA_IMPRONTA_ITALIA, lambda p: "radar_italia" in p)[0]
    with xr.open_dataset(f) as ds:
        v = ds.pioggia_max.notnull().any("time").values
        if ds.lat.values[0] < ds.lat.values[-1]:
            v = v[::-1]
    return v


def _balcani(italia_radar: np.ndarray) -> np.ndarray:
    LO, LA = np.meshgrid(LON, LAT)
    asse = 13.0 + (45.8 - LA) * (19.0 - 13.0) / (45.8 - 39.8)     # longitudine dell'asse adriatico
    nord = (LA >= 39.8) & (LO > asse)
    sud = (LA < 39.8) & (LO > 19.5)
    m = (nord | sud) & (LA >= 34.5) & (LA <= 47.0)
    trieste = (LA >= 45.4) & (LO < 14.0) & italia_radar
    return m & ~trieste


def calcola() -> dict:
    terra = _terra()
    imp = _impronta_radar_italia()
    balcani = _balcani(imp)
    italia = terra & imp & ~balcani
    np.savez_compressed(FILE, terra=terra, italia=italia, balcani=balcani, impronta_radar_italia=imp)
    return {"terra": terra, "italia": italia, "balcani": balcani, "impronta_radar_italia": imp}


def carica() -> dict:
    if not FILE.exists():
        return calcola()
    z = np.load(FILE)
    return {k: z[k] for k in z.files}


if __name__ == "__main__":
    z = calcola()
    for k, v in z.items():
        print(f"{k}: {v.mean():.1%} del dominio, {v.sum()} celle")
    for nome, (la, lo) in {"Roma": (41.9, 12.5), "Bari": (41.1, 16.9), "Lecce": (40.35, 18.17),
                           "Trieste": (45.65, 13.78), "Spalato": (43.51, 16.44), "Valona": (40.47, 19.49),
                           "Atene": (37.98, 23.73), "Patrasso": (38.25, 21.73), "Catania": (37.5, 15.09),
                           "Lubiana": (46.05, 14.51), "Belgrado": (44.79, 20.45), "Corfù": (39.62, 19.92)}.items():
        i, j = int(np.argmin(abs(LAT - la))), int(np.argmin(abs(LON - lo)))
        print(f"{nome:9s} terra {z['terra'][i, j]!s:5s} italia {z['italia'][i, j]!s:5s} balcani {z['balcani'][i, j]!s:5s}")
