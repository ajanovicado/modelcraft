[CmdletBinding()]
param(
    [switch]$Browser,
    [switch]$Native,
    [switch]$Desktop,
    [string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repository = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path ([IO.Path]::GetTempPath()) ('studio-release-checks-' + [guid]::NewGuid().ToString('N'))
}
$output = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $output -ErrorAction Stop | Out-Null
$checks = [Collections.Generic.List[object]]::new()
$report = [ordered]@{
    started_at = [DateTime]::UtcNow.ToString('o')
    status = 'running'
    scope = 'Engineering checks; not installer signing, clean-machine, model corpus or release approval'
    checks = $checks
    release_approved = $false
}

function Invoke-ReleaseCheck {
    param([string]$Name, [string]$Executable, [string[]]$CommandArguments, [string]$Directory)
    $watch = [Diagnostics.Stopwatch]::StartNew()
    Push-Location -LiteralPath $Directory
    try {
        & $Executable @CommandArguments 2>&1 | Tee-Object -FilePath (Join-Path $output ($Name + '.log'))
        $code = $LASTEXITCODE
    } finally { Pop-Location }
    $checks.Add([ordered]@{ name = $Name; exit_code = $code; elapsed_seconds = $watch.Elapsed.TotalSeconds })
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $output 'checks.json') -Encoding utf8
    if ($code -ne 0) { throw "$Name failed. See $output" }
}

Push-Location -LiteralPath $repository
try {
    $sourceFiles = & rg --files --hidden -g '!.git/**' -g '!evidence/**' -g '!demo/**'
    if ($LASTEXITCODE -ne 0) { throw 'Could not enumerate the source baseline.' }
    $manifest = foreach ($relative in ($sourceFiles | Sort-Object)) {
        $entry = Get-Item -LiteralPath (Join-Path $repository $relative)
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Source baseline includes a reparse point: $relative"
        }
        [ordered]@{ path = $relative; sha256 = (Get-FileHash -LiteralPath $entry.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
    }
    $manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $output 'source-manifest.json') -Encoding utf8
    $report.source_manifest_hash = (Get-FileHash -LiteralPath (Join-Path $output 'source-manifest.json')).Hash
    $python = Join-Path $repository '.venv/Scripts/python.exe'
    $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
    $frontend = Join-Path $repository 'apps/desktop'
    Invoke-ReleaseCheck 'ruff' $python @('-m', 'ruff', 'check', 'src', 'tests', 'typings') $repository
    Invoke-ReleaseCheck 'ruff-format' $python @('-m', 'ruff', 'format', '--check', 'src', 'tests', 'typings') $repository
    Invoke-ReleaseCheck 'mypy' $python @('-m', 'mypy', '--cache-dir', (Join-Path $output 'mypy-cache'), 'src/ai_powerbi_analyst') $repository
    Invoke-ReleaseCheck 'pytest' $python @('-m', 'pytest', ('--junitxml=' + (Join-Path $output 'pytest.xml'))) $repository
    Invoke-ReleaseCheck 'frontend-unit' $npm @('test') $frontend
    Invoke-ReleaseCheck 'frontend-lint' $npm @('run', 'lint') $frontend
    Invoke-ReleaseCheck 'frontend-build' $npm @('run', 'build') $frontend
    if ($Browser) {
        Invoke-ReleaseCheck 'chat-browser' $npm @('run', 'test:chat-browser') $frontend
        Invoke-ReleaseCheck 'data-browser' $npm @('run', 'test:data-browser') $frontend
        Invoke-ReleaseCheck 'model-setup-browser' $npm @('run', 'test:model-browser') $frontend
        Invoke-ReleaseCheck 'browser' $npm @('run', 'test:e2e') $frontend
    }
    if ($Native) {
        $cargo = (Get-Command cargo -ErrorAction Stop).Source
        Invoke-ReleaseCheck 'native-source-check' $cargo @('check', '--locked', '--features', 'source-check') (Join-Path $frontend 'src-tauri')
    }
    if ($Desktop) {
        Invoke-ReleaseCheck 'desktop-roundtrips' $python @('scripts/qualify-studio-desktop.py') $repository
    }
    $finalSourceFiles = & rg --files --hidden -g '!.git/**' -g '!evidence/**' -g '!demo/**'
    if ($LASTEXITCODE -ne 0 -or (Compare-Object ($sourceFiles | Sort-Object) ($finalSourceFiles | Sort-Object))) {
        throw 'Source files were added or removed during qualification. Rerun on the final candidate.'
    }
    foreach ($entry in $manifest) {
        if ((Get-FileHash -LiteralPath (Join-Path $repository $entry.path)).Hash.ToLowerInvariant() -ne $entry.sha256) {
            throw "Source changed during qualification: $($entry.path). Rerun on the final candidate."
        }
    }
    $report.status = 'engineering_checks_passed'
} catch {
    $report.status = 'failed'
    $report.error = $_.Exception.Message
    throw
} finally {
    $report.finished_at = [DateTime]::UtcNow.ToString('o')
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $output 'checks.json') -Encoding utf8
    Pop-Location
    Write-Host "Release evidence: $output"
}
