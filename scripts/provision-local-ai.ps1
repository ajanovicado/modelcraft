[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "Local Power BI provisioning is Windows-only."
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required before provisioning."
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "Node.js and npm are required before provisioning."
}
$OllamaCommand = Get-Command ollama -ErrorAction SilentlyContinue
$OllamaExecutable = if ($OllamaCommand) {
    $OllamaCommand.Source
} else {
    Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
}
if (-not (Test-Path -LiteralPath $OllamaExecutable -PathType Leaf)) {
    throw "Ollama is required before provisioning. Install it, then rerun this explicit setup."
}

uv sync --python 3.12.12 --extra dev
if ($LASTEXITCODE -ne 0) { throw "Python environment provisioning failed." }

uv tool install --force --prerelease=allow "pbi-cli-tool==3.11.1"
if ($LASTEXITCODE -ne 0) { throw "pbi-cli installation failed." }

npm install --global `
    "@microsoft/powerbi-report-authoring-cli@0.1.4" `
    "@microsoft/powerbi-desktop-bridge-cli@0.1.2"
if ($LASTEXITCODE -ne 0) { throw "Microsoft Power BI CLI installation failed." }

& $OllamaExecutable pull "qwen3.5:4b-q4_K_M"
if ($LASTEXITCODE -ne 0) { throw "Pinned Qwen model download failed." }

uv run python -m ai_powerbi_analyst.cli attest-model
if ($LASTEXITCODE -ne 0) { throw "Pinned model attestation failed." }

uv run python -m ai_powerbi_analyst.cli preflight
if ($LASTEXITCODE -ne 0) { throw "Local runtime preflight failed." }

Write-Host "Local AI and Power BI authoring tools are provisioned. Runtime generation remains loopback-only."
