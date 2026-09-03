param(
    [Parameter(Mandatory = $true)][ValidatePattern("^[0-9a-f]{64}$")][string]$SessionId,
    [Parameter(Mandatory = $true)][ValidatePattern("^[a-z0-9][a-z0-9-]{0,62}$")][string]$RunId,
    [Parameter(Mandatory = $true)][ValidateRange(1, 100)][int]$AttemptOrdinal,
    [Parameter(Mandatory = $true)][ValidatePattern("^[0-9a-f]{40}$")][string]$SourceCommit,
    [Parameter(Mandatory = $true)][ValidatePattern("^sha256:[0-9a-f]{64}$")][string]$PackageSha256,
    [Parameter(Mandatory = $true)][string]$Gate13Lifecycle,
    [Parameter(Mandatory = $true)][string]$Gate13Inference,
    [Parameter(Mandatory = $true)][ValidatePattern("^[0-9a-f]{64}$")][string]$Gate13LifecycleSha256,
    [Parameter(Mandatory = $true)][ValidatePattern("^[0-9a-f]{64}$")][string]$Gate13InferenceSha256,
    [Parameter(Mandatory = $true)][string]$ProductActions,
    [Parameter(Mandatory = $true)][ValidatePattern("^[0-9a-f]{64}$")][string]$ProductActionsSha256,
    [Parameter(Mandatory = $true)][string]$LifecycleConfig,
    [Parameter(Mandatory = $true)][ValidatePattern("^sha256:[0-9a-f]{64}$")][string]$LifecycleConfigSha256,
    [switch]$TransportSelfTest,
    [string]$SelfTestCleanupMarker = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProgressPreference = "SilentlyContinue"
$VerbosePreference = "SilentlyContinue"
$DebugPreference = "SilentlyContinue"
$InformationPreference = "SilentlyContinue"

$script:Gate14MaxFrameBytes = 262144
$script:Gate14Scope = "gate14-windows-lifecycle-actions"
$script:Gate14Phase = "new"
$script:Gate14Binding = $null
$script:Gate14Cleaned = $false
$script:Gate14CleanupResult = $null
$script:Gate14StateNonce = [Guid]::NewGuid().ToString("N")

function Get-Gate14NormalizedSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or -not ($item -is [IO.FileInfo])) {
        throw "action helper is unsafe"
    }
    if ($item.Length -lt 1 -or $item.Length -gt (8 * 1024 * 1024)) {
        throw "action helper size is invalid"
    }
    $bytes = [IO.File]::ReadAllBytes($item.FullName)
    $utf8 = New-Object Text.UTF8Encoding($false, $true)
    $text = $utf8.GetString($bytes)
    if ($text.Length -gt 0 -and $text[0] -eq [char]0xfeff) {
        throw "action helper encoding is invalid"
    }
    $text = $text.Replace("`r`n", "`n")
    if ($text.Contains("`r")) {
        throw "action helper line endings are invalid"
    }
    $normalized = $utf8.GetBytes($text)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($hasher.ComputeHash($normalized))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $hasher.Dispose()
    }
}

function Assert-Gate14ExactProperties {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Names
    )

    if ($null -eq $Value) {
        throw "RPC object is absent"
    }
    $actual = @($Value.PSObject.Properties | ForEach-Object { $_.Name })
    if ($actual.Count -ne $Names.Count) {
        throw "RPC object schema is invalid"
    }
    foreach ($name in $Names) {
        if (-not ($actual -ccontains $name)) {
            throw "RPC object schema is invalid"
        }
    }
}

function Assert-Gate14Binding {
    param([Parameter(Mandatory = $true)]$Binding)

    Assert-Gate14ExactProperties -Value $Binding -Names @(
        "attempt_ordinal",
        "lifecycle_config_sha256",
        "package_sha256",
        "platform",
        "run_id",
        "source_commit"
    )
    if (
        $Binding.platform -isnot [string] -or
        $Binding.platform -cne "windows" -or
        $Binding.run_id -isnot [string] -or
        $Binding.run_id -cne $RunId -or
        $Binding.source_commit -isnot [string] -or
        $Binding.source_commit -cne $SourceCommit -or
        $Binding.package_sha256 -isnot [string] -or
        $Binding.package_sha256 -cne $PackageSha256 -or
        $Binding.lifecycle_config_sha256 -isnot [string] -or
        $Binding.lifecycle_config_sha256 -cne $LifecycleConfigSha256 -or
        $Binding.attempt_ordinal -isnot [int] -or
        $Binding.attempt_ordinal -ne $AttemptOrdinal
    ) {
        throw "RPC binding is invalid"
    }
    $canonical = ConvertTo-Json -InputObject $Binding -Compress
    if ($null -eq $script:Gate14Binding) {
        $script:Gate14Binding = $canonical
    }
    elseif ($script:Gate14Binding -cne $canonical) {
        throw "RPC binding changed"
    }
}

function Write-Gate14Response {
    param(
        [Parameter(Mandatory = $true)][int]$RequestId,
        [Parameter(Mandatory = $true)][string]$Operation,
        [Parameter(Mandatory = $true)][ValidateSet("passed", "failed")][string]$Result,
        $Payload,
        [AllowNull()]$FailureCode
    )

    $response = [ordered]@{
        failure_code = $FailureCode
        operation = $Operation
        payload = $Payload
        request_id = $RequestId
        result = $Result
        schema_version = 1
        scope = $script:Gate14Scope
        session_id = $SessionId
    }
    $rendered = ConvertTo-Json -InputObject $response -Compress -Depth 20
    if ([Text.Encoding]::UTF8.GetByteCount($rendered) -gt $script:Gate14MaxFrameBytes) {
        throw "RPC response is too large"
    }
    [Console]::Out.WriteLine($rendered)
    [Console]::Out.Flush()
}

function Invoke-Gate14Cleanup {
    if ($script:Gate14Cleaned) {
        return $script:Gate14CleanupResult
    }
    if ($TransportSelfTest) {
        if ($SelfTestCleanupMarker.Length -gt 0) {
            [IO.File]::WriteAllText($SelfTestCleanupMarker, "cleaned", (New-Object Text.UTF8Encoding($false)))
        }
        $result = [ordered]@{
            action_temporaries_removed = $true
            attempt_ordinal = [int]$AttemptOrdinal
            credentials_removed = $true
            platform = "windows"
            processes_absent = $true
            run_id = [string]$RunId
            schema_version = 1
            scope = "gate14-host-lifecycle-cleanup"
        }
    }
    else {
        $result = Invoke-Gate14WindowsProductCleanup
    }
    $script:Gate14CleanupResult = $result
    $script:Gate14Cleaned = $true
    return $result
}

$lifecyclePath = (Get-Item -LiteralPath $Gate13Lifecycle -Force).FullName
$inferencePath = (Get-Item -LiteralPath $Gate13Inference -Force).FullName
$productActionsPath = (Get-Item -LiteralPath $ProductActions -Force).FullName
if (
    [IO.Path]::GetFileName($lifecyclePath) -cne "gate13_windows_packaged_lifecycle.ps1" -or
    [IO.Path]::GetFileName($inferencePath) -cne "gate13_windows_localhost_inference.ps1" -or
    [IO.Path]::GetFileName($productActionsPath) -cne "gate14_windows_product_actions.ps1" -or
    [IO.Path]::GetDirectoryName($lifecyclePath) -cne [IO.Path]::GetDirectoryName($inferencePath) -or
    [IO.Path]::GetDirectoryName($lifecyclePath) -cne [IO.Path]::GetDirectoryName($productActionsPath) -or
    (Get-Gate14NormalizedSha256 -Path $lifecyclePath) -cne $Gate13LifecycleSha256 -or
    (Get-Gate14NormalizedSha256 -Path $inferencePath) -cne $Gate13InferenceSha256 -or
    (Get-Gate14NormalizedSha256 -Path $productActionsPath) -cne $ProductActionsSha256
) {
    throw "action helper binding is invalid"
}
$configItem = Get-Item -LiteralPath $LifecycleConfig -Force
if (
    ($configItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
    -not ($configItem -is [IO.FileInfo]) -or
    $configItem.Name -cne "gate14-lifecycle.json" -or
    $configItem.Length -lt 1 -or
    $configItem.Length -gt 65536
) {
    throw "lifecycle configuration is unsafe"
}
$configHasher = [Security.Cryptography.SHA256]::Create()
try {
    $configDigest = "sha256:" + ([BitConverter]::ToString($configHasher.ComputeHash([IO.File]::ReadAllBytes($configItem.FullName)))).Replace("-", "").ToLowerInvariant()
}
finally {
    $configHasher.Dispose()
}
if ($configDigest -cne $LifecycleConfigSha256) {
    throw "lifecycle configuration binding is invalid"
}

. $lifecyclePath
. $inferencePath
. $productActionsPath
if (
    $null -eq (Get-Command Force-Gate13ProductCleanup -ErrorAction SilentlyContinue) -or
    $null -eq (Get-Command Invoke-Gate13LoopbackJson -ErrorAction SilentlyContinue) -or
    $null -eq (Get-Command Initialize-Gate14WindowsProductActions -ErrorAction SilentlyContinue) -or
    $null -eq (Get-Command Invoke-Gate14WindowsProductPrepare -ErrorAction SilentlyContinue) -or
    $null -eq (Get-Command Invoke-Gate14WindowsProductCalibrate -ErrorAction SilentlyContinue) -or
    $null -eq (Get-Command Invoke-Gate14WindowsProductCleanup -ErrorAction SilentlyContinue)
) {
    throw "required action helpers are unavailable"
}
if (-not $TransportSelfTest) {
    Initialize-Gate14WindowsProductActions `
        -LifecycleConfig $configItem.FullName `
        -RunId $RunId `
        -AttemptOrdinal $AttemptOrdinal `
        -SourceCommit $SourceCommit `
        -PackageSha256 $PackageSha256 `
        -ProductActionsPath $productActionsPath
}

$expectedRequestId = 1
try {
    while ($true) {
        $line = [Console]::In.ReadLine()
        if ($null -eq $line) {
            break
        }
        if (
            $line.Length -eq 0 -or
            $line.Contains([char]0) -or
            [Text.Encoding]::UTF8.GetByteCount($line) -gt $script:Gate14MaxFrameBytes
        ) {
            throw "RPC frame is invalid"
        }
        try {
            $frame = ConvertFrom-Json -InputObject $line
            if ((ConvertTo-Json -InputObject $frame -Compress -Depth 20) -cne $line) {
                throw "RPC frame is not canonical"
            }
            Assert-Gate14ExactProperties -Value $frame -Names @(
                "binding",
                "operation",
                "payload",
                "request_id",
                "schema_version",
                "scope",
                "session_id"
            )
            if (
                $frame.schema_version -isnot [int] -or
                $frame.schema_version -ne 1 -or
                $frame.scope -isnot [string] -or
                $frame.scope -cne $script:Gate14Scope -or
                $frame.session_id -isnot [string] -or
                $frame.session_id -cne $SessionId -or
                $frame.request_id -isnot [int] -or
                $frame.request_id -ne $expectedRequestId -or
                $frame.operation -isnot [string] -or
                $frame.operation -cnotin @("prepare", "calibrate", "cleanup")
            ) {
                throw "RPC frame binding is invalid"
            }
            Assert-Gate14Binding -Binding $frame.binding
            $operation = [string]$frame.operation
            $requestId = [int]$frame.request_id
            $expectedRequestId += 1

            if ($operation -ceq "prepare") {
                if ($script:Gate14Phase -cne "new") {
                    throw "RPC operation order is invalid"
                }
                Assert-Gate14ExactProperties -Value $frame.payload -Names @()
                if ($TransportSelfTest) {
                    $payload = [ordered]@{
                        helpers_loaded = $true
                        host_process_id = [int]$PID
                        state_nonce = $script:Gate14StateNonce
                    }
                }
                else {
                    try {
                        $payload = Invoke-Gate14WindowsProductPrepare
                    }
                    catch {
                        $script:Gate14Phase = "failed"
                        Write-Gate14Response -RequestId $requestId -Operation $operation -Result "failed" -Payload $null -FailureCode "product-prepare-failed"
                        continue
                    }
                }
                $script:Gate14Phase = "prepared"
                Write-Gate14Response -RequestId $requestId -Operation $operation -Result "passed" -FailureCode $null -Payload $payload
                continue
            }

            if ($operation -ceq "calibrate") {
                if ($script:Gate14Phase -cne "prepared") {
                    throw "RPC operation order is invalid"
                }
                Assert-Gate14ExactProperties -Value $frame.payload -Names @(
                    "challenge_sha256",
                    "controller_state_revision",
                    "issued_at_unix",
                    "expires_at_unix"
                )
                if (
                    $frame.payload.challenge_sha256 -isnot [string] -or
                    $frame.payload.challenge_sha256 -cnotmatch "^sha256:[0-9a-f]{64}$" -or
                    $frame.payload.controller_state_revision -isnot [int] -or
                    $frame.payload.controller_state_revision -lt 0 -or
                    $frame.payload.issued_at_unix -isnot [int] -or
                    $frame.payload.expires_at_unix -isnot [int] -or
                    ($frame.payload.expires_at_unix - $frame.payload.issued_at_unix) -lt 60 -or
                    ($frame.payload.expires_at_unix - $frame.payload.issued_at_unix) -gt 900
                ) {
                    throw "RPC calibration binding is invalid"
                }
                if ($TransportSelfTest) {
                    $payload = [ordered]@{
                        challenge_sha256 = [string]$frame.payload.challenge_sha256
                        host_process_id = [int]$PID
                        state_nonce = $script:Gate14StateNonce
                    }
                }
                else {
                    try {
                        $payload = [ordered]@{
                            suspensions = @(Invoke-Gate14WindowsProductCalibrate -Challenge $frame.payload)
                        }
                    }
                    catch {
                        $script:Gate14Phase = "failed"
                        Write-Gate14Response -RequestId $requestId -Operation $operation -Result "failed" -Payload $null -FailureCode "product-calibration-failed"
                        continue
                    }
                }
                $script:Gate14Phase = "calibrated"
                Write-Gate14Response -RequestId $requestId -Operation $operation -Result "passed" -FailureCode $null -Payload $payload
                continue
            }

            if ($operation -ceq "cleanup") {
                Assert-Gate14ExactProperties -Value $frame.payload -Names @()
                $payload = Invoke-Gate14Cleanup
                $script:Gate14Phase = "cleaned"
                Write-Gate14Response -RequestId $requestId -Operation $operation -Result "passed" -FailureCode $null -Payload $payload
                continue
            }
        }
        catch {
            try {
                $discardedCleanup = Invoke-Gate14Cleanup
                $discardedCleanup = $null
            }
            catch {
            }
            try {
                Write-Gate14Response -RequestId $expectedRequestId -Operation "invalid" -Result "failed" -Payload $null -FailureCode "invalid-action-frame"
            }
            catch {
            }
            break
        }
    }
}
finally {
    $discardedCleanup = Invoke-Gate14Cleanup
    $discardedCleanup = $null
}
