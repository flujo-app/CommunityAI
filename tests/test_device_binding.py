import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from drift.node import device_binding as module
from drift.node.device_binding import DeviceBindingError, DeviceBindingStore, normalize_cuda_uuid

GPU_A = "GPU-11111111-2222-3333-4444-555555555555"
GPU_B = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def make_store(tmp_path, selected=None, *, live=None):
    selected = {"cuda:0": GPU_A} if selected is None else selected
    return DeviceBindingStore(
        tmp_path / "pins",
        identity_provider=lambda device: selected.get(device),
        liveness_provider=(lambda identity: True) if live is None else live,
    )


def only_record(tmp_path):
    return next((tmp_path / "pins").glob("*.json"))


@pytest.mark.parametrize("value", [GPU_A, GPU_A[4:], GPU_A.upper(), GPU_A.lower()])
def test_uuid_normalizes_torch_bare_and_nvml_full_identity(value):
    assert normalize_cuda_uuid(value) == GPU_A


@pytest.mark.parametrize(
    "value",
    [
        None,
        1,
        {},
        "",
        "GPU-",
        "GPU-short",
        "MIG-" + GPU_A[4:],
        " " + GPU_A,
        GPU_A + "\n",
        "0" * 2000,
        "00000000-0000-0000-0000-000000000000",
        GPU_A[4:].replace("-", ""),
    ],
)
def test_uuid_rejects_nonphysical_or_malformed_identifiers(value):
    assert normalize_cuda_uuid(value) is None


def test_persisted_pin_survives_new_node_and_worker_id_case(tmp_path):
    first = make_store(tmp_path).bind("Worker-A", "cuda:0")
    original = only_record(tmp_path).read_bytes()
    second = make_store(tmp_path).bind("worker-a", "cuda:0")
    assert second.cuda_visible_devices == GPU_A
    assert first.check() is None and second.check() is None
    assert only_record(tmp_path).read_bytes() == original
    assert GPU_A not in repr(first)
    assert GPU_A[4:] not in repr(first)
    assert list((tmp_path / "pins").glob(".pending-*")) == []


def test_changed_ordinal_identity_never_overwrites_previous_selection(tmp_path):
    binding = make_store(tmp_path).bind("worker", "cuda:0")
    original = only_record(tmp_path).read_bytes()
    for selected in ({"cuda:0": GPU_B}, {}, {"cuda:1": GPU_A}):
        with pytest.raises(DeviceBindingError) as error:
            make_store(tmp_path, selected).bind("worker", "cuda:0")
        assert GPU_A not in str(error.value) and GPU_B not in str(error.value)
        assert only_record(tmp_path).read_bytes() == original
    assert binding.check() is None


def test_guard_fails_on_loss_replacement_and_missing_record(tmp_path):
    selected = {"cuda:0": GPU_A}
    binding = make_store(tmp_path, selected).bind("worker", "cuda:0")
    selected["cuda:0"] = GPU_B
    assert binding.check() is not None
    selected.clear()
    assert binding.check() is not None
    selected["cuda:0"] = GPU_A
    assert binding.check() is None
    only_record(tmp_path).unlink()
    assert binding.check() is not None


def test_cached_torch_identity_does_not_hide_nvml_device_loss(tmp_path):
    available = {GPU_A}
    binding = make_store(tmp_path, live=lambda identity: identity in available).bind("worker", "cuda:0")
    available.clear()
    assert binding.check() is not None
    with pytest.raises(DeviceBindingError):
        make_store(tmp_path, live=lambda identity: False).bind("worker", "cuda:0")


@pytest.mark.parametrize(
    "selected,live", [({}, True), ({"cuda:0": "MIG-" + GPU_A[4:]}, True), ({"cuda:0": GPU_A}, False)]
)
def test_failed_initial_identity_or_liveness_does_not_enroll(tmp_path, selected, live):
    with pytest.raises(DeviceBindingError):
        make_store(tmp_path, selected, live=lambda identity: live).bind("worker", "cuda:0")
    assert list((tmp_path / "pins").glob("*.json")) == []


@pytest.mark.parametrize("first,second", [("cpu", "cuda:0"), ("cuda:0", "cpu"), ("cuda:0", "cuda:1")])
def test_automatic_detection_cannot_change_persisted_backend_or_ordinal(tmp_path, first, second):
    store = make_store(tmp_path, {"cuda:0": GPU_A, "cuda:1": GPU_A})
    store.bind("worker", first)
    with pytest.raises(DeviceBindingError):
        store.bind("worker", second)


def test_cpu_selection_does_not_touch_gpu_metadata(tmp_path):
    def forbidden(*args):
        raise AssertionError("CPU must not query accelerators")

    store = DeviceBindingStore(tmp_path / "pins", identity_provider=forbidden, liveness_provider=forbidden)
    assert store.bind("cpu-worker", "cpu") is None
    assert store.bind("cpu-worker", "cpu") is None
    assert json.loads(only_record(tmp_path).read_text())["identity"] is None


def test_new_worker_id_is_explicit_reselection(tmp_path):
    make_store(tmp_path).bind("old-worker", "cuda:0")
    replacement = make_store(tmp_path, {"cuda:0": GPU_B}).bind("new-worker", "cuda:0")
    assert replacement.cuda_visible_devices == GPU_B
    assert len(list((tmp_path / "pins").glob("*.json"))) == 2


@pytest.mark.parametrize("worker_id", ["../outside", "x/y", "x\\y", "a" * 65, "", ".", "..", 1, None, "wørker"])
def test_invalid_worker_ids_never_create_paths(tmp_path, worker_id):
    with pytest.raises(DeviceBindingError):
        make_store(tmp_path).bind(worker_id, "cpu")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "device", ["xpu:0", "mps", "cuda", "auto", "cuda:-1", "cuda:16", "cuda:01", "cuda:99999", None]
)
def test_unsupported_or_noncanonical_device_never_enrolls(tmp_path, device):
    with pytest.raises(DeviceBindingError):
        make_store(tmp_path).bind("worker", device)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "mutation",
    ["oversized", "truncated", "duplicate", "boolean_schema", "wrong_worker", "null_gpu", "extra", "noncanonical"],
)
def test_corrupt_records_fail_closed_without_replacement_or_identity_leak(tmp_path, mutation):
    binding = make_store(tmp_path).bind("worker", "cuda:0")
    path = only_record(tmp_path)
    value = json.loads(path.read_text())
    if mutation == "oversized":
        raw = " " * 2048
    elif mutation == "truncated":
        raw = '{"identity":"' + GPU_A
    elif mutation == "duplicate":
        raw = '{"identity":"' + GPU_A + '",' + path.read_text()[1:]
    else:
        if mutation == "boolean_schema":
            value["schema_version"] = True
        elif mutation == "wrong_worker":
            value["worker_id"] = "different-worker"
        elif mutation == "null_gpu":
            value["identity"] = None
        elif mutation == "extra":
            value["secret"] = "private"
        else:
            value["identity"] = GPU_A[4:]
        raw = json.dumps(value)
    path.write_text(raw)
    assert binding.check() is not None
    with pytest.raises(DeviceBindingError) as error:
        make_store(tmp_path).bind("worker", "cuda:0")
    assert path.read_text() == raw
    assert GPU_A not in str(error.value) and str(tmp_path) not in str(error.value)


def test_atomic_publish_failure_leaves_no_partial_pin(tmp_path, monkeypatch):
    def fail_link(*args):
        raise OSError("private driver or path detail")

    monkeypatch.setattr(module.os, "link", fail_link)
    with pytest.raises(DeviceBindingError) as error:
        make_store(tmp_path).bind("worker", "cuda:0")
    assert "private driver" not in str(error.value)
    assert list((tmp_path / "pins").iterdir()) == []


def test_concurrent_first_enrollment_cannot_overwrite_winning_pin(tmp_path, monkeypatch):
    barrier = threading.Barrier(2)
    create = DeviceBindingStore._create

    def simultaneous_create(self, path, record):
        barrier.wait(timeout=5)
        return create(self, path, record)

    monkeypatch.setattr(DeviceBindingStore, "_create", simultaneous_create)

    def enroll(identity):
        try:
            return make_store(tmp_path, {"cuda:0": identity}).bind("worker", "cuda:0").cuda_visible_devices
        except DeviceBindingError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enroll, [GPU_A, GPU_B]))
    assert results.count(None) == 1
    assert json.loads(only_record(tmp_path).read_text())["identity"] == next(value for value in results if value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions and symlink restrictions need POSIX filesystem")
def test_posix_permissions_and_symlink_rejection(tmp_path):
    binding = make_store(tmp_path).bind("worker", "cuda:0")
    path = only_record(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    path.chmod(0o644)
    assert binding.check() is not None
    path.chmod(0o600)
    target = tmp_path / "outside"
    path.rename(target)
    path.symlink_to(target)
    assert binding.check() is not None
    with pytest.raises(DeviceBindingError):
        make_store(tmp_path).bind("worker", "cuda:0")


def test_linked_ancestor_is_rejected_before_writing(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "linked"
    if os.name == "nt":
        # Junction creation needs no Windows symlink privilege. No delete or
        # move is delegated to this shell; pytest owns temporary cleanup.
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)
    store = DeviceBindingStore(link / "pins")
    with pytest.raises(DeviceBindingError):
        store.bind("worker", "cpu")
    assert list(target.iterdir()) == []


def test_real_metadata_providers_use_uuid_and_fresh_liveness_without_allocations(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_DEFAULT_LIVENESS_PROBE", module._NvidiaIdentityProbe())
    operations = []
    available = [True]

    class TorchUuid:
        def __str__(self):
            return GPU_A[4:]

    def get_handle(identity):
        operations.append(("handle", identity))
        assert identity == GPU_A
        return "private-handle"

    def memory_info(handle):
        operations.append(("memory", handle))
        if not available[0]:
            raise RuntimeError("lost " + GPU_A)
        return SimpleNamespace(total=8 * 1024**3)

    cuda = SimpleNamespace(
        is_available=lambda: True,
        device_count=lambda: 1,
        get_device_properties=lambda index: SimpleNamespace(uuid=TorchUuid()),
    )
    nvml = SimpleNamespace(
        nvmlInit=lambda: operations.append(("init",)),
        nvmlDeviceGetHandleByUUID=get_handle,
        nvmlDeviceGetUUID=lambda handle: GPU_A.encode("ascii"),
        nvmlDeviceGetMemoryInfo=memory_info,
    )
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda))
    monkeypatch.setitem(sys.modules, "pynvml", nvml)
    binding = DeviceBindingStore(tmp_path / "pins").bind("worker", "cuda:0")
    operations.clear()
    assert binding.check() is None
    assert operations == [("handle", GPU_A), ("memory", "private-handle")]
    available[0] = False
    reason = binding.check()
    assert reason is not None and GPU_A not in reason


def test_missing_nvml_fails_closed_without_enrolling(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_DEFAULT_LIVENESS_PROBE", module._NvidiaIdentityProbe())
    monkeypatch.setitem(sys.modules, "pynvml", None)
    store = DeviceBindingStore(tmp_path / "pins", identity_provider=lambda device: GPU_A)
    with pytest.raises(DeviceBindingError):
        store.bind("worker", "cuda:0")
    assert list((tmp_path / "pins").glob("*.json")) == []


def test_repeated_store_reconciliation_uses_one_process_nvml_session(tmp_path, monkeypatch):
    operations = []
    monkeypatch.setattr(module, "_DEFAULT_LIVENESS_PROBE", module._NvidiaIdentityProbe())
    monkeypatch.setitem(
        sys.modules,
        "pynvml",
        SimpleNamespace(
            nvmlInit=lambda: operations.append("init"),
            nvmlDeviceGetHandleByUUID=lambda identity: operations.append("handle") or identity,
            nvmlDeviceGetUUID=lambda handle: handle,
            nvmlDeviceGetMemoryInfo=lambda handle: SimpleNamespace(total=8 * 1024**3),
        ),
    )
    bindings = []
    for index in range(20):
        store = DeviceBindingStore(tmp_path / "pins", identity_provider=lambda device: GPU_A)
        bindings.append(store.bind(f"worker-{index % 2}", "cuda:0"))
    assert operations.count("init") == 1
    operations.clear()
    assert all(binding.check() is None for binding in bindings)
    assert operations == ["handle"] * len(bindings)
