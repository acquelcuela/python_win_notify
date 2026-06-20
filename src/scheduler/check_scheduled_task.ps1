param(
    [string]$TaskName = "NightlyBatchNotify"
)

$ErrorActionPreference = "Stop"

$ProjectPath = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectPath "logs"
$today = Get-Date -Format "yyyyMMdd"
$taskLog = Join-Path $LogDir "task_runner_$today.log"
$batchLog = Join-Path $LogDir "batch_$today.log"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "Scheduled task not found: $TaskName"
    exit 1
}

$info = Get-ScheduledTaskInfo -TaskName $TaskName

Write-Host "Task name: $TaskName"
Write-Host "State: $($task.State)"
Write-Host "Last run time: $($info.LastRunTime)"
Write-Host "Last task result: $($info.LastTaskResult)"
Write-Host "Next run time: $($info.NextRunTime)"
Write-Host ""

Write-Host "Triggers:"
$task.Triggers | ForEach-Object {
    Write-Host "  StartBoundary: $($_.StartBoundary)"
    Write-Host "  Enabled: $($_.Enabled)"
    if ($_.Repetition) {
        Write-Host "  Repetition interval: $($_.Repetition.Interval)"
        Write-Host "  Repetition duration: $($_.Repetition.Duration)"
    }
}

Write-Host ""
Write-Host "Actions:"
$task.Actions | ForEach-Object {
    Write-Host "  Execute: $($_.Execute)"
    Write-Host "  Arguments: $($_.Arguments)"
    Write-Host "  WorkingDirectory: $($_.WorkingDirectory)"
}

Write-Host ""
Write-Host "Today task runner log: $taskLog"
if (Test-Path -LiteralPath $taskLog) {
    Get-Content -LiteralPath $taskLog -Tail 20
} else {
    Write-Host "  Not created yet."
}

Write-Host ""
Write-Host "Today batch log: $batchLog"
if (Test-Path -LiteralPath $batchLog) {
    Get-Content -LiteralPath $batchLog -Tail 20
} else {
    Write-Host "  Not created yet."
}
