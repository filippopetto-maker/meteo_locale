#!/usr/bin/env bash
# deploy_phase2b.sh
# Finalizza il deploy di Phase 2b (Netatmo) su GitHub.
# Eseguire dalla project root: bash deploy_phase2b.sh

set -e  # blocca al primo errore

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  deploy_phase2b.sh — Netatmo live ingestion"
echo "═══════════════════════════════════════════════════════"
echo ""

# ── 1. Verifica prerequisiti ─────────────────────────────────────────────────
echo "▶ 1/4 Verifica prerequisiti..."

if [ ! -f ".github/workflows/ingestion.yml" ]; then
    echo "  ❌ .github/workflows/ingestion.yml non trovato"
    echo "     Assicurati di eseguire dalla project root."
    exit 1
fi

if ! grep -q "SUPABASE_URL" .github/workflows/ingestion.yml; then
    echo "  ❌ ingestion.yml non contiene SUPABASE_URL — formato inatteso"
    exit 1
fi

echo "  ✅ ingestion.yml trovato"

# ── 2. Aggiunge le variabili Netatmo a ingestion.yml (se mancanti) ───────────
echo ""
echo "▶ 2/4 Aggiornamento ingestion.yml..."

if grep -q "NETATMO_CLIENT_ID" .github/workflows/ingestion.yml; then
    echo "  ℹ️  Variabili Netatmo già presenti — nessuna modifica"
else
    # Inserisce le 3 righe Netatmo subito dopo la riga SUPABASE_KEY
    python3 - << 'PYEOF'
import re

path = ".github/workflows/ingestion.yml"
with open(path, "r") as f:
    content = f.read()

netatmo_block = (
    "          NETATMO_CLIENT_ID: ${{ secrets.NETATMO_CLIENT_ID }}\n"
    "          NETATMO_CLIENT_SECRET: ${{ secrets.NETATMO_CLIENT_SECRET }}\n"
    "          NETATMO_REFRESH_TOKEN: ${{ secrets.NETATMO_REFRESH_TOKEN }}\n"
)

# Cerca la riga SUPABASE_KEY (con qualsiasi indentazione) e aggiunge dopo
pattern = r'([ \t]*SUPABASE_KEY:.*\n)'

if re.search(pattern, content):
    new_content = re.sub(pattern, r'\1' + netatmo_block, content, count=1)
    with open(path, "w") as f:
        f.write(new_content)
    print("  ✅ Variabili Netatmo aggiunte a ingestion.yml")
else:
    print("  ⚠️  Riga SUPABASE_KEY non trovata — aggiungo in fondo alla sezione env:")
    # Fallback: aggiunge alla fine del file come commento da posizionare manualmente
    with open(path, "a") as f:
        f.write("\n# TODO: aggiungere manualmente nella sezione env:\n")
        f.write("# " + netatmo_block.replace("\n", "\n# "))
    print("  ⚠️  Aggiunto come commento in fondo — posiziona manualmente nella sezione env:")
PYEOF
fi

# ── 3. Mostra diff dei file modificati ───────────────────────────────────────
echo ""
echo "▶ 3/4 File modificati da committare:"
echo ""
git diff --name-only 2>/dev/null || true
git diff --cached --name-only 2>/dev/null || true
# File nuovi non tracciati
git ls-files --others --exclude-standard 2>/dev/null || true
echo ""

# ── 4. Git add, commit, push ─────────────────────────────────────────────────
echo "▶ 4/4 Git commit e push..."
echo ""

git add db.py
git add mainMETEO.py
git add .github/workflows/ingestion.yml

# Aggiunge fetch_netatmo se esiste come file separato
[ -f "fetch_netatmo.py" ] && git add fetch_netatmo.py

git status --short
echo ""

git commit -m "Phase 2b: Netatmo live ingestion

- fetch_netatmo(): raccolta dati pubblici Netatmo per bbox Roma
  341 stazioni pubbliche, aggregazione mediana per 4 stazioni progetto
  QC a 4 livelli, raw_source JSONB, qc_log su flag=1
- db.py: upsert observations (ignore_duplicates) — fix 409 METAR duplicate
- db.py: raw_source ora incluso nell'insert
- ingestion.yml: aggiunge NETATMO_CLIENT_ID/SECRET/REFRESH_TOKEN"

git push

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  ✅ Deploy Phase 2b completato"
echo ""
echo "  Verifica il primo run automatico su:"
echo "  https://github.com/filippopetto-maker/meteo_locale/actions"
echo "═══════════════════════════════════════════════════════"
echo ""
