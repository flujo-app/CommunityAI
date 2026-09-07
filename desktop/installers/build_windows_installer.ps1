param(
    [Parameter(Mandatory=$true)][string]$Bundle,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [Parameter(Mandatory=$true)][string]$Version,
    [string]$Compiler = 'ISCC.exe',
    [string]$PublisherName = 'CommunityAI contributors',
    [string]$AppIdentifier = 'CommunityAI.Desktop',
    [string]$SigningToolCommand,
    [switch]$UnsignedEngineering
)
$ErrorActionPreference = 'Stop'
$bundlePath = (Resolve-Path -LiteralPath $Bundle).Path
if (-not (Test-Path -LiteralPath (Join-Path $bundlePath 'CommunityAI.exe') -PathType Leaf)) {
    throw 'Bundle must contain CommunityAI.exe'
}
if (-not (Test-Path -LiteralPath (Join-Path $bundlePath 'node\CommunityAI-Node.exe') -PathType Leaf)) {
    throw 'Bundle must contain the packaged node runtime'
}
if ($Version -notmatch '^\d+\.\d+\.\d+(?:[.-][A-Za-z0-9]+)*$') { throw 'Invalid version' }
if ($PublisherName -match '["\r\n]') { throw 'Invalid publisher name' }
if ($AppIdentifier -notmatch '^[A-Za-z0-9.-]+$') { throw 'Invalid application identifier' }
if (-not $SigningToolCommand -and -not $UnsignedEngineering) {
    throw 'A signing tool is required; explicitly select UnsignedEngineering for a test build'
}
if ($SigningToolCommand -and $UnsignedEngineering) { throw 'Choose signed or unsigned engineering output' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$outputPath = (Resolve-Path -LiteralPath $OutputDirectory).Path
$arguments = @('/Qp', "/DBundleDir=$bundlePath", "/DOutputPath=$outputPath", "/DAppVersion=$Version", "/DPublisherName=$PublisherName")
$arguments += "/DAppIdentifier=$AppIdentifier"
if ($SigningToolCommand) {
    $arguments += @('/DSigningTool=communityai', "/Scommunityai=$SigningToolCommand")
}
$arguments += (Join-Path $PSScriptRoot 'communityai.iss')
& $Compiler @arguments
if ($LASTEXITCODE -ne 0) { throw "Installer compiler failed: $LASTEXITCODE" }
$installer = Join-Path $outputPath "communityai-$Version-windows-setup.exe"
$signature = Get-AuthenticodeSignature -LiteralPath $installer
if ($SigningToolCommand -and $signature.Status -ne 'Valid') { throw 'The installer has no valid Authenticode signature' }
$metadata = @{
    version = $Version
    publisher = $PublisherName
    unsigned_engineering = [bool]$UnsignedEngineering
    sha256 = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    authenticode_status = [string]$signature.Status
    settings_cache_policy = 'Preserved on upgrade and uninstall'
}
$metadata | ConvertTo-Json | Set-Content -LiteralPath "$installer.json" -Encoding utf8
Write-Output $installer
