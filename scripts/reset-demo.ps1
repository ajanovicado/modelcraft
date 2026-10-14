[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VirtualPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path -LiteralPath $VirtualPython) { $VirtualPython } else { "python" }
$DemoRoot = Join-Path $ProjectRoot "fixtures\demo"
$StatePath = Join-Path $ProjectRoot ".runtime"

& $PythonExe -m ai_powerbi_analyst.runtime.preparation reset `
    --demo-root $DemoRoot `
    --state-path $StatePath
if ($LASTEXITCODE -ne 0) {
    throw "The recoverable demo reset was blocked. No broad delete fallback will be used."
}

Write-Host "Demo reset complete. The previous workspace and runtime remain under fixtures\demo\backups."
