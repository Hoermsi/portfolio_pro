"""Flatex-Depotbestand-CSV importieren (portiert aus V1).

Unterstützt getrennte Konten über die Ziel-Kategorie (z.B. 'Flatex-Aktien',
'Flatex-ETF'). Bei replace=True wird der bisherige Bestand der Kategorie ersetzt,
sodass ein Konto nach dem Upload exakt dem neuen Depotbestand entspricht.
"""
import pandas as pd

from core import db
from data import stocks as stock_data


def preview_csv(file_obj) -> tuple[bool, list[dict], list[str]]:
    """Liest einen Flatex-Export ohne Datenbankänderung für die Importvorschau."""
    try:
        file_obj.seek(0)
        df = pd.read_csv(file_obj, sep=";", encoding="latin-1")
    except Exception as e:
        return False, [], [f"CSV nicht lesbar: {e}"]
    finally:
        try:
            file_obj.seek(0)
        except Exception:
            pass

    if "ISIN" not in df.columns or "Stk./Nominale" not in df.columns:
        return False, [], ["Spalten 'ISIN' oder 'Stk./Nominale' nicht gefunden."]

    symbol_col = next((c for c in ("Symbol", "Ticker", "WKN") if c in df.columns), None)
    name_col = next((c for c in ("Bezeichnung", "Name", "Wertpapier", "Wertpapierbezeichnung")
                     if c in df.columns), None)
    parsed = []
    for _, row in df.iterrows():
        isin = str(row["ISIN"]).strip()
        if not isin or isin.lower() == "nan":
            continue
        qty_raw = str(row["Stk./Nominale"]).replace(".", "").replace(",", ".")
        price_raw = str(row.get("Einstandskurs", "0")).replace(".", "").replace(",", ".")
        try:
            qty, price = float(qty_raw), float(price_raw)
        except ValueError:
            continue
        if qty <= 0:
            continue
        symbol = (str(row[symbol_col]).strip().upper()
                  if symbol_col and str(row[symbol_col]).strip() else isin)
        name = ""
        if name_col:
            raw_name = str(row[name_col]).strip()
            if raw_name and raw_name.lower() != "nan":
                name = raw_name
        parsed.append({"symbol": symbol, "quantity": qty, "buy_price": price, "name": name})
    return True, parsed, []


def import_csv(file_obj, category: str = "Flatex-Import",
               replace: bool = True) -> tuple[bool, int, list[str]]:
    """Importiert einen Flatex-Depotbestand als Aktien-Positionen in `category`.

    Gibt (erfolg, anzahl, nicht_aufloesbare_symbole) zurück. Nicht auflösbare
    ISINs werden trotzdem gespeichert, aber zurückgemeldet. Erst wird die Datei
    vollständig geparst - fehlen Pflichtspalten, wird NICHTS gelöscht/geschrieben.
    """
    ok, parsed, errors = preview_csv(file_obj)
    if not ok:
        return False, 0, errors

    # 2) Erst jetzt (nach erfolgreichem Parsen) das Konto ersetzen
    if replace:
        db.delete_positions_by_category("stock", category)

    unresolved = []
    for row in parsed:
        symbol, qty, price, name = row["symbol"], row["quantity"], row["buy_price"], row["name"]
        if not stock_data.resolves(symbol):
            unresolved.append(symbol)
        db.save_position(symbol, "stock", qty, price, category=category,
                         source="flatex", name=name)

    return True, len(parsed), unresolved
