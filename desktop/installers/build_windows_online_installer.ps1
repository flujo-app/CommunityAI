param(
    [Parameter(Mandatory=$true)][string]$Manifest,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [string]$Compiler = 'ISCC.exe',
    [string]$PythonCommand = 'python',
    [string]$SigningToolCommand,
    [switch]$UnsignedAlpha,
    [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'
$manifestPath = (Resolve-Path -LiteralPath $Manifest).Path
$selector = Join-Path $PSScriptRoot 'release_downloads.py'
$selectedJson = & $PythonCommand $selector select $manifestPath --platform windows-x64
if ($LASTEXITCODE -ne 0) { throw 'The release download manifest was rejected' }
$artifact = ($selectedJson -join "`n") | ConvertFrom-Json

# Defense at the preprocessor boundary as well as the common manifest validator.
if ($artifact.platform -cne 'windows-x64' -or $artifact.kind -cne 'offline-installer' -or $artifact.format -cne 'exe') {
    throw 'The selected artifact is not a Windows offline installer'
}
if ($artifact.version -notmatch '^\d+\.\d+\.\d+(?:[.-][A-Za-z0-9]+)*$') { throw 'Invalid release version' }
if ($artifact.filename -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*\.exe$') { throw 'Invalid installer filename' }
if ($artifact.sha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'Invalid installer SHA-256' }
if ($artifact.size_bytes -isnot [long] -and $artifact.size_bytes -isnot [int]) { throw 'Invalid installer byte size' }
if ($artifact.size_bytes -le 0) { throw 'Invalid installer byte size' }
if ($artifact.url -notmatch '^https://[^\s"''{}\\]+$') { throw 'Invalid HTTPS installer URL' }
$publisher = if ($artifact.publisher) { $artifact.publisher } else { 'CommunityAI contributors' }
if ($publisher -match '["''{}\r\n]' -or $publisher.Length -gt 160) { throw 'Invalid publisher name' }
if ($SigningToolCommand -and $UnsignedAlpha) { throw 'Choose signed or explicitly unsigned output' }
if (-not $SigningToolCommand -and -not $UnsignedAlpha) { throw 'Specify a signing command or explicitly select UnsignedAlpha' }

if ($ValidateOnly) {
    $artifact | ConvertTo-Json -Depth 10
    exit 0
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$outputPath = (Resolve-Path -LiteralPath $OutputDirectory).Path
$filename = "communityai-$($artifact.version)-windows-online-setup.exe"
$installerPath = Join-Path $outputPath $filename
if (Test-Path -LiteralPath $installerPath) { throw 'Refusing to overwrite an existing online installer' }
$helperSource = Join-Path $PSScriptRoot 'WindowsDownload.cs'
$frameworkCompiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $frameworkCompiler -PathType Leaf)) {
    $frameworkCompiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path -LiteralPath $frameworkCompiler -PathType Leaf)) {
    throw 'The Windows .NET Framework C# compiler is required to build the downloader helper'
}
$helperBuild = Join-Path $outputPath 'downloader-build'
New-Item -ItemType Directory -Path $helperBuild -Force | Out-Null
$helperPath = Join-Path $helperBuild 'WindowsDownload.exe'
& $frameworkCompiler /nologo /target:exe /platform:anycpu /optimize+ "/out:$helperPath" $helperSource
if ($LASTEXITCODE -ne 0) { throw "Downloader helper compiler failed: $LASTEXITCODE" }
$arguments = @(
    '/Qp',
    "/DOutputPath=$outputPath",
    "/DAppVersion=$($artifact.version)",
    "/DPublisherName=$publisher",
    "/DInstallerUrl=$($artifact.url)",
    "/DInstallerFilename=$($artifact.filename)",
    "/DInstallerSha256=$($artifact.sha256)",
    "/DInstallerSize=$($artifact.size_bytes)",
    "/DDownloadHelper=$helperPath"
)
if ($SigningToolCommand) { $arguments += @('/DSigningTool=communityai', "/Scommunityai=$SigningToolCommand") }
$arguments += (Join-Path $PSScriptRoot 'communityai-online.iss')
& $Compiler @arguments
if ($LASTEXITCODE -ne 0) { throw "Online installer compiler failed: $LASTEXITCODE" }
if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) { throw 'The compiler did not produce the online installer' }
$signature = Get-AuthenticodeSignature -LiteralPath $installerPath
if ($SigningToolCommand -and $signature.Status -ne 'Valid') { throw 'The online installer has no valid Authenticode signature' }
$metadata = @{
    schema_version = 1
    kind = 'online-installer'
    version = $artifact.version
    platform = 'windows-x64'
    filename = $filename
    size_bytes = (Get-Item -LiteralPath $installerPath).Length
    sha256 = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    authenticode_status = [string]$signature.Status
    unsigned_alpha = [bool]$UnsignedAlpha
    offline_installer = $artifact
    release_manifest_sha256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    download_helper_sha256 = (Get-FileHash -LiteralPath $helperPath -Algorithm SHA256).Hash.ToLowerInvariant()
    download_helper_source_sha256 = (Get-FileHash -LiteralPath $helperSource -Algorithm SHA256).Hash.ToLowerInvariant()
    installer_script_sha256 = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'communityai-online.iss') -Algorithm SHA256).Hash.ToLowerInvariant()
    builder_script_sha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
    live_download_verified = $false
}
$metadata | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath "$installerPath.json" -Encoding utf8
Write-Output $installerPath
