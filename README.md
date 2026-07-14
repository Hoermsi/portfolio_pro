# 💼 Portfolio Pro

Professionelles Analyse- und Portfolio-Tool für **Aktien und Krypto** (getrennt geführt)
mit **Multi-Agenten-KI** („Senior Asset Manager") auf Basis der Claude API.

## Features

- **Dashboard**: Gesamtvermögen, Allokation Aktien/Krypto, Wertentwicklung über Zeit
- **Aktien**: Positionen (SQLite), Flatex-CSV-Import, Kurse via yfinance
- **Krypto**: automatischer **Kraken-Sync** (read-only) + manuelle Positionen, Kurse via CoinGecko
- **Einzelwert-Analyse**: Chart (Bollinger, MA50/200, Fibonacci), RSI/MACD, Risiko-Kennzahlen, Fundamentaldaten, News
- **Agenten-Analyse**: 4 Spezialisten (Technik, Fundamental, News, Risiko) laufen parallel,
  der Senior Asset Manager erstellt das Gesamturteil — Modelle & Kosten in der Sidebar
- **Portfolio-Review**: Diversifikation, Klumpenrisiken, Korrelationen
- **Claude-Chat**: Claude hat Tool-Zugriff auf dein echtes Portfolio und Marktdaten

## Installation

```bat
cd portfolio_pro
pip install -r requirements.txt
```

API-Keys sind **optional** und lassen sich direkt in der App eingeben (Ersteinrichtung
beim ersten Start oder später unter *Einstellungen → API-Keys*). Wer lieber eine Datei
nutzt: `copy .env.example .env` und die Keys dort eintragen.

## Starten

```bat
python -m streamlit run app.py
:: oder Doppelklick auf "Start Portfolio Pro.bat"
```

Beim **allerersten Start** (leere Datenbank) führt ein Ersteinrichtungs-Assistent durch
API-Keys und Profil — alles überspringbar. Ein vorhandenes `portfolio.json` der alten App
wird dabei automatisch übernommen.

## Wo die Daten liegen

Alle veränderlichen Daten (Positionen, Cash, in-App gespeicherte API-Keys) liegen in
`portfolio.db` **getrennt vom Code** unter `%LOCALAPPDATA%\PortfolioPro\`. Ein Update
tauscht nur den Programmcode und lässt diese Daten unangetastet. Beim ersten Start nach
dem Umzug wird eine noch im App-Ordner liegende alte `portfolio.db` automatisch dorthin
übernommen. Für die Entwicklung lässt sich der Ort per Umgebungsvariable
`PORTFOLIO_PRO_DATA_DIR` überschreiben.

## An Freunde weitergeben (Installer, empfohlen)

Für nicht-technische Empfänger gibt es einen **Installer mit gebündeltem Python**:
eine `Setup.exe` bringt App-Code, eine eigene Python-Laufzeit und eine
Start-Verknüpfung mit — kein vorinstalliertes Python, keine Admin-Rechte nötig.
Bau-Anleitung: siehe [`build/README.md`](build/README.md). Kurz:

```powershell
cd build
powershell -ExecutionPolicy Bypass -File build.ps1
:: Ergebnis: build\Output\PortfolioPro-Setup-<version>.exe
```

Der Freund führt die `Setup.exe` aus und startet die App über die Verknüpfung; beim
ersten Start durchläuft er die Ersteinrichtung mit seinen eigenen Keys.

### Updates
Neue Versionen kommen **über GitHub Releases**: Version in `core/version.py` erhöhen,
Release mit Tag `vX.Y.Z` + Code-Zip anlegen und `GITHUB_REPO` in `core/updater.py`
setzen. Der Freund sieht das Update unter *Einstellungen → Updates* und aktualisiert
per Klick — seine Daten bleiben erhalten. Details in [`build/README.md`](build/README.md).

## An Entwickler weitergeben (Code-Kopie)

Eine saubere Kopie **ohne deine Daten** erstellen:

```bat
python make_share_copy.py
:: oder Doppelklick auf "Saubere Kopie erstellen.bat"
```

Das legt `../portfolio_pro_freund` an — mit dem kompletten Code, aber **ohne** `portfolio.db`
und **ohne** `.env`. Der Empfänger führt `pip install -r requirements.txt` aus, startet die App
und durchläuft die Ersteinrichtung mit seinen eigenen Keys.

## Tests

```bat
python -m pytest tests/
```

## Hinweise

- **Kraken-Key** nur mit Berechtigung *Query Funds* anlegen (kein Handel/Withdrawal).
- Kraken liefert nur Bestände — Einstandskurse trägst du unter *Krypto → Position bearbeiten* nach.
- Die KI-Analysen sind fachliche Einschätzungen, **keine Anlageberatung**.
- Gemini aus der V1 entfällt — die Zweitmeinung übernimmt das Agenten-Team.
