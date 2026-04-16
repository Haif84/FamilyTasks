#Requires -Version 5.1
<#
.SYNOPSIS
  По SSH запрашивает состояние docker compose и логи сервиса бота на VPS.

.DESCRIPTION
  Удобно диагностировать «бот не отвечает»: смотрите Start polling, 401 Unauthorized,
  Conflict getUpdates, перезапуски контейнера.

.PARAMETER VpsHost
  IP или имя VPS.

.PARAMETER User
  Пользователь SSH (по умолчанию root).

.PARAMETER RemotePath
  Абсолютный путь к корню репозитория на сервере (где docker-compose.yml).

.PARAMETER Service
  Имя сервиса в compose (по умолчанию bot).

.PARAMETER Tail
  Сколько последних строк логов вывести (по умолчанию 200). Игнорируется при -Follow с точки зрения лимита истории: используется min(Tail, 500) как стартовый хвост.

.PARAMETER Follow
  Потоковый вывод (как docker compose logs -f). Скрипт не завершится, пока не прервёте Ctrl+C.

.PARAMETER OutFile
  Сохранить весь вывод SSH в локальный файл (UTF-8).

.EXAMPLE
  .\scripts\fetch-vps-logs.ps1 -VpsHost 88.218.123.156 -RemotePath /opt/family-tasks/FamilyTasks

.EXAMPLE
  .\scripts\fetch-vps-logs.ps1 -Host 88.218.123.156 -RemotePath /opt/family-tasks/FamilyTasks -Tail 400 -OutFile .\vps-bot.log
#>
param(
    [Parameter(Mandatory = $true)]
    [Alias("Host")]
    [string]$VpsHost,

    [string]$User = "root",

    [Parameter(Mandatory = $true)]
    [string]$RemotePath,

    [string]$Service = "bot",

    [ValidateRange(1, 100000)]
    [int]$Tail = 200,

    [switch]$Follow,

    [string]$OutFile = ""
)

$ErrorActionPreference = "Stop"

if ($RemotePath.Contains('"')) {
    Write-Error "RemotePath must not contain double quotes."
}
if ($Service.Contains('"') -or $Service -match '[^\w-]') {
    Write-Error "Service name must be a simple compose service id (letters, digits, hyphen)."
}

if ($Follow) {
    $history = [Math]::Min($Tail, 500)
    $remoteCmd = "cd $RemotePath && docker compose ps && echo '----- logs -f (Ctrl+C to stop) -----' && docker compose logs -f --tail=$history $Service"
    Write-Host "SSH: docker compose ps + logs -f (прервите Ctrl+C)..." -ForegroundColor Cyan
}
else {
    $remoteCmd = "cd $RemotePath && docker compose ps && echo '----- logs (last $Tail lines) -----' && docker compose logs --no-color --timestamps --tail=$Tail $Service"
    Write-Host "SSH: docker compose ps + logs --tail=$Tail ..." -ForegroundColor Cyan
}

if ($OutFile -ne "" -and -not $Follow) {
    $out = ssh "${User}@${VpsHost}" $remoteCmd 2>&1
    $exit = $LASTEXITCODE
    $out | ForEach-Object { $_ }
    $dir = Split-Path -Parent $OutFile
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $out | Set-Content -LiteralPath $OutFile -Encoding utf8
    Write-Host "Saved to $OutFile" -ForegroundColor Green
    if ($exit -ne 0) {
        Write-Error "SSH failed (exit $exit)."
    }
}
elseif ($OutFile -ne "" -and $Follow) {
    Write-Error "-OutFile is not supported together with -Follow. Run without -OutFile or redirect: ... | Tee-Object file.log"
}
else {
    ssh "${User}@${VpsHost}" $remoteCmd
    if ($LASTEXITCODE -ne 0) {
        Write-Error "SSH failed (exit $LASTEXITCODE)."
    }
}
