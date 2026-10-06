[CmdletBinding()]
param(
    [string]$EnvFile,
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) {
    $EnvFile = Join-Path $repoRoot ".env"
}
$EnvFile = [System.IO.Path]::GetFullPath($EnvFile)
$pythonProject = Join-Path $repoRoot "python"
$logDirectory = Join-Path $repoRoot "logs\monitoring"
$envFileArgument = if ((Split-Path -Parent $EnvFile) -eq $repoRoot) {
    ".env"
} else {
    $EnvFile
}

function Read-DotEnv([string]$Path) {
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $parts = $trimmed.Split("=", 2)
        if ($parts.Count -ne 2) {
            continue
        }
        $name = $parts[0].Trim()
        $value = $parts[1].Trim()
        if ($value.Length -ge 2 -and (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))
        )) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$name] = $value
    }
    return $values
}

function Test-Configured($Values, [string]$Name) {
    return $Values.ContainsKey($Name) -and
        -not [string]::IsNullOrWhiteSpace([string]$Values[$Name])
}

function Test-TaskRunning([string]$Marker) {
    $match = Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $Marker } |
        Select-Object -First 1
    return $null -ne $match
}

function Join-Arguments([string[]]$Arguments) {
    return ($Arguments | ForEach-Object {
        if ($_ -match '[\s"]') {
            '"' + $_.Replace('"', '\"') + '"'
        } else {
            $_
        }
    }) -join " "
}

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Environment file not found: $EnvFile"
}
if (-not (Test-Path -LiteralPath $pythonProject -PathType Container)) {
    throw "Python project not found: $pythonProject"
}
$uvPath = (Get-Command uv -ErrorAction Stop).Source
$envValues = Read-DotEnv $EnvFile
$missing = @()
foreach ($name in @(
    "BINANCE_API_KEY", "BINANCE_API_SECRET",
    "POSTGRES_USERNAME", "POSTGRES_DATABASE", "FEISHU_APP_ID", "FEISHU_APP_SECRET"
)) {
    if (-not (Test-Configured $envValues $name)) {
        $missing += $name
    }
}
if (-not (Test-Configured $envValues "BASIS_SPOT_PROFILE") -or $envValues["BASIS_SPOT_PROFILE"] -eq "gate_spot") {
    foreach ($name in @("GATE_API_KEY", "GATE_API_SECRET")) {
        if (-not (Test-Configured $envValues $name)) { $missing += $name }
    }
}
if (-not $envValues.ContainsKey("POSTGRES_PASSWORD")) {
    $missing += "POSTGRES_PASSWORD"
}
$receiverConfigured =
    (Test-Configured $envValues "FEISHU_RECEIVER_OPEN_ID") -or
    (Test-Configured $envValues "FEISHU_RECEIVER_MOBILE") -or
    (Test-Configured $envValues "FEISHU_RECEIVER_EMAIL")
if (-not $receiverConfigured) {
    $missing += "one FEISHU_RECEIVER_OPEN_ID/MOBILE/EMAIL"
}
if ($missing.Count -gt 0) {
    throw "Missing required .env settings: $($missing -join ', ')"
}

$tasks = @(
    [PSCustomObject]@{
        Name = "market-data"
        Marker = "user_strategies\.market_data"
        Arguments = @(
            "run", "--project", "python", "--no-sync",
            "--env-file", $envFileArgument,
            "python", "-m", "user_strategies.market_data"
        )
    },
    [PSCustomObject]@{
        Name = "portfolio-monitor"
        Marker = "examples[/\\]live[/\\]binance[/\\]portfolio_monitor[/\\]monitor\.py"
        Arguments = @(
            "run", "--project", "python", "--no-sync", "--env-file", $envFileArgument,
            "python", "examples/live/binance/portfolio_monitor/monitor.py"
        )
    },
    [PSCustomObject]@{
        Name = "basis-account-monitor"
        Marker = "user_strategies\.basis_account_monitor"
        Arguments = @(
            "run", "--project", "python", "--no-sync",

            "--env-file", $envFileArgument,
            "python", "-m", "user_strategies.basis_account_monitor"
        )
    },
    [PSCustomObject]@{
        Name = "portfolio-alert"
        Marker = "user_strategies\.portfolio_alert"
        Arguments = @(
            "run", "--project", "python", "--no-sync",

            "--env-file", $envFileArgument,
            "python", "-m", "user_strategies.portfolio_alert"
        )
    }
)

if ($ValidateOnly) {
    Write-Host "Configuration valid. Task status:"
    foreach ($task in $tasks) {
        $status = if (Test-TaskRunning $task.Marker) { "RUNNING (would skip)" } else { "STOPPED (would start)" }
        Write-Host "  $($task.Name): $status"
    }
    exit 0
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$failed = $false
foreach ($task in $tasks) {
    if (Test-TaskRunning $task.Marker) {
        Write-Host "[SKIP] $($task.Name) is already running."
        continue
    }
    $stdout = Join-Path $logDirectory "$($task.Name).out.log"
    $stderr = Join-Path $logDirectory "$($task.Name).err.log"
    $startParameters = @{
        FilePath = $uvPath
        ArgumentList = Join-Arguments $task.Arguments
        WorkingDirectory = $repoRoot
        WindowStyle = "Hidden"
        RedirectStandardOutput = $stdout
        RedirectStandardError = $stderr
        PassThru = $true
    }
    $process = Start-Process @startParameters
    Start-Sleep -Milliseconds 1000
    if ($process.HasExited) {
        Write-Warning "[FAILED] $($task.Name) exited during startup. See $stderr"
        $failed = $true
    } else {
        Write-Host "[STARTED] $($task.Name) (PID $($process.Id))"
    }
}

Write-Host "Logs: $logDirectory"
if ($failed) {
    exit 1
}
