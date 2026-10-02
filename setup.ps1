# ClipCraft one-shot setup (Windows / PowerShell)
# Usage:  powershell -ExecutionPolicy Bypass -File setup.ps1

$ErrorActionPreference = "Stop"
Write-Host "== ClipCraft setup ==" -ForegroundColor Cyan

# 1. Python
$py = "py"
try { & $py -3 --version | Out-Null } catch {
  Write-Error "Python 3 not found. Install from https://www.python.org/ then re-run."
}

# 2. ffmpeg
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  Write-Host "Installing ffmpeg via winget..." -ForegroundColor Yellow
  winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
} else { Write-Host "ffmpeg already present." }

# 3. Python packages
Write-Host "Installing Python packages..." -ForegroundColor Yellow
& $py -3 -m pip install --upgrade pip
& $py -3 -m pip install -r requirements.txt

# 4. Ollama model for selection
if (Get-Command ollama -ErrorAction SilentlyContinue) {
  $models = (ollama list) -join "`n"
  if ($models -notmatch "llama3.1") {
    Write-Host "Pulling llama3.1:8b (selection model)..." -ForegroundColor Yellow
    ollama pull llama3.1:8b
  } else { Write-Host "Ollama model present." }
} else {
  Write-Warning "Ollama not found. Install from https://ollama.com and run: ollama pull llama3.1:8b"
}

Write-Host "`nRunning doctor..." -ForegroundColor Cyan
& $py -3 -m clipcraft doctor
