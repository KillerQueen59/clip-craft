# Register the Clip System auto-runner triggers as Windows Scheduled Tasks
# (current user, no admin needed -- uses schtasks + .cmd wrappers so quoting and
# permissions behave). Re-run any time to update; it re-registers with /F.
#
#   powershell -ExecutionPolicy Bypass -File install_tasks.ps1
#   powershell -ExecutionPolicy Bypass -File install_tasks.ps1 -Uninstall
#
param([switch]$Uninstall)

$root  = Split-Path -Parent $MyInvocation.MyCommand.Path
$once  = Join-Path $root "_runner\once.cmd"    # single sweep of every inbox
$watch = "ClipSystem-Watch"                     # every-minute --once sweep
$night = "ClipSystem-Nightly"                   # daily safety-net sweep

if ($Uninstall) {
  cmd /c "schtasks /Delete /TN `"$watch`" /F 2>nul"
  cmd /c "schtasks /Delete /TN `"$night`" /F 2>nul"
  Write-Host "removed $watch and $night"
  return
}

if (-not (Test-Path $once)) { throw "_runner\once.cmd not found next to this script" }

# Near-instant pickup: an --once sweep every minute. No long-running process to
# babysit; the per-project lock in auto_runner.py stops overlapping sweeps.
cmd /c "schtasks /Create /TN `"$watch`" /TR `"\`"$once\`"`" /SC MINUTE /MO 1 /F"

# Safety net: one sweep at 03:00 daily (catches anything the machine missed
# while asleep). /SC MINUTE tasks that don't need admin are used above; DAILY
# likewise needs no admin. (ONLOGON would require elevation -- avoided.)
cmd /c "schtasks /Create /TN `"$night`" /TR `"\`"$once\`"`" /SC DAILY /ST 03:00 /F"

Write-Host ""
Write-Host "Done. Drop a video into projects/<name>/inbox/ -- picked up within ~60s."
Write-Host "Run now:   schtasks /Run /TN ClipSystem-Watch"
Write-Host "Uninstall: powershell -File install_tasks.ps1 -Uninstall"
