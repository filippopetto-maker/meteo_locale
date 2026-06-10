"""
patch_dashboard_chart.py
Sostituisce il grafico spezzato della Sezione 2 di output/dashboard.py
con un grafico Altair in formato long-format.

Causa del bug: pd.concat(axis=1) su indici di tempo diversi (forecast ogni 30
min, METAR ogni ~20 min) crea una matrice sparse piena di NaN → st.line_chart
spezza la linea ad ogni NaN invece di collegare i punti.

Soluzione: formato long con timestamp separati per serie, identico
all'approccio già usato nella Sezione 4.

Esegui da project root:
    python3 patch_dashboard_chart.py

Il file originale viene salvato come output/dashboard.py.bak prima della modifica.
"""

import shutil
from pathlib import Path

DASHBOARD = Path("output/dashboard.py")
BACKUP    = Path("output/dashboard.py.bak")

# ─── blocco da sostituire (righe 288-304 circa) ───────────────────────────────
OLD = """    parts = []
    if not forecasts_st.empty and target in forecasts_st.columns:
        f = forecasts_st[["valid_for", target]].rename(
            columns={"valid_for": "time", target: "Previsto"}
        ).set_index("time")
        parts.append(f)
    if not obs_df.empty and target in obs_df.columns:
        o = obs_df[["recorded_at", target]].rename(
            columns={"recorded_at": "time", target: "Osservato"}
        ).set_index("time")
        parts.append(o)

    if parts:
        chart_df = pd.concat(parts, axis=1).sort_index()
        st.line_chart(chart_df)
    else:
        st.info(f"Nessun valore di '{target}' disponibile.")"""

# ─── sostituzione: long-format + Altair ──────────────────────────────────────
NEW = """    parts_long = []
    if not forecasts_st.empty and target in forecasts_st.columns:
        fc = forecasts_st[["valid_for", target]].copy()
        fc = fc.rename(columns={"valid_for": "time", target: "valore"})
        fc["Serie"] = "Previsto"
        parts_long.append(fc)
    if not obs_df.empty and target in obs_df.columns:
        ob = obs_df[["recorded_at", target]].copy()
        ob = ob.rename(columns={"recorded_at": "time", target: "valore"})
        ob["Serie"] = "Osservato"
        parts_long.append(ob)

    if parts_long:
        plot_df = (
            pd.concat(parts_long, ignore_index=True)
            .dropna(subset=["valore"])
            .sort_values("time")
        )
        y_label = target.replace("_", " ").title()
        chart = (
            alt.Chart(plot_df)
            .mark_line(point=alt.OverlayMarkDef(size=40), strokeWidth=2)
            .encode(
                x=alt.X("time:T", title="Ora (Italia)"),
                y=alt.Y("valore:Q", title=y_label),
                color=alt.Color(
                    "Serie:N",
                    title="",
                    scale=alt.Scale(
                        domain=["Osservato", "Previsto"],
                        range=["#1f77b4", "#aec7e8"],
                    ),
                ),
                strokeDash=alt.StrokeDash(
                    "Serie:N",
                    scale=alt.Scale(
                        domain=["Previsto", "Osservato"],
                        range=[[6, 4], [0, 0]],
                    ),
                    legend=None,
                ),
                tooltip=[
                    alt.Tooltip("time:T",   title="Ora",   format="%d/%m %H:%M"),
                    alt.Tooltip("Serie:N",  title="Serie"),
                    alt.Tooltip("valore:Q", title=y_label, format=".1f"),
                ],
            )
            .properties(height=320)
        )
        st.altair_chart(chart, use_container_width=True)
    else:
        st.info(f"Nessun valore di '{target}' disponibile.")"""


def main() -> None:
    if not DASHBOARD.exists():
        print(f"[ERRORE] File non trovato: {DASHBOARD}")
        print("  Esegui da project root: python3 patch_dashboard_chart.py")
        return

    original = DASHBOARD.read_text(encoding="utf-8")

    if OLD not in original:
        print("[ERRORE] Testo target non trovato nel file.")
        print("  Il file è già stato patchato, oppure la versione è diversa.")
        return

    shutil.copy(DASHBOARD, BACKUP)
    print(f"[OK] Backup salvato → {BACKUP}")

    patched = original.replace(OLD, NEW, 1)
    DASHBOARD.write_text(patched, encoding="utf-8")
    print(f"[OK] {DASHBOARD} aggiornato.")
    print()
    print("Riavvia Streamlit:")
    print("  streamlit run output/dashboard.py")


if __name__ == "__main__":
    main()
