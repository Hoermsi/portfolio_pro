import pytest

from core import shadow


@pytest.fixture
def shadow_db(tmp_db, monkeypatch):
    """tmp_db + feste Kurse, damit Trades deterministisch sind."""
    prices = {("BTC", "crypto"): 100.0, ("SOL", "crypto"): 10.0,
              ("ENJ", "crypto"): 0.5, ("NVDA", "stock"): 120.0, ("SAP", "stock"): 200.0}
    monkeypatch.setattr(shadow, "price_eur",
                        lambda sym, at: 1.0 if at == "cash" else prices.get((sym, at)))
    return tmp_db


def test_sell_to_cash(shadow_db):
    shadow_db.set_shadow_position("crypto", "BTC", "crypto", 2.0)
    res = shadow.apply_recommendation(
        "crypto",
        {"aktion": "verkaufen", "symbol": "BTC", "asset_type": "crypto",
         "anteil_pct": 50, "begruendung": "test"})
    assert res["status"] == "ok"
    assert shadow_db.get_shadow_quantity("crypto", "BTC", "crypto") == 1.0
    # 1 BTC * 100 EUR * (1 - 0,25%) = 99.75 Cash
    assert abs(shadow.cash_balance("crypto") - 99.75) < 1e-9
    assert len(shadow_db.list_shadow_log("crypto")) == 1


def test_buy_from_cash(shadow_db):
    shadow_db.set_shadow_position("crypto", "CASH", "cash", 1000.0)
    res = shadow.apply_recommendation(
        "crypto",
        {"aktion": "kaufen", "symbol": "SOL", "asset_type": "crypto",
         "anteil_pct": 100, "begruendung": "test"})
    assert res["status"] == "ok"
    # 1000 * (1-0,25%) / 10 = 99.75 SOL
    assert abs(shadow_db.get_shadow_quantity("crypto", "SOL", "crypto") - 99.75) < 1e-9
    assert shadow.cash_balance("crypto") == 0.0


def test_reallocate_full(shadow_db):
    shadow_db.set_shadow_position("crypto", "ENJ", "crypto", 1000.0)
    res = shadow.apply_recommendation(
        "crypto",
        {"aktion": "umschichten", "symbol": "ENJ", "asset_type": "crypto",
         "ziel_symbol": "SOL", "ziel_asset_type": "crypto",
         "anteil_pct": 100, "begruendung": "ENJ raus, SOL rein"})
    assert res["status"] == "ok"
    assert shadow_db.get_shadow_quantity("crypto", "ENJ", "crypto") == 0.0
    # 1000 * 0.5 = 500 EUR, *(1-0,25%) = 498.75, /10 = 49.875 SOL
    assert abs(shadow_db.get_shadow_quantity("crypto", "SOL", "crypto") - 49.875) < 1e-9


def test_hold_only_logs(shadow_db):
    shadow_db.set_shadow_position("crypto", "BTC", "crypto", 1.0)
    res = shadow.apply_recommendation(
        "crypto",
        {"aktion": "halten", "symbol": "BTC", "asset_type": "crypto",
         "begruendung": "läuft gut"})
    assert res["status"] == "ok"
    assert shadow_db.get_shadow_quantity("crypto", "BTC", "crypto") == 1.0
    log = shadow_db.list_shadow_log("crypto")
    assert len(log) == 1 and log[0]["aktion"] == "halten"


def test_sell_without_holding_skips(shadow_db):
    res = shadow.apply_recommendation(
        "crypto",
        {"aktion": "verkaufen", "symbol": "BTC", "asset_type": "crypto",
         "anteil_pct": 100, "begruendung": "test"})
    assert res["status"] == "skip"
    # Empfehlung wird trotzdem als (nicht angewendet) protokolliert
    recs = shadow_db.list_recommendations("crypto")
    assert len(recs) == 1 and recs[0]["angewendet"] == 0


def test_cross_type_trade_rejected(shadow_db):
    """Aktien-Empfehlung im Krypto-Depot muss verworfen werden."""
    shadow_db.set_shadow_position("crypto", "CASH", "cash", 1000.0)
    res = shadow.apply_recommendation(
        "crypto",
        {"aktion": "kaufen", "symbol": "SAP", "asset_type": "stock",
         "anteil_pct": 100, "begruendung": "Aktie ins Krypto-Depot"})
    assert res["status"] == "skip"
    # Cash unangetastet, keine SAP-Position entstanden
    assert shadow.cash_balance("crypto") == 1000.0
    assert shadow_db.get_shadow_quantity("crypto", "SAP", "stock") == 0.0


def test_scopes_are_isolated(shadow_db):
    """Krypto- und Aktien-Depot teilen keine Positionen, kein Cash, keine Logs."""
    shadow_db.set_shadow_position("crypto", "CASH", "cash", 500.0)
    shadow_db.set_shadow_position("stock", "CASH", "cash", 800.0)
    shadow.apply_recommendation(
        "crypto", {"aktion": "kaufen", "symbol": "BTC", "asset_type": "crypto",
                   "anteil_pct": 100, "begruendung": "x"})
    shadow.apply_recommendation(
        "stock", {"aktion": "kaufen", "symbol": "SAP", "asset_type": "stock",
                  "anteil_pct": 100, "begruendung": "y"})
    # Jedes Depot sieht nur seine eigene Position
    assert shadow_db.get_shadow_quantity("crypto", "BTC", "crypto") > 0
    assert shadow_db.get_shadow_quantity("stock", "SAP", "stock") > 0
    assert shadow_db.get_shadow_quantity("stock", "BTC", "crypto") == 0.0
    assert len(shadow_db.list_shadow_log("crypto")) == 1
    assert len(shadow_db.list_shadow_log("stock")) == 1
    # Reset des einen lässt den anderen unberührt
    shadow.reset("crypto")
    assert shadow_db.shadow_positions("crypto") == []
    assert shadow_db.get_shadow_quantity("stock", "SAP", "stock") > 0


def test_init_aggregates_duplicate_symbols(tmp_db, monkeypatch):
    # NVDA in zwei Kategorien -> Aktien-KI-Depot muss die Menge summieren
    tmp_db.save_position("NVDA", "stock", 10, 100, category="Standard")
    tmp_db.save_position("NVDA", "stock", 10, 90, category="Flatex-Import")
    tmp_db.save_position("BTC", "crypto", 0.5, 30000, category="Standard")

    from core.models import evaluate
    monkeypatch.setattr("core.portfolio.value_position",
                        lambda p: evaluate(p, 200.0 if p.asset_type == "stock" else 40000.0, 1.0))
    shadow.init_from_real("stock")
    assert shadow.db.get_shadow_quantity("stock", "NVDA", "stock") == 20  # 10+10
    # Krypto liegt NICHT im Aktien-Depot
    assert shadow.db.get_shadow_quantity("stock", "BTC", "crypto") == 0.0


def test_init_skips_unpriceable_positions(tmp_db, monkeypatch):
    """Position ohne Kurs darf die Baseline nicht verzerren -> wird ausgelassen+gemeldet."""
    tmp_db.save_position("NVDA", "stock", 10, 100, category="Standard")
    tmp_db.save_position("XYZ", "stock", 5, 50, category="Standard")  # kein Kurs

    from core.models import evaluate
    def fake_value(p):
        price = 200.0 if p.symbol == "NVDA" else None
        return evaluate(p, price, 1.0)
    monkeypatch.setattr("core.portfolio.value_position", fake_value)

    info = shadow.init_from_real("stock")
    assert info["skipped"] == ["XYZ"]
    assert info["shadow_start"] == 2000.0                     # nur NVDA (10*200)
    assert shadow.db.get_shadow_quantity("stock", "NVDA", "stock") == 10
    assert shadow.db.get_shadow_quantity("stock", "XYZ", "stock") == 0.0
    # echter Start-Snapshot == Schatten-Start (faire Baseline)
    real_snap = [s for s in tmp_db.list_snapshots() if s["asset_type"] == "stock"]
    assert real_snap and abs(real_snap[0]["total_value_eur"] - 2000.0) < 1e-9


def test_init_maps_eur_holding_to_cash(tmp_db, monkeypatch):
    """EUR-Bestand (Kraken-Cash) wird im KI-Depot als Cash geführt."""
    tmp_db.save_position("BTC", "crypto", 1.0, 30000, category="Kraken")
    tmp_db.save_position("EUR", "crypto", 500.0, 1.0, category="Kraken")

    # valued_positions('crypto') bewertet über die Batch-Preisfunktion
    monkeypatch.setattr("data.crypto.get_prices_eur",
                        lambda syms: {"BTC": 40000.0, "EUR": 1.0})

    info = shadow.init_from_real("crypto")
    assert abs(info["shadow_start"] - 40500.0) < 1e-9        # 40000 BTC + 500 Cash
    assert shadow.cash_balance("crypto") == 500.0            # EUR -> CASH/cash
    assert shadow.db.get_shadow_quantity("crypto", "EUR", "crypto") == 0.0
    assert shadow.db.get_shadow_quantity("crypto", "BTC", "crypto") == 1.0


def test_comparison_uses_only_own_scope_snapshots(tmp_db):
    import json
    tmp_db.set_meta("shadow_start_crypto",
                    json.dumps({"date": "2026-07-10", "real_start": 1000.0, "shadow_start": 1000.0}))
    # echte Snapshots: crypto UND stock am selben Tag - nur crypto darf zählen
    tmp_db.save_snapshot("crypto", 900.0, "2026-07-09")   # vor Start -> ignorieren
    tmp_db.save_snapshot("crypto", 1000.0, "2026-07-10")
    tmp_db.save_snapshot("crypto", 1050.0, "2026-07-11")
    tmp_db.save_snapshot("stock", 99999.0, "2026-07-10")  # darf Vergleich NICHT verzerren
    tmp_db.save_shadow_snapshot("crypto", 1000.0, "2026-07-10")
    tmp_db.save_shadow_snapshot("crypto", 1100.0, "2026-07-11")

    comp = shadow.comparison_df("crypto")
    assert comp is not None
    assert comp.index.min().strftime("%Y-%m-%d") == "2026-07-10"
    assert abs(comp.iloc[0]["Echt"] - 100.0) < 1e-9
    assert abs(comp.iloc[0]["KI"] - 100.0) < 1e-9
    assert abs(comp.iloc[-1]["KI"] - 110.0) < 1e-9    # +10 %
    assert abs(comp.iloc[-1]["Echt"] - 105.0) < 1e-9  # +5 %, stock-Snapshot ignoriert


def test_valued_shadow_and_total(shadow_db):
    shadow_db.set_shadow_position("crypto", "BTC", "crypto", 2.0)
    shadow_db.set_shadow_position("crypto", "CASH", "cash", 50.0)
    import core.shadow as sh
    from data import crypto as crypto_data
    monkey = {"BTC": 100.0}
    orig = crypto_data.get_prices_eur
    crypto_data.get_prices_eur = lambda syms: {s: monkey.get(s) for s in syms}
    try:
        vals = sh.valued_shadow("crypto")
        total = sh.total_value("crypto", vals)
    finally:
        crypto_data.get_prices_eur = orig
    assert abs(total - 250.0) < 1e-9   # 2*100 + 50 Cash
