; Inno Setup Skript für Portfolio Pro.
; Baut eine Setup.exe, die App-Code + gebündelte Python-Laufzeit + Launcher pro
; Nutzer (ohne Admin) nach %LOCALAPPDATA%\Programs\PortfolioPro installiert.
;
; Voraussetzungen VOR dem Kompilieren (siehe build\README.md):
;   1) build\runtime\python\  existiert   (-> fetch_runtime.ps1)
;   2) build\dist\PortfolioPro.exe existiert (-> PyInstaller auf launcher.py)
;
; Kompilieren:  ISCC.exe /DAppVersion=1.0.0 installer.iss
; (AppVersion setzt build.ps1 automatisch aus core\version.py.)

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Portfolio Pro"
#define RepoDir SourcePath + "\.."
#define BuildDir SourcePath

[Setup]
AppId={{6F3B2A10-8C4E-4E2B-9E7A-PORTFOLIOPRO01}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Portfolio Pro
DefaultDirName={localappdata}\Programs\PortfolioPro
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#BuildDir}\Output
OutputBaseFilename=PortfolioPro-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Icon optional: SetupIconFile={#BuildDir}\icon.ico

[Languages]
Name: "de"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "Desktop-Verknüpfung anlegen"; GroupDescription: "Verknüpfungen:"

[Files]
; App-Code -> {app}\app  (persönliche Daten, Tooling und Build-Artefakte ausschließen)
Source: "{#RepoDir}\*"; DestDir: "{app}\app"; Flags: recursesubdirs createallsubdirs ignoreversion; \
  Excludes: "build\*,tests\*,.git\*,.github\*,.claude\*,.impeccable\*,.agents\*,.pytest_cache\*,__pycache__\*,*.pyc,portfolio.db,.env,*.db"
; Gebündelte Python-Laufzeit -> {app}\runtime
Source: "{#BuildDir}\runtime\*"; DestDir: "{app}\runtime"; Flags: recursesubdirs createallsubdirs ignoreversion
; Launcher-EXE -> {app}
Source: "{#BuildDir}\dist\PortfolioPro.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\PortfolioPro.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\PortfolioPro.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{group}\{#AppName} deinstallieren"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\PortfolioPro.exe"; Description: "Portfolio Pro jetzt starten"; Flags: nowait postinstall skipifsilent

; Nutzerdaten liegen in %LOCALAPPDATA%\PortfolioPro und werden bei Deinstallation
; bewusst NICHT entfernt (Positionen, Cash, API-Keys bleiben erhalten).
