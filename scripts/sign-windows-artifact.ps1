[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$FilePath,
    [Parameter(Mandatory)][ValidatePattern('^[0-9A-Fa-f]{40}$')][string]$CertificateThumbprint,
    [Parameter(Mandatory)][string]$TimestampUrl,
    [string]$SignToolPath = 'signtool.exe'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$artifact = (Resolve-Path -LiteralPath $FilePath -ErrorAction Stop).Path
$timestamp = [Uri]$TimestampUrl
if (-not $timestamp.IsAbsoluteUri -or $timestamp.Scheme -notin @('https', 'http')) {
    throw 'TimestampUrl must be an absolute RFC 3161 HTTP(S) timestamp service URL.'
}
$signTool = (Get-Command $SignToolPath -ErrorAction Stop).Source
# Tauri may request a signature again for an already sealed sidecar. Preserve
# the exact bytes if its expected timestamped signature is already valid.
$existing = Get-AuthenticodeSignature -LiteralPath $artifact
if ($existing.Status -eq 'Valid' -and $existing.SignerCertificate.Thumbprint -eq $CertificateThumbprint -and $existing.TimeStamperCertificate) {
    return
}
& $signTool sign /sha1 $CertificateThumbprint /s My /fd SHA256 /tr $TimestampUrl /td SHA256 $artifact
if ($LASTEXITCODE -ne 0) { throw 'Authenticode signing failed.' }
$signature = Get-AuthenticodeSignature -LiteralPath $artifact
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Thumbprint -ne $CertificateThumbprint -or -not $signature.TimeStamperCertificate) {
    throw 'Expected trusted, timestamped Authenticode signature was not produced.'
}
