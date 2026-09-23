import copy
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from communityai_anchor import linux_anchor_transaction as transaction


def _payload(label, size=None):
    if size is None:
        return json.dumps({"value": label}, sort_keys=True, separators=(",", ":")).encode()
    prefix = json.dumps({"value": ""}, sort_keys=True, separators=(",", ":")).encode()
    raw = json.dumps({"value": label * (size - len(prefix))}, sort_keys=True, separators=(",", ":")).encode()
    assert len(raw) == size
    return raw


def _info(*, inode, size, ctime=6):
    return SimpleNamespace(
        st_dev=1,
        st_ino=inode,
        st_mode=stat.S_IFREG | 0o600,
        st_size=size,
        st_mtime_ns=5,
        st_ctime_ns=ctime,
        st_nlink=1,
        st_uid=1000,
    )


def _ledger(*, prepared=False, published=False, context=None, size=32):
    transaction_id = "1" * 32
    files = {}
    for index, name in enumerate(transaction.TARGETS):
        source_raw = _payload(chr(ord("a") + index), size)
        target_raw = _payload(chr(ord("d") + index), size)
        source = transaction._evidence(source_raw, _info(inode=10 + index, size=len(source_raw)))
        target = transaction._prepared(target_raw) if prepared else None
        post = None
        if target is not None and published:
            target["fingerprint"] = [1, 20 + index, stat.S_IFREG | 0o600, len(target_raw), 5, 6, 1]
            target["uid"] = 1000
            post = list(target["fingerprint"])
            post[5] += 1
        files[name] = {
            "target": name,
            "temporary": transaction._temporary(transaction_id, name),
            "source": source,
            "prepared": target,
            "published_fingerprint": post,
        }
    return {
        "schema_version": 1,
        "transaction": transaction_id,
        "revision": 0,
        "previous_digest": None,
        "phase": "activated" if published else "intent",
        "context": {} if context is None else context,
        "files": files,
        "retired": [],
    }


def test_schema_accepts_only_ctime_change_after_recorded_inode():
    value = _ledger(prepared=True, published=True)
    transaction._validate_ledger(value)

    changed_inode = copy.deepcopy(value)
    changed_inode["files"]["state.json"]["published_fingerprint"][1] += 1
    with pytest.raises(transaction.RecoveryTransactionError):
        transaction._validate_ledger(changed_inode)

    changed_mtime = copy.deepcopy(value)
    changed_mtime["files"]["state.json"]["published_fingerprint"][4] += 1
    with pytest.raises(transaction.RecoveryTransactionError):
        transaction._validate_ledger(changed_mtime)


def test_strict_schema_accepts_only_the_fixed_staged_prefix_and_contiguous_publication():
    prefix = _ledger(prepared=True)
    prefix["files"]["state.json"]["prepared"] = None
    transaction._validate_ledger(prefix)

    wrong_partial = copy.deepcopy(prefix)
    wrong_partial["files"]["resources.json"]["prepared"] = None
    with pytest.raises(transaction.RecoveryTransactionError):
        transaction._validate_ledger(wrong_partial)

    published_prefix = _ledger(prepared=True, published=True)
    published_prefix["files"]["state.json"]["prepared"] = None
    published_prefix["files"]["state.json"]["published_fingerprint"] = None
    published_prefix["phase"] = "publishing"
    transaction._validate_ledger(published_prefix)

    noncontiguous = _ledger(prepared=True, published=True)
    noncontiguous["files"]["bootstrap.json"]["published_fingerprint"] = None
    noncontiguous["phase"] = "publishing"
    with pytest.raises(transaction.RecoveryTransactionError):
        transaction._validate_ledger(noncontiguous)

    invalid_retired = _ledger(prepared=True, published=True)
    raw = _payload("retired")
    invalid_retired["retired"] = [
        {
            "temporary": ".recovery-not-a-transaction-bootstrap.json.tmp",
            **transaction._evidence(raw, _info(inode=80, size=len(raw))),
        }
    ]
    with pytest.raises(transaction.RecoveryTransactionError):
        transaction._validate_ledger(invalid_retired)


def test_context_and_complete_ledger_have_independent_hard_bounds():
    transaction._validate_context({"padding": "x" * 8000})
    transaction._validate_context({"caller_validated_native_identity": 2**127})
    with pytest.raises(transaction.RecoveryTransactionError):
        transaction._validate_context({"caller_validated_native_identity": 2**128})
    with pytest.raises(transaction.RecoveryTransactionError):
        transaction._validate_context({"padding": "x" * transaction.CONTEXT_LIMIT})

    empty_context = json.dumps({"padding": ""}, sort_keys=True, separators=(",", ":")).encode()
    context = {"padding": "x" * (transaction.CONTEXT_LIMIT - len(empty_context))}
    assert len(transaction._encode_json(context, transaction.CONTEXT_LIMIT)) == transaction.CONTEXT_LIMIT
    value = _ledger(prepared=True, published=True, context=context, size=8192)
    for index, name in enumerate(transaction.TARGETS):
        raw = _payload(chr(ord("g") + index), 8192)
        value["retired"].append(
            {
                "temporary": f".recovery-{'2' * 32}-{name}.tmp",
                **transaction._evidence(raw, _info(inode=30 + index, size=len(raw))),
            }
        )
    raw = transaction._ledger_bytes(value)
    assert len(raw) <= transaction.LEDGER_LIMIT
    assert transaction._validate_ledger(transaction._decode_json(raw, transaction.LEDGER_LIMIT)) == value

    staged = _ledger(prepared=True, published=True, context=context, size=8192)
    staged["files"]["state.json"]["prepared"] = None
    staged["files"]["state.json"]["published_fingerprint"] = None
    staged["phase"] = "publishing"
    staged_raw = transaction._ledger_bytes(staged)
    assert len(staged_raw) <= transaction.LEDGER_LIMIT
    assert transaction._validate_ledger(transaction._decode_json(staged_raw, transaction.LEDGER_LIMIT)) == staged


def test_fixed_publication_order_has_state_last():
    assert transaction.TARGETS == ("bootstrap.json", "resources.json", "state.json")


@pytest.fixture
def profile(tmp_path):
    if not sys.platform.startswith("linux"):
        pytest.skip("requires real Linux no-follow descriptors, renameat2, flock authority and fsync")
    root = tmp_path / "profile"
    anchor = root / "anchor"
    root.mkdir(mode=0o700)
    anchor.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    os.chmod(anchor, 0o700)
    source = {}
    for index, name in enumerate(transaction.TARGETS):
        raw = _payload("source-" + str(index))
        path = anchor / name
        path.write_bytes(raw)
        path.chmod(0o600)
        source[name] = raw
    return SimpleNamespace(root=root, anchor=anchor, source=source, calls=[])


def _guard(profile):
    def held():
        profile.calls.append("guard")

    return held


def _prepared_values(prefix="target"):
    return {name: _payload(prefix + "-" + str(index)) for index, name in enumerate(transaction.TARGETS)}


def _prefix_values(prefix="target"):
    values = _prepared_values(prefix)
    return {name: values[name] for name in transaction.PREFIX_TARGETS}


def test_begin_records_source_before_prepared_effects_and_context_can_advance(profile):
    owner = transaction.RecoveryTransaction.begin(profile.root, context={"step": "entry"}, guard=_guard(profile))
    try:
        value = owner.snapshot()
        assert value["phase"] == "intent"
        assert all(value["files"][name]["prepared"] is None for name in transaction.TARGETS)
        assert all(
            transaction._field_raw(value["files"][name]["source"]["raw_b64"]) == profile.source[name]
            for name in transaction.TARGETS
        )
        assert not any(profile.anchor.glob(".recovery-*-*.tmp"))

        owner.update_context({"step": "layout", "root_identity": [1, 2]})
        owner.set_prepared(_prepared_values())
        assert owner.snapshot()["context"]["step"] == "layout"
        assert all(owner.snapshot()["files"][name]["prepared"] is not None for name in transaction.TARGETS)
    finally:
        owner.close()


def test_prepare_persists_all_identities_before_state_last_publication(profile, monkeypatch):
    owner = transaction.RecoveryTransaction.begin(
        profile.root, context={}, prepared=_prepared_values(), guard=_guard(profile)
    )
    destinations = []
    original = transaction.os.replace

    def observed(source, destination, *args, **kwargs):
        if destination in transaction.TARGETS:
            destinations.append(destination)
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(transaction.os, "replace", observed)
    try:
        owner.prepare()
        before = owner.snapshot()
        assert before["phase"] == "prepared"
        assert all(before["files"][name]["prepared"]["fingerprint"] is not None for name in transaction.TARGETS)
        owner.publish()
        assert destinations == list(transaction.TARGETS)
        assert owner.snapshot()["phase"] == "activated"
        assert all((profile.anchor / name).read_bytes() == _prepared_values()[name] for name in transaction.TARGETS)
    finally:
        owner.close()


def test_staged_prefix_captures_exact_post_fingerprints_before_final_state(profile):
    prefix = _prefix_values()
    final_state = _payload("sealed-state")
    owner = transaction.RecoveryTransaction.begin(profile.root, context={"no_spawn": True}, guard=_guard(profile))
    try:
        owner.set_prefix(prefix)
        published = owner.publish_prefix()
        assert published["phase"] == "publishing"
        assert published["files"]["state.json"]["prepared"] is None
        assert (profile.anchor / "state.json").read_bytes() == profile.source["state.json"]
        for name in transaction.PREFIX_TARGETS:
            record = published["files"][name]
            assert (profile.anchor / name).read_bytes() == prefix[name]
            assert record["published_fingerprint"] == list(
                transaction.private._fingerprint(os.lstat(profile.anchor / name))
            )

        owner.update_context({"no_spawn": True, "endpoint": "bound"})
        owner.set_final_state(final_state)
        assert owner.publish()["phase"] == "activated"
        assert (profile.anchor / "state.json").read_bytes() == final_state
    finally:
        owner.close()


def test_staged_calls_are_idempotent_but_never_retarget_bytes_or_publish_state_early(profile):
    prefix = _prefix_values()
    final_state = _payload("sealed-state")
    owner = transaction.RecoveryTransaction.begin(profile.root, context={}, guard=_guard(profile))
    try:
        owner.set_prefix(prefix)
        assert owner.set_prefix(prefix)["phase"] == "intent"
        with pytest.raises(transaction.RecoveryTransactionError):
            owner.set_prefix(_prefix_values("changed"))
        with pytest.raises(transaction.RecoveryTransactionError):
            owner.set_final_state(final_state)
        with pytest.raises(transaction.RecoveryTransactionError):
            owner.publish()
        assert (profile.anchor / "state.json").read_bytes() == profile.source["state.json"]
        assert not list(profile.anchor.glob(".recovery-*-state.json.tmp"))

        first = owner.publish_prefix()
        second = owner.publish_prefix()
        assert second == first
        owner.set_final_state(final_state)
        assert transaction._field_raw(owner.snapshot()["files"]["state.json"]["prepared"]["raw_b64"]) == final_state
        assert owner.set_final_state(final_state)["phase"] == "publishing"
        with pytest.raises(transaction.RecoveryTransactionError):
            owner.set_final_state(_payload("different-state"))
        assert (profile.anchor / "state.json").read_bytes() == profile.source["state.json"]
    finally:
        owner.close()


def test_staged_prefix_and_final_state_replay_after_reopen(profile):
    prefix = _prefix_values()
    final_state = _payload("sealed-state")
    owner = transaction.RecoveryTransaction.begin(profile.root, context={}, guard=_guard(profile))
    owner.set_prefix(prefix)
    owner._prepare_names(transaction.PREFIX_TARGETS)
    first = owner.snapshot()["files"]["bootstrap.json"]
    os.replace(profile.anchor / first["temporary"], profile.anchor / "bootstrap.json")
    owner.close()

    reopened = transaction.RecoveryTransaction.open(profile.root, guard=_guard(profile))
    try:
        assert reopened.publish_prefix()["phase"] == "publishing"
        reopened.set_final_state(final_state)
        reopened._prepare_names(("state.json",))
        with pytest.raises(transaction.RecoveryTransactionError):
            reopened.set_final_state(_payload("changed-after-state-prepare"))
        state_record = reopened.snapshot()["files"]["state.json"]
        os.replace(profile.anchor / state_record["temporary"], profile.anchor / "state.json")
    finally:
        reopened.close()

    final = transaction.RecoveryTransaction.open(profile.root, guard=_guard(profile))
    try:
        value = final.publish()
        assert value["phase"] == "activated"
        assert (profile.anchor / "state.json").read_bytes() == final_state
    finally:
        final.close()


def test_lost_rename_ack_uses_recorded_inode_and_captures_post_fingerprint(profile):
    prepared = _prepared_values()
    owner = transaction.RecoveryTransaction.begin(profile.root, context={}, prepared=prepared, guard=_guard(profile))
    owner.prepare()
    before = owner.snapshot()["files"]["bootstrap.json"]["prepared"]["fingerprint"]
    os.replace(
        profile.anchor / owner.snapshot()["files"]["bootstrap.json"]["temporary"],
        profile.anchor / "bootstrap.json",
    )
    owner.close()

    reopened = transaction.RecoveryTransaction.open(profile.root, guard=_guard(profile))
    try:
        value = reopened.reconcile()
        published = value["files"]["bootstrap.json"]["published_fingerprint"]
        assert published is not None
        assert published[:5] + published[6:] == before[:5] + before[6:]
        assert value["phase"] == "publishing"
    finally:
        reopened.close()


def test_unrecorded_prepared_target_is_ambiguous_and_preserved(profile):
    prepared = _prepared_values()
    owner = transaction.RecoveryTransaction.begin(profile.root, context={}, prepared=prepared, guard=_guard(profile))
    replacement = profile.anchor / ".outside.tmp"
    replacement.write_bytes(prepared["bootstrap.json"])
    replacement.chmod(0o600)
    os.replace(replacement, profile.anchor / "bootstrap.json")
    with pytest.raises(transaction.RecoveryTransactionError):
        owner.reconcile()
    assert (profile.anchor / "bootstrap.json").read_bytes() == prepared["bootstrap.json"]
    assert (profile.anchor / transaction.LEDGER_NAME).exists()
    owner.close()


def test_exact_precommitted_temp_can_complete_identity_registration(profile):
    prepared = _prepared_values()
    owner = transaction.RecoveryTransaction.begin(profile.root, context={}, prepared=prepared, guard=_guard(profile))
    record = owner.snapshot()["files"]["bootstrap.json"]
    path = profile.anchor / record["temporary"]
    path.write_bytes(prepared["bootstrap.json"])
    path.chmod(0o600)
    value = owner.reconcile()
    assert value["files"]["bootstrap.json"]["prepared"]["fingerprint"] is not None
    assert value["files"]["resources.json"]["prepared"]["fingerprint"] is None
    owner.close()


def test_retarget_snapshots_mixed_exact_sources_and_cleans_only_recorded_temps(profile):
    first = _prepared_values("first")
    owner = transaction.RecoveryTransaction.begin(
        profile.root, context={"service": 1}, prepared=first, guard=_guard(profile)
    )
    owner.prepare()
    first_value = owner.snapshot()
    bootstrap = first_value["files"]["bootstrap.json"]
    os.replace(profile.anchor / bootstrap["temporary"], profile.anchor / "bootstrap.json")
    owner.reconcile()

    owner.retarget(context={"service": 2}, prepared=None, authorize=lambda: True)
    value = owner.snapshot()
    assert value["phase"] == "intent"
    assert value["context"] == {"service": 2}
    assert transaction._field_raw(value["files"]["bootstrap.json"]["source"]["raw_b64"]) == first["bootstrap.json"]
    assert (
        transaction._field_raw(value["files"]["resources.json"]["source"]["raw_b64"])
        == profile.source["resources.json"]
    )
    assert value["retired"] == []
    assert not list(profile.anchor.glob(".recovery-*resources.json.tmp"))
    assert not list(profile.anchor.glob(".recovery-*state.json.tmp"))
    owner.close()


@pytest.mark.parametrize("boundary", ["before_unlink", "after_unlink"])
def test_reconcile_replays_interrupted_exact_retired_cleanup(profile, monkeypatch, boundary):
    first = _prepared_values("first")
    owner = transaction.RecoveryTransaction.begin(
        profile.root, context={"service": 1}, prepared=first, guard=_guard(profile)
    )
    owner.prepare()
    original_cleanup = owner._cleanup_retired
    original_write = owner._write_ledger

    if boundary == "before_unlink":
        cleanups = 0

        def stop_after_successor_write():
            nonlocal cleanups
            cleanups += 1
            if cleanups == 2:
                raise OSError("stopped")
            return original_cleanup()

        monkeypatch.setattr(owner, "_cleanup_retired", stop_after_successor_write)
    else:
        writes = 0

        def fail_after_unlink(value):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("lost unlink acknowledgement")
            return original_write(value)

        monkeypatch.setattr(owner, "_write_ledger", fail_after_unlink)

    with pytest.raises(OSError):
        owner.retarget(context={"service": 2}, authorize=lambda: True)
    monkeypatch.setattr(owner, "_cleanup_retired", original_cleanup)
    monkeypatch.setattr(owner, "_write_ledger", original_write)
    owner.close()

    reopened = transaction.RecoveryTransaction.open(profile.root, guard=_guard(profile))
    try:
        before = reopened.snapshot()
        assert before["retired"]
        reopened.reconcile()
        assert reopened.snapshot()["retired"] == []
        assert not list(profile.anchor.glob(".recovery-*bootstrap.json.tmp"))
        assert not list(profile.anchor.glob(".recovery-*resources.json.tmp"))
        assert not list(profile.anchor.glob(".recovery-*state.json.tmp"))
    finally:
        reopened.close()


def test_reconcile_preserves_changed_retired_temp(profile, monkeypatch):
    owner = transaction.RecoveryTransaction.begin(
        profile.root, context={"service": 1}, prepared=_prepared_values("first"), guard=_guard(profile)
    )
    owner.prepare()
    original_cleanup = owner._cleanup_retired
    cleanups = 0

    def stop_after_successor_write():
        nonlocal cleanups
        cleanups += 1
        if cleanups == 2:
            raise OSError("stopped")
        return original_cleanup()

    monkeypatch.setattr(owner, "_cleanup_retired", stop_after_successor_write)
    with pytest.raises(OSError):
        owner.retarget(context={"service": 2}, authorize=lambda: True)
    monkeypatch.setattr(owner, "_cleanup_retired", original_cleanup)
    retired = owner.snapshot()["retired"][0]
    path = profile.anchor / retired["temporary"]
    path.write_bytes(_payload("changed"))
    path.chmod(0o600)
    owner.close()

    reopened = transaction.RecoveryTransaction.open(profile.root, guard=_guard(profile))
    try:
        with pytest.raises(transaction.RecoveryTransactionError):
            reopened.reconcile()
        assert path.read_bytes() == _payload("changed")
        assert reopened.snapshot()["retired"]
    finally:
        reopened.close()


def test_publish_replays_from_publishing_and_is_idempotent_after_activation(profile):
    prepared = _prepared_values()
    owner = transaction.RecoveryTransaction.begin(profile.root, context={}, prepared=prepared, guard=_guard(profile))
    owner.prepare()
    first = owner.snapshot()["files"]["bootstrap.json"]
    os.replace(profile.anchor / first["temporary"], profile.anchor / "bootstrap.json")
    assert owner.reconcile()["phase"] == "publishing"
    assert owner.publish()["phase"] == "activated"
    before = (profile.anchor / transaction.LEDGER_NAME).read_bytes()
    assert owner.publish()["phase"] == "activated"
    assert (profile.anchor / transaction.LEDGER_NAME).read_bytes() == before
    owner.close()


def test_failed_retarget_authorization_and_endpoint_fence_have_no_effect(profile):
    owner = transaction.RecoveryTransaction.begin(
        profile.root, context={}, prepared=_prepared_values(), guard=_guard(profile)
    )
    before = (profile.anchor / transaction.LEDGER_NAME).read_bytes()
    with pytest.raises(transaction.RecoveryTransactionError):
        owner.retarget(context={"service": 2}, authorize=lambda: False)
    assert (profile.anchor / transaction.LEDGER_NAME).read_bytes() == before

    owner.publish()
    before = (profile.anchor / transaction.LEDGER_NAME).read_bytes()
    with pytest.raises(transaction.RecoveryTransactionError):
        owner.consume(endpoint_fence=lambda: False)
    assert (profile.anchor / transaction.LEDGER_NAME).read_bytes() == before
    owner.consume(endpoint_fence=lambda: True)
    assert not (profile.anchor / transaction.LEDGER_NAME).exists()
    owner.close()


def test_replaced_ledger_fails_cas_without_touching_product_files(profile):
    owner = transaction.RecoveryTransaction.begin(profile.root, context={}, guard=_guard(profile))
    path = profile.anchor / transaction.LEDGER_NAME
    replacement = profile.anchor / ".replacement"
    replacement.write_bytes(path.read_bytes())
    replacement.chmod(0o600)
    os.replace(replacement, path)
    with pytest.raises(transaction.RecoveryTransactionError):
        owner.update_context({"changed": True})
    assert all((profile.anchor / name).read_bytes() == profile.source[name] for name in transaction.TARGETS)
    owner.close()


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="non-Linux refusal only")
def test_non_linux_filesystem_api_is_explicitly_unsupported(tmp_path):
    with pytest.raises(transaction.RecoveryTransactionError) as caught:
        transaction.RecoveryTransaction.begin(tmp_path, context={}, guard=lambda: None)
    assert caught.value.reason == "unsupported_platform"
