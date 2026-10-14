[CmdletBinding()]
param(
    [string]$CertificateThumbprint,
    [string]$TimestampUrl,
    [string]$SignToolPath = 'signtool.exe',
    [switch]$RequireSigning
)

$ErrorActionPreference = 'Stop'
if ($RequireSigning -and -not $CertificateThumbprint) { throw 'Signing is required; supply CertificateThumbprint.' }
if ($CertificateThumbprint -and -not $TimestampUrl) { throw 'Signed release builds require an RFC 3161 TimestampUrl.' }
$repository = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$binaryRoot = Join-Path $repository 'apps\desktop\src-tauri\binaries'
$buildRoot = Join-Path $repository '.runtime\desktop-sidecar-build'
$templateRoot = Join-Path $repository 'fixtures\powerbi\client-wow-template'
$settingsFile = Join-Path $repository 'config\defaults.yaml'
$modelData = Join-Path $repository 'src\ai_powerbi_analyst\llm'
$reportCatalog = Join-Path $repository 'src\ai_powerbi_analyst\studio\report_visuals.json'
$layoutCatalog = Join-Path $repository 'src\ai_powerbi_analyst\studio\layout_templates.json'
$pbirCatalog = Join-Path $repository 'src\ai_powerbi_analyst\studio\pbir_catalog.json'
$reportThemes = Join-Path $repository 'src\ai_powerbi_analyst\powerbi\report_themes'
# Microsoft's filter and query JSON schemas, which report_edit_validation checks every edited
# filter against. Without them the check silently turns itself off in the packaged app. They must
# live under src (evidence\ does not ship), so the build refuses a checkout that lost them.
$pbirSchemas = Join-Path $repository 'src\ai_powerbi_analyst\studio\pbir_schemas'
if (-not (Test-Path -LiteralPath $pbirSchemas -PathType Container)) {
    throw "The packaged report schemas are missing: $pbirSchemas"
}
# The Desktop contract receipts imported_editing reads as its bundled fallback (a passing receipt
# for the installed Desktop version is what turns on model edits). The folder only exists once a
# check has been run and its evidence committed; the build must not fail before that.
$receiptEvidence = Join-Path $repository 'evals\imported_edit\evidence'
$receiptData = @()
if (Test-Path -LiteralPath $receiptEvidence -PathType Container) {
    $receiptData = @('--add-data', "$receiptEvidence;evals\imported_edit\evidence")
}
New-Item -ItemType Directory -Force -Path $binaryRoot, $buildRoot | Out-Null

$target = (& rustc -vV | Select-String '^host: ' | ForEach-Object { $_.Line.Substring(6) }).Trim()
if (-not $target) { throw 'Rust host target could not be determined.' }

Push-Location $repository
try {
    $sourceManifest = Join-Path $buildRoot 'source-manifest.json'
    uv run --frozen python scripts/sidecar_manifest.py capture --root $repository --manifest $sourceManifest
    if ($LASTEXITCODE -ne 0) { throw 'Could not record sidecar source inputs.' }
    uv run --frozen --extra desktop --extra databases pyinstaller `
        --noconfirm `
        --clean `
        --onefile `
        --name ai-powerbi-studio-engine `
        --distpath $buildRoot `
        --workpath (Join-Path $buildRoot 'work') `
        --specpath (Join-Path $buildRoot 'spec') `
        --add-data "$templateRoot;fixtures\powerbi\client-wow-template" `
        --add-data "$settingsFile;config" `
        --add-data "$reportCatalog;ai_powerbi_analyst\studio" `
        --add-data "$layoutCatalog;ai_powerbi_analyst\studio" `
        --add-data "$pbirCatalog;ai_powerbi_analyst\studio" `
        --add-data "$pbirSchemas;ai_powerbi_analyst\studio\pbir_schemas" `
        --add-data "$reportThemes;ai_powerbi_analyst\powerbi\report_themes" `
        --add-data "$modelData\m_validation.cjs;ai_powerbi_analyst\llm" `
        --add-data "$modelData\artifact_examples.json;ai_powerbi_analyst\llm" `
        --add-data "$modelData\MICROSOFT-POWERQUERY-PARSER-LICENSE.txt;ai_powerbi_analyst\llm" `
        --add-data "$modelData\M-VALIDATOR-THIRD-PARTY-NOTICES.txt;ai_powerbi_analyst\llm" `
        @receiptData `
        --hidden-import jsonschema `
        --hidden-import referencing.jsonschema `
        --collect-data jsonschema `
        --collect-data jsonschema_specifications `
        --collect-all powerbi_rag_runtime `
        --collect-all python_calamine `
        --collect-all pyarrow `
        --collect-all duckdb `
        --collect-all snowflake.connector `
        --hidden-import pyodbc `
        'scripts\studio_sidecar_entry.py'
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed; an older engine binary will not be promoted.' }
    $built = Join-Path $buildRoot 'ai-powerbi-studio-engine.exe'
    if (-not (Test-Path -LiteralPath $built -PathType Leaf)) { throw 'PyInstaller did not produce the studio sidecar.' }
    # Authenticode changes executable bytes: sign before recording the receipt and
    # before build_store_payload pins the installed engine's hash.
    if ($CertificateThumbprint) {
        & (Join-Path $PSScriptRoot 'sign-windows-artifact.ps1') -FilePath $built `
            -CertificateThumbprint $CertificateThumbprint -TimestampUrl $TimestampUrl -SignToolPath $SignToolPath
    }
    uv run --frozen python scripts/sidecar_manifest.py finalize --root $repository --manifest $sourceManifest --engine $built
    if ($LASTEXITCODE -ne 0) { throw 'Source changed during the build; the engine will not be promoted.' }
    Copy-Item -LiteralPath $built -Destination (Join-Path $binaryRoot "ai-powerbi-studio-engine-$target.exe") -Force
    Copy-Item -LiteralPath ([IO.Path]::ChangeExtension($built, '.build.json')) -Destination (Join-Path $binaryRoot "ai-powerbi-studio-engine-$target.build.json") -Force
}
finally {
    Pop-Location
}
