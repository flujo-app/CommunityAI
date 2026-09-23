"""Bounded, read-only inventory of the fixed Linux profile, not recovery proof.

No locks, writes, keyring access, directory enumeration or paths from saved JSON.
Byte/count limits bound work, not kernel/filesystem latency. A changing live
owner can make a snapshot inconclusive; it must never authorize any operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

from drift.node import linux_anchor as anchor, worker_loading as private
from drift.node.linux_anchor_bootstrap import MAX_OUTPUTS, _json, validate_bootstrap_record
from drift.node.linux_anchor_entry import bootstrap_catalog_binding, catalog_discriminator
from drift.node.linux_anchor_state import validate_state
from drift.node.resource_recovery import current_recovery_identity

_FILES = {
    "state": "anchor/state.json",
    "resources": "anchor/resources.json",
    "bootstrap": "anchor/bootstrap.json",
    "state_lock": "anchor-state.lock",
    "lifetime_lock": "node-lifetime.lock",
    "admission_lock": "node/resource-reservations/admission.lock",
    "catalog_lock": "node/.catalog-bootstrap.lock",
    "config_lock": "node/.node-config.json.write.lock",
}


class _Snapshot:
    """Open every ancestor without following links, retain and recheck evidence."""

    def __init__(self, root):
        self.root = Path(root)
        self.directories = {}
        self.files = {}

    def close(self):
        for descriptor, _ in self.directories.values():
            os.close(descriptor)
        self.directories.clear()

    def directory(self, path):
        path = Path(path)
        anchor._require(path.is_absolute() and ".." not in path.parts and len(path.parts) <= 64)
        if path in self.directories:
            return self.directories[path][0]
        parent = None if path == path.parent else self.directory(path.parent)
        descriptor = os.open(
            str(path) if parent is None else path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent,
        )
        try:
            info = os.fstat(descriptor)
            if path == self.root or self.root in path.parents:
                anchor._require(info.st_uid == os.geteuid() and info.st_mode & 0o777 == 0o700)
            self.directories[path] = (descriptor, (info.st_dev, info.st_ino, info.st_mode, info.st_uid))
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def file(self, relative, *, decode=False):
        # Names come only from this module or the verified fixed package plan.
        path = self.root / relative
        anchor._require(not Path(relative).is_absolute() and ".." not in Path(relative).parts)
        parent = self.directory(path.parent)
        try:
            before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            self.files[path] = None
            raise
        anchor._require(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and before.st_uid == os.geteuid()
            and before.st_mode & 0o777 == 0o600
        )
        fingerprint = (*private._fingerprint(before), before.st_uid)
        self.files[path] = fingerprint
        if not decode:
            return before
        anchor._require(0 < before.st_size <= private._MAX_JSON)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            anchor._require((*private._fingerprint(os.fstat(descriptor)), os.fstat(descriptor).st_uid) == fingerprint)
            raw = bytearray()
            while len(raw) <= private._MAX_JSON:
                part = os.read(descriptor, min(4096, private._MAX_JSON + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
            anchor._require(len(raw) == before.st_size)
            anchor._require((*private._fingerprint(os.fstat(descriptor)), os.fstat(descriptor).st_uid) == fingerprint)
            value = json.loads(raw, object_pairs_hook=private._unique, parse_constant=lambda _: anchor._require(False))
            anchor._require(type(value) is dict)
            return value
        finally:
            os.close(descriptor)

    def identity(self, relative, *, directory=False):
        if directory:
            info = os.fstat(self.directory(self.root / relative))
        else:
            info = self.file(relative)
        return [info.st_dev, info.st_ino]

    def validate(self):
        # Reopen from / rather than trusting only still-open renamed ancestors.
        other = _Snapshot(self.root)
        try:
            for path, (_descriptor, identity) in self.directories.items():
                other.directory(path)
                anchor._require(other.directories[path][1] == identity)
            for path, fingerprint in self.files.items():
                parent = other.directory(path.parent)
                try:
                    info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    anchor._require(fingerprint is None)
                else:
                    anchor._require((*private._fingerprint(info), info.st_uid) == fingerprint)
        finally:
            other.close()


def diagnose_profile(profile, plan=None):
    """Return only fixed labels; plan=None means packaged input unavailable.

    The production launcher supplies the fixed current-user profile and verified
    package plan. No data from this report may be used as lifecycle authority."""
    report = dict(
        schema_version=1,
        profile=anchor.PROFILE,
        observation="inconclusive",
        admission=False,
        maintenance=False,
        cleanup_complete=False,
        recovery_allowed=False,
        reasons=[],
        inventory={},
        saved_phase="unknown",
        credential_intent="unknown",
        keyring="not_queried",
        output_contents="not_verified",
        package="unavailable",
    )
    reasons = report["reasons"]
    if not sys.platform.startswith("linux"):
        reasons.append("unsupported_platform")
        return report
    if plan is None:
        reasons.append("package_unavailable")
    else:
        anchor._require(len(plan.outputs) <= MAX_OUTPUTS)
    snapshot = _Snapshot(profile.root)
    values = {}
    try:
        try:
            snapshot.directory(profile.root)
        except FileNotFoundError:
            reasons.append("profile_or_ancestor_missing")
            return report
        except Exception:
            reasons.append("profile_unreadable_or_unsafe")
            return report
        for label, path in _FILES.items():
            try:
                value = snapshot.file(path, decode=label in {"state", "resources", "bootstrap", "catalog_lock"})
                report["inventory"][label] = "present"
                values[label] = value
            except FileNotFoundError:
                report["inventory"][label] = "missing"
                reasons.append(label + "_missing")
            except Exception:
                report["inventory"][label] = "unreadable_or_unsafe"
                reasons.append(label + "_unreadable_or_unsafe")
        state = values.get("state")
        try:
            validate_state(state)
        except Exception:
            state = None
            reasons.append("state_invalid_or_unavailable")
        if state is not None:
            report["saved_phase"] = state["phase"]
            binding = state["binding"]
            try:
                anchor._require(
                    binding["storage"]
                    == dict(
                        profile=snapshot.identity(".", directory=True),
                        directory=snapshot.identity("anchor", directory=True),
                        lease=snapshot.identity("anchor-state.lock"),
                    )
                )
            except Exception:
                reasons.append("state_storage_binding_unverifiable")
            try:
                if current_recovery_identity().to_json() != binding["machine"]:
                    reasons.append("machine_or_boot_changed")
            except Exception:
                reasons.append("machine_or_boot_unavailable")
            try:
                if anchor.inspect_service().to_json() != binding["service"]:
                    reasons.append("service_invocation_changed")
            except Exception:
                reasons.append("service_unavailable_or_unqualified")
            try:
                resources = values.get("resources")
                anchor._require(type(resources) is dict and set(resources) == {"version", "binding", "identities"})
                anchor._require(type(resources["version"]) is int and resources["version"] == 1)
                anchor._require(resources["binding"] == binding)
                anchor._require(
                    resources["identities"]
                    == dict(
                        node=snapshot.identity("node", directory=True),
                        journal=snapshot.identity("node/resource-reservations", directory=True),
                        lifetime=snapshot.identity(_FILES["lifetime_lock"]),
                        admission=snapshot.identity(_FILES["admission_lock"]),
                    )
                )
            except Exception:
                reasons.append("resource_binding_unverifiable")
            if plan is not None and "bootstrap" in values:
                _bootstrap_inventory(profile, plan, binding, values["bootstrap"], values, snapshot, report)
        try:
            snapshot.validate()
            report["observation"] = "inventory_observed"
        except Exception:
            reasons.append("snapshot_changed_or_unverifiable")
            report["saved_phase"] = report["credential_intent"] = "unknown"
            report["package"] = "unavailable"
        return report
    finally:
        snapshot.close()


def _bootstrap_inventory(profile, plan, binding, value, values, snapshot, report):
    reasons = report["reasons"]
    if value.get("bundle") != plan.bundle_digest or value.get("plan") != plan.digest:
        report["package"] = "different_or_invalid"
        reasons.append("package_binding_changed_or_invalid")
        return
    digest = hashlib.sha256(_json(binding)).hexdigest()
    parents = {
        "anchor",
        "node",
        "node/catalogs",
        "node/manifests",
        "node/catalogs/" + plan.bootstrap.trust_root.catalog_id,
    }
    locks = (_FILES["catalog_lock"], _FILES["config_lock"])
    try:
        validate_bootstrap_record(
            value,
            plan=plan,
            binding=digest,
            service=profile.credential_service,
            account=profile.credential_account,
            parents=parents,
            lock_names=locks,
        )
    except Exception:
        reasons.append("bootstrap_record_invalid")
        return
    report["package"] = "recorded_match"
    report["credential_intent"] = value["credential"]
    if value["credential"] == "pending":
        reasons.append("credential_write_intent_retained")
    if not value["ready"]:
        reasons.append("bootstrap_incomplete")
    try:
        anchor._require(
            values.get("catalog_lock") == catalog_discriminator(profile.root, bootstrap_catalog_binding(value))
        )
        anchor._require(
            all(snapshot.identity(name, directory=True) == identity for name, identity in value["directories"].items())
        )
        anchor._require(all(snapshot.identity(name) == identity for name, identity in value["locks"].items()))
    except Exception:
        reasons.append("bootstrap_storage_binding_unverifiable")
    # Metadata only: ready user config/catalogs may legitimately have changed.
    # No content, token, digest, path or transaction ID is emitted in the report.
    output_fault = False
    for index, (name, _payload) in enumerate(plan.outputs):
        try:
            snapshot.file(name)
            exists = True
        except FileNotFoundError:
            exists = False
        except Exception:
            output_fault = True
            continue
        expected = index < value["progress"]
        pending = index == value["progress"] and value["pending"]
        if exists != expected and not pending:
            output_fault = True
    if output_fault:
        reasons.append("bootstrap_output_inventory_unverifiable")
    if value["pending"]:
        reasons.append("output_write_intent_retained")
