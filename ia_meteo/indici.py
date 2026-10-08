"""Indici convettivi vettoriali su griglie intere (migliaia di colonne alla volta).

`ml_cape_cin` riproduce `metpy.calc.mixed_layer_cape_cin` (MetPy 1.7, profondità 100 hPa)
senza ciclo sulle colonne, per poterla calcolare sull'intero dominio ERA5 per anni di dati:
- particella: temperatura potenziale e rapporto di mescolanza medi (pesati in pressione) sui
  primi `depth` hPa sopra la superficie, partenza dalla pressione al suolo;
- ambiente: come MetPy, il punto in superficie è sostituito dalla particella rimescolata e si
  usano i livelli sopra lo strato rimescolato (p < ps − depth);
- LCL con la formula di Bolton (1980); sopra l'LCL pseudoadiabatica integrata con Runge-Kutta
  sulla stessa equazione di `metpy.calc.moist_lapse`;
- galleggiamento in temperatura virtuale (come `metpy.calc.cape_cin`);
- LFC più basso e EL più alto; CAPE = Rd ∫ ΔTv dln p tra LFC ed EL, CIN dalla superficie
  all'LFC (solo negativa), su una griglia fine di 5 hPa.
La validazione contro MetPy colonna per colonna è nello smoke test S14.
"""
from __future__ import annotations

import numpy as np

RD, CP, EPS, LV = 287.04749, 1004.6662, 0.6219569, 2.50084e6   # costanti come in metpy.constants
KAPPA = RD / CP
P_TOP = 100.0          # hPa, tetto minimo dell'integrazione (di fatto: il livello più alto fornito)
PASSO = 5.0            # hPa, griglia fine


def es_hpa(t_k):
    """Pressione di vapore saturo (Bolton 1980, come MetPy), hPa."""
    t_c = t_k - 273.15
    return 6.112 * np.exp(17.67 * t_c / (t_c + 243.5))


def rs(p_hpa, t_k):
    e = es_hpa(t_k)
    return EPS * e / (p_hpa - e)


def dewpoint_da_w(p_hpa, w):
    e = np.clip(w * p_hpa / (EPS + w), 1e-6, None)
    x = np.log(e / 6.112)
    return 243.5 * x / (17.67 - x) + 273.15


def w_da_q(q):
    return q / (1 - q)


def tv(t_k, w):
    return t_k * (w + EPS) / (EPS * (1 + w))


def _lapse(p, t):
    """dT/dp pseudoadiabatica (metpy.calc.moist_lapse)."""
    r = rs(p, t)
    return (RD * t + LV * r) / (CP + LV * LV * r * EPS / (RD * t * t)) / p


def _interp_logp(p_target, p_a, v_a, p_b, v_b):
    w = (np.log(p_target) - np.log(p_a)) / (np.log(p_b) - np.log(p_a))
    return v_a + w * (v_b - v_a)


def ml_cape_cin(ps, t2, d2, livelli, T, q, depth=100.0):
    """CAPE e CIN dello strato rimescolato per N colonne.

    ps, t2, d2: (N,) pressione al suolo (hPa), temperatura e punto di rugiada a 2 m (K).
    livelli: (K,) pressioni dei livelli isobarici (hPa), decrescenti.
    T, q: (N, K) temperatura (K) e umidità specifica (kg/kg) ai livelli.
    Ritorna dict di array (N,): cape, cin (J/kg, cin ≤ 0), p_lcl, t_ml, td_ml.
    """
    ps, t2, d2 = (np.asarray(a, float) for a in (ps, t2, d2))
    livelli = np.asarray(livelli, float)
    T, w_liv = np.asarray(T, float), w_da_q(np.asarray(q, float))
    N, K = T.shape
    ptop = max(P_TOP, float(livelli.min()))
    td_liv = dewpoint_da_w(livelli[None, :], w_liv)

    # --- strato rimescolato: media pesata in pressione di theta e w tra ps e ps - depth
    p_ml = ps[:, None] - depth * np.linspace(0, 1, 21)[None, :]            # (N, 21)
    # profilo "pieno" per l'interpolazione: superficie + livelli sopra il suolo
    theta_liv = T * (1000.0 / livelli[None, :]) ** KAPPA
    theta_s = t2 * (1000.0 / ps) ** KAPPA
    w_s = rs(ps, d2)
    theta_l = np.empty_like(p_ml)
    w_l = np.empty_like(p_ml)
    for j in range(p_ml.shape[1]):
        theta_l[:, j] = _profilo(p_ml[:, j], ps, theta_s, livelli, theta_liv)
        w_l[:, j] = _profilo(p_ml[:, j], ps, w_s, livelli, w_liv)
    dp = -np.diff(p_ml, axis=1)
    theta_m = (0.5 * (theta_l[:, 1:] + theta_l[:, :-1]) * dp).sum(1) / dp.sum(1)
    w_m = (0.5 * (w_l[:, 1:] + w_l[:, :-1]) * dp).sum(1) / dp.sum(1)
    t0 = theta_m * (ps / 1000.0) ** KAPPA
    td0 = dewpoint_da_w(ps, w_m)

    # --- LCL (Bolton 1980, eq. 15 con il punto di rugiada)
    t_lcl = 1.0 / (1.0 / (td0 - 56.0) + np.log(t0 / td0) / 800.0) + 56.0
    p_lcl = ps * (t_lcl / t0) ** (1.0 / KAPPA)

    # --- griglia fine assoluta e compattata per colonna: [ps, ps-5, ps-10, ...] fino a P_TOP
    M = int(np.ceil((ps.max() - ptop) / PASSO)) + 1
    pg = ps[:, None] - PASSO * np.arange(M)[None, :]
    valido = pg >= ptop

    # ambiente sulla griglia: (ps, t0/td0) poi i livelli con p < ps - depth (come MetPy)
    sopra = livelli[None, :] < (ps[:, None] - depth)
    te = np.full((N, M), np.nan)
    tde = np.full((N, M), np.nan)
    k0 = np.argmax(sopra, axis=1)                      # primo livello sopra lo strato rimescolato
    p_k0 = livelli[k0]
    t_k0 = T[np.arange(N), k0]
    td_k0 = td_liv[np.arange(N), k0]
    lnl = np.log(livelli)
    for j in range(M):
        p = pg[:, j]
        primo = p >= p_k0
        a = _interp_logp(p, ps, t0, p_k0, t_k0)
        b = _interp_logp(p, ps, td0, p_k0, td_k0)
        # tra livelli fissi
        idx = np.clip(np.searchsorted(-lnl, -np.log(np.clip(p, ptop, None))) - 1, 0, K - 2)
        wgt = (np.log(np.clip(p, ptop, None)) - lnl[idx]) / (lnl[idx + 1] - lnl[idx])
        ta = T[np.arange(N), idx] + wgt * (T[np.arange(N), idx + 1] - T[np.arange(N), idx])
        tb = td_liv[np.arange(N), idx] + wgt * (td_liv[np.arange(N), idx + 1] - td_liv[np.arange(N), idx])
        te[:, j] = np.where(primo, a, ta)
        tde[:, j] = np.where(primo, b, tb)
    we = rs(pg, tde)

    # --- particella: secca fino all'LCL, poi pseudoadiabatica (RK4 a passi ≤ 5 hPa)
    tp = t0[:, None] * (pg / ps[:, None]) ** KAPPA
    p_cur, t_cur = p_lcl.copy(), t_lcl.copy()
    for j in range(M):
        p = pg[:, j]
        m = p < p_lcl
        if not m.any():
            continue
        h = np.where(m, p - p_cur, 0.0)
        k1 = _lapse(p_cur, t_cur)
        k2 = _lapse(p_cur + h / 2, t_cur + h / 2 * k1)
        k3 = _lapse(p_cur + h / 2, t_cur + h / 2 * k2)
        k4 = _lapse(p_cur + h, t_cur + h * k3)
        t_new = t_cur + h / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
        tp[:, j] = np.where(m, t_new, tp[:, j])
        p_cur = np.where(m, p, p_cur)
        t_cur = np.where(m, t_new, t_cur)
    wp = np.where(pg > p_lcl[:, None], w_m[:, None], rs(pg, tp))
    y = tv(tp, wp) - tv(te, we)
    y = np.where(valido, y, np.nan)

    # --- LFC (il più basso) ed EL (il più alto)
    sopra_lcl = pg <= p_lcl[:, None]
    pos = (y > 0) & valido
    lfc_idx = np.argmax(pos & sopra_lcl, axis=1)
    ha_lfc = (pos & sopra_lcl).any(1)
    ultimo_pos = M - 1 - np.argmax(pos[:, ::-1], axis=1)
    el_idx = np.minimum(ultimo_pos + 1, (valido.sum(1) - 1))
    j = np.arange(M)[None, :]
    lnp = np.log(np.where(valido, pg, ptop))
    seg = 0.5 * (y[:, 1:] + y[:, :-1]) * (lnp[:, :-1] - lnp[:, 1:])          # contributo di ogni strato
    seg = np.nan_to_num(seg)
    in_cape = (j[:, :-1] >= lfc_idx[:, None]) & (j[:, 1:] <= el_idx[:, None])
    in_cin = j[:, 1:] <= lfc_idx[:, None]
    cape = RD * (seg * in_cape).sum(1)
    cin = RD * (seg * in_cin).sum(1)
    cape = np.where(ha_lfc, np.maximum(cape, 0), 0.0)
    cin = np.where(ha_lfc, np.minimum(cin, 0), 0.0)
    return {"cape": cape, "cin": cin, "p_lcl": p_lcl, "t_ml": t0, "td_ml": td0}


def _profilo(p, ps, v_s, livelli, v_liv):
    """Valore a pressione p (N,) da un profilo superficie (ps, v_s) + livelli sopra il suolo, in log p."""
    N = p.shape[0]
    sopra = livelli[None, :] < ps[:, None]
    k0 = np.argmax(sopra, axis=1)
    p_k0, v_k0 = livelli[k0], v_liv[np.arange(N), k0]
    a = _interp_logp(p, ps, v_s, p_k0, v_k0)
    lnl = np.log(livelli)
    idx = np.clip(np.searchsorted(-lnl, -np.log(p)) - 1, 0, len(livelli) - 2)
    wgt = (np.log(p) - lnl[idx]) / (lnl[idx + 1] - lnl[idx])
    b = v_liv[np.arange(N), idx] + wgt * (v_liv[np.arange(N), idx + 1] - v_liv[np.arange(N), idx])
    return np.where(p >= p_k0, a, b)
