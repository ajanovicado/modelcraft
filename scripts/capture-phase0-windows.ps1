[CmdletBinding()]
param(
    [string]$DemoRoot = "",
    [string]$PbiExecutable = "",
    [string]$AuthoringExecutable = "",
    [string]$DesktopExecutable = "",
    [string]$DesktopVersion = "",
    [string]$WindowsBuild = "",
    [string]$NodeVersion = "24.0.0",
    [string]$OllamaVersion = "0.12.0",
    [string]$ModelTag = "",
    [string]$ModelDigest = "",
    [switch]$SealCache,
    [switch]$CaptureEnvironment,
    [switch]$CaptureModelEvaluation,
    [switch]$CaptureStrictOffline,
    [switch]$CaptureValidators,
    [switch]$RunKillTest,
    [switch]$CaptureColdRender
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "Phase-0 capture is Windows-only. No receipt was written."
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VirtualPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path -LiteralPath $VirtualPython) { $VirtualPython } else { "python" }
if (-not $DemoRoot) { $DemoRoot = Join-Path $ProjectRoot "fixtures\demo" }
if (-not $PbiExecutable) {
    $PbiExecutable = (Get-Command pbi -ErrorAction Stop).Source
}
if (-not $AuthoringExecutable) {
    $AuthoringExecutable = (Get-Command powerbi-report-author -ErrorAction Stop).Source
}

& $PythonExe -c "import ai_powerbi_analyst" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "The local project environment is not installed; capture will not download anything."
}

if ($CaptureEnvironment) {
    if (-not $DesktopExecutable -or -not $DesktopVersion -or -not $WindowsBuild -or `
        -not $ModelTag -or -not $ModelDigest) {
        throw "-CaptureEnvironment requires Desktop executable/version, Windows build, model tag, and full model digest."
    }
    $NodeExecutable = (Get-Command node -ErrorAction Stop).Source
    $OllamaExecutable = (Get-Command ollama -ErrorAction Stop).Source
    & $PythonExe -m ai_powerbi_analyst.runtime.phase0_capture capture-environment `
        --demo-root $DemoRoot `
        --pbi-executable $PbiExecutable `
        --authoring-executable $AuthoringExecutable `
        --desktop-executable $DesktopExecutable `
        --desktop-version $DesktopVersion `
        --windows-build $WindowsBuild `
        --node-executable $NodeExecutable `
        --node-version $NodeVersion `
        --ollama-executable $OllamaExecutable `
        --ollama-version $OllamaVersion `
        --model-tag $ModelTag `
        --model-digest $ModelDigest
    if ($LASTEXITCODE -ne 0) {
        throw "Environment identity capture failed. No passed environment receipt was written."
    }
    Write-Host "Windows, Desktop, tool, and installed-model identities were captured from actual probes."
}

if ($CaptureModelEvaluation) {
    if (-not $ModelTag -or -not $ModelDigest) {
        throw "-CaptureModelEvaluation requires model tag and full model digest."
    }
    & $PythonExe -m ai_powerbi_analyst.runtime.phase0_capture capture-model-evaluation `
        --repository-root $ProjectRoot `
        --demo-root $DemoRoot `
        --model-tag $ModelTag `
        --model-digest $ModelDigest
    if ($LASTEXITCODE -ne 0) {
        throw "The exact 64-case by three-repetition model evaluation failed."
    }
    Write-Host "The model receipt was generated from three actual frozen-corpus evaluator commands."
}

if ($CaptureStrictOffline) {
    & $PythonExe -m ai_powerbi_analyst.runtime.phase0_capture capture-strict-offline `
        --repository-root $ProjectRoot `
        --demo-root $DemoRoot
    if ($LASTEXITCODE -ne 0) {
        throw "The repository-owned strict-offline flow/schema capture failed."
    }
    Write-Host "Strict-offline receipt captured with physical adapters disabled."
}

if ($SealCache) {
    & $PythonExe -m ai_powerbi_analyst.runtime.phase0_capture seal-cache `
        --repository-root $ProjectRoot `
        --demo-root $DemoRoot
    if ($LASTEXITCODE -ne 0) {
        throw "The imported-data cache could not be sealed. Phase 0 remains blocked."
    }
}

$CanonicalValidatorRoot = Join-Path $DemoRoot "evidence\phase0\baseline-validators"
$CanonicalPbiReceipt = Join-Path $CanonicalValidatorRoot "pbi_cli_validation.json"
$CanonicalMicrosoftReceipt = Join-Path $CanonicalValidatorRoot "microsoft_validation.json"
$NeedsValidatorPair = $CaptureValidators -or $RunKillTest -or $CaptureColdRender
if ($CaptureValidators) {
    & $PythonExe -m ai_powerbi_analyst.runtime.phase0_capture capture-validators `
        --repository-root $ProjectRoot `
        --demo-root $DemoRoot `
        --pbi-executable $PbiExecutable `
        --authoring-executable $AuthoringExecutable `
        --pbi-cli-version 3.11.1 `
        --authoring-cli-version 0.1.4
    if ($LASTEXITCODE -ne 0) {
        throw "External validator capture failed. Failed receipts, if any, were retained for diagnosis."
    }
    Write-Host "Canonical validator receipts were captured from one successful command pair."
} elseif ($NeedsValidatorPair -and `
          (Test-Path -LiteralPath $CanonicalPbiReceipt -PathType Leaf) -and `
          (Test-Path -LiteralPath $CanonicalMicrosoftReceipt -PathType Leaf)) {
    Write-Host "Reusing the existing canonical passed validator pair."
} elseif ($NeedsValidatorPair) {
    throw "The canonical validator pair is missing. Run once with -CaptureValidators."
}
if ($RunKillTest) {
    if (-not $DesktopExecutable -or -not $DesktopVersion) {
        throw "-RunKillTest requires exact -DesktopExecutable and -DesktopVersion values."
    }
    $DesktopCaptureScript = Join-Path $PSScriptRoot "capture-powerbi-desktop-reopen.ps1"
    & $PythonExe -m ai_powerbi_analyst.runtime.phase0_capture run-kill-test `
        --repository-root $ProjectRoot `
        --demo-root $DemoRoot `
        --pbi-executable $PbiExecutable `
        --authoring-executable $AuthoringExecutable `
        --desktop-executable $DesktopExecutable `
        --desktop-version $DesktopVersion `
        --capture-script $DesktopCaptureScript `
        --pbi-cli-version 3.11.1 `
        --authoring-cli-version 0.1.4
    if ($LASTEXITCODE -ne 0) {
        throw "The ten-run mutation/Desktop/rollback gate failed. Phase 0 remains blocked."
    }
    Write-Host "Ten linked mutation, validator, Desktop screenshot, and rollback runs passed."
}
if ($CaptureColdRender) {
    if (-not $DesktopExecutable -or -not $DesktopVersion) {
        throw "-CaptureColdRender requires exact -DesktopExecutable and -DesktopVersion values."
    }
    $ThreePageCaptureScript = Join-Path $PSScriptRoot "capture-powerbi-three-pages.ps1"
    & $PythonExe -m ai_powerbi_analyst.runtime.phase0_capture capture-cold-render `
        --repository-root $ProjectRoot `
        --demo-root $DemoRoot `
        --pbi-executable $PbiExecutable `
        --authoring-executable $AuthoringExecutable `
        --desktop-executable $DesktopExecutable `
        --desktop-version $DesktopVersion `
        --capture-script $ThreePageCaptureScript `
        --pbi-cli-version 3.11.1 `
        --authoring-cli-version 0.1.4
    if ($LASTEXITCODE -ne 0) {
        throw "The cold-reset populated three-page render gate failed. Phase 0 remains blocked."
    }
    Write-Host "Cold-reset rebuild and distinct three-page Desktop captures passed."
}
Write-Host "Phase 0 remains blocked until every remaining environment, model, offline, and render receipt is generated and independently accepted."
