"""Einmaliger, strikt begrenzter Hyperliquid-Scharfschuss-Test.

Der Test umgeht bewusst den dauerhaften bot_runner und auch das Papier-Start-
kapital in ``bot_start``. Er prüft das echte Konto, eröffnet genau eine BTC-
Long-Position mit 12 USD Nominalwert und 1x Hebel, setzt den Stop 1 % unter
dem aktuellen Mid-Preis, schließt die Position wieder und räumt den Stop auf.

Ausführung nur mit dem expliziten Bestätigungstoken:
    python phase_f_shot.py --confirm BTC-LONG-12USD-1X-STOP1PCT

Der Test verwendet echte Orders und darf niemals parallel zum Runner laufen.
"""
from __future__ import annotations

import argparse
import sys
import time

from core import bot_config, db
from data.hyperliquid import LiveExchange, OrderResult

SYMBOL = "BTC"
NOTIONAL_USD = 12.0
LEVERAGE = 1.0
STOP_DISTANCE = 0.01
CONFIRMATION = "BTC-LONG-12USD-1X-STOP1PCT"


def _cancel(exchange: LiveExchange, oid: str) -> tuple[bool, str]:
    """Stop-Order gezielt stornieren; andere Orders werden nie angefasst."""
    try:
        response = exchange._exchange.cancel(SYMBOL, int(oid))  # SDK-Client
        if response.get("status") == "ok":
            return True, "ok"
        return False, str(response)
    except Exception as exc:  # noqa: BLE001 - CLI muss Aufräumfehler melden
        return False, str(exc)


def _open_orders(exchange: LiveExchange) -> list[dict]:
    return exchange.frontend_open_orders()


def _log_order(*, intent: str, result: OrderResult, requested_px: float | None,
               side: str = "long"):
    db.add_bot_order(
        symbol=SYMBOL,
        intent=intent,
        side=side,
        size=result.filled_size,
        order_type="market",
        requested_px=requested_px,
        status=result.status,
        exchange_oid=result.stop_oid,
        fill_px=result.fill_px,
        fee_usd=result.fee_usd,
        error=result.error,
    )


def run() -> int:
    db.init_db()
    # Ein neuer Versuch invalidiert einen alten Nachweis: bei einem Fehler
    # darf die UI nicht weiter so tun, als sei die aktuelle Orderlogik geprüft.
    db.delete_meta("bot_phase_f_verified_at")
    if not bot_config.live_trading_allowed():
        print("ABBRUCH: Demo-Modus aktiv.")
        return 2

    exchange = LiveExchange()
    state = exchange.account_state()
    existing_orders = _open_orders(exchange)
    if state.positions or existing_orders:
        print("ABBRUCH: Konto ist nicht leer (Positionen/Orders vorhanden).")
        return 2
    if state.equity_usd < NOTIONAL_USD:
        print(f"ABBRUCH: Kontowert {state.equity_usd:.2f} $ < {NOTIONAL_USD:.2f} $.")
        return 2

    mid = exchange.mid_price(SYMBOL)
    if not mid or mid <= 0:
        print("ABBRUCH: Kein gültiger BTC-Mid-Preis.")
        return 2
    stop_px = mid * (1.0 - STOP_DISTANCE)
    print(f"Starte BTC-Long-Test: {NOTIONAL_USD:.2f} $ · 1x · Mid {mid:.2f} $ · Stop {stop_px:.2f} $")

    opened = exchange.open_long(SYMBOL, NOTIONAL_USD, LEVERAGE, stop_px)
    _log_order(intent="phase_f_test_open", result=opened, requested_px=mid)
    if opened.status != "filled" or not opened.stop_oid or opened.error:
        print(f"FEHLER beim Eröffnen/Absichern: {opened.error or opened.status}")
        # Falls trotz fehlendem Stop eine Position offen ist, sofort schließen.
        if any(p.symbol == SYMBOL for p in exchange.account_state().positions):
            emergency = exchange.close_position(SYMBOL)
            _log_order(intent="phase_f_test_emergency_close", result=emergency,
                       requested_px=exchange.mid_price(SYMBOL))
            print(f"Notausstieg: {emergency.status} {emergency.error or ''}")
        if opened.stop_oid:
            _cancel(exchange, str(opened.stop_oid))
        return 1

    stop_oid = str(opened.stop_oid)
    visible_orders = _open_orders(exchange)
    if not any(str(order.get("oid")) == stop_oid for order in visible_orders):
        print(f"FEHLER: Stop-OID {stop_oid} nicht in den offenen Orders sichtbar.")
        emergency = exchange.close_position(SYMBOL)
        _log_order(intent="phase_f_test_emergency_close", result=emergency,
                   requested_px=exchange.mid_price(SYMBOL))
        _cancel(exchange, stop_oid)
        return 1
    print(f"Fill bestätigt · Stop-OID {stop_oid} sichtbar.")

    # Kurz warten, damit Fill/Stop in den öffentlichen Lesedaten sicher
    # angekommen sind; niemals auf die Stop-Auslösung warten.
    time.sleep(1.0)
    closed = exchange.close_position(SYMBOL)
    _log_order(intent="phase_f_test_close", result=closed,
               requested_px=exchange.mid_price(SYMBOL))
    if closed.status != "filled":
        print(f"FEHLER beim Schließen: {closed.error or closed.status}")
        return 1

    cancelled, cancel_msg = _cancel(exchange, stop_oid)
    if not cancelled and any(str(o.get("oid")) == stop_oid for o in _open_orders(exchange)):
        print(f"FEHLER: Stop-OID {stop_oid} konnte nicht gelöscht werden: {cancel_msg}")
        return 1

    final_state = exchange.account_state()
    final_orders = _open_orders(exchange)
    if final_state.positions or final_orders:
        print("FEHLER: Nach dem Test sind Positionen oder Orders übrig.")
        print(f"Positionen: {len(final_state.positions)} · Orders: {len(final_orders)}")
        return 1

    bot_config.mark_phase_f_verified()
    pnl = (closed.fill_px - opened.fill_px) * (opened.filled_size or 0.0) if closed.fill_px and opened.fill_px else 0.0
    print(f"ERFOLG: Position geschlossen · realisierte Kursdifferenz {pnl:+.6f} $ · offene Positionen 0 · Orders 0")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True, help="Exaktes Bestätigungstoken")
    args = parser.parse_args()
    if args.confirm != CONFIRMATION:
        print("ABBRUCH: Bestätigungstoken stimmt nicht.")
        return 2
    return run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("ABBRUCH: vom Nutzer unterbrochen.")
        raise SystemExit(130)
