"""Whole managed-card candidate contract; no model, GPU, enrollment or runtime claim."""

import copy
import json

import pytest

from drift.node import gpu_worker_selection as selection, worker_selection as identity_module
from drift.node.config import NodeConfig, NodeConfigError

REVISION = "sha256:" + "a" * 64
PHYSICAL = {f"cuda:{index}": f"GPU-00000000-0000-0000-0000-{index + 1:012x}" for index in range(16)}


def row(index=0, *, selected=True, **changes):
    result = {"device": f"cuda:{index}", "selected": selected, "max_vram": "60%", "max_processing_percent": 75}
    if selected:
        result["selection_token"] = "sha256:" + f"{index + 1:064x}"
    result.update(changes)
    return result


def request(rows=..., **changes):
    result = {"schema_version": 1, "expected_config_revision": REVISION, "rows": [row()] if rows is ... else rows}
    result.update(changes)
    return result


def document(workers=None, *, scope="per_device", sharing=False):
    return {
        "schema_version": 1,
        "models": [{"manifest": "manifest.json", "initial_peers": ["/ip4/127.0.0.1/tcp/31337/p2p/example"]}],
        "contribution_policy": {
            "sharing_enabled": sharing,
            "processing_scope": scope,
            "max_processing_percent": 23,
            "max_disk_space": "20GiB",
        },
        "workers": [] if workers is None else workers,
    }


def worker(index=0, *, managed=True, **changes):
    result = {
        "id": f"saved-{index}",
        "identity_path": f"private-{index}.key",
        "model": "auto" if managed else "model",
        "num_blocks": 2,
        "device": f"cuda:{index}",
        "enabled": False,
        "max_vram": "60%",
        "max_processing_percent": 75,
    }
    if managed:
        result["managed_by"] = "desktop_gpu"
    result.update(changes)
    return result


def build(tmp_path, source=None, draft=None, **kwargs):
    return selection.candidate_gpu_selection(
        document() if source is None else source,
        request() if draft is None else draft,
        base_dir=tmp_path,
        physical_devices=kwargs.pop("physical_devices", PHYSICAL),
        **kwargs,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"schema_version": 1.0},
        {"schema_version": 2},
        {"expected_config_revision": "sha256:" + "A" * 64},
        {"expected_config_revision": None},
        {"rows": {}},
        {"rows": None},
        {"rows": [row()] * 17},
        {"rows": [row(), row()]},
        {"model": "client-model"},
        {"operation": "replace"},
        {"identity_path": "client.key"},
    ],
)
def test_top_level_request_contract_is_strict(change):
    with pytest.raises(NodeConfigError):
        selection.parse_gpu_selection_request(json.dumps(request(**change)).encode())


@pytest.mark.parametrize(
    "change",
    [
        {"device": "cuda"},
        {"device": "cuda:01"},
        {"device": "cuda:16"},
        {"device": "cpu"},
        {"device": "xpu:0"},
        {"device": PHYSICAL["cuda:0"]},
        {"device": None},
        {"selected": 1},
        {"selected": None},
        {"max_vram": None},
        {"max_vram": "0%"},
        {"max_vram": "101%"},
        {"max_vram": "nan%"},
        {"max_vram": "-1GiB"},
        {"max_vram": "1" * 65},
        {"max_vram": 123},
        {"max_processing_percent": 0},
        {"max_processing_percent": 101},
        {"max_processing_percent": True},
        {"max_processing_percent": float("nan")},
        {"max_processing_percent": float("inf")},
        {"max_processing_percent": "50"},
        {"selection_token": "sha256:" + "A" * 64},
        {"selection_token": None},
        {"worker_id": "attacker"},
        {"model": "attacker"},
        {"block_indices": "0:10"},
        {"identity_path": "elsewhere.key"},
        {"environment": {}},
        {"enabled": True},
    ],
)
def test_selected_row_rejects_untrusted_or_invalid_fields(change):
    with pytest.raises(NodeConfigError):
        selection.parse_gpu_selection_request(json.dumps(request([row(**change)])).encode())


@pytest.mark.parametrize("field", ["device", "selected", "max_vram", "max_processing_percent", "selection_token"])
def test_selected_row_requires_every_field(field):
    selected = row()
    selected.pop(field)
    with pytest.raises(NodeConfigError):
        selection.validate_gpu_selection_request(request([selected]))


def test_unchecked_rows_require_explicit_limits_but_cannot_carry_tokens():
    unchecked = row(selected=False)
    assert selection.validate_gpu_selection_request(request([unchecked]))["rows"] == [unchecked]
    with pytest.raises(NodeConfigError):
        selection.validate_gpu_selection_request(request([row(selected=False, selection_token=REVISION)]))


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[]",
        b"null",
        b"\xff",
        b"x" * (selection.MAX_GPU_SELECTION_REQUEST_BYTES + 1),
        b"[" * 1500,
        b'{"rows":NaN}',
        b'{"rows":Infinity}',
        b'{"rows":-Infinity}',
        b'{"schema_version":1,"schema_version":1}',
    ],
)
def test_bad_json_and_size_are_rejected(payload):
    with pytest.raises(NodeConfigError):
        selection.parse_gpu_selection_request(payload)


def test_nested_duplicate_fields_reject_and_byte_boundary_is_inclusive():
    encoded = json.dumps(request()).encode()
    duplicate = encoded.replace(b'"selected": true', b'"selected": true, "selected": true')
    with pytest.raises(NodeConfigError):
        selection.parse_gpu_selection_request(duplicate)
    padded = encoded + b" " * (selection.MAX_GPU_SELECTION_REQUEST_BYTES - len(encoded))
    assert selection.parse_gpu_selection_request(padded) == request()
    with pytest.raises(NodeConfigError):
        selection.parse_gpu_selection_request(padded + b" ")


@pytest.mark.parametrize("raw", [".5%", "+.5%", "1e1%", "40GiB", "4096 MB", "  20 %"])
def test_valid_saved_memory_spellings_and_fractional_compute_are_preserved(raw):
    draft = request([row(max_vram=raw, max_processing_percent=12.5)])
    parsed = selection.parse_gpu_selection_request(json.dumps(draft).encode())
    assert parsed == draft
    parsed["rows"][0]["max_vram"] = "changed"
    assert draft["rows"][0]["max_vram"] == raw


def test_eight_card_candidate_is_paused_server_owned_and_has_no_side_effects(tmp_path):
    source = document(scope="node", sharing=True)
    source_before = copy.deepcopy(source)
    draft = request([row(index, max_processing_percent=10 + index) for index in reversed(range(8))])
    draft_before = copy.deepcopy(draft)
    candidate, enrollments, changed = build(tmp_path, source, draft)
    assert changed and source == source_before and draft == draft_before
    assert list(tmp_path.iterdir()) == []
    assert candidate["models"] == source["models"]
    assert candidate["contribution_policy"] == {
        **source["contribution_policy"],
        "sharing_enabled": False,
        "processing_scope": "per_device",
    }
    assert len({item["id"] for item in candidate["workers"]}) == 8
    assert [item["device"] for item in candidate["workers"]] == [f"cuda:{i}" for i in range(8)]
    for index, (saved, enrollment) in enumerate(zip(candidate["workers"], enrollments)):
        assert saved["id"].startswith("worker-") and len(saved["id"]) == 39
        assert saved["identity_path"] == f"worker-identities/{saved['id']}.key"
        assert saved["model"] == "auto" and saved["num_blocks"] == 1
        assert saved["enabled"] is False and saved["managed_by"] == "desktop_gpu"
        assert saved["max_vram"] == "60%" and saved["max_processing_percent"] == 10 + index
        assert enrollment == {
            "worker_id": saved["id"],
            "device": f"cuda:{index}",
            "selection_token": row(index)["selection_token"],
        }
    assert all(value not in json.dumps((candidate, enrollments)) for value in PHYSICAL.values())
    assert "selection_token" not in json.dumps(candidate)


def test_candidate_does_not_bypass_existing_automatic_worker_runtime_guard(tmp_path):
    candidate, _, _ = build(tmp_path, draft=request([row(0), row(1)]))
    with pytest.raises(NodeConfigError, match="at most one auto worker"):
        NodeConfig.from_dict(candidate, base_dir=tmp_path)


def test_sixteen_rows_supported_but_total_saved_workers_also_bounded(tmp_path):
    candidate, enrollments, _ = build(tmp_path, draft=request([row(index) for index in range(16)]))
    assert len(candidate["workers"]) == len(enrollments) == 16
    with pytest.raises(NodeConfigError, match="worker limit"):
        build(
            tmp_path, document([worker(0, managed=False, device="cpu")]), request([row(index) for index in range(16)])
        )
    with pytest.raises(NodeConfigError, match="worker limit"):
        build(tmp_path, document([worker()] * 17))


@pytest.mark.parametrize(
    "mapping",
    [
        None,
        {},
        {"cuda:0": None},
        {"cuda:0": "MIG-abc"},
        {"cuda:0": "secret-path"},
        {str(i): PHYSICAL["cuda:0"] for i in range(17)},
    ],
)
def test_selected_card_requires_trusted_bounded_physical_mapping(tmp_path, mapping):
    with pytest.raises(NodeConfigError) as failure:
        build(tmp_path, physical_devices=mapping)
    assert "secret-path" not in str(failure.value) and "GPU-" not in str(failure.value)


def test_physical_aliases_reject_even_when_devices_and_tokens_differ(tmp_path):
    mapping = {"cuda:0": PHYSICAL["cuda:0"], "cuda:1": PHYSICAL["cuda:0"][4:].upper()}
    with pytest.raises(NodeConfigError, match="distinct verified physical"):
        build(tmp_path, draft=request([row(0), row(1)]), physical_devices=mapping)
    assert list(tmp_path.iterdir()) == []


def test_noop_preserves_identity_worker_order_spelling_and_enrollment_plan(tmp_path, monkeypatch):
    source = document([worker(2, max_vram=".5%"), worker(0)])
    original = copy.deepcopy(source)
    monkeypatch.setattr(selection, "_new_identity", lambda *args: pytest.fail("no-op must not create an identity"))
    candidate, enrollments, changed = build(tmp_path, source, request([row(0), row(2, max_vram=".5%")]))
    assert changed is False and candidate == source == original and candidate is not source
    assert [entry["worker_id"] for entry in enrollments] == ["saved-0", "saved-2"]
    candidate["workers"][0]["max_vram"] = "90%"
    assert source == original


def test_compute_only_edit_preserves_identity_raw_memory_and_assignment(tmp_path):
    source = document([worker(max_vram="+.5%", model="existing-model", num_blocks=7, max_bandwidth_mbps=10)])
    candidate, enrollments, changed = build(
        tmp_path, source, request([row(max_vram="+.5%", max_processing_percent=25)])
    )
    assert changed
    assert candidate["workers"] == [{**source["workers"][0], "max_processing_percent": 25}]
    assert enrollments[0]["worker_id"] == "saved-0"


def test_unchecked_and_omitted_remove_only_managed_and_preserve_manual_limits(tmp_path):
    manual = worker(5, managed=False, max_vram="41GiB", max_processing_percent=37, block_indices="2:4")
    manual.pop("num_blocks")
    source = document([worker(0), manual, worker(1)])
    candidate, enrollments, changed = build(tmp_path, source, request([row(0, selected=False)]), physical_devices=None)
    assert changed and enrollments == []
    assert candidate["workers"] == [manual]
    assert candidate["contribution_policy"] == source["contribution_policy"]
    assert len(source["workers"]) == 3


def test_empty_selection_preserves_legacy_manual_policy_and_pauses_workers(tmp_path):
    manual = worker(managed=False, enabled=True)
    manual.pop("max_processing_percent")
    source = document([manual], scope="node", sharing=True)
    candidate, enrollments, changed = build(tmp_path, source, request([]), physical_devices=None)
    assert changed and enrollments == []
    assert candidate["workers"] == [{**manual, "enabled": False}]
    assert candidate["contribution_policy"] == {**source["contribution_policy"], "sharing_enabled": False}
    NodeConfig.from_dict(candidate, base_dir=tmp_path)


def test_empty_saved_selection_is_a_noop_without_physical_mapping(tmp_path):
    source = {"schema_version": 1, "models": [{"manifest": "manifest.json", "initial_peers": []}]}
    candidate, enrollments, changed = build(tmp_path, source, request([]), physical_devices=None)
    assert candidate == source and not changed and enrollments == []


def test_legacy_managed_single_card_migrates_only_explicit_allowances(tmp_path):
    managed = worker()
    managed.pop("max_processing_percent")
    source = document([managed], scope="node")
    candidate, _, _ = build(tmp_path, source, request([row(max_processing_percent=66)]))
    assert candidate["workers"][0]["id"] == managed["id"]
    assert candidate["workers"][0]["max_processing_percent"] == 66
    assert candidate["contribution_policy"]["max_processing_percent"] == 23
    assert candidate["contribution_policy"]["processing_scope"] == "per_device"
    NodeConfig.from_dict(candidate, base_dir=tmp_path)


def test_unmarked_auto_and_legacy_manual_scope_are_not_silently_migrated(tmp_path):
    legacy_auto = worker()
    legacy_auto.pop("managed_by")
    with pytest.raises(NodeConfigError, match="ownership"):
        build(tmp_path, document([legacy_auto]), request([]), physical_devices=None)
    manual = worker(5, managed=False)
    manual.pop("max_processing_percent")
    with pytest.raises(NodeConfigError, match="must be migrated"):
        build(tmp_path, document([manual], scope="node"))


@pytest.mark.parametrize("device", ["cuda:0", "cuda"])
def test_selected_manual_ordinal_conflicts_reject(tmp_path, device):
    with pytest.raises(NodeConfigError, match="manually configured"):
        build(tmp_path, document([worker(7, managed=False, device=device)]))


@pytest.mark.parametrize("device", [None, "cuda:00", "cuda:16", "cpu:0", "xpu:0"])
@pytest.mark.parametrize("selected", [False, True])
def test_ambiguous_manual_devices_reject_even_when_removing_all_managed(tmp_path, monkeypatch, device, selected):
    source = document([worker(1), worker(7, managed=False, device=device)], scope="node")
    for entry in source["workers"]:
        entry.pop("max_processing_percent")
    original = copy.deepcopy(source)
    monkeypatch.setattr(selection, "_new_identity", lambda *args: pytest.fail("reject before creating identities"))
    with pytest.raises(NodeConfigError, match="manual worker devices must be explicit"):
        build(tmp_path, source, request([row(2)] if selected else []))
    assert source == original and list(tmp_path.iterdir()) == []


def test_missing_manual_device_rejects_before_implicit_runtime_assignment(tmp_path):
    manual = worker(7, managed=False)
    manual.pop("device")
    manual.pop("max_processing_percent")
    with pytest.raises(NodeConfigError, match="manual worker devices must be explicit"):
        build(tmp_path, document([manual], scope="node"), request([]), physical_devices=None)


@pytest.mark.parametrize("device", ["cpu", "cuda", "cuda:0", "cuda:15"])
def test_supported_manual_devices_are_preserved_without_rewriting_aliases(tmp_path, device):
    manual = worker(7, managed=False, device=device, enabled=True)
    source = document([manual])
    candidate, enrollments, changed = build(tmp_path, source, request([row(2)]))
    assert changed and candidate["workers"][0] == {**manual, "enabled": False}
    assert len(enrollments) == 1 and enrollments[0]["device"] == "cuda:2"
    assert source["workers"][0] == manual


def test_duplicate_managed_devices_and_casefold_ids_reject(tmp_path):
    with pytest.raises(NodeConfigError, match="ownership is ambiguous"):
        build(tmp_path, document([worker(0), worker(1, device="cuda:0")]))
    with pytest.raises(NodeConfigError, match="unique existing worker IDs"):
        build(tmp_path, document([worker(0, id="SAVED-1"), worker(1)]))


def test_new_identities_retry_collisions_with_other_new_workers(tmp_path, monkeypatch):
    values = iter(("a" * 32, "a" * 32, "b" * 32))
    monkeypatch.setattr(identity_module.secrets, "token_hex", lambda count: next(values))
    candidate, _, _ = build(tmp_path, draft=request([row(0), row(1)]))
    assert [entry["id"] for entry in candidate["workers"]] == ["worker-" + "a" * 32, "worker-" + "b" * 32]
    assert list(tmp_path.iterdir()) == []


def test_removed_ids_and_retired_private_files_are_never_reused(tmp_path, monkeypatch):
    directory = tmp_path / "worker-identities"
    directory.mkdir()
    retired = directory / ("worker-" + "b" * 32 + ".key")
    retired.write_bytes(b"private retired identity")
    source = document([worker(0, id="worker-" + "a" * 32)])
    values = iter(("a" * 32, "b" * 32, "c" * 32))
    monkeypatch.setattr(identity_module.secrets, "token_hex", lambda count: next(values))
    candidate, _, _ = build(tmp_path, source, request([row(1)]))
    assert candidate["workers"][0]["id"] == "worker-" + "c" * 32
    assert retired.read_bytes() == b"private retired identity"
    assert list(directory.iterdir()) == [retired]


def test_exhausted_identity_generation_fails_without_source_mutation(tmp_path, monkeypatch):
    source = document([worker(0, id="worker-" + "a" * 32)])
    original = copy.deepcopy(source)
    monkeypatch.setattr(identity_module.secrets, "token_hex", lambda count: "a" * 32)
    with pytest.raises(NodeConfigError, match="fresh worker identity"):
        build(tmp_path, source, request([row(1)]))
    assert source == original and list(tmp_path.iterdir()) == []


def test_new_identity_rejects_redirected_private_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        identity_module.os.path, "isjunction", lambda path: path == tmp_path / "worker-identities", raising=False
    )
    with pytest.raises(NodeConfigError, match="unlinked directory"):
        build(tmp_path)
    assert list(tmp_path.iterdir()) == []
