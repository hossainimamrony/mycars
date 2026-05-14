param(
    [string]$TaskName = "FindMyCars_FullRun_Every5Hours",
    [int]$IntervalHours = 5
)

$ErrorActionPreference = "Stop"

if ($IntervalHours -lt 1) {
    throw "IntervalHours must be 1 or greater."
}

$projectRoot = $PSScriptRoot
$runnerPath = Join-Path $projectRoot "run_find_my_cars.bat"

if (-not (Test-Path -LiteralPath $runnerPath)) {
    throw "Runner file not found: $runnerPath"
}

$startAt = (Get-Date).AddMinutes(1)
$repeatEvery = New-TimeSpan -Hours $IntervalHours
$repeatFor = New-TimeSpan -Days 3650

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument ("/c `"`"{0}`"` --no-pause" -f $runnerPath)
$trigger = New-ScheduledTaskTrigger -Once -At $startAt -RepetitionInterval $repeatEvery -RepetitionDuration $repeatFor
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$description = "Runs find_my_cars_without_ui full run every $IntervalHours hours from $projectRoot."

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description $description -Force | Out-Null

Write-Host "Scheduled task created/updated: $TaskName"
Write-Host "Runner: $runnerPath"
Write-Host "Interval: every $IntervalHours hour(s)"
Write-Host "First run: $($startAt.ToString('yyyy-MM-dd HH:mm:ss'))"

try {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "First run started now."
}
catch {
    Write-Host "Task created, but first run could not be started automatically."
}
