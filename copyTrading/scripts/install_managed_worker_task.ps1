param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$PythonPath = "python"
)

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$pythonCommand = Get-Command -Name $PythonPath -ErrorAction Stop
$resolvedPython = $pythonCommand.Source
$workerScript = Join-Path $resolvedRoot "copyTrading\scripts\run_managed_worker.py"
$workerConfig = Join-Path $resolvedRoot "copyTrading\config\managed-worker.json"
if (-not (Test-Path -LiteralPath $workerScript) -or -not (Test-Path -LiteralPath $workerConfig)) {
    throw "Script atau konfigurasi managed worker tidak ditemukan pada project root."
}

$taskName = "Algentra Managed Copy Worker"
$launcherCommand = "Set-Location -LiteralPath '$resolvedRoot'; & '$resolvedPython' '$workerScript' '$workerConfig'"
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -Command `"$launcherCommand`"" `
    -WorkingDirectory $resolvedRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U `
    -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Menjalankan Algentra managed copier saat Windows startup." `
    -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output "Scheduled task '$taskName' dibuat dan dimulai untuk $resolvedRoot"
