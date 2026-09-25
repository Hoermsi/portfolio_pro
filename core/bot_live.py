"""Read-only-Vorprüfung für Phase F (Scharfschuss-Test).

Dieses Modul sendet absichtlich keine Order. Es prüft nur, ob die env-only
Hyperliquid-Zugangsdaten vorhanden sind, ob das Konto lesbar ist und ob die
für den manuellen Minimaltest benötigte Börsenkonfiguration erreichbar ist.
Für den Scharfschuss muss das Konto außerdem vollständig flach sein: keine
offenen Positionen und keine offenen Orders.
Die eigentliche Testorder bleibt eine bewusste Nutzeraktion außerhalb der UI.
"""
from __future__ import annotations

from core import clock, config, db


def read_only_preflight(symbol: str = "BTC") -> dict:
    """Liest den Account einmalig und liefert nur status-/prüfbare Fakten.

    Vermerkt einen erfolgreichen Lauf in `meta["bot_last_preflight_ok_at"]" -
    das ist die Voraussetzung, die core.bot_config.preflight_is_fresh() für
    den Live-Schalter in der UI prüft (kein Wechsel in den Live-Modus "ins
    Blaue", ohne dass die Verbindung kürzlich nachweislich funktioniert hat)."""
    from data.hyperliquid import LiveExchange, MIN_NOTIONAL_USD, supported_symbols

    key, address = config.hyperliquid_credentials()
    checks = [
        {"label": "Agent-Wallet-Key aus Umgebung vorhanden", "ok": bool(key)},
        {"label": "Hauptkonto-Adresse aus Umgebung vorhanden", "ok": bool(address)},
        {"label": "Nicht im Demo-Modus", "ok": config.DB_PATH != config.DEMO_DB_PATH},
    ]
    result = {"ok": False, "checks": checks, "symbol": symbol.upper(),
              "equity_usd": None, "positions": [], "open_orders": 0, "error": None,
              "min_notional_usd": MIN_NOTIONAL_USD}
    if not all(c["ok"] for c in checks):
        result["error"] = "Zugangsdaten oder Demo-Modus verhindern die Read-only-Prüfung."
        return result

    try:
        exchange = LiveExchange()
        state = exchange.account_state()
        open_orders = exchange.frontend_open_orders()
        symbols = supported_symbols()
        result["equity_usd"] = state.equity_usd
        result["positions"] = [p.symbol for p in state.positions]
        result["open_orders"] = len(open_orders)
        checks.extend([
            {"label": "Hyperliquid-Konto lesbar", "ok": True},
            {"label": f"{symbol.upper()} als Perp-Markt verfügbar", "ok": symbol.upper() in symbols},
            {"label": "Kontostand ist positiv", "ok": state.equity_usd > 0},
            {"label": "Keine offenen Hyperliquid-Positionen", "ok": not state.positions},
            {"label": "Keine offenen Hyperliquid-Orders", "ok": not open_orders},
        ])
        result["ok"] = all(c["ok"] for c in checks)
    except Exception as exc:  # noqa: BLE001 - UI erhält einen klaren Read-only-Fehler
        result["error"] = str(exc)
        checks.append({"label": "Hyperliquid-Konto lesbar", "ok": False})

    if result["ok"]:
        db.set_meta("bot_last_preflight_ok_at", clock.iso_utc())
    return result
