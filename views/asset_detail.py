"""Zwei Seiten: Marktanalyse (Markt-Temperatur, Zyklus-Position, KI-Zyklus-Einschätzung
für Krypto/Aktien) und Einzelwertanalyse (Chart, Kennzahlen, News und Agenten-Voll-Analyse
für einen Einzelwert)."""
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import streamlit as st

from agents import cycle_analyst, senior_manager
from analysis import alerts, market_timing, risk as risk_analysis, technical
from analysis import cycle as cycle_mod
from analysis import cycle_backtest, exit_ranking
from core import config, db, profile
from data import crypto as crypto_data
from data import crypto_history
from data import news as news_data
from data import sentiment
from data import stocks as stock_data
from ui import components

_PERIODS = {"3 Monate": ("3mo", 90), "1 Jahr": ("1y", 365),
            "3 Jahre": ("3y", 1095), "5 Jahre": ("5y", 1825)}


def _market_choice(widget_key: str) -> str:
    """Krypto/Aktien-Auswahl, manuell über session_state synchronisiert statt
    über den Widget-eigenen `key`-Mechanismus: der überlebt einen Wechsel
    zwischen st.navigation-Seiten in dieser Streamlit-Version NICHT
    zuverlässig (das Radio springt sonst beim Seitenwechsel auf "Krypto"
    zurück, live getestet) - dasselbe Problem, das `detail_symbol` weiter
    unten schon über manuelles session_state-Lesen/-Schreiben umgeht statt
    über einen Widget-`key`. Jede Seite bekommt einen EIGENEN `widget_key`
    (keine Kollision zwischen den Seiten), der eigentliche Wert lebt im
    gemeinsamen Klartext-Key 'market_choice'."""
    default_label = st.session_state.get("market_choice", "Krypto")
    label = st.radio("Markt", ["Krypto", "Aktien"], horizontal=True,
                     index=0 if default_label == "Krypto" else 1, key=widget_key)
    st.session_state["market_choice"] = label
    return "crypto" if label == "Krypto" else "stock"


def render_market():
    components.page_header("Analysen", "Marktanalyse",
                           "Markt-Temperatur, Zyklus-Position und KI-Einschätzung für Krypto und Aktien.")

    market = _market_choice("indicator_market_market")
    _render_market_temperature(market)

    st.divider()
    st.caption("💡 Einzelnen Wert analysieren? Siehe Seite **Einzelwertanalyse**.")


def render_asset():
    components.page_header("Analysen", "Einzelwertanalyse",
                           "Chart, Kennzahlen, News und KI-Einschätzung für einen einzelnen Wert.")

    at = _market_choice("indicator_market_asset")

    c1, c2 = st.columns([3, 1])
    symbol = c1.text_input("Symbol", value=st.session_state.get("detail_symbol", ""),
                           help=("Krypto-Symbol (BTC)" if at == "crypto"
                                 else "Aktien-Ticker (NVDA, SAP.DE)")).strip().upper()
    period_label = c2.selectbox("Zeitraum", list(_PERIODS.keys()), index=1)

    if not symbol:
        st.info("Symbol eingeben - der Wert muss nicht im Portfolio sein.")
        st.divider()
        st.caption("💡 Markt-Stimmung und Zyklus-Position? Siehe Seite **Marktanalyse**.")
        return
    st.session_state["detail_symbol"] = symbol

    period_yf, period_days = _PERIODS[period_label]
    with st.spinner("Lade Kursdaten ..."):
        if at == "crypto":
            # crypto_history.crypto_series_eur() statt crypto_data.get_history():
            # CoinGecko liefert im freien Tarif nur ~365 Tage, die "5 Jahre"-Auswahl
            # bekäme sonst weniger Daten als angezeigt. crypto_series_eur() wählt
            # die längste plausible Quelle (yfinance/Kraken/CoinGecko), siehe dort.
            series = crypto_history.crypto_series_eur(symbol, days=period_days)
            df = series.to_frame(name="Close") if series is not None else None
            currency = "EUR"
        else:
            df = stock_data.get_history(symbol, period_yf)
            currency = stock_data.get_currency(symbol)
    if df is None or df.empty:
        st.error(f"Keine Kursdaten für '{symbol}' gefunden - Symbol prüfen.")
        return

    tech = technical.summarize(df)
    risk = risk_analysis.asset_risk(df, asset_type=at)

    m1, m2, m3, m6 = st.columns(4)
    m1.metric("Kurs", f"{tech['kurs']:,.4g} {currency}")
    m2.metric("RSI (14)", f"{tech['rsi']:.0f}")
    m3.metric("Technik-Score", f"{tech['t_score']}/100")
    m6.metric("ATR% (Tagesspanne)",
             f"{tech['atr_pct']:.1f}%" if tech.get("atr_pct") is not None else "—",
             help="Durchschnittliche Tages-Schwankungsbreite (Average True Range) "
                  "relativ zum Kurs - Orientierung für Stop-/Positionsgrößen. "
                  "Für Krypto derzeit nicht verfügbar (keine Intraday-High/Low-Daten).")

    if risk:
        r1, r2, r3 = st.columns(3)
        r1.metric("Volatilität p.a.", f"{risk['volatilitaet_pct']:.0f}%")
        r2.metric("Max Drawdown", f"{risk['max_drawdown_pct']:.0f}%")
        r3.metric("Sharpe (rf=0)", f"{risk['sharpe']:.2f}")

    components.render_price_chart(tech["df"], tech["fibs"], key=f"price_{symbol}_{at}")

    col_l, col_r = st.columns(2)
    with col_l:
        st.markdown("#### 📊 Fundamentaldaten")
        fundamentals = (crypto_data.get_market_data(symbol) if at == "crypto"
                        else stock_data.get_fundamentals(symbol))
        fundamentals = {k: v for k, v in fundamentals.items() if v is not None}
        if fundamentals:
            for k, v in fundamentals.items():
                display = components.format_fundamental(k, v) if isinstance(v, (int, float)) else v
                st.markdown(f"- **{k.replace('_', ' ').title()}:** {display}")
        else:
            st.caption("Keine Fundamentaldaten verfügbar.")
    with col_r:
        st.markdown("#### 📰 News")
        news = news_data.get_news(symbol, at)
        if news:
            for n in news:
                st.caption(f"**{n['source']}**: [{n['title']}]({n['link']})")
        else:
            st.caption("Keine aktuellen News gefunden.")

    st.divider()
    st.markdown("### 🤖 Agenten-Analyse (Senior Asset Manager)")
    spec_model = st.session_state["specialist_model"]
    senior_model = st.session_state["senior_model"]
    est = senior_manager.estimate_cost("asset", spec_model, senior_model)
    st.caption(f"4 Spezialisten + Senior · geschätzte Kosten ≈ ${est:.3f} "
               f"(Modelle in der Sidebar wählbar)")

    if st.button("🚀 Voll-Analyse starten", type="primary"):
        status = st.status("Analyse läuft ...", expanded=True)
        result = senior_manager.run_asset_analysis(
            symbol, at, spec_model, senior_model,
            progress_cb=lambda msg: status.write(msg),
        )
        status.update(label="Analyse abgeschlossen", state="complete", expanded=False)
        st.session_state[f"analysis_{symbol}_{at}"] = result
        st.session_state["session_cost"] = (
            st.session_state.get("session_cost", 0.0) + result.get("total_cost_usd", 0.0)
        )

    result = st.session_state.get(f"analysis_{symbol}_{at}")
    if result:
        components.render_analysis_result(result, key_prefix=f"asset_{symbol}_{at}")

    st.divider()
    st.caption("💡 Markt-Stimmung und Zyklus-Position? Siehe Seite **Marktanalyse**.")


def _fetch_crypto_readings() -> tuple[dict, int | None]:
    """Alle Krypto-Sentiment-Quellen parallel holen (Muster wie alerts.metrics_for)."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            "fear_greed": pool.submit(sentiment.fear_greed),
            "mayer": pool.submit(market_timing.mayer_multiple),
            "breadth": pool.submit(sentiment.altcoin_breadth_30d),
            "global": pool.submit(sentiment.global_metrics),
            "meme": pool.submit(sentiment.meme_market),
            "coinbase_rank": pool.submit(sentiment.coinbase_app_rank),
        }
        results = {k: f.result() for k, f in futures.items()}
    # global_metrics() liefert ein Dict mit sowohl btc_dominance als auch
    # stablecoin_dominance - beide Scorer greifen sich daraus nur ihr eigenes Feld.
    readings = {
        "fear_greed": results["fear_greed"],
        "mayer": results["mayer"],
        "breadth": results["breadth"],
        "btc_dominance": results["global"],
        "meme": results["meme"],
        "stablecoin_dominance": results["global"],
    }
    return readings, results["coinbase_rank"]


def _fetch_stock_readings() -> dict:
    """Alle Aktien-Sentiment-Quellen sequentiell holen.

    Bewusst NICHT über ThreadPoolExecutor wie beim Krypto-Fetch: dort läuft
    yfinance nur EINMAL parallel mit (mayer_multiple, die übrigen Quellen sind
    andere APIs). Hier würden vier yf.download()-Aufrufe für verschiedene
    Ticker gleichzeitig laufen - reproduzierbar nicht thread-sicher (mal
    stille None-Rückgaben, mal harte TypeError/ValueError durch vertauschte
    Spalten). Sequentiell ist minimal langsamer, aber verlässlich; der
    bestehende ttl_cache(300) auf stock_data.get_history hält Folgeaufrufe schnell.
    """
    return {
        "vix": market_timing.vix_level(),
        "sp500_ma200": market_timing.sp500_ma200_ratio(),
        "breadth": market_timing.breadth_ratio(),
        "risk_appetite": market_timing.risk_appetite_ratio(),
    }


def _render_market_temperature(market: str):
    coinbase_rank = None
    with st.spinner("Lade Sentiment-Indikatoren …"):
        if market == "crypto":
            readings, coinbase_rank = _fetch_crypto_readings()
        else:
            readings = _fetch_stock_readings()
    temp = market_timing.market_temperature(readings, market=market)

    market_suffix = " (US-Aktienmarkt)" if market == "stock" else ""

    if temp["score"] is None:
        st.markdown(f"### 🌡️ Markt-Temperatur{market_suffix}")
        st.info("Sentiment-Daten gerade nicht erreichbar.")
        return

    # Tageswert je Indikator festhalten, mit Markt-Praefix (Krypto und Aktien
    # duerfen sich am selben Tag nicht ueberschreiben - core.db._migrate_sentiment_prefix).
    for row in temp["breakdown"]:
        db.save_sentiment(f"{market}:{row['key']}", row["score"])
    db.save_sentiment(f"{market}:overall", temp["score"])

    color = components.gauge_color(temp["score"], invert=True)
    st.markdown(
        f"### 🌡️ Markt-Temperatur{market_suffix}: "
        f"<span style='color:{color}'>{temp['classification']} — {temp['score']:.0f}/100</span>",
        unsafe_allow_html=True,
    )
    coverage = temp["coverage_pct"]
    market_sources = ("CoinGecko, Fear&Greed-Index, On-Chain-Dominanz, yfinance (Mayer)"
                      if market == "crypto" else "yfinance (VIX, S&P 500, RSP/SPY, HYG/IEF)")
    low_cov_note = ('unter 60% Abdeckung wird keine "Extreme"-Einordnung ausgegeben'
                    if coverage < 60 else "")
    components.render_datenstand(coverage, market_sources, note=low_cov_note)
    if market == "crypto":
        if coinbase_rank:
            st.warning(f"📱 Coinbase auf Platz {coinbase_rank} der Gratis-Charts "
                      f"(Apple US) — erhöhtes Retail-Interesse.")
        else:
            st.caption("Coinbase aktuell nicht in den Top-200 Gratis-Apps.")

    breakdown = temp["breakdown"]
    col_gauge, col_bars = st.columns([1, 2], vertical_alignment="center")
    with col_gauge:
        components.render_gauge(temp["score"], "Markt-Temperatur" + (" (US)" if market == "stock" else ""),
                                key=f"temp_gauge_{market}", invert=True, height=220)
    with col_bars:
        bar_rows = [{"label": row["label"].split(" (")[0], "score": row["score"], "invert": True,
                    "horizon": row.get("horizon", "")}
                   for row in breakdown]
        components.render_bar_list(bar_rows, key=f"temp_bars_{market}")

    if temp["unavailable"]:
        labels = market_timing.labels_for(market)
        missing = ", ".join(labels.get(k, k) for k in temp["unavailable"])
        st.caption(f"Gerade nicht verfügbar (Gewichte auf die übrigen umverteilt): {missing}")
    extra = (" Dominanz- und Meme-Momentum-Trends werden ab jetzt selbst aufgezeichnet "
            "(CoinGecko liefert dafür nur Momentanwerte)." if market == "crypto" else "")
    st.caption("Gewichtung ist eine Einschätzung, kein Backtest-Ergebnis." + extra)

    st.divider()
    _render_cycle_ladder(market, temp, readings)
    _render_cycle_ai(market)

    _render_position_details(market)


def _ladder_stages_line(label: str, thresholds: list[float], active_tier: int) -> str:
    """Kompakte, immer sichtbare Zeile 'Stufe 1/2/3' neben dem Barometer -
    bisher standen die eigenen Schwellenwerte nur im eingeklappten Expander
    'Eigene Stufen einstellen' und waren am Gauge selbst (nur Farbbänder,
    keine Achsenbeschriftung) kaum ablesbar. Die aktuell erreichte Stufe wird
    fett hervorgehoben, damit auf einen Blick klar ist, wo der Score gerade steht."""
    parts = []
    for i, value in enumerate(thresholds, start=1):
        text = f"Stufe {i}: {value:.0f}"
        parts.append(f"**{text}**" if i <= active_tier else text)
    return f"{label} — " + " · ".join(parts)


def _render_ladder_suggestion(market: str, cyc: dict, sell: list[float], buy: list[float]):
    """KI-Vorschlag für die sechs Stufen-Schwellen, nur auf Klick (Kosten) -
    reiner Text mit Begründung, den der Nutzer optional per Button in die
    Zahlenfelder unten übernehmen kann. core.profile.ladder_config bleibt die
    alleinige Quelle der Wahrheit; die App ändert nichts automatisch."""
    sugg_model = st.session_state.get("senior_model", config.DEFAULT_SENIOR_MODEL)
    sugg_est = cycle_analyst.estimate_ladder_suggestion_cost(sugg_model)
    if st.button("💡 KI-Vorschlag für Stufen holen", key=f"ladder_suggest_{market}"):
        with st.spinner("KI schlägt Stufen vor ..."):
            suggestion = cycle_analyst.suggest_ladder_stages(market, cyc, sell, buy, sugg_model)
        st.session_state[f"ladder_suggestion_{market}"] = suggestion
        st.session_state["session_cost"] = (
            st.session_state.get("session_cost", 0.0) + suggestion.get("total_cost_usd", 0.0)
        )
    st.caption(f"Geschätzte Kosten ≈ ${sugg_est:.4f} ({config.model_label(sugg_model)}, "
              "Modell in der Sidebar wählbar)")

    suggestion = st.session_state.get(f"ladder_suggestion_{market}")
    if not suggestion:
        return
    if "error" in suggestion:
        st.error(suggestion["error"])
        return

    s, b = suggestion["sell"], suggestion["buy"]
    st.info(f"**KI-Vorschlag:** Verkauf {s[0]:.0f} / {s[1]:.0f} / {s[2]:.0f} · "
           f"Kauf {b[0]:.0f} / {b[1]:.0f} / {b[2]:.0f}\n\n{suggestion['begruendung']}")
    if st.button("Vorschlag übernehmen", key=f"ladder_suggest_apply_{market}"):
        st.session_state[f"ladder_sell1_{market}"] = s[0]
        st.session_state[f"ladder_sell2_{market}"] = s[1]
        st.session_state[f"ladder_sell3_{market}"] = s[2]
        st.session_state[f"ladder_buy1_{market}"] = b[0]
        st.session_state[f"ladder_buy2_{market}"] = b[1]
        st.session_state[f"ladder_buy3_{market}"] = b[2]
        st.rerun()
    st.divider()


def _render_cycle_ladder(market: str, temp: dict, readings: dict):
    """Zweites Barometer 'Zyklus-Position' + eigene, frei einstellbare Kauf-/
    Verkaufs-Stufen. Zeigt nur an, was DEINE Regel gerade sagt - die App
    verkauft/kauft nichts automatisch (wie das Notgroschen-Feature: rein
    informativ, keine automatischen Aktionen) und bewertet die Regel selbst
    nicht als Anlageempfehlung.

    Krypto: analysis.cycle.cycle_score() - 7 Bausteine über die GESAMTE
    verfügbare BTC-Historie (Perzentil-Scoring statt fester Stützpunkte,
    siehe dortiger Docstring), abgelöst von der früheren 2-Indikatoren-
    Näherung (nur Fear&Greed + Mayer auf 1 Jahr Historie). Aktien: entspricht
    weiterhin 1:1 der Markt-Temperatur, da dort alle 4 Indikatoren ohnehin
    volle Kurs-Historie haben.
    """
    if market == "crypto":
        cyc = cycle_mod.cycle_score()
    else:
        cyc = temp
    if cyc["score"] is None:
        return

    cfg = profile.ladder_config(market)
    sell, buy = cfg["sell"], cfg["buy"]
    score = cyc["score"]
    sell_tier = market_timing.active_ladder_tier(score, sell, "sell")   # live, unpersistiert
    buy_tier = market_timing.active_ladder_tier(score, buy, "buy")

    # Sperrklinke wird NIE mehr automatisch beim Rendern fortgeschrieben (früher:
    # jeder Seitenaufruf mit ausreichendem Score hat sofort und dauerhaft eine
    # Stufe festgeschrieben - genügte ein einzelner, dünn abgedeckter Indikator,
    # konnte das allein durchs Öffnen der Seite passieren). Jetzt: nur noch
    # Lesen hier, Schreiben ausschließlich über den expliziten "Stufe
    # bestätigen"-Button unten.
    progress = profile.cycle_progress(market)
    confirmed_tier = progress["reached_tier"]

    st.markdown("### 🎯 Zyklus-Position" + (" (US-Aktienmarkt)" if market == "stock" else ""))
    if market == "crypto":
        st.caption(f"Zyklus-Score aus {len(cyc['breakdown'])} Bausteinen über die gesamte "
                   f"verfügbare BTC-Historie — regelbasiertes Regime: **{cyc['regime']}** "
                   f"({cyc['regime_reason']})")
        components.render_datenstand(cyc["coverage_pct"],
                                     "BTC-Kurshistorie (yfinance/Kraken/CoinGecko), On-Chain-"
                                     "Metriken (bitcoin-data.com), Fear&Greed-Index")
    else:
        st.caption("Entspricht der Markt-Temperatur (US-Aktienmarkt: S&P 500, VIX, RSP/SPY, "
                   "HYG/IEF) - alle 4 Indikatoren haben ohnehin volle Kurs-Historie.")

    col_gauge, col_status = st.columns([1, 2])
    with col_gauge:
        components.render_ladder_gauge(score, "Zyklus-Position" + (" (US)" if market == "stock" else ""),
                                       buy, sell, key=f"cycle_gauge_{market}", height=200)
    with col_status:
        st.markdown(_ladder_stages_line("🔴 Verkauf", sell, confirmed_tier))
        st.markdown(_ladder_stages_line("🟢 Kauf", buy, buy_tier))

        if sell_tier > confirmed_tier:
            st.info(f"📈 Score erreicht Verkaufs-Stufe {sell_tier}/3 (Score {score:.0f}) — "
                   "**noch nicht bestätigt**. Erst nach deiner Bestätigung zählt sie als "
                   "erreicht und bleibt (Sperrklinke) bestehen, falls der Score wieder fällt.")
            if st.button(f"Stufe {sell_tier} bestätigen", key=f"cycle_confirm_{market}"):
                basis = exit_ranking.held_symbols() if market == "crypto" else None
                profile.advance_cycle_tier(market, sell_tier, note=f"Score {score:.0f}", basis=basis)
                st.rerun()

        if confirmed_tier:
            target_pct = profile.LADDER_FRACTIONS_PCT[confirmed_tier - 1]
            executed = progress["executed_pct"]
            offen = max(0.0, target_pct - executed)
            st.warning(f"🔴 Bestätigte Verkaufs-Stufe {confirmed_tier}/3 — deine Regel: "
                      f"{target_pct}% verkaufen. Ausgeführt: {executed:.0f}% · "
                      f"**offen: {offen:.0f}%**"
                      + ("" if sell_tier >= confirmed_tier else
                         f" (hält vom früheren Höchststand fest, Score aktuell nur "
                         f"noch bei Stufe {sell_tier})"))
        elif buy_tier:
            st.success(f"🟢 Kauf-Stufe {buy_tier}/3 erreicht (Score {score:.0f}) — "
                      f"deine Regel: {profile.LADDER_FRACTIONS_PCT[buy_tier - 1]}% des "
                      "verfügbaren Cash einsetzen.")
        else:
            st.caption(f"Keine Stufe aktiv (Score {score:.0f}).")
        st.caption(
            "⚠️ Das ist **deine eigene, frei einstellbare Regel** — keine Anlageempfehlung "
            "der App. Feste Schwellen hätten in vergangenen Bullruns oft zu früh verkauft "
            "(z.B. Schwelle 80 im Nov. 2020 erreicht, BTC stieg danach noch +288%) oder "
            "Alt-Coin-Tops verpasst (die Hoch-Tage einzelner Coins liegen historisch über "
            "die gesamte Skala verteilt). Sinnvoller Einsatz eher als ein Signal unter "
            "mehreren, nicht als Automatismus."
        )

    if market == "crypto":
        _render_cycle_details(cyc, sell, confirmed_tier, progress)

    with st.expander("⚙️ Eigene Stufen einstellen"):
        _render_ladder_suggestion(market, cyc, sell, buy)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Verkauf (rot)**")
            s1 = st.number_input("Stufe 1 (33%)", 0.0, 100.0, sell[0], step=1.0, key=f"ladder_sell1_{market}")
            s2 = st.number_input("Stufe 2 (66%)", 0.0, 100.0, sell[1], step=1.0, key=f"ladder_sell2_{market}")
            s3 = st.number_input("Stufe 3 (100%)", 0.0, 100.0, sell[2], step=1.0, key=f"ladder_sell3_{market}")
        with c2:
            st.markdown("**Kauf (grün)**")
            b1 = st.number_input("Stufe 1 (33%)", 0.0, 100.0, buy[0], step=1.0, key=f"ladder_buy1_{market}")
            b2 = st.number_input("Stufe 2 (66%)", 0.0, 100.0, buy[1], step=1.0, key=f"ladder_buy2_{market}")
            b3 = st.number_input("Stufe 3 (100%)", 0.0, 100.0, buy[2], step=1.0, key=f"ladder_buy3_{market}")
        if st.button("Stufen speichern", key=f"ladder_save_{market}"):
            problems = profile.validate_ladder_stages([s1, s2, s3], [b1, b2, b3])
            if problems:
                for p in problems:
                    st.error(p)
            else:
                profile.save_ladder_config(market, [s1, s2, s3], [b1, b2, b3])
                st.success("Eigene Stufen gespeichert.")
                st.rerun()
        if market == "crypto" and st.button(
            "🔄 Zyklus-Fortschritt zurücksetzen (neuer Zyklus / Depot neu aufgebaut)",
            key="cycle_progress_reset",
        ):
            profile.reset_cycle_progress(market)
            st.success("Zurückgesetzt - die Sperrklinke beginnt wieder bei Stufe 0.")
            st.rerun()


def _render_cycle_details(cyc: dict, sell_thresholds: list[float], confirmed_tier: int, progress: dict):
    """Indikator-Tabelle, Trigger-Preise, Verkaufsliste, Ausführungs-Fortschritt
    und Backtest zur Zyklus-Position - nur Krypto (BTC-Historie + On-Chain-
    Daten sind die Grundlage, für Aktien gibt es das nicht). `confirmed_tier`
    ist die BESTÄTIGTE Stufe (core.profile.cycle_progress()["reached_tier"]),
    nicht der live gelesene Score-Stand - alles hier erscheint erst nach der
    expliziten Bestätigung in _render_cycle_ladder()."""
    if confirmed_tier:
        target_pct = profile.LADDER_FRACTIONS_PCT[confirmed_tier - 1]
        c1, c2 = st.columns([2, 1])
        with c1:
            executed = st.number_input(
                "Tatsächlich ausgeführt (%) - rein informativ, die App verkauft nichts selbst",
                0.0, 100.0, float(progress["executed_pct"]), step=1.0, key="cycle_executed_pct",
            )
        with c2:
            st.write("")
            st.write("")
            if st.button("Speichern", key="cycle_executed_save"):
                profile.mark_cycle_executed("crypto", executed)
                st.success("Gespeichert.")
                st.rerun()

    with st.expander("📋 Indikatoren im Detail"):
        rows = [{"Baustein": r["label"], "Wert": r["text"],
                "Perzentil": f"{r['score']:.0f}/100", "Gewicht": f"{r['weight_pct']:.0f}%"}
               for r in cyc["breakdown"]]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        if cyc["unavailable"]:
            st.caption("Nicht verfügbar (Gewichte auf die übrigen umverteilt): "
                      + ", ".join(cyc["unavailable"]))

    with st.expander("💰 Trigger-Preise (nächste Stufen)"):
        st.caption("Näherung: nur die preisabhängigen Bausteine (Mayer, 2-Jahres-Multiple, "
                  "ATH-Abstand) werden variiert - On-Chain- und Sentiment-Bausteine bleiben "
                  "auf dem heutigen Stand eingefroren.")
        for i, th in enumerate(sell_thresholds, start=1):
            tp = cycle_mod.trigger_price(th, cycle=cyc)
            st.write(f"Stufe {i} (Score ≥ {th:.0f}): "
                    + (f"ab BTC ≈ {tp:,.0f} €" if tp else "bei aktueller Lage auch bei "
                                                          "sehr hohem Preis nicht erreichbar"))

    if confirmed_tier:
        target_pct = profile.LADDER_FRACTIONS_PCT[confirmed_tier - 1]
        with st.expander(f"📉 Verkaufsliste für Stufe {confirmed_tier} ({target_pct}%)", expanded=True):
            partial = st.checkbox("Letzte Position anteilig verkaufen (statt ganze Position)",
                                  key="cycle_sell_partial",
                                  help="Trifft das Ziel genauer, statt bei der letzten nötigen "
                                       "Position ganz zu überschießen.")
            with st.spinner("Berechne Ausstiegs-Rangliste …"):
                plan = exit_ranking.sell_list(target_pct, basis=progress.get("basis") or {},
                                              partial_last=partial)
            if plan["basis_price_unavailable"]:
                st.caption("⚠️ Kein aktueller Kurs für Basis-Symbole: "
                          + ", ".join(plan["basis_price_unavailable"])
                          + " - Zielbetrag ggf. ungenau.")
            if plan["to_sell"]:
                rows = [{"Symbol": r["symbol"], "Menge": r["quantity"], "Kurs (€)": r["price_eur"],
                        "Wert (€)": r["value_eur"], "Exit-Score": r["exit_score"]}
                       for r in plan["to_sell"]]
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                            column_config={
                                "Menge": st.column_config.NumberColumn(format="%.6g"),
                                "Kurs (€)": st.column_config.NumberColumn(format="%.4f"),
                                "Wert (€)": st.column_config.NumberColumn(format="%.2f"),
                                "Exit-Score": st.column_config.NumberColumn(format="%.0f"),
                            })
                if plan["basis_value_eur"]:
                    st.caption(f"Gesamt: {plan['sell_value_eur']:,.2f} € — kumuliert seit "
                              f"Zyklusbeginn bereits {plan['sell_pct_actual']:.0f}% von "
                              f"{plan['basis_value_eur']:,.2f} € Ausgangsbestand verkauft "
                              f"(Ziel dieser Stufe: {target_pct}%). Schwächste Positionen werden "
                              "komplett verkauft statt jede Position anteilig anzuschneiden.")
                else:
                    st.caption(f"Gesamt: {plan['sell_value_eur']:,.2f} € "
                              f"({plan['sell_pct_actual']:.0f}% von {plan['total_value_eur']:,.2f} €). "
                              "Schwächste Positionen werden komplett verkauft statt jede Position "
                              "anteilig anzuschneiden - konzentriert den Rest in den stärksten Namen.")
            else:
                st.caption("Kein bewertbarer Krypto-Bestand für eine Verkaufsliste.")

    with st.expander("📈 Wie gut war das historisch? (Backtest)"):
        with st.spinner("Rechne Walk-Forward-Backtest …"):
            bt = cycle_backtest.run_backtest(sell_thresholds=sell_thresholds)
        if not bt.get("available"):
            st.caption(bt.get("reason", "Kein Backtest verfügbar."))
        else:
            st.caption(f"Walk-Forward {bt['start_date']} bis {bt['end_date']} - nur die "
                      "Verkaufsseite, kein simulierter Wiedereinstieg. Perzentile ausschließlich "
                      "aus Daten bis zum jeweiligen Tag (kein Zukunftswissen).")
            st.caption("⚠️ Plausibilitätscheck über einen einzigen historischen Pfad mit sehr "
                      "wenigen vollständigen Zyklen — kein Beleg für optimale Schwellen; kein "
                      "Wiedereinstieg, keine Steuern, keine Cash-Verzinsung modelliert.")
            b1, b2, b3 = st.columns(3)
            b1.metric("Deine Leiter", f"{bt['ladder_value_eur']:,.0f} €")
            b2.metric("Buy & Hold", f"{bt['buy_hold_value_eur']:,.0f} €",
                     delta=f"{bt['ladder_vs_buy_hold_pct']:+.0f}%" if bt.get("ladder_vs_buy_hold_pct") is not None else None)
            b3.metric("Perfekter Topverkauf", f"{bt['perfect_top_value_eur']:,.0f} €")
            if bt["avg_miss_vs_next_peak_pct"] is not None:
                st.caption(f"{bt['trade_count']} Verkäufe ausgelöst, im Schnitt "
                          f"{bt['avg_miss_vs_next_peak_pct']:.0f}% unter dem jeweils nächsten Hoch "
                          f"verkauft (schlechtester Einzelfall: {bt['worst_miss_vs_next_peak_pct']:.0f}%).")
            else:
                st.caption("In diesem Zeitraum wurde keine Verkaufsstufe ausgelöst.")


def _render_cycle_ai(market: str):
    """KI-Zyklus-Einschätzung: eigener dritter Analyse-Modus (agents/cycle_analyst.py),
    nur Krypto, nur auf Klick (API-Kosten) - nie automatisch beim Seitenaufruf."""
    if market != "crypto":
        return

    st.divider()
    st.markdown("### 🤖 KI-Zyklus-Einschätzung")
    model = st.session_state.get("senior_model", config.DEFAULT_SENIOR_MODEL)
    est = cycle_analyst.estimate_cost(model)
    st.caption(f"Einmalige Markteinordnung ({config.model_label(model)}) · geschätzte Kosten "
              f"≈ ${est:.3f} (Modell in der Sidebar wählbar). Bewertet den Markt, nicht dein "
              "Depot - die Ausstiegs-Rangliste oben bleibt davon unabhängig.")

    if st.button("🔮 Zyklus-Einschätzung starten", key="cycle_ai_run"):
        status = st.status("KI bewertet die Zyklus-Lage ...", expanded=True)
        result = cycle_analyst.run_cycle_analysis(model, progress_cb=lambda msg: status.write(msg))
        status.update(label="Fertig", state="complete", expanded=False)
        st.session_state["cycle_ai_result"] = result
        st.session_state["session_cost"] = (
            st.session_state.get("session_cost", 0.0) + result.get("total_cost_usd", 0.0)
        )

    result = st.session_state.get("cycle_ai_result")
    if not result:
        return
    if "error" in result:
        st.error(result["error"])
        return

    c1, c2 = st.columns([2, 1])
    with c1:
        st.markdown(f"**Regelbasiertes Regime:** {result['cycle_score']['regime']}  \n"
                   f"**KI-Einordnung:** {result['zyklus_phase']}")
        st.caption(result["phase_begruendung"])
    with c2:
        components.render_gauge(result["top_wahrscheinlichkeit"], "Top-Wahrscheinlichkeit",
                                key="cycle_ai_gauge", invert=True, height=180)
    st.caption("Getrennt vom Zyklus-Score oben: der misst Überhitzung JETZT, die "
              "Top-Wahrscheinlichkeit schätzt die Nähe zu einem Wendepunkt - beide dürfen "
              "auseinanderlaufen, das ist erwünschte Information, kein Widerspruch.")

    st.markdown(result["zusammenfassung"])

    a1, a2 = st.columns(2)
    with a1:
        st.markdown("**Spricht für ein nahes Top:**")
        for p in result["argumente_top"]:
            st.markdown(f"- {p}")
    with a2:
        st.markdown("**Spricht dagegen:**")
        for p in result["argumente_dagegen"]:
            st.markdown(f"- {p}")

    if result["modell_abweichung"]:
        st.info(f"**Abweichung vom Quant-Modell:** {result['modell_abweichung']}")

    if result["beobachten"]:
        st.markdown("**Zu beobachten:**")
        for p in result["beobachten"]:
            st.markdown(f"- {p}")

    st.caption(f"⚠️ {result['unsicherheit']}")
    components.render_usage(result.get("usage", {}))

    with st.expander("Frühere Einschätzungen"):
        runs = [r for r in db.list_agent_runs() if r["mode"] == "cycle"]
        if runs:
            rows = [{"Datum": r["created_at"][:16].replace("T", " "),
                    "Top-Wahrscheinlichkeit": r["total_score"], "Phase": r["recommendation"]}
                   for r in runs]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            st.caption("Noch keine früheren Einschätzungen.")


def _render_position_details(market: str):
    positions = db.list_positions(market)
    symbols = sorted({p.symbol for p in positions if p.symbol != "EUR" and p.quantity > 0})
    if not symbols:
        return
    label = "ATH-Abstand" if market == "crypto" else "52W-Hoch-Abstand"
    with st.expander(f"Deine Positionen im Detail ({label}, RSI)"):
        with st.spinner("Lade Kennzahlen …"):
            if market == "crypto":
                # ATH-Abstand etc. in EINEM Batch-Request holen (get_market_data_batch) -
                # ein get_market_data()-Aufruf PRO Position parallel sprengt reproduzierbar
                # das freie CoinGecko-Rate-Limit (429), wodurch der ATH-Abstand für die
                # meisten Coins leer bliebe.
                extra = crypto_data.get_market_data_batch(symbols)
                with ThreadPoolExecutor(max_workers=6) as pool:
                    metric_futures = {s: pool.submit(alerts.asset_metrics, s, market) for s in symbols}
                    metrics = {s: f.result() for s, f in metric_futures.items()}
            else:
                # Sequentiell: yfinance ist bei gleichzeitigen yf.download()-Aufrufen
                # für unterschiedliche Ticker nicht thread-sicher (siehe
                # _fetch_stock_readings) - hat hier reproduzierbar Abstürze und
                # danach über den ttl_cache falsche Werte für's eigene Symbol
                # der Einzelwertanalyse verursacht.
                metrics = {s: alerts.asset_metrics(s, market) for s in symbols}
                extra = {s: stock_data.get_fundamentals(s) for s in symbols}

        rows = []
        for s in symbols:
            price = metrics.get(s, {}).get("price_eur")
            if market == "crypto":
                dist = extra.get(s, {}).get("ath_abstand_pct")
            else:
                high_52w = extra.get(s, {}).get("52w_hoch")
                dist = (price / high_52w - 1) * 100 if price and high_52w else None
            rows.append({
                "Symbol": s,
                "Kurs (€)": price,
                "RSI": metrics.get(s, {}).get("rsi"),
                label: dist,
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                    column_config={
                        "Kurs (€)": st.column_config.NumberColumn(format="%.4f"),
                        "RSI": st.column_config.NumberColumn(format="%.0f"),
                        label: st.column_config.NumberColumn(format="%+.1f %%"),
                    })
