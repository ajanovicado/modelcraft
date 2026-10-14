#Requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RuntimeInput,
    [Parameter(Mandatory)][string]$OutputDirectory,
    [string]$ToolchainRoot,
    [string]$CertificateThumbprint,
    [string]$TimestampUrl,
    [string]$SignToolPath = 'signtool.exe',
    [switch]$AllowUnsigned,
    [switch]$AllowDirty,
    [switch]$AllowPartialTools,
    [switch]$SkipEngineeringChecks
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'Windows x64 is required.' }
if (-not $CertificateThumbprint -and -not $AllowUnsigned) {
    throw 'Supply signing credentials or explicitly use AllowUnsigned for a local candidate.'
}
if ($CertificateThumbprint -and -not $TimestampUrl) { throw 'Signed candidates require TimestampUrl.' }
$repository = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runtime = (Resolve-Path -LiteralPath $RuntimeInput).Path
if ($ToolchainRoot) { $ToolchainRoot = (Resolve-Path -LiteralPath $ToolchainRoot).Path }
$output = [IO.Path]::GetFullPath($OutputDirectory)
$uv = (Get-Command uv -ErrorAction Stop).Source
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source
$powershell = (Get-Process -Id $PID).Path
function Invoke-Checked {
    param([string]$Executable, [string[]]$CommandArguments)
    & $Executable @CommandArguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable failed with exit code $LASTEXITCODE" }
}
# Build in a copied source tree; do not replace an existing developer engine or runtime.
$snapshotArguments = @('run', '--frozen', 'python', 'scripts/windows_release.py', 'snapshot', '--output', $output)
if ($AllowDirty) { $snapshotArguments += '--allow-dirty' }
Push-Location -LiteralPath $repository
try { Invoke-Checked $uv $snapshotArguments } finally { Pop-Location }
$source = Join-Path $output 'source'
$frontend = Join-Path $source 'apps/desktop'
$artifacts = Join-Path $output 'artifacts'
New-Item -ItemType Directory -Path $artifacts | Out-Null
Push-Location -LiteralPath $source
$previousTarget = $env:CARGO_TARGET_DIR
$previousPythonEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $source '.venv'
    Start-Transcript -LiteralPath (Join-Path $output 'build.log') | Out-Null
    Invoke-Checked $uv @('sync', '--frozen', '--all-extras', '--python', '3.12.12')
    Push-Location -LiteralPath $frontend
    try { Invoke-Checked $npm @('ci') } finally { Pop-Location }
    if (-not $SkipEngineeringChecks) {
        & (Join-Path $source 'scripts/verify-release.ps1') -Browser -Native -OutputDirectory (Join-Path $output 'engineering')
    }
    $sidecarArguments = @('-NoProfile', '-File', (Join-Path $source 'scripts/build-desktop-sidecar.ps1'))
    if ($CertificateThumbprint) {
        $sidecarArguments += @('-RequireSigning', '-CertificateThumbprint', $CertificateThumbprint, '-TimestampUrl', $TimestampUrl, '-SignToolPath', $SignToolPath)
    }
    Invoke-Checked $powershell $sidecarArguments
    if (-not $ToolchainRoot -and -not $AllowPartialTools) {
        $ToolchainRoot = Join-Path $output 'powerbi-tools'
        $toolArguments = @('-NoProfile', '-File', (Join-Path $source 'scripts/build-powerbi-tools.ps1'), '-OutDirectory', $ToolchainRoot)
        if ($CertificateThumbprint) {
            $toolArguments += @('-CertificateThumbprint', $CertificateThumbprint, '-TimestampUrl', $TimestampUrl, '-SignToolPath', $SignToolPath)
        }
        Invoke-Checked $powershell $toolArguments
    }
    if ($CertificateThumbprint -and $ToolchainRoot) {
        foreach ($nativeTool in (Get-ChildItem -LiteralPath $ToolchainRoot -Recurse -File | Where-Object { $_.Extension -in @('.exe', '.dll', '.pyd') })) {
            if ((Get-AuthenticodeSignature -LiteralPath $nativeTool.FullName).Status -ne 'Valid') {
                throw "Signed candidate has an untrusted native tool: $($nativeTool.FullName)"
            }
        }
    }
    $payloadArguments = @('run', '--frozen', 'python', 'scripts/build_store_payload.py', '--portable-input', $runtime)
    if ($ToolchainRoot) { $payloadArguments += @('--toolchain-root', $ToolchainRoot) }
    Invoke-Checked $uv $payloadArguments
    Copy-Item -LiteralPath (Join-Path $runtime 'input-manifest.json') -Destination (Join-Path $artifacts 'runtime-input-manifest.json')
    if ($CertificateThumbprint) {
        $payloadRoot = Join-Path $source 'apps/desktop/src-tauri/runtime'
        $signatureRecords = [Collections.Generic.List[object]]::new()
        foreach ($nativeFile in (Get-ChildItem -LiteralPath $payloadRoot -Recurse -File | Where-Object { $_.Extension -in @('.exe', '.dll', '.pyd') })) {
            $relative = [IO.Path]::GetRelativePath($payloadRoot, $nativeFile.FullName).Replace('\', '/')
            $originalHash = (Get-FileHash -LiteralPath $nativeFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            $signature = Get-AuthenticodeSignature -LiteralPath $nativeFile.FullName
            if ($relative.StartsWith('tools/')) {
                if ($signature.Status -ne 'Valid') { throw "Bundled Power BI tool is untrusted: $relative" }
                continue # Tool launcher manifests were sealed by build-powerbi-tools.
            }
            if ($signature.Status -ne 'Valid') {
                & (Join-Path $source 'scripts/sign-windows-artifact.ps1') -FilePath $nativeFile.FullName `
                    -CertificateThumbprint $CertificateThumbprint -TimestampUrl $TimestampUrl -SignToolPath $SignToolPath
                $signature = Get-AuthenticodeSignature -LiteralPath $nativeFile.FullName
            }
            if ($signature.Status -ne 'Valid') { throw "Native inference resource is untrusted: $relative" }
            $signatureRecords.Add(@{
                path = $relative; original_sha256 = $originalHash
                signed_sha256 = (Get-FileHash -LiteralPath $nativeFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                signature_status = $signature.Status.ToString(); signer_thumbprint = $signature.SignerCertificate.Thumbprint
            })
        }
        $recordsPath = Join-Path $artifacts 'runtime-signatures.json'
        ConvertTo-Json -InputObject @($signatureRecords.ToArray()) -Depth 6 | Set-Content -LiteralPath $recordsPath -Encoding utf8NoBOM
        Invoke-Checked $uv @('run', '--frozen', 'python', 'scripts/build_store_payload.py', '--seal-signatures', $recordsPath)
    }
    # Fresh Cargo target avoids picking up old installers. Runtime staging exists only
    # inside this candidate's copied checkout and is never recursively deleted.
    $env:CARGO_TARGET_DIR = Join-Path $output 'native-target'
    $override = @{ bundle = @{ windows = @{} } }
    if ($CertificateThumbprint) {
        $override.bundle.windows.signCommand = @{
            cmd = $powershell
            args = @('-NoProfile', '-File', (Join-Path $source 'scripts/sign-windows-artifact.ps1'), '-FilePath', '%1', '-CertificateThumbprint', $CertificateThumbprint, '-TimestampUrl', $TimestampUrl, '-SignToolPath', $SignToolPath)
        }
    }
    $overridePath = Join-Path $output 'tauri-release.json'
    $override | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $overridePath -Encoding utf8
    Push-Location -LiteralPath $frontend
    try { Invoke-Checked $npm @('run', 'tauri', '--', 'build', '--locked', '--config', $overridePath) } finally { Pop-Location }
    $bundle = Join-Path $env:CARGO_TARGET_DIR 'release/bundle'
    $installers = @(Get-ChildItem -LiteralPath $bundle -Recurse -File | Where-Object { $_.Extension -in @('.msi', '.exe') })
    if ($CertificateThumbprint) {
        $gui = Join-Path $env:CARGO_TARGET_DIR 'release/ai-powerbi-studio.exe'
        $engine = Join-Path $source 'apps/desktop/src-tauri/binaries/ai-powerbi-studio-engine-x86_64-pc-windows-msvc.exe'
        foreach ($file in (@($gui, $engine) + @($installers.FullName))) {
            $signature = Get-AuthenticodeSignature -LiteralPath $file
            if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Thumbprint -ne $CertificateThumbprint -or -not $signature.TimeStamperCertificate) {
                throw "Candidate contains an unexpected signature: $file"
            }
        }
    }
    foreach ($installer in $installers) { Copy-Item -LiteralPath $installer.FullName -Destination $artifacts }
    $finishArguments = @('run', '--frozen', 'python', 'scripts/windows_release.py', 'finish', '--output', $output)
    if ($CertificateThumbprint) { $finishArguments += '--signed' }
    if (-not $SkipEngineeringChecks) { $finishArguments += '--engineering-passed' }
    if ($ToolchainRoot) { $finishArguments += '--tools-complete' }
    Invoke-Checked $uv $finishArguments
    Write-Host "Candidate artifacts: $artifacts. Installed-app qualification and release approval are still required."
} finally {
    $env:CARGO_TARGET_DIR = $previousTarget
    $env:UV_PROJECT_ENVIRONMENT = $previousPythonEnvironment
    Stop-Transcript -ErrorAction SilentlyContinue | Out-Null
    Pop-Location
}
