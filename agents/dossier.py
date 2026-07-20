"""Dossier-Aufbau: sammelt alle Daten zu einem Asset bzw. zum Portfolio,
die den Spezialisten-Agenten als Kontext dienen."""
import json

from analysis import risk as risk_analysis
from analysis import technical
from core import db
from core.portfolio import portfolio_summary, value_position
from core.profile import risk_profile, target_allocation
from data import crypto as crypto_data
from data import news as news_data
from data import stocks as stock_data


def build_asset_dossier(symbol: str, asset_type: str) -> dict | None:
    """Alle Fakten zu einem Einzelwert. None, wenn keine Kursdaten existieren."""
    symbol = symbol.strip().upper()
    if asset_type == "crypto":
        df = crypto_data.get_history(symbol, days=365)
        fundamentals = crypto_data.get_market_data(symbol)
        currency = "EUR"
    else:
        df = stock_data.get_history(symbol, "1y")
        fundamentals = stock_data.get_fundamentals(symbol)
        currency = stock_data.get_currency(symbol)
    if df is None or df.empty:
        return None

    tech = technical.summarize(df)
    risk = risk_analysis.asset_risk(df)
    news = news_data.get_news(symbol, asset_type)

    # Positions-Kontext, falls das Asset im Portfolio liegt
    position_ctx = None
    positions = [p for p in db.list_positions(asset_type) if p.symbol == symbol]
    if positions:
        total_qty = sum(p.quantity for p in positions)
        vals = [value_position(p) for p in positions]
        value = sum(v.value_eur or 0 for v in vals)
        summary = portfolio_summary()
        share = value / summary["gesamt_eur"] * 100 if summary["gesamt_eur"] > 0 else 0
        gains = [v for v in vals if v.has_cost]
        position_ctx = {
            "menge": total_qty,
            "wert_eur": round(value, 2),
            "anteil_am_portfolio_pct": round(share, 1),
            "gv_eur": round(sum(v.gain_abs for v in gains), 2) if gains else None,
        }

    return {
        "symbol": symbol,
        "asset_type": asset_type,
        "name": fundamentals.get("name") or symbol,
        "currency": currency,
        "technik_text": technical.summary_text(tech),
        "tech": tech,
        "fundamentals": {k: v for k, v in fundamentals.items() if v is not None},
        "risiko": risk,
        "news": news,
        "position": position_ctx,
    }


def risk_profile_prompt() -> str:
    """Prompt-Block mit dem Risikoprofil des Nutzers (für Stratege + Review)."""
    p = risk_profile()
    lines = [f"RISIKOPROFIL DES NUTZERS: Risikobereitschaft {int(p['risk'])}/10, "
             f"Zielrendite {p['target_return_pct']:.1f}% p.a."]
    if p["retirement_year"]:
        lines.append(f"Geplanter Pensionsantritt: {p['retirement_year']}.")
    lines.append("Richte Tonalität und Aggressivität deiner Einschätzung daran aus. "
                 "Dies bleibt eine Experiment-/Informationsfunktion, keine Anlageberatung.")
    return "\n".join(lines)


def cash_allocation_prompt() -> str:
    """Prompt-Block zu Cash und Zielallokation - genutzt von Review und Stratege.

    Nennt Bankguthaben, Ist- vs. Ziel-Allokation, die noetige EUR-Anpassung je
    Anlageklasse und das FREI investierbare Cash (Cash ueber der Ziel-Cash-Quote,
    die als Reserve stehen bleibt). Fordert konkrete Vorschlaege mit EUR-Betrag +
    Symbol, die die Zielallokation annaehern.
    """
    s = portfolio_summary()
    targets = target_allocation()
    total = s["gesamt_eur"]
    cash = s.get("cash_eur", 0.0)
    alloc = s.get("allokation") or {}

    lines = [f"BANKGUTHABEN (Cash): {cash:,.2f} EUR",
             f"Gesamtvermoegen: {total:,.2f} EUR"]
    if total > 0:
        ist = {"stock": alloc.get("aktien_pct", 0.0),
               "crypto": alloc.get("krypto_pct", 0.0),
               "cash": alloc.get("cash_pct", 0.0)}
        namen = {"stock": "Aktien", "crypto": "Krypto", "cash": "Cash"}
        lines.append("Ist- vs. Ziel-Allokation (+ = zukaufen, - = reduzieren):")
        for key in ("stock", "crypto", "cash"):
            ziel = targets[key]
            delta_eur = (ziel - ist[key]) / 100 * total
            lines.append(f"- {namen[key]}: ist {ist[key]:.1f}% / ziel {ziel:.1f}% "
                         f"-> {delta_eur:+,.0f} EUR")
        ziel_cash = targets["cash"] / 100 * total
        frei = max(0.0, cash - ziel_cash)
        lines.append(f"FREI INVESTIERBARES CASH: {frei:,.2f} EUR "
                     f"(Bankguthaben minus Ziel-Cash-Reserve von {ziel_cash:,.0f} EUR).")
        if frei > 0:
            lines.append("Wenn frei investierbares Cash vorhanden ist, mache KONKRETE "
                         "Vorschlaege mit EUR-Betrag und Symbol (z.B. '500 EUR in einen "
                         "Welt-ETF wie IWDA'), die die Ist-Allokation in Richtung Ziel "
                         "bewegen. Neue Instrumente (Aktien/ETFs) sind erlaubt, nicht nur "
                         "Bestandspositionen. Halte die Ziel-Cash-Reserve zurueck.")
        else:
            lines.append("Kein Cash ueber der Ziel-Reserve frei - empfiehl keine "
                         "cash-finanzierten Zukaeufe, sondern hoechstens Umschichtungen.")
    return "\n".join(lines)


def dossier_prompt(d: dict) -> str:
    """Basis-Prompt-Block, den alle Spezialisten erhalten."""
    lines = [
        f"Asset: {d['name']} ({d['symbol']}, {'Kryptowährung' if d['asset_type'] == 'crypto' else 'Aktie'})",
        f"Kurswährung: {d['currency']}",
        "",
        "TECHNIK:",
        d["technik_text"],
    ]
    if d["fundamentals"]:
        lines += ["", "FUNDAMENTAL / MARKTDATEN:",
                  json.dumps(d["fundamentals"], ensure_ascii=False, indent=1)]
    if d["risiko"]:
        lines += ["", "RISIKO-KENNZAHLEN (1 Jahr):",
                  json.dumps(d["risiko"], ensure_ascii=False)]
    if d["position"]:
        lines += ["", "POSITION IM PORTFOLIO:",
                  json.dumps(d["position"], ensure_ascii=False)]
    if d["news"]:
        lines += ["", "AKTUELLE SCHLAGZEILEN:"]
        lines += [f"- {n['title']} ({n['source']})" for n in d["news"]]
    lines += ["", risk_profile_prompt()]
    return "\n".join(lines)


# Review-Bereich: welcher Teil des Portfolios analysiert wird
SCOPES = {
    "all": {"keys": ("aktien", "krypto"), "name": "Gesamtportfolio"},
    "stock": {"keys": ("aktien",), "name": "Aktien-Portfolio"},
    "crypto": {"keys": ("krypto",), "name": "Krypto-Portfolio"},
}


def build_portfolio_dossier(scope: str = "all") -> dict:
    """Portfolio-Fakten für das Review - wahlweise gesamt, nur Aktien oder nur Krypto."""
    cfg = SCOPES[scope]
    summary = portfolio_summary()

    # Konzentration innerhalb des gewählten Bereichs
    values = {}
    for key in cfg["keys"]:
        for item in summary[key]:
            if item["wert_eur"] > 0:
                values[item["symbol"]] = values.get(item["symbol"], 0) + item["wert_eur"]
    conc = risk_analysis.concentration(values)

    # Korrelation der größten Positionen (max 8, um API-Limits zu schonen)
    top = sorted(values.items(), key=lambda kv: -kv[1])[:8]
    histories = {}
    crypto_symbols = {i["symbol"] for i in summary["krypto"]}
    for symbol, _ in top:
        if symbol in crypto_symbols:
            histories[symbol] = crypto_data.get_history(symbol, days=365)
        else:
            histories[symbol] = stock_data.get_history(symbol, "1y")
    corr = risk_analysis.correlation_matrix(histories)

    return {
        "scope": scope,
        "scope_name": cfg["name"],
        "scope_value_eur": round(sum(values.values()), 2),
        "summary": summary,
        "konzentration": conc,
        "korrelation": corr.to_dict() if corr is not None else None,
    }


def portfolio_prompt(d: dict) -> str:
    s = d["summary"]
    scope = d.get("scope", "all")
    lines = []
    if scope == "all":
        lines += [
            f"GESAMTPORTFOLIO (inkl. Cash): {s['gesamt_eur']:,.2f} EUR",
            f"- Aktien: {s['aktien_summe_eur']:,.2f} EUR",
            f"- Krypto: {s['krypto_summe_eur']:,.2f} EUR",
            f"- Cash (Bankguthaben): {s.get('cash_eur', 0.0):,.2f} EUR",
        ]
        if s.get("allokation"):
            alloc = s["allokation"]
            lines.append(f"- Allokation: {alloc['aktien_pct']}% Aktien / "
                         f"{alloc['krypto_pct']}% Krypto / "
                         f"{alloc.get('cash_pct', 0.0)}% Cash")
        lines += ["", "AKTIEN-POSITIONEN:", json.dumps(s["aktien"], ensure_ascii=False, indent=1),
                  "", "KRYPTO-POSITIONEN:", json.dumps(s["krypto"], ensure_ascii=False, indent=1)]
    else:
        key = "aktien" if scope == "stock" else "krypto"
        lines += [
            f"{d['scope_name'].upper()}: {d['scope_value_eur']:,.2f} EUR",
            f"(Zur Einordnung: Gesamtvermögen {s['gesamt_eur']:,.2f} EUR - "
            f"analysiert wird hier NUR der {'Aktien' if scope == 'stock' else 'Krypto'}-Teil.)",
            "", "POSITIONEN:", json.dumps(s[key], ensure_ascii=False, indent=1),
        ]
    if d["konzentration"]:
        lines += ["", "KONZENTRATION:", json.dumps(d["konzentration"], ensure_ascii=False)]
    if d["korrelation"]:
        lines += ["", "KORRELATION DER TAGESRENDITEN (Top-Positionen):",
                  json.dumps(d["korrelation"], ensure_ascii=False)]
    lines += ["", "CASH & ZIELALLOKATION:", cash_allocation_prompt()]
    lines += ["", risk_profile_prompt()]
    return "\n".join(lines)
