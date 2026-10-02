# Launch the clipcraft API on :8756. Run from anywhere; it cd's to the repo root.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here

$envFile = Join-Path $here ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim())
        }
    }
}

Set-Location $root
py -3 -m uvicorn integrations.api.app:app --host 0.0.0.0 --port 8756
