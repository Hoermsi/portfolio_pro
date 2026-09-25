"""bot_runner.py: nur die reinen, DB-basierten Hilfsfunktionen - run_forever()
selbst (Endlosschleife, echter Prozessstart) wird hier bewusst nicht getestet,
das ist Sache von core.bot_process (Teil 2) bzw. eines manuellen Laufs."""
from datetime import datetime, timedelta, timezone
import msvcrt

import bot_runner
from core import bot_config, clock, db
from data.hyperliquid import PaperExchange
import pytest


def test_stop_requested_false_by_default(tmp_db):
    assert bot_runner._stop_requested() is False


def test_stop_requested_true_when_flag_set(tmp_db):
    db.set_meta("bot_runner_stop_requested", "1")
    assert bot_runner._stop_requested() is True


def test_sleep_with_stop_check_runs_full_duration_without_stop(monkeypatch):
    slept = []
    monkeypatch.setattr(bot_runner.time, "sleep", lambda s: slept.append(s))
    wake_reason = bot_runner._sleep_with_stop_check(12)
    assert wake_reason is None
    assert sum(slept) >= 12


def test_sleep_with_stop_check_aborts_early_when_stop_requested(tmp_db, monkeypatch):
    calls = {"n": 0}

    def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            db.set_meta("bot_runner_stop_requested", "1")

    monkeypatch.setattr(bot_runner.time, "sleep", fake_sleep)
    wake_reason = bot_runner._sleep_with_stop_check(300)  # 5 Minuten, in 5s-Schritten
    assert wake_reason == "stop"
    # Nach dem ersten 5s-Chunk (der den Stop-Wunsch setzt) muss der naechste
    # Schleifendurchlauf sofort abbrechen, nicht alle 60 Schritte durchlaufen.
    assert calls["n"] <= 2


def test_close_all_requested_false_by_default(tmp_db):
    assert bot_runner._close_all_requested() is False


def test_close_all_requested_true_when_flag_set(tmp_db):
    db.set_meta("bot_close_all_requested", "1")
    assert bot_runner._close_all_requested() is True


def test_sleep_with_stop_check_aborts_early_on_close_all_request(tmp_db, monkeypatch):
    """Der Aufwach-Grund muss "close_all" sein, nicht bloss ein generisches
    True - eine fruehere Fassung gab hier nur True/False zurueck, der
    Aufrufer (run_forever) konnte "Alle Positionen schliessen" nicht von
    einem echten Stop-Wunsch unterscheiden und beendete den Runner mit allen
    Positionen offen."""
    calls = {"n": 0}

    def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            db.set_meta("bot_close_all_requested", "1")

    monkeypatch.setattr(bot_runner.time, "sleep", fake_sleep)
    wake_reason = bot_runner._sleep_with_stop_check(300)
    assert wake_reason == "close_all"
    assert calls["n"] <= 2


def test_maybe_close_all_noop_without_flag(tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(bot_runner.bot, "close_all_positions", lambda ex, reason="": calls.append(ex) or [])
    bot_runner._maybe_close_all("fake-exchange")
    assert calls == []


def test_maybe_close_all_calls_bot_and_clears_flag(tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(bot_runner.bot, "close_all_positions",
                        lambda ex, reason="": calls.append((ex, reason)) or
                        [{"symbol": "BTC", "result": {"status": "filled"}}])
    db.set_meta("bot_close_all_requested", "1")

    bot_runner._maybe_close_all("fake-exchange")

    assert calls == [("fake-exchange", "manual_close_all")]
    assert db.get_meta("bot_close_all_requested") is None


def test_maybe_close_all_keeps_flag_when_a_position_remains_open(tmp_db, monkeypatch):
    """Ein Fehlschlag (API-Fehler, IOC-Abbruch, Mindestgroesse) darf die
    Anfrage nicht stillschweigend als erledigt markieren - der naechste Takt
    muss es erneut versuchen, solange tatsaechlich noch etwas offen ist."""
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                leverage=1.0, stop_px=70000.0, exchange_oid="111")
    monkeypatch.setattr(bot_runner.bot, "close_all_positions",
                        lambda ex, reason="": [{"symbol": "BTC",
                                               "result": {"status": "blocked",
                                                         "reason": "Boerse nicht erreichbar"}}])
    db.set_meta("bot_close_all_requested", "1")

    bot_runner._maybe_close_all("fake-exchange")

    assert db.get_meta("bot_close_all_requested") == "1"
    assert db.get_bot_position("BTC")["status"] == "open"


def test_maybe_close_all_clears_flag_once_all_positions_are_actually_gone(tmp_db, monkeypatch):
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                leverage=1.0, stop_px=70000.0, exchange_oid="111")

    def _fake_close(ex, reason=""):
        db.close_bot_position("BTC", close_px=80000.0, realized_pnl_usd=0.0)
        return [{"symbol": "BTC", "result": {"status": "filled"}}]

    monkeypatch.setattr(bot_runner.bot, "close_all_positions", _fake_close)
    db.set_meta("bot_close_all_requested", "1")

    bot_runner._maybe_close_all("fake-exchange")

    assert db.get_meta("bot_close_all_requested") is None


def test_maybe_backfill_exchange_opened_at_noop_without_flag(tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(bot_runner.bot, "backfill_exchange_opened_at",
                        lambda ex: calls.append(ex) or [])
    bot_runner._maybe_backfill_exchange_opened_at("fake-exchange")
    assert calls == []


def test_maybe_backfill_exchange_opened_at_calls_bot_and_clears_flag(tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(bot_runner.bot, "backfill_exchange_opened_at",
                        lambda ex: calls.append(ex) or
                        [{"symbol": "ETH", "updated": True,
                          "exchange_opened_at": "2026-01-01T00:00:00"}])
    db.set_meta("bot_backfill_opened_at_requested", "1")

    bot_runner._maybe_backfill_exchange_opened_at("fake-exchange")

    assert calls == ["fake-exchange"]
    assert db.get_meta("bot_backfill_opened_at_requested") is None


def test_clear_runner_markers_removes_all_three_keys(tmp_db):
    db.set_meta("bot_runner_heartbeat", "2026-01-01T00:00:00")
    db.set_meta("bot_runner_pid", "12345")
    db.set_meta("bot_runner_stop_requested", "1")

    bot_runner._clear_runner_markers()

    assert db.get_meta("bot_runner_heartbeat") is None
    assert db.get_meta("bot_runner_pid") is None
    assert db.get_meta("bot_runner_stop_requested") is None


def test_maybe_refresh_costs_calls_bot_once_per_day(tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(bot_runner.bot, "refresh_live_costs", lambda ex: calls.append(ex) or True)

    bot_runner._maybe_refresh_costs("fake-exchange")
    bot_runner._maybe_refresh_costs("fake-exchange")  # zweiter Aufruf am selben Tag

    assert len(calls) == 1


def test_maybe_refresh_costs_does_not_touch_flows(tmp_db, monkeypatch):
    """Gebühren/Funding und Kapitalflüsse haben seit dem Review EIGENE Gates -
    _maybe_refresh_costs ruft refresh_live_flows gar nicht mehr auf."""
    calls = []
    monkeypatch.setattr(bot_runner.bot, "refresh_live_costs", lambda ex: True)
    monkeypatch.setattr(bot_runner.bot, "refresh_live_flows",
                        lambda ex: calls.append(ex) or True)

    bot_runner._maybe_refresh_costs("fake-exchange")

    assert calls == []


def test_build_exchange_uses_paper_by_default(tmp_db):
    exchange = bot_runner._build_exchange()
    assert isinstance(exchange, PaperExchange)


def test_build_exchange_goes_live_without_phase_f_shot(tmp_db, monkeypatch):
    """Der frueher verpflichtende Phase-F-Scharfschuss ist keine Sperre mehr
    (bewusste Nutzerentscheidung) - core.bot.open_position deckelt stattdessen
    die ERSTE Live-Order (core.bot_config.FIRST_LIVE_ORDER_CAP_USD)."""
    bot_config.set_dry_run(False)
    monkeypatch.setattr(bot_config, "live_trading_allowed", lambda: True)
    sentinel = object()
    monkeypatch.setattr(bot_runner, "LiveExchange", lambda: sentinel)
    assert bot_runner._build_exchange() is sentinel


def test_record_fatal_writes_error_and_timestamp(tmp_db):
    bot_runner._record_fatal("ABBRUCH: irgendwas ging schief")
    assert db.get_meta("bot_runner_last_error") == "ABBRUCH: irgendwas ging schief"
    assert db.get_meta("bot_runner_last_error_at") is not None


# --- KI-Aufsicht-Takt: aus dem Risikoregler statt fest "einmal taeglich" ---

def test_llm_review_due_when_never_run(tmp_db):
    assert bot_runner._llm_review_due() is True


def test_llm_review_not_due_right_after_a_run(tmp_db):
    db.set_meta(bot_runner._LAST_LLM_REVIEW_META_KEY, clock.iso_utc())
    assert bot_runner._llm_review_due() is False


def test_llm_review_due_after_the_risk_levels_interval(tmp_db):
    """Das Berichtsintervall ist eine eigene, vom Risikoregler unabhaengige
    Einstellung (core.bot_config.ai_report_interval_hours()) - ein Wechsel
    der Risikostufe darf daran nichts aendern."""
    bot_config.save_risk_level(10)
    stale = clock.now_utc() - timedelta(hours=bot_config.ai_report_interval_hours() + 1)
    db.set_meta(bot_runner._LAST_LLM_REVIEW_META_KEY, clock.iso_utc(stale))
    assert bot_runner._llm_review_due() is True


def test_llm_review_interval_is_selectable(tmp_db):
    bot_config.set_ai_report_interval_hours(24)
    assert bot_config.ai_report_interval_hours() == 24
    stale = clock.now_utc() - timedelta(hours=13)
    db.set_meta(bot_runner._LAST_LLM_REVIEW_META_KEY, clock.iso_utc(stale))
    assert bot_runner._llm_review_due() is False


def test_llm_review_interval_rejects_invalid_values(tmp_db):
    with pytest.raises(ValueError):
        bot_config.set_ai_report_interval_hours(18)


def test_llm_review_due_on_garbage_timestamp(tmp_db):
    db.set_meta(bot_runner._LAST_LLM_REVIEW_META_KEY, "kaputt")
    assert bot_runner._llm_review_due() is True


def test_llm_budget_resets_on_a_new_day(tmp_db):
    from datetime import date
    db.set_meta("bot_llm_cost_day", "2000-01-01")
    db.set_meta("bot_llm_cost_usd", "999")
    assert bot_runner._llm_budget_left_usd() == pytest.approx(bot_runner._LLM_DAILY_COST_CAP_USD)


def test_llm_budget_shrinks_within_the_same_day(tmp_db):
    bot_runner._record_llm_cost(0.10)
    bot_runner._record_llm_cost(0.05)
    assert bot_runner._llm_budget_left_usd() == pytest.approx(
        bot_runner._LLM_DAILY_COST_CAP_USD - 0.15)


def test_llm_budget_never_goes_negative(tmp_db):
    bot_runner._record_llm_cost(bot_runner._LLM_DAILY_COST_CAP_USD + 5.0)
    assert bot_runner._llm_budget_left_usd() == 0.0


# --- Nachtrag der Signal-Ergebnisse ---

def _outcome_candles(start="2026-08-01 00:00", n=200, drift=0.004):
    import numpy as np
    import pandas as pd
    idx = pd.date_range(start, periods=n, freq="h", tz="UTC")
    close = 100.0 * (1 + drift) ** np.arange(n)
    return pd.DataFrame({"Open": close, "High": close * 1.001,
                         "Low": close * 0.999, "Close": close,
                         "Volume": np.full(n, 1.0)}, index=idx)


def test_signal_outcome_measures_a_long_that_worked(tmp_db):
    import bot_runner
    df = _outcome_candles()
    row = {"price": 100.0, "side": "long", "ts": "2026-08-01 00:31:00+00:00", "stop_pct": 2.0}

    out = bot_runner._signal_outcome(row, df, 6)

    assert out["gross_return_pct"] > 0
    # Netto liegt IMMER unter brutto - Round-Trip-Kosten werden geschaetzt.
    assert out["net_return_pct"] < out["gross_return_pct"]
    assert out["mfe_pct"] > 0            # zwischenzeitlich im Plus
    assert out["first_r"] == "+1R"       # 2 % Ziel wird erreicht


def test_signal_outcome_flips_sign_for_shorts(tmp_db):
    """MFE/MAE gelten aus Sicht der WETTE: fuer einen Short ist ein steigender
    Kurs der schlechteste Verlauf."""
    import bot_runner
    df = _outcome_candles()
    row = {"price": 100.0, "side": "short", "ts": "2026-08-01 00:31:00+00:00", "stop_pct": 2.0}

    out = bot_runner._signal_outcome(row, df, 6)

    assert out["gross_return_pct"] < 0
    assert out["mae_pct"] < 0
    assert out["first_r"] == "-1R"


def test_signal_outcome_needs_price_side_and_ts(tmp_db):
    import bot_runner
    df = _outcome_candles()
    ts = "2026-08-01 00:31:00"
    assert bot_runner._signal_outcome(
        {"price": None, "side": "long", "ts": ts}, df, 6) is None
    assert bot_runner._signal_outcome(
        {"price": 100.0, "side": None, "ts": ts}, df, 6) is None
    assert bot_runner._signal_outcome(
        {"price": 100.0, "side": "long"}, df, 6) is None


def test_backfill_only_touches_rows_whose_window_has_passed(tmp_db):
    """Ein Signal von vor zwei Stunden darf nicht schon als 6-h-Ergebnis
    gelten - sonst stehen halbe Fenster als volle in den Daten."""
    import json
    from datetime import datetime, timedelta, timezone
    import bot_runner
    from core import db

    now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    db.add_bot_signal_logs([
        {"ts": (now - timedelta(hours=30)).isoformat(timespec="seconds"),
         "candle_ts": "2026-08-01 06:00", "symbol": "BTC", "side": "long",
         "score": 70.0, "price": 100.0, "stop_pct": 2.0, "action": "skipped"},
        {"ts": (now - timedelta(hours=2)).isoformat(timespec="seconds"),
         "candle_ts": "2026-08-02 10:00", "symbol": "BTC", "side": "long",
         "score": 70.0, "price": 100.0, "stop_pct": 2.0, "action": "skipped"},
    ])
    df = _outcome_candles()

    updated = bot_runner.backfill_signal_outcomes(
        candles_fn=lambda s, a, b: df, now=now)

    assert updated >= 1
    rows = sorted(db.list_bot_signal_log(), key=lambda r: r["ts"])
    alt, frisch = rows[0], rows[1]
    assert alt["outcome_last_h"] >= 6
    assert json.loads(alt["outcome_json"])["6"]["net_return_pct"] is not None
    assert frisch["outcome_last_h"] == 0      # Fenster noch nicht abgelaufen
    assert frisch["outcome_json"] is None


def test_backfill_is_idempotent_per_horizon(tmp_db):
    from datetime import datetime, timedelta, timezone
    import bot_runner
    from core import db

    now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    db.add_bot_signal_logs([{
        "ts": (now - timedelta(hours=30)).isoformat(timespec="seconds"),
        "candle_ts": "2026-08-01 06:00", "symbol": "BTC", "side": "long",
        "score": 70.0, "price": 100.0, "stop_pct": 2.0, "action": "skipped"}])
    df = _outcome_candles()

    first = bot_runner.backfill_signal_outcomes(candles_fn=lambda s, a, b: df, now=now)
    second = bot_runner.backfill_signal_outcomes(candles_fn=lambda s, a, b: df, now=now)

    assert first > 0
    assert second == 0        # nichts mehr offen fuer dieselben Horizonte


def test_signal_outcome_excludes_the_closed_decision_candle(tmp_db):
    """Die Kerze, aus deren Daten der Score berechnet wurde (candle_ts), lag
    bereits VOR der Entscheidung - ihr High/Low darf nicht ins Ergebnis
    einfliessen. Sonst kann ein Kursziel als "erreicht" gelten, obwohl es
    schon vor dem Einstieg beruehrt wurde."""
    import numpy as np
    import pandas as pd
    import bot_runner

    idx = pd.date_range("2026-08-01 00:00", periods=10, freq="h", tz="UTC")
    close = np.array([100.0, 100.0, 103.0, 100.5, 100.6, 100.7, 100.8,
                      100.9, 101.0, 101.1])
    high = close + 0.1
    high[2] = 110.0     # die Kerze VOR der Entscheidung spitzt weit ueber +1R
    low = close - 0.1
    df = pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close,
                       "Volume": np.ones(10)}, index=idx)
    # Entscheidung direkt zu Beginn von Kerze 3 (03:00) - Kerze 2 (mit dem
    # Spike) ist zu diesem Zeitpunkt bereits abgeschlossen und darf nicht
    # mehr zaehlen.
    row = {"price": 100.5, "side": "long", "ts": "2026-08-01 03:00:05+00:00", "stop_pct": 5.0}

    out = bot_runner._signal_outcome(row, df, 6)

    # Ohne den Fix waere das Fenster bei der Spike-Kerze gelandet (High=110,
    # weit ueber dem +1R-Ziel von 105.5) und haette sie sofort als "+1R"
    # gezaehlt. Mit dem Fix beginnt das Fenster danach, wo die Kerzen ruhig
    # bei ~100.5-101.1 liegen - das 5%-Ziel wird nie erreicht.
    assert out["first_r"] != "+1R"
    assert out["mfe_pct"] < 5.0


def test_signal_outcome_excludes_the_still_forming_candle_at_decision_time(tmp_db):
    """DER Kernfall aus dem zweiten Review: auch die Kerze, die zum
    Entscheidungszeitpunkt selbst noch LAEUFT, darf nicht mitzaehlen - eine
    Entscheidung um 02:31 darf nicht von der Kursbewegung profitieren, die
    zwischen 02:00 und 02:31 (VOR der Entscheidung) in derselben, noch
    offenen 02:00-03:00-Kerze bereits stattfand."""
    import numpy as np
    import pandas as pd
    import bot_runner

    idx = pd.date_range("2026-08-01 00:00", periods=10, freq="h", tz="UTC")
    close = np.array([100.0, 100.0, 100.5, 100.6, 100.7, 100.8, 100.9,
                      101.0, 101.1, 101.2])
    high = close + 0.1
    high[2] = 110.0   # Spike INNERHALB der zum Entscheidungszeitpunkt noch
                       # laufenden 02:00-03:00-Kerze
    low = close - 0.1
    df = pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close,
                       "Volume": np.ones(10)}, index=idx)
    # Entscheidung MITTEN in Kerze 2 (02:00-03:00) - genau die Kerze mit dem
    # Spike ist zu diesem Zeitpunkt noch nicht abgeschlossen.
    row = {"price": 100.5, "side": "long", "ts": "2026-08-01 02:31:00+00:00", "stop_pct": 5.0}

    out = bot_runner._signal_outcome(row, df, 6)

    # ceil("h") rundet 02:31 auf 03:00 auf - das Fenster beginnt danach und
    # ueberspringt die komplette Spike-Kerze, obwohl die Entscheidung
    # technisch waehrend ihrer Laufzeit fiel.
    assert out["first_r"] != "+1R"
    assert out["mfe_pct"] < 5.0


def test_signal_outcome_includes_funding_cost_in_net_label(tmp_db):
    """Das Netto-Label muss Funding einrechnen - sonst erscheint ein
    gehebelter Long trotz hoher Fundingkosten faelschlich profitabel."""
    import bot_runner

    df = _outcome_candles(drift=0.0)   # flacher Kurs -> gross_return ~ 0
    row = {"price": 100.0, "side": "long", "ts": "2026-08-01 00:31:00+00:00",
          "stop_pct": 5.0, "funding_hourly": 0.001}   # deutlich positiv

    out = bot_runner._signal_outcome(row, df, 6)

    without = bot_runner._signal_outcome(
        {**row, "funding_hourly": None}, df, 6)
    assert out["net_return_pct"] < without["net_return_pct"]


def test_maybe_refresh_costs_retries_after_failure_on_live(tmp_db, monkeypatch):
    """Schlaegt bei einer ECHTEN Boerse (mit Live-Faehigkeit) der Abruf fehl,
    darf das Tages-Gate NICHT gesetzt werden - sonst bleiben veraltete
    Gebuehren-/Funding-Werte einen vollen Tag stehen."""
    class _FakeLiveCapable:
        def recent_fills(self, since_ms):
            return []

    calls = []
    monkeypatch.setattr(bot_runner.bot, "refresh_live_costs", lambda ex: calls.append(1) or False)

    bot_runner._maybe_refresh_costs(_FakeLiveCapable())
    bot_runner._maybe_refresh_costs(_FakeLiveCapable())  # noch am selben Tag

    assert len(calls) == 2          # BEIDE Male versucht, kein Gate verbraucht
    assert db.get_meta(bot_runner._LAST_COST_REFRESH_META_KEY) is None


def test_maybe_refresh_costs_gates_once_daily_on_paper_despite_permanent_failure(tmp_db,
                                                                                 monkeypatch):
    """PaperExchange hat gar keine Live-Faehigkeit - refresh_live_costs
    liefert per Duck-Typing-Miss IMMER False. Das darf NICHT zu endlosen
    Versuchen fuehren, dafuer gibt es hier nichts zu gewinnen."""
    class _FakePaper:
        pass   # kein recent_fills - wie PaperExchange

    calls = []
    monkeypatch.setattr(bot_runner.bot, "refresh_live_costs", lambda ex: calls.append(1) or False)

    bot_runner._maybe_refresh_costs(_FakePaper())
    bot_runner._maybe_refresh_costs(_FakePaper())  # noch am selben Tag

    assert len(calls) == 1          # zweiter Aufruf wurde uebersprungen
    assert db.get_meta(bot_runner._LAST_COST_REFRESH_META_KEY) is not None


# --- Kapitalfluesse: eigenes, STUENDLICHES Gate ---

def test_maybe_refresh_flows_runs_immediately_on_first_call(tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(bot_runner.bot, "refresh_live_flows", lambda ex: calls.append(1) or True)

    bot_runner._maybe_refresh_flows("fake-exchange")

    assert len(calls) == 1
    assert db.get_meta(bot_runner._LAST_FLOWS_REFRESH_META_KEY) is not None


def test_maybe_refresh_flows_skips_within_the_same_hour(tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(bot_runner.bot, "refresh_live_flows", lambda ex: calls.append(1) or True)
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

    bot_runner._maybe_refresh_flows("fake-exchange", now=now)
    bot_runner._maybe_refresh_flows("fake-exchange", now=now + timedelta(minutes=30))

    assert len(calls) == 1


def test_maybe_refresh_flows_runs_again_after_an_hour(tmp_db, monkeypatch):
    """DER Kernfall aus dem Review: staerkere Kadenz als Gebuehren/Funding -
    stuendlich statt taeglich, weil Kapitalfluesse sicherheitsrelevant sind."""
    calls = []
    monkeypatch.setattr(bot_runner.bot, "refresh_live_flows", lambda ex: calls.append(1) or True)
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

    bot_runner._maybe_refresh_flows("fake-exchange", now=now)
    bot_runner._maybe_refresh_flows("fake-exchange", now=now + timedelta(hours=1, minutes=1))

    assert len(calls) == 2


def test_maybe_refresh_flows_retries_after_failure_on_live(tmp_db, monkeypatch):
    """Schlaegt der Abruf bei einer live-faehigen Boerse fehl, bleibt das
    Gate offen - der naechste Takt (15 Min spaeter) versucht es sofort
    wieder, nicht erst nach einer Stunde."""
    class _FakeLiveCapable:
        def deposit_withdrawal_usd(self, since_ms):
            return 0.0

    calls = []
    monkeypatch.setattr(bot_runner.bot, "refresh_live_flows", lambda ex: calls.append(1) or False)
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

    bot_runner._maybe_refresh_flows(_FakeLiveCapable(), now=now)
    bot_runner._maybe_refresh_flows(_FakeLiveCapable(), now=now + timedelta(minutes=15))

    assert len(calls) == 2
    assert db.get_meta(bot_runner._LAST_FLOWS_REFRESH_META_KEY) is None


def test_maybe_refresh_flows_gates_on_paper_despite_permanent_failure(tmp_db, monkeypatch):
    class _FakePaper:
        pass   # kein deposit_withdrawal_usd - wie PaperExchange

    calls = []
    monkeypatch.setattr(bot_runner.bot, "refresh_live_flows", lambda ex: calls.append(1) or False)
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

    bot_runner._maybe_refresh_flows(_FakePaper(), now=now)
    bot_runner._maybe_refresh_flows(_FakePaper(), now=now + timedelta(minutes=15))

    assert len(calls) == 1
    assert db.get_meta(bot_runner._LAST_FLOWS_REFRESH_META_KEY) is not None


# --- Paper-Persistenz ueber einen Prozessneustart hinweg ---

def test_seed_paper_state_restores_open_positions_and_cash(tmp_db):
    """DER Kernfall aus dem Review: offene Paper-Positionen ueberleben einen
    Prozessneustart, statt beim naechsten Zyklus als kuenstliches Phantom
    geschlossen zu werden."""
    db.set_meta("bot_start", '{"date": "2026-01-01", "equity_start_usd": 250.0, '
                             '"exchange_kind": "paper"}')
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                leverage=1.0, stop_px=70000.0, exchange_oid="paper-stop-BTC")
    db.add_bot_equity_point(equity_usd=245.0, equity_eur=230.0, cash_usd=210.5,
                            fees_cum_usd=0.5, funding_cum_usd=0.1)

    cash, positions = bot_runner._seed_paper_state()

    assert cash == 210.5
    assert set(positions) == {"BTC"}
    assert positions["BTC"].side == "long"
    assert positions["BTC"].entry_px == 80000.0
    assert positions["BTC"].size == 0.001


def test_seed_paper_state_falls_back_to_starting_equity_without_snapshot(tmp_db):
    """Allererster Start (noch kein Equity-Punkt aufgezeichnet): faellt auf
    das eingefrorene Startkapital zurueck - das bisherige Verhalten."""
    db.set_meta("bot_start", '{"date": "2026-01-01", "equity_start_usd": 300.0, '
                             '"exchange_kind": "paper"}')

    cash, positions = bot_runner._seed_paper_state()

    assert cash == 300.0
    assert positions == {}


def test_build_exchange_paper_resumes_open_positions(tmp_db):
    db.set_meta("bot_start", '{"date": "2026-01-01", "equity_start_usd": 250.0, '
                             '"exchange_kind": "paper"}')
    db.upsert_open_bot_position(symbol="ETH", side="short", size=0.5, entry_px=2500.0,
                                leverage=1.0, stop_px=2600.0, exchange_oid="paper-stop-ETH")
    db.add_bot_equity_point(equity_usd=240.0, equity_eur=220.0, cash_usd=180.0,
                            fees_cum_usd=1.0, funding_cum_usd=0.0)

    exchange = bot_runner._build_exchange()

    positions = exchange.account_state().positions
    assert len(positions) == 1
    assert positions[0].symbol == "ETH"
    assert positions[0].side == "short"


def test_restarted_paper_exchange_causes_no_phantom_or_unknown_on_reconcile(tmp_db):
    """Ende-zu-Ende: eine ueber _build_exchange() neu gebaute (also
    "neu gestartete") PaperExchange mit einer offenen DB-Position darf beim
    naechsten reconcile_with_exchange() weder einen Phantom-Schluss noch eine
    unbekannte Position ausloesen - der Neustart aendert nichts mehr am
    sichtbaren Zustand."""
    from core import bot

    db.set_meta("bot_start", '{"date": "2026-01-01", "equity_start_usd": 250.0, '
                             '"exchange_kind": "paper"}')
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                leverage=1.0, stop_px=70000.0, exchange_oid="paper-stop-BTC")
    db.add_bot_equity_point(equity_usd=245.0, equity_eur=230.0, cash_usd=210.5,
                            fees_cum_usd=0.5, funding_cum_usd=0.1)

    exchange = bot_runner._build_exchange()
    result = bot.reconcile_with_exchange(exchange)

    assert result == {"phantom_closed": [], "phantom_closed_details": [], "unknown_positions": [], "adopted": []}
    assert len(bot.open_positions()) == 1


def test_signal_outcome_uses_exactly_horizon_bars_not_one_extra(tmp_db):
    """DER Kernfall aus dem Review: df.loc[start:start+Nh] ist INKLUSIVE
    beider Grenzen und lieferte bei Stundenkerzen N+1 statt N Baelkchen. Der
    letzte fuer 'gross_return_pct' verwendete Schlusskurs muss aus dem
    SECHSTEN Baelkchen (Index 1..6 ab window_start) kommen, nicht aus dem
    siebten."""
    import bot_runner

    df = _outcome_candles(n=20)
    row = {"price": 100.0, "side": "long", "ts": "2026-08-01 00:31:00+00:00", "stop_pct": 50.0}
    # now weit genug in der Zukunft, dass alle 20 Kerzen als geschlossen gelten.
    now = df.index[-1] + timedelta(hours=2)

    out = bot_runner._signal_outcome(row, df, 6, now=now)

    # window_start = 01:00 (Index 1). 6 Baelkchen -> Index 1..6, letzter
    # Schluss ist df.iloc[6]["Close"] - NICHT df.iloc[7]["Close"].
    expected_close = float(df.iloc[6]["Close"])
    expected_gross = round((expected_close / 100.0 - 1) * 100, 4)
    assert out["gross_return_pct"] == pytest.approx(expected_gross, abs=1e-6)
    wrong_close = float(df.iloc[7]["Close"])
    wrong_gross = round((wrong_close / 100.0 - 1) * 100, 4)
    assert out["gross_return_pct"] != pytest.approx(wrong_gross, abs=1e-6)


def test_signal_outcome_none_while_window_not_yet_fully_closed(tmp_db):
    """DER zweite Kernfall: eine Entscheidung um 00:31 darf NICHT schon nach
    6h ab ts (also um 06:31) als vollstaendiges 6h-Ergebnis gelten - das
    echte Fenster beginnt erst um 01:00 (ceil) und braucht bis 07:00, um
    sechs VOLLSTAENDIG geschlossene Kerzen zu haben. Um 06:31 ist die
    06:00-Kerze noch am Laufen."""
    import bot_runner

    df = _outcome_candles(n=20)
    row = {"price": 100.0, "side": "long", "ts": "2026-08-01 00:31:00+00:00", "stop_pct": 50.0}
    still_running = datetime(2026, 8, 1, 6, 31, 0, tzinfo=timezone.utc)   # ts + 6h, wie der SQL-Vorfilter es zulaesst

    assert bot_runner._signal_outcome(row, df, 6, now=still_running) is None

    # Ab 07:00 ist die 06:00-Kerze fertig - jetzt sind es sechs geschlossene
    # Baelkchen (01:00 bis 06:00) und das Ergebnis wird berechnet.
    fully_closed = datetime(2026, 8, 1, 7, 0, 0, tzinfo=timezone.utc)
    assert bot_runner._signal_outcome(row, df, 6, now=fully_closed) is not None


# --- Singleton-Sperre: kein zweiter Runner-Prozess ---

def test_acquire_singleton_lock_succeeds_when_unlocked(tmp_path):
    lock_path = tmp_path / "test_runner.lock"
    assert bot_runner._acquire_singleton_lock(lock_path) is True
    bot_runner._release_singleton_lock()


def test_acquire_singleton_lock_fails_when_already_held(tmp_path):
    """Ein zweiter Runner-Prozess (egal ob per core.bot_process.start() oder
    per .bat-Doppelklick) darf niemals dieselbe Sperrdatei zusaetzlich
    exklusiv sperren."""
    lock_path = tmp_path / "test_runner.lock"
    assert bot_runner._acquire_singleton_lock(lock_path) is True
    try:
        # Zweiter, unabhaengiger Datei-Handle auf denselben Pfad - simuliert
        # einen zweiten Prozess, der dieselbe Sperrdatei zu erwerben versucht.
        second_handle = open(lock_path, "r+b")
        try:
            with pytest.raises(OSError):
                msvcrt.locking(second_handle.fileno(), msvcrt.LK_NBLCK, 1)
        finally:
            second_handle.close()
    finally:
        bot_runner._release_singleton_lock()


def test_release_singleton_lock_allows_a_later_acquire(tmp_path):
    lock_path = tmp_path / "test_runner.lock"
    assert bot_runner._acquire_singleton_lock(lock_path) is True
    bot_runner._release_singleton_lock()
    assert bot_runner._acquire_singleton_lock(lock_path) is True
    bot_runner._release_singleton_lock()


# --- _record_signal_cycle_outcome(): Gesundheitsstatus darf einen
# fehlgeschlagenen Signalzyklus nicht als Erfolg verbuchen ---

def test_record_signal_cycle_outcome_counts_a_clean_cycle_as_success(tmp_db):
    bot_config.record_cycle_failure()   # Vorzustand: schon mal ein Fehlschlag
    bot_runner._record_signal_cycle_outcome({"entry": None, "exits": []}, "paper")
    assert bot_config.consecutive_cycle_failures() == 0
    assert bot_config.last_successful_cycle_at() is not None


def test_record_signal_cycle_outcome_counts_a_signal_cycle_error_as_failure(tmp_db):
    """core.bot.run_deterministic_cycle() faengt einen Fehler im Signal-
    Zyklus selbst ab und gibt IHN im Ergebnis zurueck (`signals["error"]`),
    statt eine Exception zu werfen - der Aufrufer sah das vorher nicht und
    verbuchte trotzdem einen Erfolg (gefunden von einer externen Pruefung,
    13.09.2026). Muss stattdessen wie ein echter Fehlschlag zaehlen."""
    bot_runner._record_signal_cycle_outcome({"error": "Kursdaten nicht abrufbar"}, "paper")
    assert bot_config.consecutive_cycle_failures() == 1


def test_record_signal_cycle_outcome_marks_the_runner_degraded_after_the_threshold(tmp_db):
    """End-to-end-Nachweis: core.bot_guards.check_runner_health() (ueber
    core.bot_config.runner_degraded()) muss einen dauerhaft fehlschlagenden
    Signalzyklus tatsaechlich erkennen - vorher blieb runner_degraded() bei
    diesem Fehlertyp fuer immer False, egal wie viele Zyklen in Folge
    fehlschlugen."""
    assert bot_config.runner_degraded() is False
    for _ in range(bot_config.MAX_CONSECUTIVE_CYCLE_FAILURES):
        bot_runner._record_signal_cycle_outcome({"error": "kaputt"}, "paper")
    assert bot_config.runner_degraded() is True


def test_record_signal_cycle_outcome_notifies_only_in_live_mode_at_the_threshold(tmp_db, monkeypatch):
    sent = []
    monkeypatch.setattr(bot_runner.notify, "send", lambda msg: sent.append(msg))

    for _ in range(bot_config.MAX_CONSECUTIVE_CYCLE_FAILURES - 1):
        bot_runner._record_signal_cycle_outcome({"error": "kaputt"}, "paper")
    assert sent == []   # Schwelle noch nicht erreicht

    bot_runner._record_signal_cycle_outcome({"error": "kaputt"}, "paper")
    assert sent == []   # Papierbetrieb bleibt stumm (dieselbe Regel wie in core.notify)

    bot_config.record_cycle_success()
    for _ in range(bot_config.MAX_CONSECUTIVE_CYCLE_FAILURES - 1):
        bot_runner._record_signal_cycle_outcome({"error": "kaputt"}, "live")
    assert sent == []
    bot_runner._record_signal_cycle_outcome({"error": "kaputt"}, "live")
    assert len(sent) == 1
    assert "kaputt" in sent[0]


# --- "Position schließen"-Button je Position (Meta-Flag-Muster wie close_all) ---

def test_sleep_with_stop_check_aborts_early_on_close_position_request(tmp_db, monkeypatch):
    calls = {"n": 0}

    def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            db.set_meta("bot_close_position_requests", '{"BTC": {"requested_at": "x"}}')

    monkeypatch.setattr(bot_runner.time, "sleep", fake_sleep)
    wake_reason = bot_runner._sleep_with_stop_check(300)
    assert wake_reason == "close_position"
    assert calls["n"] <= 2


def test_maybe_close_requested_positions_noop_without_requests(tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(bot_runner.bot, "process_close_position_requests",
                        lambda ex: calls.append(ex) or [])
    bot_runner._maybe_close_requested_positions("fake-exchange")
    assert calls == ["fake-exchange"]


def test_maybe_close_requested_positions_logs_result(tmp_db, monkeypatch):
    logged = []
    monkeypatch.setattr(bot_runner, "_log", lambda msg: logged.append(msg))
    monkeypatch.setattr(bot_runner.bot, "process_close_position_requests",
                        lambda ex: [{"symbol": "BTC", "status": "filled",
                                    "realized_pnl_usd": 12.5}])

    bot_runner._maybe_close_requested_positions("fake-exchange", runner_mode="paper")

    assert any("BTC" in m and "filled" in m for m in logged)


def test_maybe_close_requested_positions_notifies_on_live_fill(tmp_db, monkeypatch):
    sent = []
    monkeypatch.setattr(bot_runner.notify, "send", lambda msg: sent.append(msg))
    monkeypatch.setattr(bot_runner.bot, "process_close_position_requests",
                        lambda ex: [{"symbol": "ETH", "status": "filled",
                                    "realized_pnl_usd": -3.0}])

    bot_runner._maybe_close_requested_positions("fake-exchange", runner_mode="live")

    assert any("ETH" in m for m in sent)
