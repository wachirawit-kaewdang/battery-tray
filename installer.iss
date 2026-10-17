; Inno Setup script for Battery Tray. Built by release.ps1, which passes /DAppVersion.
; Per-user install: no admin prompt, lives in %LOCALAPPDATA%\Programs\BatteryTray.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppUrl "https://github.com/wachirawit-kaewdang/battery-tray"

[Setup]
AppId={{8F3B2C1E-6D4A-4E7B-9C1F-2A5D7E9B3C40}
AppName=Battery Tray
AppVersion={#AppVersion}
AppPublisher=Wachirawit Kaewdaeng
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppUpdatesURL={#AppUrl}/releases
DefaultDirName={localappdata}\Programs\BatteryTray
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=release
OutputBaseFilename=BatteryTray-{#AppVersion}-setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\BatteryTray.exe
UninstallDisplayName=Battery Tray
LicenseFile=LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "autostart"; Description: "Start Battery Tray when I sign in to Windows"

[Files]
Source: "dist\BatteryTray\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Battery Tray"; Filename: "{app}\BatteryTray.exe"

[Registry]
; Same value the app's own "Start with Windows" menu item writes.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "BatteryTray"; ValueData: """{app}\BatteryTray.exe"""; Tasks: autostart
; Always remove it on uninstall, even if it was turned on from the tray menu.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "BatteryTray"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\BatteryTray.exe"; Description: "Launch Battery Tray now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM BatteryTray.exe /F"; Flags: runhidden; RunOnceId: "StopBatteryTray"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\BatteryTray"
