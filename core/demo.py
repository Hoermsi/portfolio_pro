"""Demo-Modus: fiktives, aber plausibles Beispiel-Depot in einer separaten DB.

Wird nur aktiv, wenn der Nutzer den Demo-Modus einschaltet (app.py schaltet dann
config.DB_PATH auf die Demo-Datei um). `ensure_seeded()` befüllt die Demo-DB
einmalig mit echten, liquiden Titeln (fiktive Stückzahlen/Einstände), Cash, einem
Risikoprofil und einem deterministisch erzeugten ~18-Monats-Verlauf. Die aktuellen
Kurse holt die App danach ganz normal live - so ergeben die Zahlen Sinn, ohne echte
Vermögensdaten preiszugeben. Reine db/profile-Aufrufe, kein Streamlit-Import.
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta

from core import db
from core.profile import save_risk_profile

# Echte, liquide Ticker mit fiktiven Mengen + Einständen (€), Mix aus G/V.
SEED_STOCKS = [
    # symbol, name, menge, einstand_eur
    ("AAPL", "Apple Inc.", 40, 150.0),
    ("MSFT", "Microsoft Corp.", 25, 300.0),
    ("NVDA", "NVIDIA Corporation", 60, 90.0),
    ("GOOGL", "Alphabet Inc.", 30, 140.0),
    ("IWDA.AS", "iShares Core MSCI World", 100, 90.0),
]
SEED_CRYPTO = [
    ("BTC", "Bitcoin", 0.35, 45000.0),
    ("ETH", "Ethereum", 4.0, 2500.0),
    ("SOL", "Solana", 40.0, 100.0),
    ("ADA", "Cardano", 5000.0, 0.40),
]
SEED_CASH = 12000.0

_STOCK_CATEGORY = "Demo-Depot"
_CRYPTO_CATEGORY = "Demo-Kraken"

# Endpunkte (€) des synthetischen Verlaufs je Anlageklasse - grob passend zum
# heutigen Live-Wert des Seed-Depots, damit der Übergang zum ersten Live-Snapshot
# unauffällig bleibt.
_HISTORY_DAYS = 540
_HISTORY_TARGETS = {
    # asset_type: (startwert, endwert, tages-volatilität)
    "stock": (22000.0, 38000.0, 0.008),
    "crypto": (12000.0, 40000.0, 0.030),
    "cash": (SEED_CASH, SEED_CASH, 0.0),
}
_SEED = 20240614  # fester Seed -> stabiler Verlauf über App-Neustarts hinweg


def is_seeded() -> bool:
    return bool(db.get_meta("demo_seeded"))


def _synthetic_series(start: float, end: float, vol: float, days: int,
                      rng: random.Random) -> list[float]:
    """Multiplikativer Random-Walk mit Drift von start nach end (auf end skaliert)."""
    if days < 2:
        return [end]
    drift = (end / start) ** (1 / (days - 1)) - 1 if start > 0 else 0.0
    values = [start]
    v = start
    for _ in range(days - 1):
        shock = rng.uniform(-vol, vol)
        v = max(1.0, v * (1 + drift + shock))
        values.append(v)
    # Sauber auf den Zielwert enden lassen (Knick am Übergang minimieren).
    factor = end / values[-1] if values[-1] else 1.0
    return [x * factor for x in values]


def _seed_history():
    """~18 Monate Tages-Snapshots je Anlageklasse deterministisch erzeugen."""
    rng = random.Random(_SEED)
    start_day = date.today() - timedelta(days=_HISTORY_DAYS)
    dates = [start_day + timedelta(days=i) for i in range(_HISTORY_DAYS)]
    for asset_type, (start, end, vol) in _HISTORY_TARGETS.items():
        series = _synthetic_series(start, end, vol, len(dates), rng)
        for d, value in zip(dates, series):
            db.save_snapshot(asset_type, round(value, 2), snap_date=d.isoformat())


def ensure_seeded():
    """Demo-DB einmalig befüllen. Idempotent (No-op, wenn schon geschehen)."""
    if is_seeded():
        return

    for symbol, name, qty, cost in SEED_STOCKS:
        db.save_position(symbol, "stock", qty, cost, category=_STOCK_CATEGORY,
                         source="demo", name=name)
    for symbol, name, qty, cost in SEED_CRYPTO:
        db.save_position(symbol, "crypto", qty, cost, category=_CRYPTO_CATEGORY,
                         source="demo", name=name)
    db.add_cash_entry(SEED_CASH)

    # Ersteinrichtung überspringen + plausible Leitplanken setzen.
    db.set_meta("onboarded", datetime.now().isoformat(timespec="seconds"))
    db.set_meta("target_allocation", json.dumps({"stock": 45, "crypto": 40, "cash": 15}))
    save_risk_profile(6, 8.0, 2050, 500.0)

    _seed_history()
    db.set_meta("demo_seeded", datetime.now().isoformat(timespec="seconds"))
