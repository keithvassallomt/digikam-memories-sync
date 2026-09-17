; Inno Setup script for the DigiMem installer.
;
; The installer is not signed, so Windows SmartScreen warns the first people to
; download each release until it has seen enough of them. That is expected.
#define AppName "DigiMem"
#define AppPublisher "Keith Vassallo"
#define AppURL "https://github.com/keithvassallomt/digikam-memories-sync"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7C2F4E10-9B3D-4D6A-9F2E-5A1C8D3B6E41}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Per-user by default, so no administrator prompt is needed to install.
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=DigiMem-{#AppVersion}-Setup
SetupIconFile=..\..\digimem\icons\digimem.ico
UninstallDisplayIcon={app}\digimem.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "autostart"; Description: "Start {#AppName} when I log in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; The whole PyInstaller onedir tree, kept intact.
Source: "..\..\dist\digimem\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\digimem.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\digimem.exe"; Tasks: desktopicon

[Run]
; Let DigiMem register its own login item, so one mechanism owns it.
Filename: "{app}\digimem.exe"; Parameters: "autostart enable"; Tasks: autostart; Flags: runhidden waituntilterminated
Filename: "{app}\digimem.exe"; Description: "Open {#AppName} now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{app}\digimem.exe"; Parameters: "autostart disable"; Flags: runhidden waituntilterminated; RunOnceId: "DigiMemAutostartOff"
