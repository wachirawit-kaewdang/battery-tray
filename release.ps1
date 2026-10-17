# Builds everything that goes on a GitHub release, into .\release:
#   BatteryTray-<ver>-setup.exe      installer (per-user, no admin)
#   BatteryTray-<ver>-portable.zip   unzip and run BatteryTray.exe
#   SHA256SUMS.txt
# Needs: pip install -r requirements.txt pyinstaller, and Inno Setup 6.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$version = (Select-String -Path battery_tray.pyw -Pattern '^APP_VERSION = "([^"]+)"').Matches[0].Groups[1].Value
if (-not $version) { throw "APP_VERSION not found in battery_tray.pyw" }
$parts = ($version.Split(".") + @("0", "0", "0"))[0..3] -join ", "
Write-Host "Building Battery Tray $version"

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }

# Windows version resource, so Properties > Details shows a name and version.
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($parts), prodvers=($parts), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Wachirawit Kaewdaeng'),
      StringStruct('FileDescription', 'Battery Tray'),
      StringStruct('FileVersion', '$version'),
      StringStruct('InternalName', 'BatteryTray'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'BatteryTray.exe'),
      StringStruct('ProductName', 'Battery Tray'),
      StringStruct('ProductVersion', '$version')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@ | Set-Content -Encoding utf8 version_info.txt

python make_icon.py
if ($LASTEXITCODE) { throw "make_icon failed" }

# Folder build rather than --onefile: starts faster and trips antivirus heuristics far less.
python -m PyInstaller --noconfirm --clean --onedir --windowed --name BatteryTray `
    --icon app.ico --version-file version_info.txt battery_tray.pyw
if ($LASTEXITCODE) { throw "PyInstaller failed" }

New-Item -ItemType Directory -Force release | Out-Null
Get-ChildItem release -Filter "BatteryTray-$version-*" | Remove-Item -Force

& $iscc /Q "/DAppVersion=$version" installer.iss
if ($LASTEXITCODE) { throw "Inno Setup failed" }

Compress-Archive -Path dist\BatteryTray -DestinationPath "release\BatteryTray-$version-portable.zip" -Force

Get-ChildItem release -Filter "BatteryTray-$version-*" | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.Name
} | Set-Content -Encoding ascii release\SHA256SUMS.txt

Remove-Item -Recurse -Force build, version_info.txt -ErrorAction SilentlyContinue
Write-Host ""
Get-Content release\SHA256SUMS.txt
