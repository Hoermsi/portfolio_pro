"""core/bot_config.py: Limits-Clamping, Kill-Switch-Sperrklinke, Demo-/Dry-Run-Guards."""
import json

from core import bot_config, config


# --- bot_limits() / save_bot_limits() ---

def _expected_defaults(level: int) -> dict:
    """bot_limits() ohne Overrides = abgeleitete Risiko-Limits + feste
    Strategie-Konstanten - beide reine Funktionen, siehe core.bot_config
    strategy_constants()-Docstring."""
    return {**bot_config.derive_limits(level), **bot_config.strategy_constants()}


def test_bot_limits_defaults_on_empty_db(tmp_db):
    """Ohne gespeicherte Stufe gilt Risikostufe 5 - die mittleren Ankerwerte."""
    assert bot_config.risk_level() == 5
    limits = bot_config.bot_limits()
    assert limits == _expected_defaults(5)
    # Stufe 5 = Validierungsphase: 1x Hebel, kleines Risiko je Trade (siehe
    # core/bot_config.py-Modul-Docstring und CLAUDE.md-Feedback).
    assert limits["max_leverage"] == 1.0
    assert limits["risk_per_trade_pct"] == 0.4
    assert limits["max_total_exposure_pct"] == 55.0
    assert limits["max_portfolio_heat_pct"] == 1.5
    assert limits["max_position_pct"] == 25.0
    assert limits["max_positions"] == 3
    # Strategie-Konstante, nicht mehr vom Regler abhaengig.
    assert limits["max_trades_per_day"] == 6
    assert limits["equity_floor_pct"] == 70.0


def test_bot_limits_survive_garbage_meta(tmp_db):
    tmp_db.set_meta("bot_limits", "not valid json {{{")
    assert bot_config.bot_limits() == _expected_defaults(5)


def test_bot_limits_survive_non_dict_json(tmp_db):
    tmp_db.set_meta("bot_limits", json.dumps([1, 2, 3]))
    assert bot_config.bot_limits() == _expected_defaults(5)


def test_garbage_risk_level_falls_back_to_default(tmp_db):
    tmp_db.set_meta("bot_risk_level", "sehr viel")
    assert bot_config.risk_level() == 5


def test_save_bot_limits_roundtrip(tmp_db):
    out = bot_config.save_bot_limits(max_positions=5, daily_loss_limit_pct=5.0)
    assert out["max_positions"] == 5
    assert out["daily_loss_limit_pct"] == 5.0
    # unveraenderte Werte folgen weiterhin dem Regler
    assert out["max_leverage"] == bot_config.derive_limits(5)["max_leverage"]
    # persistiert tatsaechlich
    reloaded = bot_config.bot_limits()
    assert reloaded["max_positions"] == 5


def test_limits_for_risk_level_applies_stored_overrides_at_any_level(tmp_db):
    """Phase 6 (CLAUDE.md-Feedback 'Rücktest live-identisch machen'):
    analysis.bot_backtest.run_backtest(risk_level=X) muss dieselben
    gespeicherten Experten-Overrides sehen wie der Live-Pfad (bot_limits()),
    auch wenn X NICHT die gerade aktive Risikostufe ist - vorher rechnete
    der Rücktest mit derive_limits() allein und liess Overrides unsichtbar
    aussen vor."""
    bot_config.save_risk_level(3)
    bot_config.save_bot_limits(max_positions=9)
    # Aktive Stufe (3) traegt den Override ganz normal ueber bot_limits().
    assert bot_config.bot_limits()["max_positions"] == 9
    # Eine ANDERE Stufe (8) muss denselben Override ebenfalls sehen - nur
    # die abgeleiteten (nicht ueberschriebenen) Werte unterscheiden sich.
    at_level_8 = bot_config.limits_for_risk_level(8)
    assert at_level_8["max_positions"] == 9
    assert at_level_8["max_leverage"] == bot_config.derive_limits(8)["max_leverage"]
    assert at_level_8["max_leverage"] != bot_config.derive_limits(3)["max_leverage"]
    # Strategie-Konstanten sind unabhaengig von der Stufe identisch.
    assert at_level_8["atr_stop_mult"] == bot_config.bot_limits()["atr_stop_mult"]


def test_save_bot_limits_cannot_touch_strategy_constants(tmp_db):
    """Der ganze Punkt des Umbaus: max_trades_per_day & Co. sind seit dem
    Umbau feste Konstanten in core/bot_signals.py - kein Eintrag in
    _RISK_ANCHORS mehr, also ignoriert save_bot_limits() einen Versuch,
    sie zu ueberschreiben, stillschweigend (wie jeden anderen unbekannten
    Schluessel, siehe test_save_bot_limits_ignores_unknown_keys)."""
    out = bot_config.save_bot_limits(max_trades_per_day=99,
                                     atr_stop_mult=0.1, symbol_cooldown_hours=0.0)
    assert out["max_trades_per_day"] == bot_config.strategy_constants()["max_trades_per_day"]
    assert out["atr_stop_mult"] == bot_config.strategy_constants()["atr_stop_mult"]
    assert out["symbol_cooldown_hours"] == bot_config.strategy_constants()["symbol_cooldown_hours"]
    # entry_score_min (V1) ist ersatzlos gestrichen - kein Schluessel mehr,
    # den man ueberhaupt versuchen koennte zu ueberschreiben.
    assert "entry_score_min" not in bot_config.strategy_constants()


def test_save_bot_limits_ignores_unknown_keys(tmp_db):
    out = bot_config.save_bot_limits(not_a_real_limit=999)
    assert "not_a_real_limit" not in out


def test_max_leverage_clamped_to_hard_cap(tmp_db):
    """Selbst ein absichtlich zu hoch gespeicherter Wert darf 2x nie
    ueberschreiten - das ist die vom Nutzer vorgegebene harte Obergrenze."""
    bot_config.save_bot_limits(max_leverage=10.0)
    assert bot_config.bot_limits()["max_leverage"] == 2.0


def test_max_leverage_clamped_below_one(tmp_db):
    bot_config.save_bot_limits(max_leverage=0.1)
    assert bot_config.bot_limits()["max_leverage"] == 1.0


def test_max_same_side_positions_never_exceeds_max_positions(tmp_db):
    bot_config.save_bot_limits(max_same_side_positions=8, max_positions=5)
    limits = bot_config.bot_limits()
    assert limits["max_same_side_positions"] <= limits["max_positions"]


def test_bot_limits_int_fields_stay_int(tmp_db):
    limits = bot_config.bot_limits()
    assert isinstance(limits["max_positions"], int)
    assert isinstance(limits["max_same_side_positions"], int)
    assert isinstance(limits["max_position_pct"], float)


# --- Kill-Switch: Sperrklinke ---

def test_kill_switch_starts_inactive(tmp_db):
    assert bot_config.is_killed() is False
    info = bot_config.kill_switch_info()
    assert info["active"] is False
    assert info["reason"] is None


def test_trip_kill_switch_activates_with_reason(tmp_db):
    bot_config.trip_kill_switch("Equity-Boden unterschritten")
    assert bot_config.is_killed() is True
    info = bot_config.kill_switch_info()
    assert info["reason"] == "Equity-Boden unterschritten"
    assert info["at"] is not None


def test_trip_kill_switch_is_idempotent_keeps_first_reason(tmp_db):
    """Der ERSTE Grund bleibt sichtbar - der hat tatsaechlich zum Stopp gefuehrt."""
    bot_config.trip_kill_switch("Erster Grund")
    bot_config.trip_kill_switch("Zweiter Grund")
    assert bot_config.kill_switch_info()["reason"] == "Erster Grund"


def test_kill_switch_never_falls_back_automatically(tmp_db):
    """Simuliert: Ursache des Stopps ist behoben (z.B. Equity wieder ueber dem
    Boden) - der Switch bleibt trotzdem aktiv, bis reset_kill_switch()
    EXPLIZIT aufgerufen wird."""
    bot_config.trip_kill_switch("Equity-Boden unterschritten")
    assert bot_config.is_killed() is True
    # nichts weiter passiert automatisch - kein impliziter Reset-Pfad existiert
    assert bot_config.is_killed() is True


def test_reset_kill_switch_clears_everything(tmp_db):
    bot_config.trip_kill_switch("Grund")
    bot_config.reset_kill_switch()
    assert bot_config.is_killed() is False
    info = bot_config.kill_switch_info()
    assert info["reason"] is None
    assert info["at"] is None


# --- Runner-Gesundheit: aufeinanderfolgende Zyklus-Fehlschlaege ---

def test_runner_degraded_false_until_threshold(tmp_db):
    for _ in range(bot_config.MAX_CONSECUTIVE_CYCLE_FAILURES - 1):
        bot_config.record_cycle_failure()
    assert bot_config.runner_degraded() is False
    bot_config.record_cycle_failure()
    assert bot_config.runner_degraded() is True


def test_record_cycle_success_resets_failure_counter(tmp_db):
    bot_config.record_cycle_failure()
    bot_config.record_cycle_failure()
    bot_config.record_cycle_success()
    assert bot_config.consecutive_cycle_failures() == 0
    assert bot_config.runner_degraded() is False
    assert bot_config.last_successful_cycle_at() is not None


# --- Demo-Modus-Guard ---

def test_live_trading_allowed_true_by_default(tmp_db):
    assert bot_config.live_trading_allowed() is True


def test_live_trading_blocked_in_demo_mode(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", config.DEMO_DB_PATH)
    assert bot_config.live_trading_allowed() is False


# --- Dry-Run ---

def test_dry_run_enabled_by_default(tmp_db):
    """Muss unabhaengig von jeder Einstellung hart auf True stehen, bis der
    Nutzer bewusst 'Live-Handel aktivieren' bestaetigt."""
    assert bot_config.dry_run_enabled() is True


def test_dry_run_can_be_disabled_and_reenabled(tmp_db):
    bot_config.set_dry_run(False)
    assert bot_config.dry_run_enabled() is False
    bot_config.set_dry_run(True)
    assert bot_config.dry_run_enabled() is True


# --- KI-Bericht-Schalter ---

def test_ai_report_enabled_by_default(tmp_db):
    assert bot_config.ai_report_enabled() is True


def test_ai_report_can_be_disabled_and_reenabled(tmp_db):
    bot_config.set_ai_report_enabled(False)
    assert bot_config.ai_report_enabled() is False
    bot_config.set_ai_report_enabled(True)
    assert bot_config.ai_report_enabled() is True


def test_log_walkforward_run_records_both_pass_and_fail(tmp_db):
    """Jeder Lauf landet in db.bot_walkforward_runs, nicht nur der zuletzt
    bestandene - sonst ist nicht nachvollziehbar, wie oft eine andere
    Fenstergroesse/Risikostufe zuerst durchgefallen ist, bevor eine
    Kombination bestand."""
    from core import db

    bot_config.log_walkforward_run(
        {"gate": {"ok": False, "reasons": ["Nur 20 Trades insgesamt, mindestens 40 nötig."]}},
        window_days=45, num_windows=4, risk_level=5)
    bot_config.log_walkforward_run(
        {"gate": {"ok": True, "reasons": [], "pooled": {"trades": 50}}},
        window_days=45, num_windows=4, risk_level=9)

    runs = db.list_bot_walkforward_runs()
    assert len(runs) == 2
    # Neuester zuerst.
    assert runs[0]["gate_ok"] == 1
    assert runs[0]["risk_level"] == 9
    assert runs[1]["gate_ok"] == 0
    assert "40 nötig" in runs[1]["gate_reasons_json"]


def test_log_walkforward_run_stores_a_real_validation_hash(tmp_db):
    """Vorher fest `validation_hash=None`: ein gespeicherter Lauf liess sich
    nachtraeglich keinem Code-/Konfigurationsstand zuordnen (gefunden von
    einer externen Pruefung, 13.09.2026). Muss jetzt den config_fingerprint()
    der fuer GENAU diese Risikostufe wirksamen Limits tragen - identisch fuer
    zwei Laeufe DERSELBEN Stufe, unterschiedlich fuer verschiedene Stufen
    (sofern deren Limits tatsaechlich abweichen)."""
    from core import db

    bot_config.log_walkforward_run(
        {"gate": {"ok": True, "reasons": [], "pooled": {"trades": 50}}},
        window_days=45, num_windows=4, risk_level=5)
    bot_config.log_walkforward_run(
        {"gate": {"ok": True, "reasons": [], "pooled": {"trades": 50}}},
        window_days=45, num_windows=4, risk_level=5)
    bot_config.log_walkforward_run(
        {"gate": {"ok": True, "reasons": [], "pooled": {"trades": 60}}},
        window_days=45, num_windows=4, risk_level=9)

    runs = db.list_bot_walkforward_runs()
    assert len(runs) == 3
    for run in runs:
        assert run["validation_hash"]
        assert run["validation_hash"] == bot_config.config_fingerprint(
            bot_config.limits_for_risk_level(run["risk_level"]))
    # Zwei Laeufe derselben Stufe -> derselbe Fingerabdruck.
    same_level = [r["validation_hash"] for r in runs if r["risk_level"] == 5]
    assert same_level[0] == same_level[1]


def test_log_walkforward_run_ignores_a_result_without_gate(tmp_db):
    """Ein fehlgeschlagener Datenabruf (kein 'gate'-Schluessel, z.B.
    {'error': ...}) darf nicht als stiller Fehlschlag protokolliert werden -
    das waere irrefuehrend, weil gar kein Gate ausgewertet wurde."""
    from core import db

    bot_config.log_walkforward_run({"error": "Keine Kursdaten geladen."},
                                   window_days=45, num_windows=4, risk_level=5)
    assert db.list_bot_walkforward_runs() == []


# --- Live-Schalter-Voraussetzung: Preflight-Frische ---

def test_preflight_is_fresh_false_without_any_run(tmp_db):
    assert bot_config.last_preflight_ok_at() is None
    assert bot_config.preflight_is_fresh() is False


def test_preflight_is_fresh_true_shortly_after_success(tmp_db):
    from core import clock
    tmp_db.set_meta("bot_last_preflight_ok_at", clock.iso_utc())
    assert bot_config.preflight_is_fresh(max_age_minutes=60.0) is True


def test_preflight_is_fresh_false_when_too_old(tmp_db):
    from datetime import timedelta

    from core import clock
    old = clock.now_utc() - timedelta(hours=2)
    tmp_db.set_meta("bot_last_preflight_ok_at", clock.iso_utc(old))
    assert bot_config.preflight_is_fresh(max_age_minutes=60.0) is False


def test_preflight_is_fresh_false_on_corrupted_timestamp(tmp_db):
    tmp_db.set_meta("bot_last_preflight_ok_at", "not-a-timestamp")
    assert bot_config.preflight_is_fresh() is False


def test_phase_f_is_fresh_requires_success_marker(tmp_db):
    assert bot_config.phase_f_is_fresh() is False


def test_phase_f_is_fresh_true_shortly_after_success(tmp_db):
    from core import clock
    tmp_db.set_meta("bot_phase_f_verified_at", clock.iso_utc())
    assert bot_config.phase_f_is_fresh() is True


def test_phase_f_is_fresh_false_when_too_old_or_corrupt(tmp_db):
    from datetime import timedelta

    from core import clock
    tmp_db.set_meta("bot_phase_f_verified_at",
                    clock.iso_utc(clock.now_utc() - timedelta(days=2)))
    assert bot_config.phase_f_is_fresh() is False
    tmp_db.set_meta("bot_phase_f_verified_at", "not-a-timestamp")
    assert bot_config.phase_f_is_fresh() is False


# --- Risikoregler ---

def test_risk_level_monotonic_across_scale(tmp_db):
    """Hoehere Stufe = mehr Risikobereitschaft in JEDE Richtung. Waere eine
    dieser Achsen invertiert, wuerde der Regler an einer Stelle das Gegenteil
    dessen tun, was sein Name verspricht. Nur noch Risiko-Achsen
    (core.bot_config._RISK_ANCHORS) - Strategie-Parameter (entry_score_min,
    atr_stop_mult, ...) gehoeren seit dem Umbau nicht mehr zum Regler, siehe
    test_derive_limits_never_touches_strategy_constants unten."""
    low, mid, high = (bot_config.derive_limits(1), bot_config.derive_limits(5),
                      bot_config.derive_limits(10))
    for key in ("max_position_pct", "max_positions", "max_same_side_positions",
                "max_leverage", "daily_loss_limit_pct", "risk_per_trade_pct",
                "max_total_exposure_pct", "max_portfolio_heat_pct"):
        assert low[key] <= mid[key] <= high[key], key
    # Sinkt mit steigendem Risiko (tieferer Boden).
    assert low["equity_floor_pct"] >= mid["equity_floor_pct"] >= high["equity_floor_pct"]


def test_derive_limits_never_touches_strategy_constants(tmp_db):
    """Der ganze Punkt des Umbaus: core.bot_config._RISK_ANCHORS enthaelt
    KEINE Strategie-Parameter mehr - derive_limits() darf sie fuer KEINE
    Risikostufe zurueckgeben (siehe core/bot_config.py-Modul-Docstring)."""
    strategy_keys = set(bot_config.strategy_constants())
    for level in (1, 5, 10):
        assert strategy_keys.isdisjoint(bot_config.derive_limits(level))


def test_risk_level_is_clamped_to_scale(tmp_db):
    assert bot_config.clamp_risk_level(0) == 1
    assert bot_config.clamp_risk_level(99) == 10
    assert bot_config.clamp_risk_level(None) == 5


def test_max_leverage_stays_capped_even_at_top_risk(tmp_db):
    bot_config.save_risk_level(10)
    assert bot_config.bot_limits()["max_leverage"] == 2.0


def test_save_risk_level_discards_expert_overrides(tmp_db):
    """Sonst zoege der Nutzer den Regler und auf genau dieser Achse passierte
    nichts - der haeufigste Weg, wie eine Einstellung stillschweigend
    wirkungslos wird."""
    bot_config.save_bot_limits(max_position_pct=99.0)
    assert bot_config.bot_limits()["max_position_pct"] == 99.0
    bot_config.save_risk_level(3)
    assert bot_config.bot_limits()["max_position_pct"] == bot_config.derive_limits(3)["max_position_pct"]
    assert bot_config.overridden_keys() == []


def test_overrides_stay_on_top_of_the_slider(tmp_db):
    bot_config.save_risk_level(8)
    bot_config.save_bot_limits(max_positions=2)
    limits = bot_config.bot_limits()
    assert limits["max_positions"] == 2
    assert limits["max_position_pct"] == bot_config.derive_limits(8)["max_position_pct"]
    assert bot_config.overridden_keys() == ["max_positions"]


# --- Machbarkeit gegen die Boersen-Mindestordergroesse ---

def test_feasibility_flags_account_too_small_for_risk_level(tmp_db):
    """Der stille Killer bei einem kleinen Konto: 10 $ Mindestorder sind bei
    44 $ Equity bereits 22 % - eine niedrige Stufe kann dann gar nicht
    handeln, ohne dass irgendwo ein Fehler auftaucht."""
    check = bot_config.feasibility(44.57, bot_config.derive_limits(1), min_notional_usd=10.0)
    assert check["ok"] is False
    assert "Mindest" in check["hint"] or "mindestens" in check["hint"]
    assert round(check["min_pct_needed"], 1) == 22.4


def test_feasibility_ok_when_position_clears_min_notional(tmp_db):
    # 250 $ statt 44.57 $: das Risikobudget je Trade wurde fuer die
    # Validierungsphase bewusst konservativer kalibriert (Stufe 10 jetzt
    # 1,0 % statt vormals 2,0 %, und der worst-case ATR-Stop ist mit dem
    # V2-Umbau breiter: core.bot_signals._ATR_PCT_MAX 10.0 statt 4.0) - ein
    # derart kleines Konto klaert die Hyperliquid-Mindestordergroesse selbst
    # bei Stufe 10 nicht mehr, siehe
    # test_feasibility_flags_account_too_small_for_risk_level fuer genau
    # diesen (jetzt haeufigeren) Fall.
    check = bot_config.feasibility(250.0, bot_config.derive_limits(10), min_notional_usd=10.0)
    assert check["ok"] is True
    assert check["max_notional_usd"] > 10.0


def test_feasibility_without_equity_is_not_a_crash(tmp_db):
    check = bot_config.feasibility(0.0, bot_config.derive_limits(5), min_notional_usd=10.0)
    assert check["ok"] is False
    assert check["min_pct_needed"] is None
