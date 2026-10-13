# Builds dist\lazyFinds (PyInstaller) and packaging\Output\lazyFinds-Setup-<version>.exe (Inno Setup 6).
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
uv run pyinstaller packaging\lazyFinds.spec --noconfirm --distpath dist --workpath build
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 isn't installed: winget install JRSoftware.InnoSetup" }
& $iscc /Q packaging\lazyFinds.iss
Get-ChildItem packaging\Output\*.exe | ForEach-Object { "{0}  {1:N0} MB" -f $_.FullName, ($_.Length / 1MB) }
