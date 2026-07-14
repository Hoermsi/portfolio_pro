"""Gemeinsame Bausteine für die Aktien- und Krypto-Portfolioseiten."""
import pandas as pd
import plotly.express as px
import streamlit as st

from analysis import alerts, performance
from core import db
from core.portfolio import total_value, valued_positions
from data import crypto as crypto_data
from data import stocks as stock_data
from ui import components

_GREEN = "color: #23c55e; font-weight: 600"
_RED = "color: #ff4b4b; font-weight: 600"


def _gv_style(v):
    """Grün/Rot für G/V-Zellen (pandas Styler)."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == 0:
        return ""
    return _GREEN if v > 0 else _RED


def _backfill_names(vals, asset_type: str) -> None:
    """Fehlende Anzeigenamen einmalig aus yfinance/CoinGecko holen und in die DB
    schreiben - ab dann kommen sie ohne Netz-Call aus der DB."""
    for v in vals:
        if v.position.name:
            continue
        symbol = v.position.symbol
        try:
            if symbol == "EUR":
                name = "Euro (Cash)"
            elif asset_type == "stock":
                name = stock_data.get_fundamentals(symbol).get("name")
            else:
                name = crypto_data.get_market_data(symbol).get("name")
        except Exception:
            name = None
        if name:
            db.set_asset_name(symbol, asset_type, name)
            v.position.name = name


def _display_name(pos, max_len: int = 20) -> str:
    """Kurzer Anzeigename fürs Pie-Chart: 'Name (SYM)', Fallback Symbol."""
    name = (pos.name or "").strip()
    if not name:
        return pos.symbol
    if len(name) > max_len:
        name = name[: max_len - 1] + "…"
    return f"{name} ({pos.symbol})"


def render_positions_table(asset_type: str) -> None:
    """Bewertete Positionstabelle + Verteilungs-Chart + Bearbeiten/Löschen."""
    with st.spinner("Lade Kurse ..."):
        all_vals = valued_positions(asset_type)
    if not all_vals:
        st.info("Noch keine Positionen erfasst.")
        return

    with st.spinner("Lade Wertpapier-Namen ..."):
        _backfill_names(all_vals, asset_type)

    # Tages-Snapshot des vollen Asset-Typs festhalten -> Verlauf wächst auch beim
    # Öffnen dieser Seite (idempotent pro Tag, ungefilterter Gesamtwert).
    db.save_snapshot(asset_type, total_value(all_vals))

    # --- Konto-/Kategorie-Filter (Einzelauswahl) ---
    categories = sorted({v.position.category for v in all_vals})
    vals = all_vals
    if len(categories) > 1:
        choice = st.radio("Ansicht", ["🌐 Alle Konten"] + categories,
                          horizontal=True, key=f"filter_{asset_type}",
                          label_visibility="collapsed")
        if choice != "🌐 Alle Konten":
            vals = [v for v in all_vals if v.position.category == choice]

    portfolio_value = total_value(vals)
    cost_total = sum(v.cost_basis for v in vals if v.has_cost)
    gain_total = portfolio_value - cost_total if cost_total else None
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Gesamtwert", f"{portfolio_value:,.2f} €")
    m2.metric("Positionen", len(vals))
    m3.metric("Einstand", f"{cost_total:,.2f} €" if cost_total else "—")
    m4.metric("G/V", f"{gain_total:+,.2f} €" if gain_total is not None else "—",
              f"{gain_total / cost_total * 100:+.1f} %" if cost_total and gain_total is not None else None)

    _render_category_overview(vals)

    query = st.text_input("Position filtern", placeholder="Symbol oder Name", key=f"search_{asset_type}")
    if query:
        needle = query.lower().strip()
        vals = [v for v in vals if needle in v.position.symbol.lower() or needle in (v.position.name or "").lower()]
    if not vals:
        st.info("Der Filter liefert keine Positionen.")
        return

    rows = []
    for v in vals:
        p = v.position
        if v.has_cost and v.gain_abs != 0:
            ampel = "🟢" if v.gain_abs > 0 else "🔴"
        else:
            ampel = "⚪"
        rows.append({
            "±": ampel,
            "Symbol": p.symbol,
            "Name": p.name or "",
            "Kategorie": p.category,
            "Menge": p.quantity,
            "Kurs (€)": round(v.price_eur, 4) if v.price_eur else None,
            "Wert (€)": round(v.value_eur, 2) if v.value_eur is not None else None,
            "Anteil (%)": round((v.value_eur or 0) / portfolio_value * 100, 1) if portfolio_value else 0,
            "Einstand (€)": round(v.cost_basis, 2) if v.has_cost else None,
            "G/V (€)": round(v.gain_abs, 2) if v.has_cost else None,
            "G/V (%)": round(v.gain_pct, 1) if v.has_cost else None,
            "Status": v.error or "OK",
        })
    df = pd.DataFrame(rows)
    styled = (df.style
              .map(_gv_style, subset=["G/V (€)", "G/V (%)"])
              .format({"Wert (€)": "{:,.2f}", "Einstand (€)": "{:,.2f}",
                       "G/V (€)": "{:+,.2f}", "G/V (%)": "{:+.1f}",
                       "Kurs (€)": "{:,.4f}", "Menge": "{:,.6g}"}, na_rep="—"))
    st.dataframe(styled, use_container_width=True, hide_index=True,
                 column_config={"Anteil (%)": st.column_config.ProgressColumn("Anteil", min_value=0, max_value=100,
                                                                                format="%.1f %%")})

    col_pie, col_hist = st.columns(2)
    with col_pie:
        items = [{"Wertpapier": _display_name(v.position), "Wert": v.value_eur}
                 for v in vals if v.value_eur]
        components.render_allocation_bars(
            [{"Name": item["Wertpapier"], "Wert": item["Wert"]} for item in items],
            "Größte Gewichtungen", key=f"allocation_positions_{asset_type}")
    with col_hist:
        _render_history_chart(asset_type)

    with st.expander("Position bearbeiten oder löschen"):
        options = {f"{v.position.symbol} · {v.position.category}": v.position for v in vals}
        sel = st.selectbox("Position wählen", list(options.keys()), key=f"edit_sel_{asset_type}")
        pos = options[sel]
        c1, c2, c3 = st.columns([1, 1, 1])
        qty = c1.number_input("Menge", value=float(pos.quantity), min_value=0.0,
                              format="%.8f", key=f"edit_qty_{asset_type}")
        buy = c2.number_input("Einstandskurs (€/Stück)", value=float(pos.buy_price_eur),
                              min_value=0.0, format="%.4f", key=f"edit_buy_{asset_type}")
        if c3.button("Änderungen speichern", key=f"edit_save_{asset_type}", use_container_width=True):
            db.save_position(pos.symbol, asset_type, qty, buy,
                             category=pos.category, source=pos.source)
            st.rerun()
        st.divider()
        confirm = st.checkbox("Ich möchte diese Position endgültig löschen.", key=f"edit_confirm_{asset_type}")
        if st.button("Position löschen", key=f"edit_del_{asset_type}", disabled=not confirm):
            db.delete_position(pos.id)
            st.rerun()


def _render_history_chart(asset_type: str) -> None:
    """Wertverlauf der Asset-Klasse mit wählbarem Zeitbereich.
    Krypto nutzt bevorzugt die aus der Kraken-Historie rekonstruierte Reihe."""
    st.markdown("**📈 Wertverlauf**")
    choice = st.radio("Zeitbereich", list(performance.PERIODS.keys()),
                      horizontal=True, index=3, key=f"histrange_{asset_type}",
                      label_visibility="collapsed")
    days = performance.PERIODS[choice]
    if asset_type == "crypto":
        df, quelle = performance.crypto_history_series(days)
    else:
        df, quelle = performance.snapshot_series(asset_type, days), "aufgezeichnet"

    if df is None or len(df) < 2:
        st.caption("Noch nicht genug Verlaufsdaten für diesen Zeitraum — der Verlauf "
                   "wächst mit jedem Öffnen der Seite.")
        return
    fig = px.line(df, x=df.index, y="Wert", markers=True,
                  labels={"Wert": "Wert (€)", "x": "Datum"})
    fig.update_traces(line_color="#23c55e")
    fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0),
                      xaxis_title=None)
    st.plotly_chart(fig, use_container_width=True, key=f"history_{asset_type}")
    if quelle == "rekonstruiert":
        st.caption("Echter Verlauf aus deiner Kraken-Historie (tatsächliche Mengen × "
                   "historische Kurse; nur Kraken-Bestände).")
    else:
        st.caption("Zeigt das gesamte Depot dieser Asset-Klasse (der Konto-Filter oben "
                   "wirkt auf Tabelle und Verteilung).")


def _render_category_overview(vals) -> None:
    """Übersicht je Konto/Kategorie (Summe, Anzahl, G/V) - aus bereits geladenen vals."""
    by_cat: dict[str, dict] = {}
    for v in vals:
        c = by_cat.setdefault(v.position.category, {"wert": 0.0, "einstand": 0.0,
                                                    "anzahl": 0, "hat_kosten": False})
        c["wert"] += v.value_eur or 0.0
        c["anzahl"] += 1
        if v.has_cost:
            c["einstand"] += v.cost_basis
            c["hat_kosten"] = True
    if len(by_cat) < 2:
        return  # nur eine Kategorie -> keine getrennte Übersicht nötig

    rows = []
    for cat, c in sorted(by_cat.items(), key=lambda kv: -kv[1]["wert"]):
        gv = c["wert"] - c["einstand"] if c["hat_kosten"] else None
        gv_pct = (gv / c["einstand"] * 100) if c["hat_kosten"] and c["einstand"] > 0 else None
        ampel = "⚪"
        if gv is not None and gv != 0:
            ampel = "🟢" if gv > 0 else "🔴"
        rows.append({
            "±": ampel,
            "Konto / Kategorie": cat,
            "Positionen": c["anzahl"],
            "Wert (€)": round(c["wert"], 2),
            "G/V (€)": round(gv, 2) if gv is not None else None,
            "G/V (%)": round(gv_pct, 1) if gv_pct is not None else None,
        })
    st.markdown("##### 🗂️ Konten-Übersicht")
    df = pd.DataFrame(rows)
    styled = (df.style
              .map(_gv_style, subset=["G/V (€)", "G/V (%)"])
              .format({"Wert (€)": "{:,.2f}", "G/V (€)": "{:+,.2f}",
                       "G/V (%)": "{:+.1f}"}, na_rep="—"))
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _resolve_watchlist_name(symbol: str, asset_type: str) -> str:
    """Anzeigenamen für ein neues Watchlist-Symbol best effort holen."""
    try:
        if asset_type == "crypto":
            return crypto_data.get_market_data(symbol).get("name") or ""
        return stock_data.get_fundamentals(symbol).get("name") or ""
    except Exception:
        return ""


def render_watchlist(asset_type: str) -> None:
    """Favoriten-Watchlist mit Kennzahlen und konfigurierbaren Kursalarmen.
    Gemeinsam für Aktien- und Krypto-Seite (asset_type steuert Kursquelle)."""
    st.markdown("### ⭐ Watchlist")
    st.caption("Favorisierte Werte beobachten und Kursalarme setzen – ausgelöste "
               "Alarme erscheinen auf dem Dashboard.")

    # --- Hinzufügen ---
    with st.form(f"watch_add_{asset_type}", clear_on_submit=True):
        c1, c2 = st.columns([3, 1])
        help_txt = ("Krypto-Symbol, z.B. BTC, ETH, SOL" if asset_type == "crypto"
                    else "yfinance-Ticker, z.B. NVDA, AAPL, SAP.DE")
        symbol = c1.text_input("Symbol", help=help_txt,
                               label_visibility="collapsed",
                               placeholder="Symbol zur Watchlist hinzufügen …").strip().upper()
        add = c2.form_submit_button("➕ Hinzufügen", use_container_width=True)
    if add and symbol:
        resolvable = (crypto_data.resolve_id(symbol) is not None if asset_type == "crypto"
                      else stock_data.resolves(symbol))
        if not resolvable:
            st.error(f"'{symbol}' konnte nicht aufgelöst werden – Symbol prüfen.")
        else:
            name = _resolve_watchlist_name(symbol, asset_type)
            db.add_watchlist(symbol, asset_type, name)
            st.rerun()

    entries = db.list_watchlist(asset_type)
    if not entries:
        st.info("Noch keine Favoriten. Füge oben ein Symbol hinzu.")
        return

    # --- Kennzahlen-Tabelle ---
    metrics_by_id: dict[int, dict] = {}
    rows = []
    with st.spinner("Lade Kurse …"):
        for e in entries:
            m = alerts.asset_metrics(e["symbol"], e["asset_type"])
            metrics_by_id[e["id"]] = m
            schwellen = []
            if e["target_above"] is not None:
                schwellen.append(f"↑{e['target_above']:,.2f}")
            if e["target_below"] is not None:
                schwellen.append(f"↓{e['target_below']:,.2f}")
            if e["day_move_pct"] is not None:
                schwellen.append(f"±{e['day_move_pct']:.1f}%")
            if e["rsi_alert"]:
                schwellen.append("RSI")
            rows.append({
                "Symbol": e["symbol"],
                "Name": e["name"] or "",
                "Kurs (€)": round(m["price_eur"], 4) if m["price_eur"] is not None else None,
                "Tag (%)": round(m["day_pct"], 2) if m["day_pct"] is not None else None,
                "RSI": round(m["rsi"], 0) if m["rsi"] is not None else None,
                "Alarme": " · ".join(schwellen) if schwellen else "—",
            })
    df = pd.DataFrame(rows)
    styled = (df.style
              .map(_gv_style, subset=["Tag (%)"])
              .format({"Kurs (€)": "{:,.4f}", "Tag (%)": "{:+.2f}", "RSI": "{:.0f}"}, na_rep="—"))
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # --- Alarme konfigurieren / entfernen ---
    with st.expander("Alarme bearbeiten oder Favorit entfernen"):
        options = {f"{e['symbol']} · {e['name'] or '—'}": e for e in entries}
        sel = st.selectbox("Favorit wählen", list(options.keys()), key=f"watch_sel_{asset_type}")
        e = options[sel]
        m = metrics_by_id.get(e["id"], {})
        if m.get("price_eur") is not None:
            st.caption(f"Aktueller Kurs: {m['price_eur']:,.2f} €")
        c1, c2 = st.columns(2)
        above = c1.number_input("Zielkurs oben (€, 0 = aus)", min_value=0.0,
                                value=float(e["target_above"] or 0.0), format="%.4f",
                                key=f"watch_above_{asset_type}")
        below = c2.number_input("Zielkurs unten (€, 0 = aus)", min_value=0.0,
                                value=float(e["target_below"] or 0.0), format="%.4f",
                                key=f"watch_below_{asset_type}")
        c3, c4 = st.columns(2)
        day_move = c3.number_input("Tages-Sprung Alarm (±%, 0 = aus)", min_value=0.0,
                                   value=float(e["day_move_pct"] or 0.0), step=0.5, format="%.1f",
                                   key=f"watch_move_{asset_type}")
        rsi_on = c4.checkbox("RSI-Alarm (>70 / <30)", value=bool(e["rsi_alert"]),
                             key=f"watch_rsi_{asset_type}")
        b1, b2 = st.columns(2)
        if b1.button("Alarme speichern", key=f"watch_save_{asset_type}", use_container_width=True):
            db.update_watchlist_alert(
                e["id"],
                above if above > 0 else None,
                below if below > 0 else None,
                day_move if day_move > 0 else None,
                rsi_on,
            )
            st.rerun()
        if b2.button("🗑️ Favorit entfernen", key=f"watch_del_{asset_type}", use_container_width=True):
            db.remove_watchlist(e["id"])
            st.rerun()


def render_add_form(asset_type: str, symbol_help: str) -> None:
    with st.expander("Neue Position hinzufügen"):
        cats = db.list_categories(asset_type) or ["Standard"]
        with st.form(f"add_{asset_type}", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            symbol = c1.text_input("Symbol", help=symbol_help).strip().upper()
            qty = c2.number_input("Menge", min_value=0.0, format="%.8f")
            buy = c3.number_input("Einstandskurs (€ / Stück)", min_value=0.0, format="%.4f")
            c4, c5 = st.columns(2)
            cat_sel = c4.selectbox("Konto", cats + ["+ Neues Konto"])
            new_cat = c5.text_input("Neues Konto (falls gewählt)").strip()
            if st.form_submit_button("Position hinzufügen", type="primary") and symbol:
                category = new_cat if cat_sel == "+ Neues Konto" and new_cat else cat_sel
                if category == "+ Neues Konto":
                    category = "Standard"
                db.save_position(symbol, asset_type, qty, buy, category=category)
                st.rerun()
