#Requires -Version 5.1
<#
.SYNOPSIS
  Init git if needed, set local author from GitHub, commit, create repo via gh, push.

.PARAMETER RepoName
  GitHub repository name (default: FamilyTasks).

.PARAMETER Visibility
  public or private.
#>
param(
    [string]$RepoName = "FamilyTasks",
    [ValidateSet("public", "private")]
    [string]$Visibility = "public"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $root "pyproject.toml"))) {
    Write-Error "Run this script from the FamilyTasks repo root (next to pyproject.toml)."
}

Set-Location $root

function Require-Cmd($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        Write-Error "Command not found: '$name'. Install Git and GitHub CLI and add them to PATH."
    }
}

function Ensure-GhAuth {
    gh auth status 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Run: gh auth login"
        gh auth login
    }
}

function Ensure-GitAuthorLocal {
    $email = git config --get user.email 2>$null
    $name = git config --get user.name 2>$null
    if ($email -and $name) {
        return
    }
    $login = gh api user --jq .login
    $id = gh api user --jq .id
    if (-not $login -or -not $id) {
        Write-Error "Could not read GitHub user via gh api. Run: gh auth login"
    }
    git config user.email "${id}+${login}@users.noreply.github.com"
    git config user.name $login
    Write-Host "Set local git identity: $login <$(git config --get user.email)>"
}

Require-Cmd git
Require-Cmd gh
Ensure-GhAuth

if (-not (Test-Path ".git")) {
    git init
    git branch -M main
}

Ensure-GitAuthorLocal

git add -A
$status = git status --porcelain
if ($status) {
    git commit -m "Initial commit: Family Tasks bot (MVP + Docker + CI/CD)"
} else {
    Write-Host "Nothing to commit."
}

$login = gh api user --jq .login
$repoUrl = "https://github.com/$login/$RepoName.git"

$hasOrigin = $false
git remote get-url origin 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    $hasOrigin = $true
}

if (-not $hasOrigin) {
    gh repo create $RepoName --$Visibility --source=. --remote=origin --description "Telegram bot: family household tasks (aiogram + SQLite)" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "gh repo create failed (repo may already exist). Configuring remote origin."
        git remote add origin $repoUrl 2>$null
        if ($LASTEXITCODE -ne 0) {
            git remote set-url origin $repoUrl
        }
    }
}

$head = git rev-parse HEAD 2>$null
if (-not $head) {
    Write-Error "No commit to push. Fix any errors above and run again."
}

git push -u origin main
Write-Host ("Done. Repository: https://github.com/{0}/{1}" -f $login, $RepoName)
