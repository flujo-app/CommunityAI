"""Bounded durable three-file transaction for anchor replacement recovery.

This module owns only file evidence and publication.  The caller must retain all
ordered lifecycle locks, validate the opaque context, prove any prior service
dead before retargeting, contain native descendants, and establish the endpoint
fence before consuming the journal.  A journal is an active fence until it is
explicitly consumed; there is no historical-receipt interpretation here.
"""

from __future__ import annotations

import base64
import copy
import ctypes
import hashlib
import json
import os
import re
import stat
import sys
import threading
from functools import wraps
from pathlib import Path
from uuid import uuid4

from communityai_anchor import worker_loading as private
from communityai_anchor.resource_recovery import RecoverableStateError

LEDGER_NAME = "recovery.json"
LEDGER_LIMIT = 128 * 1024
CONTEXT_LIMIT = 8 * 1024
PAYLOAD_LIMIT = 8 * 1024
TARGETS = ("bootstrap.json", "resources.json", "state.json")
PREFIX_TARGETS = TARGETS[:2]

_LEDGER_TEMP_PREFIX = ".recovery-ledger-"
_PHASES = {"intent", "prepared", "publishing", "activated"}
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_TEMPORARY = re.compile(r"\.recovery-[0-9a-f]{32}-(?:bootstrap|resources|state)\.json\.tmp")


class RecoveryTransactionError(RecoverableStateError):
    """The fixed recovery evidence is absent, ambiguous, or changed."""


def _fail():
    raise RecoveryTransactionError("unverifiable_recovery")


def _require(condition):
    if not condition:
        _fail()


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(type(key) is str and key not in result)
        result[key] = value
    return result


def _json_value(value, *, depth=0):
    _require(depth <= 12)
    if value is None or type(value) in {bool, str}:
        if type(value) is str:
            _require(len(value) <= LEDGER_LIMIT and "\x00" not in value)
        return
    if type(value) is int:
        # Context semantics belong to the caller and include unsigned native
        # identities/fingerprints. The encoded-size bound remains independent.
        _require(-(2**127) < value < 2**128)
        return
    if type(value) is list:
        _require(len(value) <= 256)
        for item in value:
            _json_value(item, depth=depth + 1)
        return
    if type(value) is dict:
        _require(len(value) <= 256)
        for key, item in value.items():
            _require(type(key) is str and len(key) <= 256 and "\x00" not in key)
            _json_value(item, depth=depth + 1)
        return
    _fail()


def _encode_json(value, limit):
    _json_value(value)
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):
        _fail()
    _require(0 < len(raw) <= limit)
    return raw


def _decode_json(raw, limit):
    _require(type(raw) is bytes and 0 < len(raw) <= limit)
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_json_pairs,
            parse_constant=lambda _: _fail(),
        )
    except RecoveryTransactionError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
        _fail()
    _json_value(value)
    return value


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _raw_field(raw):
    _validate_payload(raw)
    return base64.b64encode(raw).decode("ascii")


def _field_raw(value):
    _require(type(value) is str and 0 < len(value) <= 4 * ((PAYLOAD_LIMIT + 2) // 3))
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError):
        _fail()
    _require(base64.b64encode(raw).decode("ascii") == value)
    _validate_payload(raw)
    return raw


def _validate_payload(raw):
    _require(type(raw) is bytes and 0 < len(raw) <= PAYLOAD_LIMIT)
    value = _decode_json(raw, PAYLOAD_LIMIT)
    _require(type(value) is dict)


def _fingerprint(value):
    _require(
        type(value) is list
        and len(value) == 7
        and all(type(item) is int and 0 <= item < 2**64 for item in value)
        and value[1] > 0
        and value[6] == 1
    )
    _require(stat.S_ISREG(value[2]))
    return tuple(value)


def _stable_fingerprint(value):
    value = tuple(value)
    return value[:5] + value[6:]


def _evidence(raw, info):
    _validate_payload(raw)
    fingerprint = list(private._fingerprint(info))
    _fingerprint(fingerprint)
    return {
        "raw_b64": _raw_field(raw),
        "sha256": _digest(raw),
        "fingerprint": fingerprint,
        "uid": info.st_uid,
    }


def _prepared(raw):
    _validate_payload(raw)
    return {
        "raw_b64": _raw_field(raw),
        "sha256": _digest(raw),
        "fingerprint": None,
        "uid": None,
    }


def _validate_evidence(value, *, nullable_fingerprint=False):
    _require(type(value) is dict and set(value) == {"raw_b64", "sha256", "fingerprint", "uid"})
    raw = _field_raw(value["raw_b64"])
    _require(type(value["sha256"]) is str and _HEX64.fullmatch(value["sha256"]) is not None)
    _require(value["sha256"] == _digest(raw))
    fingerprint = value["fingerprint"]
    uid = value["uid"]
    if nullable_fingerprint and fingerprint is None:
        _require(uid is None)
    else:
        _fingerprint(fingerprint)
        _require(type(uid) is int and 0 <= uid < 2**63)
        _require(fingerprint[3] == len(raw))
    return raw


def _validate_context(context):
    _require(type(context) is dict)
    _encode_json(context, CONTEXT_LIMIT)


def _serialized(method):
    @wraps(method)
    def held(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)

    return held


def _temporary(transaction, target):
    return f".recovery-{transaction}-{target}.tmp"


def _validate_ledger(value):
    _require(
        type(value) is dict
        and set(value)
        == {
            "schema_version",
            "transaction",
            "revision",
            "previous_digest",
            "phase",
            "context",
            "files",
            "retired",
        }
    )
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    transaction = value["transaction"]
    _require(type(transaction) is str and _HEX32.fullmatch(transaction) is not None)
    revision = value["revision"]
    _require(type(revision) is int and 0 <= revision < 2**63)
    previous = value["previous_digest"]
    _require(
        (revision == 0 and previous is None) or (revision > 0 and type(previous) is str and _HEX64.fullmatch(previous))
    )
    _require(type(value["phase"]) is str and value["phase"] in _PHASES)
    _validate_context(value["context"])
    files = value["files"]
    _require(type(files) is dict and set(files) == set(TARGETS))
    prepared_count = published_count = 0
    prepared_gap = published_gap = False
    for target in TARGETS:
        record = files[target]
        _require(
            type(record) is dict
            and set(record) == {"target", "temporary", "source", "prepared", "published_fingerprint"}
            and record["target"] == target
            and record["temporary"] == _temporary(transaction, target)
        )
        _validate_evidence(record["source"])
        prepared = record["prepared"]
        if prepared is not None:
            _require(not prepared_gap)
            _validate_evidence(prepared, nullable_fingerprint=True)
            prepared_count += 1
        else:
            prepared_gap = True
        published = record["published_fingerprint"]
        if published is not None:
            _require(prepared is not None and prepared["fingerprint"] is not None)
            _fingerprint(published)
            _require(_stable_fingerprint(published) == _stable_fingerprint(prepared["fingerprint"]))
            _require(not published_gap)
            published_count += 1
        else:
            published_gap = True
    # The staged path durably prepares/publishes bootstrap and resources before
    # the caller can construct state.json with exact post-publication evidence.
    # No other partial target set is representable.
    _require(prepared_count in {0, len(PREFIX_TARGETS), len(TARGETS)})
    _require(published_count <= prepared_count)
    if value["phase"] == "intent":
        _require(published_count == 0)
        if prepared_count == len(TARGETS):
            _require(any(files[name]["prepared"]["fingerprint"] is None for name in TARGETS))
    elif value["phase"] == "prepared":
        _require(
            prepared_count == len(TARGETS)
            and published_count == 0
            and all(files[name]["prepared"]["fingerprint"] is not None for name in TARGETS)
        )
    elif value["phase"] == "publishing":
        _require(prepared_count in {len(PREFIX_TARGETS), len(TARGETS)} and 0 < published_count < len(TARGETS))
    else:
        _require(prepared_count == published_count == len(TARGETS))
    retired = value["retired"]
    _require(type(retired) is list and len(retired) <= len(TARGETS))
    names = set()
    for record in retired:
        _require(
            type(record) is dict
            and set(record) == {"temporary", "raw_b64", "sha256", "fingerprint", "uid"}
            and type(record["temporary"]) is str
            and _TEMPORARY.fullmatch(record["temporary"]) is not None
            and record["temporary"] not in names
        )
        names.add(record["temporary"])
        _validate_evidence({key: record[key] for key in ("raw_b64", "sha256", "fingerprint", "uid")})
    _encode_json(value, LEDGER_LIMIT)
    return value


def _ledger_bytes(value):
    _validate_ledger(value)
    return _encode_json(value, LEDGER_LIMIT)


def _next_value(current, **changes):
    proposed = copy.deepcopy(current)
    proposed.update(copy.deepcopy(changes))
    proposed["revision"] = current["revision"] + 1
    _require(proposed["revision"] < 2**63)
    proposed["previous_digest"] = _digest(_ledger_bytes(current))
    return _validate_ledger(proposed)


def _platform():
    if not sys.platform.startswith("linux"):
        raise RecoveryTransactionError("unsupported_platform")


class RecoveryTransaction:
    """Active fixed-path recovery ledger. Instances own pinned directory fds."""

    def __init__(self, root, guard):
        _platform()
        _require(callable(guard))
        self._mutex = threading.RLock()
        self.guard = guard
        self.root = private._directory(root)
        self.anchor = self.root / "anchor"
        private._directory(self.anchor)
        self.root_identity = private._identity(private._stat(self.root, directory=True))
        self.anchor_identity = private._identity(private._stat(self.anchor, directory=True))
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        self.root_fd = self.anchor_fd = None
        self.value = self.raw = self.fingerprint = None
        try:
            self.root_fd = os.open(self.root, flags)
            self.anchor_fd = os.open("anchor", flags, dir_fd=self.root_fd)
            self._boundary()
        except BaseException:
            self.close()
            raise

    @classmethod
    def begin(cls, root, *, context, prepared=None, guard):
        owner = cls(root, guard)
        try:
            _validate_context(context)
            prepared = owner._prepared_map(prepared)
            owner._require_no_ledger_or_temps()
            transaction = uuid4().hex
            files = {}
            owner._boundary()
            for target in TARGETS:
                raw, info = owner._read_named(target, PAYLOAD_LIMIT)
                files[target] = {
                    "target": target,
                    "temporary": _temporary(transaction, target),
                    "source": _evidence(raw, info),
                    "prepared": None if prepared is None else _prepared(prepared[target]),
                    "published_fingerprint": None,
                }
            owner._boundary()
            value = _validate_ledger(
                {
                    "schema_version": 1,
                    "transaction": transaction,
                    "revision": 0,
                    "previous_digest": None,
                    "phase": "intent",
                    "context": copy.deepcopy(context),
                    "files": files,
                    "retired": [],
                }
            )
            for target in TARGETS:
                owner._match_exact(owner._optional_named(target, PAYLOAD_LIMIT), files[target]["source"])
            owner._publish_initial(value)
            owner._inventory_temps()
            return owner
        except BaseException:
            owner.close()
            raise

    @classmethod
    def open(cls, root, *, guard):
        owner = cls(root, guard)
        try:
            owner.raw, info = owner._read_named(LEDGER_NAME, LEDGER_LIMIT)
            owner.fingerprint = tuple(private._fingerprint(info))
            owner.value = _validate_ledger(_decode_json(owner.raw, LEDGER_LIMIT))
            _require(owner.raw == _ledger_bytes(owner.value))
            owner._inventory_temps()
            owner._sync_named(LEDGER_NAME)
            owner._sync_anchor()
            owner._validate_current()
            return owner
        except BaseException:
            owner.close()
            raise

    @_serialized
    def close(self):
        for name in ("anchor_fd", "root_fd"):
            descriptor = getattr(self, name, None)
            if descriptor is not None:
                setattr(self, name, None)
                os.close(descriptor)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @_serialized
    def snapshot(self):
        self._validate_current()
        return copy.deepcopy(self.value)

    @_serialized
    def update_context(self, context):
        _validate_context(context)
        self._validate_current()
        self._write_ledger(_next_value(self.value, context=copy.deepcopy(context)))
        return self.snapshot()

    @_serialized
    def set_prepared(self, prepared):
        prepared = self._prepared_map(prepared)
        _require(prepared is not None)
        self.reconcile()
        _require(self.value["phase"] == "intent")
        _require(all(self.value["files"][name]["prepared"] is None for name in TARGETS))
        files = copy.deepcopy(self.value["files"])
        for name in TARGETS:
            files[name]["prepared"] = _prepared(prepared[name])
        self._write_ledger(_next_value(self.value, files=files))
        return self.snapshot()

    @_serialized
    def set_prefix(self, prepared):
        """Durably set only bootstrap/resources targets before prefix effects.

        The final state payload is deliberately absent: callers construct it
        later from exact post-publication prefix and endpoint evidence.
        Repeating the call with the same bytes is an acknowledgement retry;
        different bytes can never retarget an in-flight prefix.
        """

        prepared = self._prepared_subset(prepared, PREFIX_TARGETS)
        self.reconcile()
        files = self.value["files"]
        _require(files["state.json"]["prepared"] is None)
        existing = [files[name]["prepared"] for name in PREFIX_TARGETS]
        if any(value is not None for value in existing):
            _require(all(value is not None for value in existing))
            for name in PREFIX_TARGETS:
                _require(_validate_evidence(files[name]["prepared"], nullable_fingerprint=True) == prepared[name])
            return self.snapshot()
        _require(self.value["phase"] == "intent")
        updated = copy.deepcopy(files)
        for name in PREFIX_TARGETS:
            updated[name]["prepared"] = _prepared(prepared[name])
        self._write_ledger(_next_value(self.value, files=updated))
        return self.snapshot()

    @_serialized
    def set_final_state(self, raw):
        """Set the one final state payload after the exact prefix is published."""

        _validate_payload(raw)
        self.reconcile()
        files = self.value["files"]
        _require(all(files[name]["published_fingerprint"] is not None for name in PREFIX_TARGETS))
        record = files["state.json"]
        _require(record["published_fingerprint"] is None)
        if record["prepared"] is not None:
            _require(_validate_evidence(record["prepared"], nullable_fingerprint=True) == raw)
            return self.snapshot()
        self._match_exact(self._optional_named("state.json", PAYLOAD_LIMIT), record["source"])
        _require(self._optional_named(record["temporary"], PAYLOAD_LIMIT) is None)
        updated = copy.deepcopy(files)
        updated["state.json"]["prepared"] = _prepared(raw)
        self._write_ledger(_next_value(self.value, files=updated))
        return self.snapshot()

    @_serialized
    def reconcile(self):
        self._validate_current()
        self._cleanup_retired()
        files = copy.deepcopy(self.value["files"])
        changed = False
        published_prefix = True
        lost_acknowledgement = False
        for name in TARGETS:
            record = files[name]
            prepared = record["prepared"]
            target = self._optional_named(name, PAYLOAD_LIMIT)
            temporary = self._optional_named(record["temporary"], PAYLOAD_LIMIT)
            if prepared is None:
                _require(record["published_fingerprint"] is None and temporary is None)
                self._match_exact(target, record["source"])
                published_prefix = False
                continue
            prepared_raw = _validate_evidence(prepared, nullable_fingerprint=True)
            if record["published_fingerprint"] is not None:
                _require(published_prefix and temporary is None)
                self._match_exact(target, prepared, fingerprint=record["published_fingerprint"])
                continue
            if prepared["fingerprint"] is None:
                self._match_exact(target, record["source"])
                if temporary is not None:
                    info = self._sync_unregistered(record["temporary"], prepared_raw)
                    prepared["fingerprint"] = list(private._fingerprint(info))
                    prepared["uid"] = info.st_uid
                    _validate_evidence(prepared, nullable_fingerprint=True)
                    changed = True
                published_prefix = False
                continue
            if self._matches_exact(target, record["source"]):
                self._match_exact(temporary, prepared)
                published_prefix = False
                continue
            _require(published_prefix and not lost_acknowledgement and temporary is None)
            self._match_published(target, prepared)
            self._sync_published(name, prepared)
            raw, info = self._read_named(name, PAYLOAD_LIMIT)
            _require(
                raw == prepared_raw
                and _stable_fingerprint(private._fingerprint(info)) == _stable_fingerprint(prepared["fingerprint"])
            )
            record["published_fingerprint"] = list(private._fingerprint(info))
            lost_acknowledgement = True
            changed = True
        phase = self._phase(files)
        if phase != self.value["phase"]:
            changed = True
        if changed:
            self._write_ledger(_next_value(self.value, files=files, phase=phase))
        self._inventory_temps()
        return self.snapshot()

    @_serialized
    def prepare(self):
        self.reconcile()
        _require(all(self.value["files"][name]["prepared"] is not None for name in TARGETS))
        if self.value["phase"] == "activated":
            return self.snapshot()
        self._prepare_names(TARGETS)
        self.reconcile()
        _require(self.value["phase"] in {"prepared", "publishing", "activated"})
        return self.snapshot()

    @_serialized
    def publish_prefix(self):
        """Publish bootstrap/resources exactly, never state.json."""

        self.reconcile()
        _require(self.value["files"]["state.json"]["prepared"] is None)
        _require(all(self.value["files"][name]["prepared"] is not None for name in PREFIX_TARGETS))
        self._prepare_names(PREFIX_TARGETS)
        self._publish_names(PREFIX_TARGETS)
        self.reconcile()
        _require(
            self.value["phase"] == "publishing"
            and all(self.value["files"][name]["published_fingerprint"] is not None for name in PREFIX_TARGETS)
            and self.value["files"]["state.json"]["prepared"] is None
        )
        return self.snapshot()

    def _prepare_names(self, names):
        for name in names:
            self.reconcile()
            record = self.value["files"][name]
            _require(record["prepared"] is not None)
            if record["published_fingerprint"] is not None:
                continue
            if record["prepared"]["fingerprint"] is not None:
                continue
            self._match_exact(self._optional_named(name, PAYLOAD_LIMIT), record["source"])
            _require(self._optional_named(record["temporary"], PAYLOAD_LIMIT) is None)
            raw = _validate_evidence(record["prepared"], nullable_fingerprint=True)
            self._create_prepared(record["temporary"], raw)
            observed, info = self._read_named(record["temporary"], PAYLOAD_LIMIT)
            _require(observed == raw)
            files = copy.deepcopy(self.value["files"])
            prepared = files[name]["prepared"]
            prepared["fingerprint"] = list(private._fingerprint(info))
            prepared["uid"] = info.st_uid
            phase = self._phase(files)
            self._write_ledger(_next_value(self.value, files=files, phase=phase))

    @_serialized
    def publish(self):
        self.prepare()
        self._publish_names(TARGETS)
        _require(self.value["phase"] == "activated")
        return self.snapshot()

    def _publish_names(self, names):
        for name in names:
            self.reconcile()
            record = self.value["files"][name]
            if record["published_fingerprint"] is not None:
                continue
            for prior in TARGETS[: TARGETS.index(name)]:
                _require(self.value["files"][prior]["published_fingerprint"] is not None)
            self._match_exact(self._optional_named(name, PAYLOAD_LIMIT), record["source"])
            self._match_exact(self._optional_named(record["temporary"], PAYLOAD_LIMIT), record["prepared"])
            self._effect(
                lambda: os.replace(
                    record["temporary"],
                    name,
                    src_dir_fd=self.anchor_fd,
                    dst_dir_fd=self.anchor_fd,
                )
            )
            self._sync_anchor()
            self.reconcile()
            _require(self.value["files"][name]["published_fingerprint"] is not None)

    @_serialized
    def retarget(self, *, context, prepared=None, authorize):
        _require(callable(authorize) and authorize() is True)
        _validate_context(context)
        prepared = self._prepared_map(prepared)
        self.reconcile()
        _require(not self.value["retired"])
        sources = {}
        retired = []
        for name in TARGETS:
            raw, info = self._read_named(name, PAYLOAD_LIMIT)
            sources[name] = _evidence(raw, info)
            record = self.value["files"][name]
            temporary = self._optional_named(record["temporary"], PAYLOAD_LIMIT)
            if temporary is not None:
                temp_raw, temp_info = temporary
                _require(record["prepared"] is not None)
                self._match_exact(temporary, record["prepared"])
                retired.append(
                    {
                        "temporary": record["temporary"],
                        **_evidence(temp_raw, temp_info),
                    }
                )
        transaction = uuid4().hex
        files = {}
        for name in TARGETS:
            files[name] = {
                "target": name,
                "temporary": _temporary(transaction, name),
                "source": sources[name],
                "prepared": None if prepared is None else _prepared(prepared[name]),
                "published_fingerprint": None,
            }
        proposed = _next_value(
            self.value,
            transaction=transaction,
            phase="intent",
            context=copy.deepcopy(context),
            files=files,
            retired=retired,
        )
        for name in TARGETS:
            self._match_exact(self._optional_named(name, PAYLOAD_LIMIT), sources[name])
        for record in retired:
            self._match_exact(
                self._optional_named(record["temporary"], PAYLOAD_LIMIT),
                {key: record[key] for key in ("raw_b64", "sha256", "fingerprint", "uid")},
            )
        self._write_ledger(proposed)
        self._cleanup_retired()
        self._inventory_temps()
        return self.snapshot()

    @_serialized
    def consume(self, *, endpoint_fence):
        _require(callable(endpoint_fence))
        self.reconcile()
        _require(self.value["phase"] == "activated" and not self.value["retired"])
        _require(endpoint_fence() is True)
        self._validate_current()
        self._effect(lambda: os.unlink(LEDGER_NAME, dir_fd=self.anchor_fd))
        self._sync_anchor()
        _require(self._optional_named(LEDGER_NAME, LEDGER_LIMIT) is None)
        self.value = self.raw = self.fingerprint = None

    def _prepared_map(self, value):
        if value is None:
            return None
        return self._prepared_subset(value, TARGETS)

    def _prepared_subset(self, value, names):
        _require(type(value) is dict and set(value) == set(names))
        result = {}
        for name in names:
            raw = value[name]
            _validate_payload(raw)
            result[name] = raw
        return result

    def _phase(self, files):
        prepared = [files[name]["prepared"] for name in TARGETS]
        published = [files[name]["published_fingerprint"] is not None for name in TARGETS]
        if all(published):
            return "activated"
        if any(published):
            return "publishing"
        if all(value is not None and value["fingerprint"] is not None for value in prepared):
            return "prepared"
        return "intent"

    def _boundary(self):
        self.guard()
        self._validate_storage()

    def _effect(self, call):
        self._boundary()
        result = call()
        self._boundary()
        return result

    def _validate_storage(self):
        _require(self.root_fd is not None and self.anchor_fd is not None)
        root_info = os.fstat(self.root_fd)
        anchor_info = os.fstat(self.anchor_fd)
        _require(stat.S_ISDIR(root_info.st_mode) and stat.S_ISDIR(anchor_info.st_mode))
        _require(private._identity(root_info) == self.root_identity)
        _require(private._identity(anchor_info) == self.anchor_identity)
        _require(private._identity(private._stat(self.root, directory=True)) == self.root_identity)
        _require(private._identity(private._stat(self.anchor, directory=True)) == self.anchor_identity)

    def _validate_current(self):
        _require(self.value is not None and self.raw is not None and self.fingerprint is not None)
        self._boundary()
        raw, info = self._read_named(LEDGER_NAME, LEDGER_LIMIT)
        _require(raw == self.raw and tuple(private._fingerprint(info)) == self.fingerprint)
        decoded = _validate_ledger(_decode_json(raw, LEDGER_LIMIT))
        _require(raw == _ledger_bytes(decoded) and decoded == self.value)

    def _require_no_ledger_or_temps(self):
        _require(self._optional_named(LEDGER_NAME, LEDGER_LIMIT) is None)
        for name in os.listdir(self.anchor_fd):
            _require(not name.startswith(".recovery-"))

    def _inventory_temps(self):
        allowed = {record["temporary"] for record in self.value["files"].values()}
        allowed.update(record["temporary"] for record in self.value["retired"])
        for name in os.listdir(self.anchor_fd):
            if name.startswith(".recovery-"):
                _require(name in allowed)

    def _read_named(self, name, limit):
        _require(type(name) is str and 0 < len(name) <= 255 and "/" not in name and "\\" not in name)
        before = os.stat(name, dir_fd=self.anchor_fd, follow_symlinks=False)
        self._validate_file_info(before, limit)
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=self.anchor_fd)
        try:
            opened = os.fstat(descriptor)
            self._validate_file_info(opened, limit)
            _require(private._fingerprint(opened) == private._fingerprint(before))
            parts = bytearray()
            while len(parts) <= limit:
                chunk = os.read(descriptor, min(65536, limit + 1 - len(parts)))
                if not chunk:
                    break
                parts.extend(chunk)
            _require(len(parts) == before.st_size)
            _require(private._fingerprint(os.fstat(descriptor)) == private._fingerprint(opened))
        finally:
            os.close(descriptor)
        after = os.stat(name, dir_fd=self.anchor_fd, follow_symlinks=False)
        _require(private._fingerprint(after) == private._fingerprint(before))
        return bytes(parts), after

    def _optional_named(self, name, limit):
        try:
            return self._read_named(name, limit)
        except FileNotFoundError:
            return None

    def _validate_file_info(self, info, limit):
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and info.st_ino > 0
            and info.st_uid == os.geteuid()
            and not info.st_mode & 0o077
            and 0 < info.st_size <= limit
        )

    def _matches_exact(self, observed, evidence):
        if observed is None:
            return False
        raw, info = observed
        expected = _validate_evidence(evidence, nullable_fingerprint=True)
        if evidence["fingerprint"] is None:
            return False
        return (
            raw == expected
            and list(private._fingerprint(info)) == evidence["fingerprint"]
            and info.st_uid == evidence["uid"]
        )

    def _match_exact(self, observed, evidence, *, fingerprint=None):
        _require(observed is not None)
        raw, info = observed
        expected_raw = _validate_evidence(evidence, nullable_fingerprint=True)
        expected_fingerprint = evidence["fingerprint"] if fingerprint is None else fingerprint
        _require(expected_fingerprint is not None)
        _require(
            raw == expected_raw
            and list(private._fingerprint(info)) == list(expected_fingerprint)
            and info.st_uid == evidence["uid"]
        )

    def _match_published(self, observed, prepared):
        _require(observed is not None and prepared["fingerprint"] is not None)
        raw, info = observed
        expected = _validate_evidence(prepared, nullable_fingerprint=True)
        current = private._fingerprint(info)
        _require(
            raw == expected
            and private._identity(info) == tuple(prepared["fingerprint"][:2])
            and _stable_fingerprint(current) == _stable_fingerprint(prepared["fingerprint"])
            and info.st_uid == prepared["uid"]
        )

    def _create_prepared(self, name, raw):
        descriptor = self._effect(
            lambda: os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.anchor_fd,
            )
        )
        try:
            offset = 0
            while offset < len(raw):
                written = self._effect(lambda: os.write(descriptor, raw[offset:]))
                _require(written > 0)
                offset += written
            self._effect(lambda: os.fsync(descriptor))
        finally:
            os.close(descriptor)
        self._sync_anchor()

    def _publish_initial(self, value):
        raw = _ledger_bytes(value)
        temporary = _LEDGER_TEMP_PREFIX + value["transaction"] + ".tmp"
        self._write_new(temporary, raw)
        try:
            self._boundary()
            _require(self._optional_named(LEDGER_NAME, LEDGER_LIMIT) is None)
            self._effect(lambda: self._rename_new(temporary, LEDGER_NAME))
            self._sync_anchor()
            observed, info = self._read_named(LEDGER_NAME, LEDGER_LIMIT)
            _require(observed == raw)
            self.value, self.raw, self.fingerprint = copy.deepcopy(value), raw, tuple(private._fingerprint(info))
            self._validate_current()
        except BaseException:
            raise

    def _write_ledger(self, proposed):
        proposed = _validate_ledger(proposed)
        raw = _ledger_bytes(proposed)
        self._validate_current()
        temporary = _LEDGER_TEMP_PREFIX + uuid4().hex + ".tmp"
        self._write_new(temporary, raw)
        self._validate_current()
        self._effect(
            lambda: os.replace(
                temporary,
                LEDGER_NAME,
                src_dir_fd=self.anchor_fd,
                dst_dir_fd=self.anchor_fd,
            )
        )
        self._sync_anchor()
        observed, info = self._read_named(LEDGER_NAME, LEDGER_LIMIT)
        _require(observed == raw)
        self.value, self.raw, self.fingerprint = copy.deepcopy(proposed), raw, tuple(private._fingerprint(info))
        self._validate_current()

    def _write_new(self, name, raw):
        descriptor = self._effect(
            lambda: os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.anchor_fd,
            )
        )
        try:
            offset = 0
            while offset < len(raw):
                written = self._effect(lambda: os.write(descriptor, raw[offset:]))
                _require(written > 0)
                offset += written
            self._effect(lambda: os.fsync(descriptor))
        finally:
            os.close(descriptor)
        observed, _ = self._read_named(name, max(len(raw), 1))
        _require(observed == raw)

    def _rename_new(self, source, destination):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renameat2
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(self.anchor_fd, os.fsencode(source), self.anchor_fd, os.fsencode(destination), 1) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))

    def _sync_named(self, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=self.anchor_fd)
        try:
            self._validate_file_info(os.fstat(descriptor), LEDGER_LIMIT)
            self._effect(lambda: os.fsync(descriptor))
        finally:
            os.close(descriptor)

    def _sync_published(self, name, prepared):
        before = os.stat(name, dir_fd=self.anchor_fd, follow_symlinks=False)
        self._validate_file_info(before, PAYLOAD_LIMIT)
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=self.anchor_fd)
        try:
            opened = os.fstat(descriptor)
            self._validate_file_info(opened, PAYLOAD_LIMIT)
            _require(private._fingerprint(opened) == private._fingerprint(before))
            expected = _validate_evidence(prepared, nullable_fingerprint=True)
            raw = bytearray()
            while len(raw) <= PAYLOAD_LIMIT:
                part = os.read(descriptor, min(65536, PAYLOAD_LIMIT + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
            _require(
                bytes(raw) == expected
                and _stable_fingerprint(private._fingerprint(opened)) == _stable_fingerprint(prepared["fingerprint"])
                and opened.st_uid == prepared["uid"]
            )
            self._effect(lambda: os.fsync(descriptor))
            _require(private._fingerprint(os.fstat(descriptor)) == private._fingerprint(opened))
        finally:
            os.close(descriptor)
        after = os.stat(name, dir_fd=self.anchor_fd, follow_symlinks=False)
        _require(private._fingerprint(after) == private._fingerprint(before))
        self._sync_anchor()

    def _sync_unregistered(self, name, expected):
        before = os.stat(name, dir_fd=self.anchor_fd, follow_symlinks=False)
        self._validate_file_info(before, PAYLOAD_LIMIT)
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=self.anchor_fd)
        try:
            opened = os.fstat(descriptor)
            self._validate_file_info(opened, PAYLOAD_LIMIT)
            _require(private._fingerprint(opened) == private._fingerprint(before))
            raw = bytearray()
            while len(raw) <= PAYLOAD_LIMIT:
                part = os.read(descriptor, min(65536, PAYLOAD_LIMIT + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
            _require(bytes(raw) == expected)
            self._effect(lambda: os.fsync(descriptor))
            _require(private._fingerprint(os.fstat(descriptor)) == private._fingerprint(opened))
        finally:
            os.close(descriptor)
        after = os.stat(name, dir_fd=self.anchor_fd, follow_symlinks=False)
        _require(private._fingerprint(after) == private._fingerprint(before))
        self._sync_anchor()
        raw, info = self._read_named(name, PAYLOAD_LIMIT)
        _require(raw == expected and private._fingerprint(info) == private._fingerprint(before))
        return info

    def _sync_anchor(self):
        self._effect(lambda: os.fsync(self.anchor_fd))

    def _cleanup_retired(self):
        while self.value["retired"]:
            record = self.value["retired"][0]
            observed = self._optional_named(record["temporary"], PAYLOAD_LIMIT)
            if observed is not None:
                self._match_exact(
                    observed,
                    {key: record[key] for key in ("raw_b64", "sha256", "fingerprint", "uid")},
                )
                self._effect(lambda: os.unlink(record["temporary"], dir_fd=self.anchor_fd))
                self._sync_anchor()
            retired = copy.deepcopy(self.value["retired"][1:])
            self._write_ledger(_next_value(self.value, retired=retired))


__all__ = [
    "CONTEXT_LIMIT",
    "LEDGER_LIMIT",
    "LEDGER_NAME",
    "PAYLOAD_LIMIT",
    "PREFIX_TARGETS",
    "RecoveryTransaction",
    "RecoveryTransactionError",
    "TARGETS",
]
