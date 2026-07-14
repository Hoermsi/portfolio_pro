"""Datenmodelle und Positionsbewertung."""
from dataclasses import dataclass, field


@dataclass
class Position:
    id: int
    symbol: str
    asset_type: str          # "stock" | "crypto"
    name: str
    currency: str
    quantity: float
    buy_price_eur: float
    category: str
    source: str              # "manuell" | "kraken" | "flatex"


@dataclass
class Valuation:
    """Bewertete Position in EUR."""
    position: Position
    price_native: float | None = None
    price_eur: float | None = None
    value_eur: float | None = None
    cost_basis: float = 0.0
    gain_abs: float = 0.0
    gain_pct: float = 0.0
    has_cost: bool = False
    error: str | None = None


def evaluate(position: Position, price_native: float | None, fx_to_eur: float | None) -> Valuation:
    """Bewertet eine Position. Einstandskurs ist in EUR (Flatex-/Kraken-Konvention)."""
    v = Valuation(position=position)
    if price_native is None:
        v.error = "Kein Kurs verfügbar"
        return v
    fx = fx_to_eur if fx_to_eur else 1.0
    v.price_native = price_native
    v.price_eur = price_native * fx
    v.value_eur = v.price_eur * position.quantity
    v.cost_basis = position.buy_price_eur * position.quantity
    v.has_cost = position.buy_price_eur > 0
    if v.has_cost and v.cost_basis > 0:
        v.gain_abs = v.value_eur - v.cost_basis
        v.gain_pct = v.gain_abs / v.cost_basis * 100
    return v


@dataclass
class AgentReport:
    """Ergebnis eines einzelnen Agenten."""
    agent: str
    score: int = 50
    urteil: str = ""
    zusammenfassung: str = ""
    punkte: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    error: str | None = None
