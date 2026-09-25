# Erneute Analyse des Trading-Bots

Stand: 23.09.2026, nach den Korrekturen zur ersten Analyse. Dieser Bericht aktualisiert die Bewertung in `TRADING_BOT_ANALYSE.md`. Untersucht wurden die geänderten Fehlerpfade und ihre Tests. Anwendungscode und Handelsparameter wurden nicht verändert; keine echten Handelsaufträge wurden ausgelöst. Die aktive Handelsdatenbank wurde bei dieser Nachprüfung nicht erneut ausgewertet; ältere Performancezahlen werden deshalb nicht als aktueller Stand übernommen.

## Ergebnis

Die Korrekturen sind vorhanden und verbessern das Verhalten deutlich. Der ursprüngliche API-Kontostandsfehler, die fehlende Stop-Wiederholung im Sicherheitstakt und die fehlende Wiedereinstiegssperre im Rücktest sind für die geprüften Abläufe behoben. Notausstieg und Fill-Abrechnung enthalten jedoch weiterhin reproduzierbare Lücken.

**446 bestehende Tests bestanden**, 44 Pandas-Deprecation-Warnungen, Laufzeit 78,69 Sekunden. Gegenüber dem früheren gezielten Lauf sind neun Tests hinzugekommen. Zusätzlich wurden fünf isolierte Szenarien mit simulierten Börsenantworten und SQLite im Arbeitsspeicher ausgeführt.

## Bestätigte Korrekturen

| Vorheriger Befund | Aktueller Stand |
|---|---|
| Kontostandsabfrage nach Fill verhindert Notausstieg | Behoben für diesen Fehler: Ausnahme wird abgefangen, Position rekonstruiert, Notausstieg versucht. Unabhängig nachgestellt: ein Schließungsversuch, keine offene Position danach. |
| Scheiternder Notausstieg ohne zuverlässige Folgesperre | Verbessert: Wenn der Fehler den vorgesehenen Notausstiegszweig erreicht, bleiben Position, Wiederholungsmarker und Kill-Switch erhalten. |
| Teilgewinn doppelt gezählt | Nur teilweise behoben: funktioniert bei zeitlich klar getrennten Fills, scheitert innerhalb derselben Sekunde. |
| Stop-Ersetzung erst nach vier Stunden wiederholt | Für den geprüften Ablauf behoben: `retry_unconfirmed_exchange_stops()` wird auch ohne neues Entscheidungsfenster aufgerufen. |
| Rücktest ohne Wiedereinstiegssperre | Behoben: Live und Rücktest verwenden `reentry_block_reason()`; der Rücktest setzt beim Schließen den Sperrzustand. |

## Offene Befunde

### P1 – Datenbankfehler nach Fill verhindert weiterhin den Notausstieg

Fundstelle: `core/bot.py:864` und die weiteren Buchungen vor dem Notausstiegszweig.

Nach einem bestätigten Einstieg mit fehlgeschlagener Stop-Platzierung wird zuerst `db.add_bot_order()` ausgeführt. Wirft diese Buchung etwa `sqlite3.OperationalError('database is locked')`, wird die Funktion vor dem Schließungsversuch verlassen. Auch spätere DB-Buchungen liegen weiterhin vor der eigentlichen Notfallbehandlung.

**Reproduktion:** Simulierter Einstieg gefüllt, Stop fehlgeschlagen, anschließende Order-Buchung wirft eine DB-Ausnahme. Ergebnis:

- Position an der simulierten Börse weiterhin offen.
- Kein Aufruf von `exchange.close_position()`.
- Keine offene Position in der DB.
- Kein gesetzter Kill-Switch.

Das ist kein Nachweis, dass die produktive Datenbank aktuell blockiert ist. Es zeigt, dass ein solcher Fehler weiterhin genau die sofortige Absicherung verhindert, die in diesem Zustand benötigt wird. Ein übergeordneter Runner kann später erneut abgleichen; der unmittelbare Notausstieg ist jedoch ausgefallen.

**Korrekturansatz:** Den Sicherheitsabschluss eines bestätigten Fills ohne Stop unabhängig vom Erfolg der DB-Protokollierung versuchen. Der Notfallpfad muss die bereits vorliegenden Fill-Daten nutzen können, ohne zuerst eine offene DB-Zeile vorauszusetzen. Fehlgeschlagene Dokumentation separat nachführen und sichtbar melden.

### P1 – Ausfall der Kursabfrage blockiert den Schließungsversuch

Fundstelle: `core/bot.py:1046`.

`close_position()` behandelt den Ausfall von `account_state()` jetzt defensiv. Die davor liegende Abfrage `exchange.mid_price(symbol)` ist jedoch weiterhin ungeschützt. Der Ausdruck `or pos['entry_px']` hilft bei `None`, nicht bei einer Ausnahme. Die echte Hyperliquid-Anbindung kann einen Fehler aus der öffentlichen Kursabfrage weitergeben.

**Reproduktion:** Nach Fill und fehlgeschlagenem Stop fallen Kontostands- und Kursabfrage aus. Ergebnis: Position bleibt offen, **kein Schließungsversuch** wird an den Exchange-Adapter gesendet. Immerhin bleiben jetzt die DB-Zeile und der Kill-Switch erhalten.

**Korrekturansatz:** Auch den Fehler der zusätzlichen Kursabfrage abfangen und einen ausdrücklich für Notfälle vorgesehenen reduce-only-Abschluss versuchen. Dabei muss geprüft werden, welche Daten der darunterliegende SDK-Aufruf seinerseits benötigt. Das kann keinen erfolgreichen Abschluss bei einem vollständigen Börsenausfall garantieren, verhindert aber einen vorzeitigen Abbruch allein wegen dieser vorgeschalteten Hilfsabfrage.

### P2 – Fill-Zeitfilter zählt schnelle Teilschließungen weiterhin doppelt

Fundstellen: `core/bot.py:1669`, `core/db.py:1666`, `core/clock.py:41`.

Die Korrektur verwendet den lokalen Buchungszeitpunkt der letzten Bot-Schließung als Beginn der Fill-Suche. Dieser wird auf ganze Sekunden gekürzt. Die Börsen-Fills haben Millisekundenauflösung; `recent_fills()` nimmt Fills ab dieser Grenze einschließlich auf.

Beispiel:

1. Bot-Teil-Fill um **12:00:00.100**, bereits verbuchter Gewinn **10 $**.
2. Lokale Order-Buchung um **12:00:00.800**, gespeichert als **12:00:00**.
3. Restschluss auf der Börse um **12:00:30**, Gewinn **20 $**.
4. Die Suche ab **12:00:00** enthält beide Fills. Zu deren 30 $ werden die bereits gebuchten 10 $ nochmals addiert.

**Unabhängig reproduziert:** Erwartet **30 $**, gespeichert **40 $**. Die Gegenprobe mit dem Teil-Fill in der vorhergehenden Sekunde liefert korrekt 30 $. Der neue bestehende Regressionstest verwendet einen Abstand von 30 Sekunden und erfasst diesen Grenzfall deshalb nicht.

Der lokale Buchungszeitpunkt ist auch grundsätzlich keine zuverlässige Grenze zwischen bereits abgerechneten und neuen Börsen-Fills: Bei verzögerter Antwort kann ein Restschluss vor der lokalen Buchung liegen und herausgefiltert werden. Mehr Zeitstempelpräzision allein löst diese Zuordnung nicht.

**Korrekturansatz:** Ausführungen anhand eindeutiger Börsen-Fill-IDs genau einmal verbuchen, alternativ die vollständige Positionsabrechnung konsistent aus Fills rekonstruieren und bereits gebuchte Teilbeträge gezielt ersetzen. Tests für gleiche Sekunde, verzögerte Antwort und Gebühren ergänzen.

## Rücktest: verbleibende Einschränkungen

Die Wiedereinstiegssperre ist jetzt vorhanden. Zwei zuvor beschriebene Einschränkungen bestehen weiter:

- Der historische Liquiditätsfilter wird durch `volume_24h_usd=_UNLIMITED_VOLUME` umgangen.
- Das heutige Kandidatenuniversum wird weiterhin rückwirkend verwendet.

Diese Annahmen können Auswahl und Ergebnisse verändern. Erfolgreiche Softwaretests sind deshalb weiterhin kein Rentabilitätsnachweis. Alte Rücktestergebnisse ohne Wiedereinstiegssperre dürfen außerdem nicht als Ergebnis des jetzt korrigierten Rücktests dargestellt werden; dafür ist ein neuer Lauf mit dokumentiertem Code- und Konfigurationsstand erforderlich. Ein solcher historischer Marktdatenlauf war nicht Bestandteil dieser Nachprüfung.

## Verifikationsübersicht

| Zusätzlich geprüftes Szenario | Beobachtung |
|---|---|
| Nur Kontostandsabfrage fällt nach Fill aus | Notausstieg einmal versucht, Position geschlossen |
| Kontostand und Kursabfrage fallen nach Fill aus | Kein Schließungsversuch, Position offen, DB-Eintrag und Kill-Switch vorhanden |
| Order-Buchung fällt nach Fill aus | Kein Schließungsversuch, Position offen, kein offener DB-Eintrag, kein Kill-Switch |
| Bereits verbuchter Teil-Fill in vorheriger Sekunde | Korrekt 30 $ |
| Bereits verbuchter Teil-Fill in derselben Sekunde | Weiterhin falsch 40 $ statt 30 $ |

Bestehende Tests ausgeführt: `test_bot`, `test_bot_guards`, `test_bot_signals`, `test_hyperliquid`, `test_bot_live`, `test_bot_live_backtest_parity`, `test_bot_backtest`, `test_bot_walkforward`. Die Tests liefen mit isolierten Testdaten außerhalb der Windows-Sandbox, da deren Dateizugriffsbeschränkungen bereits bei der ersten Prüfung pytest behindert hatten.

**Priorität:** Notausstieg von vorgeschalteten DB- und Hilfsabfragen entkoppeln, danach Fill-Abrechnung eindeutig machen. Die bereits bestätigten Korrekturen müssen dafür nicht zurückgenommen werden.
