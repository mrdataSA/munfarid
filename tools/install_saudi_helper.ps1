# Munfarid "Saudi helper" on Raed's PC that stays on all day (see tools/saudi_helper.py).
# Double-click tools\install_saudi_helper.bat once (from the munfarid folder). It:
#   1. installs Python 3.11 if missing (winget), the project's packages in .venv, and the Chromium browser
#      Playwright uses for Jadarat
#   2. checks the helper key (%USERPROFILE%\.munfarid\helper_key; the cloud keeps only its hash).
#      No database password is needed: the helper talks to the site with this key
#   3. keeps the PC awake while plugged in (it must stay on 24 hours)
#   4. registers two Windows tasks (replacing older ones of the same names):
#      - "Munfarid Saudi Helper": every day at 04:00 RIYADH time (whatever this PC's clock zone is) for up
#        to one hour; if the PC was off then, it runs as soon as it is on again
#      - "Munfarid LinkedIn": every 30 minutes, LinkedIn only (Raed holds its written permission)
#   5. checks the connection to the site
# Remove them any time:  Unregister-ScheduledTask -TaskName 'Munfarid Saudi Helper' -Confirm:$false
#                        Unregister-ScheduledTask -TaskName 'Munfarid LinkedIn' -Confirm:$false
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$taskName = 'Munfarid Saudi Helper'
$liTaskName = 'Munfarid LinkedIn'

Write-Host '1/5 Python...' -ForegroundColor Cyan
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    $havepy = $false
    try { & py -3.11 --version *> $null; $havepy = ($LASTEXITCODE -eq 0) } catch {}
    if (-not $havepy) {
        Write-Host '   Installing Python 3.11 (a few minutes)...'
        try {
            winget install --id Python.Python.3.11 -e --silent --accept-package-agreements --accept-source-agreements
        } catch {
            throw 'This Windows has no winget. Install Python 3.11 from https://www.python.org/downloads/ (tick "Add to PATH"), then run this again.'
        }
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    }
    try { & py -3.11 -m venv .venv } catch { & python -m venv .venv }
}
if (-not (Test-Path $venvPy)) { throw 'Python could not be installed. Install Python 3.11 from python.org, then run this again.' }
$venvPyw = Join-Path $root '.venv\Scripts\pythonw.exe'

Write-Host '2/5 Packages and the browser...' -ForegroundColor Cyan
& $venvPy -m pip install --disable-pip-version-check -q -r requirements-collect.txt
if ($LASTEXITCODE -ne 0) { throw 'Installing the packages failed (check the internet connection), then run this again.' }
& $venvPy -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Installing the Chromium browser failed, then run this again.' }

Write-Host '3/5 Helper key...' -ForegroundColor Cyan
$secretDir = Join-Path $env:USERPROFILE '.munfarid'
New-Item -ItemType Directory -Force $secretDir | Out-Null
$keyFile = Join-Path $secretDir 'helper_key'
$dbFile = Join-Path $secretDir 'database_url'
if (-not (Test-Path $keyFile) -and -not (Test-Path $dbFile)) {
    $key = Read-Host 'Paste the helper key (from the Munfarid team) and press Enter'
    if ($key.Trim().Length -lt 24) { throw 'That is not a helper key.' }
    [IO.File]::WriteAllText($keyFile, $key.Trim())
}

Write-Host '4/5 Keep the PC awake + the tasks...' -ForegroundColor Cyan
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
# pythonw = no window at all; the script finds its key and logs (logs\saudi_helper.log) by itself
$action = New-ScheduledTaskAction -Execute $venvPyw -Argument 'tools\saudi_helper.py' -WorkingDirectory $root
# 04:00 in Riyadh, written in this PC's own clock time (Saudi Arabia has no daylight saving)
$riyadh = [TimeZoneInfo]::FindSystemTimeZoneById('Arab Standard Time')
$at = [TimeZoneInfo]::ConvertTime([DateTime]::SpecifyKind([DateTime]::Today.AddHours(4), 'Unspecified'), $riyadh, [TimeZoneInfo]::Local)
$daily = New-ScheduledTaskTrigger -Daily -At $at
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 70) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $daily -Settings $settings `
    -Description 'Munfarid: once a day (04:00 Riyadh), fetch the job sites that refuse cloud servers (Jadarat, Tanqeeb, ...) for the cloud' -Force | Out-Null
$liAction = New-ScheduledTaskAction -Execute $venvPyw -Argument 'tools\saudi_helper.py --only linkedin --minutes 25' -WorkingDirectory $root
$every30 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$liSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 28) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $liTaskName -Action $liAction -Trigger $every30 -Settings $liSettings `
    -Description 'Munfarid: every 30 minutes, every new LinkedIn job in Saudi Arabia (polite pace, pauses on any block)' -Force | Out-Null

Write-Host '5/5 Connection check...' -ForegroundColor Cyan
& $venvPy tools\saudi_helper.py --check
if ($LASTEXITCODE -ne 0) { throw 'The helper could not reach the site with its key. Check the internet connection, then run this again.' }

Write-Host ''
Write-Host ("Done: the Saudi helper runs every day at 04:00 Riyadh time (" + $at.ToString('HH:mm') + " on this PC's clock), and LinkedIn every 30 minutes (no window).") -ForegroundColor Green
Write-Host "Run it now once:  Start-ScheduledTask -TaskName '$taskName'   (the dashboard shows the result)"
