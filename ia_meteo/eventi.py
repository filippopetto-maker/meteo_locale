"""Tabella degli eventi (planning, Fase 1): data/events.parquet e su HF conferme/eventi.

Una riga per finestra caricata: id, finestra, bbox, componente (positiva/negativa/casuale), insieme,
sensori e versioni delle fonti, regime, quota della convezione in Italia e nei Balcani.

Regime (planning: plume spagnolo, ciclone di Genova, ciclogenesi tirrenica, convezione adriatico-balcanica,
medicane, temporali orografici appenninici), con regole fisse sui dati della finestra (analisi.py):
- prima la sinottica: un minimo di pressione chiuso sul mare (profondità rispetto all'anello a 400–650 km)
  presente in almeno ORE_MINIMO ore della finestra decide medicane / ciclone di Genova / ciclogenesi tirrenica;
- poi dove sta la convezione, misurata in modo uniforme su tutto il dominio dal satellite (frazione di
  cime più fredde del vapore acqueo, WV − IR > 0) più l'eco radar ≥ 45 dBZ dove il radar vede:
  Balcani e Adriatico orientale → adriatico-balcanica; Italia → appenninica; ovest con flusso da
  sud-ovest a 500 hPa e aria calda a 850 hPa → plume spagnolo; altrimenti "altro" con la zona.
  Il regime sinottico vale solo se il minimo è entro DIST_MINIMO_KM dal riquadro con più convezione.
  Categoria aggiunta al planning: "temporali orografici alpini e padani" (riquadro principale a nord di 45°N).
Sono regole, non un giudizio sinottico esperto: vanno lette insieme ai controlli sugli eventi noti.

Uso:
    python eventi.py           # (ri)costruisce events.parquet dalle finestre analizzate e lo carica su HF
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import analisi as an
import archivio as ar
from common import DOMAIN_BBOX

FILE = ar.DATA / "events.parquet"
ORE_MINIMO = 3
PROFONDITA_CICLONE = 3.0        # hPa rispetto all'anello a 400–650 km
PROFONDITA_MEDICANE = 6.0
VENTO_MEDICANE = 17.0           # m/s a 10 m entro 300 km dal minimo
ATTIVITA_MIN = 0.002
IR_SOGLIA = 0.0005              # frazione del riquadro con Tb < 220 K (analisi.calibra_ir, 20/10/2026: contro i
                                # fulmini POD 0,46, FAR 0,10; contro il radar POD 0,48, FAR 0,07)
DIST_MINIMO_KM = 800.0          # il minimo di pressione decide il regime solo se vicino alla convezione principale            # sotto: nessuna convezione apprezzabile nella finestra
VERSIONI = {
    "satellite_seviri": "SEVIRI L1.5 HRSEVIRI (EO:EUM:DAT:MSG:HRSEVIRI), calibrazione satpy",
    "satellite_fci": "FCI L1c FDHSI (EO:EUM:DAT:0662)",
    "radar_2km": "OPERA ODYSSEY DBZH 2 km / 15 min (openradar-archive)",
    "radar_1km": "OPERA CIRRUS DBZH 1 km / 5 min (openradar-archive)",
    "imerg": "GPM_3IMERGHH V07 (Final)",
    "radar_italia": "IT-DPC-SRI v0.1.0 (Zenodo 10.5281/zenodo.18637608)",
    "fulmini": "MTG LI AF (EO:EUM:DAT:0686)",
}


def _attivita(r: pd.DataFrame) -> pd.Series:
    return r.ir_frac_wv_gt_ir.fillna(0) + r.opera_f45.where(r.opera_cop >= 0.5, 0).fillna(0)


def _dist_km(la1, lo1, la2, lo2):
    return 6371 * np.hypot(np.radians(la1 - la2), np.radians(lo1 - lo2) * np.cos(np.radians((la1 + la2) / 2)))


def regime(riq: pd.DataFrame, sin: pd.DataFrame) -> dict:
    r = riq.assign(att=_attivita(riq))
    per_riq = r.groupby("riquadro").agg(att=("att", "sum"), italia=("italia", "first"), balcani=("balcani", "first"),
                                        v500=("v500", "mean"), u500=("u500", "mean"), t850=("t850", "mean"))
    tot = per_riq.att.sum()
    out = {"attivita": float(tot)}
    if tot < ATTIVITA_MIN:
        return out | {"regime": "nessuna convezione"}
    w = per_riq.att / tot
    lat_c = per_riq.index.str[:5].astype(float) + 2.5
    lon_c = per_riq.index.str[5:].astype(float) + 2.5
    top = per_riq.att.idxmax()
    la_t, lo_t = float(top[:5]) + 2.5, float(top[5:]) + 2.5
    vicini = _dist_km(lat_c, lon_c, la_t, lo_t) <= 600            # il riquadro principale e quelli attorno
    out |= {"riquadro_principale": top, "lat_principale": la_t, "lon_principale": lo_t,
            "quota_principale": float(w[vicini].sum()),
            "quota_italia": float((w * per_riq.italia).sum()), "quota_balcani": float((w * per_riq.balcani).sum()),
            "italia_principale": float(per_riq.italia[top]), "balcani_principale": float(per_riq.balcani[top]),
            "v500_principale": float(per_riq.v500[top]), "u500_principale": float(per_riq.u500[top]),
            "t850_principale": float(per_riq.t850[top])}
    # sinottica: minimo chiuso sul mare in almeno ORE_MINIMO ore, entro DIST_MINIMO_KM dalla convezione principale
    s = sin[sin.msl_profondita >= PROFONDITA_CICLONE].copy()
    s["vicino"] = _dist_km(s.msl_min_lat, s.msl_min_lon, la_t, lo_t) <= DIST_MINIMO_KM
    zona = lambda la, lo, la0, la1, lo0, lo1: (la >= la0) & (la <= la1) & (lo >= lo0) & (lo <= lo1)
    med = s[(s.msl_profondita >= PROFONDITA_MEDICANE) & (s.vento10_max_vicino >= VENTO_MEDICANE)
            & zona(s.msl_min_lat, s.msl_min_lon, 31, 40, 10, 29) & (s.msl_min <= 1005)]
    gen = s[zona(s.msl_min_lat, s.msl_min_lon, 42, 45.5, 6.5, 12)]
    tir = s[zona(s.msl_min_lat, s.msl_min_lon, 35, 42, 9, 16.5)]          # Tirreno e Canale di Sicilia
    out |= {"ore_medicane": int(med.vicino.sum()), "ore_genova": int(gen.vicino.sum()), "ore_tirreno": int(tir.vicino.sum()),
            "medicane_nel_dominio": len(med) >= ORE_MINIMO,
            "msl_min": float(sin.msl_min.min()) if len(sin) else np.nan,
            "profondita_max": float(sin.msl_profondita.max()) if len(sin) else np.nan}
    caldo_sw = out["v500_principale"] > 2 and out["t850_principale"] >= 14
    if out["ore_medicane"] >= ORE_MINIMO:
        reg = "medicane"
    elif out["ore_genova"] >= ORE_MINIMO:
        reg = "ciclone di Genova"
    elif out["ore_tirreno"] >= ORE_MINIMO:
        reg = "ciclogenesi tirrenica"
    elif out["balcani_principale"] >= 0.5:
        reg = "convezione adriatico-balcanica"
    elif out["italia_principale"] >= 0.15:
        if caldo_sw and lo_t < 12.5:
            reg = "plume spagnolo"
        elif la_t >= 47.5:
            reg = "temporali orografici alpini e padani"       # categoria aggiunta: non è nel planning
        else:
            reg = "temporali orografici appenninici"
    elif la_t >= 40 and lo_t < 12.5 and caldo_sw:
        reg = "plume spagnolo"
    else:
        if lo_t < 5:
            zona_c = "iberico-atlantico" if la_t >= 35 else "nordafricano"
        elif la_t < 35:
            zona_c = "nordafricano"
        elif la_t >= 47.5:
            zona_c = "Europa centrale"
        else:
            zona_c = "Mediterraneo centro-orientale"
        reg = f"altro ({zona_c})"
    out["regime"] = reg
    return out


def versioni(voce: dict, meta: dict) -> dict:
    sat = VERSIONI["satellite_fci"] if voce["satellite"] == "fci" else VERSIONI["satellite_seviri"]
    via = meta.get("misure", {}).get("satellite", {}).get("via", voce.get("via", "completo"))
    passo = meta.get("misure", {}).get("radar", {}).get("passo_min")
    v = {"v_satellite": f"{sat}, via {via}",
         "v_radar": VERSIONI["radar_1km"] if passo == 5 else VERSIONI["radar_2km"],
         "v_ambiente": f"{meta.get('misure', {}).get('ambiente', {}).get('fonte', 'ERA5 (Copernicus CDS)')}, "
                       f"ambiente v{voce.get('ambiente_v', 1)}"}
    for s in ("imerg", "radar_italia", "fulmini"):
        v[f"v_{s}"] = VERSIONI[s] if s in voce["sorgenti"] else None
    return v


def conferme(riq: pd.DataFrame) -> dict:
    """Riquadri-ora con convezione vista dal radar e, dove il radar non vede, dall'IR (nubi < 220 K)."""
    radar = (riq.opera_cop >= 0.5) | (riq.itdpc_cop.fillna(0) >= 0.5)
    conv_radar = radar & ((riq.opera_f45.fillna(0) >= 0.001) | (riq.itdpc_f10.fillna(0) >= 0.002))
    conv_ir = ~radar & (riq.ir_frac220 >= IR_SOGLIA)
    return {"riquadri_ora_radar": int(radar.sum()), "conv_radar": int(conv_radar.sum()),
            "riquadri_ora_senza_radar": int((~radar).sum()), "conv_ir_senza_radar": int(conv_ir.sum())}


def costruisci() -> pd.DataFrame:
    reg = ar.registro()["finestre"]
    ordine = pd.read_parquet(ar.DATA / "finestre_ordine.parquet").set_index("id")
    riq_all, sin_all = an.carica()
    righe = []
    for fid, voce in sorted(reg.items()):
        riq, sin = riq_all[riq_all.id == fid], sin_all[sin_all.id == fid]
        if riq.empty:
            continue
        meta = json.loads(ar.scarica("finestre", fid, lambda p: p.endswith("meta.json"))[0].read_text())
        o = ordine.loc[fid] if fid in ordine.index else None
        righe.append({"id": fid, "inizio": pd.Timestamp(voce["inizio"]), "fine": pd.Timestamp(voce["fine"]),
                      "bbox": json.dumps(DOMAIN_BBOX), "componente": voce.get("componente", o.componente if o is not None else None),
                      "rosa": voce.get("rosa", o.rosa if o is not None else None),
                      "insieme": voce.get("insieme", o.insieme if o is not None else None), "posizione": int(o.posizione) if o is not None else None,
                      "satellite": voce["satellite"], "sorgenti": ",".join(sorted(voce["sorgenti"])),
                      "GB": voce["GB"]} | versioni(voce, meta) | conferme(riq) | regime(riq, sin))
    df = pd.DataFrame(righe)
    neg = an.controlla_negativi(riq_all)                 # negativi rivisti con tutti i dati della finestra
    if not neg.empty:
        k = neg.groupby("id").agg(riquadri_negativi=("pulito", "size"), riquadri_negativi_puliti=("pulito", "sum"))
        df = df.merge(k, left_on="id", right_index=True, how="left")
    df.to_parquet(FILE, index=False)
    return df


def carica_su_hf(df: pd.DataFrame) -> str:
    return ar.carica_o_parcheggia("conferme", "eventi", {"conferme/eventi.parquet": FILE},
                                  {"righe": len(df), "regimi": df.regime.value_counts().to_dict()})


if __name__ == "__main__":
    df = costruisci()
    print(df.regime.value_counts().to_string())
    print(carica_su_hf(df))
