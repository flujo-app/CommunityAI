from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE = ROOT / "scripts" / "gate13_windows_packaged_lifecycle.ps1"
INFERENCE = ROOT / "scripts" / "gate13_windows_localhost_inference.ps1"
CONTROLLER = ROOT / "scripts" / "gate13_packaged_lifecycle.py"
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")


def _ps_literal(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_powershell(source: str, tmp_path: Path, *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
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


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="native Windows PowerShell is required")
def test_json_input_bound_accepts_production_scale_and_rejects_above_limit(tmp_path):
    accepted = tmp_path / "production-provenance.json"
    accepted.write_text(
        json.dumps({"padding": "a" * 1_241_800}),
        encoding="utf-8",
        newline="\n",
    )
    rejected = tmp_path / "oversized-provenance.json"
    rejected.write_bytes(b"{" + b" " * (2 * 1024 * 1024) + b"}")
    source = f"""
. {_ps_literal(LIFECYCLE)}
$accepted = $false
$rejected = $false
try {{
    $value = Read-Gate13JsonFile -Path {_ps_literal(accepted)}
    $accepted = ($value.padding.Length -eq 1241800)
}}
catch {{ }}
try {{
    [void](Read-Gate13JsonFile -Path {_ps_literal(rejected)})
}}
catch {{
    $rejected = ($_.Exception.Message -ceq 'JSON input rejected')
}}
[Console]::Out.WriteLine((@{{
    production_scale_accepted = $accepted
    above_limit_rejected = $rejected
}} | ConvertTo-Json -Compress))
"""
    result = _run_powershell(source, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "production_scale_accepted": True,
        "above_limit_rejected": True,
    }


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="native Windows PowerShell is required")
def test_native_interop_compiles_and_compound_collision_falls_back(tmp_path):
    service = "org.communityai.desktop"
    account = "local-node-control-v1"
    secret = "drift_control_" + "A" * 43
    source = f"""
. {_ps_literal(LIFECYCLE)}
Initialize-Gate13CredentialInterop
Initialize-Gate13NativeHost
$resolved = [Gate13.NativeCredential]::ResolveControlKeyForTest(
    '{service}',
    '{account}',
    '{service}',
    'another-user',
    $null,
    '{account}@{service}',
    '{account}',
    '{secret}'
)
$compoundMismatchRejected = $false
try {{
    [void][Gate13.NativeCredential]::ResolveControlKeyForTest(
        '{service}',
        '{account}',
        '{service}',
        'another-user',
        $null,
        '{account}@{service}',
        'another-user',
        '{secret}'
    )
}}
catch {{
    $compoundMismatchRejected = $true
}}
[Console]::Out.WriteLine((@{{
    resolved = ($resolved -ceq '{secret}')
    compound_mismatch_rejected = $compoundMismatchRejected
}} | ConvertTo-Json -Compress))
"""
    result = _run_powershell(source, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "resolved": True,
        "compound_mismatch_rejected": True,
    }


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="native Windows PowerShell is required")
def test_job_owns_and_reaps_late_descendant(tmp_path):
    pid_file = tmp_path / "descendant.pid"
    system_ping = Path(r"C:\Windows\System32\PING.EXE")
    root_command = (
        f"$p=Start-Process -FilePath '{system_ping}' "
        "-ArgumentList @('-t','127.0.0.1') -PassThru;"
        f"[IO.File]::WriteAllText('{str(pid_file).replace(chr(39), chr(39) * 2)}',[string]$p.Id)"
    )
    source = f"""
. {_ps_literal(LIFECYCLE)}
Initialize-Gate13NativeHost
$owner = [Gate13.NativeHost]::Start(
    {_ps_literal(POWERSHELL)},
    [string[]]@('-NoLogo','-NoProfile','-NonInteractive','-Command',{_ps_literal(root_command)}),
    {_ps_literal(tmp_path)}
)
$deadline = [DateTime]::UtcNow.AddSeconds(20)
while (-not (Test-Path -LiteralPath {_ps_literal(pid_file)}) -and [DateTime]::UtcNow -lt $deadline) {{
    Start-Sleep -Milliseconds 20
}}
if (-not (Test-Path -LiteralPath {_ps_literal(pid_file)})) {{ throw 'pid missing' }}
$pidValue = [int][IO.File]::ReadAllText({_ps_literal(pid_file)})
while (-not $owner.RootExited -and [DateTime]::UtcNow -lt $deadline) {{
    Start-Sleep -Milliseconds 20
}}
$activeBefore = $owner.ActiveProcessCount
$owner.ForceAndVerify(30000)
$aliveAfter = $null -ne (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)
[Console]::Out.WriteLine((@{{
    active_before = $activeBefore
    active_after = $owner.ActiveProcessCount
    descendant_alive_after = $aliveAfter
}} | ConvertTo-Json -Compress))
"""
    result = _run_powershell(source, tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["active_before"] >= 1
    assert payload["active_after"] == 0
    assert payload["descendant_alive_after"] is False


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="native Windows PowerShell is required")
def test_contained_timeout_reaps_entire_job(tmp_path):
    pid_file = tmp_path / "timed-out-descendant.pid"
    system_ping = Path(r"C:\Windows\System32\PING.EXE")
    root_command = (
        f"$p=Start-Process -FilePath '{system_ping}' "
        "-ArgumentList @('-t','127.0.0.1') -PassThru;"
        f"[IO.File]::WriteAllText('{str(pid_file).replace(chr(39), chr(39) * 2)}',[string]$p.Id);"
        "Wait-Process -Id $p.Id"
    )
    source = f"""
. {_ps_literal(LIFECYCLE)}
$failed = $false
try {{
    [void](Invoke-Gate13Contained -Executable {_ps_literal(POWERSHELL)} -Arguments ([string[]]@(
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        {_ps_literal(root_command)}
    )) -WorkingDirectory {_ps_literal(tmp_path)} -TimeoutSeconds 1)
}}
catch {{
    $failed = $true
}}
if (-not (Test-Path -LiteralPath {_ps_literal(pid_file)})) {{ throw 'pid missing' }}
$pidValue = [int][IO.File]::ReadAllText({_ps_literal(pid_file)})
Start-Sleep -Milliseconds 100
$aliveAfter = $null -ne (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)
[Console]::Out.WriteLine((@{{
    failed = $failed
    descendant_alive_after = $aliveAfter
}} | ConvertTo-Json -Compress))
"""
    result = _run_powershell(source, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "failed": True,
        "descendant_alive_after": False,
    }


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="native Windows PowerShell is required")
def test_partial_key_create_cleanup_targets_reserved_label_only(tmp_path):
    control = "drift_control_" + "A" * 43
    source = f"""
. {_ps_literal(INFERENCE)}
function Assert-Gate13NoTranscript {{ }}
function Initialize-Gate13CredentialInterop {{ }}
function Read-Gate13ControlKey {{ return '{control}' }}
function Get-Gate13SelectedProfile {{
    return [pscustomobject]@{{
        ModelId = 'Qwen3.5 2B'
        ManifestDigest = '3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33'
    }}
}}
$script:createLost = $false
$script:reservedRevoked = $false
$script:deleted = New-Object System.Collections.ArrayList
function New-KeyRecord([string]$id, [string]$label, [object]$revoked) {{
    return [pscustomobject]@{{
        id = $id
        label = $label
        fingerprint = 'abcdef012345'
        created_at = [int64]1
        revoked_at = $revoked
    }}
}}
function Invoke-Gate13LoopbackJson {{
    param([string]$Method, [string]$Path, [string]$BearerToken, [object]$Body)
    if ($Path -eq '/control/v1/status') {{ return [pscustomobject]@{{}} }}
    if ($Path -eq '/control/v1/keys' -and $Method -eq 'POST') {{
        $script:createLost = $true
        throw 'simulated lost response'
    }}
    if ($Path -eq '/control/v1/keys' -and $Method -eq 'GET') {{
        $keys = New-Object System.Collections.ArrayList
        [void]$keys.Add((New-KeyRecord 'key_1111111111111111' 'baseline' $null))
        if ($script:createLost) {{
            [void]$keys.Add((New-KeyRecord 'key_2222222222222222' 'unrelated-concurrent' $null))
            $revoked = if ($script:reservedRevoked) {{ [int64]2 }} else {{ $null }}
            [void]$keys.Add((New-KeyRecord 'key_3333333333333333' $script:QualificationKeyLabel $revoked))
        }}
        return [pscustomobject]@{{ keys = @($keys) }}
    }}
    if ($Path -match '^/control/v1/keys/(key_[0-9a-f]{{16}})$' -and $Method -eq 'DELETE') {{
        [void]$script:deleted.Add($Matches[1])
        if ($Matches[1] -eq 'key_3333333333333333') {{ $script:reservedRevoked = $true }}
        return [pscustomobject]@{{}}
    }}
    throw 'unexpected request'
}}
$failed = $false
try {{ [void](Invoke-Gate13WindowsLocalhostInference) }} catch {{ $failed = $true }}
[Console]::Out.WriteLine((@{{
    failed = $failed
    deleted = @($script:deleted)
}} | ConvertTo-Json -Compress))
"""
    result = _run_powershell(source, tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["failed"] is True
    assert payload["deleted"] == ["key_3333333333333333"]


def _acquisition_record(*, digest: str, source_verified: bool = True, cache_after: int = 3):
    return {
        "schema_version": 1,
        "acquired_at_unix": 1,
        "runtime": {"python": "3", "platform": "Windows", "drift": "2"},
        "model": {
            "id": "Fixture",
            "manifest_digest": f"sha256:{digest}",
            "repository": "owner/repo",
            "revision": "a" * 40,
            "dtype": "float32",
        },
        "selection": {
            "startup_artifact_paths": ["config.json"],
            "weight_artifact_paths": [],
            "artifact_count": 1,
            "artifact_bytes": 3,
            "weight_artifact_bytes": 0,
        },
        "artifacts": [
            {
                "path": "config.json",
                "role": "config",
                "size_bytes": 3,
                "sha256": hashlib.sha256(b"abc").hexdigest(),
                "materialization_attempts": 1,
                "resumptions": 0,
                "resumed_from_bytes": [],
                "elapsed_seconds": 0.1,
            }
        ],
        "transfer": {
            "direct_upstream_transfer": True,
            "mirror_used": False,
            "source_class_verified": source_verified,
            "transport_override_present": False,
            "elapsed_seconds": 0.1,
            "max_resumptions": 3,
            "resumptions": 0,
            "completed": True,
        },
        "storage": {
            "cold_start": True,
            "cache_bytes_before": 0,
            "cache_bytes_after": cache_after,
            "cache_growth_bytes": cache_after,
            "verified": True,
        },
        "privacy": {
            "credentials_retained": False,
            "local_paths_retained": False,
            "response_bodies_retained": False,
            "urls_retained": False,
        },
    }


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="native Windows PowerShell is required")
@pytest.mark.parametrize(
    ("source_verified", "cache_after", "expected_passed"),
    [(True, 3, True), (False, 3, False), (True, 4, False)],
)
def test_acquisition_projector_fails_closed_on_raw_proof_tampering(
    tmp_path, source_verified, cache_after, expected_passed
):
    digest = "d" * 64
    cache = tmp_path / "cache"
    snapshot = cache / "manifest-artifacts" / digest / "snapshot"
    snapshot.mkdir(parents=True)
    artifact_path = snapshot / "config.json"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    raw = json.dumps(
        _acquisition_record(
            digest=digest,
            source_verified=source_verified,
            cache_after=cache_after,
        ),
        separators=(",", ":"),
    )
    source = f"""
. {_ps_literal(LIFECYCLE)}
$script:RawAcquisition = @'
{raw}
'@
function Test-Gate13NoTransportOverride {{ }}
function Invoke-Gate13Contained {{
    [IO.File]::WriteAllBytes({_ps_literal(artifact_path)}, [byte[]](97, 98, 99))
    return $script:RawAcquisition
}}
$script:LifecycleAcquisitionInvoked = $false
$profile = [pscustomobject]@{{
    ModelId = 'Fixture'
    ManifestDigest = '{digest}'
    RevisionCommit = '{'a' * 40}'
    SelectedCount = 1
    SelectedBytes = [int64]3
}}
$manifest = [pscustomobject]@{{
    artifacts = @([pscustomobject]@{{
        path = 'config.json'
        role = 'config'
        size = [int64]3
        sha256 = '{hashlib.sha256(b"abc").hexdigest()}'
    }})
}}
$context = [pscustomobject]@{{
    ManifestPath = {_ps_literal(manifest_path)}
    Manifest = $manifest
    ManifestDigest = '{digest}'
    CacheDir = {_ps_literal(cache)}
}}
$passed = $true
$errorMessage = $null
try {{
    $result = Invoke-Gate13VerifiedAcquisition -Profile $profile -Context $context
    $passed = ($result.Inventory.Count -eq 1 -and $result.Inventory.Bytes -eq 3)
}}
catch {{
    $passed = $false
    $errorMessage = $_.Exception.Message
}}
[Console]::Out.WriteLine((@{{ passed = $passed; error = $errorMessage }} | ConvertTo-Json -Compress))
"""
    result = _run_powershell(source, tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["passed"] is expected_passed, payload["error"]


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="native Windows PowerShell is required")
def test_path_guards_reject_single_backslash_and_traversal(tmp_path):
    source = f"""
. {_ps_literal(LIFECYCLE)}
$superscriptDevice = [string]::Concat('COM', [char]0x00B9, '.bin')
$controlName = [string]::Concat('bad', [char]1, '.bin')
[Console]::Out.WriteLine((@{{
    artifact_backslash = Test-Gate13SafeArtifactPath '..\\evil.bin'
    artifact_forward_traversal = Test-Gate13SafeArtifactPath '../evil.bin'
    artifact_device = Test-Gate13SafeArtifactPath 'weights/CON.txt'
    artifact_console_output = Test-Gate13SafeArtifactPath 'weights/conout$.bin'
    artifact_superscript_device = Test-Gate13SafeArtifactPath ('weights/' + $superscriptDevice)
    artifact_trailing_dot = Test-Gate13SafeArtifactPath 'weights/model.'
    artifact_trailing_space = Test-Gate13SafeArtifactPath 'weights/model '
    artifact_control = Test-Gate13SafeArtifactPath ('weights/' + $controlName)
    artifact_invalid_character = Test-Gate13SafeArtifactPath 'weights/model?.bin'
    artifact_valid = Test-Gate13SafeArtifactPath 'weights/model.safetensors'
    archive_backslash = Test-Gate13SafeArchivePath 'CommunityAI\\evil.exe'
    archive_forward_traversal = Test-Gate13SafeArchivePath 'CommunityAI/../evil.exe'
    archive_double_trailing_slash = Test-Gate13SafeArchivePath 'CommunityAI/node//'
    archive_device = Test-Gate13SafeArchivePath 'CommunityAI/AUX/file.exe'
    archive_console_input = Test-Gate13SafeArchivePath 'CommunityAI/CONIN$'
    archive_valid = Test-Gate13SafeArchivePath 'CommunityAI/node/CommunityAI-Node.exe'
}} | ConvertTo-Json -Compress))
"""
    result = _run_powershell(source, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "artifact_backslash": False,
        "artifact_forward_traversal": False,
        "artifact_device": False,
        "artifact_console_output": False,
        "artifact_superscript_device": False,
        "artifact_trailing_dot": False,
        "artifact_trailing_space": False,
        "artifact_control": False,
        "artifact_invalid_character": False,
        "artifact_valid": True,
        "archive_backslash": False,
        "archive_forward_traversal": False,
        "archive_double_trailing_slash": False,
        "archive_device": False,
        "archive_console_input": False,
        "archive_valid": True,
    }


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="native Windows PowerShell is required")
def test_pause_queries_exact_workers_and_requires_original_pid_absent(tmp_path):
    source = f"""
. {_ps_literal(LIFECYCLE)}
$script:LifecycleProcess = [pscustomobject]@{{ ActiveProcessCount = 3 }}
$script:workerQueries = 0
function Invoke-Gate13LoopbackJson {{
    param([string]$Method, [string]$Path, [string]$BearerToken, [object]$Body)
    if ($Method -ne 'GET') {{ throw 'unexpected method' }}
    if ($Path -eq '/control/v1/status') {{
        return [pscustomobject]@{{
            contribution = [pscustomobject]@{{
                workers = @([pscustomobject]@{{
                    id = 'automatic'
                    state = 'paused'
                    desired_running = $false
                }})
            }}
        }}
    }}
    if ($Path -eq '/control/v1/workers') {{
        $script:workerQueries += 1
        return [pscustomobject]@{{
            workers = @([pscustomobject]@{{
                id = 'automatic'
                state = 'paused'
                desired_running = $false
                operator_paused = $true
                pid = $null
            }})
        }}
    }}
    throw 'unexpected path'
}}
$liveRejected = $false
try {{
    Wait-Gate13ContributionPaused -ControlToken 'x' -BaselineProcessCount 3 -WorkerPid $PID -TimeoutSeconds 1
}}
catch {{
    $liveRejected = $true
}}
Wait-Gate13ContributionPaused `
    -ControlToken 'x' `
    -BaselineProcessCount 3 `
    -WorkerPid 2147483000 `
    -TimeoutSeconds 2
[Console]::Out.WriteLine((@{{
    live_pid_rejected = $liveRejected
    exact_worker_queries = $script:workerQueries
}} | ConvertTo-Json -Compress))
"""
    result = _run_powershell(source, tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["live_pid_rejected"] is True
    assert payload["exact_worker_queries"] >= 2


def test_adapter_contains_exact_safety_and_lifecycle_contracts():
    lifecycle = LIFECYCLE.read_text(encoding="utf-8")
    inference = INFERENCE.read_text(encoding="utf-8")

    phases = [
        "package_verification",
        "clean_install",
        "packaged_self_tests",
        "signed_bootstrap",
        "selected_bytes",
        "verified_acquisition",
        "localhost_inference",
        "bounded_contribution",
        "contribution_pause",
        "restart_cache_reuse",
        "manual_replacement",
        "recovery",
        "uninstall_retain",
        "retained_data_reinstall",
        "uninstall_delete",
        "process_cleanup",
    ]
    positions = [lifecycle.index(f'-Name "{phase}"') for phase in phases]
    assert positions == sorted(positions)
    assert lifecycle.count('-Name "') >= len(phases)

    for required in (
        "CreateSuspended",
        "JobObjectLimitKillOnJobClose",
        "AssignProcessToJobObject(job, process.process)",
        "ResumeThread(process.thread)",
        "StopGracefully",
        "RequestWindowClose",
        "ForceAndVerify",
        "--require_direct_upstream",
        '"source_class_verified"',
        '"transport_override_present"',
        '"cache_bytes_after"',
        '"cache_growth_bytes"',
        "--delete-control-key",
        "RunWithInput",
        '"/control/v1/workers"',
        "WorkerPid",
        '"2.6.0+cu124"',
        "-TimeoutSeconds 3600",
        "_internal\\bootstrap\\catalog-bootstrap.json",
        '"package_sha256"',
        '"package_bytes"',
        '"source_commit"',
        '"model_id"',
        '"manifest_digest"',
    ):
        assert required in lifecycle

    assert lifecycle.index("AssignProcessToJobObject(job, process.process)") < lifecycle.index(
        "ResumeThread(process.thread)"
    )
    assert lifecycle.index("StopGracefully") < lifecycle.index("ForceAndVerify")
    assert "http://127.0.0.1:8080" in inference
    assert 'model = "auto"' in inference
    assert "$handler.AllowAutoRedirect = $false" in inference
    assert "$handler.UseProxy = $false" in inference
    assert 'account + "@" + service' in inference
    assert "$current.QualificationIds" in inference
    assert "$current.ActiveIds | Where-Object" not in inference

    secret_markers = ("BearerToken =", "apiToken =", "controlToken =")
    for marker in secret_markers:
        assert f"$env:{marker}" not in lifecycle
        assert f"$env:{marker}" not in inference


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="native Windows PowerShell is required")
def test_argument_failure_is_one_generic_canonical_record(tmp_path):
    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LIFECYCLE),
            "unexpected",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert result.stderr == ""
    assert result.stdout == (
        '{"failure_code":"windows_packaged_lifecycle_failed",' '"result":"failed","schema_version":1}\n'
    )


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="native Windows PowerShell is required")
def test_package_audit_verifies_archive_inventory_metrics_and_publication(tmp_path):
    import zipfile

    stage = tmp_path / "stage"
    payload = stage / "payload"
    audit = stage / "audit"
    payload.mkdir(parents=True)
    audit.mkdir()
    files = {
        "CommunityAI/CommunityAI.exe": b"desktop",
        "CommunityAI/node/CommunityAI-Node.exe": b"node",
        "CommunityAI/_internal/bootstrap/catalog-bootstrap.json": b"bootstrap",
    }
    artifacts = []
    for name, body in sorted(files.items()):
        artifacts.append(
            {
                "path": name,
                "kind": "file",
                "mode": 0o644,
                "sha256": hashlib.sha256(body).hexdigest(),
                "size_bytes": len(body),
            }
        )
    archive_path = payload / "communityai-desktop-windows.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    archive_body = archive_path.read_bytes()

    checksum_text = "".join(f"{item['sha256']}  {item['path']}\n" for item in artifacts)
    catalog_digest = "sha256:" + "c" * 64
    bootstrap_digest = "sha256:" + "d" * 64
    bootstrap_file_digest = (
        "sha256:" + hashlib.sha256(files["CommunityAI/_internal/bootstrap/catalog-bootstrap.json"]).hexdigest()
    )
    publication = {
        "schema_version": 1,
        "scope": "catalog-publication-bundle",
        "catalog_id": "communityai-public-alpha-v1",
        "catalog_sequence": 1,
        "catalog_digest": catalog_digest,
        "bootstrap_digest": bootstrap_digest,
        "bundle_index_digest": "sha256:" + "e" * 64,
        "member_count": 1,
        "member_digests": {"catalog-bootstrap.json": bootstrap_file_digest},
        "complete_release_qualification": False,
    }
    install_archive = {
        "schema_version": 1,
        "path": "communityai-desktop-windows.zip",
        "format": "zip",
        "platform": "Windows",
        "artifact_root": "CommunityAI",
        "sha256": hashlib.sha256(archive_body).hexdigest(),
        "size_bytes": len(archive_body),
        "entry_count": len(files),
        "preserves_executable_modes": False,
        "preserves_internal_file_symlinks": False,
    }
    source_commit = "f" * 40
    source_tree = "a" * 40
    bundle_bytes = sum(len(body) for body in files.values())
    node_bytes = len(files["CommunityAI/node/CommunityAI-Node.exe"])
    node_runtime = {
        "schema_version": 1,
        "application": "CommunityAI-Node",
        "drift": "2.3.0.dev2",
        "torch": "2.6.0+cu124",
        "transformers": "5.13.1",
        "hivemind": "1.1.12",
        "fastapi": "0.141.1",
        "uvicorn": "0.52.4",
        "keyring": "25.7.0",
        "p2pd": "p2pd.exe",
        "catalog_bootstrap_schema": 1,
        "frozen": True,
    }
    worker_runtime = {
        "schema_version": 1,
        "application": "CommunityAI-Worker",
        "entrypoint": "server",
        "server_class": "Server",
        "model_loading_performed": False,
        "network_join_performed": False,
        "throughput_mode": "dry_run",
        "training_rpcs_enabled": False,
        "process_lifetime_guard_armed": True,
        "frozen": True,
    }
    metrics = {
        "schema_version": 1,
        "application": "CommunityAI",
        "package": "communityai-desktop",
        "platform": "Windows",
        "python": "3.12",
        "bundle_bytes": bundle_bytes,
        "file_count": len(files),
        "runtime": {"shell": "pyside", "framework": "PySide6", "version": "6.11.2"},
        "acceptance": {
            "api_version": 1,
            "model_count": 3,
            "worker_actions": 3,
            "key_lifecycle": "passed",
            "contribution_policy": "passed",
            "policy_update": "passed",
            "auto_selection": "passed",
        },
        "ui_smoke_passed": True,
        "onboarding_ui_smoke_passed": True,
        "node_sidecar": {
            "relative_executable": "node/CommunityAI-Node.exe",
            "bundle_bytes": node_bytes,
            "file_count": 1,
            "runtime": node_runtime,
            "worker_runtime": worker_runtime,
            "self_test_passed": True,
            "worker_self_test_passed": True,
            "node_entrypoint_smoke_passed": True,
            "worker_entrypoint_smoke_passed": True,
        },
        "console_window": False,
        "signed": False,
        "catalog_bootstrap_bundled": True,
        "catalog_publication_bundle": publication,
        "release_artifacts": {
            "schema_version": 1,
            "artifact_count": len(files),
            "artifact_bytes": bundle_bytes,
            "checksums_sha256": hashlib.sha256(checksum_text.encode()).hexdigest(),
            "install_archive": install_archive,
            "source_commit": source_commit,
            "source_tree": source_tree,
            "unsigned": True,
            "complete_release_qualification": False,
        },
    }
    metrics_body = json.dumps(metrics, separators=(",", ":"), sort_keys=True).encode()
    (audit / "desktop-metrics.json").write_bytes(metrics_body)
    provenance = {
        "schema_version": 1,
        "product": "CommunityAI",
        "package": "communityai-desktop",
        "release_channel": "public-alpha",
        "source_commit": source_commit,
        "source_tree": source_tree,
        "build_workflow": "fixture",
        "build_platform": "Windows",
        "build_python": "3.12",
        "build_pyinstaller": "6",
        "artifact_root": "CommunityAI",
        "checksum_manifest": "SHA256SUMS",
        "artifacts": artifacts,
        "install_archive": install_archive,
        "desktop_metrics": {
            "schema_version": 1,
            "path": "desktop-metrics.json",
            "sha256": hashlib.sha256(metrics_body).hexdigest(),
            "size_bytes": len(metrics_body),
        },
        "catalog_publication_bundle": publication,
        "unsigned": True,
        "publisher_signature": False,
        "automatic_updates": False,
        "complete_release_qualification": False,
    }
    (audit / "provenance.json").write_text(
        json.dumps(provenance, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    metadata = {
        "schema_version": 1,
        "product": "CommunityAI",
        "package": "communityai-desktop",
        "release_channel": "public-alpha",
        "warning": "Unsigned public-alpha fixture",
        "unsigned": True,
        "publisher_signature": False,
        "automatic_updates": False,
        "supported_platforms": ["Windows", "Linux"],
        "macos_supported": False,
        "credits_enabled": False,
        "complete_release_qualification": False,
        "artifact_root": "CommunityAI",
        "artifact_inventory": "regular-files-and-relative-internal-file-symlinks-with-file-modes",
        "checksum_manifest": "SHA256SUMS",
        "install_archive_required": True,
        "install_archive_provenance": "provenance.json#install_archive",
        "desktop_metrics": "desktop-metrics.json",
        "provenance": "provenance.json",
    }
    (audit / "release-metadata.json").write_text(
        json.dumps(metadata, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    (audit / "SHA256SUMS").write_text(checksum_text, encoding="utf-8", newline="\n")
    run_input = stage / "gate13-windows-run.json"
    run_record = {
        "schema_version": 1,
        "run_id": "gate13-fixture",
        "source_commit": source_commit,
        "package_version": node_runtime["drift"],
        "package_sha256": hashlib.sha256(archive_body).hexdigest(),
        "package_bytes": len(archive_body),
        "model_id": "Qwen3.5 2B",
        "manifest_digest": "3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33",
    }
    run_input.write_text(
        json.dumps(run_record, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    controller = stage / "gate13_packaged_lifecycle.py"
    controller.write_text("# fixture\n", encoding="utf-8")

    source = f"""
. {_ps_literal(LIFECYCLE)}
$script:LifecycleArchive = {_ps_literal(archive_path)}
$script:LifecycleAuditRoot = {_ps_literal(audit)}
$script:LifecycleRunInput = {_ps_literal(run_input)}
$script:LifecycleController = {_ps_literal(controller)}
$result = Test-Gate13PackageAudit
[Console]::Out.WriteLine((@{{
    artifact_count = $result.ArtifactCount
    weight_count = $result.WeightCount
    package_digest = $result.PackageDigest
    catalog_digest = $result.PublicationCatalogDigest
    bootstrap_digest = $result.PublicationBootstrapDigest
    bootstrap_file_digest = $result.PublicationBootstrapFileDigest
}} | ConvertTo-Json -Compress))
"""
    result = _run_powershell(source, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "artifact_count": len(files),
        "weight_count": 0,
        "package_digest": hashlib.sha256(archive_body).hexdigest(),
        "catalog_digest": catalog_digest,
        "bootstrap_digest": bootstrap_digest,
        "bootstrap_file_digest": bootstrap_file_digest,
    }

    def rewrite_bound_audit() -> None:
        current_metrics_body = json.dumps(metrics, separators=(",", ":"), sort_keys=True).encode()
        (audit / "desktop-metrics.json").write_bytes(current_metrics_body)
        provenance["desktop_metrics"]["sha256"] = hashlib.sha256(current_metrics_body).hexdigest()
        provenance["desktop_metrics"]["size_bytes"] = len(current_metrics_body)
        (audit / "provenance.json").write_text(
            json.dumps(provenance, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

    publication["bootstrap_digest"] = bootstrap_digest.upper()
    rewrite_bound_audit()
    uppercase_digest = _run_powershell(source, tmp_path)
    assert uppercase_digest.returncode != 0
    publication["bootstrap_digest"] = bootstrap_digest

    publication["member_digests"]["catalog-bootstrap.json"] = "sha256:" + "0" * 64
    rewrite_bound_audit()
    mismatched_bootstrap_file = _run_powershell(source, tmp_path)
    assert mismatched_bootstrap_file.returncode != 0
    publication["member_digests"]["catalog-bootstrap.json"] = bootstrap_file_digest
    rewrite_bound_audit()

    tampered_run = dict(run_record)
    tampered_run["package_sha256"] = "0" * 64
    run_input.write_text(
        json.dumps(tampered_run, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    tampered = _run_powershell(source, tmp_path)
    assert tampered.returncode != 0
    run_input.write_text(
        json.dumps(run_record, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )

    cpu_metrics = json.loads(metrics_body)
    cpu_metrics["node_sidecar"]["runtime"]["torch"] = "2.6.0+cpu"
    cpu_metrics_body = json.dumps(cpu_metrics, separators=(",", ":"), sort_keys=True).encode()
    (audit / "desktop-metrics.json").write_bytes(cpu_metrics_body)
    provenance["desktop_metrics"]["sha256"] = hashlib.sha256(cpu_metrics_body).hexdigest()
    provenance["desktop_metrics"]["size_bytes"] = len(cpu_metrics_body)
    (audit / "provenance.json").write_text(
        json.dumps(provenance, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    cpu_rejected = _run_powershell(source, tmp_path)
    assert cpu_rejected.returncode != 0

    (audit / "desktop-metrics.json").write_bytes(cpu_metrics_body + b"x")
    digest_rejected = _run_powershell(source, tmp_path)
    assert digest_rejected.returncode != 0
