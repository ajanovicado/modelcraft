[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$OutDirectory,
    [string]$CertificateThumbprint,
    [string]$TimestampUrl = 'http://timestamp.digicert.com',
    [string]$SignToolPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repository = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$project = Join-Path $repository 'packaging\powerbi-tools'
$output = [IO.Path]::GetFullPath($OutDirectory)
if (Test-Path -LiteralPath $output) { throw 'Use a new output directory for the bundled Power BI tools.' }
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'The Windows .NET Framework compiler is required to build the tool launchers.' }
$node = (Get-Command node.exe -ErrorAction Stop).Source
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source

function Invoke-ToolSigning([string]$Path) {
    if (-not $CertificateThumbprint) { return }
    if ((Get-AuthenticodeSignature -LiteralPath $Path).Status -eq 'Valid') { return }
    & (Join-Path $PSScriptRoot 'sign-windows-artifact.ps1') -FilePath $Path `
        -CertificateThumbprint $CertificateThumbprint -TimestampUrl $TimestampUrl -SignToolPath $SignToolPath
}

function Get-ToolHashes {
    $hashes = [ordered]@{}
    foreach ($file in (Get-ChildItem -LiteralPath $output -Recurse -File | Sort-Object FullName)) {
        if ($file.Name -in @('payload-manifest.json', 'toolchain.json')) { continue }
        $relative = [IO.Path]::GetRelativePath($output, $file.FullName).Replace('\', '/')
        if ($relative.StartsWith('bin/')) { continue }
        if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Tool payload contains a redirected file.' }
        $hashes[$relative] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    return $hashes
}

$previousPythonEnvironment = $env:UV_PROJECT_ENVIRONMENT
Push-Location -LiteralPath $repository
try {
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $project '.venv'
    uv sync --frozen --project $project --python 3.12.12
    if ($LASTEXITCODE -ne 0) { throw 'Locked Power BI Python tooling could not be installed.' }
    & $npm ci --prefix $project --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Locked Microsoft Power BI tooling could not be installed.' }
    New-Item -ItemType Directory -Path $output | Out-Null
    $buildRoot = Join-Path $repository ('.runtime\powerbi-tool-build-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $buildRoot | Out-Null
    $python = Join-Path $project '.venv\Scripts\python.exe'
    & $python -m PyInstaller --noconfirm --clean --onedir --name pbi `
        --distpath $output --workpath (Join-Path $buildRoot 'work') --specpath $buildRoot `
        --collect-all pbi_cli --collect-all clr_loader --collect-all pythonnet `
        --copy-metadata pbi-cli-tool --hidden-import win32api --hidden-import win32file `
        (Join-Path $project 'pbi_entry.py')
    if ($LASTEXITCODE -ne 0) { throw 'The bundled Power BI Python tool did not build.' }
    $nodeRoot = Join-Path $output 'node'
    New-Item -ItemType Directory -Path $nodeRoot | Out-Null
    Copy-Item -LiteralPath $node -Destination (Join-Path $nodeRoot 'node.exe')
    $npmRoot = Join-Path $output 'npm'
    New-Item -ItemType Directory -Path $npmRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $project 'node_modules') -Destination $npmRoot -Recurse
    Copy-Item -LiteralPath (Join-Path $project 'package-lock.json') -Destination $npmRoot
    Copy-Item -LiteralPath (Join-Path $project 'uv.lock') -Destination (Join-Path $output 'python-dependencies.lock')
    # Carry the original interpreter and package license notices, including Microsoft's DLL terms.
    $metadata = Join-Path $project '.venv\Lib\site-packages'
    $licenseRoot = Join-Path $output 'licenses'
    New-Item -ItemType Directory -Path $licenseRoot | Out-Null
    $pythonBase = (& $python -c 'import sys; print(sys.base_prefix)').Trim()
    $pythonLicense = Join-Path $pythonBase 'LICENSE.txt'
    if (-not (Test-Path -LiteralPath $pythonLicense)) { throw 'The bundled Python interpreter license is missing.' }
    Copy-Item -LiteralPath $pythonLicense -Destination (Join-Path $licenseRoot 'PYTHON-LICENSE.txt')
    foreach ($directory in (Get-ChildItem -LiteralPath $metadata -Directory -Filter '*.dist-info')) {
        $destination = Join-Path $licenseRoot $directory.Name
        New-Item -ItemType Directory -Path $destination | Out-Null
        Copy-Item -LiteralPath (Join-Path $directory.FullName 'METADATA') -Destination $destination
        foreach ($notice in (Get-ChildItem -LiteralPath $directory.FullName -Recurse -File | Where-Object { $_.Name -match '^(LICENSE|COPYING|NOTICE|THIRD_PARTY)' })) {
            $relative = [IO.Path]::GetRelativePath($directory.FullName, $notice.FullName)
            $target = Join-Path $destination $relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            Copy-Item -LiteralPath $notice.FullName -Destination $target
        }
    }
    # Node's own release license must accompany its executable; use its exact version.
    $nodeVersion = (& $node --version).Trim()
    if ($nodeVersion -notmatch '^v\d+\.\d+\.\d+$') { throw 'Could not identify the bundled Node release.' }
    Invoke-WebRequest -Uri ('https://raw.githubusercontent.com/nodejs/node/' + $nodeVersion + '/LICENSE') `
        -OutFile (Join-Path $nodeRoot 'LICENSE.txt')
    foreach ($binary in (Get-ChildItem -LiteralPath $output -Recurse -File | Where-Object Extension -In @('.exe','.dll','.pyd'))) {
        Invoke-ToolSigning $binary.FullName
    }
    $payload = [ordered]@{ schema_version = 1; files = (Get-ToolHashes) }
    $payloadPath = Join-Path $output 'payload-manifest.json'
    $payload | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $payloadPath -Encoding utf8NoBOM
    $payloadHash = (Get-FileHash -LiteralPath $payloadPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $launcherSource = (Get-Content -LiteralPath (Join-Path $project 'ToolLauncher.cs') -Raw).Replace('__PAYLOAD_MANIFEST_SHA256__', $payloadHash)
    $sourcePath = Join-Path $buildRoot 'ToolLauncher.cs'
    Set-Content -LiteralPath $sourcePath -Value $launcherSource -Encoding utf8NoBOM
    $binRoot = Join-Path $output 'bin'
    New-Item -ItemType Directory -Path $binRoot | Out-Null
    $definitions = [ordered]@{ 'pbi' = 'PBI'; 'powerbi-report-author' = 'AUTHOR'; 'powerbi-desktop' = 'DESKTOP' }
    foreach ($name in $definitions.Keys) {
        $launcher = Join-Path $binRoot ($name + '.exe')
        & $compiler /nologo /target:exe /reference:System.Web.Extensions.dll ('/define:' + $definitions[$name]) ('/out:' + $launcher) $sourcePath
        if ($LASTEXITCODE -ne 0) { throw "The $name launcher did not compile." }
        Invoke-ToolSigning $launcher
    }
    $files = Get-ToolHashes
    $files['payload-manifest.json'] = $payloadHash
    foreach ($launcher in (Get-ChildItem -LiteralPath $binRoot -File)) {
        $files['bin/' + $launcher.Name] = (Get-FileHash -LiteralPath $launcher.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $manifest = [ordered]@{
        schema_version = 1
        tools = [ordered]@{
            pbi = @{ executable = 'bin/pbi.exe'; version = '3.11.1'; distribution_version = '3.11.1' }
            'powerbi-report-author' = @{ executable = 'bin/powerbi-report-author.exe'; version = '0.1.4' }
            'powerbi-desktop' = @{ executable = 'bin/powerbi-desktop.exe'; version = '0.1.2' }
        }
        files = $files
        signed = [bool]$CertificateThumbprint
    }
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $output 'toolchain.json') -Encoding utf8NoBOM
    Write-Host "Bundled Power BI tools: $output"
} finally {
    $env:UV_PROJECT_ENVIRONMENT = $previousPythonEnvironment
    Pop-Location
}
