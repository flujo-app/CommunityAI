"""Private selection inspection cannot create a new hardware choice."""

import hashlib
import json

import pytest

from drift.node.device_binding import DeviceBindingError, DeviceBindingStore
from drift.node.gpu_selection_tokens import GpuSelectionChangedError, GpuSelectionTokens

UUID_A = "GPU-00000000-0000-0000-0000-000000000001"
UUID_B = "GPU-00000000-0000-0000-0000-000000000002"
REVISION = "sha256:" + "a" * 64


def test_inspection_does_not_enroll_a_missing_directory_or_pin(tmp_path):
    probes = []
    directory = tmp_path / "pins"
    store = DeviceBindingStore(directory, identity_provider=lambda device: probes.append(device) or UUID_A)
    with pytest.raises(DeviceBindingError):
        store.load_existing("worker", "cuda:0")
    assert not directory.exists() and probes == []
    directory.mkdir(mode=0o700)
    with pytest.raises(DeviceBindingError):
        store.load_existing("worker", "cuda:0")
    assert list(directory.iterdir()) == [] and probes == []


def test_inspection_preserves_valid_pin_and_rejects_renumbering(tmp_path):
    mapping = {"cuda:0": UUID_A}
    store = DeviceBindingStore(tmp_path / "pins", identity_provider=mapping.get, liveness_provider=lambda value: True)
    original = store.bind("worker", "cuda:0")
    pin = next((tmp_path / "pins").glob("*.json"))
    before = pin.read_bytes()
    inspected = store.load_existing("worker", "cuda:0")
    assert inspected.cuda_visible_devices == original.cuda_visible_devices == UUID_A
    assert pin.read_bytes() == before
    mapping["cuda:0"] = UUID_B
    with pytest.raises(DeviceBindingError):
        store.load_existing("worker", "cuda:0")
    assert pin.read_bytes() == before


def test_inspection_rejects_deleted_or_changed_records(tmp_path):
    store = DeviceBindingStore(
        tmp_path / "pins", identity_provider=lambda device: UUID_A, liveness_provider=lambda _: True
    )
    store.bind("worker", "cuda:0")
    pin = tmp_path / "pins" / (hashlib.sha256(b"worker").hexdigest() + ".json")
    record = json.loads(pin.read_text())
    record["worker_id"] = "other"
    pin.write_text(json.dumps(record))
    with pytest.raises(DeviceBindingError):
        store.load_existing("worker", "cuda:0")
    pin.unlink()
    with pytest.raises(DeviceBindingError):
        store.load_existing("worker", "cuda:0")
    assert not pin.exists()


def test_private_verified_identity_deduplicates_alias_ordinals_without_public_uuid():
    tokens = GpuSelectionTokens(devices=lambda: ("cuda:0", "cuda:1"), identity=lambda _: UUID_A, live=lambda _: True)
    snapshot = tokens.snapshot(REVISION)
    assert UUID_A not in json.dumps(snapshot)
    identities = [
        tokens.verify_identity(row["device"], REVISION, row["selection_token"]) for row in snapshot["devices"]
    ]
    assert identities == [UUID_A, UUID_A]
    assert snapshot["devices"][0]["selection_token"] != snapshot["devices"][1]["selection_token"]
    row = snapshot["devices"][0]
    assert tokens.verify(row["device"], REVISION, row["selection_token"]) is None


@pytest.mark.parametrize("change", ["revision", "mapping", "liveness", "token"])
def test_verified_identity_rejects_stale_or_invalid_context(change):
    current = {"identity": UUID_A, "live": True}
    tokens = GpuSelectionTokens(
        devices=lambda: ("cuda:0",), identity=lambda _: current["identity"], live=lambda _: current["live"]
    )
    token = tokens.snapshot(REVISION)["devices"][0]["selection_token"]
    revision = REVISION
    if change == "revision":
        revision = "sha256:" + "b" * 64
    elif change == "mapping":
        current["identity"] = UUID_B
    elif change == "liveness":
        current["live"] = False
    else:
        token = "sha256:" + "0" * 64
    with pytest.raises(GpuSelectionChangedError):
        tokens.verify_identity("cuda:0", revision, token)
