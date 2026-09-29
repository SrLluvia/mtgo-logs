# Builds the Windows installer: installer\Output\MTGO-Replay-Setup-<version>.exe
# Needs Python 3.10+ and Inno Setup 6 (winget install JRSoftware.InnoSetup).
# pip/PyInstaller log to stderr: check exit codes instead of stopping on stderr output
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

$version = (Select-String -Path "mtgo_replay\__init__.py" -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
Write-Host "Building MTGO Replay $version"

# 1. isolated build environment with PyInstaller
if (-not (Test-Path ".venv-build\Scripts\python.exe")) { py -m venv .venv-build }
& .venv-build\Scripts\python.exe -m pip install --quiet --disable-pip-version-check --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller failed" }

# 2. icon
& .venv-build\Scripts\python.exe installer\make_icon.py installer\icon.ico
if ($LASTEXITCODE -ne 0) { throw "icon failed" }

# 3. the app (one folder: starts faster and trips antivirus less than a single-file exe)
& .venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed `
    --name "MTGO Replay" --icon "$PSScriptRoot\installer\icon.ico" `
    --add-data "$PSScriptRoot\mtgo_replay\viewer;mtgo_replay\viewer" `
    --distpath build\dist --workpath build\work --specpath build `
    --log-level WARN mtgo_replay_app.py 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# 4. the installer
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }
& $iscc "/DAppVersion=$version" installer\mtgo-replay.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Write-Host "Done: installer\Output\MTGO-Replay-Setup-$version.exe"
