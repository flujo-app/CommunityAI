from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import gateq38_linux_host_transport as transport, gateq38_route_controller as route

from tests import test_gateq38_route_controller as route_test

NOW = 1_900_000_000
KEY = bytes(range(transport.KEY_BYTES))
OTHER_KEY = b"x" * transport.KEY_BYTES
BOOT_ID = "01234567-89ab-4cde-8fab-0123456789ab"
PREPARED_DIGEST = "sha256:" + "9" * 64
INSTANCE_ID = "123456789"
CREATED = "2026-09-03T01:20:00+00:00"


@pytest.fixture(autouse=True)
def _trusted_plan_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(route, "_assert_protected_path", lambda *args, **kwargs: None)


def _plan(tmp_path: Path) -> route.RoutePlan:
    return route_test._load_plan(tmp_path)


def _worker_resource(plan: route.RoutePlan, index: int = 0) -> route.ResourcePlan:
    return [item for item in plan.resources if item.kind == "worker_instance"][index]


def _bootstrap_resource(plan: route.RoutePlan) -> route.ResourcePlan:
    return next(item for item in plan.resources if item.kind == "bootstrap_instance")


def _context(
    plan: route.RoutePlan,
    resource: route.ResourcePlan,
    *,
    key: bytes = KEY,
    instance_id: str = INSTANCE_ID,
    created: str = CREATED,
) -> dict[str, object]:
    return transport.build_instance_context(
        plan,
        resource.name,
        instance_id,
        created,
        issued_at_unix=NOW - 10,
        expires_at_unix=NOW + 600,
        key=key,
    )


def _worker_payload(plan: route.RoutePlan, resource: route.ResourcePlan, state: str = "ready") -> dict[str, object]:
    assert resource.worker_id is not None
    worker = plan.worker_by_id[resource.worker_id]
    return {
        "state": state,
        "machine_id": worker.machine_id,
        "peer_id": "Qm" + "a" * 44 if state == "ready" else None,
        "source_commit": plan.source_commit,
        "plan_digest": plan.plan_digest,
        "worker_plan_digest": plan.worker_plan_digest,
        "start_action_id": route._action_id(plan, "start_route"),
        "span": worker.span,
        "manifest_digest": plan.manifest_digest,
        "artifact_bytes": worker.artifact_bytes,
        "artifact_set_digest": worker.artifact_set_digest,
        "cache_root": worker.cache_root,
    }


def _bootstrap_payload(plan: route.RoutePlan, state: str = "running") -> dict[str, object]:
    return {
        "state": state,
        "job_id": plan.route_job_id,
        "collect_action_id": route._action_id(plan, "collect_route"),
        "run_id": plan.run_id,
        "plan_digest": plan.plan_digest,
        "source_commit": plan.source_commit,
        "manifest_digest": plan.manifest_digest,
        "worker_plan_digest": plan.worker_plan_digest,
        "evidence_digest": None,
        "route_record": None,
    }


def _envelope(
    plan: route.RoutePlan,
    resource: route.ResourcePlan,
    *,
    key: bytes = KEY,
    state: str = "ready",
) -> dict[str, object]:
    context = _context(plan, resource, key=key)
    payload = (
        _worker_payload(plan, resource, state)
        if resource.kind == "worker_instance"
        else _bootstrap_payload(plan, "running")
    )
    return transport.build_status_envelope(
        context,
        payload,
        plan,
        key=key,
        boot_id=BOOT_ID,
        revision=1,
        published_at_unix=NOW,
        prepared_record_digest=PREPARED_DIGEST,
    )


def _validate(
    envelope: dict[str, object],
    plan: route.RoutePlan,
    resource: route.ResourcePlan,
    *,
    key: bytes = KEY,
    now: int = NOW,
    minimum_revision: int = 0,
    boot_id: str | None = None,
) -> dict[str, object]:
    return transport.validate_status_envelope(
        envelope,
        plan,
        key=key,
        now_unix=now,
        expected_resource_name=resource.name,
        expected_generation_digest=envelope["context"]["instance_generation_digest"],
        minimum_revision=minimum_revision,
        expected_boot_id=boot_id,
    )


def test_worker_status_round_trip_binds_exact_instance_and_plan(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    resource = _worker_resource(plan)
    envelope = _envelope(plan, resource)

    decoded = transport.decode_status_envelope(transport.encode_status_envelope(envelope))
    validated = _validate(decoded, plan, resource, boot_id=BOOT_ID)

    assert validated == envelope
    assert validated["context"]["resource_name"] == resource.name
    assert validated["context"]["instance_generation_digest"] == route.instance_generation_digest(
        resource.name,
        INSTANCE_ID,
        CREATED,
    )
    assert validated["payload"]["span"] == plan.worker_by_id[resource.worker_id].span


def test_bootstrap_status_round_trip_has_no_worker_identity(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    resource = _bootstrap_resource(plan)
    envelope = _envelope(plan, resource)

    validated = _validate(envelope, plan, resource)

    assert validated["context"]["role"] == "bootstrap"
    assert validated["context"]["worker_id"] is None
    assert validated["payload"]["state"] == "running"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "other-run"),
        ("source_commit", "b" * 40),
        ("plan_digest", "sha256:" + "1" * 64),
        ("execution_inventory_digest", "sha256:" + "2" * 64),
        ("worker_plan_digest", "sha256:" + "3" * 64),
        ("start_action_id", "sha256:" + "4" * 64),
        ("collect_action_id", "sha256:" + "5" * 64),
        ("project", "other-project"),
        ("zone", "us-central1-c"),
        ("resource_kind", "bootstrap_instance"),
        ("role", "bootstrap"),
        ("worker_id", None),
        ("instance_id", "987654321"),
        ("creation_timestamp", "2026-09-03T01:21:00+00:00"),
        ("instance_generation_digest", "sha256:" + "6" * 64),
    ],
)
def test_context_substitution_fails_closed(tmp_path: Path, field: str, value: object) -> None:
    plan = _plan(tmp_path)
    resource = _worker_resource(plan)
    context = _context(plan, resource)
    context[field] = value

    with pytest.raises(transport.Q38LinuxHostTransportError):
        transport.validate_instance_context(context, plan, key=KEY, now_unix=NOW)


def test_context_rejects_wrong_key_and_cross_instance_replay(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    first = _worker_resource(plan, 0)
    second = _worker_resource(plan, 1)
    context = _context(plan, first)

    with pytest.raises(transport.Q38LinuxHostTransportError, match="authentication"):
        transport.validate_instance_context(context, plan, key=OTHER_KEY, now_unix=NOW)
    with pytest.raises(transport.Q38LinuxHostTransportError, match="resource"):
        transport.validate_instance_context(
            context,
            plan,
            key=KEY,
            now_unix=NOW,
            expected_resource_name=second.name,
        )


@pytest.mark.parametrize(
    ("issued", "expires", "now"),
    [
        (NOW + 31, NOW + 100, NOW),
        (NOW - transport.MAX_CONTEXT_SECONDS - 1, NOW + 1, NOW),
        (NOW - 10, NOW, NOW),
        (NOW - 10, NOW + transport.MAX_CONTEXT_SECONDS + 1, NOW),
    ],
)
def test_context_time_window_fails_closed(
    tmp_path: Path,
    issued: int,
    expires: int,
    now: int,
) -> None:
    plan = _plan(tmp_path)
    resource = _worker_resource(plan)
    with pytest.raises(transport.Q38LinuxHostTransportError, match="time window|stale"):
        context = transport.build_instance_context(
            plan,
            resource.name,
            INSTANCE_ID,
            CREATED,
            issued_at_unix=issued,
            expires_at_unix=expires,
            key=KEY,
        )
        transport.validate_instance_context(context, plan, key=KEY, now_unix=now)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("boot_id",), "not-a-boot-id"),
        (("revision",), 0),
        (("prepared_record_digest",), "wrong"),
        (("payload_digest",), "sha256:" + "0" * 64),
        (("envelope_hmac",), "hmac-sha256:" + "0" * 64),
        (("payload", "artifact_bytes"), 1),
        (("payload", "state"), "absent"),
        (("context", "instance_generation_digest"), "sha256:" + "0" * 64),
    ],
)
def test_envelope_mutation_fails_closed(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
) -> None:
    plan = _plan(tmp_path)
    resource = _worker_resource(plan)
    envelope = copy.deepcopy(_envelope(plan, resource))
    target = envelope
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(transport.Q38LinuxHostTransportError):
        _validate(envelope, plan, resource)


def test_status_rejects_stale_revision_time_and_boot(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    resource = _worker_resource(plan)
    envelope = _envelope(plan, resource)

    with pytest.raises(transport.Q38LinuxHostTransportError, match="revision"):
        _validate(envelope, plan, resource, minimum_revision=1)
    with pytest.raises(transport.Q38LinuxHostTransportError, match="publication"):
        _validate(envelope, plan, resource, now=NOW + transport.MAX_STATUS_AGE_SECONDS + 1)
    with pytest.raises(transport.Q38LinuxHostTransportError, match="boot"):
        _validate(
            envelope,
            plan,
            resource,
            boot_id="11234567-89ab-4cde-8fab-0123456789ab",
        )


def test_worker_and_bootstrap_payloads_cannot_cross_roles(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    worker = _worker_resource(plan)
    bootstrap = _bootstrap_resource(plan)

    with pytest.raises(transport.Q38LinuxHostTransportError, match="worker status"):
        transport.build_status_envelope(
            _context(plan, worker),
            _bootstrap_payload(plan),
            plan,
            key=KEY,
            boot_id=BOOT_ID,
            revision=1,
            published_at_unix=NOW,
            prepared_record_digest=PREPARED_DIGEST,
        )
    with pytest.raises(transport.Q38LinuxHostTransportError, match="route-job status"):
        transport.build_status_envelope(
            _context(plan, bootstrap),
            _worker_payload(plan, worker),
            plan,
            key=KEY,
            boot_id=BOOT_ID,
            revision=1,
            published_at_unix=NOW,
            prepared_record_digest=PREPARED_DIGEST,
        )


def test_ready_worker_requires_peer_and_non_passed_job_rejects_evidence(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    worker = _worker_resource(plan)
    worker_payload = _worker_payload(plan, worker)
    worker_payload["peer_id"] = None

    with pytest.raises(transport.Q38LinuxHostTransportError, match="peer"):
        transport.build_status_envelope(
            _context(plan, worker),
            worker_payload,
            plan,
            key=KEY,
            boot_id=BOOT_ID,
            revision=1,
            published_at_unix=NOW,
            prepared_record_digest=PREPARED_DIGEST,
        )

    bootstrap = _bootstrap_resource(plan)
    job = _bootstrap_payload(plan)
    job["evidence_digest"] = "sha256:" + "e" * 64
    with pytest.raises(transport.Q38LinuxHostTransportError, match="exposed evidence"):
        transport.build_status_envelope(
            _context(plan, bootstrap),
            job,
            plan,
            key=KEY,
            boot_id=BOOT_ID,
            revision=1,
            published_at_unix=NOW,
            prepared_record_digest=PREPARED_DIGEST,
        )


def test_transport_framing_is_canonical_bounded_and_duplicate_safe(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    resource = _worker_resource(plan)
    envelope = _envelope(plan, resource)
    encoded = transport.encode_status_envelope(envelope)

    assert encoded.endswith(b"\n")
    assert len(encoded) <= transport.MAX_ENVELOPE_BYTES
    assert transport.encode_status_envelope(transport.decode_status_envelope(encoded)) == encoded

    with pytest.raises(transport.Q38LinuxHostTransportError, match="duplicate"):
        transport.decode_status_envelope(b'{"schema_version":1,"schema_version":1}\n')
    with pytest.raises(transport.Q38LinuxHostTransportError, match="framing"):
        transport.decode_status_envelope(encoded + b"\n")
    with pytest.raises(transport.Q38LinuxHostTransportError, match="bytes"):
        transport.decode_status_envelope(b"x" * (transport.MAX_ENVELOPE_BYTES + 1))
    with pytest.raises(transport.Q38LinuxHostTransportError, match="not canonical"):
        transport.decode_status_envelope(encoded.replace(b"{", b"{ ", 1))


@pytest.mark.parametrize(
    "payload",
    [
        b"[" * 10_000 + b"0" + b"]" * 10_000 + b"\n",
        b'{"integer":' + b"9" * 5_000 + b"}\n",
    ],
)
def test_bounded_parser_complexity_errors_are_wrapped(payload: bytes) -> None:
    assert len(payload) <= transport.MAX_ENVELOPE_BYTES

    with pytest.raises(transport.Q38LinuxHostTransportError, match="transport JSON|canonical"):
        transport.decode_status_envelope(payload)


def test_status_cannot_predate_its_controller_context(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    resource = _worker_resource(plan)
    context = transport.build_instance_context(
        plan,
        resource.name,
        INSTANCE_ID,
        CREATED,
        issued_at_unix=NOW,
        expires_at_unix=NOW + 600,
        key=KEY,
    )
    envelope = transport.build_status_envelope(
        context,
        _worker_payload(plan, resource),
        plan,
        key=KEY,
        boot_id=BOOT_ID,
        revision=1,
        published_at_unix=NOW,
        prepared_record_digest=PREPARED_DIGEST,
    )
    envelope["published_at_unix"] = NOW - 1
    envelope["envelope_hmac"] = transport._mac(
        b"gateq38-host-status-v1",
        transport._envelope_unsigned(envelope),
        KEY,
    )

    with pytest.raises(transport.Q38LinuxHostTransportError, match="publication"):
        _validate(envelope, plan, resource)


@pytest.mark.parametrize("key", [b"", b"x" * 31, b"x" * 33, "x" * 32])
def test_transport_key_is_exactly_32_bytes(tmp_path: Path, key: object) -> None:
    plan = _plan(tmp_path)
    resource = _worker_resource(plan)

    with pytest.raises(transport.Q38LinuxHostTransportError, match="key"):
        _context(plan, resource, key=key)


def test_instance_context_transport_is_canonical_and_bounded(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    context = _context(plan, _worker_resource(plan))

    encoded = transport.encode_instance_context(context)
    assert transport.decode_instance_context(encoded) == context

    with pytest.raises(transport.Q38LinuxHostTransportError, match="canonical"):
        transport.decode_instance_context(b" " + encoded)
    with pytest.raises(transport.Q38LinuxHostTransportError, match="framing"):
        transport.decode_instance_context(encoded + b"\n")


def test_initial_status_payload_matches_controller_absence_rules(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    worker = _worker_resource(plan)
    bootstrap = _bootstrap_resource(plan)

    worker_payload = transport.initial_status_payload(_context(plan, worker), plan)
    assert worker_payload["state"] == "starting"
    assert worker_payload["peer_id"] is None

    bootstrap_payload = transport.initial_status_payload(_context(plan, bootstrap), plan)
    assert bootstrap_payload == {field: ("absent" if field == "state" else None) for field in route._ROUTE_JOB_FIELDS}
    envelope = transport.build_status_envelope(
        _context(plan, bootstrap),
        bootstrap_payload,
        plan,
        key=KEY,
        boot_id=BOOT_ID,
        revision=1,
        published_at_unix=NOW,
        prepared_record_digest=PREPARED_DIGEST,
    )
    assert _validate(envelope, plan, bootstrap)["payload"] == bootstrap_payload


@pytest.mark.parametrize("state", ["starting", "failed"])
def test_unfinished_worker_cannot_expose_peer(tmp_path: Path, state: str) -> None:
    plan = _plan(tmp_path)
    worker = _worker_resource(plan)
    payload = _worker_payload(plan, worker, state)
    payload["peer_id"] = "Qm" + "a" * 44

    with pytest.raises(transport.Q38LinuxHostTransportError, match="unfinished worker"):
        transport.build_status_envelope(
            _context(plan, worker),
            payload,
            plan,
            key=KEY,
            boot_id=BOOT_ID,
            revision=1,
            published_at_unix=NOW,
            prepared_record_digest=PREPARED_DIGEST,
        )


def _delivery_material(
    plan: route.RoutePlan,
    *,
    key: bytes = KEY,
    epoch: int = 1,
    previous_record_digest: str | None = None,
) -> route.InstanceGenerationKey:
    resource = _worker_resource(plan)
    record = route._instance_key_record(
        plan,
        resource,
        INSTANCE_ID,
        CREATED,
        key=key,
        key_epoch=epoch,
        issued_at_unix=NOW - 10,
        previous_record_digest=previous_record_digest,
    )
    return route.InstanceGenerationKey(record, key)


def test_instance_delivery_round_trip_is_secret_free_outside_payload(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    material = _delivery_material(plan)

    delivery = transport.build_instance_delivery(plan, material, now_unix=NOW)
    record, context, key = transport.validate_instance_delivery(
        delivery,
        plan,
        now_unix=NOW,
    )

    assert record == dict(delivery.record)
    assert context["resource_name"] == _worker_resource(plan).name
    assert key == KEY
    public = json.dumps(dict(delivery.record), sort_keys=True).encode()
    assert KEY not in public
    assert KEY.hex().encode() not in public
    assert "payload=" not in repr(delivery)


def test_instance_delivery_rejects_mutation_and_wrong_generation(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    delivery = transport.build_instance_delivery(
        plan,
        _delivery_material(plan),
        now_unix=NOW,
    )
    mutated = bytearray(delivery.payload)
    mutated[-1] ^= 1
    forged = transport.InstanceDelivery(delivery.record, bytes(mutated))

    with pytest.raises(transport.Q38LinuxHostTransportError):
        transport.validate_instance_delivery(forged, plan, now_unix=NOW)
    with pytest.raises(transport.Q38LinuxHostTransportError, match="generation"):
        transport.validate_instance_delivery(
            delivery,
            plan,
            now_unix=NOW,
            expected_generation_digest="sha256:" + "0" * 64,
        )


def test_instance_delivery_rotation_binds_predecessor(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    first = _delivery_material(plan)
    rotated = _delivery_material(
        plan,
        key=OTHER_KEY,
        epoch=2,
        previous_record_digest=first.record["record_digest"],
    )

    delivery = transport.build_instance_delivery(plan, rotated, now_unix=NOW)
    record, _context_value, key = transport.validate_instance_delivery(
        delivery,
        plan,
        now_unix=NOW,
    )

    assert record["key_epoch"] == 2
    assert record["previous_key_record_digest"] == first.record["record_digest"]
    assert key == OTHER_KEY


def test_instance_delivery_rejects_noncanonical_or_trailing_framing(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    delivery = transport.build_instance_delivery(
        plan,
        _delivery_material(plan),
        now_unix=NOW,
    )
    magic_size = len(transport.DELIVERY_MAGIC)
    header_end = delivery.payload.find(b"\n", magic_size)
    header = json.loads(delivery.payload[magic_size:header_end])
    noncanonical = (
        transport.DELIVERY_MAGIC
        + json.dumps(header, indent=2).encode("ascii")
        + b"\n"
        + delivery.payload[header_end + 1 :]
    )

    with pytest.raises(transport.Q38LinuxHostTransportError, match="header"):
        transport.decode_instance_delivery(noncanonical, plan, now_unix=NOW)
    with pytest.raises(transport.Q38LinuxHostTransportError, match="framing"):
        transport.decode_instance_delivery(delivery.payload + b"x", plan, now_unix=NOW)


def test_instance_delivery_receipt_is_authenticated_and_secret_free(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    delivery = transport.build_instance_delivery(
        plan,
        _delivery_material(plan),
        now_unix=NOW,
    )
    receipt = transport.build_instance_delivery_receipt(
        delivery,
        plan,
        installed_at_unix=NOW,
    )

    assert (
        transport.validate_instance_delivery_receipt(
            receipt,
            delivery,
            plan,
            now_unix=NOW,
        )
        == receipt
    )
    public = json.dumps(receipt, sort_keys=True).encode()
    assert KEY not in public
    assert KEY.hex().encode() not in public

    changed = copy.deepcopy(receipt)
    changed["key_epoch"] += 1
    with pytest.raises(transport.Q38LinuxHostTransportError):
        transport.validate_instance_delivery_receipt(
            changed,
            delivery,
            plan,
            now_unix=NOW,
        )


def test_instance_delivery_receipt_rejects_authenticated_stale_replay(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    delivery = transport.build_instance_delivery(
        plan,
        _delivery_material(plan),
        now_unix=NOW,
    )
    receipt = transport.build_instance_delivery_receipt(
        delivery,
        plan,
        installed_at_unix=NOW,
    )

    with pytest.raises(transport.Q38LinuxHostTransportError, match="receipt is stale"):
        transport.validate_instance_delivery_receipt(
            receipt,
            delivery,
            plan,
            now_unix=NOW + 900,
        )


@pytest.mark.parametrize(
    "installed_at_unix",
    [
        NOW - transport.MAX_DELIVERY_RECEIPT_AGE_SECONDS - 1,
        NOW + transport.MAX_FUTURE_SKEW_SECONDS + 1,
    ],
)
def test_instance_delivery_receipt_authenticates_before_time_semantics(
    tmp_path: Path,
    installed_at_unix: int,
) -> None:
    plan = _plan(tmp_path)
    delivery = transport.build_instance_delivery(
        plan,
        _delivery_material(plan),
        now_unix=NOW,
    )
    receipt = transport.build_instance_delivery_receipt(
        delivery,
        plan,
        installed_at_unix=NOW,
    )
    receipt["installed_at_unix"] = installed_at_unix

    with pytest.raises(transport.Q38LinuxHostTransportError, match="receipt digest changed"):
        transport.validate_instance_delivery_receipt(
            receipt,
            delivery,
            plan,
            now_unix=NOW,
        )

    receipt["receipt_digest"] = transport._receipt_digest_value(receipt)
    with pytest.raises(transport.Q38LinuxHostTransportError, match="receipt authentication failed"):
        transport.validate_instance_delivery_receipt(
            receipt,
            delivery,
            plan,
            now_unix=NOW,
        )
