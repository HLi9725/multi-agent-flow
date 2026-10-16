# ==============================================================================
# YY-Flow shared canonical installer (Windows PowerShell 5.1+)
# Default source: HLi9725/multi-agent-flow
# Default ref:    main
# This wrapper does not edit user-global Cursor, Codex, or Antigravity settings.
# ==============================================================================
param(
    [Parameter(Position = 0)]
    [string]$Ref = "main",
    [string]$Source = "HLi9725/multi-agent-flow",
    [string]$Dest = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Installer = Join-Path $ScriptDir "_lib\onboarding\global_install.py"
if (-not (Test-Path -LiteralPath $Installer)) {
    Write-Host "[FAILED] missing installer module"
    exit 2
}

$pyArgs = @($Installer, "--source", $Source, "--ref", $Ref)
if ($Dest) {
    $pyArgs += @("--dest", $Dest)
}

& python @pyArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAILED] installer exit $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Host "[SUCCESS] shared canonical install finished"
exit 0
