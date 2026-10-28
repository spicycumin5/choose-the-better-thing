#Requires -RunAsAdministrator
<#
  Installs FocusGuard: copies the daemon to Program Files and registers a
  scheduled task that runs it as SYSTEM at boot and relaunches it every minute
  if it isn't running. Re-running this script upgrades the code in place.
#>
$ErrorActionPreference = 'Stop'

$TaskName   = 'FocusGuard'
$InstallDir = Join-Path $env:ProgramFiles 'FocusGuard'
$DataDir    = Join-Path $env:ProgramData 'FocusGuard'
$Source     = Join-Path $PSScriptRoot 'focusguard'

# --- Find a real Python interpreter (not the Microsoft Store stub) -----------
$python = $null
try { $python = (& python -c "import sys; print(sys.executable)" 2>$null) } catch {}
if (-not $python -or -not (Test-Path $python)) {
    throw 'Python 3.9+ was not found on PATH. Install it from python.org and try again.'
}
$version = & $python -c "import sys; print(sys.version_info >= (3, 9))"
if ($version -ne 'True') { throw "Python 3.9+ is required ($python is too old)." }
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }
Write-Host "Using Python: $pythonw"

# --- Copy the code somewhere a normal user can't edit -------------------------
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host 'Existing install found: upgrading.'
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like '*FocusGuard\focusguard\daemon.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

New-Item -ItemType Directory -Force (Join-Path $InstallDir 'focusguard') | Out-Null
Copy-Item -Path (Join-Path $Source '*') -Destination (Join-Path $InstallDir 'focusguard') -Recurse -Force -Exclude '__pycache__'

# --- Data directory: SYSTEM and Administrators write, Users read --------------
New-Item -ItemType Directory -Force $DataDir | Out-Null
# SIDs keep this working on non-English Windows: SYSTEM, Administrators, Users.
icacls $DataDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null

# --- Scheduled task ----------------------------------------------------------
$daemon   = Join-Path $InstallDir 'focusguard\daemon.py'
$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "-E -s `"$daemon`"" -WorkingDirectory (Join-Path $InstallDir 'focusguard')
$triggers = @(
    New-ScheduledTaskTrigger -AtStartup
    New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 9999)
)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Principal $principal `
    -Settings $settings -Description 'FocusGuard site blocker' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

# --- Desktop shortcut to the UI ----------------------------------------------
$shortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'FocusGuard.url'
Set-Content -Path $shortcut -Value "[InternetShortcut]`r`nURL=http://127.0.0.1:8765/" -Encoding ASCII

# --- Wait for it to come up --------------------------------------------------
$up = $false
foreach ($i in 1..20) {
    Start-Sleep -Seconds 1
    try { Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/status' -TimeoutSec 2 | Out-Null; $up = $true; break } catch {}
}
if ($up) {
    Write-Host "`nFocusGuard is running. Open http://127.0.0.1:8765 (shortcut on your desktop)." -ForegroundColor Green
    Write-Host 'Restart Chrome/Edge if a blocked site is already open, then check chrome://policy.'
} else {
    Write-Warning "The task was registered but the UI didn't answer. Check $DataDir\daemon.log."
}
