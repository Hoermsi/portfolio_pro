"""core/bot_guards.py: reine Funktionen, keine DB noetig.

Jeder Guard einzeln (Erlaubt- und Verbot-Fall), dann die beiden Aggregatoren
- besonders die Faelle, in denen evaluate_exit_order() bewusst laxer sein
muss als evaluate_entry_order() (eine Position schliessen darf nie durch ein
Risikolimit blockiert werden, das eigentlich davor schuetzen soll)."""
from datetime import datetime, timedelta

from core import bot_guards as g

_LIMITS = {
    "max_position_pct": 35.0,
    "min_positions": 3,
    "max_positions": 5,
    "max_leverage": 2.0,
    "max_trades_per_day": 10,
    "target_trades_per_day": 3,
    "symbol_cooldown_hours": 12.0,
    "daily_loss_limit_pct": 8.0,
    "equity_floor_pct": 60.0,
    "max_total_exposure_pct": 150.0,
    # Phase 5: fail-closed wie max_total_exposure_pct - ohne diese Schluessel
    # wuerde jeder bestehende Test hier ab jetzt an check_portfolio_heat()/
    # check_same_side_concentration() scheitern, obwohl er diese Dimension
    # gar nicht pruefen will.
    "max_portfolio_heat_pct": 5.0,
    "max_same_side_positions": 3,
    "loss_streak_limit": 4,
}

NOW = datetime(2026, 8, 25, 12, 0, 0)


def _entry_kwargs(**overrides):
    kwargs = dict(
        kill_switch_active=False, live_trading_allowed=True,
        order_notional_usd=50.0, max_reasonable_notional_usd=200.0,
        equity_usd=250.0, equity_start_usd=250.0, equity_start_of_day_usd=250.0,
        # 0.0, nicht ein kleiner positiver Wert: check_leverage prueft das
        # Funding-Budget seit der Korrektur (13.09.2026) bei JEDEM Hebel,
        # nicht nur >1x (siehe core.bot_guards.check_leverage) - ein von
        # dieser Erweiterung unbetroffener Test soll daran nicht scheitern.
        leverage=1.0, funding_rate_hourly=0.0,
        stop_px=90.0, side="long", entry_px=100.0,
        current_open_count=1, is_new_symbol=True,
        last_closed_at=None, now=NOW, trades_today=1, limits=_LIMITS,
    )
    kwargs.update(overrides)
    return kwargs


# --- GuardResult ---

def test_guard_result_bool():
    assert bool(g.GuardResult(True)) is True
    assert bool(g.GuardResult(False, "x")) is False
    assert g.GuardResult(True).reason == ""


# --- check_kill_switch / check_demo_mode ---

def test_kill_switch_blocks():
    assert g.check_kill_switch(True).allowed is False
    assert g.check_kill_switch(False).allowed is True


def test_demo_mode_blocks():
    assert g.check_demo_mode(live_trading_allowed=False).allowed is False
    assert g.check_demo_mode(live_trading_allowed=True).allowed is True


def test_runner_health_blocks_only_when_degraded():
    assert g.check_runner_health(True).allowed is False
    assert g.check_runner_health(False).allowed is True


# --- check_order_plausibility ---

def test_order_plausibility_rejects_zero_and_negative():
    assert g.check_order_plausibility(0, 200).allowed is False
    assert g.check_order_plausibility(-5, 200).allowed is False


def test_order_plausibility_rejects_absurdly_large():
    r = g.check_order_plausibility(5000, 200)
    assert r.allowed is False
    assert "unplausibel" in r.reason


def test_order_plausibility_allows_normal():
    assert g.check_order_plausibility(50, 200).allowed is True


# --- check_position_size ---

def test_position_size_rejects_over_limit():
    r = g.check_position_size(order_notional_usd=100, equity_usd=200, max_position_pct=35.0)
    assert r.allowed is False  # 50% > 35%


def test_position_size_allows_within_limit():
    r = g.check_position_size(order_notional_usd=50, equity_usd=200, max_position_pct=35.0)
    assert r.allowed is True  # 25% <= 35%


def test_position_size_rejects_zero_equity():
    assert g.check_position_size(50, 0, 35.0).allowed is False


# --- check_position_count ---

def test_position_count_blocks_new_symbol_at_max():
    r = g.check_position_count(is_new_symbol=True, current_open_count=5, max_positions=5)
    assert r.allowed is False


def test_position_count_allows_nachkauf_at_max():
    """Nachkauf auf ein BESTEHENDES Symbol erhoeht die Positionsanzahl nicht."""
    r = g.check_position_count(is_new_symbol=False, current_open_count=5, max_positions=5)
    assert r.allowed is True


def test_position_count_allows_new_symbol_below_max():
    r = g.check_position_count(is_new_symbol=True, current_open_count=4, max_positions=5)
    assert r.allowed is True


# --- check_leverage ---

def test_leverage_rejects_above_account_cap():
    r = g.check_leverage(requested_leverage=3.0, funding_rate_hourly=-0.0001, max_leverage=2.0)
    assert r.allowed is False


def test_leverage_2x_requires_non_positive_funding():
    r = g.check_leverage(requested_leverage=2.0, funding_rate_hourly=0.0001, max_leverage=2.0)
    assert r.allowed is False
    r_ok = g.check_leverage(requested_leverage=2.0, funding_rate_hourly=0.0, max_leverage=2.0)
    assert r_ok.allowed is True
    r_neg = g.check_leverage(requested_leverage=2.0, funding_rate_hourly=-0.0001, max_leverage=2.0)
    assert r_neg.allowed is True


def test_leverage_2x_rejects_unknown_funding():
    """Fehlt die Funding-Rate (None), darf trotzdem kein Hebel gezogen werden -
    unbekannt ist kein Freifahrtschein."""
    r = g.check_leverage(requested_leverage=2.0, funding_rate_hourly=None, max_leverage=2.0)
    assert r.allowed is False


def test_leverage_1x_still_checks_funding_budget_when_rate_is_known():
    """Frueherer Bug (siehe CLAUDE.md-Feedback, externe Pruefung 13.09.2026):
    ein `if requested_leverage <= 1.0: return _OK`-Kurzschluss liess Hebel-1-
    Orders komplett am Funding-Budget vorbei - obwohl Funding auf die
    NOMINALE gezahlt wird, nicht auf die Margin, und eine 1x-Position mit
    teurem Funding also genauso viel kostet wie dieselbe Nominale mit 2x."""
    r = g.check_leverage(requested_leverage=1.0, funding_rate_hourly=0.01, max_leverage=2.0)
    assert r.allowed is False
    r_ok = g.check_leverage(requested_leverage=1.0, funding_rate_hourly=0.0, max_leverage=2.0)
    assert r_ok.allowed is True


def test_leverage_1x_ignores_unknown_funding():
    """Anders als bei zusaetzlichem Hebel bleibt Hebel 1 bei UNBEKANNTER
    Rate erlaubt - dort vergroessert kein Hebel die Nominale pro Margin, ein
    fehlender Wert (Rücktest-Fenster ohne Fundinghistorie, kurzzeitig nicht
    abrufbarer Live-Wert) soll nicht jede gewoehnliche Order blockieren."""
    r = g.check_leverage(requested_leverage=1.0, funding_rate_hourly=None, max_leverage=2.0)
    assert r.allowed is True


# --- check_stop_loss_present ---

def test_stop_loss_required():
    assert g.check_stop_loss_present(None, "long", 100.0).allowed is False
    assert g.check_stop_loss_present(0, "long", 100.0).allowed is False


def test_stop_loss_must_be_below_long_entry():
    assert g.check_stop_loss_present(90.0, "long", 100.0).allowed is True
    assert g.check_stop_loss_present(110.0, "long", 100.0).allowed is False


def test_stop_loss_must_be_above_short_entry():
    assert g.check_stop_loss_present(110.0, "short", 100.0).allowed is True
    assert g.check_stop_loss_present(90.0, "short", 100.0).allowed is False


# --- check_symbol_cooldown ---

def test_cooldown_blocks_within_window():
    last_closed = NOW - timedelta(hours=5)
    r = g.check_symbol_cooldown(last_closed, NOW, cooldown_hours=12.0)
    assert r.allowed is False
    assert "noch" in r.reason


def test_cooldown_allows_after_window():
    last_closed = NOW - timedelta(hours=13)
    r = g.check_symbol_cooldown(last_closed, NOW, cooldown_hours=12.0)
    assert r.allowed is True


def test_cooldown_allows_without_prior_close():
    assert g.check_symbol_cooldown(None, NOW, cooldown_hours=12.0).allowed is True


def test_cooldown_is_immune_to_dst_spring_forward():
    """Sommerzeit-Grenzfall (core/clock.py, Phase 1): check_symbol_cooldown
    rechnet ausschliesslich in TZ-AWARE UTC - Europe/Berlin sprang am
    29.03.2026 von 01:30 CET (UTC+1) direkt auf 03:30 CEST (UTC+2), lokal
    also ein Sprung von "zwei Stunden", obwohl real nur EINE Stunde verging.
    Eine Implementierung, die (versehentlich) mit naiven lokalen Zeiten statt
    UTC-Deltas rechnet, wuerde hier 2.0h statt der tatsaechlichen 1.0h
    ermitteln und einen noch aktiven Cooldown faelschlich als abgelaufen
    behandeln."""
    from datetime import timezone
    last_closed = datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc)   # 01:30 CET
    now = datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)            # 03:30 CEST, +1h UTC
    r = g.check_symbol_cooldown(last_closed, now, cooldown_hours=1.5)
    assert r.allowed is False   # real nur 1.0h vergangen, Cooldown (1.5h) laeuft noch
    assert "noch 0.5h" in r.reason


# --- check_daily_trade_limit ---

def test_daily_trade_limit():
    assert g.check_daily_trade_limit(trades_today=9, max_trades_per_day=10).allowed is True
    assert g.check_daily_trade_limit(trades_today=10, max_trades_per_day=10).allowed is False


# --- check_daily_loss_limit ---

def test_daily_loss_limit_blocks_new_entries():
    r = g.check_daily_loss_limit(equity_now_usd=90, equity_start_of_day_usd=100,
                                 daily_loss_limit_pct=8.0, is_new_entry=True)
    assert r.allowed is False  # 10% Verlust >= 8% Limit


def test_daily_loss_limit_never_blocks_exits():
    r = g.check_daily_loss_limit(equity_now_usd=50, equity_start_of_day_usd=100,
                                 daily_loss_limit_pct=8.0, is_new_entry=False)
    assert r.allowed is True


def test_daily_loss_limit_allows_small_loss():
    r = g.check_daily_loss_limit(equity_now_usd=97, equity_start_of_day_usd=100,
                                 daily_loss_limit_pct=8.0, is_new_entry=True)
    assert r.allowed is True


# --- check_equity_floor ---

def test_equity_floor_blocks_below_threshold():
    r = g.check_equity_floor(equity_now_usd=59, equity_start_usd=100, floor_pct=60.0)
    assert r.allowed is False


def test_equity_floor_allows_at_threshold():
    r = g.check_equity_floor(equity_now_usd=60, equity_start_usd=100, floor_pct=60.0)
    assert r.allowed is True


# --- check_equity_trusted ---

def test_equity_trusted_blocks_when_false():
    assert g.check_equity_trusted(False).allowed is False


def test_equity_trusted_allows_when_true():
    assert g.check_equity_trusted(True).allowed is True


# --- check_total_exposure ---

def test_total_exposure_blocks_over_limit():
    r = g.check_total_exposure(order_notional_usd=50, open_notional_usd=110,
                               equity_usd=100, max_total_exposure_pct=150.0)
    assert r.allowed is False  # (110+50)/100 = 160% > 150%


def test_total_exposure_allows_at_exact_limit():
    r = g.check_total_exposure(order_notional_usd=50, open_notional_usd=100,
                               equity_usd=100, max_total_exposure_pct=150.0)
    assert r.allowed is True  # genau 150%


def test_total_exposure_allows_below_limit():
    r = g.check_total_exposure(order_notional_usd=10, open_notional_usd=0,
                               equity_usd=100, max_total_exposure_pct=150.0)
    assert r.allowed is True


def test_total_exposure_ok_at_zero_equity():
    """Equity <= 0 wird von check_position_size bereits abgefangen - dieser
    Guard darf bei einem unplausiblen Kontostand nicht selbst durch Zero
    Division abstuerzen."""
    r = g.check_total_exposure(order_notional_usd=50, open_notional_usd=0,
                               equity_usd=0, max_total_exposure_pct=150.0)
    assert r.allowed is True


# --- check_portfolio_heat ---

def test_portfolio_heat_blocks_over_limit():
    r = g.check_portfolio_heat(order_heat_usd=3.0, open_heat_usd=2.0,
                               equity_usd=100.0, max_portfolio_heat_pct=4.0)
    assert r.allowed is False  # (2+3)/100 = 5% > 4%


def test_portfolio_heat_allows_at_exact_limit():
    r = g.check_portfolio_heat(order_heat_usd=2.0, open_heat_usd=2.0,
                               equity_usd=100.0, max_portfolio_heat_pct=4.0)
    assert r.allowed is True  # genau 4%


def test_portfolio_heat_blocks_when_limit_is_missing():
    """Fail-closed - derselbe Grundsatz wie check_total_exposure: eine
    fehlende Konfiguration darf nicht lautlos durchlassen."""
    r = g.check_portfolio_heat(order_heat_usd=1.0, open_heat_usd=0.0,
                               equity_usd=100.0, max_portfolio_heat_pct=None)
    assert r.allowed is False


def test_portfolio_heat_ok_at_zero_equity():
    r = g.check_portfolio_heat(order_heat_usd=1.0, open_heat_usd=0.0,
                               equity_usd=0.0, max_portfolio_heat_pct=4.0)
    assert r.allowed is True


# --- check_same_side_concentration ---

def test_same_side_concentration_blocks_at_max():
    r = g.check_same_side_concentration(is_new_symbol=True, side="long",
                                        current_same_side_count=2, max_same_side_positions=2)
    assert r.allowed is False


def test_same_side_concentration_allows_below_max():
    r = g.check_same_side_concentration(is_new_symbol=True, side="long",
                                        current_same_side_count=1, max_same_side_positions=2)
    assert r.allowed is True


def test_same_side_concentration_ignores_nachkauf():
    """Nur beim Eroeffnen eines NEUEN Symbols relevant - ein Nachkauf auf ein
    bestehendes Symbol erhoeht die Richtungs-Konzentration nicht."""
    r = g.check_same_side_concentration(is_new_symbol=False, side="long",
                                        current_same_side_count=5, max_same_side_positions=2)
    assert r.allowed is True


def test_same_side_concentration_blocks_when_limit_is_missing():
    r = g.check_same_side_concentration(is_new_symbol=True, side="long",
                                        current_same_side_count=0, max_same_side_positions=None)
    assert r.allowed is False


# --- check_loss_streak ---

def test_loss_streak_blocks_at_limit_same_day():
    r = g.check_loss_streak(consecutive_losses=4, last_loss_closed_at=NOW - timedelta(hours=1),
                            now=NOW, loss_streak_limit=4)
    assert r.allowed is False


def test_loss_streak_allows_below_limit():
    r = g.check_loss_streak(consecutive_losses=3, last_loss_closed_at=NOW - timedelta(hours=1),
                            now=NOW, loss_streak_limit=4)
    assert r.allowed is True


def test_loss_streak_stays_blocked_across_midnight_until_full_24_hours():
    last_loss = NOW.replace(hour=23, minute=55) - timedelta(days=1)
    shortly_after_midnight = last_loss + timedelta(minutes=10)
    r = g.check_loss_streak(consecutive_losses=5, last_loss_closed_at=last_loss,
                            now=shortly_after_midnight, loss_streak_limit=4)
    assert r.allowed is False


def test_loss_streak_resets_after_full_24_hours():
    last_loss = NOW - timedelta(hours=24)
    r = g.check_loss_streak(consecutive_losses=5, last_loss_closed_at=last_loss,
                            now=NOW, loss_streak_limit=4)
    assert r.allowed is True


def test_loss_streak_ok_without_prior_loss():
    r = g.check_loss_streak(consecutive_losses=0, last_loss_closed_at=None,
                            now=NOW, loss_streak_limit=4)
    assert r.allowed is True


# --- evaluate_entry_order ---

def test_entry_order_allowed_happy_path():
    ev = g.evaluate_entry_order(**_entry_kwargs())
    assert ev.allowed is True
    assert ev.failed_checks == []


def test_entry_order_collects_all_failures():
    """Kill-Switch UND fehlender Stop gleichzeitig -> beide Gruende im Ergebnis,
    kein Short-Circuit nach dem ersten Fehler."""
    ev = g.evaluate_entry_order(**_entry_kwargs(kill_switch_active=True, stop_px=None))
    assert ev.allowed is False
    assert len(ev.failed_checks) >= 2
    assert "Kill-Switch" in ev.reason


def test_entry_order_blocks_on_kill_switch():
    ev = g.evaluate_entry_order(**_entry_kwargs(kill_switch_active=True))
    assert ev.allowed is False


def test_entry_order_blocks_on_demo_mode():
    ev = g.evaluate_entry_order(**_entry_kwargs(live_trading_allowed=False))
    assert ev.allowed is False


def test_entry_order_blocks_on_equity_floor():
    ev = g.evaluate_entry_order(**_entry_kwargs(equity_usd=100, equity_start_usd=250))
    assert ev.allowed is False


def test_entry_order_blocks_on_missing_stop():
    ev = g.evaluate_entry_order(**_entry_kwargs(stop_px=None))
    assert ev.allowed is False


def test_entry_order_blocks_on_cooldown_only_for_new_symbol():
    recently_closed = NOW - timedelta(hours=1)
    ev_new = g.evaluate_entry_order(**_entry_kwargs(is_new_symbol=True, last_closed_at=recently_closed))
    assert ev_new.allowed is False
    # Nachkauf auf bestehendes Symbol: Cooldown greift gar nicht erst
    ev_existing = g.evaluate_entry_order(**_entry_kwargs(is_new_symbol=False, last_closed_at=recently_closed))
    assert ev_existing.allowed is True


def test_entry_order_blocks_on_leverage_without_favorable_funding():
    ev = g.evaluate_entry_order(**_entry_kwargs(leverage=2.0, funding_rate_hourly=0.0002))
    assert ev.allowed is False


def test_entry_order_defaults_trust_and_exposure_to_safe_values():
    """Aufrufer von vor dieser Erweiterung (equity_trusted/open_notional_usd
    nicht angegeben) duerfen nicht ploetzlich blockiert werden."""
    ev = g.evaluate_entry_order(**_entry_kwargs())
    assert ev.allowed is True


def test_entry_order_blocks_on_untrusted_equity():
    ev = g.evaluate_entry_order(**_entry_kwargs(equity_trusted=False))
    assert ev.allowed is False
    assert "vertrauenswürdig" in ev.reason


def test_entry_order_blocks_on_total_exposure():
    # equity=250, bereits 350 offen + 50 neu = 400 -> 160% > 150%-Grenze
    ev = g.evaluate_entry_order(**_entry_kwargs(open_notional_usd=350.0))
    assert ev.allowed is False
    assert "Exposure" in ev.reason


def test_entry_order_blocks_on_portfolio_heat():
    # equity=250, max_portfolio_heat_pct=2.0 -> 5$ erlaubt, 6$ Heat > Limit
    ev = g.evaluate_entry_order(**_entry_kwargs(
        limits={**_LIMITS, "max_portfolio_heat_pct": 2.0}, order_heat_usd=6.0))
    assert ev.allowed is False
    assert "Portfolio-Heat" in ev.reason


def test_entry_order_blocks_on_same_side_concentration():
    ev = g.evaluate_entry_order(**_entry_kwargs(
        limits={**_LIMITS, "max_same_side_positions": 1}, same_side_open_count=1))
    assert ev.allowed is False
    assert "gleichgerichtet" in ev.reason


def test_entry_order_blocks_on_loss_streak():
    ev = g.evaluate_entry_order(**_entry_kwargs(
        consecutive_losses=4, last_loss_closed_at=NOW - timedelta(hours=1)))
    assert ev.allowed is False
    assert "Verlierer in Folge" in ev.reason


def test_entry_order_defaults_heat_and_streak_to_safe_values():
    """Aufrufer von vor Phase 5 (order_heat_usd/same_side_open_count/
    consecutive_losses nicht angegeben) duerfen nicht ploetzlich blockiert
    werden - nur max_portfolio_heat_pct/max_same_side_positions selbst
    bleiben fail-closed, wenn sie in `limits` fehlen (siehe _LIMITS oben,
    das beide bewusst grosszuegig fuehrt)."""
    ev = g.evaluate_entry_order(**_entry_kwargs())
    assert ev.allowed is True


# --- evaluate_exit_order: bewusst laxer als evaluate_entry_order ---

def test_exit_order_allowed_even_at_deep_loss():
    """Equity weit unter dem Boden - ein Ausstieg muss trotzdem durchgehen,
    sonst haelt der Guard genau die Position fest, die er verhindern soll."""
    ev = g.evaluate_exit_order(
        live_trading_allowed=True,
        order_notional_usd=30.0, max_reasonable_notional_usd=200.0,
    )
    assert ev.allowed is True


def test_exit_order_not_blocked_by_kill_switch():
    """Der Kill-Switch existiert als Parameter absichtlich NICHT in
    evaluate_exit_order - er stoppt nur neue Einstiege. core/bot.py's
    'Equity-Boden -> alles schliessen' braucht genau das, um noch offene
    Positionen zu schliessen, NACHDEM der Switch schon ausgeloest wurde."""
    import inspect
    assert "kill_switch_active" not in inspect.signature(g.evaluate_exit_order).parameters


def test_exit_order_still_blocked_by_demo_mode():
    ev = g.evaluate_exit_order(
        live_trading_allowed=False,
        order_notional_usd=30.0, max_reasonable_notional_usd=200.0,
    )
    assert ev.allowed is False


def test_exit_order_still_catches_implausible_size():
    ev = g.evaluate_exit_order(
        live_trading_allowed=True,
        order_notional_usd=5000.0, max_reasonable_notional_usd=200.0,
    )
    assert ev.allowed is False


# --- Fail-closed statt fail-open ---

def test_total_exposure_blocks_when_limit_is_missing():
    """Fehlt die Grenze, wird BLOCKIERT. Vorher stand am Aufrufer ein Default
    von 999999.0 - ein fehlender oder vertippter Schluessel haette den Guard
    lautlos abgeschaltet, statt aufzufallen."""
    assert not g.check_total_exposure(50.0, 0.0, 250.0, None).allowed

    limits_without = {k: v for k, v in _LIMITS.items() if k != "max_total_exposure_pct"}
    evaluation = g.evaluate_entry_order(**_entry_kwargs(limits=limits_without))
    assert not evaluation.allowed
    assert "Gesamt-Exposure-Limit" in evaluation.reason


# --- Uebernahme-Kette (evaluate_adoption) ---

def _adopt_kwargs(**overrides):
    kwargs = dict(
        live_trading_allowed=True, order_notional_usd=50.0,
        max_reasonable_notional_usd=200.0, equity_usd=250.0,
        leverage=1.0, funding_rate_hourly=0.0, stop_px=90.0, side="long",
        entry_px=100.0, current_open_count=0, limits=_LIMITS,
        equity_trusted=True, open_notional_usd=0.0,
    )
    kwargs.update(overrides)
    return kwargs


def test_adoption_allows_a_plain_position():
    assert g.evaluate_adoption(**_adopt_kwargs()).allowed


def test_adoption_blocks_on_cumulative_exposure():
    """Der gemeldete Fall: einzeln unauffaellig, in der Summe ueber dem Limit."""
    evaluation = g.evaluate_adoption(**_adopt_kwargs(open_notional_usd=340.0))
    assert not evaluation.allowed
    assert "Gesamt-Exposure" in evaluation.reason


def test_adoption_blocks_leverage_above_limit():
    assert not g.evaluate_adoption(**_adopt_kwargs(leverage=20.0)).allowed


def test_adoption_blocks_oversized_position():
    assert not g.evaluate_adoption(**_adopt_kwargs(order_notional_usd=150.0)).allowed


def test_adoption_blocks_without_a_stop():
    assert not g.evaluate_adoption(**_adopt_kwargs(stop_px=None)).allowed


def test_adoption_ignores_pacing_rules_by_design():
    """Taktregeln (Tageslimit, Cooldown, Mindestordergroesse) sind fuer das
    AUSWAEHLEN von Trades da. Eine bereits offene Fremdposition unbeaufsichtigt
    und ohne Stop zu lassen, weil das Tageskontingent erschoepft ist, waere
    die falsche Reaktion - deshalb taucht keiner dieser Guards hier auf."""
    # Eine Staubposition weit unter der Mindestordergroesse wird trotzdem
    # angenommen: Schutz vor gar keinem Schutz.
    assert g.evaluate_adoption(**_adopt_kwargs(order_notional_usd=0.5)).allowed


def test_adoption_still_respects_demo_mode():
    assert not g.evaluate_adoption(**_adopt_kwargs(live_trading_allowed=False)).allowed
