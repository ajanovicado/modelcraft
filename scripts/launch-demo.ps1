[CmdletBinding()]
param(
    [ValidateSet("real", "mock")]
    [string]$Mode = "real",
    [string]$BundlePath = "",
    [int]$Port = 8501
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Port -lt 1024 -or $Port -gt 65535) {
    throw "Port must be between 1024 and 65535."
}
$RunningOnWindows = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
if (-not $RunningOnWindows -and $Mode -eq "real") {
    throw "Real Power BI mode requires Windows. Use -Mode mock for local UI verification."
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VirtualPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path -LiteralPath $VirtualPython) { $VirtualPython } else { "python" }
$AppPath = Join-Path $ProjectRoot "src\ai_powerbi_analyst\ui\app.py"
$StatePath = Join-Path $ProjectRoot ".runtime"
if (-not $BundlePath) {
    $BundlePath = Join-Path $ProjectRoot "fixtures\demo\project-bundle.json"
}

$env:AI_POWERBI_PROJECT_ROOT = $ProjectRoot
$env:AI_POWERBI_UI_MODE = $Mode
$env:AI_POWERBI_STATE_DIR = $StatePath
$env:AI_POWERBI_BUNDLE_PATH = $BundlePath
$env:AI_POWERBI_SETTINGS_PATH = Join-Path $ProjectRoot "config\defaults.yaml"
$env:NO_PROXY = "127.0.0.1,localhost"
$env:STREAMLIT_BROWSER_GATHER_USAGE_STATS = "false"

function ConvertTo-LiteralProcessArgument([string]$Value) {
    if ($Value.IndexOf('"') -ge 0 -or $Value.IndexOf("`0") -ge 0 -or `
        $Value.IndexOf("`r") -ge 0 -or $Value.IndexOf("`n") -ge 0) {
        throw "A child-process argument contains a character that cannot be passed literally."
    }
    return '"' + $Value + '"'
}

& $PythonExe -c "import streamlit, ai_powerbi_analyst" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "The offline project environment is not installed. Run the documented Windows setup first."
}

$Model = ""
$ModelDigest = ""
$OllamaProcess = $null
if ($Mode -eq "real") {
    $AllowedInheritedOllamaHosts = @(
        "127.0.0.1:11434",
        "localhost:11434",
        "http://127.0.0.1:11434",
        "http://localhost:11434"
    )
    if ($env:OLLAMA_HOST -and $env:OLLAMA_HOST -notin $AllowedInheritedOllamaHosts) {
        throw "Inherited OLLAMA_HOST is not the approved loopback endpoint."
    }
    $ListenerCommand = Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue
    if ($null -eq $ListenerCommand) {
        throw "Get-NetTCPConnection is required to verify Ollama's loopback-only listener."
    }
    $UnsafeOllamaListeners = @(
        Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -notin @("127.0.0.1", "::1") }
    )
    if ($UnsafeOllamaListeners.Count -gt 0) {
        throw "Port 11434 already has a non-loopback listener; local-only launch is blocked."
    }
    $env:OLLAMA_HOST = "127.0.0.1:11434"
    if (-not (Test-Path -LiteralPath $BundlePath -PathType Leaf)) {
        throw "Prepared ProjectBundle not found at $BundlePath"
    }
    $DemoRoot = Split-Path -Parent $BundlePath
    & $PythonExe -m ai_powerbi_analyst.runtime.preparation verify `
        --repository-root $ProjectRoot `
        --demo-root $DemoRoot `
        --transaction-state (Join-Path $StatePath "powerbi") `
        --require-attested
    if ($LASTEXITCODE -ne 0) {
        throw "The prepared demo failed integrity or Phase-0 attestation verification."
    }

    $Bundle = Get-Content -LiteralPath $BundlePath -Raw | ConvertFrom-Json
    $EnvironmentReceiptPath = Join-Path $DemoRoot "evidence\phase0\environment.json"
    if (-not (Test-Path -LiteralPath $EnvironmentReceiptPath -PathType Leaf)) {
        throw "The accepted Phase-0 environment receipt is missing."
    }
    $ExpectedEnvironmentReceiptHash = [string]$Bundle.phase0_receipt_hashes.environment
    $ActualEnvironmentReceiptHash = (Get-FileHash -LiteralPath $EnvironmentReceiptPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if (-not $ExpectedEnvironmentReceiptHash -or $ActualEnvironmentReceiptHash -ne $ExpectedEnvironmentReceiptHash) {
        throw "The named environment receipt no longer matches the accepted ProjectBundle."
    }
    $EnvironmentReceipt = Get-Content -LiteralPath $EnvironmentReceiptPath -Raw | ConvertFrom-Json
    $AttestedOllamaPath = [string]$EnvironmentReceipt.tool_executables.ollama.path
    $AttestedOllamaHash = [string]$EnvironmentReceipt.tool_executables.ollama.sha256
    if (-not $AttestedOllamaPath -or -not (Test-Path -LiteralPath $AttestedOllamaPath -PathType Leaf)) {
        throw "The attested Ollama executable is missing."
    }
    $ActualOllamaHash = (Get-FileHash -LiteralPath $AttestedOllamaPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualOllamaHash -ne $AttestedOllamaHash) {
        throw "The attested Ollama executable hash changed."
    }
    $Model = [string]$Bundle.compatibility.model_tag
    $ModelDigest = [string]$Bundle.compatibility.model_digest
    $DesktopVersion = [string]$Bundle.compatibility.desktop_version
    if (-not $Model -or -not $ModelDigest -or -not $DesktopVersion) {
        throw "The attested ProjectBundle does not contain exact model and Desktop pins."
    }

    $OllamaEndpoint = "http://127.0.0.1:11434"
    $OllamaReady = $false
    $LoopbackListeners = @(Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue)
    if ($LoopbackListeners.Count -gt 1) {
        throw "Multiple Ollama listeners exist; owning executable identity is ambiguous."
    }
    if ($LoopbackListeners.Count -eq 1) {
        $Owner = Get-Process -Id $LoopbackListeners[0].OwningProcess -ErrorAction Stop
        $OwnerPath = [string]$Owner.Path
        if (-not $OwnerPath -or -not (Test-Path -LiteralPath $OwnerPath -PathType Leaf)) {
            throw "The existing Ollama listener owner executable cannot be identified."
        }
        $OwnerHash = (Get-FileHash -LiteralPath $OwnerPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($OwnerHash -ne $AttestedOllamaHash) {
            throw "The existing loopback Ollama listener is not owned by the attested executable."
        }
        try {
            $null = Invoke-RestMethod -Uri "$OllamaEndpoint/api/tags" -TimeoutSec 1
            $OllamaReady = $true
        } catch {
            throw "The attested Ollama listener did not answer its loopback API."
        }
    } else {
        $OllamaProcess = Start-Process -FilePath $AttestedOllamaPath `
            -ArgumentList @("serve") -PassThru -WindowStyle Hidden
    }
    for ($Attempt = 0; -not $OllamaReady -and $Attempt -lt 40; $Attempt++) {
        if ($null -ne $OllamaProcess -and $OllamaProcess.HasExited) {
            throw "Ollama exited before its loopback API became ready."
        }
        Start-Sleep -Milliseconds 250
        try {
            $null = Invoke-RestMethod -Uri "$OllamaEndpoint/api/tags" -TimeoutSec 1
            $OllamaReady = $true
        } catch {
            # The bounded loop retries only the local Ollama endpoint.
        }
    }
    if (-not $OllamaReady) {
        throw "Ollama did not become ready on the loopback interface."
    }
    $VerifiedListeners = @(Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction Stop)
    if ($VerifiedListeners.Count -ne 1 -or $VerifiedListeners[0].LocalAddress -notin @("127.0.0.1", "::1")) {
        throw "Ollama readiness was not backed by one loopback-only listener."
    }
    $VerifiedOwner = Get-Process -Id $VerifiedListeners[0].OwningProcess -ErrorAction Stop
    $VerifiedOwnerPath = [string]$VerifiedOwner.Path
    if (-not $VerifiedOwnerPath -or -not (Test-Path -LiteralPath $VerifiedOwnerPath -PathType Leaf)) {
        throw "The ready Ollama listener owner executable cannot be identified."
    }
    $VerifiedOwnerHash = (Get-FileHash -LiteralPath $VerifiedOwnerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($VerifiedOwnerHash -ne $AttestedOllamaHash) {
        throw "The ready Ollama listener is not owned by the attested executable."
    }

    & $PythonExe -m ai_powerbi_analyst.ui.launch_checks `
        --mode real --model $Model --digest $ModelDigest --desktop-version $DesktopVersion
    if ($LASTEXITCODE -ne 0) {
        throw "Preflight failed. The launcher will not weaken a pin or download a model."
    }

    $WarmPayload = @{
        model = $Model
        prompt = ""
        stream = $false
        keep_alive = "5m"
        options = @{ temperature = 0; num_predict = 1 }
    } | ConvertTo-Json -Depth 4
    $null = Invoke-RestMethod -Method Post -Uri "$OllamaEndpoint/api/generate" `
        -ContentType "application/json" -Body $WarmPayload -TimeoutSec 60

    Write-Host "Validating the complete local analytics and Power BI workspace..."
    & $PythonExe -m ai_powerbi_analyst.ui.production_runtime --preflight
    if ($LASTEXITCODE -ne 0) {
        throw "Complete application preflight failed before browser launch."
    }
} else {
    & $PythonExe -m ai_powerbi_analyst.ui.launch_checks --mode mock --model "unused"
    if ($LASTEXITCODE -ne 0) {
        throw "Local mock-mode preflight failed."
    }
}

$StreamlitArguments = @(
    "-m", "streamlit", "run", $AppPath,
    "--server.address", "127.0.0.1",
    "--server.port", "$Port",
    "--server.headless", "true",
    "--browser.gatherUsageStats", "false"
)
$LiteralStreamlitArguments = @(
    $StreamlitArguments | ForEach-Object { ConvertTo-LiteralProcessArgument ([string]$_) }
)
$AppProcess = Start-Process -FilePath $PythonExe -ArgumentList $LiteralStreamlitArguments `
    -PassThru -NoNewWindow
$Url = "http://127.0.0.1:$Port"

try {
    $AppReady = $false
    for ($Attempt = 0; -not $AppReady -and $Attempt -lt 60; $Attempt++) {
        if ($AppProcess.HasExited) {
            throw "The local application exited before it became ready."
        }
        Start-Sleep -Milliseconds 250
        try {
            $Response = Invoke-WebRequest -Uri "$Url/_stcore/health" `
                -UseBasicParsing -TimeoutSec 1
            $AppReady = $Response.StatusCode -eq 200
        } catch {
            # The bounded loop retries only the local Streamlit health endpoint.
        }
    }
    if (-not $AppReady) {
        throw "The local application did not become healthy within 15 seconds."
    }
    Start-Process $Url
    Write-Host "Modelcraft is running locally at $Url"
    Wait-Process -Id $AppProcess.Id
} finally {
    if (-not $AppProcess.HasExited) {
        Stop-Process -Id $AppProcess.Id
    }
    if ($null -ne $OllamaProcess -and -not $OllamaProcess.HasExited) {
        Stop-Process -Id $OllamaProcess.Id
    }
}
