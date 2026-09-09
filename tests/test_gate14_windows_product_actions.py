from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE = ROOT / "scripts" / "gate13_windows_packaged_lifecycle.ps1"
INFERENCE = ROOT / "scripts" / "gate13_windows_localhost_inference.ps1"
PRODUCT = ROOT / "scripts" / "gate14_windows_product_actions.ps1"
HOST = ROOT / "scripts" / "gate14_windows_lifecycle_actions.ps1"
TRANSPORT = ROOT / "scripts" / "gate14_windows_action_transport.py"
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
MODEL_ID = "Qwen3.5 2B"
MANIFEST = "3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33"


def _ps_literal(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_powershell(
    source: str,
    tmp_path: Path,
    *,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    driver = tmp_path / "driver.ps1"
    driver.write_text(
        "$ErrorActionPreference = 'Stop'\n$global:LASTEXITCODE = 0\n" + source,
        encoding="utf-8",
        newline="\n",
    )
    return subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(driver),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_sources_bind_real_windows_product_actions():
    product = PRODUCT.read_text(encoding="utf-8")
    host = HOST.read_text(encoding="utf-8")
    transport = TRANSPORT.read_text(encoding="utf-8")
    lifecycle = LIFECYCLE.read_text(encoding="utf-8")

    for required in (
        "function Initialize-Gate14WindowsProductActions",
        "function Invoke-Gate14WindowsProductPrepare",
        "function Invoke-Gate14WindowsProductCalibrate",
        "function Invoke-Gate14WindowsProductCleanup",
        "Test-Gate13PackageAudit",
        "Install-Gate13VerifiedPackage",
        "Invoke-Gate13Bootstrap",
        "Move-Gate14WindowsWarmCache",
        "Start-Gate14WindowsProduct",
        "Get-Gate14WindowsExactCacheInventory",
        "Invoke-Gate14WindowsLowVramProbe",
        "Invoke-Gate14WindowsCpuPowerProbe",
        "Invoke-Gate14WindowsCrashRecovery",
        "Invoke-Gate14WindowsPause",
        "Invoke-Gate14WindowsRestart",
        "Gate14.LoopbackLoad",
        "Start-Gate14WindowsPowerBurn",
        "Get-Gate14WindowsClosedSchedule",
        "challenge expired during calibration",
    ):
        assert required in product

    assert ". $inferencePath" in host
    assert ". $productActionsPath" in host
    assert "product-prepare-failed" in host
    assert "product-calibration-failed" in host
    assert "Invoke-Gate14WindowsProductCleanup" in host
    assert "_PRODUCT_ACTIONS_SHA256" in transport
    assert '"-ProductActionsSha256"' in transport
    assert "_open_verified_source" in transport
    assert "Remove-Item -LiteralPath $full -Recurse" not in product
    assert "Stop-Gate13Product\n    Start-Gate14WindowsProduct" in product
    assert "public bool ContainsProcessId" in lifecycle
    assert "public void KillMemberProcess" in lifecycle


@pytest.mark.skipif(
    not POWERSHELL.is_file(),
    reason="native Windows PowerShell is required",
)
def test_job_membership_and_exact_member_termination_are_native(tmp_path):
    source = f"""
. {_ps_literal(LIFECYCLE)}
Initialize-Gate13NativeHost
$owner = [Gate13.NativeHost]::Start(
    {_ps_literal(POWERSHELL)},
    [string[]]@(
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        'Start-Sleep -Seconds 30'
    ),
    {_ps_literal(tmp_path)}
)
try {{
    $field = $owner.GetType().GetField(
        'processId',
        [Reflection.BindingFlags]'NonPublic,Instance'
    )
    $memberPid = [int]$field.GetValue($owner)
    $containedBefore = $owner.ContainsProcessId($memberPid)
    $owner.KillMemberProcess($memberPid, 30000)
    $emptyAfter = ($owner.ActiveProcessCount -eq 0)
    [Console]::Out.WriteLine((@{{
        contained_before = $containedBefore
        empty_after = $emptyAfter
    }} | ConvertTo-Json -Compress))
}}
finally {{
    $owner.ForceAndVerify(30000)
    $owner.Dispose()
}}
"""
    result = _run_powershell(source, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "contained_before": True,
        "empty_after": True,
    }


@pytest.mark.skipif(
    not POWERSHELL.is_file(),
    reason="native Windows PowerShell is required",
)
def test_product_start_uses_only_action_specific_node_paths_on_initial_and_restart(
    tmp_path,
):
    product_root = tmp_path / "CommunityAI"
    persistent_root = tmp_path / "persistent"
    node_config = persistent_root / "node-config.json"
    bootstrap = product_root / "_internal" / "bootstrap" / "catalog-bootstrap.json"
    desktop = product_root / "CommunityAI.exe"
    fake_profile = tmp_path / "profile"
    persistent_root.mkdir()
    bootstrap.parent.mkdir(parents=True)
    node_config.write_text("{}\n", encoding="utf-8")
    bootstrap.write_text("{}\n", encoding="utf-8")
    desktop.write_bytes(b"desktop")

    source = f"""
. {_ps_literal(LIFECYCLE)}
. {_ps_literal(INFERENCE)}
. {_ps_literal(PRODUCT)}

$env:USERPROFILE = {_ps_literal(fake_profile)}
$script:LifecycleProcess = $null
$script:LifecycleDesktopExe = {_ps_literal(desktop)}
$script:LifecycleProductRoot = {_ps_literal(product_root)}
$script:LifecycleNodeConfig = {_ps_literal(node_config)}
$script:LifecyclePersistentRoot = {_ps_literal(persistent_root)}
$script:LifecycleBootstrap = {_ps_literal(bootstrap)}
$script:capturedStarts = New-Object Collections.ArrayList

function Initialize-Gate13NativeHost {{ }}
function New-Gate14WindowsContainedProductProcess {{
    param($Executable, $Arguments, $WorkingDirectory)
    [void]$script:capturedStarts.Add([pscustomobject]@{{
        executable = $Executable
        arguments = @($Arguments)
        working_directory = $WorkingDirectory
    }})
    return [pscustomobject]@{{ ActiveProcessCount = 1 }}
}}

Start-Gate14WindowsProduct
$script:LifecycleProcess = $null
Start-Gate14WindowsProduct
$defaultNodeRoot = Join-Path $env:USERPROFILE '.drift\\node'
[Console]::Out.WriteLine((@{{
    starts = @($script:capturedStarts)
    default_root_absent = -not (Test-Path -LiteralPath $defaultNodeRoot)
}} | ConvertTo-Json -Compress -Depth 8))
"""
    result = _run_powershell(source, tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["default_root_absent"] is True
    assert len(payload["starts"]) == 2
    for start in payload["starts"]:
        assert start == {
            "executable": str(desktop),
            "arguments": [
                "--node-config",
                str(node_config),
                "--node-data-dir",
                str(persistent_root),
                "--bootstrap-config",
                str(bootstrap),
            ],
            "working_directory": str(product_root),
        }


@pytest.mark.skipif(
    not POWERSHELL.is_file(),
    reason="native Windows PowerShell is required",
)
def test_post_credential_start_failure_runs_exact_cleanup(tmp_path):
    work_root = tmp_path / "work"
    staging_root = tmp_path / "staging"
    package = tmp_path / "CommunityAI-windows.zip"
    warm_cache = work_root / "gate14-warm-cache"
    config_path = tmp_path / "gate14-lifecycle.json"
    work_root.mkdir()
    staging_root.mkdir()
    warm_cache.mkdir()
    package.write_bytes(b"package")
    artifacts = [
        {
            "path": f"blobs/{index:02d}.bin",
            "role": "weight",
            "sha256": "sha256:" + f"{index + 1:064x}",
            "size_bytes": index + 1,
        }
        for index in range(8)
    ]
    config_path.write_text(
        json.dumps(
            {
                "run_id": "gate14-windows-product-a",
                "attempt_ordinal": 1,
                "source_commit": "a" * 40,
                "package_sha256": "sha256:" + "b" * 64,
                "platform": "windows",
                "model_id": MODEL_ID,
                "manifest_digest": "sha256:" + MANIFEST,
                "work_root": str(work_root),
                "staging_root": str(staging_root),
                "package_path": str(package),
                "package_bytes": package.stat().st_size,
                "disk_bytes": 8_000_000_000,
                "vram_bytes": 20_000_000_000,
                "bandwidth_mbps": 250.0,
                "power_watts": 200.0,
                "pause_timeout_seconds": 30.0,
                "sample_interval_seconds": 1.0,
                "warm_cache": {"artifacts": artifacts},
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    source = f"""
. {_ps_literal(LIFECYCLE)}
. {_ps_literal(INFERENCE)}
. {_ps_literal(PRODUCT)}

$script:testCredentialCount = 0
$script:testCleanupCalls = 0
function Initialize-Gate13CredentialInterop {{ }}
function New-Gate14WindowsRunInput {{ }}
function Test-Gate13PackageAudit {{
    return [pscustomobject]@{{
        SourceCommit = ('a' * 40)
        PackageDigest = ('b' * 64)
    }}
}}
function Install-Gate13VerifiedPackage {{
    param($Audit)
    New-Item -ItemType Directory -Path $script:LifecycleProductRoot -Force | Out-Null
    [IO.File]::WriteAllText(
        $script:LifecycleDesktopExe,
        'desktop',
        (New-Object Text.UTF8Encoding($false))
    )
}}
function Test-Gate13PackagedSelfTests {{
    return [pscustomobject]@{{ result = 'passed' }}
}}
function Invoke-Gate13Bootstrap {{
    if ($script:testCredentialCount -ne 0) {{
        throw 'bootstrap observed an unexpected credential'
    }}
    return [pscustomobject]@{{ result = 'passed' }}
}}
function Get-Gate13CredentialCount {{
    return [int]$script:testCredentialCount
}}
function Get-Gate13ProductProcessCount {{ return 0 }}
function Get-Gate13SelectedManifestContext {{
    param($Profile)
    return [pscustomobject]@{{
        CacheDir = (Join-Path $script:Gate14ProductActionRoot 'cache')
        ManifestPath = (Join-Path $script:Gate14ProductActionRoot 'manifest.json')
    }}
}}
function Move-Gate14WindowsWarmCache {{ }}
function Get-Gate14WindowsExactCacheInventory {{
    param($Root, $Context)
    return [pscustomobject]@{{ Entries = @(); Count = 8; Bytes = 4571197320 }}
}}
function Assert-Gate14WindowsSameCacheInventory {{ }}
function Start-Gate14WindowsProduct {{
    if ($script:testCredentialCount -ne 0) {{
        throw 'start observed an unexpected credential'
    }}
    $script:testCredentialCount = 1
}}
function Wait-Gate13ProductStatus {{
    param($TimeoutSeconds)
    return [pscustomobject]@{{
        Profile = [pscustomobject]@{{
            ModelId = "Qwen3.5 2B"
            ManifestDigest = "3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33"
        }}
        ControlToken = ('drift_control_' + ('A' * 43))
    }}
}}
function Set-Gate14WindowsContributionPolicy {{
    param($ControlToken, $Schedule)
    if (
        $script:testCredentialCount -ne 1 -or
        $ControlToken -cne ('drift_control_' + ('A' * 43))
    ) {{
        throw 'post-start state was not established'
    }}
    throw 'post-start-policy-failure'
}}
function Force-Gate13ProductCleanup {{
    $script:testCleanupCalls += 1
}}
function Invoke-Gate13Contained {{
    param($Executable, $Arguments, $WorkingDirectory, $TimeoutSeconds)
    if ($Arguments -ccontains '--delete-control-key') {{
        $script:testCredentialCount = 0
    }}
    return '{{"result":"passed"}}'
}}

Initialize-Gate14WindowsProductActions `
    -LifecycleConfig {_ps_literal(config_path)} `
    -RunId 'gate14-windows-product-a' `
    -AttemptOrdinal 1 `
    -SourceCommit ('a' * 40) `
    -PackageSha256 ('sha256:' + ('b' * 64)) `
    -ProductActionsPath {_ps_literal(PRODUCT)}

$actionRoot = $script:Gate14ProductActionRoot
$warmRoot = $script:Gate14ProductWarmCache
$message = $null
try {{
    [void](Invoke-Gate14WindowsProductPrepare)
}}
catch {{
    $message = $_.Exception.Message
}}
$cleanup = Invoke-Gate14WindowsProductCleanup
[Console]::Out.WriteLine((@{{
    error = $message
    cleanup_calls = $script:testCleanupCalls
    credential_count = $script:testCredentialCount
    action_root_absent = -not (Test-Path -LiteralPath $actionRoot)
    warm_root_absent = -not (Test-Path -LiteralPath $warmRoot)
    cleaned = $script:Gate14ProductCleaned
    cleanup = $cleanup
}} | ConvertTo-Json -Compress -Depth 8))
"""
    result = _run_powershell(source, tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["error"] == "post-start-policy-failure"
    assert payload["cleanup_calls"] == 1
    assert payload["credential_count"] == 0
    assert payload["action_root_absent"] is True
    assert payload["warm_root_absent"] is True
    assert payload["cleaned"] is True
    assert payload["cleanup"] == {
        "schema_version": 1,
        "scope": "gate14-host-lifecycle-cleanup",
        "run_id": "gate14-windows-product-a",
        "platform": "windows",
        "attempt_ordinal": 1,
        "processes_absent": True,
        "credentials_removed": True,
        "action_temporaries_removed": True,
    }


@pytest.mark.parametrize("failure_mode", ["process", "burn", "credential"])
@pytest.mark.skipif(
    not POWERSHELL.is_file(),
    reason="native Windows PowerShell is required",
)
def test_cleanup_preserves_deletion_tool_and_retries_transient_failures(
    tmp_path,
    failure_mode,
):
    action_root = tmp_path / "action"
    warm_root = tmp_path / "warm"
    product_root = action_root / "install" / "CommunityAI"
    desktop = product_root / "CommunityAI.exe"
    product_root.mkdir(parents=True)
    warm_root.mkdir()
    desktop.write_bytes(b"desktop")

    source = f"""
. {_ps_literal(LIFECYCLE)}
. {_ps_literal(INFERENCE)}
. {_ps_literal(PRODUCT)}

$script:Gate14ProductInitialized = $true
$script:Gate14ProductCleaned = $false
$script:Gate14ProductConfig = [pscustomobject]@{{
    run_id = 'gate14-cleanup-retry-a'
    attempt_ordinal = 1
}}
$script:Gate14ProductActionRoot = [IO.Path]::GetFullPath({_ps_literal(action_root)})
$script:Gate14ProductWarmCache = [IO.Path]::GetFullPath({_ps_literal(warm_root)})
$script:LifecycleProductRoot = {_ps_literal(product_root)}
$script:LifecycleDesktopExe = {_ps_literal(desktop)}
$script:Gate14ProductBurns = New-Object Collections.ArrayList
$script:Gate14ProductCacheLocks = New-Object Collections.ArrayList
$script:testFailureMode = '{failure_mode}'
$script:testForceCalls = 0
$script:testDeleteCalls = 0
$script:testBurnCalls = 0
$script:testProcessCount = 1
$script:testCredentialCount = 1
if ($script:testFailureMode -ceq 'burn') {{
    $burn = [pscustomobject]@{{ ActiveProcessCount = 1 }}
    $burn | Add-Member -MemberType ScriptMethod -Name ForceAndVerify -Value {{
        param([int]$TimeoutMilliseconds)
        $script:testBurnCalls += 1
        if ($script:testBurnCalls -eq 1) {{
            throw 'one-shot burn cleanup failure'
        }}
        $this.ActiveProcessCount = 0
    }}
    [void]$script:Gate14ProductBurns.Add($burn)
}}

function Force-Gate13ProductCleanup {{
    $script:testForceCalls += 1
    if (
        $script:testFailureMode -ceq 'process' -and
        $script:testForceCalls -eq 1
    ) {{
        throw 'one-shot process cleanup failure'
    }}
    $script:testProcessCount = 0
}}
function Get-Gate13ProductProcessCount {{
    return [int]$script:testProcessCount
}}
function Get-Gate13CredentialCount {{
    return [int]$script:testCredentialCount
}}
function Invoke-Gate13Contained {{
    param($Executable, $Arguments, $WorkingDirectory, $TimeoutSeconds)
    if (-not ($Arguments -ccontains '--delete-control-key')) {{
        throw 'unexpected cleanup command'
    }}
    $script:testDeleteCalls += 1
    if (
        $script:testFailureMode -ceq 'credential' -and
        $script:testDeleteCalls -eq 1
    ) {{
        throw 'one-shot credential cleanup failure'
    }}
    $script:testCredentialCount = 0
    return '{{"result":"passed"}}'
}}

$firstError = $null
try {{
    [void](Invoke-Gate14WindowsProductCleanup)
}}
catch {{
    $firstError = $_.Exception.Message
}}
$preserved = (
    (Test-Path -LiteralPath $script:Gate14ProductActionRoot) -and
    (Test-Path -LiteralPath $script:Gate14ProductWarmCache) -and
    (Test-Path -LiteralPath $script:LifecycleDesktopExe)
)
$firstCredential = $script:testCredentialCount
$firstProcess = $script:testProcessCount
$firstBurnCount = $script:Gate14ProductBurns.Count
$cleanup = Invoke-Gate14WindowsProductCleanup
[Console]::Out.WriteLine((@{{
    first_error = $firstError
    preserved = $preserved
    first_credential = $firstCredential
    first_process = $firstProcess
    first_burn_count = $firstBurnCount
    force_calls = $script:testForceCalls
    burn_calls = $script:testBurnCalls
    delete_calls = $script:testDeleteCalls
    final_credential = $script:testCredentialCount
    final_process = $script:testProcessCount
    final_burn_count = $script:Gate14ProductBurns.Count
    action_absent = -not (Test-Path -LiteralPath $script:Gate14ProductActionRoot)
    warm_absent = -not (Test-Path -LiteralPath $script:Gate14ProductWarmCache)
    cleaned = $script:Gate14ProductCleaned
    cleanup = $cleanup
}} | ConvertTo-Json -Compress -Depth 8))
"""
    result = _run_powershell(source, tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    expected_phase = "credential" if failure_mode == "credential" else "process"
    assert payload["first_error"] == (f"Windows packaged product {expected_phase} cleanup was not proved")
    assert payload["preserved"] is True
    assert payload["first_credential"] == 1
    assert payload["first_process"] == (1 if failure_mode == "process" else 0)
    assert payload["first_burn_count"] == (1 if failure_mode == "burn" else 0)
    assert payload["force_calls"] == 2
    assert payload["burn_calls"] == (2 if failure_mode == "burn" else 0)
    assert payload["delete_calls"] == (2 if failure_mode == "credential" else 1)
    assert payload["final_credential"] == 0
    assert payload["final_process"] == 0
    assert payload["final_burn_count"] == 0
    assert payload["action_absent"] is True
    assert payload["warm_absent"] is True
    assert payload["cleaned"] is True
    assert payload["cleanup"]["scope"] == "gate14-host-lifecycle-cleanup"


@pytest.mark.skipif(
    not POWERSHELL.is_file(),
    reason="native Windows PowerShell is required",
)
def test_exact_cache_inventory_and_cleanup_reject_unexpected_or_reparse_entries(
    tmp_path,
):
    cache_root = tmp_path / "cache"
    outside = tmp_path / "outside"
    artifact_path = cache_root / "manifest-artifacts" / MANIFEST / "snapshot" / "blobs" / "a.bin"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"abc")
    outside.mkdir()
    (outside / "sentinel.txt").write_text("outside", encoding="utf-8")
    artifact_digest = hashlib.sha256(b"abc").hexdigest()

    source = f"""
. {_ps_literal(LIFECYCLE)}
. {_ps_literal(INFERENCE)}
. {_ps_literal(PRODUCT)}

$script:Gate14ProductProfile = [pscustomobject]@{{
    SelectedCount = 1
    SelectedBytes = 3
}}
$script:Gate14ProductArtifacts = @([pscustomobject]@{{
    path = 'blobs/a.bin'
    role = 'weight'
    sha256 = '{artifact_digest}'
    size_bytes = [int64]3
}})
$context = [pscustomobject]@{{ ManifestDigest = '{MANIFEST}' }}
$valid = Get-Gate14WindowsExactCacheInventory -Root {_ps_literal(cache_root)} -Context $context

$unexpectedRejected = $false
[IO.File]::WriteAllText(
    (Join-Path {_ps_literal(cache_root)} 'unexpected.bin'),
    'x',
    (New-Object Text.UTF8Encoding($false))
)
try {{
    [void](Get-Gate14WindowsExactCacheInventory -Root {_ps_literal(cache_root)} -Context $context)
}}
catch {{
    $unexpectedRejected = $_.Exception.Message -like '*unexpected file*'
}}
[IO.File]::Delete((Join-Path {_ps_literal(cache_root)} 'unexpected.bin'))

$digestRejected = $false
[IO.File]::WriteAllBytes({_ps_literal(artifact_path)}, [byte[]](97, 98, 100))
try {{
    [void](Get-Gate14WindowsExactCacheInventory -Root {_ps_literal(cache_root)} -Context $context)
}}
catch {{
    $digestRejected = $_.Exception.Message -like '*artifact verification failed*'
}}
[IO.File]::WriteAllBytes({_ps_literal(artifact_path)}, [byte[]](97, 98, 99))

$missingRejected = $false
[IO.File]::Delete({_ps_literal(artifact_path)})
try {{
    [void](Get-Gate14WindowsExactCacheInventory -Root {_ps_literal(cache_root)} -Context $context)
}}
catch {{
    $missingRejected = $_.Exception.Message -like '*inventory is incomplete*'
}}
[IO.File]::WriteAllBytes({_ps_literal(artifact_path)}, [byte[]](97, 98, 99))
$restored = Get-Gate14WindowsExactCacheInventory -Root {_ps_literal(cache_root)} -Context $context

$junction = Join-Path {_ps_literal(cache_root)} 'outside-junction'
New-Item -ItemType Junction -Path $junction -Target {_ps_literal(outside)} | Out-Null
$reparseRejected = $false
try {{
    [void](Get-Gate14WindowsExactCacheInventory -Root {_ps_literal(cache_root)} -Context $context)
}}
catch {{
    $reparseRejected = $_.Exception.Message -like '*reparse point*'
}}

$script:Gate14ProductActionRoot = [IO.Path]::GetFullPath({_ps_literal(cache_root)})
$script:Gate14ProductWarmCache = [IO.Path]::GetFullPath(
    (Join-Path {_ps_literal(tmp_path)} 'unused-warm-cache')
)
$cleanupRejected = $false
try {{
    Remove-Gate14WindowsExactTree -Path $script:Gate14ProductActionRoot
}}
catch {{
    $cleanupRejected = $_.Exception.Message -like '*descendant is unsafe*'
}}
$outsidePreserved = Test-Path -LiteralPath (Join-Path {_ps_literal(outside)} 'sentinel.txt')
[IO.Directory]::Delete($junction, $false)
$locked = Get-Gate14WindowsExactCacheInventory -Root {_ps_literal(cache_root)} -Context $context -HoldLocks
$identityMatches = (
    $locked.Entries[0].FileIdentity -ceq $restored.Entries[0].FileIdentity
)
$mutationRejected = $false
try {{
    [IO.File]::WriteAllBytes({_ps_literal(artifact_path)}, [byte[]](120, 121, 122))
}}
catch {{
    $mutationRejected = $true
}}
Close-Gate14WindowsCacheLocks
Remove-Gate14WindowsExactTree -Path $script:Gate14ProductActionRoot

[Console]::Out.WriteLine((@{{
    valid_count = $valid.Count
    valid_bytes = $valid.Bytes
    unexpected_rejected = $unexpectedRejected
    digest_rejected = $digestRejected
    missing_rejected = $missingRejected
    reparse_rejected = $reparseRejected
    cleanup_rejected = $cleanupRejected
    outside_preserved = $outsidePreserved
    identity_matches = $identityMatches
    mutation_rejected = $mutationRejected
    cache_removed = -not (Test-Path -LiteralPath {_ps_literal(cache_root)})
}} | ConvertTo-Json -Compress))
"""
    result = _run_powershell(source, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "valid_count": 1,
        "valid_bytes": 3,
        "unexpected_rejected": True,
        "digest_rejected": True,
        "missing_rejected": True,
        "reparse_rejected": True,
        "cleanup_rejected": True,
        "outside_preserved": True,
        "identity_matches": True,
        "mutation_rejected": True,
        "cache_removed": True,
    }


@pytest.mark.skipif(
    not POWERSHELL.is_file(),
    reason="native Windows PowerShell is required",
)
def test_calibration_record_and_stale_challenge_fail_closed(tmp_path):
    source = f"""
. {_ps_literal(LIFECYCLE)}
. {_ps_literal(INFERENCE)}
. {_ps_literal(PRODUCT)}
$script:Gate14ProductConfig = [pscustomobject]@{{
    sample_interval_seconds = 1.0
}}
$challenge = [pscustomobject]@{{
    challenge_sha256 = ('sha256:' + ('c' * 64))
}}
$record = New-Gate14WindowsCalibrationRecord `
    -Kind 'bandwidth' `
    -Challenge $challenge `
    -StartedAt 100.0 `
    -EndedAt 102.0 `
    -Baseline 10.0 `
    -Trigger 300.0 `
    -Resume 9.0 `
    -Source 'host-network-counters' `
    -Scope 'aggregate-host-network' `
    -Configured 250.0 `
    -Duration 2.0
$badWindowRejected = $false
try {{
    [void](New-Gate14WindowsCalibrationRecord `
        -Kind 'bandwidth' `
        -Challenge $challenge `
        -StartedAt 100.0 `
        -EndedAt 101.0 `
        -Baseline 10.0 `
        -Trigger 300.0 `
        -Resume 9.0 `
        -Source 'host-network-counters' `
        -Scope 'aggregate-host-network' `
        -Configured 250.0 `
        -Duration 1.0)
}}
catch {{
    $badWindowRejected = $true
}}
$script:Gate14ProductPrepared = $true
$script:Gate14ProductCleaned = $false
$staleRejected = $false
try {{
    [void](Invoke-Gate14WindowsProductCalibrate -Challenge ([pscustomobject]@{{
        challenge_sha256 = ('sha256:' + ('d' * 64))
        controller_state_revision = 1
        issued_at_unix = 0
        expires_at_unix = 60
    }}))
}}
catch {{
    $staleRejected = ($_.Exception.Message -ceq 'controller challenge is invalid or stale')
}}
[Console]::Out.WriteLine((@{{
    kind = $record.kind
    sample_count = $record.calibration.sample_count
    challenge_sha256 = $record.calibration.challenge_sha256
    bad_window_rejected = $badWindowRejected
    stale_rejected = $staleRejected
}} | ConvertTo-Json -Compress))
"""
    result = _run_powershell(source, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "kind": "bandwidth",
        "sample_count": 3,
        "challenge_sha256": "sha256:" + "c" * 64,
        "bad_window_rejected": True,
        "stale_rejected": True,
    }
