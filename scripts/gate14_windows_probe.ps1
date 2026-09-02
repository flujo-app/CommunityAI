param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$Facts,
    [Parameter(Mandatory = $true)][string]$Challenge,
    [Parameter(Mandatory = $true)][string]$Package,
    [Parameter(Mandatory = $true)][string]$ReleaseMetadata,
    [Parameter(Mandatory = $true)][string]$Output
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$probe = Join-Path $PSScriptRoot "gate14_host_probe.py"
& $Python $probe --platform windows --facts $Facts --challenge $Challenge --package $Package --release-metadata $ReleaseMetadata --output $Output
if ($LASTEXITCODE -ne 0) {
    throw "Gate 14 Windows probe failed"
}
