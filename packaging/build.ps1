# Builds dist\lazyFinds (PyInstaller), then into packaging\Output:
#   lazyFinds-Setup-<version>.exe          the web installer (~3 MB): downloads the app archive from the GitHub release
#   lazyFinds-<version>-app.7z             that archive (upload it to the release as is)
#   lazyFinds-Setup-<version>-offline.exe  everything inside, for PCs without internet
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 [-ArchiveUrl http://...]   (a URL only for testing)
# Run `uv sync` once first. The build itself never syncs, so a lazyFinds running from .venv doesn't block it.
param([string]$ArchiveUrl = "")
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$version = (Select-String -Path pyproject.toml -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
uv run --no-sync pyinstaller packaging\lazyFinds.spec --noconfirm --distpath dist --workpath build
if ($LASTEXITCODE) { throw "PyInstaller failed" }
$defines = (uv run --no-sync python packaging\make_archive.py $version) -split " "
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6.5 or later isn't installed: winget install JRSoftware.InnoSetup" }
$web = @("/Q", "/DWeb", "/DAppVersion=$version") + $defines
if ($ArchiveUrl) { $web += "/DArchiveUrl=$ArchiveUrl" }
& $iscc @web packaging\lazyFinds.iss
if ($LASTEXITCODE) { throw "the web installer didn't build" }
& $iscc /Q "/DAppVersion=$version" packaging\lazyFinds.iss
if ($LASTEXITCODE) { throw "the offline installer didn't build" }
Get-ChildItem packaging\Output\* -Include *.exe, *.7z | ForEach-Object { "{0}  {1:N1} MB" -f $_.Name, ($_.Length / 1MB) }
