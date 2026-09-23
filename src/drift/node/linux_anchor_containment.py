"""Read-only authority for mandatory anchor recovery containment.

This object never kills, creates, removes, adopts, or acknowledges anything.
The recovery coordinator may use :meth:`validate` as the basic guard while it
independently attempts the two exact state-bound containment roots.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat

from communityai_anchor import linux_anchor as anchor, linux_anchor_state as state, worker_loading as private
from communityai_anchor.resource_recovery import RecoverableStateError, current_recovery_identity


def _require(condition):
    if not condition:
        raise RecoverableStateError() from None


def _evidence(value):
    _require(type(value) is dict and set(value) == {"fingerprint", "digest"})
    fingerprint = value["fingerprint"]
    _require(
        type(fingerprint) is list
        and len(fingerprint) == 7
        and all(type(item) is int and 0 <= item < 2**64 for item in fingerprint)
        and fingerprint[1] > 0
        and stat.S_ISREG(fingerprint[2])
        and fingerprint[6] == 1
    )
    _require(type(value["digest"]) is str and re.fullmatch("[0-9a-f]{64}", value["digest"]) is not None)
    return copy.deepcopy(value)


def _canonical(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n"
    ).encode()


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _service_now():
    try:
        return anchor.inspect_service(starting=True)
    except RecoverableStateError:
        return anchor.inspect_service()


def _require_dead(value):
    try:
        ticks, _group = anchor._process(value["pid"])
    except FileNotFoundError:
        _require(not os.path.lexists(f"/proc/{value['pid']}"))
        return
    _require(ticks != value["start_ticks"])


class StateContainment:
    """Continuously revalidated, state-only authority; contains no effects."""

    def __init__(
        self,
        root,
        service,
        channel_lease,
        state_lease,
        lifetime_lease,
        value,
        evidence,
    ):
        try:
            _require(type(service) is anchor.ServiceIdentity)
            _require(type(channel_lease) is anchor.AnchorChannelLease)
            _require(type(state_lease) is state.PrivateLease and type(lifetime_lease) is state.PrivateLease)
            _require(state_lease.created is False and lifetime_lease.created is False)
            self.root = private._directory(root)
            _require(state_lease.root == self.root and lifetime_lease.root == self.root)
            _require(state_lease.path == self.root / "anchor-state.lock")
            _require(lifetime_lease.path == self.root / "node-lifetime.lock")
            self.service = service
            self.channel_lease = channel_lease
            self.state_lease = state_lease
            self.lifetime_lease = lifetime_lease
            self.value = copy.deepcopy(state.validate_state(copy.deepcopy(value)))
            self.evidence = _evidence(evidence)
            self.profiles = tuple(
                anchor.cg.LinuxCgroupProfile.from_json(item) for item in self.value["binding"]["layout"]
            )
            self.path = self.root / "anchor" / "state.json"
            self.validate()
        except RecoverableStateError:
            raise
        except Exception:
            raise RecoverableStateError() from None

    def _read_exact_state(self):
        before = private._stat(self.path)
        value = private._read(self.path)
        after = private._stat(self.path)
        fingerprint = list(private._fingerprint(after))
        _require(private._fingerprint(before) == private._fingerprint(after))
        _require(value == self.value)
        _require(dict(fingerprint=fingerprint, digest=_digest(value)) == self.evidence)

    def validate(self):
        try:
            self.channel_lease.validate()
            self.state_lease.validate()
            self.lifetime_lease.validate()
            _require(self.state_lease.created is False and self.lifetime_lease.created is False)
            _require(self.state_lease.root == self.root and self.lifetime_lease.root == self.root)
            _require(self.state_lease.path == self.root / "anchor-state.lock")
            _require(self.lifetime_lease.path == self.root / "node-lifetime.lock")
            binding = self.value["binding"]
            storage = binding["storage"]
            _require(list(private._identity(private._stat(self.root, directory=True))) == storage["profile"])
            directory = self.root / "anchor"
            _require(list(private._identity(private._stat(directory, directory=True))) == storage["directory"])
            _require(list(self.state_lease.identity) == storage["lease"])
            _require(current_recovery_identity().to_json() == binding["machine"])
            current = _service_now()
            _require(current == self.service and current.pid == os.getpid())
            previous = binding["service"]
            _require(current.uid == previous["uid"] and current.control_group == previous["control_group"])
            _require_dead(previous)
            root_profile = self.profiles[0]
            _require(anchor._delegated_path(current) == root_profile.root)
            _require(anchor.cg.observe_cgroup_profile(root_profile.root, require_unfrozen=False) == root_profile)
            self._read_exact_state()
            return True
        except RecoverableStateError:
            raise
        except Exception:
            raise RecoverableStateError() from None


__all__ = ["StateContainment"]
