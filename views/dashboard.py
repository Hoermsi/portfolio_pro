"""Zentrale Übersicht: Vermögen, Allokation, Performance und Handlungsbedarf."""
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from analysis import alerts, performance
from core.portfolio import total_value, valued_positions
from core.profile import risk_profile
from ui import components
from views.settings import target_allocation

# Dashboard-Benchmarks: alle über yfinance (auch BTC-EUR), damit lange
# Historie verfügbar ist - CoinGecko free liefert nur 365 Tage.
# Hebel-ETFs: CL2.PA = Amundi MSCI USA Daily 2x (FR0010755611, seit 2009),
# LVWC.DE = Amundi MSCI World 2x (FR0014010HV4, Xetra, erst seit Okt 2025).
_BENCHMARKS = {
    "S&P 500 (^GSPC)": "^GSPC",
    "MSCI World (URTH)": "URTH",
    "Amundi MSCI USA 2x (CL2)": "CL2.PA",
    "Amundi MSCI World 2x (LVWC)": "LVWC.DE",
    "Bitcoin (BTC-EUR)": "BTC-EUR",
}
# Zeitbereich für den Benchmark-Kontext im Vergleich (Tage zurück ab heute).
# Der Vergleich (0 %) ankert immer am ersten Portfolio-Datenpunkt; dieser Regler
# steuert nur, wie weit der Benchmark zusätzlich als Kontext nach links reicht.
_COMPARE_RANGES = {"3 Monate": 90, "6 Monate": 180, "1 Jahr": 365,
                   "3 Jahre": 1095, "5 Jahre": 1825, "10 Jahre": 3650}
_COMPARE_DEFAULT = "1 Jahr"
_C_BENCH = "#60a5fa"     # Benchmark-Linie
_C_PORT = "#a78bfa"      # Portfolio-Overlay
_C_ACTUAL = "#34d399"    # historischer Ist-Wert (Projektion)
_C_PROJ = "#8894a3"      # Projektionslinie (gestrichelt, neutral)


def _previous_total(history: pd.DataFrame | None) -> float | None:
    if history is None or len(history) < 2:
        return None
    return float(history["Gesamt"].iloc[-2])


def _allocation_rows(stock_total: float, crypto_total: float, cash: float) -> list[dict]:
    return [
        {"Name": "Aktien", "Wert": stock_total, "key": "stock"},
        {"Name": "Krypto", "Wert": crypto_total, "key": "crypto"},
        {"Name": "Cash", "Wert": cash, "key": "cash"},
    ]


def _render_rebalancing(rows: list[dict], total: float):
    targets = target_allocation()
    table = []
    hints = []
    for row in rows:
        current = row["Wert"] / total * 100 if total else 0.0
        target = targets[row["key"]]
        deviation = current - target
        # Positiv = aufbauen/kaufen, negativ = reduzieren/verkaufen (Ziel minus Ist).
        adjust_eur = (target - current) / 100 * total if total else 0.0
        table.append({"Anlageklasse": row["Name"], "Ist": round(current, 1),
                      "Ziel": round(target, 1), "Abweichung": round(deviation, 1),
                      "Anpassung (€)": round(adjust_eur, 0)})
        if abs(deviation) >= 5:
            direction = "reduzieren" if deviation > 0 else "aufbauen"
            hints.append(f"{row['Name']} {direction} ({deviation:+.1f} %-Pkt. / {adjust_eur:+,.0f} €)")
    st.markdown("#### Zielallokation")
    st.dataframe(
        pd.DataFrame(table), hide_index=True, width="stretch",
        column_config={
            "Ist": st.column_config.ProgressColumn("Ist (%)", min_value=0, max_value=100, format="%.1f %%"),
            "Ziel": st.column_config.NumberColumn("Ziel (%)", format="%.1f %%"),
            "Abweichung": st.column_config.NumberColumn("Abweichung", format="%+.1f %%"),
            "Anpassung (€)": st.column_config.NumberColumn("Anpassung (€)", format="%+.0f €"),
        },
    )
    st.caption("Anpassung (€): + = zukaufen, − = reduzieren, um die Zielallokation zu erreichen.")
    if hints:
        st.warning(" · ".join(hints))
    else:
        st.success("Deine Allokation liegt innerhalb von ±5 Prozentpunkten der Ziele.")


def _render_performance(history: pd.DataFrame | None):
    st.markdown("#### Entwicklung")
    if history is None or history.empty:
        st.caption("Noch keine Verlaufsdaten. Beim Öffnen des Dashboards wird täglich ein Snapshot gespeichert.")
        return
    if len(history) < 2:
        st.caption("Der Verlauf beginnt gerade erst - jeder Tag fügt einen weiteren Datenpunkt hinzu. "
                   "Projektion und Vergleich kannst du trotzdem schon nutzen.")
    modus = st.radio("Modus", ["Projektion", "Vergleich"], horizontal=True,
                     label_visibility="collapsed", key="dashboard_mode")
    if modus == "Projektion":
        _render_projection(history)
    else:
        _render_comparison(history)


def _render_projection(history: pd.DataFrame):
    """Gesamtwert historisch + Zinseszins-Fortschreibung in die Zukunft."""
    sp_cagr = performance.sp500_cagr()
    c1, c2 = st.columns([1, 1])
    use_sp = c1.checkbox("Ø-Rendite des S&P 500 verwenden", value=True, key="dashboard_proj_sp",
                         help="An: historische Ø-Rendite des S&P 500. Aus: eigene Renditeannahme per Regler.")

    if use_sp and sp_cagr is not None:
        annual, source = sp_cagr, "Ø S&P 500"
    else:
        if use_sp and sp_cagr is None:
            st.caption("S&P-500-Historie momentan nicht verfügbar - bitte eigene Rendite wählen.")
        default_pct = min(30.0, max(-5.0, round((sp_cagr if sp_cagr is not None else 0.07) * 100, 1)))
        annual = c2.slider("Erwartete Rendite p.a. (%)", -5.0, 30.0, default_pct, step=0.5,
                           key="dashboard_proj_return") / 100.0
        source = "eigene Annahme"

    # Horizont-Regler; das Pensionsjahr aus den Einstellungen liefert nur den Startwert.
    profile = risk_profile()
    default_h = 30
    if profile["retirement_year"]:
        default_h = min(40, max(1, int(profile["retirement_year"]) - date.today().year))
    horizon = st.slider("Horizont (Jahre)", 1, 40, default_h, key="dashboard_proj_years")

    monthly = float(profile["monthly_contribution"])
    port = history["Gesamt"].dropna()
    if port.empty or float(port.iloc[-1]) <= 0:
        st.caption("Sobald dein Portfolio einen Wert > 0 hat, erscheint hier die Projektion.")
        return
    fig = go.Figure()
    fig.add_scatter(x=port.index, y=port, name="Gesamtwert", mode="lines",
                    line=dict(color=_C_ACTUAL))
    proj = performance.projection_series(float(port.iloc[-1]), port.index[-1], annual,
                                         horizon_years=horizon, monthly_contribution=monthly)
    if proj is not None:
        fig.add_scatter(x=proj.index, y=proj, name="Projektion", mode="lines",
                        line=dict(color=_C_PROJ, dash="dash"))
        start_value = float(port.iloc[-1])
        n_months = len(proj) - 1
        endwert = float(proj.iloc[-1])
        eingezahlt = start_value + monthly * n_months
        wertzuwachs = endwert - eingezahlt
        spar_txt = f" + {monthly:,.0f} €/Monat" if monthly > 0 else ""
        st.caption(f"Projektion mit {annual * 100:.1f} % p.a. ({source}){spar_txt} auf {horizon} Jahre "
                   f"→ ~{endwert:,.0f} € bis {proj.index[-1].year}. "
                   f"Davon ~{eingezahlt:,.0f} € Startwert + Einzahlungen, "
                   f"~{wertzuwachs:,.0f} € Wertzuwachs. Keine Prognosegarantie.")
    fig.update_layout(height=360, margin=dict(l=0, r=0, t=10, b=0),
                      legend=dict(orientation="h", y=1.08), xaxis_title=None,
                      yaxis_title="Wert (€)")
    st.plotly_chart(fig, width="stretch", config={"responsive": True},
                    key="dashboard_proj_chart")


def _render_comparison(history: pd.DataFrame):
    """Benchmark und Portfolio-Overlay prozentual ab dem ersten Portfolio-Datenpunkt."""
    c1, c2, c3 = st.columns(3)
    benchmark = c1.selectbox("Benchmark", list(_BENCHMARKS), key="dashboard_cmp_benchmark")
    overlay = c2.selectbox("Portfolio-Overlay", ["Gesamt", "Aktien", "Krypto"],
                           key="dashboard_cmp_overlay")
    zeitraum = c3.selectbox("Zeitbereich (Benchmark)", list(_COMPARE_RANGES),
                            index=list(_COMPARE_RANGES).index(_COMPARE_DEFAULT),
                            key="dashboard_cmp_range",
                            help="Wie weit der Benchmark zusätzlich als Kontext in die "
                                 "Vergangenheit gezeigt wird. Der Vergleich (0 %) startet immer "
                                 "an deinem ersten Portfolio-Datenpunkt.")

    port = history[overlay].dropna()
    if overlay != "Gesamt":
        port = port[port > 0]

    # Portfolio-Anker nur, wenn ein Wert > 0 vorliegt; sonst Benchmark solo.
    anchor = None
    port_pct = None
    if not port.empty and float(port.iloc[-1]) > 0:
        anchor = port.index[0]
        port_pct = performance.pct_from_anchor(port, anchor)

    # Benchmark-Zeitfenster: gewählter Kontext, mit Anker mindestens bis dorthin
    # zurück (damit dort rebasiert werden kann), ohne Anker der gewählte Zeitbereich.
    if anchor is not None:
        since_anchor = (date.today() - anchor.date()).days + 1
        days = max(since_anchor, _COMPARE_RANGES[zeitraum])
    else:
        days = _COMPARE_RANGES[zeitraum]
    bench = performance.benchmark_series(_BENCHMARKS[benchmark], days=days)

    fig = go.Figure()
    fig.add_hline(y=0, line_color="#26344d", line_width=1)
    if bench is not None:
        # Ohne Portfolio-Anker den Benchmark auf seinen eigenen Fensterstart rebasen.
        bench_anchor = anchor if anchor is not None else bench.index[0]
        bench_pct = performance.pct_from_anchor(bench, bench_anchor)
        if bench_pct is not None:
            fig.add_scatter(x=bench_pct.index, y=bench_pct, name=benchmark, mode="lines",
                            line=dict(color=_C_BENCH))
    elif port_pct is not None:
        st.caption(f"{benchmark} ist momentan nicht verfügbar - nur dein Portfolio wird gezeigt.")
    else:
        st.caption(f"{benchmark} ist momentan nicht verfügbar.")
    if port_pct is not None:
        fig.add_scatter(x=port_pct.index, y=port_pct, name=f"Portfolio: {overlay}",
                        mode="lines+markers", line=dict(color=_C_PORT))
        if len(port_pct) < 2:
            st.caption("Dein Portfolio hat erst einen Datenpunkt - die Vergleichslinie wächst mit jedem Tag.")
    else:
        st.caption("Sobald dein Portfolio einen Wert > 0 hat, erscheint hier deine Vergleichslinie.")
    fig.update_layout(height=360, margin=dict(l=0, r=0, t=10, b=0),
                      legend=dict(orientation="h", y=1.08), xaxis_title=None,
                      yaxis_title="Veränderung seit Portfolio-Start (%)")
    st.plotly_chart(fig, width="stretch", config={"responsive": True},
                    key=f"dashboard_cmp_{benchmark}_{zeitraum}")


def _render_top_positions(vals: list, total: float):
    st.markdown("#### Größte Positionen")
    valued = [v for v in vals if v.value_eur is not None]
    if not valued:
        st.caption("Noch keine bewertbaren Positionen.")
        return
    rows = []
    for v in sorted(valued, key=lambda x: x.value_eur or 0, reverse=True)[:5]:
        rows.append({"Position": v.position.name or v.position.symbol,
                     "Symbol": v.position.symbol,
                     "Wert": round(v.value_eur or 0, 2),
                     "Anteil": round((v.value_eur or 0) / total * 100, 1) if total else 0,
                     "G/V": round(v.gain_pct, 1) if v.has_cost else None})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={
                     "Wert": st.column_config.NumberColumn("Wert (€)", format="%.2f €"),
                     "Anteil": st.column_config.ProgressColumn("Anteil", min_value=0, max_value=100, format="%.1f %%"),
                     "G/V": st.column_config.NumberColumn("G/V", format="%+.1f %%"),
                 })


def _render_attention(vals: list, total: float, rows: list[dict]):
    issues = []
    missing = [v.position.symbol for v in vals if v.error]
    if missing:
        issues.append(f"Kurs fehlt: {', '.join(missing)}")
    largest = max((v for v in vals if v.value_eur is not None), key=lambda x: x.value_eur or 0, default=None)
    if largest and total and (largest.value_eur or 0) / total >= 0.25:
        issues.append(f"Konzentration: {largest.position.symbol} macht {(largest.value_eur or 0) / total * 100:.1f} % des Vermögens aus")
    targets = target_allocation()
    for row in rows:
        current = row["Wert"] / total * 100 if total else 0
        if abs(current - targets[row["key"]]) >= 10:
            issues.append(f"Allokation: {row['Name']} weicht deutlich vom Ziel ab")
    st.markdown("#### Im Blick behalten")
    if issues:
        for issue in issues[:3]:
            st.info(issue)
    else:
        st.success("Keine auffälligen Konzentrationen oder Datenlücken erkannt.")


def _render_alerts():
    """Ausgelöste, noch nicht quittierte Kursalarme der Watchlist-Favoriten.
    'Gesehen' unterdrückt einen Alarm, bis Art/Schwelle geändert werden."""
    triggered = alerts.evaluate_watchlist()
    if not triggered:
        return
    st.divider()
    st.markdown("#### 🔔 Alarme")
    for item in triggered:
        label = item["name"] or item["symbol"]
        typ = "Krypto" if item["asset_type"] == "crypto" else "Aktie"
        for t in item["triggers"]:
            c1, c2 = st.columns([5, 1], vertical_alignment="center")
            c1.warning(f"**{label}** ({item['symbol']}, {typ}): {t['text']}")
            c2.button("✓ Gesehen", key=f"ack_{item['id']}_{t['kind']}_{t['threshold']}",
                      on_click=alerts.acknowledge, args=(item["id"], [t]),
                      help="Diesen Alarm ausblenden, bis sich die Schwelle ändert.")
    st.divider()


def render():
    components.page_header("Übersicht", "Dein Portfolio", "Vermögen, Entwicklung und die wichtigsten Signale auf einen Blick.")
    with st.spinner("Aktualisiere Kurse …"):
        stock_vals = valued_positions("stock")
        crypto_vals = valued_positions("crypto")
    stock_total = total_value(stock_vals)
    crypto_total = total_value(crypto_vals)
    from core.db import latest_cash_balance
    bank_cash = latest_cash_balance() or 0.0
    total_wealth = stock_total + crypto_total + bank_cash

    performance.record_snapshots(stock_total, crypto_total, bank_cash)
    from core import shadow
    for scope in shadow.SCOPES:
        if shadow.exists(scope):
            shadow.record_snapshot(scope)
    history = performance.history_df()
    previous = _previous_total(history)
    delta = total_wealth - previous if previous is not None else None

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Gesamtvermögen", f"{total_wealth:,.2f} €", f"{delta:+,.2f} €" if delta is not None else None)
    c2.metric("Aktien", f"{stock_total:,.2f} €", f"{stock_total / total_wealth * 100:.1f} %" if total_wealth else None)
    c3.metric("Krypto", f"{crypto_total:,.2f} €", f"{crypto_total / total_wealth * 100:.1f} %" if total_wealth else None)
    c4.metric("Liquid", f"{bank_cash:,.2f} €", f"{bank_cash / total_wealth * 100:.1f} %" if total_wealth else None)

    _render_alerts()

    all_vals = stock_vals + crypto_vals
    allocation = _allocation_rows(stock_total, crypto_total, bank_cash)
    left, right = st.columns([1, 1.1])
    with left:
        components.render_allocation_pie(allocation, "Name", "Wert", "Vermögensaufteilung", key="pie_dash_total")
    with right:
        _render_rebalancing(allocation, total_wealth)

    st.divider()
    _render_performance(history)
    st.divider()
    c1, c2 = st.columns([1.25, 1])
    with c1:
        _render_top_positions(all_vals, total_wealth)
    with c2:
        _render_attention(all_vals, total_wealth, allocation)

