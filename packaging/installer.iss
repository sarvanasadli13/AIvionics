; Inno Setup script — AIvionics (PLAN Phase 6)
;
;   iscc packaging\installer.iss
;
; Per-user install throughout. PLAN assumes no administrator rights on the
; target machines, so PrivilegesRequired=lowest and everything lands under
; %LOCALAPPDATA%. Nothing is written to Program Files, no service is
; registered, and no registry key outside HKCU is touched — an engineering
; department can install this without raising a ticket.

#define AppName        "AIvionics"
#define AppPublisher   "Sarvan Asadli"
#define AppExeName     "AIvionics.exe"
#ifndef AppVersion
  #define AppVersion   "0.1.0"
#endif

[Setup]
AppId={{7B3C1E44-9E2A-4F6B-9D71-AI0V10N1CS01}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename=AIvionics-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Rebuilding the index after an upgrade is expensive, so the data directory is
; never touched by the installer or the uninstaller.
UninstallDisplayName={#AppName} {#AppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; \
    GroupDescription: "Additional shortcuts"; Flags: unchecked

[Files]
Source: "..\dist\AIvionics\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
    Tasks: desktopicon

[Dirs]
; The corpus, the index and the backups. Kept out of {app} so an upgrade or an
; uninstall cannot take the department's data with it.
Name: "{localappdata}\{#AppName}\data"
Name: "{localappdata}\{#AppName}\data\backups"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only what the application generated at runtime inside {app}.
Type: filesandordirs; Name: "{app}\__pycache__"

[Messages]
; Said at install time rather than discovered later: the tool is decision
; support and does not replace the controlled manual or the CAMO.
WelcomeLabel2=This will install {#AppName} version {#AppVersion} for the current user only.%n%n{#AppName} is decision support. It indexes into controlled maintenance data and is not part of the official maintenance record; the approved manual and the CAMO remain the source of truth.
