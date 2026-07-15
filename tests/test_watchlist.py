"""Tests für Watchlist-DB und Alarm-Auswertung (ohne Netzwerkzugriff)."""
from analysis import alerts


def test_watchlist_crud(tmp_db):
    db = tmp_db
    wid = db.add_watchlist("NVDA", "stock", "NVIDIA")
    assert wid > 0
    # idempotent: gleiches Symbol legt keinen zweiten Eintrag an
    db.add_watchlist("NVDA", "stock")
    entries = db.list_watchlist("stock")
    assert len(entries) == 1
    assert entries[0]["name"] == "NVIDIA"

    db.update_watchlist_alert(wid, target_above=200.0, target_below=None,
                              day_move_pct=5.0, rsi_alert=True)
    e = db.list_watchlist("stock")[0]
    assert e["target_above"] == 200.0
    assert e["target_below"] is None
    assert e["day_move_pct"] == 5.0
    assert e["rsi_alert"] == 1

    db.remove_watchlist(wid)
    assert db.list_watchlist("stock") == []


def test_list_watchlist_filters_by_type(tmp_db):
    db = tmp_db
    db.add_watchlist("NVDA", "stock")
    db.add_watchlist("BTC", "crypto")
    assert {e["symbol"] for e in db.list_watchlist("stock")} == {"NVDA"}
    assert {e["symbol"] for e in db.list_watchlist("crypto")} == {"BTC"}
    assert len(db.list_watchlist()) == 2


def test_evaluate_watchlist_triggers(tmp_db, monkeypatch):
    db = tmp_db
    wid = db.add_watchlist("NVDA", "stock", "NVIDIA")
    db.update_watchlist_alert(wid, target_above=180.0, target_below=None,
                              day_move_pct=3.0, rsi_alert=True)

    monkeypatch.setattr(alerts, "asset_metrics",
                        lambda s, t: {"price_eur": 190.0, "day_pct": 4.5, "rsi": 75.0})
    result = alerts.evaluate_watchlist()
    assert len(result) == 1
    kinds = {t["kind"] for t in result[0]["triggers"]}
    assert kinds == {"above", "move", "rsi"}
    texts = [t["text"] for t in result[0]["triggers"]]
    assert any("über Zielkurs" in x for x in texts)
    assert any("überkauft" in x for x in texts)


def test_metrics_for_parallel(tmp_db, monkeypatch):
    """metrics_for holt Kennzahlen für alle Einträge und mappt sie auf die id."""
    db = tmp_db
    id1 = db.add_watchlist("NVDA", "stock")
    id2 = db.add_watchlist("BTC", "crypto")
    monkeypatch.setattr(alerts, "asset_metrics",
                        lambda s, t: {"price_eur": 42.0 if s == "NVDA" else 99.0,
                                      "day_pct": None, "rsi": None})
    m = alerts.metrics_for(db.list_watchlist())
    assert m[id1]["price_eur"] == 42.0
    assert m[id2]["price_eur"] == 99.0


def test_acknowledge_hides_until_threshold_changes(tmp_db, monkeypatch):
    """'Gesehen' unterdrückt den Alarm; eine neue Schwelle lässt ihn wieder feuern."""
    db = tmp_db
    wid = db.add_watchlist("NVDA", "stock")
    db.update_watchlist_alert(wid, target_above=180.0, target_below=None,
                              day_move_pct=None, rsi_alert=False)
    monkeypatch.setattr(alerts, "asset_metrics",
                        lambda s, t: {"price_eur": 190.0, "day_pct": None, "rsi": None})

    result = alerts.evaluate_watchlist()
    assert len(result) == 1
    alerts.acknowledge(wid, result[0]["triggers"])
    assert alerts.evaluate_watchlist() == []            # quittiert -> weg

    db.update_watchlist_alert(wid, target_above=185.0, target_below=None,
                              day_move_pct=None, rsi_alert=False)
    refired = alerts.evaluate_watchlist()               # neue Schwelle -> wieder da
    assert len(refired) == 1
    assert refired[0]["triggers"][0]["threshold"] == 185.0


def test_evaluate_watchlist_no_trigger(tmp_db, monkeypatch):
    db = tmp_db
    wid = db.add_watchlist("NVDA", "stock")
    db.update_watchlist_alert(wid, target_above=500.0, target_below=None,
                              day_move_pct=10.0, rsi_alert=True)
    monkeypatch.setattr(alerts, "asset_metrics",
                        lambda s, t: {"price_eur": 190.0, "day_pct": 1.0, "rsi": 50.0})
    assert alerts.evaluate_watchlist() == []


def test_evaluate_skips_unconfigured(tmp_db, monkeypatch):
    db = tmp_db
    db.add_watchlist("NVDA", "stock")  # keine Schwellen gesetzt

    def _boom(*a, **k):
        raise AssertionError("asset_metrics darf ohne konfigurierten Alarm nicht aufgerufen werden")

    monkeypatch.setattr(alerts, "asset_metrics", _boom)
    assert alerts.evaluate_watchlist() == []
