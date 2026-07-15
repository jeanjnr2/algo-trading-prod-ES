param(
    [Parameter(Position = 0)]
    [string]$Algo,

    [Parameter(Position = 1)]
    [string]$Mode
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = if ($env:ALGO_ENV_FILE) { $env:ALGO_ENV_FILE } else { Join-Path $ProjectRoot ".env" }
$ComposeFile = if ($env:ALGO_COMPOSE_FILE) { $env:ALGO_COMPOSE_FILE } else { Join-Path $ProjectRoot "docker-compose.yml" }
$DryRun = ($env:ALGOCTL_DRY_RUN -eq "true")

function Show-Usage {
    Write-Host "Usage:"
    Write-Host "  .\scripts\algoctl-vps.ps1 PROD-ES PAPER"
    Write-Host "  .\scripts\algoctl-vps.ps1 PROD-ES LIVE"
    exit 1
}

if (-not $Algo -or -not $Mode) {
    Show-Usage
}

switch ($Algo.ToUpperInvariant()) {
    "PROD-ES" {
        $Service = "strategy-prod-v8-es"
        $Image = "strategy-prod-v8-es:local"
        $InstanceId = "V8_ES"
    }
    "ES" {
        $Algo = "PROD-ES"
        $Service = "strategy-prod-v8-es"
        $Image = "strategy-prod-v8-es:local"
        $InstanceId = "V8_ES"
    }
    default {
        Write-Host "Unknown algo: $Algo"
        Show-Usage
    }
}

switch ($Mode.ToUpperInvariant()) {
    "PAPER" {
        $Mode = "paper"
        $AccountKey = "TWS_ACCOUNT_PAPER"
    }
    "LIVE" {
        $Mode = "live"
        $AccountKey = "TWS_ACCOUNT_LIVE"
    }
    default {
        Show-Usage
    }
}

if (-not (Test-Path $EnvFile)) {
    New-Item -ItemType File -Path $EnvFile | Out-Null
}

function Get-EnvFileValue {
    param([string]$Key)
    $line = Get-Content $EnvFile | Where-Object { $_ -match "^$([regex]::Escape($Key))=" } | Select-Object -Last 1
    if (-not $line) { return "" }
    return $line.Substring($Key.Length + 1)
}

function Get-WindowsEnvValue {
    param([string]$Key)
    $value = [Environment]::GetEnvironmentVariable($Key, "Process")
    if (-not $value) {
        $value = [Environment]::GetEnvironmentVariable($Key, "User")
    }
    if (-not $value) {
        $value = [Environment]::GetEnvironmentVariable($Key, "Machine")
    }
    return $value
}

function Set-EnvFileValue {
    param(
        [string]$Key,
        [string]$Value
    )
    $lines = @(Get-Content $EnvFile)
    $found = $false
    $newLines = @(foreach ($line in $lines) {
        if ($line -match "^$([regex]::Escape($Key))=") {
            $found = $true
            "$Key=$Value"
        } else {
            $line
        }
    })
    if (-not $found) {
        $newLines += "$Key=$Value"
    }
    Set-Content -Path $EnvFile -Value $newLines -Encoding ASCII
}

function Import-WindowsEnvForCompose {
    param([string]$Key)
    $value = Get-WindowsEnvValue $Key
    if ($value) {
        [Environment]::SetEnvironmentVariable($Key, $value, "Process")
    }
}

$Account = Get-WindowsEnvValue $AccountKey
if (-not $Account) {
    $Account = Get-EnvFileValue $AccountKey
}
if (-not $Account) {
    $Account = Read-Host "Enter $AccountKey"
}

if (-not $Account) {
    throw "$AccountKey cannot be empty"
}

if ($DryRun) {
    Write-Host "DRY_RUN=true"
    Write-Host "Would set ALGO_TRADING_MODE=$Mode"
    Write-Host "Would set TWS_ACCOUNT from $AccountKey"
    Write-Host "Would set ALGO_INSTANCE_ID=$InstanceId"
    Write-Host "Would check image: docker image inspect $Image"
    Write-Host "Would build if missing: docker compose --env-file $EnvFile -f $ComposeFile build $Service"
    Write-Host "Would run: docker compose --env-file $EnvFile -f $ComposeFile up -d --force-recreate $Service"
    exit 0
}

Set-EnvFileValue "ALGO_TRADING_MODE" $Mode
Set-EnvFileValue "TWS_ACCOUNT" $Account
Set-EnvFileValue "ALGO_INSTANCE_ID" $InstanceId
Set-EnvFileValue "IBKR_AUTO_START_GATEWAY" "true"

if (-not (Get-EnvFileValue "IBKR_HOST")) {
    Set-EnvFileValue "IBKR_HOST" "host.docker.internal"
}

Import-WindowsEnvForCompose "DATABENTO_API_KEY"
Import-WindowsEnvForCompose "TWS_USERNAME"
Import-WindowsEnvForCompose "TWS_PASSWORD"
Import-WindowsEnvForCompose "TWS_ACCOUNT_PAPER"
Import-WindowsEnvForCompose "TWS_ACCOUNT_LIVE"
Import-WindowsEnvForCompose "IBKR_ES_INSTRUMENT_ID"
Import-WindowsEnvForCompose "IBKR_CLIENT_ID"

Write-Host "Algo set to $Algo"
Write-Host "Service set to $Service"
Write-Host "Image set to $Image"
Write-Host "Account set from $AccountKey"
Write-Host "Mode set to $Mode"

docker image inspect $Image *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Image not found, building first..."
    docker compose --env-file $EnvFile -f $ComposeFile build $Service
} else {
    Write-Host "Image already exists, skipping build."
}

Write-Host "Starting container..."
docker compose --env-file $EnvFile -f $ComposeFile up -d --force-recreate $Service
Write-Host "Done."
Write-Host "Logs:"
Write-Host "  docker logs -f $Service"
