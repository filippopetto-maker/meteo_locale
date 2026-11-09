"""
modelli.py — controllo dei file dei modelli congelati

I modelli (model/m2*/, model/lgbm_*.txt, model/rf_correttore_*.pkl, data/arsial_bias_table.json)
non stanno nel repo pubblico: arrivano dalla release privata indicata in models.lock
(azione .github/actions/fetch-models su Actions, tools/pack_models.sh --local in locale).
Se manca un file si esce con un messaggio chiaro invece di uno stack trace.
"""

from __future__ import annotations

import sys
from pathlib import Path

MESSAGGIO = "modelli mancanti: eseguire fetch-models o tools/pack_models.sh --local"
_ROOT = Path(__file__).resolve().parent.parent


class ModelliMancanti(FileNotFoundError):
    pass


def richiedi(*paths: Path) -> None:
    """Solleva ModelliMancanti se uno dei file non esiste."""
    mancanti = [p for p in paths if not Path(p).is_file()]
    if mancanti:
        elenco = ", ".join(str(Path(p).resolve().relative_to(_ROOT)) if Path(p).resolve().is_relative_to(_ROOT)
                           else str(p) for p in mancanti)
        raise ModelliMancanti(f"{MESSAGGIO} (manca: {elenco})")


_hook_precedente = sys.excepthook


def _hook(tipo, valore, tb):
    if issubclass(tipo, ModelliMancanti):
        print(f"ERRORE: {valore}", file=sys.stderr)
        return
    _hook_precedente(tipo, valore, tb)


# Uscita pulita (codice 1, senza stack trace) quando l'errore arriva fino in cima.
sys.excepthook = _hook
