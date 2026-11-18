# Munfarid "Saudi helper" on the old PC that stays on all day (see tools/saudi_helper.py).
# Double-click tools\install_saudi_helper.bat once. It:
#   1. installs Python 3.11 if missing (winget), the project's packages in .venv, and the Chromium browser
#      Playwright uses for Jadarat
#   2. asks once for the database link (kept in %USERPROFILE%\.munfarid\database_url, never in the project)
#   3. keeps the PC awake while plugged in (it must stay on 24 hours)
#   4. registers a Windows task: every day at 04:00 for up to one hour (if the PC was off then, it runs
#      as soon as it is on again)
# Remove it any time:  Unregister-ScheduledTask -TaskName 'Munfarid Saudi Helper' -Confirm:$false
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

Write-Host '1/5 Python...' -ForegroundColor Cyan
$py = $null
try { & py -3.11 --version *> $null; if ($LASTEXITCODE -eq 0) { $py = 'py -3.11' } } catch {}
if (-not $py) {
    Write-Host '   Installing Python 3.11 (a few minutes)...'
    try {
        winget install --id Python.Python.3.11 -e --silent --accept-package-agreements --accept-source-agreements
    } catch {
        throw 'This Windows has no winget. Install Python 3.11 from https://www.python.org/downloads/ (tick "Add to PATH"), then run this again.'
    }
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    try { & py -3.11 -m venv .venv } catch { & python -m venv .venv }
}
if (-not (Test-Path $venvPy)) { throw 'Python could not be installed. Install Python 3.11 from python.org, then run this again.' }

Write-Host '2/5 Packages and the browser...' -ForegroundColor Cyan
& $venvPy -m pip install --disable-pip-version-check -q -r requirements-collect.txt 'w3lib<2.3'
if ($LASTEXITCODE -ne 0) { throw 'Installing the packages failed (check the internet connection), then run this again.' }
& $venvPy -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Installing the Chromium browser failed, then run this again.' }

Write-Host '3/5 Database link...' -ForegroundColor Cyan
$secretDir = Join-Path $env:USERPROFILE '.munfarid'
$dbFile = Join-Path $secretDir 'database_url'
if (-not (Test-Path $dbFile)) {
    New-Item -ItemType Directory -Force $secretDir | Out-Null
    $url = Read-Host 'Paste the DATABASE_URL (postgresql://...) and press Enter'
    if (-not $url.StartsWith('postgres')) { throw 'That is not a database link.' }
    [IO.File]::WriteAllText($dbFile, $url.Trim())
}

Write-Host '4/5 Keep the PC awake while plugged in...' -ForegroundColor Cyan
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0

Write-Host '5/5 Daily task...' -ForegroundColor Cyan
$bat = Join-Path $PSScriptRoot 'saudi_helper.bat'
$action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument "--headless cmd.exe /c `"$bat`""
$daily = New-ScheduledTaskTrigger -Daily -At 4:00am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 70) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'Munfarid Saudi Helper' -Action $action -Trigger $daily -Settings $settings `
    -Description 'Munfarid: once a day, fetch the job sites that refuse cloud servers (Jadarat, Tanqeeb, ...) for the cloud' -Force | Out-Null

Write-Host ''
Write-Host 'Done: the Saudi helper runs every day at 04:00 for up to one hour.' -ForegroundColor Green
Write-Host 'Test it now (5 minutes):  .venv\Scripts\python.exe tools\saudi_helper.py --minutes 5'
