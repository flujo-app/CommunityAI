import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from drift.model_manifest import ModelManifest
from drift.node.model_manager import (
    AmbiguousModelError,
    AutoModelUnavailableError,
    ModelDescriptor,
    ModelInUseError,
    ModelManager,
    ModelManagerClosedError,
    ModelRuntime,
    ModelState,
    ModelUnloadError,
)


def test_manifest_registration_resolves_name_alias_and_digest():
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manager = ModelManager()
    manager.register_manifest(manifest, lambda: ModelRuntime(object(), object()))

    for identifier in (manifest.name, *manifest.aliases, manifest.digest_id):
        assert manager.resolve(identifier).manifest_digest == manifest.digest_id
    assert manager.snapshots()[0].state is ModelState.KNOWN


@pytest.mark.parametrize(
    ("manifest_path", "expected_bytes"),
    (
        (
            "public-alpha/catalog-v1/manifests/3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33.json",
            4_571_197_320,
        ),
        (
            "public-alpha/catalog-v1/manifests/2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd.json",
            10_278_818_149,
        ),
    ),
)
def test_public_manifest_snapshot_reports_exact_selected_whole_shard_bytes(manifest_path, expected_bytes):
    manifest = ModelManifest.load(manifest_path)
    manager = ModelManager()
    manager.register_manifest(manifest, lambda: ModelRuntime(object(), object()))

    snapshot = manager.snapshots()[0].to_dict()

    assert snapshot["download"] == {
        "schema_version": 1,
        "selected_whole_shard_bytes": expected_bytes,
    }
    assert expected_bytes == sum(artifact.size for artifact in manifest.artifacts)


def test_manager_rejects_alias_collisions_case_insensitively():
    manager = ModelManager()
    manager.register(ModelDescriptor("First", aliases=("shared",)), lambda: ModelRuntime(object(), object()))

    with pytest.raises(ValueError, match="already registered"):
        manager.register(ModelDescriptor("Second", aliases=("SHARED",)), lambda: ModelRuntime(object(), object()))


def test_lazy_load_is_serialized_and_published_once():
    manager = ModelManager()
    calls = 0
    calls_lock = threading.Lock()
    runtime = ModelRuntime(object(), object())

    def loader():
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        return runtime

    manager.register(ModelDescriptor("model", aliases=("alias",)), loader)
    with ThreadPoolExecutor(max_workers=8) as executor:
        loaded = list(executor.map(lambda _: manager.load("alias"), range(8)))

    assert calls == 1
    assert all(item.runtime is runtime for item in loaded)
    assert manager.snapshots()[0].state is ModelState.READY


def test_failed_lazy_load_is_visible_and_can_retry():
    manager = ModelManager()
    attempts = 0

    def loader():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary failure")
        return ModelRuntime(object(), object())

    manager.register(ModelDescriptor("model"), loader)
    with pytest.raises(RuntimeError, match="temporary failure"):
        manager.load("model")
    failed = manager.snapshots()[0]
    assert failed.state is ModelState.UNAVAILABLE
    assert failed.last_error == "RuntimeError: temporary failure"

    assert manager.load("model").descriptor.model_id == "model"
    assert manager.snapshots()[0].state is ModelState.READY


def test_omitted_model_is_only_valid_for_one_registration():
    manager = ModelManager()
    manager.register_loaded("one", object(), object())
    assert manager.load(None).descriptor.model_id == "one"
    manager.register_loaded("two", object(), object())

    with pytest.raises(AmbiguousModelError, match="model is required"):
        manager.load(None)


def test_shutdown_closes_loaded_runtime_once_and_blocks_new_loads():
    manager = ModelManager()
    close_calls = 0

    def close():
        nonlocal close_calls
        close_calls += 1

    manager.register(ModelDescriptor("model"), lambda: ModelRuntime(object(), object(), close=close))
    manager.load("model")
    manager.shutdown()
    manager.shutdown()

    assert close_calls == 1
    assert manager.closed
    assert manager.snapshots()[0].state is ModelState.STOPPING
    with pytest.raises(ModelManagerClosedError):
        manager.load("model")


def test_shutdown_continues_after_one_runtime_close_fails(caplog):
    manager = ModelManager()
    second_closed = []

    def fail_close():
        raise RuntimeError("close failed")

    manager.register(ModelDescriptor("first"), lambda: ModelRuntime(None, None, fail_close))
    manager.register(ModelDescriptor("second"), lambda: ModelRuntime(None, None, lambda: second_closed.append(True)))
    manager.load("first")
    manager.load("second")

    manager.shutdown()

    assert second_closed == [True]
    assert "Failed to close model runtime 'first'" in caplog.text


def test_runtime_lease_blocks_unload_until_released():
    manager = ModelManager(max_loaded_models=1)
    closed = []
    manager.register(
        ModelDescriptor("model", aliases=("alias",)),
        lambda: ModelRuntime(object(), object(), close=lambda: closed.append("model")),
    )

    lease = manager.load("alias")
    assert manager.snapshots()[0].active_requests == 1
    with pytest.raises(ModelInUseError, match="1 active request"):
        manager.unload("model")

    lease.release()
    lease.release()
    assert manager.snapshots()[0].active_requests == 0
    assert manager.unload("alias") is True
    assert manager.unload("alias") is False
    assert closed == ["model"]
    assert manager.snapshots()[0].state is ModelState.KNOWN


def test_snapshot_marks_loaded_incomplete_routes_as_degraded():
    manager = ModelManager()
    route = {"status": "incomplete", "covered_blocks": 1, "missing_blocks": [1]}
    manager.register(
        ModelDescriptor("model"),
        lambda: ModelRuntime(object(), object(), route_health=lambda: route),
    )

    manager.load("model").release()
    snapshot = manager.snapshots()[0]

    assert snapshot.state is ModelState.DEGRADED
    assert snapshot.route == route


def test_unloaded_model_can_publish_lightweight_discovery_health():
    manager = ModelManager()
    route = {"status": "complete", "source": "discovery"}
    manager.register(
        ModelDescriptor("model"),
        lambda: ModelRuntime(object(), object()),
        route_health=lambda: route,
    )

    snapshot = manager.snapshots()[0]

    assert snapshot.state is ModelState.KNOWN
    assert snapshot.route == route


def test_auto_selects_the_first_catalog_model_with_a_complete_live_route():
    manager = ModelManager()
    routes = {
        "primary": {
            "status": "complete",
            "source": "discovery",
            "covered_blocks": 24,
            "total_blocks": 24,
            "peer_count": 1,
        },
        "standby": {
            "status": "complete",
            "source": "discovery",
            "covered_blocks": 24,
            "total_blocks": 24,
            "peer_count": 2,
        },
    }
    manager.register(
        ModelDescriptor("Qwen candidate", manifest_digest="sha256:" + "a" * 64),
        lambda: ModelRuntime(object(), object()),
        route_health=lambda: routes["primary"],
    )
    manager.register(
        ModelDescriptor("Gemma standby", manifest_digest="sha256:" + "b" * 64),
        lambda: ModelRuntime(object(), object()),
        route_health=lambda: routes["standby"],
    )
    manager.configure_auto_selection(("Qwen candidate", "Gemma standby"))

    selection = manager.auto_selection_snapshot()
    assert selection["status"] == "selected"
    assert selection["model"] == "Qwen candidate"
    assert selection["covered_blocks"] == selection["total_blocks"] == 24
    assert "catalog priority 1" in selection["reason"]
    assert manager.load("auto").descriptor.model_id == "Qwen candidate"

    routes["primary"] = {
        "status": "incomplete",
        "source": "discovery",
        "covered_blocks": 23,
        "total_blocks": 24,
        "peer_count": 1,
    }
    assert manager.resolve("auto").model_id == "Gemma standby"
    assert manager.resolve("Qwen candidate").model_id == "Qwen candidate"


def test_auto_fails_closed_when_no_catalog_model_has_a_complete_route():
    manager = ModelManager()
    manager.register(
        ModelDescriptor("candidate", manifest_digest="sha256:" + "c" * 64),
        lambda: ModelRuntime(object(), object()),
        route_health=lambda: {
            "status": "incomplete",
            "source": "discovery",
            "covered_blocks": 23,
            "total_blocks": 24,
            "peer_count": 1,
        },
    )
    manager.configure_auto_selection(("candidate",))

    assert manager.auto_selection_snapshot()["status"] == "unavailable"
    with pytest.raises(AutoModelUnavailableError, match="complete live route"):
        manager.load("auto")


def test_shutdown_callbacks_run_once_even_when_one_fails(caplog):
    manager = ModelManager()
    calls = []
    manager.add_shutdown_callback(lambda: (_ for _ in ()).throw(RuntimeError("service failed")))
    manager.add_shutdown_callback(lambda: calls.append("closed"))

    manager.shutdown()
    manager.shutdown()

    assert calls == ["closed"]
    assert "Failed to close a model-manager service" in caplog.text


def test_bounded_residency_evicts_the_least_recent_idle_model():
    manager = ModelManager(max_loaded_models=2)
    closed = []
    for name in ("alpha", "beta", "gamma"):
        manager.register(
            ModelDescriptor(name),
            lambda selected=name: ModelRuntime(object(), object(), close=lambda: closed.append(selected)),
        )

    manager.load("alpha").release()
    time.sleep(0.002)
    manager.load("beta").release()
    time.sleep(0.002)
    manager.load("alpha").release()
    time.sleep(0.002)
    manager.load("gamma").release()

    snapshots = {snapshot.model_id: snapshot for snapshot in manager.snapshots()}
    assert snapshots["alpha"].state is ModelState.READY
    assert snapshots["beta"].state is ModelState.KNOWN
    assert snapshots["gamma"].state is ModelState.READY
    assert manager.residency() == {"max_loaded_models": 2, "resident_models": 2}
    assert closed == ["beta"]


def test_capacity_waits_instead_of_evicting_an_active_runtime():
    manager = ModelManager(max_loaded_models=1)
    closed = []
    attempted_second = threading.Event()
    manager.register(
        ModelDescriptor("first"),
        lambda: ModelRuntime(object(), object(), close=lambda: closed.append("first")),
    )
    manager.register(ModelDescriptor("second"), lambda: ModelRuntime(object(), object()))
    first = manager.load("first")

    def load_second():
        attempted_second.set()
        return manager.load("second")

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(load_second)
        assert attempted_second.wait(timeout=1)
        time.sleep(0.02)
        assert not pending.done()
        first.release()
        second = pending.result(timeout=1)

    assert closed == ["first"]
    assert second.descriptor.model_id == "second"
    second.release()


def test_failed_cleanup_keeps_its_runtime_slot_reserved():
    manager = ModelManager(max_loaded_models=1)
    manager.register(
        ModelDescriptor("broken"),
        lambda: ModelRuntime(object(), object(), close=lambda: (_ for _ in ()).throw(RuntimeError("close failed"))),
    )
    manager.register(ModelDescriptor("other"), lambda: ModelRuntime(object(), object()))
    manager.load("broken").release()

    with pytest.raises(ModelUnloadError, match="close failed"):
        manager.unload("broken")

    snapshot = {item.model_id: item for item in manager.snapshots()}["broken"]
    assert snapshot.state is ModelState.UNAVAILABLE
    assert manager.residency() == {"max_loaded_models": 1, "resident_models": 1}
    with pytest.raises(ModelUnloadError, match="failed cleanup"):
        manager.load("other")


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_runtime_budget_must_be_a_positive_integer(value):
    with pytest.raises(ValueError, match="max_loaded_models"):
        ModelManager(max_loaded_models=value)
