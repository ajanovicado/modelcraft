[CmdletBinding()]
param(
    [string]$DemoRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VirtualPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path -LiteralPath $VirtualPython) { $VirtualPython } else { "python" }
if (-not $DemoRoot) { $DemoRoot = Join-Path $ProjectRoot "fixtures\demo" }

& $PythonExe -c "import ai_powerbi_analyst" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "The local project environment is not installed. Preparation will not install or download anything."
}

$PreparationArguments = @(
    "-m", "ai_powerbi_analyst.runtime.preparation",
    "prepare",
    "--repository-root", $ProjectRoot,
    "--demo-root", $DemoRoot
)

& $PythonExe @PreparationArguments
if ($LASTEXITCODE -ne 0) {
    throw "Windows demo preparation failed closed. Existing live workspaces are never overwritten."
}
