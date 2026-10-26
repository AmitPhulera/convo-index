# convo-index installer for Windows (PowerShell).
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/AmitPhulera/convo-index/main/install.ps1 | iex"
#
# Installs uv if it is missing, installs the convo-index CLI as a uv tool straight from
# GitHub, then runs `convo-index up`: config, UI server at logon, daily refresh (both as
# Task Scheduler tasks), index build in the background, and opens the UI.
# Overrides: $env:CONVO_INDEX_REPO, $env:CONVO_INDEX_REF, $env:CONVO_INDEX_SOURCE, $env:CONVO_INDEX_NO_UP
$ErrorActionPreference = "Stop"

$repo = if ($env:CONVO_INDEX_REPO) { $env:CONVO_INDEX_REPO } else { "AmitPhulera/convo-index" }
$ref = if ($env:CONVO_INDEX_REF) { $env:CONVO_INDEX_REF } else { "main" }
$source = if ($env:CONVO_INDEX_SOURCE) { $env:CONVO_INDEX_SOURCE } else { "git+https://github.com/$repo.git@$ref" }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found; installing it from https://astral.sh/uv ..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

Write-Host "installing convo-index from $source ..."
uv tool install --reinstall --quiet $source
if ($LASTEXITCODE -ne 0) { Write-Error "uv tool install failed (exit $LASTEXITCODE)"; exit 1 }
$bin = (uv tool dir --bin).Trim()
if ($LASTEXITCODE -ne 0 -or -not $bin) { Write-Error "could not locate the uv tool bin directory"; exit 1 }
Write-Host ""
Write-Host "installed: $bin\convo-index.exe"
if (-not (($env:Path -split ";") -contains $bin)) {
    Write-Host "note: $bin is not on your PATH; run 'uv tool update-shell' and open a new terminal"
}
if ($env:CONVO_INDEX_NO_UP) {
    Write-Host "next: convo-index up    # config, UI server at logon, daily refresh, index build"
    exit 0
}
Write-Host "first run: config, UI server at logon, daily refresh, index build in the background ..."
& "$bin\convo-index.exe" up
