# Creates a "Clip System" shortcut on the Desktop that launches the app with a
# real icon and no console window (pythonw). Run once:
#   powershell -ExecutionPolicy Bypass -File gui\install-shortcut.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot          # project root (parent of gui\)
$appPy = Join-Path $root "gui\app.py"
$icon  = Join-Path $root "gui\assets\icon.ico"

# Prefer pythonw.exe (no console) next to the active python; fall back to pyw.
$pythonExe = (& py -3 -c "import sys; print(sys.executable)").Trim()
$pythonw = Join-Path (Split-Path $pythonExe) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = "pyw" }   # py-launcher windowless

$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "Clip System.lnk"

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $pythonw
$lnk.Arguments = "`"$appPy`""
$lnk.WorkingDirectory = $root
$lnk.IconLocation = $icon
$lnk.Description = "Clip System — clipcraft desktop"
$lnk.WindowStyle = 1
$lnk.Save()

Write-Host "Shortcut created: $lnkPath"
Write-Host "Target: $pythonw `"$appPy`""
