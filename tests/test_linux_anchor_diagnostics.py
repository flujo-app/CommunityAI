"""Real Linux files/locks; service, packaged model and keyring are fixtures."""

import json
import os
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from communityai_desktop.profiles import VolunteerProfile
from test_catalog_publication import _documents
from test_linux_anchor_bootstrap import Store
from test_linux_anchor_native import running  # noqa: F401
from test_linux_anchor_state_native import journal  # noqa: F401

from drift.catalog_release import write_catalog_publication_bundle
from drift.node import (
    linux_anchor_bootstrap as bootstrap,
    linux_anchor_diagnostics as diagnostics,
    linux_anchor_resources as resources,
    linux_anchor_state as state,
    worker_loading as private,
)

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not os.environ.get("COMMUNITYAI_TEST_CGROUP_ROOT"),
    reason="requires isolated native Linux fixture",
)


@pytest.fixture
def installed(journal):
    profile = VolunteerProfile(journal.profile)
    resources.create_directories(profile.root, journal.owner.binding)
    lease = state.node_lease(profile.root)
    private._exclusive(profile.data_dir / "resource-reservations" / "admission.lock", {})
    resources.create_resources(profile.root, journal.owner.binding)
    config, envelope, manifests = _documents()
    bundle = journal.profile.parent / "bundle"
    write_catalog_publication_bundle(bundle, config, envelope, manifests)
    plan = bootstrap.build_bootstrap_plan(bundle, profile, initialize=True)
    store = Store()
    owner = bootstrap.AnchorBootstrap(plan, profile, store)
    owner.bind(journal.owner, lambda: None, initialize=True)
    value = SimpleNamespace(profile=profile, plan=plan, store=store, owner=owner, journal=journal)
    try:
        yield value
    finally:
        lease.close()


def _prepare(f):
    f.owner.prepare(
        dict(
            phase="starting",
            operation="start",
            request_id="a" * 32,
            generation=dict(id="b" * 32, cgroup=None, pid=None),
        ),
        cancelled=lambda: False,
    )


def _image(root):
    return {
        str(path.relative_to(root)): (
            path.lstat().st_ino,
            path.lstat().st_mode,
            path.lstat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in (root, *root.rglob("*"))
    }


def _report(f):
    result = diagnostics.diagnose_profile(f.profile, f.plan)
    text = json.dumps(result)
    assert all(result[key] is False for key in ("admission", "maintenance", "cleanup_complete", "recovery_allowed"))
    assert result["keyring"] == "not_queried" and result["output_contents"] == "not_verified"
    assert str(f.profile.root) not in text and f.owner.binding not in text
    assert f.owner.value["transaction"] not in text
    assert not f.store.secret or f.store.secret not in text
    assert len(text) < 4096
    return result


def test_diagnostic_does_not_write_lock_query_credentials_or_start(installed, monkeypatch):
    import fcntl

    f = installed
    _prepare(f)
    before = _image(f.profile.root)
    original_open = os.open

    def safe_open(path, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return original_open(path, flags, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("read-only inventory attempted mutation or credentials")

    with monkeypatch.context() as m:
        m.setattr(os, "open", safe_open)
        for name in ("write", "mkdir", "unlink", "rename", "replace", "chmod", "fsync"):
            m.setattr(os, name, forbidden)
        m.setattr(fcntl, "flock", forbidden)
        m.setattr(f.store, "get", forbidden)
        m.setattr(f.store, "set", forbidden)
        result = _report(f)
    assert result["observation"] == "inventory_observed"
    assert result["reasons"] == []
    assert result["credential_intent"] == "ready" and result["package"] == "recorded_match"
    assert before == _image(f.profile.root)


def test_diagnostic_reads_legacy_v1_with_derived_catalog_binding(installed):
    f = installed
    _prepare(f)
    legacy = dict(f.owner.value)
    legacy["schema_version"] = 1
    legacy.pop("catalog_binding")
    private._replace(f.owner.path, legacy)
    result = _report(f)
    assert result["reasons"] == [] and result["package"] == "recorded_match"


def test_diagnostic_uses_immutable_catalog_binding_but_checks_dynamic_binding(installed):
    from drift.node.linux_anchor_entry import catalog_discriminator

    f = installed
    _prepare(f)
    value = dict(f.owner.value, catalog_binding="c" * 64)
    private._replace(f.owner.path, value)
    catalog_lock = f.profile.root / diagnostics._FILES["catalog_lock"]
    identity = catalog_lock.stat()
    catalog_lock.write_bytes(private._encode(catalog_discriminator(f.profile.root, value["catalog_binding"])))
    assert os.path.samestat(identity, catalog_lock.stat())
    result = _report(f)
    assert result["reasons"] == [] and result["package"] == "recorded_match"

    value["binding"] = "d" * 64
    private._replace(f.owner.path, value)
    result = _report(f)
    assert "bootstrap_record_invalid" in result["reasons"]


def test_empty_existing_profile_and_absent_profile_are_not_adopted(installed):
    f = installed
    root = f.profile.root / "unrelated-empty"
    root.mkdir(mode=0o700)
    result = diagnostics.diagnose_profile(VolunteerProfile(root), f.plan)
    assert "state_missing" in result["reasons"] and list(root.iterdir()) == []
    root = f.profile.root / "absent"
    result = diagnostics.diagnose_profile(VolunteerProfile(root), f.plan)
    assert "profile_or_ancestor_missing" in result["reasons"] and not root.exists()


@pytest.mark.parametrize("label", list(diagnostics._FILES))
def test_missing_fixed_evidence_retained_without_recreation(installed, label):
    f = installed
    path = f.profile.root / diagnostics._FILES[label]
    path.rename(path.with_name("retained-" + path.name))
    before = _image(f.profile.root)
    result = _report(f)
    assert label + "_missing" in result["reasons"]
    assert not path.exists() and before == _image(f.profile.root)


@pytest.mark.parametrize("label", ["state_lock", "lifetime_lock", "admission_lock", "catalog_lock", "config_lock"])
def test_replacement_lock_keeps_lost_identity_visible(installed, label):
    f = installed
    path = f.profile.root / diagnostics._FILES[label]
    payload = path.read_bytes()
    path.rename(path.with_name("retained-" + path.name))
    path.write_bytes(payload)
    path.chmod(0o600)
    result = _report(f)
    expected = (
        "state_storage_binding_unverifiable"
        if label == "state_lock"
        else (
            "resource_binding_unverifiable"
            if label in {"lifetime_lock", "admission_lock"}
            else "bootstrap_storage_binding_unverifiable"
        )
    )
    assert expected in result["reasons"]


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "mode", "oversize", "duplicate", "secret"])
def test_unsafe_or_malformed_files_never_leak_or_block_on_fifo(installed, kind):
    f = installed
    path = f.owner.path
    path.rename(path.with_name("retained-bootstrap"))
    if kind == "symlink":
        path.symlink_to(path.with_name("retained-bootstrap"))
    elif kind == "hardlink":
        os.link(path.with_name("retained-bootstrap"), path)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_bytes(
            {
                "mode": b"{}",
                "oversize": b"x" * 8193,
                "duplicate": b'{"a":1,"a":2}',
                "secret": b'{"bundle":"synthetic-secret-do-not-print"}',
            }[kind]
        )
        path.chmod(0o644 if kind == "mode" else 0o600)
    result = _report(f)
    assert "synthetic-secret-do-not-print" not in json.dumps(result)
    assert any(reason.startswith("bootstrap_") or reason.startswith("package_binding") for reason in result["reasons"])


def test_pending_credentials_and_outputs_are_retained_intent_not_success(installed):
    f = installed
    f.owner._write(
        attempt=dict(request_id="a" * 32, generation="b" * 32), admitted_at_ms=f.plan.initialization_admitted_at_ms
    )
    f.owner._write(credential="pending", credential_digest="c" * 64)
    result = _report(f)
    assert result["credential_intent"] == "pending"
    assert "credential_write_intent_retained" in result["reasons"]
    assert f.store.sets == 0
    f.owner._write(credential="ready", pending=True)
    assert "output_write_intent_retained" in _report(f)["reasons"]
    assert f.store.sets == 0


def test_missing_acknowledged_output_and_future_output_are_diagnosed(installed):
    f = installed
    f.profile.config_path.write_bytes(b"{}")
    f.profile.config_path.chmod(0o600)
    assert "bootstrap_output_inventory_unverifiable" in _report(f)["reasons"]
    f.profile.config_path.unlink()
    _prepare(f)
    (f.profile.root / f.plan.outputs[0][0]).unlink()
    assert "bootstrap_output_inventory_unverifiable" in _report(f)["reasons"]


def test_ready_user_config_changes_are_not_misreported_as_original_bytes(installed):
    f = installed
    _prepare(f)
    f.profile.config_path.write_bytes(b'{"intentionally":"not-validated-by-inventory"}')
    result = _report(f)
    assert result["reasons"] == [] and result["output_contents"] == "not_verified"


def test_package_boot_and_service_changes_have_fixed_reasons(installed, monkeypatch):
    f = installed
    result = diagnostics.diagnose_profile(f.profile, replace(f.plan, digest="f" * 64))
    assert "package_binding_changed_or_invalid" in result["reasons"]
    assert "package_unavailable" in diagnostics.diagnose_profile(f.profile)["reasons"]
    monkeypatch.setattr(diagnostics, "current_recovery_identity", lambda: SimpleNamespace(to_json=lambda: {}))
    monkeypatch.setattr(diagnostics.anchor, "inspect_service", lambda: SimpleNamespace(to_json=lambda: {}))
    result = _report(f)
    assert {"machine_or_boot_changed", "service_invocation_changed"} <= set(result["reasons"])


def test_unavailable_live_observation_redacts_exception(installed, monkeypatch):
    def fail():
        raise OSError("synthetic-secret-do-not-print")

    monkeypatch.setattr(diagnostics, "current_recovery_identity", fail)
    monkeypatch.setattr(diagnostics.anchor, "inspect_service", fail)
    result = _report(installed)
    assert {"machine_or_boot_unavailable", "service_unavailable_or_unqualified"} <= set(result["reasons"])
    assert "synthetic-secret-do-not-print" not in json.dumps(result)


@pytest.mark.parametrize("kind", ["root_rename", "state_replace", "missing_appears"])
def test_snapshot_changes_are_inconclusive(installed, monkeypatch, kind):
    f = installed
    if kind == "missing_appears":
        (f.profile.root / "node-lifetime.lock").unlink()
    original = diagnostics._Snapshot.validate

    def mutate(snapshot):
        if kind == "root_rename":
            f.profile.root.rename(f.profile.root.with_name("retained-profile"))
            f.profile.root.mkdir(mode=0o700)
        elif kind == "state_replace":
            path = f.journal.owner.path
            payload = path.read_bytes()
            path.rename(path.with_name("retained-state"))
            path.write_bytes(payload)
            path.chmod(0o600)
        else:
            (f.profile.root / "node-lifetime.lock").touch(mode=0o600)
        original(snapshot)

    monkeypatch.setattr(diagnostics._Snapshot, "validate", mutate)
    result = _report(f)
    assert result["observation"] == "inconclusive"
    assert "snapshot_changed_or_unverifiable" in result["reasons"]
    assert result["credential_intent"] == result["saved_phase"] == "unknown"


def test_symlink_ancestor_is_never_followed(installed):
    f = installed
    link = f.profile.root.parent / "alias"
    link.symlink_to(f.profile.root, target_is_directory=True)
    result = diagnostics.diagnose_profile(VolunteerProfile(link), f.plan)
    assert result["reasons"] == ["profile_unreadable_or_unsafe"]
