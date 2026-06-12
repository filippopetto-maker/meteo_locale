"""
add_new_stations.py
Calcola le feature orografiche per le nuove stazioni e genera l'SQL pronto
per Supabase. Eseguire dalla project root con conda activate meteo.

    cd ~/Desktop/meteo_locale
    conda activate meteo
    python3 add_new_stations.py

Output:
  - Tabella riepilogativa con tutti i valori calcolati
  - SQL da incollare nell'SQL Editor di Supabase (una sola query)
"""

import sys
from pathlib import Path

# Assicura che features.py (project root) sia importabile
sys.path.insert(0, str(Path(__file__).resolve().parent))

from features import compute_static_orography

# ── Nuove stazioni ────────────────────────────────────────────────────────────
#
# Format: (name, lat, lon, microclima, source, note)
#
# Stazioni da DISATTIVARE (is_active = FALSE):
#   id=1  Roma Nord     → duplicato METAR LIRA, stesso dato di Roma Centro
#   id=2  Roma Centro   → duplicato METAR LIRA
#   id=4  Ostia         → sostituita da Ostia Lido (posizione più precisa)
#
# Stazione da MANTENERE:
#   id=3  Roma Sud (Casal Palocco) → unica sul settore sud, mantenuta
#
NEW_STATIONS = [
    # name                      lat        lon       microclima      source
    ("Pratica di Mare",       41.656,   12.449,   "standard",    "metar"),
    ("Cerveteri Ladispoli",   41.9500,  12.0750,  "costiera",    "netatmo"),
    ("Saxa Rubra",            42.0050,  12.4900,  "standard",    "netatmo"),
    ("Selva Nera",            41.9100,  12.3800,  "standard",    "netatmo"),
    ("Cisterna Latina",       41.5900,  12.8270,  "standard",    "netatmo"),
    ("Bracciano",             42.1040,  12.1710,  "quota",       "netatmo"),
]

# ── Calcolo orografia ─────────────────────────────────────────────────────────

print("\n" + "="*80)
print("  Calcolo feature orografiche per le nuove stazioni")
print("="*80)
print(f"\n{'Stazione':<20} {'alt':>5} {'dist_sea':>9} {'dist_ctr':>9} {'bearing':>8}  microclima")
print("-"*80)

results = []
for name, lat, lon, microclima, source in NEW_STATIONS:
    print(f"  Calcolo {name}...", end=" ", flush=True)
    meta = compute_static_orography(lat, lon, microclima)
    meta.update({"name": name, "lat": lat, "lon": lon, "source": source})
    results.append(meta)

    alt  = f"{meta['altitude']:.0f}m" if meta["altitude"] else "n/a"
    print(f"\r  {name:<20} {alt:>5} {meta['dist_sea_km']:>8.2f}k {meta['dist_center_km']:>8.2f}k "
          f"{meta['bearing_sea']:>7.1f}°  {microclima}")

print("-"*80)

# ── Genera SQL ────────────────────────────────────────────────────────────────

print("\n" + "="*80)
print("  SQL DA ESEGUIRE SU SUPABASE (SQL Editor)")
print("="*80)

sql_parts = []

# 2. Inserisce le nuove stazioni
values_lines = []
for r in results:
    alt = f"{r['altitude']:.1f}" if r["altitude"] is not None else "NULL"
    line = (
        f"  ('{r['name']}', {r['lat']}, {r['lon']}, "
        f"{alt}, "
        f"'{r['source']}', "
        f"'{r['microclima']}', "
        f"TRUE, "
        f"{r['dist_sea_km']:.2f}, "
        f"{r['dist_center_km']:.2f}, "
        f"{r['bearing_sea']:.1f})"
    )
    values_lines.append(line)

sql_parts.append(
    "-- ── STEP 2: Inserisce le nuove stazioni ────────────────────────────────────\n"
    "INSERT INTO stations\n"
    "  (name, lat, lon, altitude, source, microclima, is_active,\n"
    "   dist_sea_km, dist_center_km, bearing_sea)\n"
    "VALUES\n" +
    ",\n".join(values_lines) +
    ";\n"
)

# 3. Verifica finale
sql_parts.append("""-- ── STEP 3: Verifica risultato ───────────────────────────────────────────────
SELECT id, name, lat, lon, altitude, microclima,
       dist_sea_km, dist_center_km, bearing_sea, is_active
FROM   stations
ORDER  BY id;
""")

full_sql = "\n".join(sql_parts)
print(full_sql)
print("="*80)
print("\n✅ Copia tutto il blocco SQL qui sopra e incollalo in:")
print("   Supabase → SQL Editor → New query → Run")
print()
