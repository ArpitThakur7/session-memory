# Install the session-memory Claude Code plugin from this folder (Windows PowerShell 5.1 and 7).
# Usage: powershell -ExecutionPolicy Bypass -File install.ps1 [-Yes] [-NoPillow] [-DryRun] [-Uninstall]
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$NoPillow,
    [switch]$DryRun,
    [switch]$Uninstall
)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Plugin = 'session-memory@session-memory'
$Market = 'session-memory'

function Say([string]$Text) { Write-Host $Text }
function Step([string]$Text) { Write-Host ''; Write-Host "== $Text" }

# Run a claude command; output matching $OkPattern also counts as success.
function Invoke-ClaudeStep([string]$OkPattern, [string[]]$ClaudeArgs) {
    $shown = 'claude ' + ($ClaudeArgs -join ' ')
    if ($DryRun) { Say "Would run: $shown"; return $true }
    Say "Running: $shown"
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out = & claude @ClaudeArgs 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    $text = ($out -join "`n").Trim()
    if ($text) { Say $text }
    if ($code -eq 0) { return $true }
    if ($text -match $OkPattern) { Say 'Already done.'; return $true }
    Say "This step failed (exit $code)."
    return $false
}

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

if ($DryRun) { Say 'Dry run: nothing will be changed.' }

if ($Uninstall) {
    Step 'Uninstalling the plugin'
    if (-not (Test-Command 'claude')) {
        Say 'Claude Code CLI (claude) not found. Run these later:'
        Say "  claude plugin uninstall $Plugin"
        Say "  claude plugin marketplace remove $Market"
        exit 1
    }
    if (-not (Invoke-ClaudeStep '(?i)not installed|not found|no plugin' @('plugin', 'uninstall', $Plugin))) { exit 1 }
    if (-not (Invoke-ClaudeStep '(?i)not found|no marketplace|not configured' @('plugin', 'marketplace', 'remove', $Market))) { exit 1 }
    Say ''
    if ($DryRun) { Say 'Dry run finished.' } else { Say 'Done.' }
    Say 'Memory folders in your projects (.claude\session-memory) were left alone; delete them by hand if you want.'
    Say 'If you added the MCP server, remove it with: claude mcp remove session-memory -s user'
    exit 0
}

Step 'Finding Python 3.10 or newer'
$Py = $null
$PyArgs = @()
foreach ($cand in @(@('py', '-3'), @('python3'), @('python'))) {
    $exe = $cand[0]
    $pre = @($cand | Select-Object -Skip 1)
    if (-not (Test-Command $exe)) { continue }
    & $exe @pre -c 'import sys; sys.exit(sys.version_info < (3, 10))' *> $null
    if ($LASTEXITCODE -eq 0) { $Py = $exe; $PyArgs = $pre; break }
}
if (-not $Py) {
    Say 'No Python 3.10 or newer found. Install it from https://www.python.org (tick "Add python.exe to PATH") and run this again.'
    exit 1
}
$PyShown = (@($Py) + $PyArgs) -join ' '
$info = & $Py @PyArgs -c 'import sys; print(sys.version.split()[0], sys.executable)'
Say "Using ${PyShown}: $info"

Step 'Pillow (optional, for the one-image summary)'
if ($NoPillow) {
    Say 'Skipped (-NoPillow).'
} else {
    & $Py @PyArgs -c 'import PIL' *> $null
    if ($LASTEXITCODE -eq 0) {
        Say 'Pillow is already installed.'
    } elseif ($DryRun) {
        Say "Would ask, then run: $PyShown -m pip install --user pillow"
    } else {
        $answer = 'y'
        if (-not $Yes) { $answer = Read-Host "Install Pillow with `"$PyShown -m pip install --user pillow`"? [Y/n]" }
        if ($answer -eq '' -or $answer -match '^(?i)y(es)?$') {
            & $Py @PyArgs -m pip install --user pillow
            if ($LASTEXITCODE -ne 0) { Say 'Pillow was not installed; everything else still works.' }
        } else {
            Say "Skipped. Install it later with: $PyShown -m pip install --user pillow"
        }
    }
}

Step 'Adding the plugin to Claude Code'
if (Test-Command 'claude') {
    if (-not (Invoke-ClaudeStep '(?i)already' @('plugin', 'marketplace', 'add', $Root))) { exit 1 }
    if (-not (Invoke-ClaudeStep '(?i)already' @('plugin', 'install', $Plugin))) { exit 1 }
} else {
    Say 'Claude Code CLI (claude) not found. After installing Claude Code, run:'
    Say "  claude plugin marketplace add `"$Root`""
    Say "  claude plugin install $Plugin"
}

Step "Checking the setup for $((Get-Location).Path)"
if ($DryRun) {
    Say "Would run: $PyShown -m memgraph.cli doctor (with PYTHONPATH=$Root)"
    exit 0
}
$oldPath = $env:PYTHONPATH
$env:PYTHONPATH = $Root
try {
    & $Py @PyArgs -m memgraph.cli doctor
    $code = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $oldPath
}
if ($code -eq 0) {
    Say ''
    Say 'Next: open Claude Code in your project and use /memory-map.'
}
exit $code
