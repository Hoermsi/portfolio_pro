"""Tests für analysis/exit_ranking.py (Marktdaten vollständig gemockt)."""
import pytest

from analysis import alerts, exit_ranking
from data import crypto as crypto_data


def _row(name, ath_pct, d7, d30, rang, kurs=1.0):
    return {
        "name": name, "rang": rang, "kurs_eur": kurs,
        "ath_abstand_pct": ath_pct,  # negativ = unter ATH (CoinGecko-Konvention)
        "aenderung_7d_pct": d7, "aenderung_30d_pct": d30,
    }


def _mock_batch(monkeypatch, rows: dict, btc_30d=0.0):
    """rows: {symbol: _row(...)}; BTC wird automatisch ergänzt."""
    data = dict(rows)
    data["BTC"] = _row("Bitcoin", -10.0, 1.0, btc_30d, 1, kurs=50000.0)

    def fake_batch(symbols):
        return {s: data.get(s, {}) for s in symbols}

    monkeypatch.setattr(crypto_data, "get_market_data_batch", fake_batch)


def _mock_rsi(monkeypatch, rsi_by_symbol: dict):
    monkeypatch.setattr(
        alerts, "asset_metrics",
        lambda symbol, asset_type: {"rsi": rsi_by_symbol.get(symbol), "price_eur": None, "day_pct": None},
    )


def test_minmax_score_handles_constant_values():
    scored = exit_ranking._minmax_score({"A": 5.0, "B": 5.0, "C": 5.0})
    assert scored == {"A": 50.0, "B": 50.0, "C": 50.0}


def test_minmax_score_handles_single_value():
    scored = exit_ranking._minmax_score({"A": 3.0})
    assert scored == {"A": 50.0}


def test_minmax_score_preserves_none():
    scored = exit_ranking._minmax_score({"A": 1.0, "B": 2.0, "C": None})
    assert scored["C"] is None
    assert scored["A"] < scored["B"]


def test_minmax_score_orders_correctly():
    scored = exit_ranking._minmax_score({"A": 0.0, "B": 50.0, "C": 100.0})
    assert scored["A"] == 0.0
    assert scored["B"] == 50.0
    assert scored["C"] == 100.0


def test_exit_ranking_weakest_coin_ranked_first(monkeypatch):
    """STRONG schlägt BTC deutlich, ist nah am ATH, hoher Marktkap.-Rang
    (liquide) -> niedriger exit_score. WEAK das Gegenteil -> muss oben stehen."""
    rows = {
        "STRONG": _row("Strong Coin", ath_pct=-5.0, d7=10.0, d30=40.0, rang=5),
        "WEAK": _row("Weak Coin", ath_pct=-95.0, d7=-20.0, d30=-60.0, rang=800),
    }
    _mock_batch(monkeypatch, rows, btc_30d=5.0)
    _mock_rsi(monkeypatch, {"STRONG": 75.0, "WEAK": 25.0})

    ranking = exit_ranking.exit_ranking({"STRONG": 10.0, "WEAK": 10.0})
    assert [r["symbol"] for r in ranking] == ["WEAK", "STRONG"]
    assert ranking[0]["exit_score"] > ranking[1]["exit_score"]
    assert ranking[0]["coverage_pct"] == 100.0


def test_exit_ranking_computes_value_eur(monkeypatch):
    rows = {"XYZ": _row("XYZ", ath_pct=-50.0, d7=0.0, d30=0.0, rang=100, kurs=2.0)}
    _mock_batch(monkeypatch, rows)
    _mock_rsi(monkeypatch, {"XYZ": 50.0})

    ranking = exit_ranking.exit_ranking({"XYZ": 10.0})
    assert ranking[0]["value_eur"] == pytest.approx(20.0)


def test_exit_ranking_missing_market_data_still_returns_row(monkeypatch):
    """Symbol ohne Treffer im Batch (z.B. CoinGecko kennt es nicht) fällt aus
    der Gewichtung (coverage_pct 0), erscheint aber weiterhin in der Liste -
    nicht stillschweigend verschluckt."""
    _mock_batch(monkeypatch, {})  # kein Treffer für UNKNOWN
    _mock_rsi(monkeypatch, {})

    ranking = exit_ranking.exit_ranking({"UNKNOWN": 5.0})
    assert len(ranking) == 1
    assert ranking[0]["exit_score"] is None
    assert ranking[0]["coverage_pct"] == 0.0
    assert ranking[0]["value_eur"] is None


def test_exit_ranking_empty_positions_returns_empty():
    assert exit_ranking.exit_ranking({}) == []


def test_held_symbols_excludes_eur_and_aggregates(tmp_db):
    tmp_db.save_position("BTC", "crypto", 1.0, 10000.0, category="A")
    tmp_db.save_position("BTC", "crypto", 0.5, 10000.0, category="B")
    tmp_db.save_position("EUR", "crypto", 500.0, 1.0, category="A")

    held = exit_ranking.held_symbols()
    assert held == {"BTC": 1.5}


# --- sell_list() ---

def _ranking_fixture():
    return [
        {"symbol": "WEAK", "value_eur": 100.0, "exit_score": 90.0},
        {"symbol": "MID", "value_eur": 100.0, "exit_score": 50.0},
        {"symbol": "STRONG", "value_eur": 100.0, "exit_score": 10.0},
    ]


def test_sell_list_stops_once_target_reached():
    result = exit_ranking.sell_list(33.0, ranking=_ranking_fixture())
    assert [r["symbol"] for r in result["to_sell"]] == ["WEAK"]
    assert result["sell_pct_actual"] == pytest.approx(100 / 3, rel=0.01)
    assert {r["symbol"] for r in result["keep"]} == {"MID", "STRONG"}


def test_sell_list_full_liquidation_at_100_pct():
    result = exit_ranking.sell_list(100.0, ranking=_ranking_fixture())
    assert len(result["to_sell"]) == 3
    assert result["keep"] == []
    assert result["sell_pct_actual"] == pytest.approx(100.0)


def test_sell_list_two_of_three_for_66_pct():
    result = exit_ranking.sell_list(66.0, ranking=_ranking_fixture())
    assert [r["symbol"] for r in result["to_sell"]] == ["WEAK", "MID"]


def test_sell_list_empty_ranking_returns_zeroed_result():
    result = exit_ranking.sell_list(50.0, ranking=[])
    assert result["to_sell"] == []
    assert result["total_value_eur"] == 0.0


def test_sell_list_skips_rows_without_value():
    ranking = [
        {"symbol": "NOVAL", "value_eur": None, "exit_score": 99.0},
        {"symbol": "OK", "value_eur": 100.0, "exit_score": 10.0},
    ]
    result = exit_ranking.sell_list(100.0, ranking=ranking)
    assert [r["symbol"] for r in result["to_sell"]] == ["OK"]
    # NOVAL hat keinen Wert -> weder verkauft noch sinnvoll "gehalten" bewertbar,
    # bleibt aber in keep, damit es in der UI nicht verschwindet.
    assert any(r["symbol"] == "NOVAL" for r in result["keep"])


# --- sell_list() mit Zyklus-Basis (A1) ---

def test_sell_list_without_basis_matches_todays_behavior():
    """Regressionsschutz: basis=None (Default) verhält sich exakt wie vorher -
    keine neuen Felder mit Werten befüllt."""
    result = exit_ranking.sell_list(33.0, ranking=_ranking_fixture())
    assert result["basis_value_eur"] is None
    assert result["already_sold_value_eur"] is None
    assert result["basis_price_unavailable"] == []


def test_sell_list_stage2_plans_only_remaining_amount_after_stage1_sold(monkeypatch):
    """Kernfix: Stufe 2 (66%) darf NICHT 66% des jetzt (nach Stufe-1-Verkauf)
    kleineren Depots verkaufen, sondern nur die verbleibenden Prozentpunkte
    des Zyklus-Ausgangswerts. Ohne den Fix müsste hier zusätzlich STRONG
    verkauft werden (66% von 200 aktuell = 132 > 100 von MID allein)."""
    basis = {"WEAK": 10.0, "MID": 10.0, "STRONG": 10.0}  # je 10 Stück a 10 EUR = 300 EUR Basis
    # WEAK wurde bereits komplett verkauft (Stufe 1) - nicht mehr im Bestand/Ranking.
    ranking = [
        {"symbol": "MID", "quantity": 10.0, "price_eur": 10.0, "value_eur": 100.0, "exit_score": 50.0},
        {"symbol": "STRONG", "quantity": 10.0, "price_eur": 10.0, "value_eur": 100.0, "exit_score": 10.0},
    ]
    monkeypatch.setattr(crypto_data, "get_market_data_batch",
                        lambda symbols: {s: {"kurs_eur": 10.0} for s in symbols})

    result = exit_ranking.sell_list(66.0, ranking=ranking, basis=basis)

    assert result["basis_value_eur"] == pytest.approx(300.0)
    assert result["already_sold_value_eur"] == pytest.approx(100.0)
    # Ziel: 66% von 300 = 198, abzüglich bereits verkaufter 100 EUR = 98 EUR Rest.
    assert [r["symbol"] for r in result["to_sell"]] == ["MID"]  # 100 EUR deckt die 98 EUR Rest
    assert result["sell_pct_actual"] == pytest.approx((100.0 + 100.0) / 300.0 * 100, rel=0.01)


def test_sell_list_flags_missing_basis_price(monkeypatch):
    basis = {"GONE": 5.0}
    ranking = [{"symbol": "MID", "quantity": 10.0, "price_eur": 10.0, "value_eur": 100.0, "exit_score": 50.0}]
    monkeypatch.setattr(crypto_data, "get_market_data_batch", lambda symbols: {s: {} for s in symbols})

    result = exit_ranking.sell_list(66.0, ranking=ranking, basis=basis)
    assert result["basis_value_eur"] is None
    assert "GONE" in result["basis_price_unavailable"]
    # Kein bewertbares Basis-Symbol -> Rückfall auf "ohne Basis"-Ziel (66% von 100 aktuell).
    assert [r["symbol"] for r in result["to_sell"]] == ["MID"]


# --- sell_list() Teilverkauf der letzten Position (A1) ---

def test_sell_list_overshoots_without_partial_last():
    ranking = [
        {"symbol": "WEAK", "quantity": 50.0, "price_eur": 2.0, "value_eur": 100.0, "exit_score": 90.0},
        {"symbol": "MID", "quantity": 20.0, "price_eur": 5.0, "value_eur": 100.0, "exit_score": 50.0},
    ]
    result = exit_ranking.sell_list(40.0, ranking=ranking, partial_last=False)
    assert result["sell_value_eur"] == pytest.approx(100.0)  # ganze Position, ueber dem 80-EUR-Ziel


def test_sell_list_partial_last_trim_math():
    ranking = [
        {"symbol": "WEAK", "quantity": 50.0, "price_eur": 2.0, "value_eur": 100.0, "exit_score": 90.0},
        {"symbol": "MID", "quantity": 20.0, "price_eur": 5.0, "value_eur": 100.0, "exit_score": 50.0},
    ]
    # Ziel 40% von 200 = 80 EUR - WEAK allein (100) würde ohne Trim um 20 EUR überschießen.
    result = exit_ranking.sell_list(40.0, ranking=ranking, partial_last=True)
    assert len(result["to_sell"]) == 1
    trimmed = result["to_sell"][0]
    assert trimmed["symbol"] == "WEAK"
    assert trimmed["value_eur"] == pytest.approx(80.0)
    assert trimmed["quantity"] == pytest.approx(40.0)  # 50 * (80/100)
    assert result["sell_value_eur"] == pytest.approx(80.0)


def test_sell_list_partial_last_not_applied_when_exact():
    """Trifft der Greedy-Walk das Ziel exakt, gibt es nichts zu kürzen."""
    ranking = [
        {"symbol": "WEAK", "quantity": 10.0, "price_eur": 10.0, "value_eur": 100.0, "exit_score": 90.0},
        {"symbol": "MID", "quantity": 10.0, "price_eur": 10.0, "value_eur": 100.0, "exit_score": 50.0},
    ]
    result = exit_ranking.sell_list(50.0, ranking=ranking, partial_last=True)  # 50% von 200 = 100 = WEAK exakt
    assert [r["symbol"] for r in result["to_sell"]] == ["WEAK"]
    assert result["sell_value_eur"] == pytest.approx(100.0)
    assert result["to_sell"][0]["quantity"] == pytest.approx(10.0)  # unverändert, kein Trim nötig
