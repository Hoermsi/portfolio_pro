"""Trading-Bot: EINE Uhr fuer alle Zeitstempel - tz-aware UTC.

WARUM ES DIESES MODUL GIBT: core/db.py schrieb bislang durchgehend
datetime.now() (lokale Wanduhr, tz-NAIV), data/hyperliquid.py dagegen
pd.to_datetime(ms, unit="ms") ohne utc=True (ebenfalls tz-naiv, aber in
Wirklichkeit UTC). Beide Werte sind vom PYTHON-TYP her identisch - ein
Vergleich zwischen ihnen wirft deshalb NIE eine Exception, liegt aber je
nach Jahreszeit bis zu zwei Stunden daneben. Genau das hat die Lern-Label-
Fenster in bot_runner._signal_outcome() verschoben, ohne dass es irgendwo
aufgefallen waere.

Dieses Modul ist ab Strategie V2 die EINZIGE Stelle, die "jetzt" fuer den
Bot definiert. Jeder neue Bot-Schreibpfad (core/db.py, core/bot.py,
core/bot_config.py, core/bot_signals.py, bot_runner.py, core/bot_live.py)
geht ausschliesslich hierueber - und weil alle Werte tz-AWARE sind, wirft
eine kuenftige Vermischung mit einem noch-naiven Wert eine laute
TypeError statt still falsch zu rechnen.

views/trading_bot.py ist die einzige Stelle, die in die Gegenrichtung
rechnet: Speicherung bleibt immer UTC, erst beim ANZEIGEN wird ueber
to_local()/local_str() in die lokale Systemzeit umgerechnet.
"""
from datetime import datetime, timezone


def now_utc() -> datetime:
    """Aktueller Zeitpunkt, tz-aware UTC. Ersetzt jedes datetime.now() auf
    einem Bot-Schreibpfad."""
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None = None) -> str:
    """ISO-8601 MIT UTC-Offset (z.B. "2026-09-03T08:31:00+00:00"), Sekunden-
    Aufloesung - fuer DB-Spalten und Meta-Werte. Ein `dt` ohne eigene
    Zeitzone wird als bereits-UTC behandelt statt eine Exception zu werfen,
    damit ein versehentlich naiv erzeugter Wert nicht die ganze Kette
    sprengt."""
    dt = dt or now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value) -> datetime | None:
    """Parst einen ISO-Zeitstempel (String oder datetime) zu tz-aware UTC.

    Ein Wert OHNE Zeitzone (z.B. ein Rest aus der Zeit vor diesem Modul,
    oder aus einer Alt-Installation) wird als bereits-UTC interpretiert
    statt eine Exception zu werfen - dieselbe Read-Time-Robustheit wie bei
    core.bot_config: ein kaputter oder alter Zeitstempel darf keinen Guard
    sprengen. Gibt None zurueck, wenn der Wert gar nicht parsbar ist."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_local(value) -> datetime | None:
    """UTC-Zeitstempel (String oder datetime) in die lokale Systemzeit
    umgerechnet - AUSSCHLIESSLICH fuer die Anzeige (views/trading_bot.py).
    Jeder Vergleich/jede Guard-Berechnung bleibt in UTC, siehe now_utc()."""
    dt = parse_utc(value)
    if dt is None:
        return None
    return dt.astimezone()


def local_str(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Formatierte lokale Zeit fuer die UI. Faellt auf den rohen Wert
    zurueck, wenn er sich nicht parsen laesst, damit eine kaputte oder
    fremde Zeichenkette die Seite nie zum Absturz bringt, sondern nur
    haesslich anzeigt."""
    dt = to_local(value)
    if dt is None:
        return str(value) if value else ""
    return dt.strftime(fmt)
