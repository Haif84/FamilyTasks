#Requires -Version 5.1
<#
.SYNOPSIS
  Ensure repo exists on VPS, upload .env, git pull, docker compose.

.PARAMETER VpsHost
  VPS IP or hostname.

.PARAMETER User
  SSH user (default: root).

.PARAMETER RemotePath
  Absolute path to repo root on VPS (directory with docker-compose.yml).

.PARAMETER RepoUrl
  HTTPS clone URL for first deploy when RemotePath is missing or has no docker-compose.yml.

.EXAMPLE
  .\scripts\deploy-vps.ps1 -VpsHost 88.218.123.156 -RemotePath /opt/family-tasks/FamilyTasks -RepoUrl https://github.com/Haif84/FamilyTasks.git
#>
param(
    [Parameter(Mandatory = $true)]
    [Alias("Host")]
    [string]$VpsHost,

    [string]$User = "root",

    [Parameter(Mandatory = $true)]
    [string]$RemotePath,

    [string]$RepoUrl = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "Подсказка: новый код на VPS попадёт только после git push в origin (ветка main/master)." -ForegroundColor Yellow

if (-not (Test-Path ".env")) {
    Write-Error ".env not found in project root: $root"
}
if (-not (Test-Path "docker-compose.yml")) {
    Write-Error "docker-compose.yml not found in project root."
}

if ($RemotePath.Contains('"')) {
    Write-Error "RemotePath must not contain double quotes."
}
if ($RepoUrl.Contains('"')) {
    Write-Error "RepoUrl must not contain double quotes."
}

$idx = $RemotePath.LastIndexOf('/')
if ($idx -le 0) {
    Write-Error "RemotePath must be an absolute path with a parent directory (e.g. /opt/family-tasks/FamilyTasks)."
}
$remoteParent = $RemotePath.Substring(0, $idx)

# --- 1) Ensure clone exists on server (single argv to ssh via base64) ---
$repoArg = $RepoUrl
$bootstrap = @"
set -e
R="$RemotePath"
P="$remoteParent"
REPO="$repoArg"
if [ ! -f "`$R/docker-compose.yml" ]; then
  if [ -z "`$REPO" ]; then
    echo "Remote folder is missing or empty. Re-run with -RepoUrl https://github.com/USER/REPO.git"
    exit 1
  fi
  mkdir -p "`$P"
  if [ ! -d "`$R" ]; then
    git clone "`$REPO" "`$R"
  else
    echo "Directory exists but docker-compose.yml missing. Fix or remove: `$R"
    exit 1
  fi
fi
"@
$bootstrap = $bootstrap -replace "`r`n", "`n" -replace "`r", "`n"
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($bootstrap))

Write-Host "SSH: ensure project directory exists..."
ssh "${User}@${VpsHost}" "echo $b64 | base64 -d | bash"
if ($LASTEXITCODE -ne 0) {
    Write-Error "SSH bootstrap failed (exit $LASTEXITCODE)."
}

# --- 2) Upload .env ---
$target = "${User}@${VpsHost}:${RemotePath}/.env"
Write-Host "Uploading .env -> $target"
scp ".env" $target
if ($LASTEXITCODE -ne 0) {
    Write-Error "scp .env failed (exit $LASTEXITCODE)."
}

# --- 3) Pull + compose ---
# CACHEBUST: каждый деплой уникален — иначе docker build может остаться полностью CACHED со старым образом.
# APP_VERSION: берём из src/family_tasks_bot/version.py.
$remoteCmd = @"
set -euo pipefail
cd "$RemotePath"
git fetch --all --tags
(git checkout main 2>/dev/null || git checkout master 2>/dev/null || true)
git pull --ff-only
echo '--- git HEAD ---'
git rev-parse HEAD
git log -1 --oneline
APP_VERSION=`$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' src/family_tasks_bot/version.py | head -1)
if [ -z "`$APP_VERSION" ]; then
  echo "Failed to parse APP_VERSION from src/family_tasks_bot/version.py"
  exit 1
fi
echo "--- app version ---"
echo "`$APP_VERSION"
CACHEBUST=`$(date +%s) APP_VERSION="`$APP_VERSION" docker compose build --pull
APP_VERSION="`$APP_VERSION" docker compose up -d --remove-orphans
docker compose ps
"@
$remoteCmd = $remoteCmd -replace "`r`n", "`n" -replace "`r", "`n"
$remoteCmdB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remoteCmd))

Write-Host "SSH: git pull + docker compose..."
ssh "${User}@${VpsHost}" "echo $remoteCmdB64 | base64 -d | bash"
if ($LASTEXITCODE -ne 0) {
    Write-Error "SSH deploy failed (exit $LASTEXITCODE)."
}

Write-Host "Done."
