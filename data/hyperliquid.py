"""Hyperliquid-Börsenanbindung (Perpetuals) für den Trading-Bot.

Zwei Implementierungen hinter demselben Protokoll (core/bot.py, Phase C,
kennt nur dieses Protokoll, nicht welche der beiden dahinter läuft):

- LiveExchange: echte, signierte Order über hyperliquid-python-sdk. Braucht
  HYPERLIQUID_AGENT_KEY (Agent-Wallet-Privatschlüssel - KANN HANDELN, NICHT
  AUSZAHLEN) + HYPERLIQUID_ACCOUNT_ADDRESS (öffentliche Hauptkonto-Adresse,
  die Agent-Wallet selbst hält kein Geld) in der Umgebung, siehe
  core.config.hyperliquid_credentials(). NICHT gegen ein echtes Konto
  getestet in dieser Session (keine Private Keys verfügbar/gewünscht) - der
  Plan sieht dafür in Phase F einen dedizierten Scharfschuss-Test mit
  minimalem Echtgeld-Einsatz vor, bevor produktiv gehandelt wird.
- PaperExchange: simulierte Fills gegen echte Marktpreise, sendet NIE eine
  echte Order. Preis-/Funding-Quelle ist austauschbar (Default: die
  öffentlichen, anmeldungsfreien Lesefunktionen unten) - Tests injizieren
  feste Werte, exakt das core/shadow.py-Muster (dort: price_eur monkey-
  patchen statt HTTP mocken).

Die lesenden Modulfunktionen (mid_price, funding_rate_hourly,
supported_symbols) brauchen keine Anmeldung und sind gegen die echte
Hyperliquid-API testbar, ohne je zu handeln.
"""
import math
import time
from dataclasses import dataclass, field
from typing import Protocol

from hyperliquid.exchange import Exchange as _HlExchange
from hyperliquid.info import Info as _HlInfo
from hyperliquid.utils import constants as _hl_constants

from core import config
from core.cache import ttl_cache

MAINNET_URL = _hl_constants.MAINNET_API_URL
TESTNET_URL = _hl_constants.TESTNET_API_URL

MIN_NOTIONAL_USD = 10.0     # Hyperliquid-Mindestordergröße (Nominale)
TAKER_FEE = 0.00045         # Basis-Tier (< 5 Mio. $ 14-Tage-Volumen)
MAKER_FEE = 0.00015
# 1% statt SDK-Default 5%: unsere Ordergrößen (~200-300 $ Gesamtkapital) sind
# klein genug, dass ein enges Slippage-Band selten reisst, verhindert aber
# einen extrem schlechten Fill bei einem duennen Orderbuch.
_ENTRY_SLIPPAGE = 0.01

# Erwarteter Ausfuehrungsnachteil je Seite (halber Spread + Marktimpact) fuer
# PaperExchange UND analysis/bot_backtest.py. NICHT dasselbe wie
# _ENTRY_SLIPPAGE: das ist der aggressive Limitpreis-Versatz, mit dem eine
# LiveExchange-Order sicher marktfaehig wird, keine Kostenschaetzung.
#
# Bis hierhin fuellte das Papierdepot exakt zum mid_price - ein Ergebnis, das
# sich auf einem echten Orderbuch nie einstellt und die Papier-Bilanz
# systematisch zu gut aussehen liess. Bewusst konservativ gewaehlt: 0,05 % je
# Seite, also 0,10 % Round-Trip zusaetzlich zu 2 x TAKER_FEE (0,09 %). Auf
# BTC/ETH ist das eher zu viel, auf duennen Altcoin-Perps eher zu wenig - im
# Zweifel soll die Simulation untertreiben, nicht uebertreiben.
PAPER_SLIPPAGE_PCT = 0.0005


def slipped_price(price: float, side: str, closing: bool = False) -> float:
    """Ausfuehrungskurs inklusive Slippage, IMMER zum Nachteil des Handelnden:
    ein Long kauft teurer und verkauft billiger, ein Short umgekehrt."""
    opening_buy = (side == "long") if not closing else (side == "short")
    factor = 1.0 + PAPER_SLIPPAGE_PCT if opening_buy else 1.0 - PAPER_SLIPPAGE_PCT
    return price * factor
_STOP_VERIFY_ATTEMPTS = 5
_STOP_VERIFY_DELAY = 0.2


class HyperliquidError(Exception):
    pass


@dataclass(frozen=True)
class Position:
    symbol: str
    side: str                       # "long" | "short"
    size: float
    entry_px: float
    leverage: float
    unrealized_pnl_usd: float
    liquidation_px: float | None = None


@dataclass(frozen=True)
class AccountState:
    equity_usd: float
    withdrawable_usd: float
    positions: list[Position] = field(default_factory=list)
    # False, wenn equity_usd moeglicherweise nur die Margin offener Positionen
    # ist statt der echten Gesamt-Equity (Unified-/Portfolio-Margin-Konto,
    # dessen Spot-/USDC-Saldo nicht lesbar war) - siehe LiveExchange.account_state().
    # Default True: PaperExchange und alte Tests bauen AccountState ohne dieses
    # Feld und meinen damit immer eine vertrauenswuerdige Messung.
    equity_trusted: bool = True


@dataclass(frozen=True)
class OrderResult:
    status: str                     # "filled" | "error"
    fill_px: float | None = None
    filled_size: float | None = None
    fee_usd: float | None = None
    stop_oid: str | None = None
    error: str | None = None
    # TATSAECHLICH angewandter Hebel - kann vom angeforderten abweichen
    # (LiveExchange._ensure_leverage rundet auf eine Ganzzahl AB, Hyperliquid
    # erlaubt keine Nachkommastellen). None nur, wenn der Aufrufer selbst
    # keinen Hebel kennt (z.B. ein Fehlerergebnis vor der Hebel-Anwendung).
    leverage: float | None = None
    # TATSAECHLICH platzierter Stop-Kurs, aus dem bestaetigten Fill berechnet
    # (open_long/open_short nehmen `stop_distance_pct`, keinen fertigen
    # Kurs) - core.bot.open_position() speichert GENAU diesen Wert, nicht
    # selbst nochmal berechnet aus einem moeglicherweise veralteten Scan-Kurs.
    stop_px: float | None = None

    def __bool__(self) -> bool:
        return self.status == "filled"


class ExchangeProtocol(Protocol):
    """Gemeinsames Interface, gegen das core/bot.py (Phase C) programmiert -
    strukturell identisch für LiveExchange und PaperExchange (kein
    gemeinsamer Basisklassen-Zwang, beide erfüllen es per Duck-Typing)."""

    def mid_price(self, symbol: str) -> float | None: ...
    def funding_rate_hourly(self, symbol: str) -> float | None: ...
    def account_state(self) -> AccountState: ...
    # stop_distance_pct, NICHT stop_px: der Stop wird ERST nach dem
    # bestaetigten Fill aus dem tatsaechlichen Einstandskurs berechnet, nicht
    # aus einem vor der Order bekannten (moeglicherweise veralteten) Kurs -
    # siehe LiveExchange._place_entry_and_stop.
    def open_long(self, symbol: str, notional_usd: float, leverage: float,
                 stop_distance_pct: float) -> OrderResult: ...
    def open_short(self, symbol: str, notional_usd: float, leverage: float,
                   stop_distance_pct: float) -> OrderResult: ...
    def close_position(self, symbol: str) -> OrderResult: ...


# --- RETRY MIT BACKOFF (nur fuer LESENDE Info-Aufrufe) ---
#
# data/hyperliquid.py hatte bislang KEINERLEI Retry-Logik: ein einzelner
# transienter Fehlschlag (Rate-Limit, kurzer Netzwerk-Ruckler) liess jede
# aufrufende Funktion sofort mit None/leer zurueckkehren (siehe deren
# Docstrings) - im Ruecktest/Walk-Forward (analysis/bot_backtest.py,
# analysis/bot_ablation.py) sah das identisch aus wie ein echter, laengerer
# Datenausfall (Phase 1s Guards schlagen dann korrekt, aber unnoetig, laut
# an). Beobachteter Fall: ein Walk-Forward-Lauf UND der 15-Minuten-Runner-
# Takt trafen zeitlich zusammen auf dieselbe Hyperliquid-API, mehrere
# Symbole und sogar BTCs Tagesdaten kamen mit 0 Kerzen zurueck, obwohl ein
# einzelner erneuter Versuch Sekunden spaeter (manuell getestet) sofort
# funktionierte.
#
# BEWUSST NUR fuer LESENDE Aufrufe (candles/funding/mids/account_state) -
# NIE fuer Order-Platzierung/-Aenderung/-Stornierung: ein wiederholter
# Schreibversuch koennte eine bereits erfolgreich ausgefuehrte Order
# verdoppeln, ohne dass Hyperliquid dafuer eine Idempotenz-Garantie bietet.
# Die Order-Pfade (LiveExchange._place_entry_and_stop, replace_stop_order,
# market_open/market_close, _ensure_leverage) bleiben deshalb unveraendert
# ohne Retry - ihre bestehende Fehlerbehandlung (OrderResult.error,
# core.bot's Notausstieg bei fehlgeschlagener Stop-Order) ist dafuer der
# richtige, bereits vorhandene Mechanismus.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_BASE_SECONDS = 0.5  # 0.5s, 1s (zwischen den 3 Versuchen)


def _with_retry(fn, *args, **kwargs):
    """Ruft `fn(*args, **kwargs)` auf, versucht bei einer Exception bis zu
    `_RETRY_ATTEMPTS`-mal erneut (exponentielles Backoff), und wirft danach
    die LETZTE Exception unveraendert weiter. Jeder bestehende Aufrufer hat
    bereits ein eigenes `except Exception` um den Originalaufruf (Rueckgabe
    None/Abbruch der Schleife) - dieses Verhalten bleibt exakt erhalten,
    nur dass ein einzelner kurzer Ausfall jetzt meist gar nicht mehr bis
    dorthin durchdringt."""
    last_exc = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < _RETRY_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_BASE_SECONDS * (2 ** attempt))
    raise last_exc


# --- ÖFFENTLICHE, ANMELDUNGSFREIE INFO-EBENE ---
# Ein geteilter Info-Client mit skip_ws=True: der Bot pollt (core/bot.py,
# Phase C, deterministischer Zyklus alle 15 Min), statt einen WebSocket
# dauerhaft offen zu halten - passt zum "kein Dauerprozess-Zustand"-Charakter
# der App. Kurze TTLs, damit Guard-Entscheidungen nicht auf Minuten alten
# Daten laufen, aber wiederholte Aufrufe im selben Zyklus nicht jedes Mal neu
# ins Netz gehen (Muster: core/cache.ttl_cache, wie im Rest der App).

@ttl_cache(5)
def _info_client() -> _HlInfo:
    return _HlInfo(MAINNET_URL, skip_ws=True)


@ttl_cache(5)
def _mids() -> dict[str, str]:
    return _with_retry(_info_client().all_mids)


@ttl_cache(30)
def _meta_and_ctxs() -> list:
    return _with_retry(_info_client().meta_and_asset_ctxs)


def _universe_and_ctxs() -> tuple[list[dict], list[dict]]:
    data = _meta_and_ctxs()
    return data[0]["universe"], data[1]


def mid_price(symbol: str) -> float | None:
    """Live-Mid-Preis in USD - öffentliche API, keine Anmeldung nötig."""
    raw = _mids().get(symbol.upper())
    return float(raw) if raw is not None else None


def funding_rate_hourly(symbol: str) -> float | None:
    """Aktuelle STÜNDLICHE Funding-Rate als Dezimalbruch (Hyperliquid rechnet
    und zahlt stündlich ab, kein zusätzlicher Umrechnungsfaktor nötig), z.B.
    0.0001 = 0,01%/h. > 0: Longs zahlen Shorts. None, wenn das Symbol nicht
    gelistet ist oder das Feld fehlt."""
    symbol = symbol.upper()
    universe, ctxs = _universe_and_ctxs()
    for idx, asset in enumerate(universe):
        if asset["name"] == symbol:
            raw = ctxs[idx].get("funding")
            return float(raw) if raw is not None else None
    return None


def day_notional_volume_usd(symbol: str) -> float | None:
    """24h-Handelsvolumen in USD (Hyperliquid-Feld `dayNtlVlm`) - Liquiditaets-
    Pflichtbedingung der V2-Signal-Engine (core.bot_signals, Gate 7): ein duenn
    gehandelter Markt laesst sich leicht bewegen und ist fuer Stop/Fill-
    Qualitaet riskanter, unabhaengig davon, wie sauber sein Chart aussieht.
    None, wenn das Symbol nicht gelistet ist oder das Feld fehlt - wird von
    core.bot_signals.evaluate() als "Liquiditaet unbekannt" behandelt (blockt,
    statt optimistisch durchzulassen)."""
    symbol = symbol.upper()
    universe, ctxs = _universe_and_ctxs()
    for idx, asset in enumerate(universe):
        if asset["name"] == symbol:
            raw = ctxs[idx].get("dayNtlVlm")
            return float(raw) if raw is not None else None
    return None


def supported_symbols() -> set[str]:
    """Alle auf Hyperliquid handelbaren Perp-Symbole - der Bot darf nur
    innerhalb dieser Menge Kandidaten vorschlagen. Viele Coins, die die
    App sonst führt (z.B. ENJ), sind hier nicht gelistet."""
    universe, _ = _universe_and_ctxs()
    return {a["name"] for a in universe}


@ttl_cache(300)
def _abstraction_mode(info: _HlInfo, address: str) -> str | None:
    """Kontomodus (u.a. 'unifiedAccount'/'portfolioMargin') - ändert sich
    praktisch nie waehrend eines Laufs, daher grosszuegiges TTL (5 Min.)
    statt bei jedem account_state()-Aufruf neu abzufragen."""
    result = _with_retry(info.query_user_abstraction_state, address)
    if isinstance(result, dict):
        return result.get("abstraction") or result.get("mode")
    return result


def max_leverage_for(symbol: str) -> int | None:
    universe, _ = _universe_and_ctxs()
    for asset in universe:
        if asset["name"] == symbol.upper():
            return int(asset["maxLeverage"])
    return None


@ttl_cache(300)
def candles_df(symbol: str, interval: str = "1h", lookback_hours: int = 400):
    """OHLCV-Kerzen direkt von der Börse, auf der auch gehandelt wird.

    Quelle für core/bot_signals.py. Bewusst Hyperliquid statt CoinGecko/Kraken
    (data/crypto.py): Der Bot handelt Perps auf DIESEM Orderbuch, also sollen
    Trendsignale und Stop-Abstände auch aus dessen Kursen kommen - sonst
    entstehen Signale auf einem Kurs, zu dem nie gefüllt wird. Ausserdem
    liefert nur diese Quelle Intraday-Kerzen mit High/Low, ohne die
    analysis.technical.add_indicators() kein ATR berechnen kann.

    Öffentliche, anmeldungsfreie API. TTL 300 s: der Runner-Takt sind 15 Min,
    ein 1h-Chart ändert sich dazwischen kaum - so kostet ein Scan über zehn
    Kandidaten nicht zehn Netzaufrufe pro Tick.

    Gibt None zurück (kein Wurf), wenn das Symbol unbekannt ist oder die API
    nichts liefert - ein einzelner fehlender Kandidat darf einen Scan nie
    abbrechen.
    """
    import pandas as pd

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(lookback_hours * 3600 * 1000)
    try:
        raw = _with_retry(_info_client().candles_snapshot, symbol.upper(), interval, start_ms, end_ms)
    except Exception:
        return None
    if not raw:
        return None
    rows = []
    for candle in raw:
        try:
            rows.append({
                "Time": pd.to_datetime(int(candle["t"]), unit="ms", utc=True),
                "Open": float(candle["o"]), "High": float(candle["h"]),
                "Low": float(candle["l"]), "Close": float(candle["c"]),
                "Volume": float(candle.get("v") or 0.0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows).set_index("Time").sort_index()
    # Die letzte Zeile ist die noch LAUFENDE Kerze - sie bleibt hier erhalten
    # (roher Datenlieferant), core.bot_signals verwirft sie bewusst, damit ein
    # Signal nicht davon abhängt, an welcher Minute der Stunde der Tick läuft.
    return df[~df.index.duplicated(keep="last")]


# Hyperliquid liefert je Antwort hoechstens ~5000 Kerzen. Fuer den Backtest
# (analysis/bot_backtest.py, 6-12 Monate Stundenkerzen = 4000-9000 Stueck)
# reicht ein einzelner Aufruf daher nicht immer aus.
_CANDLES_MAX_PER_REQUEST = 5000

_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
                "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

# Hyperliquid liefert je user_funding_history-Aufruf hoechstens ~500
# Eintraege (empirisch beobachtet: ein 400-Tage-Fenster eines lange
# laufenden Kontos lieferte genau 500 in der ersten Antwort, ein Eintrag
# fehlte ohne Fortsetzung). LiveExchange.funding_payments_usd() paginiert
# danach.
_FUNDING_HISTORY_PAGE_SIZE = 500


def _candles_to_rows(raw) -> list[dict]:
    """Rohantwort -> Zeilen. Defekte Einzelkerzen werden uebersprungen statt
    den ganzen Abruf scheitern zu lassen."""
    rows = []
    for candle in raw or []:
        try:
            rows.append({
                "Time": int(candle["t"]),
                "Open": float(candle["o"]), "High": float(candle["h"]),
                "Low": float(candle["l"]), "Close": float(candle["c"]),
                "Volume": float(candle.get("v") or 0.0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def candles_range(symbol: str, start_ms: int, end_ms: int, interval: str = "1h"):
    """Kerzen fuer einen BELIEBIG langen Zeitraum, stueckweise geholt.

    candles_df() deckt nur ein gleitendes Fenster ab (lookback_hours) und
    cached es 300 s - richtig fuer den 15-Minuten-Takt des Bots, aber
    unbrauchbar fuer einen Rücktest ueber Monate. Diese Funktion holt
    stattdessen in Stuecken zu `_CANDLES_MAX_PER_REQUEST` Kerzen und haengt
    sie aneinander. BEWUSST OHNE TTL-Cache: der Aufrufer (Backtest) laedt
    einmal und arbeitet dann offline weiter, ein Cache wuerde nur Speicher
    binden.

    Gibt None zurueck, wenn gar nichts geladen werden konnte - eine
    Teilantwort wird dagegen zurueckgegeben (lieber ein kuerzerer Rücktest
    als gar keiner, der Aufrufer sieht die Spanne am Index).
    """
    import pandas as pd

    step_ms = _INTERVAL_MS.get(interval)
    if step_ms is None:
        raise ValueError(f"Unbekanntes Intervall '{interval}'.")
    window_ms = step_ms * _CANDLES_MAX_PER_REQUEST

    rows: list[dict] = []
    cursor = int(start_ms)
    end_ms = int(end_ms)
    while cursor < end_ms:
        chunk_end = min(cursor + window_ms, end_ms)
        try:
            raw = _with_retry(_info_client().candles_snapshot, symbol.upper(), interval, cursor, chunk_end)
        except Exception:
            break
        chunk = _candles_to_rows(raw)
        if not chunk:
            # Keine Daten in diesem Fenster (Symbol jung, Boersen-Ausfall):
            # weiterspringen statt abbrechen, sonst endet der Rücktest an der
            # ersten Luecke.
            cursor = chunk_end
            continue
        rows.extend(chunk)
        newest = max(r["Time"] for r in chunk)
        # Immer ECHT weiterlaufen, sonst dreht sich die Schleife endlos, wenn
        # die Boerse denselben Block erneut liefert.
        cursor = max(newest + step_ms, cursor + step_ms)

    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["Time"] = pd.to_datetime(df["Time"], unit="ms", utc=True)
    df = df.set_index("Time").sort_index()
    return df[~df.index.duplicated(keep="last")]


def funding_history(symbol: str, start_ms: int, end_ms: int):
    """Historische STUNDEN-Fundingraten eines Symbols als Serie.

    Zwingend fuer einen ehrlichen Rücktest: Funding ist laut Planrecherche
    der groesste Kostenblock bei gehebelten Dauerpositionen, und
    funding_rate_hourly() liefert nur den AKTUELLEN Wert. Nicht zu
    verwechseln mit LiveExchange.funding_payments_usd(), das
    `user_funding_history` nutzt - das ist die eigene Kontohistorie und fuer
    ein nie gehaltenes Symbol wertlos.

    Gibt None zurueck, wenn nichts geladen werden konnte; der Backtest
    vermerkt das dann sichtbar im Ergebnis, statt still mit 0 zu rechnen.
    """
    import pandas as pd

    rows: list[dict] = []
    cursor = int(start_ms)
    end_ms = int(end_ms)
    guard = 0
    while cursor < end_ms and guard < 500:
        guard += 1
        try:
            raw = _with_retry(_info_client().funding_history, symbol.upper(), cursor, end_ms)
        except Exception:
            break
        if not isinstance(raw, list) or not raw:
            break
        newest = cursor
        for entry in raw:
            try:
                ts = int(entry["time"])
                rows.append({"Time": ts, "rate": float(entry["fundingRate"])})
                newest = max(newest, ts)
            except (KeyError, TypeError, ValueError):
                continue
        if newest <= cursor:
            break
        cursor = newest + _INTERVAL_MS["1h"]

    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["Time"] = pd.to_datetime(df["Time"], unit="ms", utc=True)
    series = df.set_index("Time")["rate"].sort_index()
    return series[~series.index.duplicated(keep="last")]


def _sz_decimals(symbol: str) -> int:
    universe, _ = _universe_and_ctxs()
    for asset in universe:
        if asset["name"] == symbol.upper():
            return int(asset["szDecimals"])
    raise HyperliquidError(f"{symbol}: kein Hyperliquid-Perp-Markt (nicht in supported_symbols()).")


def _round_size(symbol: str, size: float) -> float:
    """Immer ABrunden, nie aufrunden - eine Order darf durch Rundung nie
    größer werden als vom Aufrufer (und damit von core.bot_guards) geprüft."""
    decimals = _sz_decimals(symbol)
    factor = 10 ** decimals
    return math.floor(abs(size) * factor) / factor


def _round_price(symbol: str, price: float) -> float:
    """Preis im Hyperliquid-Perp-Format (max. 5 signifikante Stellen).

    Die SDK rundet aggressive Marktpreise intern auf dieses Format. Trigger-
    Orders gehen jedoch über ``order``/``bulk_orders`` direkt an die API und
    müssen daher hier ebenfalls normalisiert werden; andernfalls verwirft
    Hyperliquid z.B. einen BTC-Stop mit mehr als der zulässigen Präzision.
    """
    decimals = max(0, 6 - _sz_decimals(symbol))
    return round(float(f"{float(price):.5g}"), decimals)


# --- LIVE: echte, signierte Order ---

class LiveExchange:
    def __init__(self, base_url: str = MAINNET_URL):
        from eth_account import Account

        key, address = config.hyperliquid_credentials()
        if not key or not address:
            raise HyperliquidError(
                "HYPERLIQUID_AGENT_KEY / HYPERLIQUID_ACCOUNT_ADDRESS fehlen in der Umgebung."
            )
        self._address = address
        self._info = _HlInfo(base_url, skip_ws=True)
        wallet = Account.from_key(key)
        self._exchange = _HlExchange(wallet, base_url, account_address=address)
        self._leverage_set: dict[str, float] = {}  # vermeidet redundante update_leverage-Calls

    def mid_price(self, symbol: str) -> float | None:
        return mid_price(symbol)

    def funding_rate_hourly(self, symbol: str) -> float | None:
        return funding_rate_hourly(symbol)

    def frontend_open_orders(self) -> list[dict]:
        """Offene Orders mit Trigger-/TP-SL-Metadaten lesen.

        ``open_orders`` liefert nur die Buchbasis und kann eine Trigger-Order
        daher wie eine normale Limit-Order aussehen lassen. Für die
        Sicherheitsprüfung verwenden wir bewusst den Frontend-Endpunkt, der
        ``isTrigger``, ``orderType`` und ``triggerPx`` mitliefert.
        """
        reader = getattr(self._info, "frontend_open_orders", None)
        if reader is None:
            raise HyperliquidError("SDK unterstützt frontend_open_orders nicht.")
        raw = _with_retry(reader, self._address)
        return raw if isinstance(raw, list) else []

    @staticmethod
    def _flatten_frontend_orders(orders: list[dict]):
        """Eltern- und Kind-Orders des Frontend-Endpunkts durchsuchen."""
        for order in orders:
            if not isinstance(order, dict):
                continue
            yield order
            children = order.get("children")
            if isinstance(children, list):
                yield from LiveExchange._flatten_frontend_orders(children)

    def _stop_is_verified(self, stop_oid: str, symbol: str, stop_px: float) -> bool:
        """Nur eine echte, sichtbare Stop-Market-Trigger-Order akzeptieren."""
        try:
            orders = self.frontend_open_orders()
        except Exception:
            return False
        for order in self._flatten_frontend_orders(orders):
            if str(order.get("oid")) != str(stop_oid):
                continue
            order_type = str(order.get("orderType", "")).lower()
            try:
                trigger_px = float(order.get("triggerPx", 0) or 0)
            except (TypeError, ValueError):
                continue
            if (
                str(order.get("coin", "")).upper() == symbol.upper()
                and bool(order.get("isTrigger"))
                and bool(order.get("reduceOnly"))
                and "stop" in order_type
                and trigger_px > 0
                and abs(trigger_px - stop_px) <= max(0.02, stop_px * 0.0002)
            ):
                return True
        return False

    def _wait_for_verified_stop(self, stop_oid: str, symbol: str, stop_px: float) -> bool:
        for attempt in range(_STOP_VERIFY_ATTEMPTS):
            if self._stop_is_verified(stop_oid, symbol, stop_px):
                return True
            if attempt + 1 < _STOP_VERIFY_ATTEMPTS:
                time.sleep(_STOP_VERIFY_DELAY)
        return False

    def account_state(self) -> AccountState:
        raw = _with_retry(self._info.user_state, self._address)
        positions = []
        for entry in raw.get("assetPositions", []):
            p = entry.get("position", {})
            szi = float(p.get("szi", 0) or 0)
            if szi == 0:
                continue
            positions.append(Position(
                symbol=p["coin"], side="long" if szi > 0 else "short", size=abs(szi),
                entry_px=float(p.get("entryPx") or 0),
                leverage=float((p.get("leverage") or {}).get("value", 1)),
                unrealized_pnl_usd=float(p.get("unrealizedPnl", 0) or 0),
                liquidation_px=float(p["liquidationPx"]) if p.get("liquidationPx") else None,
            ))
        margin = raw.get("marginSummary", {})
        equity_usd = float(margin.get("accountValue", 0) or 0)
        withdrawable_usd = float(raw.get("withdrawable", 0) or 0)

        # Unified-/Portfolio-Margin-Konten führen ihr Guthaben in der Spot-/
        # Unified-Balance, nicht im klassischen Perps-Clearinghouse. DESSEN
        # `accountValue` ist in diesem Kontomodus NICHT die Gesamt-Equity,
        # sondern - beobachtet an einer echten offenen Position - offenbar nur
        # die für gerade offene Positionen hinterlegte Margin (z.B. 5.92 $
        # statt der tatsächlichen ~44.50 $ Gesamt-Equity, während exakt eine
        # kleine Live-Position offen war). Ein Filter "nur wenn accountValue
        # <= 0" hätte diesen Fall NICHT abgefangen (der Wert war klein, aber
        # positiv) - genau das hat einmal live die Equity um >85% zu niedrig
        # ausgewiesen und hätte beim nächsten Zyklus fälschlich den
        # Equity-Boden-Kill-Switch auslösen können. Deshalb wird der
        # Kontomodus jetzt IMMER geprüft (gecacht, siehe _abstraction_mode)
        # und bei Unified-/Portfolio-Margin bedingungslos die Unified-Rechnung
        # verwendet - nicht nur als Rückfall bei accountValue<=0.
        #
        # WICHTIG: `usdc.total` aus spotClearinghouseState ist bei diesem
        # Kontomodus bereits die vollständig bewertete Gesamt-Equity - das
        # offene Perp-PnL ist Hyperliquid-seitig (Cross-Margin über Spot und
        # Perps) darin schon enthalten. Ein zusätzliches Aufaddieren von
        # `p.unrealized_pnl_usd` zählte das offene PnL EIN ZWEITES MAL
        # (live nachgerechnet: Spot-USDC-Saldo 521,22 $ traf für sich allein
        # bereits Hyperliquids eigene Portfolio-Historie von ~521,11 $; mit
        # zusätzlicher PnL-Addition ergaben sich fälschlich 518,47 $). Bei
        # offenen Verlusten wies das die Equity zu NIEDRIG aus (verfrühter
        # Equity-Boden/Tagesverlust-Guard), bei offenen Gewinnen zu HOCH
        # (großzügigere Positionsgrößen/Heat-Budget als tatsächlich gedeckt).
        # `total` allein ist deshalb schon die korrekte Antwort.
        #
        # equity_trusted=False markiert die Faelle, in denen wir NICHT
        # verifizieren konnten, dass equity_usd die echte Gesamt-Equity ist -
        # frueher hiess es hier "die klassische Antwort bleibt die sichere
        # Basis", aber genau die war der oben beschriebene Fehlwert. Ein
        # unbekannter Kontomodus oder ein Unified-Konto ohne lesbaren
        # USDC-Eintrag laesst equity_usd auf dem (potenziell falschen)
        # accountValue stehen, aber core.bot.equity_reading() darf sich
        # darauf dann nicht fuer eine Zwangsschliessung verlassen.
        equity_trusted = True
        try:
            abstraction = _abstraction_mode(self._info, self._address)
            if abstraction in {"unifiedAccount", "portfolioMargin"}:
                spot = _with_retry(self._info.spot_user_state, self._address)
                usdc = next((b for b in spot.get("balances", [])
                             if str(b.get("coin", "")).upper() == "USDC"), None)
                if usdc is not None:
                    total = float(usdc.get("total", 0) or 0)
                    hold = float(usdc.get("hold", 0) or 0)
                    equity_usd = max(0.0, total)
                    withdrawable_usd = max(0.0, total - hold)
                else:
                    equity_trusted = False
        except Exception:
            equity_trusted = False
        return AccountState(
            equity_usd=equity_usd,
            withdrawable_usd=withdrawable_usd,
            positions=positions,
            equity_trusted=equity_trusted,
        )

    def _ensure_leverage(self, symbol: str, leverage: float) -> float:
        """Setzt den Hebel auf der Börse (falls nötig) und gibt den
        TATSÄCHLICH angewandten, ABGERUNDETEN Wert zurück - core.bot muss
        DIESEN in bot_positions.leverage speichern, nicht den angeforderten.
        Sonst zeigt die DB z.B. 1,9x, während die Börse tatsächlich mit 1x
        führt (Hyperliquid erlaubt keine Nachkommastellen)."""
        max_lev = max_leverage_for(symbol) or 1
        # ABRUNDEN, nicht runden: Hyperliquid verlangt einen GANZZAHLIGEN
        # Hebel. round(1.9) = 2 haette das auf der Boerse gesetzte Limit ueber
        # das konfigurierte hinausgehoben (core.bot_config._RISK_ANCHORS
        # leitet z.B. bei Risikostufe 9 max_leverage=1.9 ab) - ein
        # ABGERUNDETER Hebel ist dagegen immer INNERHALB der konfigurierten
        # Grenze, nie darueber.
        capped = max(1, min(int(leverage), max_lev))
        if self._leverage_set.get(symbol) == leverage:
            return capped
        self._exchange.update_leverage(capped, symbol, is_cross=True)
        self._leverage_set[symbol] = leverage
        return capped

    def _place_entry_and_stop(self, symbol: str, is_buy: bool, notional_usd: float,
                              leverage: float, stop_distance_pct: float) -> OrderResult:
        """`stop_distance_pct` statt eines fertigen `stop_px`: der Stop wird
        ERST NACH dem bestätigten Fill aus dem TATSÄCHLICHEN Einstandskurs
        berechnet, nicht aus dem Scan-Zeitpunkt-Kurs des Aufrufers. Zwischen
        dem Scan (core.bot_signals.evaluate, Grundlage für
        entry_signal.price) und diesem Fill liegen mindestens eine weitere
        Kontostand-Abfrage und die Ausführung selbst - bei einer volatilen
        Kryptoposition genug Zeit für eine Kursbewegung. Ein Stop, der auf
        dem VOR-Fill-Kurs beruht, sichert dann ein anderes Risiko ab als das,
        auf dem core.bot_signals.position_size_usd() die Positionsgröße
        bemessen hat. Der Rücktest (analysis.bot_backtest) hat das schon
        immer richtig gemacht (Stop aus dem simulierten Fill) - hier wird
        live dasselbe Prinzip nachgezogen."""
        symbol = symbol.upper()
        if symbol not in supported_symbols():
            return OrderResult(status="error", error=f"{symbol}: kein Hyperliquid-Perp-Markt.")
        price = mid_price(symbol)
        if not price:
            return OrderResult(status="error", error=f"{symbol}: kein Live-Preis verfügbar.")
        size = _round_size(symbol, notional_usd / price)
        if size <= 0:
            return OrderResult(status="error", error=f"{symbol}: Ordergröße rundet auf 0.")

        try:
            applied_leverage = self._ensure_leverage(symbol, leverage)
            resp = self._exchange.market_open(symbol, is_buy, size, slippage=_ENTRY_SLIPPAGE)
            entry = self._parse_order_response(resp)
        except Exception as e:
            return OrderResult(status="error", error=f"Order fehlgeschlagen: {e}")
        if entry.status != "filled":
            return entry

        # Stop-Loss SOFORT nach der Eröffnung - Pflicht, siehe core.bot_guards.
        # Gegenrichtung + reduce_only, damit der Stop nur schliesst, nie eine
        # neue/größere Position in die andere Richtung eröffnet.
        #
        # ERST HIER, aus dem BESTÄTIGTEN Fill berechnet - nicht aus einem vom
        # Aufrufer vorgegebenen Kurs. Das ist der Kern dieser Funktion (siehe
        # Docstring): entry.fill_px ist der Kurs, zu dem tatsächlich gehandelt
        # wurde, und genau darauf muss sich der Stop-Abstand beziehen, damit
        # er das Risiko absichert, das core.bot_signals.position_size_usd()
        # tatsächlich bemessen hat.
        stop_px = (entry.fill_px * (1 - stop_distance_pct / 100.0) if is_buy
                  else entry.fill_px * (1 + stop_distance_pct / 100.0))
        stop_px = _round_price(symbol, stop_px)

        stop_oid = None
        try:
            # Einzelner nativer Stop-Market-Trigger nach dem offiziellen SDK-
            # Muster. Die Preisnormalisierung oben ist Pflicht, weil die API
            # Triggerpreise nicht automatisch auf die BTC-Tickpräzision rundet.
            stop_resp = self._exchange.order(
                symbol, not is_buy, entry.filled_size, stop_px,
                order_type={"trigger": {"triggerPx": stop_px, "isMarket": True, "tpsl": "sl"}},
                reduce_only=True,
            )
            stop_oid = self._parse_stop_oid(stop_resp)
            if not stop_oid:
                # Eine Antwort ohne OID ist kein verifizierter Börsen-Stop.
                # Auch wenn die API dabei keinen Fehler liefert, darf der
                # Aufrufer die Position nicht als abgesichert weiterführen.
                raise HyperliquidError("Stop-Order bestätigt, aber ohne gültige Order-ID.")
            if not self._wait_for_verified_stop(stop_oid, symbol, stop_px):
                raise HyperliquidError(
                    "Stop-Order mit OID bestätigt, aber nicht als sichtbare Stop-Market-Trigger-Order verifiziert."
                )
        except Exception as e:
            # Position ist offen, aber OHNE Stop - darf nicht still verschluckt
            # werden. core/bot.py (Phase C) muss diesen Fall erkennen (error
            # gesetzt trotz status="filled") und sofort nachbessern oder die
            # Position notfalls direkt wieder schliessen.
            return OrderResult(status="filled", fill_px=entry.fill_px,
                               filled_size=entry.filled_size, fee_usd=entry.fee_usd,
                               stop_oid=stop_oid, leverage=applied_leverage, stop_px=stop_px,
                               error=f"EROEFFNET, aber Stop-Order fehlgeschlagen: {e}")
        return OrderResult(status="filled", fill_px=entry.fill_px, filled_size=entry.filled_size,
                           fee_usd=entry.fee_usd, stop_oid=stop_oid, leverage=applied_leverage,
                           stop_px=stop_px)

    def replace_stop_order(self, symbol: str, side: str, size: float, stop_px: float,
                           old_oid: str | None) -> tuple[str | None, str | None]:
        """Nachgezogenen Stop auf der Börse platzieren. Gibt (neue OID, Fehler).

        REIHENFOLGE IST SICHERHEITSRELEVANT: erst der neue Stop, dann die
        Stornierung des alten. Andersherum entstünde ein Zeitfenster ganz ohne
        Absicherung; so gibt es stattdessen kurz zwei reduce-only-Stops - der
        harmlosere Fehler, denn der zweite kann nach dem Schliessen nichts mehr
        auslösen.

        Kein Teil von ExchangeProtocol: PaperExchange hat keine echte
        Stop-Order (core/bot.py erkennt das per getattr, wie schon bei
        apply_funding und cancel_order).
        """
        symbol = symbol.upper()
        rounded = _round_price(symbol, stop_px)
        rounded_size = _round_size(symbol, size)
        if rounded_size <= 0:
            return None, f"{symbol}: Stop-Größe rundet auf 0."
        is_buy = side == "short"     # Gegenrichtung zur Position
        try:
            resp = self._exchange.order(
                symbol, is_buy, rounded_size, rounded,
                order_type={"trigger": {"triggerPx": rounded, "isMarket": True, "tpsl": "sl"}},
                reduce_only=True,
            )
            new_oid = self._parse_stop_oid(resp)
            if not new_oid:
                return None, f"{symbol}: nachgezogener Stop ohne gültige Order-ID."
            if not self._wait_for_verified_stop(new_oid, symbol, rounded):
                return None, f"{symbol}: nachgezogener Stop nicht als Trigger-Order verifiziert."
        except Exception as e:
            return None, f"{symbol}: nachgezogener Stop fehlgeschlagen: {e}"
        if old_oid:
            try:
                self.cancel_order(symbol, old_oid)
            except Exception:
                # Best effort: der neue Stop steht bereits, ein verwaister
                # alter ist unangenehm, aber kein Sicherheitsproblem - er
                # liegt weiter weg und ist reduce-only.
                pass
        return new_oid, None

    def open_long(self, symbol: str, notional_usd: float, leverage: float,
                 stop_distance_pct: float) -> OrderResult:
        return self._place_entry_and_stop(symbol, True, notional_usd, leverage,
                                          stop_distance_pct)

    def open_short(self, symbol: str, notional_usd: float, leverage: float,
                   stop_distance_pct: float) -> OrderResult:
        return self._place_entry_and_stop(symbol, False, notional_usd, leverage,
                                          stop_distance_pct)

    def close_position(self, symbol: str) -> OrderResult:
        symbol = symbol.upper()
        try:
            resp = self._exchange.market_close(symbol, slippage=_ENTRY_SLIPPAGE)
        except Exception as e:
            return OrderResult(status="error", error=f"Schliessen fehlgeschlagen: {e}")
        if resp is None:
            return OrderResult(status="error", error=f"{symbol}: keine offene Position zum Schliessen.")
        return self._parse_order_response(resp)

    def cancel_order(self, symbol: str, oid: str) -> bool:
        """Storniert eine offene Order (i.d.R. der Stop nach einem Close,
        siehe core.bot.close_position) - best effort, kein Teil von
        ExchangeProtocol: PaperExchange hat nichts zu stornieren, ihr
        'paper-stop-...' ist nur ein Platzhalter-String."""
        try:
            resp = self._exchange.cancel(symbol.upper(), int(oid))
            return isinstance(resp, dict) and resp.get("status") == "ok"
        except Exception:
            return False

    def recent_fills(self, since_ms: int) -> list[dict]:
        """Echte Fills (inkl. tatsächlich gezahlter Gebühr, Feld 'fee') seit
        einem Zeitpunkt (ms seit Epoch) - für core.bot.refresh_live_costs():
        OrderResult.fee_usd aus _parse_order_response() ist nur eine
        Schätzung (filled_size * fill_px * TAKER_FEE), keine Abrechnung.
        Antwortform gegen das echte, verknüpfte Konto verifiziert (Felder:
        coin, px, sz, side, dir, closedPnl, fee, feeToken, time, oid, tid)."""
        raw = _with_retry(self._info.user_fills, self._address)
        if not isinstance(raw, list):
            return []
        return [f for f in raw if int(f.get("time", 0) or 0) >= since_ms]

    def funding_payments_usd(self, since_ms: int) -> float:
        """Netto seit `since_ms` TATSÄCHLICH gezahltes/erhaltenes Funding, in
        core.bot's Vorzeichenkonvention (positiv = Kosten, negativ =
        Einnahme) - LiveExchange hat kein apply_funding() wie PaperExchange
        (dort wird Funding manuell nachgebildet), sondern liest es aus der
        echten Historie. Das rohe 'usdc'-Feld der API ist umgekehrt gepolt
        (negativ = Geld hat das Konto verlassen = aus Nutzersicht eine
        Kosten), daher die Negation unten.

        Paginiert wie candles_range()/funding_history(): `user_funding_history`
        liefert je Aufruf hoechstens `_FUNDING_HISTORY_PAGE_SIZE` Eintraege -
        bei einem lange laufenden Konto (grosses `since_ms`-Fenster) fehlte
        ohne Fortsetzung sonst ein Teil der Historie, ohne dass das sichtbar
        gewesen waere (beim Audit: ein einzelner Eintrag von 545 fehlte)."""
        total = 0.0
        cursor = int(since_ms)
        guard = 0
        while guard < 50:
            guard += 1
            raw = _with_retry(self._info.user_funding_history, self._address, startTime=cursor)
            if not isinstance(raw, list) or not raw:
                break
            newest = cursor
            for entry in raw:
                delta = entry.get("delta") or {}
                if delta.get("type") == "funding":
                    try:
                        total -= float(delta.get("usdc", 0) or 0)
                    except (TypeError, ValueError):
                        pass
                try:
                    newest = max(newest, int(entry.get("time", 0) or 0))
                except (TypeError, ValueError):
                    continue
            if len(raw) < _FUNDING_HISTORY_PAGE_SIZE or newest <= cursor:
                break
            cursor = newest + 1
        return total

    def deposit_withdrawal_usd(self, since_ms: int) -> float:
        """Netto seit `since_ms` ein-/ausgezahltes Kapital (nicht: Handels-PnL) -
        aus Hyperliquids Ledger-Endpunkt. Positiv = Kapital kam hinzu, negativ
        = Kapital wurde abgezogen.

        Gegen das echte, verknüpfte Konto verifiziert: Eine externe Ein-
        zahlung (USDC von einer anderen Adresse) erscheint dort NICHT als
        delta.type 'deposit' (das ist offenbar nur für den L1-Bridge-Pfad
        reserviert), sondern als delta.type 'send' mit einem 'destination'-
        Feld, das der EIGENEN Adresse entspricht - der Betrag steht im Feld
        'usdcValue', NICHT 'usdc' (das Feld, das funding_payments_usd()
        nutzt, existiert bei 'send' gar nicht). Eine Auszahlung ist
        symmetrisch: 'send' mit 'user' (Absender) == der eigenen Adresse.
        Die ursprünglich aus der SDK-Docstring angenommenen Typen
        'deposit'/'withdraw' werden zusätzlich unterstützt (falls ein
        L1-Bridge-Vorgang je auftaucht), mit Fallback über mehrere
        Betragsfeld-Namen, da deren genaue Form nie beobachtet wurde."""
        raw = _with_retry(self._info.user_non_funding_ledger_updates, self._address, startTime=since_ms)
        if not isinstance(raw, list):
            return 0.0
        my_address = self._address.lower()
        total = 0.0
        for entry in raw:
            delta = entry.get("delta") or {}
            dtype = delta.get("type")
            amount = None
            for field in ("usdcValue", "usdc", "amount"):
                raw_value = delta.get(field)
                if raw_value is None:
                    continue
                try:
                    amount = float(raw_value)
                    break
                except (TypeError, ValueError):
                    continue
            if amount is None:
                continue
            if dtype == "send":
                if str(delta.get("destination", "")).lower() == my_address:
                    total += amount
                elif str(delta.get("user", "")).lower() == my_address:
                    total -= amount
            elif dtype == "deposit":
                # Unsigned Betragsfeld angenommen (wie bei 'send'/usdcValue) -
                # die Richtung steckt im type-Namen, nicht im Vorzeichen.
                total += abs(amount)
            elif dtype == "withdraw":
                total -= abs(amount)
        return total

    @staticmethod
    def _parse_order_response(resp: dict) -> OrderResult:
        """Format laut Hyperliquid-API-Doku (POST /exchange, type 'order'):
        response.data.statuses[0] ist entweder {"filled": {totalSz, avgPx,
        oid}}, {"resting": {oid}} oder {"error": "..."}."""
        try:
            statuses = resp["response"]["data"]["statuses"]
        except (KeyError, TypeError):
            return OrderResult(status="error", error=f"Unerwartete Order-Antwort: {resp}")
        if not statuses:
            return OrderResult(status="error", error="Leere Order-Antwort.")
        s = statuses[0]
        if "filled" in s:
            f = s["filled"]
            fill_px = float(f["avgPx"])
            filled_size = float(f["totalSz"])
            return OrderResult(status="filled", fill_px=fill_px, filled_size=filled_size,
                               fee_usd=filled_size * fill_px * TAKER_FEE)
        if "error" in s:
            return OrderResult(status="error", error=str(s["error"]))
        if "resting" in s:
            # IOC-Market-Order, die nicht sofort voll gefuellt wurde - fuer
            # unsere kleinen Ordergroessen auf liquiden Perps ein Warnsignal,
            # kein Normalfall. Als Fehler behandeln, damit core/bot.py es
            # nicht stillschweigend als Erfolg verbucht.
            oid = s["resting"].get("oid")
            return OrderResult(status="error", error=f"Order resting statt gefuellt (oid={oid}).")
        return OrderResult(status="error", error=f"Unbekannter Order-Status: {s}")

    @staticmethod
    def _parse_stop_oid(resp: dict) -> str | None:
        try:
            s = resp["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError):
            return None
        for key in ("resting", "filled"):
            payload = s.get(key)
            if not isinstance(payload, dict):
                continue
            oid = payload.get("oid")
            if oid not in (None, ""):
                return str(oid)
        return None


# --- PAPER: simulierte Fills, sendet nie eine echte Order ---

class PaperExchange:
    """Verwaltet Positionen rein im Prozessspeicher (kein DB-Zugriff - das
    ist core/bot.py's Aufgabe, Phase C). Ein Nachkauf auf ein bereits offenes
    Symbol wird als Mengen-gewichteter Einstandskurs simuliert, ein Trade in
    Gegenrichtung wird abgelehnt (erst schliessen, dann neu eröffnen) - genau
    das Verhalten, das core.bot_guards.check_position_count für einen
    "Nachkauf auf bestehendes Symbol" voraussetzt.

    Fills laufen über slipped_price() und damit NICHT mehr exakt zum
    mid_price - siehe PAPER_SLIPPAGE_PCT.

    Der Zustand lebt weiterhin NUR im Prozessspeicher - aber `positions`/
    `starting_equity_usd` lassen sich beim Konstruieren SEEDEN, damit
    bot_runner.py nach einem Prozessneustart dort weitermacht, wo die letzte
    Sitzung stand, statt bei einem leeren Depot. Ohne das musste
    core.bot.reconcile_with_exchange() jede noch offene Position beim
    Neustart als "Phantom" künstlich schliessen (zum aktuellen Kurs, mit
    einem Ereignis, das nie stattgefunden hat) - technisch sicher (keine
    Positions-Leiche bleibt unauffindbar liegen), aber für mehrtägige
    Paper-Läufe und die daraus abgeleiteten Lernlabels (core.db.
    bot_signal_log) unbrauchbar, weil jeder Neustart echte offene Positionen
    vorzeitig und künstlich beendet hätte."""

    def __init__(self, starting_equity_usd: float, price_fn=mid_price,
                funding_fn=funding_rate_hourly, positions: dict[str, Position] | None = None):
        self._cash_usd = float(starting_equity_usd)
        self._positions: dict[str, Position] = dict(positions or {})
        self._price_fn = price_fn
        self._funding_fn = funding_fn

    def mid_price(self, symbol: str) -> float | None:
        return self._price_fn(symbol)

    def funding_rate_hourly(self, symbol: str) -> float | None:
        return self._funding_fn(symbol)

    def account_state(self) -> AccountState:
        positions = []
        unrealized_total = 0.0
        for symbol, pos in list(self._positions.items()):
            price = self._price_fn(symbol)
            if price is None:
                # Kein aktueller Kurs (z.B. API-Ausfall) - letzten bekannten
                # unrealisierten Stand weiterreichen statt abzustuerzen oder
                # ihn faelschlich auf 0 zu setzen.
                positions.append(pos)
                unrealized_total += pos.unrealized_pnl_usd
                continue
            direction = 1 if pos.side == "long" else -1
            upnl = (price - pos.entry_px) * pos.size * direction
            refreshed = Position(symbol=symbol, side=pos.side, size=pos.size,
                                 entry_px=pos.entry_px, leverage=pos.leverage,
                                 unrealized_pnl_usd=upnl, liquidation_px=pos.liquidation_px)
            self._positions[symbol] = refreshed
            positions.append(refreshed)
            unrealized_total += upnl
        return AccountState(equity_usd=self._cash_usd + unrealized_total,
                            withdrawable_usd=self._cash_usd, positions=positions)

    def _open(self, symbol: str, side: str, notional_usd: float, leverage: float,
             stop_distance_pct: float) -> OrderResult:
        """`stop_distance_pct`, nicht ein fertiger `stop_px` - derselbe Grund
        wie bei LiveExchange._place_entry_and_stop: der Stop wird aus dem
        TATSAECHLICHEN (verrutschten) Fill berechnet, nicht aus dem Kurs zum
        Scan-Zeitpunkt des Aufrufers. Der zurueckgegebene stop_px ist der
        Wert, den core.bot.open_position() in bot_positions.stop_px
        speichert."""
        symbol = symbol.upper()
        price = self._price_fn(symbol)
        if not price:
            return OrderResult(status="error", error=f"{symbol}: kein Preis verfügbar.")
        existing = self._positions.get(symbol)
        if existing is not None and existing.side != side:
            return OrderResult(status="error",
                               error=f"{symbol}: Gegenrichtung zur offenen Position "
                                     f"({existing.side}) - erst schliessen, dann neu eröffnen.")
        fill_px = slipped_price(price, side)
        stop_px = (fill_px * (1 - stop_distance_pct / 100.0) if side == "long"
                  else fill_px * (1 + stop_distance_pct / 100.0))
        size = notional_usd / fill_px
        fee = notional_usd * TAKER_FEE
        if fee > self._cash_usd:
            return OrderResult(status="error", error="Nicht genug Cash für die Gebühr.")
        self._cash_usd -= fee
        # Auch im Papierlauf ABRUNDEN: Hyperliquid akzeptiert live keinen
        # gebrochenen Hebel (LiveExchange._ensure_leverage). Wuerde Paper den
        # angeforderten Wert unveraendert uebernehmen, saehe ein Paper-Test
        # bei z.B. 1,9x einen Hebel, den der echte Livegang nie bekommen
        # wuerde - genau die Art Abweichung, die Paper vor dem Livegang
        # eigentlich aufdecken soll.
        applied_leverage = max(1, int(leverage))
        if existing is None:
            new_pos = Position(symbol=symbol, side=side, size=size, entry_px=fill_px,
                               leverage=applied_leverage, unrealized_pnl_usd=0.0)
        else:
            total_size = existing.size + size
            weighted_entry = (existing.entry_px * existing.size + fill_px * size) / total_size
            new_pos = Position(symbol=symbol, side=side, size=total_size, entry_px=weighted_entry,
                               leverage=applied_leverage, unrealized_pnl_usd=0.0)
        self._positions[symbol] = new_pos
        return OrderResult(status="filled", fill_px=fill_px, filled_size=size, fee_usd=fee,
                           stop_oid=f"paper-stop-{symbol}", leverage=applied_leverage,
                           stop_px=stop_px)

    def open_long(self, symbol: str, notional_usd: float, leverage: float,
                 stop_distance_pct: float) -> OrderResult:
        return self._open(symbol, "long", notional_usd, leverage, stop_distance_pct)

    def open_short(self, symbol: str, notional_usd: float, leverage: float,
                   stop_distance_pct: float) -> OrderResult:
        return self._open(symbol, "short", notional_usd, leverage, stop_distance_pct)

    def close_position(self, symbol: str) -> OrderResult:
        symbol = symbol.upper()
        pos = self._positions.get(symbol)
        if pos is None:
            return OrderResult(status="error", error=f"{symbol}: keine offene Position.")
        price = self._price_fn(symbol)
        if not price:
            return OrderResult(status="error", error=f"{symbol}: kein Preis verfügbar zum Schliessen.")
        direction = 1 if pos.side == "long" else -1
        fill_px = slipped_price(price, pos.side, closing=True)
        notional = fill_px * pos.size
        fee = notional * TAKER_FEE
        pnl = (fill_px - pos.entry_px) * pos.size * direction
        self._cash_usd += pnl - fee
        del self._positions[symbol]
        return OrderResult(status="filled", fill_px=fill_px, filled_size=pos.size, fee_usd=fee)

    def apply_funding(self, interval_hours: float = 0.25) -> float:
        """NUR PaperExchange (kein Teil von ExchangeProtocol - LiveExchange
        braucht das nicht, siehe unten): ein echtes Hyperliquid-Konto bekommt
        Funding automatisch stündlich ver-/berechnet, ein Papierdepot muss das
        selbst nachbilden - sonst wirkt Hebel in der Simulation günstiger, als
        er auf dem echten Konto wäre (Funding ist laut Planrecherche der mit
        Abstand größte Kostenblock bei gehebelten Dauerpositionen).

        Vorzeichen: positive Rate -> Longs zahlen, Shorts bekommen. Gibt die
        NETTO gezahlte Summe zurück (positiv = Kosten, negativ = Einnahme)."""
        total_cost = 0.0
        for symbol, pos in list(self._positions.items()):
            rate = self._funding_fn(symbol)
            price = self._price_fn(symbol)
            if rate is None or price is None:
                continue
            direction = 1 if pos.side == "long" else -1
            cost = rate * price * pos.size * direction * interval_hours
            self._cash_usd -= cost
            total_cost += cost
        return total_cost
