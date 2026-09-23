# Registers two per-user Windows Scheduled Tasks so the portfolio's prices
# and history stay fresh without ever clicking the dashboard's Refresh /
# Sync buttons:
#
#   PortfolioTracker-Refresh  - Finnhub quotes, every 15 minutes
#   PortfolioTracker-Sync     - Yahoo daily/intraday bars + fundamentals, once
#                               a day at 17:30 (after US market close)
#
# Run this yourself, once, from a normal (non-admin) PowerShell window:
#
#   powershell -ExecutionPolicy Bypass -File setup-scheduled-tasks.ps1
#
# Re-running it is safe - it replaces the same two tasks (-Force) rather than
# duplicating them. Both tasks only run while you're logged in (no stored
# password needed) and write to logs\refresh.log / logs\sync.log. To remove
# them later:
#
#   Unregister-ScheduledTask -TaskName "PortfolioTracker-Refresh" -Confirm:$false
#   Unregister-ScheduledTask -TaskName "PortfolioTracker-Sync" -Confirm:$false

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

$refreshAction = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"$root\scheduled-refresh.cmd`"" -WorkingDirectory $root
# [TimeSpan]::MaxValue overflows Task Scheduler's XML duration field
# (P9999999DT23H59M59S is out of range) - 10 years is effectively
# "indefinitely" for this purpose and is a value it actually accepts.
$refreshTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "PortfolioTracker-Refresh" -Action $refreshAction `
    -Trigger $refreshTrigger -Description "Portfolio Tracker: Finnhub price refresh every 15 min" `
    -Force | Out-Null
Write-Host "Registered PortfolioTracker-Refresh (every 15 minutes)."

$syncAction = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"$root\scheduled-sync.cmd`"" -WorkingDirectory $root
$syncTrigger = New-ScheduledTaskTrigger -Daily -At "17:30"
Register-ScheduledTask -TaskName "PortfolioTracker-Sync" -Action $syncAction `
    -Trigger $syncTrigger -Description "Portfolio Tracker: Yahoo history sync, daily at 17:30" `
    -Force | Out-Null
Write-Host "Registered PortfolioTracker-Sync (daily at 17:30)."

Write-Host "`nDone. Check logs\refresh.log and logs\sync.log after the next run, or trigger one now with:"
Write-Host "  Start-ScheduledTask -TaskName PortfolioTracker-Refresh"
