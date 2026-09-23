"""Fixed-profile same-boot service replacement coordinator.

No public reset, path selection, credential operation or reboot adoption lives
here. All filesystem mutation is serialized by the original durable locks.
Installed manager/session and physical power-loss qualification is separate.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import signal
import stat
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from uuid import uuid4

from communityai_anchor import (
    linux_anchor as anchor,
    linux_anchor_replacement_layout as layouts,
    linux_anchor_state as state,
    worker_loading as private,
)
from communityai_anchor.linux_anchor_endpoint import EndpointFence, validate_endpoint
from communityai_anchor.linux_anchor_transaction import RecoveryTransaction
from communityai_anchor.resource_recovery import RecoverableStateError, current_recovery_identity
from drift.node import linux_anchor_bootstrap as bootstrap, linux_anchor_resources as resources
from drift.node.linux_anchor_entry import bootstrap_catalog_binding, catalog_discriminator
from drift.node.linux_anchor_node import AnchorNode
from drift.node.resource_reservations import ResourceReservationManager

_FILES = ("bootstrap.json", "resources.json", "state.json")


def _require(condition, category):
    if not condition:
        raise RecoverableStateError(category)


def _digest(value):
    return hashlib.sha256(bootstrap._json(value)).hexdigest()


def _read_record(root, name):
    anchor._require(name in {*_FILES, "retirement.json", "endpoint.json"})
    path = root / "anchor" / name
    before = private._fingerprint(private._stat(path))
    value = private._read(path)
    anchor._require(private._fingerprint(private._stat(path)) == before)
    return value, dict(fingerprint=list(before), digest=_digest(value))


def _service_now():
    try:
        service = anchor.inspect_service(starting=True)
    except RecoverableStateError:
        service = anchor.inspect_service()
    anchor._require(service.pid == os.getpid())
    return service


def _dead(service):
    """Unreadable identity is not death. Never signal a numeric PID here."""
    try:
        ticks, _group = anchor._process(service["pid"])
    except FileNotFoundError:
        anchor._require(not os.path.lexists(f"/proc/{service['pid']}"))
        return
    _require(ticks != service["start_ticks"], "active_owner")


def _parents(plan):
    return {
        "anchor",
        "node",
        "node/catalogs",
        "node/manifests",
        "node/catalogs/" + plan.bootstrap.trust_root.catalog_id,
    }


def _validate_bootstrap(root, value, plan, profile, binding):
    names = ("node/.catalog-bootstrap.lock", "node/.node-config.json.write.lock")
    anchor._require(profile.config_path == root / "node" / "node-config.json")
    bootstrap.validate_bootstrap_record(
        value,
        plan=plan,
        binding=binding,
        service=profile.credential_service,
        account=profile.credential_account,
        parents=_parents(plan),
        lock_names=names,
    )
    for name, identity in value["directories"].items():
        private._directory(root / name)
        anchor._require(list(private._identity(private._stat(root / name, directory=True))) == identity)
    for name, identity in value["locks"].items():
        anchor._require(list(anchor._lock_identity((root / name).lstat())) == identity)
    anchor._require(private._read(root / names[0]) == catalog_discriminator(root, bootstrap_catalog_binding(value)))


@contextmanager
def _writers(root, value, guard):
    import fcntl

    descriptors = []

    def validate():
        guard()
        for name, fd in descriptors:
            expected = value["locks"][name]
            anchor._require(list(anchor._lock_identity(os.fstat(fd))) == expected)
            anchor._require(list(anchor._lock_identity((root / name).lstat())) == expected)

    try:
        for name in ("node/.catalog-bootstrap.lock", "node/.node-config.json.write.lock"):
            guard()
            fd = os.open(root / name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            descriptors.append((name, fd))
            anchor._require(list(anchor._lock_identity(os.fstat(fd))) == value["locks"][name])
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RecoverableStateError("active_owner") from None
            validate()
        yield validate
        validate()
    finally:
        for _name, fd in reversed(descriptors):
            os.close(fd)


def _kill_old_trees(profiles, guard, cancelled):
    errors = []
    for profile in profiles[2:]:
        descriptor = None
        try:
            guard()
            descriptor = anchor.cg._open_root(profile.root)
            anchor._require(anchor.cg._observe_root(profile.root, descriptor) == profile)
            control = anchor.cg._control(descriptor, "cgroup.kill", write=True)
            try:
                anchor._require(os.write(control, b"1\n") == 2)
            finally:
                os.close(control)
            deadline = time.monotonic() + 5
            while anchor.cg._events(anchor.cg._read_control(descriptor, "cgroup.events")):
                _require(time.monotonic() < deadline, "cleanup_pending")
                time.sleep(0.025)
            anchor._require(anchor.cg._observe_root(profile.root, descriptor) == profile)
            guard()
        except Exception as error:
            errors.append(error)
        finally:
            if descriptor is not None:
                os.close(descriptor)
    # Neither a failed journal stage nor one root failure skips the other root.
    _require(not errors and not cancelled(), "cleanup_pending")
    for profile in profiles[2:]:
        anchor._require(anchor.cg.verify_cgroup_tree_empty(profile.root) == profile)


def _receipt(root, binding, fence):
    records = {name: _read_record(root, name)[1] for name in _FILES}
    anchor._require(fence.value["phase"] == "bound" and fence.value["binding"] == _digest(binding))
    retired = dict(fence.value, phase="retired", socket=None)
    return dict(
        schema_version=1,
        binding=_digest(binding),
        records=records,
        endpoint_source=copy.deepcopy(fence.value),
        endpoint=_digest(retired),
    )


def _record_evidence(value):
    anchor._require(type(value) is dict and set(value) == {"fingerprint", "digest"})
    anchor._require(type(value["digest"]) is str and anchor.re.fullmatch("[0-9a-f]{64}", value["digest"]) is not None)
    fingerprint = value["fingerprint"]
    anchor._require(type(fingerprint) is list and len(fingerprint) == 7)
    anchor._require(all(type(n) is int and 0 <= n < 2**64 for n in fingerprint))
    anchor._require(fingerprint[1] > 0 and fingerprint[6] == 1 and stat.S_ISREG(fingerprint[2]))
    return value


def _make_quiescence(root, value, fence):
    """Caller holds fresh node/worker/journal emptiness and all writer guards."""
    binding = _digest(value["binding"])
    fence.validate()
    anchor._require(fence.value["phase"] == "bound" and fence.value["binding"] == binding)
    anchor._require(list(anchor._socket_identity(fence.lease.directory / "control.sock")) == fence.value["socket"])
    return dict(
        epoch=uuid4().hex,
        binding=binding,
        generation=_digest(value["generation"]),
        records={name: _read_record(root, name)[1] for name in ("bootstrap.json", "resources.json", "endpoint.json")},
    )


def _check_quiescence(root, value, fence):
    state.validate_state(value)
    proof = value.get("quiescence")
    anchor._require(proof is not None and value["phase"] == "idle")
    current = _make_quiescence(root, value, fence)
    current["epoch"] = proof["epoch"]
    anchor._require(current == proof)
    return True


def _ledger_value(record):
    return json.loads(base64.b64decode(record["raw_b64"], validate=True))


def _ledger_sources(snapshot):
    return {
        name: dict(
            fingerprint=list(snapshot["files"][name]["source"]["fingerprint"]),
            digest=_digest(_ledger_value(snapshot["files"][name]["source"])),
        )
        for name in _FILES
    }


def _check_no_spawn(snapshot, context):
    marker = context["no_spawn"]
    anchor._require(marker is not None and marker["attempt"] == _digest(context["attempt"]))
    anchor._require(marker["records"] == _ledger_sources(snapshot))
    target = marker["binding"]
    for name in _FILES:
        record = snapshot["files"][name]
        if target is None:
            anchor._require(record["prepared"] is None and record["published_fingerprint"] is None)
        elif record["prepared"] is not None:
            value = _ledger_value(record["prepared"])
            observed = value["binding"] if name == "bootstrap.json" else _digest(value["binding"])
            anchor._require(observed == target)
            if name != "bootstrap.json":
                anchor._require(value["binding"]["service"] == context["attempt"]["service"])
                anchor._require(value["binding"]["layout"][0] == context["attempt"]["root"])
    return True


def _validate_receipt(value):
    anchor._require(
        type(value) is dict and set(value) == {"schema_version", "binding", "records", "endpoint_source", "endpoint"}
    )
    anchor._require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    for digest in (value["binding"], value["endpoint"]):
        anchor._require(type(digest) is str and anchor.re.fullmatch("[0-9a-f]{64}", digest) is not None)
    source = validate_endpoint(value["endpoint_source"])
    anchor._require(source["phase"] == "bound" and source["binding"] == value["binding"])
    anchor._require(value["endpoint"] == _digest(dict(source, phase="retired", socket=None)))
    anchor._require(type(value["records"]) is dict and set(value["records"]) == set(_FILES))
    for record in value["records"].values():
        _record_evidence(record)
    private._encode(value)
    return value


def _check_receipt(root, binding, fence, receipt):
    _validate_receipt(receipt)
    fence.validate()
    anchor._require(receipt["binding"] == _digest(binding))
    anchor._require(receipt["records"] == {name: _read_record(root, name)[1] for name in _FILES})
    source = receipt["endpoint_source"]
    allowed = (source, dict(source, phase="clearing"), dict(source, phase="retired", socket=None))
    anchor._require(fence.value in allowed)


def _seal_retirement(controller, channel, fence, plan, profile, lease):
    """Called only after quiescence inside the controller's final empty guard."""
    controller._clean_proof()
    controller._state.validate()
    bound = controller._state.binding
    root = profile.root
    value, _ = _read_record(root, "bootstrap.json")

    def guard():
        lease.validate()
        controller._clean_proof()

    with _writers(root, value, guard):
        _validate_bootstrap(root, value, plan, profile, _digest(bound))
        path = root / "anchor" / "retirement.json"
        if os.path.lexists(path):
            receipt = private._read(path)
            _check_receipt(root, bound, fence, receipt)
        else:
            fence.require_bound(_digest(bound), channel)
            if controller._state.value.get("quiescence") is not None:
                _check_quiescence(root, controller._state.value, fence)
            receipt = _validate_receipt(_receipt(root, bound, fence))
            private._exclusive(path, receipt)
        # The clean seal precedes endpoint removal, so manager pruning after
        # any later crash cannot erase the only evidence of checked retirement.
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            anchor._require(private._fingerprint(os.fstat(descriptor)) == private._fingerprint(private._stat(path)))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        state._sync_directory(path.parent, tuple(bound["storage"]["directory"]))
        anchor._require(private._read(path) == receipt)
        _check_receipt(root, bound, fence, receipt)
        guard()
        fence.clear(channel=channel)
        _check_receipt(root, bound, fence, receipt)
        guard()


def _binding(value):
    state.validate_state(
        dict(
            schema_version=1,
            profile=anchor.PROFILE,
            binding=value,
            revision=0,
            phase="checking",
            generation=None,
            request_id=None,
            operation=None,
        )
    )
    return value


def _context(value):
    anchor._require(
        type(value) is dict
        and set(value)
        == {
            "schema_version",
            "origin",
            "mode",
            "attempt",
            "children",
            "pending_child",
            "migrated",
            "receipt",
            "receipt_identity",
            "receipt_consuming",
            "endpoint",
            "endpoint_target",
            "quiescent_source",
            "no_spawn",
        }
    )
    anchor._require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    anchor._require(type(value["mode"]) is str and value["mode"] in {"retained", "retired"})
    anchor._require(type(value["migrated"]) is bool and type(value["receipt_consuming"]) is bool)
    anchor._require(type(value["quiescent_source"]) is bool)
    origin = _binding(value["origin"])
    # Reuse the complete strict state binding codec without granting activation.
    anchor._require(origin["machine"] == current_recovery_identity().to_json())
    attempt = value["attempt"]
    anchor._require(type(attempt) is dict and set(attempt) == {"service", "root"})
    service = attempt["service"]
    _binding(dict(origin, service=service))
    anchor._require(
        service["uid"] == origin["service"]["uid"] and service["control_group"] == origin["service"]["control_group"]
    )
    root = anchor.cg.LinuxCgroupProfile.from_json(attempt["root"])
    original_root = origin["layout"][0]
    expected_root = dict(original_root, root_identity=[original_root["root_identity"][0], root.root_identity[1]])
    # Manager recreation changes only the inode, never the cgroup filesystem,
    # mounted view, namespaces, owner or fixed path. No-spawn is not migration
    # authority across an unrelated hierarchy with the same spelling.
    anchor._require(root.to_json() == expected_root)
    anchor._require(value["mode"] != "retained" or root.to_json() == original_root)
    anchor._require(type(value["children"]) is list and len(value["children"]) <= 3)
    identities = {root.root_identity}
    for index, child in enumerate(value["children"]):
        parsed = anchor.cg.LinuxCgroupProfile.from_json(child)
        expected = dict(
            root.to_json(), root=root.root + "/" + anchor._CHILDREN[index], root_identity=child["root_identity"]
        )
        anchor._require(parsed.to_json() == expected and parsed.root_identity not in identities)
        identities.add(parsed.root_identity)
    pending = value["pending_child"]
    anchor._require(
        pending is None
        or (type(pending) is str and len(value["children"]) < 3 and pending == anchor._CHILDREN[len(value["children"])])
    )
    validate_endpoint(value["endpoint"])
    target = value["endpoint_target"]
    anchor._require(target is None or (type(target) is str and anchor.re.fullmatch("[0-9a-f]{64}", target) is not None))
    if value["receipt"] is not None:
        _validate_receipt(value["receipt"])
        anchor._require(value["receipt"]["binding"] == _digest(origin))
        identity = value["receipt_identity"]
        # Reuse the exact record-fingerprint validator rather than equality
        # accepting bool/int aliases in persisted identities.
        _validate_receipt(dict(value["receipt"], records={name: identity for name in _FILES}))
        anchor._require(identity["digest"] == _digest(value["receipt"]))
    else:
        anchor._require(value["receipt_identity"] is None and not value["receipt_consuming"])
    marker = value["no_spawn"]
    if marker is not None:
        anchor._require(type(marker) is dict and set(marker) == {"attempt", "records", "binding"})
        anchor._require(marker["attempt"] == _digest(attempt))
        target_binding = marker["binding"]
        anchor._require(
            target_binding is None
            or (type(target_binding) is str and anchor.re.fullmatch("[0-9a-f]{64}", target_binding) is not None)
        )
        anchor._require(type(marker["records"]) is dict and set(marker["records"]) == set(_FILES))
        for evidence in marker["records"].values():
            _record_evidence(evidence)
    anchor._require(target is None or (marker is not None and marker["binding"] == target))
    anchor._require(
        value["mode"] != "retired" or value["receipt"] is not None or value["quiescent_source"] or marker is not None
    )
    anchor._require(value["mode"] != "retained" or (not value["children"] and pending is None))
    private._encode(value)
    return value


class FixedAnchorSession:
    """Internal service lifecycle; the launcher supplies only fixed components."""

    def __init__(self, profile, plan, launch_factory, credential_executor, *, cancelled):
        self.profile, self.plan, self.launch_factory = profile, plan, launch_factory
        self.credential_executor = credential_executor
        self.cancelled = cancelled
        self.root = Path(profile.root)
        self.lease = self.layout = self.controller = self.channel = self.fence = None
        self.state_lease = self.lifetime = self.manager = self.journal = None
        self.context = self.origin = self.service = None
        self.short_guard = self.writer_guard = None
        self.transferred = False
        self.prior_attempt = None

    def _guard(self):
        self.lease.validate()
        anchor._require(_service_now() == self.service)
        if self.state_lease is not None:
            self.state_lease.validate()
        if self.lifetime is not None:
            self.lifetime.validate()
        if self.origin is not None:
            binding = self.origin
            anchor._require(binding["machine"] == current_recovery_identity().to_json())
            anchor._require(
                list(private._identity(private._stat(self.root, directory=True))) == binding["storage"]["profile"]
            )
            anchor._require(
                list(private._identity(private._stat(self.root / "anchor", directory=True)))
                == binding["storage"]["directory"]
            )
            anchor._require(
                list(anchor._lock_identity((self.root / "anchor-state.lock").lstat())) == binding["storage"]["lease"]
            )

    def _transaction_guard(self):
        self._held_guard()
        _require(not self.cancelled(), "cleanup_pending")

    def _held_guard(self):
        self._guard()
        anchor._require(self.short_guard is not None and self.writer_guard is not None)
        self.short_guard.validate(self.manager)
        self.writer_guard()
        self.fence.validate()

    def _progress(self, **changes):
        proposed = _context(dict(self.context, **copy.deepcopy(changes)))
        self.journal.update_context(proposed)
        self.context = proposed

    def _retire(self, controller):
        anchor._require(self.channel is not None and self.fence is not None)
        _seal_retirement(controller, self.channel, self.fence, self.plan, self.profile, self.lease)

    def _quiesce(self, controller, *, validate_only=False, request_id=None):
        """Atomic idle proof, called inside the owner's fresh admission guard."""
        anchor._require(type(validate_only) is bool and self.channel is not None)
        controller._clean_proof()
        value, _ = _read_record(self.root, "bootstrap.json")

        def guard():
            self.lease.validate()
            controller._clean_proof()

        held = nullcontext(self.writer_guard) if self.writer_guard is not None else _writers(self.root, value, guard)
        with held as writer_guard:
            writer_guard()
            _validate_bootstrap(self.root, value, self.plan, self.profile, _digest(controller._state.binding))
            self.fence.require_bound(_digest(controller._state.binding), self.channel)
            if controller._state.value.get("quiescence") is not None:
                _check_quiescence(self.root, controller._state.value, self.fence)
            if validate_only:
                anchor._require(request_id is None)
                _check_quiescence(self.root, controller._state.value, self.fence)
            else:
                proof = _make_quiescence(self.root, controller._state.value, self.fence)
                options = {} if request_id is None else dict(request_id=request_id, operation="drain")
                controller._state.write_quiescent(controller._state.value["revision"], proof, **options)
                _check_quiescence(self.root, controller._state.value, self.fence)
            writer_guard()
            guard()
        return controller._state.value

    def initialize(self, initialize_profile):
        anchor._require(callable(initialize_profile) and not self.cancelled())
        self.layout = anchor.AnchorLayout()
        self.service = self.layout.service
        initialize_profile()
        from communityai_anchor.linux_anchor_credentials import CredentialIdentity

        preparation = bootstrap.AnchorBootstrap(
            self.plan,
            self.profile,
            CredentialIdentity(self.profile.credential_service, self.profile.credential_account),
        )
        self.controller = AnchorNode(
            self.layout,
            self.root,
            self.launch_factory,
            initialize=True,
            bootstrap=preparation,
            credential_executor=self.credential_executor,
            retirement_hook=self._retire,
            quiescence_hook=self._quiesce,
            activate=False,
        )
        self.origin = copy.deepcopy(self.controller._state.binding)
        self.state_lease = self.controller._state.lease
        self.lifetime = self.controller._lease
        self.transferred = True
        self.fence = EndpointFence.create(self.root, self.lease, _digest(self.origin), guard=self._guard)
        self.channel = anchor.AnchorChannel.bind_only(self.layout, self.controller, lease=self.lease)
        self.fence.record_bound(self.channel)
        _require(not self.cancelled(), "cleanup_pending")
        self.fence.require_bound(_digest(self.origin), self.channel)
        self.controller.activate_owner()
        self.channel.begin_serving()

    def _load_resources(self, expected_storage=None):
        value, _ = _read_record(self.root, "resources.json")
        anchor._require(type(value) is dict and set(value) == {"version", "binding", "identities"})
        anchor._require(type(value["version"]) is int and value["version"] == 1)
        _binding(value["binding"])
        identities = value["identities"]
        anchor._require(type(identities) is dict and set(identities) == {"node", "journal", "lifetime", "admission"})
        for identity in identities.values():
            anchor._require(type(identity) is list and len(identity) == 2)
            anchor._require(all(type(n) is int and 0 <= n < 2**64 for n in identity) and identity[1] > 0)
        anchor._require(value["identities"] == resources.identities(self.root))
        if expected_storage is not None:
            anchor._require(value["binding"]["storage"] == expected_storage)
        anchor._require(value["identities"]["lifetime"] == list(self.lifetime.identity))
        return value

    def _new_context(self, old, boot_record):
        origin = old["binding"]
        anchor._require(origin["machine"] == current_recovery_identity().to_json())
        anchor._require(self.service.uid == origin["service"]["uid"])
        anchor._require(self.service.control_group == origin["service"]["control_group"])
        _dead(origin["service"])
        self.origin = copy.deepcopy(origin)
        self._guard()
        anchor._require(self.fence.value["binding"] == _digest(origin))
        resource_record = self._load_resources(origin["storage"])
        anchor._require(resource_record["binding"] == origin)
        _validate_bootstrap(self.root, boot_record, self.plan, self.profile, _digest(origin))
        root_profile = anchor.cg.observe_cgroup_profile(anchor._delegated_path(self.service), require_unfrozen=False)
        receipt = receipt_identity = None
        if os.path.lexists(self.root / "anchor" / "retirement.json"):
            receipt, receipt_identity = _read_record(self.root, "retirement.json")
            _check_receipt(self.root, origin, self.fence, receipt)
        sealed = False
        if receipt is None and old.get("quiescence") is not None:
            _check_quiescence(self.root, old, self.fence)
            sealed = True
        mode = "retained" if root_profile.to_json() == origin["layout"][0] else "retired"
        anchor._require(mode == "retained" or receipt is not None or sealed)
        return _context(
            dict(
                schema_version=1,
                origin=copy.deepcopy(origin),
                mode=mode,
                attempt=dict(service=self.service.to_json(), root=root_profile.to_json()),
                children=[],
                pending_child=None,
                migrated=False,
                receipt=receipt,
                receipt_identity=receipt_identity,
                receipt_consuming=False,
                endpoint=copy.deepcopy(self.fence.value),
                endpoint_target=None,
                quiescent_source=sealed,
                no_spawn=None,
            )
        )

    def _resume_context(self, snapshot):
        context = _context(copy.deepcopy(snapshot["context"]))
        self.origin = context["origin"]
        self._guard()
        self.prior_attempt = copy.deepcopy(context["attempt"]["service"])
        _dead(context["attempt"]["service"])
        _dead(context["origin"]["service"])
        anchor._require(self.service.control_group == context["origin"]["service"]["control_group"])
        anchor._require(self.service.uid == context["origin"]["service"]["uid"])
        self._endpoint_replay(context)
        clean_attempt = context["no_spawn"] is not None
        if clean_attempt:
            _check_no_spawn(snapshot, context)
        current_root = anchor.cg.observe_cgroup_profile(anchor._delegated_path(self.service), require_unfrozen=False)
        if current_root.to_json() != context["attempt"]["root"]:
            # A live journal fences owner activation. Its exact no-spawn proof
            # survives file publication and retargeting; a vanished root alone
            # is never authority to recreate a layout.
            if not clean_attempt:
                if context["receipt"] is not None and not context["receipt_consuming"]:
                    receipt, identity = _read_record(self.root, "retirement.json")
                    anchor._require(receipt == context["receipt"] and identity == context["receipt_identity"])
                else:
                    anchor._require(context["quiescent_source"])
                    _check_quiescence(self.root, _read_record(self.root, "state.json")[0], self.fence)
            fd = anchor.cg._open_root(current_root.root)
            try:
                anchor._require(not anchor._subgroups(fd))
            finally:
                os.close(fd)
            context.update(mode="retired", children=[], pending_child=None)
        elif clean_attempt:
            self._empty_attempt(context, current_root)
        context.update(
            attempt=dict(service=self.service.to_json(), root=current_root.to_json()),
            migrated=False,
            endpoint=copy.deepcopy(self.fence.value),
            endpoint_target=None,
        )
        if clean_attempt:
            # Retarget's source capture/CAS checks these exact observations.
            # Refreshing ancestry in that same CAS closes the next crash gap.
            context["no_spawn"] = dict(
                attempt=_digest(context["attempt"]),
                records={name: _read_record(self.root, name)[1] for name in _FILES},
                binding=None,
            )
        return _context(context)

    def _empty_attempt(self, context, root):
        descriptor = anchor.cg._open_root(root.root)
        try:
            anchor._require(anchor.cg._observe_root(root.root, descriptor) == root)
            children = anchor._subgroups(descriptor)
            expected = context["origin"]["layout"][1:] if context["mode"] == "retained" else context["children"]
            recorded = {anchor._CHILDREN[index]: item for index, item in enumerate(expected)}
            allowed = set(recorded)
            if context["pending_child"] is not None:
                allowed.add(context["pending_child"])
            anchor._require(children <= allowed and set(recorded) <= children)
            for name in children:
                observed = anchor.cg.verify_cgroup_tree_empty(root.root + "/" + name)
                if name in recorded:
                    anchor._require(observed.to_json() == recorded[name])
            anchor._require(anchor.cg._observe_root(root.root, descriptor) == root)
        finally:
            os.close(descriptor)

    def _endpoint_replay(self, context):
        """Only the fixed endpoint transition authorized before its first effect."""
        self.fence.validate()
        observed, source = self.fence.value, context["endpoint"]
        if observed == source:
            return
        target = context["endpoint_target"]
        anchor._require(target is not None)
        anchor._require(observed["directory"] == source["directory"] and observed["lease"] == source["lease"])
        if observed["binding"] == source["binding"]:
            anchor._require(observed["nonce"] == source["nonce"])
            anchor._require(observed["phase"] in {"clearing", "retired"})
            if observed["phase"] == "clearing" and source["phase"] != "pending":
                anchor._require(observed["socket"] == source["socket"])
        else:
            anchor._require(observed["binding"] == target and observed["phase"] in {"pending", "bound"})
            # Prefix publication precedes endpoint changes; state is still the
            # exact source until the endpoint's inode is embedded in the seal.
            current = _read_record(self.root, "resources.json")[0]
            anchor._require(_digest(current["binding"]) == target)
            anchor._require(current["binding"]["service"] == context["attempt"]["service"])

    def _layout(self):
        origin = self.context["origin"]
        expected = tuple(anchor.cg.LinuxCgroupProfile.from_json(item) for item in origin["layout"])
        if self.context["mode"] == "retained":
            self.layout = layouts.prepare_retained_layout(expected, cancelled=self.cancelled)
            _kill_old_trees(expected, self._held_guard, self.cancelled)
            anchor._require(self.short_guard.recover())
            self.short_guard.require_empty()
        else:
            self.short_guard.require_empty_journal()

            def begin_child(name, root_profile, recorded):
                anchor._require(root_profile.to_json() == self.context["attempt"]["root"])
                encoded = [p.to_json() for p in recorded]
                anchor._require(encoded == self.context["children"])
                self._progress(pending_child=name)

            def record_child(name, profile):
                anchor._require(name == self.context["pending_child"])
                self._progress(children=[*self.context["children"], profile.to_json()], pending_child=None)

            self.layout = layouts.prepare_manager_pruned_layout(
                recorded_children=tuple(anchor.cg.LinuxCgroupProfile.from_json(p) for p in self.context["children"]),
                pending_name=self.context["pending_child"],
                begin_child=begin_child,
                record_child=record_child,
                cancelled=self.cancelled,
            )
            self.short_guard.require_empty()

        def begin_migration(*_args):
            anchor._require(self.layout.service == self.service)
            self._progress(migrated=False)

        def record_migration(*_args):
            self._progress(migrated=True)

        self.layout.migrate_self(
            begin_migration=begin_migration, record_migration=record_migration, cancelled=self.cancelled
        )
        self.short_guard.require_empty()

    def _targets(self):
        current = state.validate_state(_read_record(self.root, "state.json")[0])
        origin = self.context["origin"]
        anchor._require(current["binding"]["storage"] == origin["storage"])
        target_binding = dict(
            service=self.service.to_json(),
            machine=current_recovery_identity().to_json(),
            layout=[p.to_json() for p in self.layout.profiles],
            storage=copy.deepcopy(origin["storage"]),
        )
        generation = copy.deepcopy(current["generation"])
        if self.context["mode"] == "retired":
            generation = None
        target_state = state.validate_state(
            dict(
                current,
                **({"quiescence": None} if current["schema_version"] == 2 else {}),
                binding=target_binding,
                revision=current["revision"] + 1,
                phase="idle",
                generation=generation,
            )
        )
        resource_record = self._load_resources(origin["storage"])
        target_resources = dict(resource_record, binding=target_binding)
        boot_record = _read_record(self.root, "bootstrap.json")[0]
        _validate_bootstrap(self.root, boot_record, self.plan, self.profile, boot_record["binding"])
        target_bootstrap = bootstrap.derive_rebound_bootstrap_record(
            boot_record,
            plan=self.plan,
            binding=boot_record["binding"],
            new_binding=_digest(target_binding),
            service=self.profile.credential_service,
            account=self.profile.credential_account,
            parents=_parents(self.plan),
            lock_names=("node/.catalog-bootstrap.lock", "node/.node-config.json.write.lock"),
        )
        return target_binding, {
            "bootstrap.json": bootstrap._json(target_bootstrap),
            "resources.json": bootstrap._json(target_resources),
            "state.json": bootstrap._json(target_state),
        }

    def _consume_receipt(self):
        if self.context["receipt"] is None:
            anchor._require(not os.path.lexists(self.root / "anchor" / "retirement.json"))
            return
        if not self.context["receipt_consuming"]:
            receipt, identity = _read_record(self.root, "retirement.json")
            anchor._require(receipt == self.context["receipt"] and identity == self.context["receipt_identity"])
            self._progress(receipt_consuming=True)
        path = self.root / "anchor" / "retirement.json"
        if os.path.lexists(path):
            receipt, identity = _read_record(self.root, "retirement.json")
            anchor._require(receipt == self.context["receipt"] and identity == self.context["receipt_identity"])
            os.unlink(path)
        state._sync_directory(path.parent, tuple(self.origin["storage"]["directory"]))
        self._transaction_guard()

    def recover(self):
        private._directory(self.root)
        self.service = anchor.inspect_service(starting=True)
        self.state_lease = state.PrivateLease(self.root, "anchor-state.lock", create=False)
        self.lifetime = state.node_lease(self.root, create=False)
        value, evidence = _read_record(self.root, "state.json")
        old = state.validate_state(value)
        from drift.node.linux_anchor_containment import StateContainment

        containment = None
        try:
            containment = StateContainment(
                self.root, self.service, self.lease, self.state_lease, self.lifetime, old, evidence
            )
        except RecoverableStateError:
            # A manager-pruned layout has no retained-root containment grant.
            # It still needs the separate exact retirement/quiescence proof.
            pass
        try:
            self._recover_owned(old)
        except BaseException:
            if containment is not None:
                try:
                    # Corrupt endpoint/resource/journal state withholds restart
                    # and clean acknowledgement, never independent Stop. Each
                    # root re-proves its own exact identity before the kill.
                    _kill_old_trees(containment.profiles, containment.validate, lambda: False)
                except Exception:
                    pass  # All roots were attempted; the original failure wins.
            raise

    def _recover_owned(self, old):
        self.fence = EndpointFence(self.root, self.lease, guard=self._guard)
        boot_record = _read_record(self.root, "bootstrap.json")[0]
        exists = os.path.lexists(self.root / "anchor" / "recovery.json")
        resource_record = self._load_resources()
        identities = resource_record["identities"]
        self.manager = ResourceReservationManager(
            self.root / "node" / "resource-reservations",
            loading_protocol=True,
            recovery_protocol=True,
            worker_cgroup_root=anchor._delegated_path(self.service) + "/workers",
            storage_binding=dict(directory=tuple(identities["journal"]), lease=tuple(identities["admission"])),
        )
        _require(not self.cancelled(), "cleanup_pending")
        # Once owned, containment attempts remain mandatory even if Stop
        # arrives between the two roots. Effect publication checks cancellation
        # independently at each durable transaction boundary.
        with self.manager.recovery_guard() as guard:
            self.short_guard = guard
            with _writers(self.root, boot_record, self._guard) as writers:
                self.writer_guard = writers
                if exists:
                    self.journal = RecoveryTransaction.open(self.root, guard=self._transaction_guard)
                    self.journal.reconcile()
                    self.context = self._resume_context(self.journal.snapshot())
                    self.journal.retarget(
                        context=self.context, prepared=None, authorize=lambda: self._retarget_authorized()
                    )
                else:
                    self.context = self._new_context(old, boot_record)
                    self.journal = RecoveryTransaction.begin(
                        self.root, context=self.context, prepared=None, guard=self._transaction_guard
                    )
                self._layout()
                binding, targets = self._targets()
                self._progress(
                    no_spawn=dict(
                        attempt=_digest(self.context["attempt"]),
                        records=_ledger_sources(self.journal.snapshot()),
                        binding=_digest(binding),
                    )
                )
                _check_no_spawn(self.journal.snapshot(), self.context)
                self.journal.set_prefix({name: targets[name] for name in _FILES[:2]})
                self.journal.publish_prefix()
                self._progress(endpoint_target=_digest(binding))
                self.fence.clear()
                self.fence.reserve(_digest(binding))
                self.channel = anchor.AnchorChannel.bind_only(self.layout, lease=self.lease)
                self.fence.record_bound(self.channel)
                target_state = json.loads(targets["state.json"])
                target_state.update(schema_version=2, quiescence=_make_quiescence(self.root, target_state, self.fence))
                state.validate_state(target_state)
                self.journal.set_final_state(bootstrap._json(target_state))
                self.journal.publish()
                _check_no_spawn(self.journal.snapshot(), self.context)
                _check_quiescence(self.root, target_state, self.fence)
                current_state = state.AnchorState(self.root, self.layout, held_lease=self.state_lease)
                from communityai_anchor.linux_anchor_credentials import CredentialIdentity

                preparation = bootstrap.AnchorBootstrap(
                    self.plan,
                    self.profile,
                    CredentialIdentity(self.profile.credential_service, self.profile.credential_account),
                )
                self.controller = AnchorNode.adopt_inactive(
                    self.layout,
                    self.root,
                    self.launch_factory,
                    state=current_state,
                    node_lifetime_lease=self.lifetime,
                    reservation_manager=self.manager,
                    reservation_guard=guard,
                    resources_record=resources.read_resources(self.root, binding),
                    bootstrap=preparation,
                    credential_executor=self.credential_executor,
                    retirement_hook=self._retire,
                    quiescence_hook=self._quiesce,
                )
                self.transferred = True
                self.channel.controller = self.controller
                self._consume_receipt()
                guard.require_empty()
                self.journal.consume(endpoint_fence=lambda: self._activation_fence(binding))
                self.writer_guard = None
            self.short_guard = None
        _require(not self.cancelled(), "cleanup_pending")
        self.controller.activate_owner()
        self.channel.begin_serving()

    def _activation_fence(self, binding):
        self.fence.require_bound(_digest(binding), self.channel)
        self.controller._state.validate()
        return _check_quiescence(self.root, self.controller._state.value, self.fence)

    def _retarget_authorized(self):
        self._transaction_guard()
        _dead(self.context["origin"]["service"])
        anchor._require(self.prior_attempt is not None)
        _dead(self.prior_attempt)
        return True

    def close(self):
        # A live owner or failed clean close retains *all* authority until the
        # service exits. In particular, never unlink a bound endpoint merely
        # because graceful retirement failed. The next owner needs its inode.
        if self.controller is not None and not self.controller.close(timeout=2.0):
            return False
        try:
            if self.channel is not None:
                self.channel.abandon()
            if self.journal is not None:
                self.journal.close()
            if not self.transferred:
                if self.manager is not None:
                    anchor._require(self.manager.close())
                if self.lifetime is not None:
                    self.lifetime.close()
                if self.state_lease is not None:
                    self.state_lease.close()
        finally:
            if self.layout is not None:
                self.layout.close()
            if self.lease is not None:
                self.lease.close()
        return True


def serve_fixed_anchor(
    profile, plan, launch_factory, credential_executor, *, initialize=False, initialize_profile=None
):
    """No caller-controlled command/path; only the fixed frozen launcher calls it."""
    stopping = False
    handlers = {}
    session = FixedAnchorSession(profile, plan, launch_factory, credential_executor, cancelled=lambda: stopping)

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    try:
        anchor._require(type(initialize) is bool)
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, stop)
        session.lease = anchor.AnchorChannelLease()
        if initialize:
            session.initialize(initialize_profile)
        else:
            session.recover()
        while True:
            if stopping:
                session.controller.request_shutdown()
                if session.controller.finished.is_set():
                    return 0 if session.controller.close() else 75
            session.fence.require_bound(_digest(session.controller._state.binding), session.channel)
            session.channel.serve_once()
    except Exception:
        raise RecoverableStateError() from None
    finally:
        try:
            session.close()
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
