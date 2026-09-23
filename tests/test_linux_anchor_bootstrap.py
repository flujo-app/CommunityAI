"""Signed-plan tests; native filesystem/keyring doubles are labelled explicitly."""

import json
import os
import sys
import threading
from copy import deepcopy
from types import SimpleNamespace

import pytest
from communityai_desktop.credentials import CredentialMissingError
from communityai_desktop.profiles import VolunteerProfile
from test_catalog_publication import _documents

from drift.catalog_release import write_catalog_publication_bundle
from drift.node import linux_anchor_bootstrap as bootstrap
from drift.node.resource_recovery import RecoverableStateError


def _codec_fixture(version=1, *, binding="a" * 64, catalog_binding="c" * 64, marker="absent"):
    plan = SimpleNamespace(
        bundle_digest="b" * 64,
        digest="d" * 64,
        outputs=(("node/output", b"payload"),),
        envelope=SimpleNamespace(signed=SimpleNamespace(issued_at_ms=100, expires_at_ms=200)),
    )
    parents = {"anchor"}
    locks = ("node/.catalog-bootstrap.lock",)
    value = dict(
        schema_version=version,
        binding=binding,
        transaction="e" * 32,
        bundle=plan.bundle_digest,
        plan=plan.digest,
        directories={"anchor": [1, 2]},
        locks={locks[0]: [3, 4]},
        service="service",
        account="account",
        attempt=None,
        admitted_at_ms=None,
        progress=0,
        pending=False,
        credential="absent",
        credential_digest=None,
        ready=False,
    )
    if version == 2:
        value["catalog_binding"] = catalog_binding
    if marker != "absent":
        value.update(
            attempt={"request_id": "1" * 32, "generation": "2" * 32},
            admitted_at_ms=150,
            credential="pending" if marker == "pending" else "ready",
            credential_digest="3" * 64,
        )
    if marker == "output_pending":
        value["pending"] = True
    arguments = dict(
        plan=plan,
        binding=binding,
        service="service",
        account="account",
        parents=parents,
        lock_names=locks,
    )
    return value, arguments


def test_strict_bootstrap_codec_reads_v1_and_separates_v2_catalog_binding():
    from drift.node.linux_anchor_entry import bootstrap_catalog_binding

    legacy, arguments = _codec_fixture()
    bootstrap.validate_bootstrap_record(legacy, **arguments)
    assert bootstrap_catalog_binding(legacy) == legacy["binding"]
    current, arguments = _codec_fixture(version=2)
    bootstrap.validate_bootstrap_record(current, **arguments)
    assert bootstrap_catalog_binding(current) == current["catalog_binding"] != current["binding"]


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(schema_version=True),
        lambda value: value.update(schema_version=3),
        lambda value: value.update(unexpected=True),
        lambda value: value.update(catalog_binding="f" * 64),
    ],
)
def test_strict_v1_bootstrap_codec_rejects_schema_extension(change):
    value, arguments = _codec_fixture()
    change(value)
    with pytest.raises(RecoverableStateError):
        bootstrap.validate_bootstrap_record(value, **arguments)


@pytest.mark.parametrize("catalog_binding", [None, True, "f" * 63, "F" * 64])
def test_strict_v2_bootstrap_codec_rejects_invalid_catalog_binding(catalog_binding):
    value, arguments = _codec_fixture(version=2, catalog_binding=catalog_binding)
    with pytest.raises(RecoverableStateError):
        bootstrap.validate_bootstrap_record(value, **arguments)


@pytest.mark.parametrize("marker", ["absent", "pending", "ready", "output_pending"])
def test_rebind_derivation_is_pure_deterministic_and_preserves_all_transaction_evidence(marker):
    legacy, arguments = _codec_fixture(marker=marker)
    original = deepcopy(legacy)
    rebound = bootstrap.derive_rebound_bootstrap_record(legacy, new_binding="f" * 64, **arguments)
    assert legacy == original
    assert rebound == bootstrap.derive_rebound_bootstrap_record(legacy, new_binding="f" * 64, **arguments)
    assert rebound["schema_version"] == 2
    assert rebound["binding"] == "f" * 64
    assert rebound["catalog_binding"] == original["binding"]
    assert {
        key: value for key, value in rebound.items() if key not in {"schema_version", "binding", "catalog_binding"}
    } == {key: value for key, value in original.items() if key not in {"schema_version", "binding"}}
    for key in (
        "credential",
        "credential_digest",
        "attempt",
        "admitted_at_ms",
        "progress",
        "pending",
        "locks",
        "directories",
    ):
        assert rebound[key] == original[key]

    second = bootstrap.derive_rebound_bootstrap_record(
        rebound, new_binding="1" * 64, **dict(arguments, binding="f" * 64)
    )
    assert second["catalog_binding"] == original["binding"]
    assert second["binding"] == "1" * 64
    assert {
        key: value for key, value in second.items() if key not in {"schema_version", "binding", "catalog_binding"}
    } == {key: value for key, value in rebound.items() if key not in {"schema_version", "binding", "catalog_binding"}}


@pytest.mark.parametrize("new_binding", ["a" * 64, "f" * 63, "F" * 64, True])
def test_rebind_derivation_requires_a_distinct_canonical_dynamic_binding(new_binding):
    value, arguments = _codec_fixture()
    with pytest.raises(RecoverableStateError):
        bootstrap.derive_rebound_bootstrap_record(value, new_binding=new_binding, **arguments)


@pytest.fixture
def planned(tmp_path):
    config, envelope, manifests = _documents()
    bundle = tmp_path / "bundle"
    write_catalog_publication_bundle(bundle, config, envelope, manifests)
    profile = VolunteerProfile(tmp_path / "profile")
    plan = bootstrap.build_bootstrap_plan(bundle, profile, initialize=True)
    return SimpleNamespace(profile=profile, plan=plan, bundle=bundle)


def test_fixed_signed_plan_is_pure_deterministic_bounded_and_config_last(planned):
    f = planned
    assert not f.profile.root.exists()
    rebuilt = bootstrap.build_bootstrap_plan(f.bundle, f.profile)
    assert rebuilt.outputs == f.plan.outputs and rebuilt.digest == f.plan.digest
    assert f.plan.outputs[-1][0] == "node/node-config.json"
    config = json.loads(f.plan.outputs[-1][1])
    assert config["contribution_policy"]["sharing_enabled"] is False
    assert len(f.plan.outputs) == 7
    assert "rollback-state.json" in f.plan.outputs[-2][0]
    assert all(name.startswith("node/") for name, _ in f.plan.outputs)


def test_initialization_checks_current_expiry_without_creating_root(planned):
    f = planned
    with pytest.raises(ValueError, match="expired"):
        bootstrap.build_bootstrap_plan(
            f.bundle, f.profile, initialize=True, now=f.plan.envelope.signed.expires_at_ms / 1000 + 1
        )
    assert not f.profile.root.exists()


def test_plan_refuses_changed_bundle_and_runtime_rejection_before_any_output(planned, monkeypatch):
    from drift.node import loading

    f = planned

    def reject(*args):
        raise ValueError("unsupported runtime")

    monkeypatch.setattr(loading, "validate_manifest_execution", reject)
    with pytest.raises(ValueError, match="unsupported runtime"):
        bootstrap.build_bootstrap_plan(f.bundle, f.profile)
    assert not f.profile.root.exists()
    (f.bundle / "catalog.signed.json").write_text("{}")
    with pytest.raises(ValueError):
        bootstrap.build_bootstrap_plan(f.bundle, f.profile)
    assert not f.profile.root.exists()


class Store:
    """In-memory keyring double, never actual Secret Service qualification."""

    service = "org.communityai.desktop.multigpu-volunteer"
    account = "multigpu-volunteer-control-v1"

    def __init__(self):
        self.secret = None
        self.gets = 0
        self.sets = 0
        self.after_set = lambda: None

    def get(self):
        self.gets += 1
        if self.secret is None:
            raise CredentialMissingError()
        return self.secret

    def set(self, secret):
        self.sets += 1
        self.secret = secret
        self.after_set()


@pytest.fixture
def transaction(planned):
    if not sys.platform.startswith("linux"):
        pytest.skip("actual Linux private filesystem/fsync/renameat2")
    f = planned
    f.profile.root.mkdir(mode=0o700)
    (f.profile.root / "anchor").mkdir(mode=0o700)
    f.profile.data_dir.mkdir(mode=0o700)
    (f.profile.root / "anchor-state.lock").touch(mode=0o600)
    f.store = Store()
    f.state = SimpleNamespace(binding={"fixture": "same live owner"})
    f.owner = bootstrap.AnchorBootstrap(f.plan, f.profile, f.store)
    f.owner.bind(f.state, lambda: None, initialize=True)
    f.intent = dict(
        phase="starting", operation="start", request_id="a" * 32, generation=dict(id="b" * 32, cgroup=None, pid=None)
    )
    return f


def prepare(f, cancelled=lambda: False):
    f.owner.prepare(f.intent, cancelled=cancelled)


def reopen(f):
    owner = bootstrap.AnchorBootstrap(f.plan, f.profile, f.store)
    owner.bind(f.state, lambda: None)
    f.owner = owner


def test_new_enrollment_is_v2_and_legacy_v1_reopens_without_rewriting_catalog_lock(transaction):
    from drift.node import linux_anchor_entry as entry, worker_loading as private

    f = transaction
    assert f.owner.value["schema_version"] == 2
    assert f.owner.value["catalog_binding"] == f.owner.value["binding"] == f.owner.binding
    lock = f.profile.root / f.owner.lock_names[0]
    lock_identity, lock_payload = lock.stat(), lock.read_bytes()
    legacy = dict(f.owner.value)
    legacy["schema_version"] = 1
    legacy.pop("catalog_binding")
    private._replace(f.owner.path, legacy)
    reopen(f)
    prepare(f)
    assert f.owner.value["schema_version"] == 1 and "catalog_binding" not in f.owner.value
    assert os.path.samestat(lock_identity, lock.stat()) and lock.read_bytes() == lock_payload
    proof = entry._catalog_entry(f.profile.root, f.state.binding, f.intent["generation"])
    assert proof[2] == f.owner.value


def test_data_only_rebound_v2_reopens_with_dynamic_binding_and_original_catalog_lock(transaction):
    from drift.node import linux_anchor_entry as entry, worker_loading as private

    f = transaction
    prepare(f)
    old = deepcopy(f.owner.value)
    old_lock = f.profile.root / f.owner.lock_names[0]
    lock_identity, lock_payload = old_lock.stat(), old_lock.read_bytes()
    new_state_binding = {"fixture": "replacement service invocation"}
    new_binding = bootstrap._digest(bootstrap._json(new_state_binding))
    rebound = bootstrap.derive_rebound_bootstrap_record(
        old,
        plan=f.plan,
        binding=f.owner.binding,
        new_binding=new_binding,
        service=f.store.service,
        account=f.store.account,
        parents=f.owner.parents,
        lock_names=f.owner.lock_names,
    )
    private._replace(f.owner.path, rebound)
    replacement = bootstrap.AnchorBootstrap(f.plan, f.profile, f.store)
    replacement.bind(SimpleNamespace(binding=new_state_binding), lambda: None)
    before_gets, before_sets = f.store.gets, f.store.sets
    replacement.prepare(f.intent, cancelled=lambda: False)
    assert replacement.value["ready"] and f.store.gets > before_gets and f.store.sets == before_sets
    proof = entry._catalog_entry(f.profile.root, new_state_binding, f.intent["generation"])
    assert proof[2] == replacement.value == rebound
    assert rebound["catalog_binding"] == old["catalog_binding"] == f.owner.binding
    assert os.path.samestat(lock_identity, old_lock.stat()) and old_lock.read_bytes() == lock_payload
    with pytest.raises(RecoverableStateError):
        entry._catalog_entry(f.profile.root, f.state.binding, f.intent["generation"])


def test_real_private_outputs_and_ready_retry_preserve_user_settings(transaction):
    f = transaction
    prepare(f)
    assert f.owner.value["ready"] and f.store.sets == 1
    assert f.owner.value["attempt"] == dict(request_id="a" * 32, generation="b" * 32)
    assert f.store.secret not in f.owner.path.read_text()
    for name, payload in f.plan.outputs:
        assert (f.profile.root / name).read_bytes() == payload
        assert (f.profile.root / name).stat().st_mode & 0o777 == 0o600
    config = json.loads(f.profile.config_path.read_text())
    config["contribution_policy"]["max_processing_percent"] = 17
    f.profile.config_path.write_text(json.dumps(config))
    reopen(f)
    prepare(f)
    assert json.loads(f.profile.config_path.read_text())["contribution_policy"]["max_processing_percent"] == 17
    assert f.store.sets == 1


@pytest.mark.parametrize("target", ["credential", "config", "manifest", "rollback", "journal", "directory"])
def test_ready_missing_or_replaced_evidence_never_recreated(transaction, target):
    f = transaction
    prepare(f)
    if target == "credential":
        f.store.secret = None
    elif target == "directory":
        path = f.profile.data_dir / "manifests"
        path.rename(path.with_name("retained-manifests"))
        path.mkdir(mode=0o700)
    else:
        path = {
            "config": f.profile.config_path,
            "manifest": f.profile.root / f.plan.outputs[0][0],
            "rollback": f.profile.root / f.plan.outputs[-2][0],
            "journal": f.owner.path,
        }[target]
        path.rename(path.with_name(path.name + ".retained"))
    with pytest.raises(RecoverableStateError):
        prepare(f)
    assert f.owner.poisoned and f.store.sets == 1


def test_existing_key_or_legacy_file_is_not_first_use_authority(transaction):
    f = transaction
    f.store.secret = "drift_control_" + "x" * 43
    with pytest.raises(RecoverableStateError):
        prepare(f)
    assert f.store.sets == 0 and not f.profile.config_path.exists()


def test_keyring_raised_after_commit_is_reconciled_without_rotation(transaction):
    f = transaction

    def ambiguous():
        raise OSError("backend reply lost")

    f.store.after_set = ambiguous
    prepare(f)
    assert f.owner.value["ready"] and f.store.sets == 1


def test_keyring_unknown_outcome_poison_preserves_pending_and_no_config(transaction):
    f = transaction

    def lose():
        f.store.secret = None
        raise OSError("unknown backend result")

    f.store.after_set = lose
    with pytest.raises(RecoverableStateError):
        prepare(f)
    assert f.owner.poisoned and f.owner.value["credential"] == "pending"
    assert not f.profile.config_path.exists()
    reopen(f)
    with pytest.raises(RecoverableStateError):
        prepare(f)
    assert f.store.sets == 1


def test_cancel_waits_for_keyring_reconciliation_then_checked_retry(transaction):
    f = transaction
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    results = []

    def barrier():
        entered.set()
        assert release.wait(5)

    f.store.after_set = barrier

    def run():
        try:
            prepare(f, cancelled.is_set)
        except RecoverableStateError as error:
            results.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(5)
    cancelled.set()
    assert thread.is_alive() and f.owner.value["credential"] == "pending"
    release.set()
    thread.join(5)
    assert not thread.is_alive() and results
    assert not f.owner.poisoned and f.owner.value["credential"] == "ready"
    assert not f.profile.config_path.exists()
    reopen(f)
    prepare(f)
    assert f.owner.value["ready"] and f.store.sets == 1


@pytest.mark.parametrize("index", range(7))
def test_cancel_at_every_durable_output_boundary_then_resume(transaction, index):
    f = transaction
    with pytest.raises(RecoverableStateError):
        prepare(f, lambda: f.owner.value["progress"] > index)
    assert not f.owner.poisoned and f.owner.value["progress"] == index + 1
    reopen(f)
    prepare(f)
    assert f.owner.value["ready"] and f.store.sets == 1


@pytest.mark.parametrize("index", range(7))
def test_lost_progress_ack_adopts_only_exact_pending_output(transaction, monkeypatch, index):
    f = transaction
    write = f.owner._write

    def fail(**changes):
        if changes.get("progress") == index + 1:
            raise OSError("lost progress acknowledgement")
        return write(**changes)

    monkeypatch.setattr(f.owner, "_write", fail)
    with pytest.raises(RecoverableStateError):
        prepare(f)
    assert f.owner.poisoned and f.owner.value["progress"] == index and f.owner.value["pending"]
    reopen(f)
    prepare(f)
    assert f.owner.value["ready"] and f.store.sets == 1


def test_future_exact_output_without_pending_intent_is_refused(transaction):
    f = transaction
    path = f.profile.config_path
    path.write_bytes(f.plan.outputs[-1][1])
    path.chmod(0o600)
    with pytest.raises(RecoverableStateError):
        prepare(f)
    assert f.store.sets == 0 and f.owner.poisoned


@pytest.mark.parametrize("damage", ["partial", "symlink", "hardlink", "wrong-final"])
def test_interrupted_or_unsafe_output_is_retained_without_overwrite(transaction, monkeypatch, damage):
    f = transaction
    original = bootstrap._rename_new

    def fail(directory, source, destination):
        path = f.owner._temporary(0)
        if damage == "partial":
            path.write_bytes(b"incomplete")
        elif damage == "symlink":
            path.rename(path.with_name("retained-input"))
            path.symlink_to(path.with_name("retained-input"))
        elif damage == "hardlink":
            os.link(path, path.with_name("retained-link"))
        else:
            final = f.profile.root / f.plan.outputs[0][0]
            final.write_bytes(b"other owner")
            final.chmod(0o600)
        raise OSError("interrupted output")

    monkeypatch.setattr(bootstrap, "_rename_new", fail)
    with pytest.raises(RecoverableStateError):
        prepare(f)
    assert f.owner.poisoned
    monkeypatch.setattr(bootstrap, "_rename_new", original)
    reopen(f)
    with pytest.raises(RecoverableStateError):
        prepare(f)
    assert f.owner.poisoned and not f.profile.config_path.exists()


def test_rename_no_replace_does_not_overwrite_competing_final(transaction):
    f = transaction
    directory = os.open(f.profile.data_dir, os.O_RDONLY | os.O_DIRECTORY)
    source, target = f.profile.data_dir / "source", f.profile.data_dir / "target"
    source.write_text("new")
    target.write_text("retained")
    try:
        with pytest.raises(FileExistsError):
            bootstrap._rename_new(directory, source.name, target.name)
        assert source.read_text() == "new" and target.read_text() == "retained"
    finally:
        os.close(directory)


def test_expired_first_attempt_is_retryable_without_effects(transaction, monkeypatch):
    f = transaction
    monkeypatch.setattr(bootstrap.time, "time", lambda: f.plan.envelope.signed.expires_at_ms / 1000 + 1)
    for _ in range(2):
        with pytest.raises(RecoverableStateError):
            prepare(f)
        assert f.owner.retryable and not f.owner.poisoned
        assert f.owner.value["attempt"] is None and f.store.gets == f.store.sets == 0


@pytest.mark.parametrize("stop_after", [0, 1, 7])
def test_exact_admitted_transaction_resumes_after_expiry(transaction, monkeypatch, stop_after):
    f = transaction
    with pytest.raises(RecoverableStateError):
        prepare(f, lambda: f.owner.value["credential"] == "ready" and f.owner.value["progress"] >= stop_after)
    admitted = f.owner.value["admitted_at_ms"]
    assert admitted is not None and not f.owner.value["ready"]
    reopen(f)
    monkeypatch.setattr(bootstrap.time, "time", lambda: f.plan.envelope.signed.expires_at_ms / 1000 + 10)
    prepare(f)
    assert f.owner.value["ready"] and f.owner.value["admitted_at_ms"] == admitted and f.store.sets == 1


def test_keyring_unavailable_before_creation_can_unlock_and_retry(transaction, monkeypatch):
    f = transaction
    original = f.store.get

    def locked():
        raise OSError("fixture locked keyring")

    monkeypatch.setattr(f.store, "get", locked)
    with pytest.raises(RecoverableStateError):
        prepare(f)
    assert f.owner.retryable and not f.owner.poisoned and f.store.sets == 0
    monkeypatch.setattr(f.store, "get", original)
    prepare(f)
    assert f.owner.value["ready"] and f.store.sets == 1


@pytest.mark.parametrize("credential", ["pending", "ready"])
def test_locked_existing_digest_retries_same_owner_without_new_set(transaction, monkeypatch, credential):
    f = transaction
    if credential == "pending":
        # Storage-only pending digest after a successful external set, before
        # ready acknowledgement. Never simulate missing-secret recovery.
        f.store.set("drift_control_" + "P" * 43)
        f.owner._write(
            attempt=dict(request_id="a" * 32, generation="b" * 32),
            admitted_at_ms=f.plan.initialization_admitted_at_ms,
            credential="pending",
            credential_digest=bootstrap._digest(f.store.secret.encode()),
        )
    else:
        prepare(f)
    original = f.store.get

    def locked():
        raise OSError("fixture temporarily unavailable")

    monkeypatch.setattr(f.store, "get", locked)
    with pytest.raises(RecoverableStateError):
        prepare(f)
    assert f.owner.retryable and not f.owner.poisoned and f.store.sets == 1
    monkeypatch.setattr(f.store, "get", original)
    prepare(f)
    assert f.owner.value["ready"] and f.owner.value["credential"] == "ready" and f.store.sets == 1


@pytest.mark.parametrize("kind", ["catalog", "config"])
@pytest.mark.parametrize("fault", ["missing", "replaced", "open-race", "acquire-race"])
def test_bound_writer_opens_only_original_inode(transaction, monkeypatch, kind, fault):
    from drift.node import catalog_bootstrap, config_lock

    f = transaction
    path = f.profile.root / f.owner.lock_names[0 if kind == "catalog" else 1]
    identity = tuple(f.owner.value["locks"][path.relative_to(f.profile.root).as_posix()])
    held = path.with_name(path.name + ".retained")

    def replace():
        path.rename(held)
        if fault != "missing":
            path.write_bytes(b"{}")
            path.chmod(0o600)

    if fault in {"missing", "replaced"}:
        replace()
    elif fault == "open-race":
        original = os.open

        def swapped(candidate, *args, **kwargs):
            if os.fspath(candidate) == os.fspath(path):
                replace()
            return original(candidate, *args, **kwargs)

        monkeypatch.setattr(os, "open", swapped)
    else:
        module, attribute = (
            (catalog_bootstrap, "_acquire_process_lock") if kind == "catalog" else (config_lock, "_acquire")
        )
        original = getattr(module, attribute)

        def swapped(descriptor):
            original(descriptor)
            replace()

        monkeypatch.setattr(module, attribute, swapped)
    lock = (
        catalog_bootstrap._catalog_bootstrap_lock(path, expected_identity=identity)
        if kind == "catalog"
        else config_lock.persistent_sidecar_lock(f.profile.config_path, expected_identity=identity)
    )
    with pytest.raises((catalog_bootstrap.CatalogBootstrapError, config_lock.NodeConfigWriteLockError)):
        with lock:
            pytest.fail("replaced bound lock granted write authority")
    assert held.exists()
    assert path.exists() == (fault != "missing")
    if path.exists():
        assert path.read_bytes() == b"{}"


def test_ordinary_install_below_unrelated_anchor_filename_is_not_anchored(planned):
    from drift.node.catalog_bootstrap import CatalogBootstrapInstaller

    f = planned
    (f.profile.root.parent / "anchor-state.lock").touch()
    (f.profile.root.parent / "node-lifetime.lock").touch()
    installer = CatalogBootstrapInstaller(
        f.plan.bootstrap, data_dir=f.profile.data_dir, config_path=f.profile.config_path
    )
    assert installer._require_write_authority() is None


@pytest.mark.parametrize("kind", ["nested-link", "bind"])
def test_generic_writer_alias_refuses_before_any_mkdir(transaction, kind):
    import subprocess

    from drift.node.catalog_bootstrap import CatalogBootstrapError, CatalogBootstrapInstaller

    f = transaction
    alias = f.profile.root.parent / "alias"
    if kind == "bind":
        if not os.environ.get("COMMUNITYAI_TEST_CGROUP_ROOT"):
            pytest.skip("requires isolated privileged native mount fixture")
        alias.mkdir()
        subprocess.run(["mount", "--bind", str(f.profile.data_dir), str(alias)], check=True)
        data = alias
    else:
        alias.symlink_to(f.profile.data_dir, target_is_directory=True)
        data = alias / "not-created"
    try:
        installer = CatalogBootstrapInstaller(f.plan.bootstrap, data_dir=data, config_path=data / "node-config.json")
        with pytest.raises(CatalogBootstrapError, match="exact admitted node"):
            installer.install()
        assert not (f.profile.data_dir / "not-created").exists()
        assert not f.profile.config_path.exists() and f.store.sets == 0
    finally:
        if kind == "bind":
            subprocess.run(["umount", str(alias)], check=True)


@pytest.mark.parametrize("point", ["inventory", "attempt", "credential-read"])
def test_cancel_before_creation_never_calls_set_or_publishes_output(transaction, monkeypatch, point):
    f = transaction
    cancelled = threading.Event()
    if point == "inventory":
        original = f.owner._inventory

        def inventory():
            original()
            cancelled.set()

        monkeypatch.setattr(f.owner, "_inventory", inventory)
    elif point == "attempt":
        original = f.owner._write

        def write(**changes):
            original(**changes)
            cancelled.set()

        monkeypatch.setattr(f.owner, "_write", write)
    else:
        original = f.store.get

        def get():
            cancelled.set()
            return original()

        monkeypatch.setattr(f.store, "get", get)
    with pytest.raises(RecoverableStateError):
        prepare(f, cancelled.is_set)
    assert not f.owner.poisoned and f.store.sets == 0
    assert f.store.gets == (1 if point == "credential-read" else 0)
    assert not f.profile.config_path.exists()


def test_owner_death_before_keyring_set_retains_pending_without_regeneration(transaction):
    f = transaction
    f.owner._write(
        attempt=dict(request_id="a" * 32, generation="b" * 32), admitted_at_ms=f.plan.initialization_admitted_at_ms
    )
    f.owner._write(credential="pending", credential_digest="0" * 64)
    reopen(f)  # Storage-only rebind, NOT automatic production owner recovery.
    with pytest.raises(RecoverableStateError):
        prepare(f)
    assert f.owner.poisoned and f.store.sets == 0 and not f.profile.config_path.exists()


@pytest.mark.parametrize("lock", ["catalog", "config"])
def test_shared_writer_lock_blocks_bootstrap_before_keyring_and_allows_retry(transaction, lock):
    from drift.node.catalog_bootstrap import _catalog_bootstrap_lock
    from drift.node.config_lock import persistent_sidecar_lock

    f = transaction
    with (
        _catalog_bootstrap_lock(f.profile.data_dir / ".catalog-bootstrap.lock")
        if lock == "catalog"
        else persistent_sidecar_lock(f.profile.config_path)
    ):
        with pytest.raises(RecoverableStateError):
            prepare(f)
        assert f.owner.retryable and not f.owner.poisoned and f.store.gets == 0
    prepare(f)
    assert f.owner.value["ready"]


@pytest.mark.parametrize("method", ["install", "refresh", "repair_existing_config"])
def test_generic_catalog_writer_cannot_mutate_anchor_profile(transaction, method):
    from drift.node.catalog_bootstrap import CatalogBootstrapError, CatalogBootstrapInstaller

    f = transaction
    installer = CatalogBootstrapInstaller(
        f.plan.bootstrap, data_dir=f.profile.data_dir, config_path=f.profile.config_path
    )
    before = {
        str(path.relative_to(f.profile.root)): path.read_bytes() for path in f.profile.root.rglob("*") if path.is_file()
    }
    with pytest.raises(CatalogBootstrapError, match="exact admitted node"):
        getattr(installer, method)()
    assert {
        str(path.relative_to(f.profile.root)): path.read_bytes() for path in f.profile.root.rglob("*") if path.is_file()
    } == before


def test_generic_config_writer_cannot_create_or_adopt_anchor_lock(transaction):
    from drift.node.config_lock import NodeConfigWriteLockError, node_config_write_lock

    f = transaction
    original = (f.profile.root / f.owner.lock_names[1]).stat()
    with pytest.raises(NodeConfigWriteLockError, match="intact admitted authority"):
        with node_config_write_lock(f.profile.config_path):
            pytest.fail("standalone config writer borrowed anchor authority")
    assert os.path.samestat(original, (f.profile.root / f.owner.lock_names[1]).stat())
    assert not f.profile.config_path.exists()


def test_separate_compute_sidecar_does_not_require_config_writer_authority(transaction):
    from drift.server.processing_budget import ProcessingBudget

    f = transaction
    budget = f.profile.data_dir / ".fixture.processing-budget"
    assert ProcessingBudget(50, path=budget).run(lambda: 42) == 42
    assert not f.profile.config_path.exists()


def test_ready_rejects_mutable_catalog_cache_without_rewriting_config(transaction):
    f = transaction
    prepare(f)
    config = json.loads(f.profile.config_path.read_text())
    config["catalog_path"] = str((f.profile.root / f.plan.outputs[-2][0]).parent / "catalog.signed.json")
    f.profile.config_path.write_text(json.dumps(config))
    changed = f.profile.config_path.read_bytes()
    with pytest.raises(RecoverableStateError):
        prepare(f)
    assert f.owner.poisoned and f.profile.config_path.read_bytes() == changed


def test_ready_rechecks_actual_runtime_validator(transaction, monkeypatch):
    from drift.node import loading

    f = transaction
    prepare(f)

    def reject(*args):
        raise ValueError("fixture changed unsupported runtime")

    monkeypatch.setattr(loading, "validate_manifest_execution", reject)
    with pytest.raises(RecoverableStateError):
        prepare(f)
    assert f.owner.poisoned


@pytest.mark.parametrize(
    "changes",
    [
        dict(progress=True),
        dict(progress=1),
        dict(pending=True),
        dict(ready=True),
        dict(credential="ready"),
        dict(admitted_at_ms=1),
    ],
)
def test_invalid_local_marker_transition_is_not_written(transaction, changes):
    f = transaction
    before = f.owner.path.read_bytes()
    with pytest.raises(RecoverableStateError):
        f.owner._write(**changes)
    assert f.owner.path.read_bytes() == before and not f.owner.poisoned


@pytest.mark.parametrize("phase", ["temporary", "published", "journal-directory"])
def test_fsync_uncertainty_retains_evidence_for_storage_reconciliation(transaction, monkeypatch, phase):
    f = transaction
    original = bootstrap.os.fsync
    failed = []

    def fsync(descriptor):
        path = os.readlink(f"/proc/self/fd/{descriptor}")
        match = (
            phase == "temporary"
            and "/.anchor-bootstrap-" in path
            or phase == "published"
            and path.endswith(f.plan.outputs[0][0])
            or phase == "journal-directory"
            and path == str(f.owner.path.parent)
        )
        if match and not failed:
            failed.append(path)
            raise OSError("fixture uncertain fsync")
        return original(descriptor)

    monkeypatch.setattr(bootstrap.os, "fsync", fsync)
    with pytest.raises(RecoverableStateError):
        prepare(f)
    assert failed and f.owner.poisoned
    monkeypatch.setattr(bootstrap.os, "fsync", original)
    reopen(f)
    prepare(f)
    assert f.owner.value["ready"] and f.store.sets == 1


def test_rename_capability_failure_precedes_keyring_and_product_outputs(planned, monkeypatch):
    if not sys.platform.startswith("linux"):
        pytest.skip("actual Linux enrollment preflight")
    f = planned
    f.profile.root.mkdir(mode=0o700)
    (f.profile.root / "anchor").mkdir(mode=0o700)
    f.profile.data_dir.mkdir(mode=0o700)
    store = Store()
    owner = bootstrap.AnchorBootstrap(f.plan, f.profile, store)

    def unsupported(*args):
        raise OSError("fixture unsupported renameat2")

    monkeypatch.setattr(bootstrap, "_rename_new", unsupported)
    with pytest.raises(OSError, match="unsupported renameat2"):
        owner.bind(SimpleNamespace(binding={}), lambda: None, initialize=True)
    assert store.gets == store.sets == 0
    assert list(f.profile.data_dir.iterdir()) == [] and not owner.path.exists()
    assert (owner.path.parent / ".bootstrap-rename-probe").is_file()


def test_initializer_admission_does_not_recheck_wall_time_after_creating_profile(planned, monkeypatch):
    if not sys.platform.startswith("linux"):
        pytest.skip("actual Linux enrollment preflight")
    f = planned
    f.profile.root.mkdir(mode=0o700)
    (f.profile.root / "anchor").mkdir(mode=0o700)
    f.profile.data_dir.mkdir(mode=0o700)
    monkeypatch.setattr(bootstrap.time, "time", lambda: f.plan.envelope.signed.expires_at_ms / 1000 + 1)
    owner = bootstrap.AnchorBootstrap(f.plan, f.profile, Store())
    owner.bind(SimpleNamespace(binding={}), lambda: None, initialize=True)
    assert owner.value["attempt"] is None


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_ready_keeps_profile_unchanged_when_required_packaged_bundle_is_lost(transaction, damage):
    from drift.node.catalog_bootstrap import CatalogBootstrapError

    f = transaction
    prepare(f)
    saved = f.owner.path.read_bytes(), f.profile.config_path.read_bytes()
    if damage == "missing":
        f.bundle.rename(f.bundle.with_name("retained-bundle"))
    else:
        (f.bundle / "publication-preflight.json").write_text("{}")
    with pytest.raises(CatalogBootstrapError):
        bootstrap.build_bootstrap_plan(f.bundle, f.profile)
    assert (f.owner.path.read_bytes(), f.profile.config_path.read_bytes()) == saved
