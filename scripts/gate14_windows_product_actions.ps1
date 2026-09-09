# Concrete Gate 14 Windows packaged-product actions.
#
# This module is dot-sourced only after the exact Gate 13 lifecycle and
# inference helpers plus this file have been source-digest verified by the
# persistent Gate 14 action host. Product, Job Object, credential, and cache
# state remain in that one PowerShell process across the controller challenge.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$VerbosePreference = "SilentlyContinue"
$DebugPreference = "SilentlyContinue"
$InformationPreference = "SilentlyContinue"

$script:Gate14ProductInitialized = $false
$script:Gate14ProductPrepared = $false
$script:Gate14ProductCleaned = $false
$script:Gate14ProductCredentialCreated = $false
$script:Gate14ProductConfig = $null
$script:Gate14ProductProfile = $null
$script:Gate14ProductContext = $null
$script:Gate14ProductInventory = $null
$script:Gate14ProductArtifacts = @()
$script:Gate14ProductExpectedPolicy = $null
$script:Gate14ProductWorkerPid = 0
$script:Gate14ProductBaselineProcesses = 0
$script:Gate14ProductActionRoot = ""
$script:Gate14ProductWarmCache = ""
$script:Gate14ProductSourcePath = ""
$script:Gate14ProductBurns = New-Object System.Collections.ArrayList
$script:Gate14ProductCacheLocks = New-Object System.Collections.ArrayList

function Assert-Gate14WindowsProductHelpers {
    foreach ($name in @(
        "Assert-Gate13ExactProperties",
        "Assert-Gate13NoTranscript",
        "Assert-Gate13SameArtifactInventory",
        "ConvertFrom-Gate13Json",
        "Force-Gate13ProductCleanup",
        "Get-Gate13CredentialCount",
        "Get-Gate13ExactWorkerSnapshot",
        "Get-Gate13FileCount",
        "Get-Gate13ProductProcessCount",
        "Get-Gate13Property",
        "Get-Gate13SelectedManifestContext",
        "Get-Gate13SelectedProfile",
        "Get-Gate13StreamSha256",
        "Initialize-Gate13CredentialInterop",
        "Initialize-Gate13NativeHost",
        "Install-Gate13VerifiedPackage",
        "Invoke-Gate13Bootstrap",
        "Invoke-Gate13Contained",
        "Invoke-Gate13LoopbackJson",
        "Read-Gate13ControlKey",
        "Read-Gate13JsonFile",
        "Stop-Gate13Product",
        "Test-Gate13PackageAudit",
        "Test-Gate13PackagedSelfTests",
        "Test-Gate13SafeArtifactPath",
        "Wait-Gate13ProductStatus"
    )) {
        if ($null -eq (Get-Command $name -CommandType Function -ErrorAction SilentlyContinue)) {
            throw "Gate 13 product helper binding is incomplete"
        }
    }
}

function Get-Gate14WindowsUnixSeconds {
    return [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
}

function Test-Gate14WindowsInteger {
    param([object]$Value)
    return ($Value -is [int] -or $Value -is [long])
}

function Test-Gate14WindowsActiveWorker {
    param([Parameter(Mandatory = $true)][object]$Worker)
    $state = Get-Gate13Property $Worker "state"
    return ($state -cin @("starting", "running", "stopping"))
}

function Assert-Gate14WindowsFreshDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw ($Label + " is unavailable")
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw ($Label + " is unsafe")
    }
}

function Write-Gate14WindowsUtf8New {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )
    if (Test-Path -LiteralPath $Path) {
        throw "temporary product input already exists"
    }
    $bytes = (New-Object Text.UTF8Encoding($false, $true)).GetBytes($Text)
    $stream = $null
    try {
        $stream = New-Object IO.FileStream(
            $Path,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None,
            4096,
            [IO.FileOptions]::WriteThrough
        )
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Write-Gate14WindowsAtomicBytes {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][byte[]]$Bytes
    )
    $directory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path))
    $temporary = Join-Path $directory (".gate14-" + [Guid]::NewGuid().ToString("N") + ".tmp")
    $stream = $null
    try {
        $stream = New-Object IO.FileStream(
            $temporary,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None,
            4096,
            [IO.FileOptions]::WriteThrough
        )
        $stream.Write($Bytes, 0, $Bytes.Length)
        $stream.Flush($true)
        $stream.Dispose()
        $stream = $null
        Move-Item -LiteralPath $temporary -Destination $Path -Force -ErrorAction Stop
    }
    finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-Gate14WindowsPackageVersion {
    $metricsPath = Join-Path $script:LifecycleAuditRoot "desktop-metrics.json"
    $metrics = Read-Gate13JsonFile -Path $metricsPath
    $node = Get-Gate13Property $metrics "node_sidecar"
    $runtime = Get-Gate13Property $node "runtime"
    $version = Get-Gate13Property $runtime "drift"
    if (-not ($version -is [string]) -or $version -notmatch "^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$") {
        throw "package version binding is invalid"
    }
    return $version
}

function New-Gate14WindowsRunInput {
    $value = [ordered]@{
        schema_version = 1
        run_id = [string](Get-Gate13Property $script:Gate14ProductConfig "run_id")
        source_commit = [string](Get-Gate13Property $script:Gate14ProductConfig "source_commit")
        package_version = Get-Gate14WindowsPackageVersion
        package_sha256 = ([string](Get-Gate13Property $script:Gate14ProductConfig "package_sha256")).Substring(7)
        package_bytes = [int64](Get-Gate13Property $script:Gate14ProductConfig "package_bytes")
        model_id = [string](Get-Gate13Property $script:Gate14ProductConfig "model_id")
        manifest_digest = ([string](Get-Gate13Property $script:Gate14ProductConfig "manifest_digest")).Substring(7)
    }
    $rendered = ConvertTo-Json -InputObject $value -Compress -Depth 8
    Write-Gate14WindowsUtf8New -Path $script:LifecycleRunInput -Text $rendered
}

function Get-Gate14WindowsArtifactRecords {
    $warm = Get-Gate13Property $script:Gate14ProductConfig "warm_cache"
    $raw = @((Get-Gate13Property $warm "artifacts"))
    if ($raw.Count -lt 1) {
        throw "warm cache artifact inventory is absent"
    }
    $records = New-Object System.Collections.ArrayList
    $seen = New-Object "System.Collections.Generic.HashSet[string]" ([StringComparer]::OrdinalIgnoreCase)
    foreach ($item in $raw) {
        Assert-Gate13ExactProperties -InputObject $item -Names @(
            "path", "role", "sha256", "size_bytes"
        )
        $relative = Get-Gate13Property $item "path"
        $role = Get-Gate13Property $item "role"
        $digest = Get-Gate13Property $item "sha256"
        $size = Get-Gate13Property $item "size_bytes"
        if (
            -not ($relative -is [string]) -or
            -not (Test-Gate13SafeArtifactPath -Path $relative) -or
            -not $seen.Add($relative) -or
            -not ($role -is [string]) -or
            $role -notmatch "^[a-z][a-z0-9_-]{0,31}$" -or
            -not ($digest -is [string]) -or
            $digest -notmatch "^sha256:[0-9a-f]{64}$" -or
            -not (Test-Gate14WindowsInteger $size) -or
            [int64]$size -lt 1
        ) {
            throw "warm cache artifact inventory is invalid"
        }
        [void]$records.Add([pscustomobject]@{
            path = $relative
            role = $role
            sha256 = $digest.Substring(7)
            size_bytes = [int64]$size
        })
    }
    return @($records)
}

function New-Gate14WindowsProfile {
    $modelId = [string](Get-Gate13Property $script:Gate14ProductConfig "model_id")
    $manifest = [string](Get-Gate13Property $script:Gate14ProductConfig "manifest_digest")
    if ($modelId -cne "Qwen3.5 2B" -or $manifest -cne "sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33") {
        throw "Windows product model binding changed"
    }
    $base = $script:Profiles[$modelId]
    if (
        $null -eq $base -or
        $base.ManifestDigest -cne $manifest.Substring(7) -or
        [int]$base.SelectedCount -ne 8 -or
        [int64]$base.SelectedBytes -ne 4571197320
    ) {
        throw "Windows product profile binding changed"
    }
    return [pscustomobject]@{
        ModelId = $modelId
        ManifestDigest = $base.ManifestDigest
        RevisionCommit = $base.RevisionCommit
        SelectedCount = [int]$base.SelectedCount
        SelectedBytes = [int64]$base.SelectedBytes
        TotalBlocks = 24
        Gate9EnvelopeSha256 = "sha256:cd68afb67d9b0f3cb8c82db0d3314ad89b558c20880998ea4d8c4493e9f4bc9f"
    }
}

function Initialize-Gate14WindowsProductActions {
    param(
        [Parameter(Mandatory = $true)][string]$LifecycleConfig,
        [Parameter(Mandatory = $true)][string]$RunId,
        [Parameter(Mandatory = $true)][int]$AttemptOrdinal,
        [Parameter(Mandatory = $true)][string]$SourceCommit,
        [Parameter(Mandatory = $true)][string]$PackageSha256,
        [Parameter(Mandatory = $true)][string]$ProductActionsPath
    )
    if ($script:Gate14ProductInitialized) {
        throw "Windows product actions are already initialized"
    }
    Assert-Gate14WindowsProductHelpers
    Assert-Gate13NoTranscript
    Initialize-Gate13NativeHost
    Initialize-Gate13CredentialInterop
    $config = Read-Gate13JsonFile -Path $LifecycleConfig
    foreach ($field in @(
        "run_id", "attempt_ordinal", "source_commit", "package_sha256", "platform",
        "model_id", "manifest_digest", "work_root", "staging_root", "package_path",
        "package_bytes", "disk_bytes", "vram_bytes", "bandwidth_mbps",
        "power_watts", "pause_timeout_seconds", "sample_interval_seconds", "warm_cache"
    )) {
        $discarded = Get-Gate13Property $config $field
        $discarded = $null
    }
    if (
        (Get-Gate13Property $config "run_id") -cne $RunId -or
        [int](Get-Gate13Property $config "attempt_ordinal") -ne $AttemptOrdinal -or
        (Get-Gate13Property $config "source_commit") -cne $SourceCommit -or
        (Get-Gate13Property $config "package_sha256") -cne $PackageSha256 -or
        (Get-Gate13Property $config "platform") -cne "windows"
    ) {
        throw "Windows product action binding changed"
    }
    $sourcePath = [IO.Path]::GetFullPath($ProductActionsPath)
    if (
        [IO.Path]::GetFileName($sourcePath) -cne "gate14_windows_product_actions.ps1" -or
        -not (Test-Path -LiteralPath $sourcePath -PathType Leaf)
    ) {
        throw "Windows product action source identity is invalid"
    }

    $workRoot = [IO.Path]::GetFullPath([string](Get-Gate13Property $config "work_root"))
    $stagingRoot = [IO.Path]::GetFullPath([string](Get-Gate13Property $config "staging_root"))
    $packagePath = [IO.Path]::GetFullPath([string](Get-Gate13Property $config "package_path"))
    $actionRoot = [IO.Path]::GetFullPath((Join-Path $workRoot "gate14-product-action"))
    $warmCache = [IO.Path]::GetFullPath((Join-Path $workRoot "gate14-warm-cache"))
    if (
        $workRoot -ceq [IO.Path]::GetPathRoot($workRoot) -or
        $actionRoot -ceq $workRoot -or
        -not $actionRoot.StartsWith($workRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
        -not $warmCache.StartsWith($workRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
        $stagingRoot.StartsWith($workRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
        $workRoot.StartsWith($stagingRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $packagePath -PathType Leaf)
    ) {
        throw "Windows product path binding is unsafe"
    }

    $script:Gate14ProductConfig = $config
    $script:Gate14ProductSourcePath = $sourcePath
    $script:Gate14ProductActionRoot = $actionRoot
    $script:Gate14ProductWarmCache = $warmCache
    $script:LifecycleArchive = $packagePath
    $script:LifecycleAuditRoot = Join-Path $stagingRoot "release-audit"
    $script:LifecycleRunInput = Join-Path $actionRoot "gate13-windows-run.json"
    $script:LifecycleController = $sourcePath
    $script:LifecycleWorkRoot = $actionRoot
    $script:LifecycleInstallRoot = Join-Path $actionRoot "install"
    $script:LifecycleProductRoot = Join-Path $script:LifecycleInstallRoot "CommunityAI"
    $script:LifecycleDesktopExe = Join-Path $script:LifecycleProductRoot "CommunityAI.exe"
    $script:LifecycleNodeExe = Join-Path $script:LifecycleProductRoot "node\CommunityAI-Node.exe"
    $script:LifecycleBootstrap = Join-Path $script:LifecycleProductRoot "_internal\bootstrap\catalog-bootstrap.json"
    $script:LifecyclePersistentRoot = Join-Path $actionRoot "persistent"
    $script:LifecycleNodeConfig = Join-Path $script:LifecyclePersistentRoot "node-config.json"
    $script:LifecycleProcess = $null
    $script:LifecycleOwnWorkRoot = $false
    $script:LifecycleOwnPersistentRoot = $false
    $script:Gate14ProductProfile = New-Gate14WindowsProfile
    $script:Gate14ProductArtifacts = @(Get-Gate14WindowsArtifactRecords)
    $script:Gate14ProductInitialized = $true
}

function Get-Gate14WindowsProductArguments {
    if (
        -not (Test-Path -LiteralPath $script:LifecycleNodeConfig -PathType Leaf) -or
        -not (Test-Path -LiteralPath $script:LifecyclePersistentRoot -PathType Container) -or
        -not (Test-Path -LiteralPath $script:LifecycleBootstrap -PathType Leaf)
    ) {
        throw "Windows product launch paths are unavailable"
    }
    return [string[]]@(
        "--node-config",
        $script:LifecycleNodeConfig,
        "--node-data-dir",
        $script:LifecyclePersistentRoot,
        "--bootstrap-config",
        $script:LifecycleBootstrap
    )
}

function New-Gate14WindowsContainedProductProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )
    return [Gate13.NativeHost]::Start($Executable, $Arguments, $WorkingDirectory)
}

function Start-Gate14WindowsProduct {
    if ($null -ne $script:LifecycleProcess) {
        throw "product already running"
    }
    Initialize-Gate13NativeHost
    $arguments = Get-Gate14WindowsProductArguments
    try {
        $script:LifecycleProcess = New-Gate14WindowsContainedProductProcess `
            -Executable $script:LifecycleDesktopExe `
            -Arguments $arguments `
            -WorkingDirectory $script:LifecycleProductRoot
    }
    finally {
        $arguments = $null
    }
}

function Get-Gate14WindowsFullSchedule {
    return [ordered]@{
        timezone = "UTC"
        windows = @(
            [ordered]@{
                days = @("mon", "tue", "wed", "thu", "fri", "sat", "sun")
                start = "00:00"
                end = "23:59"
            }
        )
    }
}

function Get-Gate14WindowsClosedSchedule {
    $names = @("sun", "mon", "tue", "wed", "thu", "fri", "sat")
    $tomorrow = ([int][DateTime]::UtcNow.DayOfWeek + 1) % 7
    return [ordered]@{
        timezone = "UTC"
        windows = @(
            [ordered]@{
                days = @($names[$tomorrow])
                start = "00:00"
                end = "00:01"
            }
        )
    }
}

function Set-Gate14WindowsContributionPolicy {
    param(
        [Parameter(Mandatory = $true)][string]$ControlToken,
        [int64]$VramBytes = -1,
        [object]$Schedule = $null
    )
    if ($VramBytes -lt 0) {
        $VramBytes = [int64](Get-Gate13Property $script:Gate14ProductConfig "vram_bytes")
    }
    if ($null -eq $Schedule) {
        $Schedule = Get-Gate14WindowsFullSchedule
    }
    $snapshot = Invoke-Gate13LoopbackJson -Method "GET" -Path "/control/v1/contribution-policy" -BearerToken $ControlToken
    Assert-Gate13ExactProperties -InputObject $snapshot -Names @(
        "schema_version", "config_revision", "policy"
    )
    $revision = Get-Gate13Property $snapshot "config_revision"
    if ((Get-Gate13Property $snapshot "schema_version") -ne 1 -or -not ($revision -is [string])) {
        throw "contribution policy snapshot is invalid"
    }
    $policy = [ordered]@{
        sharing_enabled = $true
        allowed_models = @($script:Gate14ProductProfile.ModelId)
        preferred_models = @($script:Gate14ProductProfile.ModelId)
        denied_models = @()
        max_disk_space = ([int64](Get-Gate13Property $script:Gate14ProductConfig "disk_bytes")).ToString() + "B"
        max_vram = $VramBytes.ToString() + "B"
        max_bandwidth_mbps = [double](Get-Gate13Property $script:Gate14ProductConfig "bandwidth_mbps")
        max_power_watts = [double](Get-Gate13Property $script:Gate14ProductConfig "power_watts")
        pause_timeout = [double](Get-Gate13Property $script:Gate14ProductConfig "pause_timeout_seconds")
        schedule = $Schedule
    }
    $response = Invoke-Gate13LoopbackJson -Method "PUT" -Path "/control/v1/contribution-policy" -BearerToken $ControlToken -Body ([ordered]@{
        schema_version = 1
        expected_config_revision = $revision
        policy = $policy
    })
    Assert-Gate13ExactProperties -InputObject $response -Names @(
        "schema_version", "config_revision", "policy"
    )
    if (
        (Get-Gate13Property $response "schema_version") -ne 1 -or
        ((Get-Gate13Property $response "policy") | ConvertTo-Json -Compress -Depth 16) -cne
            ($policy | ConvertTo-Json -Compress -Depth 16)
    ) {
        throw "contribution policy update was not preserved"
    }
    $script:Gate14ProductExpectedPolicy = $policy
    return $response
}

function Get-Gate14WindowsExactWorker {
    param(
        [Parameter(Mandatory = $true)][string]$ControlToken,
        [switch]$Running
    )
    $snapshot = Get-Gate13ExactWorkerSnapshot -ControlToken $ControlToken
    $workers = @($snapshot.Workers)
    $automatic = $snapshot.Automatic
    $active = @($workers | Where-Object { Test-Gate14WindowsActiveWorker $_ })
    if ($Running -and ($active.Count -ne 1 -or $active[0] -ne $automatic)) {
        throw "automatic worker identity is invalid"
    }
    if ($Running) {
        $pidValue = Get-Gate13Property $automatic "pid"
        if (
            (Get-Gate13Property $automatic "state") -cne "running" -or
            (Get-Gate13Property $automatic "desired_running") -ne $true -or
            (Get-Gate13Property $automatic "model") -cne $script:Gate14ProductProfile.ModelId -or
            (Get-Gate13Property $automatic "intent_published") -ne $true -or
            (Get-Gate13Property $automatic "remote_acknowledged") -ne $true -or
            -not (Test-Gate14WindowsInteger $pidValue) -or
            [int64]$pidValue -lt 1 -or
            $null -eq $script:LifecycleProcess -or
            -not $script:LifecycleProcess.ContainsProcessId([int]$pidValue)
        ) {
            throw "automatic worker is not running with acknowledged owned intent"
        }
    }
    return $automatic
}

function Get-Gate14WindowsPublicWorker {
    param([Parameter(Mandatory = $true)][string]$ControlToken)
    $status = Invoke-Gate13LoopbackJson -Method "GET" -Path "/control/v1/status" -BearerToken $ControlToken
    $profile = Get-Gate13SelectedProfile -Status $status
    if (
        $profile.ModelId -cne $script:Gate14ProductProfile.ModelId -or
        $profile.ManifestDigest -cne $script:Gate14ProductProfile.ManifestDigest
    ) {
        throw "public product model identity changed"
    }
    $contribution = Get-Gate13Property $status "contribution"
    if ((Get-Gate13Property $contribution "schema_version") -ne 3) {
        throw "public contribution status is invalid"
    }
    $automatic = @(@((Get-Gate13Property $contribution "workers")) | Where-Object {
        (Get-Gate13Property $_ "id") -ceq "automatic"
    })
    if ($automatic.Count -ne 1) {
        throw "public automatic worker status is invalid"
    }
    return $automatic[0]
}

function Wait-Gate14WindowsRunning {
    param(
        [Parameter(Mandatory = $true)][string]$ControlToken,
        [int]$TimeoutSeconds = 300
    )
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $last = $null
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        try {
            return Get-Gate14WindowsExactWorker -ControlToken $ControlToken -Running
        }
        catch {
            $last = $_
            Start-Sleep -Milliseconds 250
        }
    }
    throw "automatic worker did not reach the required state"
}

function Wait-Gate14WindowsInactive {
    param(
        [Parameter(Mandatory = $true)][string]$ControlToken,
        [switch]$Resource,
        [switch]$Schedule,
        [int]$PriorPid = 0,
        [int]$TimeoutSeconds = 300
    )
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        try {
            $private = Get-Gate14WindowsExactWorker -ControlToken $ControlToken
            $public = Get-Gate14WindowsPublicWorker -ControlToken $ControlToken
            $valid = (
                (Get-Gate13Property $private "desired_running") -eq $true -and
                $null -eq (Get-Gate13Property $private "pid") -and
                -not (Test-Gate14WindowsActiveWorker $private) -and
                (Get-Gate13Property $public "desired_running") -eq $true -and
                -not (Test-Gate14WindowsActiveWorker $public)
            )
            if ($Resource) {
                $valid = $valid -and ((Get-Gate13Property $private "resource_suspended") -eq $true)
            }
            if ($Schedule) {
                $valid = $valid -and ((Get-Gate13Property $private "schedule_suspended") -eq $true)
            }
            if ($PriorPid -gt 0 -and $null -ne (Get-Process -Id $PriorPid -ErrorAction SilentlyContinue)) {
                $valid = $false
            }
            if ($valid) {
                return [pscustomobject]@{ Private = $private; Public = $public }
            }
        }
        catch {
        }
        Start-Sleep -Milliseconds 250
    }
    throw "worker suspension did not reach the required state"
}

function Invoke-Gate14WindowsLowVramProbe {
    param([Parameter(Mandatory = $true)][string]$ControlToken)
    $before = Get-Gate14WindowsExactWorker -ControlToken $ControlToken -Running
    $priorPid = [int](Get-Gate13Property $before "pid")
    $discarded = Set-Gate14WindowsContributionPolicy -ControlToken $ControlToken -VramBytes 1
    $discarded = $null
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt 300) {
        $worker = Get-Gate14WindowsExactWorker -ControlToken $ControlToken
        if (
            $null -eq (Get-Gate13Property $worker "pid") -and
            (Get-Gate13Property $worker "resource_admitted") -eq $false -and
            $null -eq (Get-Process -Id $priorPid -ErrorAction SilentlyContinue)
        ) {
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if ($timer.Elapsed.TotalSeconds -ge 300) {
        throw "low VRAM policy was not rejected"
    }
    $discarded = Set-Gate14WindowsContributionPolicy -ControlToken $ControlToken
    $discarded = $null
    $worker = Wait-Gate14WindowsRunning -ControlToken $ControlToken
    $script:Gate14ProductWorkerPid = [int](Get-Gate13Property $worker "pid")
}

function Set-Gate14WindowsNodeConfigBytes {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    Write-Gate14WindowsAtomicBytes -Path $script:LifecycleNodeConfig -Bytes $Bytes
}

function Invoke-Gate14WindowsCpuPowerProbe {
    param([Parameter(Mandatory = $true)][string]$ControlToken)
    $original = [IO.File]::ReadAllBytes($script:LifecycleNodeConfig)
    try {
        $utf8 = New-Object Text.UTF8Encoding($false, $true)
        $config = ConvertFrom-Gate13Json -Payload $utf8.GetString($original)
        $automatic = @(@((Get-Gate13Property $config "workers")) | Where-Object {
            (Get-Gate13Property $_ "id") -ceq "automatic"
        })
        if ($automatic.Count -ne 1) {
            throw "automatic worker configuration is invalid"
        }
        $automatic[0].device = "cpu"
        $changedText = ConvertTo-Json -InputObject $config -Compress -Depth 32
        $changed = $utf8.GetBytes($changedText)
        try {
            Set-Gate14WindowsNodeConfigBytes -Bytes $changed
        }
        finally {
            [Array]::Clear($changed, 0, $changed.Length)
        }
        $timer = [Diagnostics.Stopwatch]::StartNew()
        while ($timer.Elapsed.TotalSeconds -lt 300) {
            $worker = Get-Gate14WindowsExactWorker -ControlToken $ControlToken
            $reason = Get-Gate13Property $worker "resource_reason"
            if (
                $null -eq (Get-Gate13Property $worker "pid") -and
                (Get-Gate13Property $worker "resource_admitted") -eq $false -and
                $reason -is [string] -and
                $reason.Contains("power telemetry is unavailable")
            ) {
                break
            }
            Start-Sleep -Milliseconds 250
        }
        if ($timer.Elapsed.TotalSeconds -ge 300) {
            throw "CPU power telemetry was not rejected"
        }
    }
    finally {
        Set-Gate14WindowsNodeConfigBytes -Bytes $original
        [Array]::Clear($original, 0, $original.Length)
    }
    $worker = Wait-Gate14WindowsRunning -ControlToken $ControlToken
    $script:Gate14ProductWorkerPid = [int](Get-Gate13Property $worker "pid")
    return [ordered]@{
        device = "cpu"
        configured_limit = "power_watts"
        start_rejected = $true
        reason_code = "power-telemetry-unavailable"
        private_detail_retained = $false
    }
}

function Invoke-Gate14WindowsCrashRecovery {
    param([Parameter(Mandatory = $true)][string]$ControlToken)
    $before = Get-Gate14WindowsExactWorker -ControlToken $ControlToken -Running
    $oldPid = [int](Get-Gate13Property $before "pid")
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $script:LifecycleProcess.KillMemberProcess($oldPid, 30000)
    while ($timer.Elapsed.TotalSeconds -lt 300) {
        try {
            $after = Get-Gate14WindowsExactWorker -ControlToken $ControlToken -Running
            $newPid = [int](Get-Gate13Property $after "pid")
            if ($newPid -ne $oldPid -and $null -eq (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
                $script:Gate14ProductWorkerPid = $newPid
                return [ordered]@{
                    worker_crash_observed = $true
                    worker_restarted = $true
                    restart_seconds = [Math]::Round($timer.Elapsed.TotalSeconds, 6)
                    previous_worker_absent = $true
                    manifest_unchanged = ((Get-Gate13Property $after "model") -ceq $script:Gate14ProductProfile.ModelId)
                    automatic_block_range_valid = ((Get-Gate13Property $after "block_indices") -is [string])
                    desired_intent_preserved = ((Get-Gate13Property $after "desired_running") -eq $true)
                }
            }
        }
        catch {
        }
        Start-Sleep -Milliseconds 250
    }
    throw "worker crash recovery did not complete"
}

function Invoke-Gate14WindowsPause {
    param([Parameter(Mandatory = $true)][string]$ControlToken)
    $before = Get-Gate14WindowsExactWorker -ControlToken $ControlToken -Running
    $oldPid = [int](Get-Gate13Property $before "pid")
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $discarded = Invoke-Gate13LoopbackJson -Method "POST" -Path "/control/v1/workers/automatic/pause" -BearerToken $ControlToken
    $discarded = $null
    while ($timer.Elapsed.TotalSeconds -lt 300) {
        $worker = Get-Gate14WindowsExactWorker -ControlToken $ControlToken
        if (
            (Get-Gate13Property $worker "state") -ceq "paused" -and
            (Get-Gate13Property $worker "desired_running") -eq $false -and
            (Get-Gate13Property $worker "operator_paused") -eq $true -and
            $null -eq (Get-Gate13Property $worker "pid") -and
            $null -eq (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) -and
            $script:LifecycleProcess.ActiveProcessCount -eq $script:Gate14ProductBaselineProcesses
        ) {
            $result = [ordered]@{
                requested = $true
                completed = $true
                duration_seconds = [Math]::Round($timer.Elapsed.TotalSeconds, 6)
                worker_count_after = 0
                descendant_count_after = 0
            }
            $discarded = Invoke-Gate13LoopbackJson -Method "POST" -Path "/control/v1/workers/automatic/start" -BearerToken $ControlToken
            $discarded = $null
            $running = Wait-Gate14WindowsRunning -ControlToken $ControlToken
            $script:Gate14ProductWorkerPid = [int](Get-Gate13Property $running "pid")
            return $result
        }
        Start-Sleep -Milliseconds 250
    }
    throw "automatic worker did not pause"
}

function Invoke-Gate14WindowsRestart {
    param([Parameter(Mandatory = $true)][string]$ControlToken)
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $before = Get-Gate14WindowsExactCacheInventory -Root $script:Gate14ProductContext.CacheDir -Context $script:Gate14ProductContext
    Stop-Gate13Product
    Start-Gate14WindowsProduct
    $status = Wait-Gate13ProductStatus -TimeoutSeconds 300
    $running = Wait-Gate14WindowsRunning -ControlToken $ControlToken
    $policy = Invoke-Gate13LoopbackJson -Method "GET" -Path "/control/v1/contribution-policy" -BearerToken $ControlToken
    if (
        ((Get-Gate13Property $policy "policy") | ConvertTo-Json -Compress -Depth 16) -cne
            ($script:Gate14ProductExpectedPolicy | ConvertTo-Json -Compress -Depth 16)
    ) {
        throw "restart did not preserve policy"
    }
    $after = Get-Gate14WindowsExactCacheInventory -Root $script:Gate14ProductContext.CacheDir -Context $script:Gate14ProductContext
    Assert-Gate14WindowsSameCacheInventory -Expected $before -Actual $after
    $script:Gate14ProductWorkerPid = [int](Get-Gate13Property $running "pid")
    return [ordered]@{
        node_restarted = $true
        policy_persisted = $true
        desired_intent_persisted = ((Get-Gate13Property $running "desired_running") -eq $true)
        worker_resumed = $true
        duration_seconds = [Math]::Round($timer.Elapsed.TotalSeconds, 6)
        cache_reused = $true
    }
}

function Invoke-Gate14WindowsExactCacheInventoryCore {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][object]$Context,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][Collections.ArrayList]$Opened
    )
    $rootHandle = [Gate13.NativeHost]::OpenReadOnlyNoFollow(
        [IO.Path]::GetFullPath($Root),
        $true
    )
    [void]$Opened.Add($rootHandle)
    $manifestDigest = Get-Gate13Property $Context "ManifestDigest"
    if (-not ($manifestDigest -is [string]) -or $manifestDigest -notmatch "^[0-9a-f]{64}$") {
        throw "materialized cache manifest identity is invalid"
    }

    $prefix = "manifest-artifacts/" + $manifestDigest + "/snapshot"
    $expectedFiles = New-Object "System.Collections.Generic.Dictionary[string,object]" (
        [StringComparer]::Ordinal
    )
    $expectedDirectories = New-Object "System.Collections.Generic.HashSet[string]" (
        [StringComparer]::Ordinal
    )
    $expectedPaths = New-Object "System.Collections.Generic.HashSet[string]" (
        [StringComparer]::OrdinalIgnoreCase
    )
    [int64]$expectedBytes = 0
    foreach ($record in @($script:Gate14ProductArtifacts)) {
        $artifactRelative = Get-Gate13Property $record "path"
        $cacheRelative = $prefix + "/" + $artifactRelative
        if ($expectedFiles.ContainsKey($cacheRelative) -or -not $expectedPaths.Add($cacheRelative)) {
            throw "materialized cache expected inventory collides"
        }
        $expectedFiles.Add($cacheRelative, $record)
        $segments = $cacheRelative.Split("/")
        for ($index = 1; $index -lt $segments.Count; $index++) {
            $parent = [string]::Join("/", [string[]]$segments[0..($index - 1)])
            if ($expectedDirectories.Add($parent) -and -not $expectedPaths.Add($parent)) {
                throw "materialized cache expected inventory collides"
            }
        }
        $expectedBytes = [int64]($expectedBytes + [int64](Get-Gate13Property $record "size_bytes"))
    }
    if (
        $expectedFiles.Count -ne [int]$script:Gate14ProductProfile.SelectedCount -or
        $expectedBytes -ne [int64]$script:Gate14ProductProfile.SelectedBytes
    ) {
        throw "materialized cache expected totals changed"
    }

    $pending = New-Object Collections.Stack
    $pending.Push([pscustomobject]@{ FullPath = [IO.Path]::GetFullPath($Root); RelativePath = "" })
    $seenPaths = New-Object "System.Collections.Generic.HashSet[string]" (
        [StringComparer]::OrdinalIgnoreCase
    )
    $seenDirectories = New-Object "System.Collections.Generic.HashSet[string]" (
        [StringComparer]::Ordinal
    )
    $seenFiles = New-Object "System.Collections.Generic.HashSet[string]" (
        [StringComparer]::Ordinal
    )
    $entries = New-Object Collections.ArrayList
    [int64]$actualBytes = 0

    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        foreach ($item in @(Get-ChildItem -LiteralPath $current.FullPath -Force -ErrorAction Stop)) {
            $itemIsDirectory = $item -is [IO.DirectoryInfo]
            try {
                $lockedItem = [Gate13.NativeHost]::OpenReadOnlyNoFollow(
                    $item.FullName,
                    $itemIsDirectory
                )
            }
            catch {
                throw "materialized cache contains a reparse point or changed entry"
            }
            [void]$Opened.Add($lockedItem)
            $relative = if ($current.RelativePath.Length -eq 0) {
                $item.Name
            }
            else {
                $current.RelativePath + "/" + $item.Name
            }
            if (-not (Test-Gate13SafeArtifactPath -Path $relative) -or -not $seenPaths.Add($relative)) {
                throw "materialized cache path is unsafe or colliding"
            }

            if ($lockedItem.IsDirectory) {
                if (-not $expectedDirectories.Contains($relative) -or -not $seenDirectories.Add($relative)) {
                    throw "materialized cache contains an unexpected directory"
                }
                $pending.Push([pscustomobject]@{
                    FullPath = $item.FullName
                    RelativePath = $relative
                })
                continue
            }
            if ($itemIsDirectory -or $lockedItem.IsDirectory) {
                throw "materialized cache contains a special entry"
            }
            if (-not $expectedFiles.ContainsKey($relative) -or -not $seenFiles.Add($relative)) {
                throw "materialized cache contains an unexpected file"
            }

            $record = $expectedFiles[$relative]
            $expectedSize = [int64](Get-Gate13Property $record "size_bytes")
            $expectedDigest = [string](Get-Gate13Property $record "sha256")
            $openedLength = [int64]$lockedItem.Length
            $actualDigest = $lockedItem.Sha256()
            if (
                $openedLength -ne $expectedSize -or
                $actualDigest -cne $expectedDigest
            ) {
                throw "materialized cache artifact verification failed"
            }
            [void]$entries.Add([pscustomobject]@{
                RelativePath = [string](Get-Gate13Property $record "path")
                Size = $expectedSize
                Digest = $expectedDigest
                FileIdentity = $lockedItem.FileIdentity
                LocalPath = $item.FullName
            })
            $actualBytes = [int64]($actualBytes + $expectedSize)
        }
    }

    if (
        $seenFiles.Count -ne $expectedFiles.Count -or
        $seenDirectories.Count -ne $expectedDirectories.Count -or
        $actualBytes -ne $expectedBytes
    ) {
        throw "materialized cache inventory is incomplete"
    }
    return [pscustomobject]@{
        Entries = @($entries | Sort-Object RelativePath)
        Count = $entries.Count
        Bytes = $actualBytes
    }
}

function Close-Gate14WindowsCacheLocks {
    foreach ($locked in @($script:Gate14ProductCacheLocks)) {
        $locked.Dispose()
    }
    $script:Gate14ProductCacheLocks = New-Object System.Collections.ArrayList
}

function Get-Gate14WindowsExactCacheInventory {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][object]$Context,
        [switch]$HoldLocks
    )
    Assert-Gate14WindowsFreshDirectory -Path $Root -Label "materialized cache"
    Initialize-Gate13NativeHost
    $opened = New-Object Collections.ArrayList
    try {
        $result = Invoke-Gate14WindowsExactCacheInventoryCore `
            -Root $Root `
            -Context $Context `
            -Opened $opened
        if ($HoldLocks) {
            $previous = $script:Gate14ProductCacheLocks
            $script:Gate14ProductCacheLocks = $opened
            $opened = New-Object Collections.ArrayList
            foreach ($locked in @($previous)) {
                $locked.Dispose()
            }
        }
        return $result
    }
    finally {
        foreach ($locked in @($opened)) {
            $locked.Dispose()
        }
    }
}

function Assert-Gate14WindowsSameCacheInventory {
    param(
        [Parameter(Mandatory = $true)][object]$Expected,
        [Parameter(Mandatory = $true)][object]$Actual
    )
    Assert-Gate13SameArtifactInventory -Expected $Expected -Actual $Actual
    for ($index = 0; $index -lt $Expected.Count; $index++) {
        if (
            $Expected.Entries[$index].FileIdentity -cne
                $Actual.Entries[$index].FileIdentity
        ) {
            throw "verified cache file identity changed"
        }
    }
}

function Move-Gate14WindowsWarmCache {
    Assert-Gate14WindowsFreshDirectory -Path $script:Gate14ProductWarmCache -Label "fresh materialized cache"
    $destination = $script:Gate14ProductContext.CacheDir
    $parent = [IO.Path]::GetDirectoryName($destination)
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force -ErrorAction Stop | Out-Null
    }
    if (Test-Path -LiteralPath $destination) {
        Assert-Gate14WindowsFreshDirectory -Path $destination -Label "selected cache"
        if ((Get-Gate13FileCount $destination) -ne 0) {
            throw "selected cache is not empty"
        }
        Remove-Item -LiteralPath $destination -Force -ErrorAction Stop
    }
    [IO.Directory]::Move($script:Gate14ProductWarmCache, $destination)
}

function Invoke-Gate14WindowsPrepareCore {
    Assert-Gate13NoTranscript
    if ($script:Gate14ProductPrepared -or $script:Gate14ProductCleaned) {
        throw "Windows product prepare order is invalid"
    }
    if (Test-Path -LiteralPath $script:Gate14ProductActionRoot) {
        throw "Windows product action root is not fresh"
    }
    Assert-Gate14WindowsFreshDirectory -Path $script:Gate14ProductWarmCache -Label "fresh materialized cache"
    if ((Get-Gate13CredentialCount) -ne 0 -or (Get-Gate13ProductProcessCount) -ne 0) {
        throw "clean host product baseline is not empty"
    }

    New-Item -ItemType Directory -Path $script:Gate14ProductActionRoot -ErrorAction Stop | Out-Null
    $script:LifecycleOwnWorkRoot = $true
    $script:LifecycleOwnPersistentRoot = $true
    New-Gate14WindowsRunInput
    $audit = Test-Gate13PackageAudit
    if (
        $audit.SourceCommit -cne (Get-Gate13Property $script:Gate14ProductConfig "source_commit") -or
        $audit.PackageDigest -cne ([string](Get-Gate13Property $script:Gate14ProductConfig "package_sha256")).Substring(7)
    ) {
        throw "package source binding changed"
    }
    Install-Gate13VerifiedPackage -Audit $audit
    $selfTests = Test-Gate13PackagedSelfTests
    if ((Get-Gate13CredentialCount) -ne 0) {
        throw "packaged self-test retained credential"
    }
    $bootstrap = Invoke-Gate13Bootstrap
    if ((Get-Gate13CredentialCount) -ne 0) {
        throw "packaged bootstrap unexpectedly retained a credential"
    }
    $script:Gate14ProductContext = Get-Gate13SelectedManifestContext -Profile $script:Gate14ProductProfile
    $warmInventory = Get-Gate14WindowsExactCacheInventory -Root $script:Gate14ProductWarmCache -Context $script:Gate14ProductContext
    Move-Gate14WindowsWarmCache
    $script:Gate14ProductInventory = Get-Gate14WindowsExactCacheInventory -Root $script:Gate14ProductContext.CacheDir -Context $script:Gate14ProductContext -HoldLocks
    Assert-Gate14WindowsSameCacheInventory -Expected $warmInventory -Actual $script:Gate14ProductInventory
    $warmInventory = $null

    Start-Gate14WindowsProduct
    $status = Wait-Gate13ProductStatus -TimeoutSeconds 300
    if ((Get-Gate13CredentialCount) -ne 1) {
        throw "packaged product credential was not created exactly once"
    }
    $script:Gate14ProductCredentialCreated = $true
    if (
        $status.Profile.ModelId -cne $script:Gate14ProductProfile.ModelId -or
        $status.Profile.ManifestDigest -cne $script:Gate14ProductProfile.ManifestDigest
    ) {
        throw "packaged product model identity changed"
    }

    $control = [string]$status.ControlToken
    $status.ControlToken = $null
    try {
        $discarded = Set-Gate14WindowsContributionPolicy -ControlToken $control
        $discarded = $null
        $script:Gate14ProductBaselineProcesses = [int]$script:LifecycleProcess.ActiveProcessCount
        $discarded = Invoke-Gate13LoopbackJson -Method "POST" -Path "/control/v1/workers/automatic/start" -BearerToken $control
        $discarded = $null
        $worker = Wait-Gate14WindowsRunning -ControlToken $control -TimeoutSeconds 1800
        $script:Gate14ProductWorkerPid = [int](Get-Gate13Property $worker "pid")
        $script:Gate14ProductBaselineProcesses = [int]$script:LifecycleProcess.ActiveProcessCount - 1
        if ($script:Gate14ProductBaselineProcesses -lt 1) {
            throw "packaged product process baseline is invalid"
        }
        $public = Get-Gate14WindowsPublicWorker -ControlToken $control
        $placement = Get-Gate13Property $public "placement"
        $limits = Get-Gate13Property (Get-Gate13Property $public "resources") "limits"
        $blockIndices = Get-Gate13Property $placement "block_indices"
        if (-not ($blockIndices -is [string]) -or $blockIndices -notmatch "^([0-9]{1,3}):([0-9]{1,3})$") {
            throw "automatic block placement is invalid"
        }
        $blockStart = [int]$Matches[1]
        $blockEnd = [int]$Matches[2]
        if (
            (Get-Gate13Property $placement "automatic") -ne $true -or
            $blockEnd -le $blockStart -or
            $blockEnd -gt $script:Gate14ProductProfile.TotalBlocks -or
            [int64](Get-Gate13Property $limits "disk_bytes") -ne [int64](Get-Gate13Property $script:Gate14ProductConfig "disk_bytes") -or
            [int64](Get-Gate13Property $limits "vram_bytes") -ne [int64](Get-Gate13Property $script:Gate14ProductConfig "vram_bytes") -or
            [double](Get-Gate13Property $limits "bandwidth_mbps") -ne [double](Get-Gate13Property $script:Gate14ProductConfig "bandwidth_mbps") -or
            [double](Get-Gate13Property $limits "power_watts") -ne [double](Get-Gate13Property $script:Gate14ProductConfig "power_watts")
        ) {
            throw "resolved contribution limits changed"
        }

        Invoke-Gate14WindowsLowVramProbe -ControlToken $control
        $unsupported = Invoke-Gate14WindowsCpuPowerProbe -ControlToken $control
        $recovery = Invoke-Gate14WindowsCrashRecovery -ControlToken $control
        $pause = Invoke-Gate14WindowsPause -ControlToken $control
        $restart = Invoke-Gate14WindowsRestart -ControlToken $control
        $after = Get-Gate14WindowsExactCacheInventory -Root $script:Gate14ProductContext.CacheDir -Context $script:Gate14ProductContext
        Assert-Gate14WindowsSameCacheInventory -Expected $script:Gate14ProductInventory -Actual $after
        $script:Gate14ProductPrepared = $true
        return [ordered]@{
            schema_version = 1
            scope = "gate14-prepared-host-observations"
            run_id = [string](Get-Gate13Property $script:Gate14ProductConfig "run_id")
            platform = "windows"
            attempt_ordinal = [int](Get-Gate13Property $script:Gate14ProductConfig "attempt_ordinal")
            source_commit = [string](Get-Gate13Property $script:Gate14ProductConfig "source_commit")
            package_sha256 = [string](Get-Gate13Property $script:Gate14ProductConfig "package_sha256")
            model = [ordered]@{
                id = $script:Gate14ProductProfile.ModelId
                manifest_digest = "sha256:" + $script:Gate14ProductProfile.ManifestDigest
                revision_commit = $script:Gate14ProductProfile.RevisionCommit
                gate9_envelope_sha256 = $script:Gate14ProductProfile.Gate9EnvelopeSha256
                selected_artifact_count = [int]$script:Gate14ProductProfile.SelectedCount
                selected_artifact_bytes = [int64]$script:Gate14ProductProfile.SelectedBytes
                total_blocks = [int]$script:Gate14ProductProfile.TotalBlocks
            }
            cache = [ordered]@{
                verified_bytes_before = [int64]$script:Gate14ProductInventory.Bytes
                verified_bytes_after = [int64]$after.Bytes
                transfer_bytes_during_gate = [int64]0
                digest_mismatch_count = 0
                forbidden_model_acquired = $false
            }
            placement = [ordered]@{
                automatic = $true
                worker_count = 1
                block_start = $blockStart
                block_end = $blockEnd
                intent_published = $true
                remote_acknowledged = $true
            }
            limits = [ordered]@{
                disk_bytes = [int64](Get-Gate13Property $script:Gate14ProductConfig "disk_bytes")
                vram_bytes = [int64](Get-Gate13Property $script:Gate14ProductConfig "vram_bytes")
                bandwidth_mbps = [double](Get-Gate13Property $script:Gate14ProductConfig "bandwidth_mbps")
                power_watts = [double](Get-Gate13Property $script:Gate14ProductConfig "power_watts")
                schedule_timezone = "UTC"
                resource_limit_count = 5
                configured_and_resolved_match = $true
                low_vram_rejected = $true
            }
            recovery = $recovery
            pause = $pause
            restart = $restart
            unsupported_telemetry = $unsupported
        }
    }
    finally {
        $control = $null
    }
}

function Initialize-Gate14WindowsLoadInterop {
    if ($null -ne ("Gate14.LoopbackLoad" -as [type])) {
        return
    }
    Add-Type -TypeDefinition @'
using System;
using System.Net;
using System.Net.Sockets;
using System.Threading;

namespace Gate14
{
    public sealed class LoopbackLoad : IDisposable
    {
        private readonly ManualResetEvent stop = new ManualResetEvent(false);
        private readonly TcpListener listener;
        private readonly Thread receiver;
        private readonly Thread sender;
        private TcpClient accepted;
        private TcpClient client;

        public LoopbackLoad()
        {
            listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start(1);
            int port = ((IPEndPoint)listener.LocalEndpoint).Port;
            receiver = new Thread(delegate()
            {
                byte[] bytes = new byte[1048576];
                try
                {
                    accepted = listener.AcceptTcpClient();
                    NetworkStream stream = accepted.GetStream();
                    while (!stop.WaitOne(0))
                    {
                        if (stream.Read(bytes, 0, bytes.Length) == 0)
                        {
                            break;
                        }
                    }
                }
                catch
                {
                    if (!stop.WaitOne(0))
                    {
                        stop.Set();
                    }
                }
            });
            sender = new Thread(delegate()
            {
                byte[] bytes = new byte[1048576];
                try
                {
                    client = new TcpClient();
                    client.Connect(IPAddress.Loopback, port);
                    NetworkStream stream = client.GetStream();
                    while (!stop.WaitOne(0))
                    {
                        stream.Write(bytes, 0, bytes.Length);
                    }
                }
                catch
                {
                    if (!stop.WaitOne(0))
                    {
                        stop.Set();
                    }
                }
            });
            receiver.IsBackground = true;
            sender.IsBackground = true;
            receiver.Start();
            sender.Start();
        }

        public void Dispose()
        {
            stop.Set();
            try { if (client != null) client.Close(); } catch { }
            try { if (accepted != null) accepted.Close(); } catch { }
            try { listener.Stop(); } catch { }
            receiver.Join(5000);
            sender.Join(5000);
            stop.Dispose();
        }
    }
}
'@ -Language CSharp -ErrorAction Stop | Out-Null
}

function Get-Gate14WindowsMeasurement {
    param(
        [Parameter(Mandatory = $true)][object]$Worker,
        [Parameter(Mandatory = $true)][string]$Field
    )
    $value = Get-Gate13Property $Worker $Field
    if (-not ($value -is [int]) -and -not ($value -is [long]) -and -not ($value -is [double]) -and -not ($value -is [decimal])) {
        throw "physical resource measurement is unavailable"
    }
    $number = [double]$value
    if ([double]::IsNaN($number) -or [double]::IsInfinity($number) -or $number -lt 0) {
        throw "physical resource measurement is unavailable"
    }
    return $number
}

function New-Gate14WindowsCalibrationRecord {
    param(
        [Parameter(Mandatory = $true)][string]$Kind,
        [Parameter(Mandatory = $true)][object]$Challenge,
        [Parameter(Mandatory = $true)][double]$StartedAt,
        [Parameter(Mandatory = $true)][double]$EndedAt,
        [Parameter(Mandatory = $true)][double]$Baseline,
        [Parameter(Mandatory = $true)][double]$Trigger,
        [Parameter(Mandatory = $true)][double]$Resume,
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Scope,
        [Parameter(Mandatory = $true)][double]$Configured,
        [Parameter(Mandatory = $true)][double]$Duration
    )
    $interval = [double](Get-Gate13Property $script:Gate14ProductConfig "sample_interval_seconds")
    if ($EndedAt - $StartedAt -lt 2 * $interval -or $EndedAt - $StartedAt -gt 120) {
        throw "physical calibration sample window is invalid"
    }
    return [ordered]@{
        kind = $Kind
        suspended = $true
        resumed = $true
        desired_intent_preserved = $true
        worker_count_during = 0
        duration_seconds = [Math]::Round($Duration, 6)
        calibration = [ordered]@{
            measurement_source = $Source
            measurement_scope = $Scope
            sample_count = 3
            sample_interval_seconds = $interval
            baseline_value = $Baseline
            configured_limit = $Configured
            trigger_value = $Trigger
            resume_value = $Resume
            challenge_sha256 = [string](Get-Gate13Property $Challenge "challenge_sha256")
            sample_started_at_unix = $StartedAt
            sample_ended_at_unix = $EndedAt
        }
    }
}

function Invoke-Gate14WindowsBandwidthCalibration {
    param(
        [Parameter(Mandatory = $true)][string]$ControlToken,
        [Parameter(Mandatory = $true)][object]$Challenge
    )
    $limit = [double](Get-Gate13Property $script:Gate14ProductConfig "bandwidth_mbps")
    $interval = [double](Get-Gate13Property $script:Gate14ProductConfig "sample_interval_seconds")
    $startedAt = Get-Gate14WindowsUnixSeconds
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $samples = @()
    foreach ($index in 1..3) {
        $samples += Get-Gate14WindowsMeasurement -Worker (Get-Gate14WindowsExactWorker -ControlToken $ControlToken -Running) -Field "current_bandwidth_mbps"
        Start-Sleep -Milliseconds ([int]($interval * 1000))
    }
    $baseline = [double]($samples | Measure-Object -Minimum).Minimum
    if ($baseline -ge $limit) {
        throw "bandwidth baseline already exceeds its limit"
    }
    Initialize-Gate14WindowsLoadInterop
    $load = New-Object Gate14.LoopbackLoad
    try {
        $inactive = Wait-Gate14WindowsInactive -ControlToken $ControlToken -Resource -PriorPid $script:Gate14ProductWorkerPid
        $trigger = Get-Gate14WindowsMeasurement -Worker $inactive.Private -Field "current_bandwidth_mbps"
    }
    finally {
        $load.Dispose()
    }
    $running = Wait-Gate14WindowsRunning -ControlToken $ControlToken
    $resume = Get-Gate14WindowsMeasurement -Worker $running -Field "current_bandwidth_mbps"
    $endedAt = Get-Gate14WindowsUnixSeconds
    if (-not ($baseline -lt $limit -and $limit -lt $trigger -and $resume -lt $limit)) {
        throw "bandwidth calibration did not cross its limit"
    }
    $script:Gate14ProductWorkerPid = [int](Get-Gate13Property $running "pid")
    return New-Gate14WindowsCalibrationRecord -Kind "bandwidth" -Challenge $Challenge -StartedAt $startedAt -EndedAt $endedAt -Baseline $baseline -Trigger $trigger -Resume $resume -Source "host-network-counters" -Scope "aggregate-host-network" -Configured $limit -Duration $timer.Elapsed.TotalSeconds
}

function Start-Gate14WindowsPowerBurn {
    $arguments = [string[]]@(
        "edge-benchmark",
        $script:Gate14ProductContext.ManifestPath,
        "--cache_dir",
        $script:Gate14ProductContext.CacheDir,
        "--allow_warm_cache",
        "--prompt",
        "CommunityAI Gate 14 calibration",
        "--max_new_tokens",
        "128",
        "--supervisor_timeout",
        "120"
    )
    $burn = [Gate13.NativeHost]::Start(
        $script:LifecycleNodeExe,
        $arguments,
        $script:LifecycleProductRoot
    )
    [void]$script:Gate14ProductBurns.Add($burn)
    return $burn
}

function Stop-Gate14WindowsPowerBurn {
    param([Parameter(Mandatory = $true)][object]$Burn)
    $Burn.ForceAndVerify(30000)
    if ($Burn.ActiveProcessCount -ne 0) {
        throw "power calibration burn cleanup was not proved"
    }
    $burnIndex = $script:Gate14ProductBurns.IndexOf($Burn)
    if ($burnIndex -lt 0) {
        throw "power calibration burn tracking changed"
    }
    $script:Gate14ProductBurns.RemoveAt($burnIndex)
}

function Invoke-Gate14WindowsPowerCalibration {
    param(
        [Parameter(Mandatory = $true)][string]$ControlToken,
        [Parameter(Mandatory = $true)][object]$Challenge
    )
    $limit = [double](Get-Gate13Property $script:Gate14ProductConfig "power_watts")
    $interval = [double](Get-Gate13Property $script:Gate14ProductConfig "sample_interval_seconds")
    $startedAt = Get-Gate14WindowsUnixSeconds
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $samples = @()
    foreach ($index in 1..3) {
        $samples += Get-Gate14WindowsMeasurement -Worker (Get-Gate14WindowsExactWorker -ControlToken $ControlToken -Running) -Field "current_power_watts"
        Start-Sleep -Milliseconds ([int]($interval * 1000))
    }
    $baseline = [double]($samples | Measure-Object -Minimum).Minimum
    if ($baseline -ge $limit) {
        throw "power baseline already exceeds its limit"
    }
    $burn = Start-Gate14WindowsPowerBurn
    try {
        $inactive = Wait-Gate14WindowsInactive -ControlToken $ControlToken -Resource -PriorPid $script:Gate14ProductWorkerPid
        $trigger = Get-Gate14WindowsMeasurement -Worker $inactive.Private -Field "current_power_watts"
    }
    finally {
        Stop-Gate14WindowsPowerBurn -Burn $burn
    }
    $running = Wait-Gate14WindowsRunning -ControlToken $ControlToken
    $resume = Get-Gate14WindowsMeasurement -Worker $running -Field "current_power_watts"
    $endedAt = Get-Gate14WindowsUnixSeconds
    if (-not ($baseline -lt $limit -and $limit -lt $trigger -and $resume -lt $limit)) {
        throw "power calibration did not cross its limit"
    }
    $script:Gate14ProductWorkerPid = [int](Get-Gate13Property $running "pid")
    return New-Gate14WindowsCalibrationRecord -Kind "power" -Challenge $Challenge -StartedAt $startedAt -EndedAt $endedAt -Baseline $baseline -Trigger $trigger -Resume $resume -Source "nvidia-nvml-device-power" -Scope "selected-nvidia-l4-device" -Configured $limit -Duration $timer.Elapsed.TotalSeconds
}

function Invoke-Gate14WindowsScheduleCalibration {
    param(
        [Parameter(Mandatory = $true)][string]$ControlToken,
        [Parameter(Mandatory = $true)][object]$Challenge
    )
    $interval = [double](Get-Gate13Property $script:Gate14ProductConfig "sample_interval_seconds")
    $startedAt = Get-Gate14WindowsUnixSeconds
    $timer = [Diagnostics.Stopwatch]::StartNew()
    Start-Sleep -Milliseconds ([int](2 * $interval * 1000))
    $discarded = Set-Gate14WindowsContributionPolicy -ControlToken $ControlToken -Schedule (Get-Gate14WindowsClosedSchedule)
    $discarded = $null
    try {
        $inactive = Wait-Gate14WindowsInactive -ControlToken $ControlToken -Schedule -PriorPid $script:Gate14ProductWorkerPid
    }
    finally {
        $discarded = Set-Gate14WindowsContributionPolicy -ControlToken $ControlToken -Schedule (Get-Gate14WindowsFullSchedule)
        $discarded = $null
    }
    $running = Wait-Gate14WindowsRunning -ControlToken $ControlToken
    $endedAt = Get-Gate14WindowsUnixSeconds
    $script:Gate14ProductWorkerPid = [int](Get-Gate13Property $running "pid")
    return New-Gate14WindowsCalibrationRecord -Kind "schedule" -Challenge $Challenge -StartedAt $startedAt -EndedAt $endedAt -Baseline 1.0 -Trigger 0.0 -Resume 1.0 -Source "utc-policy-clock" -Scope "utc-schedule-policy" -Configured 0.5 -Duration $timer.Elapsed.TotalSeconds
}

function Get-Gate14WindowsCleanupRecord {
    return [ordered]@{
        schema_version = 1
        scope = "gate14-host-lifecycle-cleanup"
        run_id = [string](Get-Gate13Property $script:Gate14ProductConfig "run_id")
        platform = "windows"
        attempt_ordinal = [int](Get-Gate13Property $script:Gate14ProductConfig "attempt_ordinal")
        processes_absent = $true
        credentials_removed = $true
        action_temporaries_removed = $true
    }
}

function Remove-Gate14WindowsExactTree {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    if (
        $full -cne $script:Gate14ProductActionRoot -and
        $full -cne $script:Gate14ProductWarmCache
    ) {
        throw "Gate 14 cleanup path rejected"
    }
    if (-not (Test-Path -LiteralPath $full)) {
        return
    }

    Initialize-Gate13NativeHost
    $rootLock = $null
    $pending = New-Object "System.Collections.Generic.Stack[string]"
    $directories = New-Object Collections.ArrayList
    $files = New-Object Collections.ArrayList
    try {
        try {
            $rootLock = [Gate13.NativeHost]::OpenReadOnlyNoFollow($full, $true)
        }
        catch {
            throw "Gate 14 cleanup root is unsafe"
        }

        $pending.Push($full)
        while ($pending.Count -gt 0) {
            $current = $pending.Pop()
            foreach ($child in @(Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop)) {
                $isDirectory = $child -is [IO.DirectoryInfo]
                try {
                    $locked = [Gate13.NativeHost]::OpenReadOnlyNoFollow(
                        $child.FullName,
                        $isDirectory
                    )
                }
                catch {
                    throw "Gate 14 cleanup descendant is unsafe: $($child.FullName)"
                }
                $entry = [pscustomobject]@{
                    Path = [string]$child.FullName
                    Lock = $locked
                }
                if ($locked.IsDirectory) {
                    [void]$directories.Add($entry)
                    $pending.Push([string]$child.FullName)
                }
                elseif (-not $isDirectory) {
                    [void]$files.Add($entry)
                }
                else {
                    $locked.Dispose()
                    $entry.Lock = $null
                    throw "Gate 14 cleanup descendant has an unsupported type: $($child.FullName)"
                }
            }
        }

        foreach ($file in @($files)) {
            $file.Lock.Dispose()
            $file.Lock = $null
            [IO.File]::Delete([string]$file.Path)
        }
        foreach ($directory in @(
            $directories | Sort-Object -Property { $_.Path.Length } -Descending
        )) {
            $directory.Lock.Dispose()
            $directory.Lock = $null
            [IO.Directory]::Delete([string]$directory.Path, $false)
        }
        $rootLock.Dispose()
        $rootLock = $null
        [IO.Directory]::Delete($full, $false)
    }
    finally {
        foreach ($file in @($files)) {
            if ($null -ne $file.Lock) {
                $file.Lock.Dispose()
            }
        }
        foreach ($directory in @($directories)) {
            if ($null -ne $directory.Lock) {
                $directory.Lock.Dispose()
            }
        }
        if ($null -ne $rootLock) {
            $rootLock.Dispose()
        }
    }
}

function Invoke-Gate14WindowsProductCleanup {
    if (-not $script:Gate14ProductInitialized) {
        throw "Windows product actions are not initialized"
    }
    if ($script:Gate14ProductCleaned) {
        return Get-Gate14WindowsCleanupRecord
    }

    $processFailure = $false
    foreach ($burn in @($script:Gate14ProductBurns)) {
        try {
            Stop-Gate14WindowsPowerBurn -Burn $burn
        }
        catch {
            $processFailure = $true
        }
    }
    try {
        Force-Gate13ProductCleanup
    }
    catch {
        $processFailure = $true
    }
    try {
        if (
            $script:Gate14ProductBurns.Count -ne 0 -or
            (Get-Gate13ProductProcessCount) -ne 0
        ) {
            $processFailure = $true
        }
    }
    catch {
        $processFailure = $true
    }
    if ($processFailure) {
        throw "Windows packaged product process cleanup was not proved"
    }

    $credentialFailure = $false
    try {
        $credentialCount = Get-Gate13CredentialCount
        if ($credentialCount -gt 0) {
            if (-not (Test-Path -LiteralPath $script:LifecycleDesktopExe -PathType Leaf)) {
                $credentialFailure = $true
            }
            else {
                $discarded = Invoke-Gate13Contained -Executable $script:LifecycleDesktopExe -Arguments ([string[]]@("--delete-control-key")) -WorkingDirectory $script:LifecycleProductRoot -TimeoutSeconds 180
                $discarded = $null
            }
        }
        if ((Get-Gate13CredentialCount) -ne 0) {
            $credentialFailure = $true
        }
    }
    catch {
        $credentialFailure = $true
    }
    if ($credentialFailure) {
        throw "Windows packaged product credential cleanup was not proved"
    }
    $script:Gate14ProductCredentialCreated = $false

    Close-Gate14WindowsCacheLocks
    $rootFailure = $false
    try {
        Remove-Gate14WindowsExactTree -Path $script:Gate14ProductWarmCache
    }
    catch {
        $rootFailure = $true
    }
    try {
        Remove-Gate14WindowsExactTree -Path $script:Gate14ProductActionRoot
    }
    catch {
        $rootFailure = $true
    }
    try {
        if ((Get-Gate13CredentialCount) -ne 0 -or (Get-Gate13ProductProcessCount) -ne 0) {
            $rootFailure = $true
        }
    }
    catch {
        $rootFailure = $true
    }
    if (
        (Test-Path -LiteralPath $script:Gate14ProductWarmCache) -or
        (Test-Path -LiteralPath $script:Gate14ProductActionRoot) -or
        $script:Gate14ProductBurns.Count -ne 0 -or
        $rootFailure
    ) {
        throw "Windows packaged product cleanup was not proved"
    }
    $script:Gate14ProductCleaned = $true
    return Get-Gate14WindowsCleanupRecord
}

function Invoke-Gate14WindowsProductPrepare {
    if (-not $script:Gate14ProductInitialized) {
        throw "Windows product actions are not initialized"
    }
    try {
        return Invoke-Gate14WindowsPrepareCore
    }
    catch {
        $failure = $_
        try {
            $discarded = Invoke-Gate14WindowsProductCleanup
            $discarded = $null
        }
        catch {
            throw "Windows product prepare failed and cleanup was incomplete"
        }
        throw $failure
    }
}

function Invoke-Gate14WindowsProductCalibrate {
    param([Parameter(Mandatory = $true)][object]$Challenge)
    if (-not $script:Gate14ProductPrepared -or $script:Gate14ProductCleaned) {
        throw "Windows product calibration order is invalid"
    }
    Assert-Gate13ExactProperties -InputObject $Challenge -Names @(
        "challenge_sha256", "controller_state_revision", "issued_at_unix", "expires_at_unix"
    )
    $now = Get-Gate14WindowsUnixSeconds
    if (
        -not ((Get-Gate13Property $Challenge "challenge_sha256") -is [string]) -or
        (Get-Gate13Property $Challenge "challenge_sha256") -notmatch "^sha256:[0-9a-f]{64}$" -or
        -not (Test-Gate14WindowsInteger (Get-Gate13Property $Challenge "controller_state_revision")) -or
        -not (Test-Gate14WindowsInteger (Get-Gate13Property $Challenge "issued_at_unix")) -or
        -not (Test-Gate14WindowsInteger (Get-Gate13Property $Challenge "expires_at_unix")) -or
        [double](Get-Gate13Property $Challenge "issued_at_unix") -gt $now -or
        [double](Get-Gate13Property $Challenge "expires_at_unix") -lt $now
    ) {
        throw "controller challenge is invalid or stale"
    }
    $control = Read-Gate13ControlKey
    try {
        $records = @(
            Invoke-Gate14WindowsBandwidthCalibration -ControlToken $control -Challenge $Challenge
            Invoke-Gate14WindowsPowerCalibration -ControlToken $control -Challenge $Challenge
            Invoke-Gate14WindowsScheduleCalibration -ControlToken $control -Challenge $Challenge
        )
        if ((Get-Gate14WindowsUnixSeconds) -gt [double](Get-Gate13Property $Challenge "expires_at_unix")) {
            throw "controller challenge expired during calibration"
        }
        return @($records)
    }
    finally {
        $control = $null
    }
}
