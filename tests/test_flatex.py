import io

import pytest

from data import flatex


def _csv(rows: list[tuple[str, str, str]]) -> io.BytesIO:
    """Baut eine Flatex-artige CSV (ISIN;Stk./Nominale;Einstandskurs) im Speicher."""
    lines = ["ISIN;Stk./Nominale;Einstandskurs"]
    for isin, stk, einstand in rows:
        lines.append(f"{isin};{stk};{einstand}")
    return io.BytesIO("\n".join(lines).encode("latin-1"))


def _csv_with_name(rows: list[tuple[str, str, str, str]]) -> io.BytesIO:
    """CSV mit Bezeichnung-Spalte (wie im echten Flatex-Export)."""
    lines = ["ISIN;Bezeichnung;Stk./Nominale;Einstandskurs"]
    for isin, name, stk, einstand in rows:
        lines.append(f"{isin};{name};{stk};{einstand}")
    return io.BytesIO("\n".join(lines).encode("latin-1"))


@pytest.fixture(autouse=True)
def no_resolve(monkeypatch):
    # yfinance-Auflösung im Test deaktivieren (kein Netz)
    monkeypatch.setattr("data.stocks.resolves", lambda s: True)


def test_delete_positions_by_category(tmp_db):
    tmp_db.save_position("NVDA", "stock", 10, 100, category="Flatex-Aktien")
    tmp_db.save_position("SAP", "stock", 5, 90, category="Flatex-Aktien")
    tmp_db.save_position("IWDA", "stock", 20, 80, category="Flatex-ETF")
    n = tmp_db.delete_positions_by_category("stock", "Flatex-Aktien")
    assert n == 2
    remaining = [p.symbol for p in tmp_db.list_positions("stock")]
    assert remaining == ["IWDA"]


def test_import_into_separate_accounts(tmp_db):
    ok1, n1, _ = flatex.import_csv(_csv([("US67066G1040", "10", "100,00")]),
                                   category="Flatex-Aktien")
    ok2, n2, _ = flatex.import_csv(_csv([("IE00B4L5Y983", "20", "80,00")]),
                                   category="Flatex-ETF")
    assert ok1 and ok2 and n1 == 1 and n2 == 1
    cats = {p.category for p in tmp_db.list_positions("stock")}
    assert cats == {"Flatex-Aktien", "Flatex-ETF"}


def test_reupload_replaces_only_that_account(tmp_db):
    # Erst-Upload beider Konten
    flatex.import_csv(_csv([("US67066G1040", "10", "100,00"),
                            ("US0378331005", "5", "150,00")]), category="Flatex-Aktien")
    flatex.import_csv(_csv([("IE00B4L5Y983", "20", "80,00")]), category="Flatex-ETF")

    # Aktien-Konto neu: eine Position verkauft (nur noch 1 Zeile), Menge geändert
    ok, n, _ = flatex.import_csv(_csv([("US67066G1040", "8", "100,00")]),
                                 category="Flatex-Aktien", replace=True)
    assert ok and n == 1

    aktien = [p for p in tmp_db.list_positions("stock") if p.category == "Flatex-Aktien"]
    etf = [p for p in tmp_db.list_positions("stock") if p.category == "Flatex-ETF"]
    # Aktien-Konto ersetzt: nur noch die eine Position mit neuer Menge
    assert len(aktien) == 1 and aktien[0].quantity == 8
    # ETF-Konto unberührt
    assert len(etf) == 1 and etf[0].quantity == 20


def test_import_takes_name_from_bezeichnung_column(tmp_db):
    ok, n, _ = flatex.import_csv(
        _csv_with_name([("US67066G1040", "NVIDIA Corp.", "10", "100,00"),
                        ("IE00B4L5Y983", "iShares Core MSCI World", "20", "80,00")]),
        category="Flatex-Aktien")
    assert ok and n == 2
    by_symbol = {p.symbol: p.name for p in tmp_db.list_positions("stock")}
    assert by_symbol["US67066G1040"] == "NVIDIA Corp."
    assert by_symbol["IE00B4L5Y983"] == "iShares Core MSCI World"


def test_set_asset_name(tmp_db):
    tmp_db.save_position("NVDA", "stock", 10, 100, category="Standard")
    tmp_db.set_asset_name("NVDA", "stock", "NVIDIA Corporation")
    assert tmp_db.list_positions("stock")[0].name == "NVIDIA Corporation"
    # leerer Name überschreibt nicht
    tmp_db.set_asset_name("NVDA", "stock", "  ")
    assert tmp_db.list_positions("stock")[0].name == "NVIDIA Corporation"


def test_missing_columns_does_not_delete(tmp_db):
    tmp_db.save_position("NVDA", "stock", 10, 100, category="Flatex-Aktien")
    bad = io.BytesIO("Foo;Bar\n1;2".encode("latin-1"))
    ok, n, err = flatex.import_csv(bad, category="Flatex-Aktien", replace=True)
    assert not ok and n == 0
    # Bestehende Position darf NICHT gelöscht worden sein
    assert len(tmp_db.list_positions("stock")) == 1
