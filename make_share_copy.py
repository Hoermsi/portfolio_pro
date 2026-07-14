"""Erstellt eine saubere, datenfreie Kopie von Portfolio Pro zum Weitergeben.

Kopiert den gesamten Projektordner in ein Zielverzeichnis, LÄSST aber alle
persönlichen Daten, Secrets und Caches weg (portfolio.db, .env, __pycache__ …).
Der Empfänger startet die Kopie und durchläuft beim ersten Start die
Ersteinrichtung (API-Keys + Profil, alles optional).

Nutzung:
    python make_share_copy.py                     # -> ../portfolio_pro_freund
    python make_share_copy.py "C:\\Pfad\\zum\\Ziel"
"""
import shutil
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
DEFAULT_TARGET = SOURCE.parent / "portfolio_pro_freund"

# Diese Namen/Ordner werden NIE mitkopiert (persönliche Daten, Secrets, Caches, Tooling).
EXCLUDE = {
    "portfolio.db", ".env", "__pycache__", ".pytest_cache",
    ".git", ".claude", ".impeccable", ".agents", ".vscode", ".idea",
}
EXCLUDE_SUFFIXES = {".db", ".pyc"}


def _ignore(_dir, names):
    ignored = set()
    for name in names:
        if name in EXCLUDE or Path(name).suffix in EXCLUDE_SUFFIXES:
            ignored.add(name)
    return ignored


def main():
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_TARGET
    if target.exists():
        sys.exit(f"Zielordner existiert bereits: {target}\n"
                 "Bitte löschen oder einen anderen Pfad angeben.")

    shutil.copytree(SOURCE, target, ignore=_ignore)

    # Sicherheitsnetz: prüfen, dass wirklich keine persönlichen Daten mitgekommen sind.
    leaked = [p.name for p in (target / "portfolio.db", target / ".env") if p.exists()]
    leaked += [str(p.relative_to(target)) for p in target.rglob("*.db")]
    if leaked:
        shutil.rmtree(target, ignore_errors=True)
        sys.exit(f"ABBRUCH: persönliche Dateien wären mitkopiert worden: {leaked}")

    has_example = (target / ".env.example").exists()
    print(f"Saubere Kopie erstellt: {target}")
    print("Enthalten: der komplette Code, KEINE portfolio.db, KEINE .env.")
    print(f".env.example vorhanden: {'ja' if has_example else 'NEIN (bitte prüfen)'}")
    print("\nDer Empfänger startet so:")
    print("  pip install -r requirements.txt")
    print("  python -m streamlit run app.py")
    print("Beim ersten Start erscheint die Ersteinrichtung (Keys optional).")


if __name__ == "__main__":
    main()
