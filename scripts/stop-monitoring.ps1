[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$tasks = @(
    [PSCustomObject]@{ Name = "market-data"; Marker = "user_strategies\.market_data" },
    [PSCustomObject]@{
        Name = "portfolio-monitor"
        Marker = "examples[/\\]live[/\\]binance[/\\]portfolio_monitor[/\\]monitor\.py"
    },
    [PSCustomObject]@{ Name = "basis-account-monitor"; Marker = "user_strategies\.basis_account_monitor" },
    [PSCustomObject]@{ Name = "portfolio-alert"; Marker = "user_strategies\.portfolio_alert" }
)

foreach ($task in $tasks) {
    $processes = @(Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $task.Marker })
    if ($processes.Count -eq 0) {
        Write-Host "[SKIP] $($task.Name) is not running."
        continue
    }
    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Write-Host "[STOPPED] $($task.Name)"
}
