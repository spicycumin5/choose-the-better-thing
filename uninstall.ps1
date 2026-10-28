#Requires -RunAsAdministrator
<#
  Removes FocusGuard. Only works once an uninstall requested from the UI has
  waited out its delay (Settings -> Request uninstall).
#>
$ErrorActionPreference = 'Stop'

$TaskName   = 'FocusGuard'
$InstallDir = Join-Path $env:ProgramFiles 'FocusGuard'
$DataDir    = Join-Path $env:ProgramData 'FocusGuard'
$StateFile  = Join-Path $DataDir 'state.json'

$approved = $false
if (Test-Path $StateFile) {
    $approved = [bool](Get-Content $StateFile -Raw | ConvertFrom-Json).uninstall_approved
}
if (-not $approved) {
    Write-Host 'Uninstall has not been approved yet.' -ForegroundColor Yellow
    Write-Host 'Open http://127.0.0.1:8765, go to Settings and click "Request uninstall".'
    Write-Host 'Once the delay has passed, run this script again.'
    exit 1
}

# --- Stop the daemon ---------------------------------------------------------
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like '*FocusGuard\focusguard\daemon.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# --- Remove the hosts block and browser policies (the daemon should already
#     have done this once uninstall was approved; this is a safety net) --------
$hosts = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
$inside = $false
$kept = foreach ($line in [IO.File]::ReadAllLines($hosts)) {
    if ($line.Trim().StartsWith('# FOCUSGUARD BEGIN')) { $inside = $true; continue }
    if ($inside) { if ($line.Trim().StartsWith('# FOCUSGUARD END')) { $inside = $false }; continue }
    $line
}
[IO.File]::WriteAllLines($hosts, [string[]]$kept)

foreach ($key in 'HKLM:\SOFTWARE\Policies\Google\Chrome\URLBlocklist',
                 'HKLM:\SOFTWARE\Policies\Microsoft\Edge\URLBlocklist',
                 'HKLM:\SOFTWARE\Policies\BraveSoftware\Brave\URLBlocklist') {
    if (Test-Path $key) { Remove-Item $key -Recurse -Force }
}
ipconfig /flushdns | Out-Null

# --- Keep a copy of the unlock history, then remove files ---------------------
$desktop = [Environment]::GetFolderPath('Desktop')
$history = Join-Path $DataDir 'unlocks.jsonl'
if (Test-Path $history) {
    Copy-Item $history (Join-Path $desktop 'FocusGuard-history.jsonl') -Force
    Write-Host "Saved your unlock history to $desktop\FocusGuard-history.jsonl"
}
Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $DataDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $desktop 'FocusGuard.url') -Force -ErrorAction SilentlyContinue

Write-Host 'FocusGuard has been removed.' -ForegroundColor Green
