# Installs the Munfarid "Saudi helper" task (every 2 hours while this PC is on, no window).
# Remove it any time:  Unregister-ScheduledTask -TaskName 'Munfarid Saudi Helper' -Confirm:$false
$bat = Join-Path $PSScriptRoot 'saudi_helper.bat'
$action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument "--headless cmd.exe /c `"$bat`""
$every2h = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 2)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 45) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'Munfarid Saudi Helper' -Action $action -Trigger @($every2h, $logon) -Settings $settings `
    -Description 'Munfarid: fetch Jadarat and Tanqeeb from this home connection for the cloud' -Force | Out-Null
Write-Host ''
Write-Host 'Done: the Saudi helper now runs every 2 hours while this PC is on.' -ForegroundColor Green
