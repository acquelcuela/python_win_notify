param(
    [string]$TaskName = "NightlyBatchNotify"
)

$ErrorActionPreference = "Stop"

$ProjectPath = Split-Path -Parent $PSScriptRoot
$RunBat = Join-Path $ProjectPath "run.bat"
$ConfigFile = Join-Path $ProjectPath "config.json"

if (-not (Test-Path -LiteralPath $RunBat)) {
    throw "run.bat was not found: $RunBat"
}

if (-not (Test-Path -LiteralPath $ConfigFile)) {
    throw "config.json was not found: $ConfigFile"
}

$config = Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
# batch_schedule entries can be a plain "HH:mm" string or an object like
# {"time": "HH:mm", "days": [...]} for entries with a custom day-of-week
# override; only the time portion is needed here for registration/display.
$scheduleTimes = @($config.batch_schedule) | ForEach-Object {
    if ($_ -is [string]) {
        $_.Trim()
    } elseif ($_.PSObject.Properties.Name -contains 'time') {
        "$($_.time)".Trim()
    } else {
        ""
    }
} | Where-Object { $_ }

if (-not $scheduleTimes) {
    throw "config.json batch_schedule does not contain any HH:mm entries."
}

foreach ($scheduleTime in $scheduleTimes) {
    if ($scheduleTime -notmatch "^\d{2}:\d{2}$") {
        throw "Invalid schedule entry: $scheduleTime. Expected HH:mm."
    }

    $parts = $scheduleTime -split ":", 2
    $hour = [int]$parts[0]
    $minute = [int]$parts[1]

    if ($hour -lt 0 -or $hour -gt 23 -or $minute -lt 0 -or $minute -gt 59) {
        throw "Invalid schedule entry: $scheduleTime. Expected HH:mm."
    }
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$startBoundary = (Get-Date).Date.ToString("yyyy-MM-ddTHH:mm:ss")
$userId = "$env:USERDOMAIN\$env:USERNAME"
$escapedUserId = [Security.SecurityElement]::Escape($userId)
$escapedDescription = [Security.SecurityElement]::Escape("Runs NightlyBatchNotify from $ProjectPath")
$escapedArguments = [Security.SecurityElement]::Escape("/d /s /c `"`"$RunBat`"`"")
$escapedWorkingDirectory = [Security.SecurityElement]::Escape($ProjectPath)

$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$escapedDescription</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <Repetition>
        <Interval>PT15M</Interval>
        <Duration>P1D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>$startBoundary</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$escapedUserId</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>$escapedArguments</Arguments>
      <WorkingDirectory>$escapedWorkingDirectory</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

Register-ScheduledTask -TaskName $TaskName -Xml $taskXml -Force | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Project path: $ProjectPath"
Write-Host "Run file: $RunBat"
Write-Host "Task trigger: every 15 minutes, using one daily repeating trigger"
Write-Host "Batch active windows from config.json: $($scheduleTimes -join ', ')"
Write-Host ""
Write-Host "Check:"
Write-Host "  Get-ScheduledTask -TaskName `"$TaskName`""
