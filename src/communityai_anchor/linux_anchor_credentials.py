"""Private anchored Secret Service calls; local death never cancels remote SET.

Only the trusted launcher supplies command/environment. The owning AnchorNode
retains process handles and durable leaf authority. Same-UID code is cooperative,
not sandboxed. No public lifecycle authority follows from a helper response.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import subprocess
import sys
import time
from dataclasses import dataclass
from uuid import uuid4

from communityai_anchor import linux_anchor as anchor, worker_loading as private
from communityai_anchor.linux_anchor_resources import read_resources
from communityai_anchor.linux_anchor_state import validate_state
from communityai_anchor.resource_recovery import current_recovery_identity

MAX_MESSAGE = 2048
CALL_SECONDS = 20.0
CLEANUP_SECONDS = 5.0
TRANSACTION_SECONDS = 60.0
INVALID_CREDENTIAL = object()


class CredentialExecutionError(RuntimeError):
    def __init__(self):
        super().__init__("The anchored credential operation could not be verified")


@dataclass(frozen=True)
class CredentialIdentity:
    """Identity only: the production parent cannot call a keyring through this."""

    service: str
    account: str


def _json(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n"
    ).encode()


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _hex(value, length):
    return type(value) is str and anchor.re.fullmatch("[0-9a-f]{" + str(length) + "}", value) is not None


def _key(value):
    # Exact native control-key wire shape; no credential-store import in parent
    # framing. The admitted child still uses the native adapter's validation.
    anchor._require(
        type(value) is str
        and len(value) <= 1024
        and anchor.re.fullmatch(r"drift_control_[A-Za-z0-9_-]{43,}", value) is not None
    )
    return value


def _decode(raw):
    anchor._require(0 < len(raw) <= MAX_MESSAGE)
    value = json.loads(
        bytes(raw).decode("ascii"), object_pairs_hook=anchor._unique, parse_constant=lambda _: anchor._require(False)
    )
    anchor._require(type(value) is dict and _json(value) == bytes(raw))
    return value


def _validate_request(value):
    anchor._require(set(value) == {"version", "nonce", "operation", "files", "executable", "helper", "secret"})
    anchor._require(type(value["version"]) is int and value["version"] == 1 and _hex(value["nonce"], 32))
    anchor._require(type(value["operation"]) is str and value["operation"] in {"get", "set"})
    executable = value["executable"]
    anchor._require(
        type(executable) is list and len(executable) == 2 and all(type(n) is int and n > 0 for n in executable)
    )
    helper = value["helper"]
    anchor._require(type(helper) is list and len(helper) == 2 and all(type(n) is int and n > 0 for n in helper))
    files = value["files"]
    anchor._require(type(files) is dict and set(files) == {"state.json", "bootstrap.json", "resources.json"})
    for evidence in files.values():
        anchor._require(type(evidence) is dict and set(evidence) == {"digest", "fingerprint"})
        anchor._require(_hex(evidence["digest"], 64))
        fp = evidence["fingerprint"]
        anchor._require(type(fp) is list and len(fp) == 7 and all(type(n) is int and n >= 0 for n in fp))
    if value["operation"] == "get":
        anchor._require(value["secret"] is None)
    else:
        _key(value["secret"])
    return value


def _response(raw, nonce, operation):
    value = _decode(raw)
    anchor._require(set(value) == {"version", "nonce", "operation", "status", "digest"})
    anchor._require(type(value["version"]) is int and value["version"] == 1 and value["nonce"] == nonce)
    anchor._require(value["operation"] == operation)
    if operation == "set":
        anchor._require(value["status"] == "written" and value["digest"] is None)
    else:
        anchor._require(type(value["status"]) is str and value["status"] in {"found", "missing", "invalid"})
        anchor._require(_hex(value["digest"], 64) if value["status"] == "found" else value["digest"] is None)
    return value


def _kill_pinned_helper(owner, helper, descriptor, deadline):
    # Path replacement withholds acknowledgement but must not cancel stop of
    # the exact cgroup descriptor retained before helper birth.
    owner.layout.validate()
    owner._lease.validate()
    anchor._require(private._identity(os.fstat(descriptor)) == helper.root_identity)
    control = anchor.cg._control(descriptor, "cgroup.kill", write=True)
    try:
        anchor._require(os.write(control, b"1\n") == 2)
    finally:
        os.close(control)
    while anchor.cg._events(anchor.cg._read_control(descriptor, "cgroup.events")):
        anchor._require(time.monotonic() < deadline)
        time.sleep(0.05)
    anchor._require(private._identity(os.fstat(descriptor)) == helper.root_identity)


class CredentialExecutor:
    """Serialized native helper in the owner's already durably bound node leaf."""

    def __init__(self, command, environment):
        self.command, self.environment = tuple(command), dict(environment)

    @classmethod
    def for_profile(cls, profile):
        anchor._require(sys.platform.startswith("linux") and getattr(sys, "frozen", False) is True)
        return cls(("/proc/self/exe", "--anchor-credential-helper"), _environment(profile))

    def __call__(self, owner, operation, secret, marker, *, cancelled, deadline, reconcile=False):
        from drift.node import linux_cgroup_process as native

        # Check this before entering cleanup: this executor has no authority to
        # stop a running main node or another in-flight helper.
        owner._integrity()
        owner._validate_leaf()
        state = owner._state.value
        anchor._require(state["phase"] == "starting" and state["operation"] == "start")
        anchor._require(state["generation"]["pid"] is None and state["generation"]["start_ticks"] is None)
        anchor._require(owner._process is None and owner._credential_process is None and not owner._fatal)
        anchor._require(state["generation"]["cgroup"] == owner._leaf.to_json())
        anchor._require(marker == owner._bootstrap.value)
        nonce = uuid4().hex
        evidence = (
            ("state.json", state, owner._state._fingerprint),
            ("bootstrap.json", marker, owner._bootstrap.fingerprint),
            ("resources.json", owner._resources[0], owner._resources[1]),
        )
        request = dict(
            version=1,
            nonce=nonce,
            operation=operation,
            secret=secret,
            executable=list(private._identity(os.stat("/proc/self/exe"))),
            files={name: dict(digest=_digest(_json(value)), fingerprint=list(fp)) for name, value, fp in evidence},
        )
        if operation == "set":
            _key(secret)
        read_fd = write_fd = None
        helper_fd = helper = None
        helper_created = False
        helper_name = "credential-" + nonce
        process = response = None
        success = False
        # Reserve cleanup time within one monotonic budget, not a fresh timeout
        # after each read. Synchronous kernel/filesystem I/O is not hard realtime.
        deadline = min(time.monotonic() + CALL_SECONDS, deadline - (0 if reconcile else CALL_SECONDS))
        work_deadline = deadline - CLEANUP_SECONDS
        try:
            owner._integrity()
            owner._validate_leaf()
            # After durable SET intent, dispatch is not skipped merely because
            # Stop raced with intent publication. Reconciliation still runs.
            anchor._require(operation == "set" or not cancelled())
            anchor._require(time.monotonic() < work_deadline)
            anchor._require(owner._state.value["generation"]["cgroup"] == owner._leaf.to_json())
            anchor._require(not anchor._subgroups(owner._leaf_fd))
            anchor.cg.verify_cgroup_tree_empty(owner._leaf.root)
            # One-use child cgroups also avoid the kernel kill_seq regression
            # when CLONE_INTO_CGROUP targets a previously killed cgroup. The
            # durable generation owns this entire subtree after parent death.
            os.mkdir(helper_name, mode=0o700, dir_fd=owner._leaf_fd)
            helper_created = True
            helper_fd = anchor.cg._open_directory(helper_name, parent=owner._leaf_fd)
            helper = anchor.cg.validate_cgroup_profile(owner._leaf.root + "/" + helper_name)
            anchor._require(anchor.cg._observe_root(helper.root, helper_fd) == helper)
            request["helper"] = list(helper.root_identity)
            payload = _json(_validate_request(request))
            anchor._require(len(payload) <= MAX_MESSAGE)
            read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
            anchor._require(len(payload) <= os.fpathconf(write_fd, "PC_PIPE_BUF"))
            anchor._require(os.write(write_fd, payload) == len(payload))
            os.close(write_fd)
            write_fd = None
            process = native.spawn(
                helper_fd,
                self.command,
                env=self.environment,
                input_fd=read_fd,
                text=False,
                stderr=subprocess.DEVNULL,
            )
            owner._credential_process = process
            os.close(read_fd)
            read_fd = None
            anchor._require(process.cgroup_identity == helper.root_identity)
            _ticks, group = anchor._process(process.pid)
            anchor._require(
                group
                == owner.layout.service.control_group
                + "/nodes/node-"
                + owner._state.value["generation"]["id"]
                + "/"
                + helper_name
            )
            owner._integrity()
            owner._validate_leaf()
            anchor._require(anchor.cg._observe_root(helper.root, helper_fd) == helper)
            anchor._require(anchor._subgroups(owner._leaf_fd) == {helper_name})
            anchor._require((operation == "set" or not cancelled()) and time.monotonic() < work_deadline)
            process.release_gate()
            process.await_exec()
            stream = process.stdout.fileno()
            os.set_blocking(stream, False)
            poller = select.poll()
            poller.register(stream, select.POLLIN | select.POLLHUP | select.POLLERR)
            raw, eof = bytearray(), False
            while not eof or process.poll() is None:
                anchor._require(not cancelled() and time.monotonic() < work_deadline)
                if not eof and poller.poll(50):
                    try:
                        part = os.read(stream, MAX_MESSAGE + 1 - len(raw))
                    except BlockingIOError:
                        continue
                    raw.extend(part)
                    anchor._require(len(raw) <= MAX_MESSAGE)
                    eof = not part
                elif eof:
                    time.sleep(0.02)
            anchor._require(process.returncode == 0 and not cancelled())
            response = _response(raw, nonce, operation)
            success = True
        except Exception:
            success = False
        finally:
            cleanup_errors = []
            # Independent must-run containment and direct-handle stages. A bad
            # journal does not cancel Stop, but it still forbids acknowledgement.
            if process is not None:
                try:
                    process.kill()
                except Exception:
                    cleanup_errors.append(True)
            try:
                if helper is not None:
                    _kill_pinned_helper(owner, helper, helper_fd, deadline)
                    anchor._require(anchor.cg._observe_root(helper.root, helper_fd) == helper)
                    anchor.cg.verify_cgroup_tree_empty(helper.root)
                    anchor._require(not anchor._subgroups(helper_fd))
                elif helper_created:
                    # An incomplete child-directory binding cannot be adopted
                    # or retried. Stop through the original durable ancestor.
                    owner._fatal = True
                    owner._kill_profile(owner._leaf, deadline=deadline)
                    raise CredentialExecutionError()
                owner._validate_leaf()
                anchor.cg.verify_cgroup_tree_empty(owner._leaf.root)
                anchor._require(anchor._subgroups(owner._leaf_fd) == ({helper_name} if helper_created else set()))
            except Exception:
                cleanup_errors.append(True)
                # Independent original ancestor authority covers descendants
                # even when exact child pathname proof was damaged. Never
                # continue the transaction after taking this failure path.
                try:
                    owner._kill_profile(owner._leaf, deadline=deadline)
                    anchor.cg.verify_cgroup_tree_empty(owner._leaf.root)
                except Exception:
                    cleanup_errors.append(True)
            if process is not None:
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except Exception:
                    cleanup_errors.append(True)
            if helper is not None and not cleanup_errors:
                try:
                    # Only the exact newly created, pinned, proved-empty cgroup
                    # may be removed; never recursively delete a directory.
                    owner._validate_leaf()
                    anchor._require(anchor.cg._observe_root(helper.root, helper_fd) == helper)
                    os.rmdir(helper_name, dir_fd=owner._leaf_fd)
                    anchor._require(not anchor._subgroups(owner._leaf_fd))
                except Exception:
                    cleanup_errors.append(True)
                    # A late repopulation/removal race still requires both stop
                    # authorities before returning, not just a blocked status.
                    try:
                        _kill_pinned_helper(owner, helper, helper_fd, deadline)
                    except Exception:
                        cleanup_errors.append(True)
                    try:
                        owner._kill_profile(owner._leaf, deadline=deadline)
                        anchor.cg.verify_cgroup_tree_empty(owner._leaf.root)
                    except Exception:
                        cleanup_errors.append(True)
            for descriptor in (read_fd, write_fd, helper_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except Exception:
                        cleanup_errors.append(True)
            if process is not None and process.stdout is not None:
                try:
                    process.stdout.close()
                except Exception:
                    cleanup_errors.append(True)
            try:
                owner._integrity()
            except Exception:
                cleanup_errors.append(True)
            if cleanup_errors:
                owner._fatal = True
                success = False
            else:
                owner._credential_process = None
        if not success:
            raise CredentialExecutionError() from None
        return INVALID_CREDENTIAL if response["status"] == "invalid" else response["digest"]


def _read_pinned(directory, name, evidence):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        anchor._require(anchor.stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077)
        anchor._require(list(private._fingerprint(info)) == evidence["fingerprint"] and 0 < info.st_size <= 8192)
        raw = os.read(descriptor, 8193)
        anchor._require(len(raw) == info.st_size and not os.read(descriptor, 1))
        value = json.loads(raw, object_pairs_hook=anchor._unique, parse_constant=lambda _: anchor._require(False))
        anchor._require(type(value) is dict and _digest(_json(value)) == evidence["digest"])
        anchor._require(private._fingerprint(os.fstat(descriptor)) == private._fingerprint(info))
        anchor._require(
            private._fingerprint(os.stat(name, dir_fd=directory, follow_symlinks=False)) == private._fingerprint(info)
        )
        return value
    finally:
        os.close(descriptor)


def _admit_helper(profile, request, *, directory=None):
    """Read-only proof of exact parent, current intent, leaf and original locks."""
    import fcntl

    _validate_request(request)
    root = private._directory(profile.root)
    private._directory(root / "anchor")
    if directory is None:
        descriptor = anchor.cg._open_root(str(root / "anchor"))
        try:
            return _admit_helper(profile, request, directory=descriptor)
        finally:
            os.close(descriptor)
    values = {name: _read_pinned(directory, name, proof) for name, proof in request["files"].items()}
    state = validate_state(values["state.json"])
    marker = values["bootstrap.json"]
    generation = state["generation"]
    anchor._require(state["phase"] == "starting" and state["operation"] == "start")
    anchor._require(generation["cgroup"] is not None and generation["pid"] is None)
    anchor._require(marker["binding"] == _digest(_json(state["binding"])))
    anchor._require(marker["service"] == profile.credential_service and marker["account"] == profile.credential_account)
    anchor._require(marker["attempt"] == dict(generation=generation["id"], request_id=state["request_id"]))
    if request["operation"] == "set":
        anchor._require(marker["credential"] == "pending" and marker["ready"] is False)
        anchor._require(marker["credential_digest"] == _digest(request["secret"].encode()))
    service = anchor.inspect_service()
    anchor._require(service.pid == os.getppid() and state["binding"]["service"] == service.to_json())
    # The nondumpable parent's exe link is restricted. Its private gated
    # request binds its executable to this helper's own executable inode.
    anchor._require(list(private._identity(os.stat("/proc/self/exe"))) == request["executable"])
    anchor._require(state["binding"]["machine"] == current_recovery_identity().to_json())
    profiles = anchor._observe_layout(service)
    anchor._require(state["binding"]["layout"] == [p.to_json() for p in profiles])
    storage = state["binding"]["storage"]
    anchor._require(list(private._identity(private._stat(root, directory=True))) == storage["profile"])
    anchor._require(list(private._identity(private._stat(root / "anchor", directory=True))) == storage["directory"])
    anchor._require(list(private._identity(os.fstat(directory))) == storage["directory"])
    resources = read_resources(root, state["binding"])
    anchor._require(
        resources[0] == values["resources.json"]
        and list(resources[1]) == request["files"]["resources.json"]["fingerprint"]
    )
    locks = {
        "anchor-state.lock": storage["lease"],
        "node-lifetime.lock": resources[0]["identities"]["lifetime"],
        "node/resource-reservations/admission.lock": resources[0]["identities"]["admission"],
        "node/.catalog-bootstrap.lock": marker["locks"]["node/.catalog-bootstrap.lock"],
        "node/.node-config.json.write.lock": marker["locks"]["node/.node-config.json.write.lock"],
    }
    for name, identity in locks.items():
        parent = anchor.cg._open_root(str((root / name).parent))
        try:
            descriptor = os.open(
                (root / name).name, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
        finally:
            os.close(parent)
        try:
            anchor._require(list(anchor._lock_identity(os.fstat(descriptor))) == identity)
            anchor._require(list(anchor._lock_identity((root / name).lstat())) == identity)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise CredentialExecutionError()
        finally:
            os.close(descriptor)
    leaf = anchor.cg.LinuxCgroupProfile.from_json(generation["cgroup"])
    _ticks, group = anchor._process(os.getpid())
    helper_name = "credential-" + request["nonce"]
    anchor._require(group == service.control_group + "/nodes/node-" + generation["id"] + "/" + helper_name)
    anchor._require(anchor.cg.validate_cgroup_profile(leaf.root) == leaf)
    helper = anchor.cg.validate_cgroup_profile(leaf.root + "/" + helper_name)
    anchor._require(list(helper.root_identity) == request["helper"])
    descriptor = anchor.cg._open_root(leaf.root)
    try:
        anchor._require(anchor.cg._observe_root(leaf.root, descriptor) == leaf)
        anchor._require(anchor._subgroups(descriptor) == {helper_name})
    finally:
        os.close(descriptor)
    anchor._require(validate_state(private._read(root / "anchor" / "state.json")) == state)
    anchor._require(private._read(root / "anchor" / "bootstrap.json") == marker)
    anchor._require(read_resources(root, state["binding"]) == resources)
    anchor._require(anchor.inspect_service() == service and anchor._observe_layout(service) == profiles)
    for name, proof in request["files"].items():
        anchor._require(_read_pinned(directory, name, proof) == values[name])


def _native_store(profile):
    from communityai_desktop.credentials import NativeCredentialStore
    from keyring.errors import KeyringError

    from communityai_anchor.linux_secret_service import fixed_secret_service

    class FixedSecretService(NativeCredentialStore):
        @staticmethod
        def _keyring():
            return fixed_secret_service(), KeyringError

        def get(self):
            from communityai_desktop.credentials import CredentialMissingError

            backend, _ = self._keyring()
            value = backend.get_password(self.service, self.account)
            if value is None:
                raise CredentialMissingError()
            # Preserve authoritative malformed content vs thrown backend error.
            # Only the admitted helper inspects this value; never return it.
            return value

    return FixedSecretService(profile.credential_service, profile.credential_account)


def _environment(profile):
    import pwd

    home = pwd.getpwuid(os.geteuid()).pw_dir
    anchor._require(str(profile.root.parent.parent) == home)
    runtime = anchor._runtime_directory()
    bus = (runtime / "bus").lstat()
    anchor._require(anchor.stat.S_ISSOCK(bus.st_mode) and bus.st_uid == os.geteuid())
    return dict(
        PATH="/usr/bin:/bin",
        LANG="C.UTF-8",
        LC_ALL="C.UTF-8",
        HOME=home,
        XDG_RUNTIME_DIR=str(runtime),
        DBUS_SESSION_BUS_ADDRESS="unix:path=" + str(runtime / "bus"),
    )


def protect_parent_memory():
    from communityai_anchor.linux_secret_service import protect_credential_memory

    protect_credential_memory()


def _lockdown():
    import ctypes
    import signal

    parent = os.getppid()
    anchor._require(parent > 1)
    libc = ctypes.CDLL(None, use_errno=True)
    anchor._require(libc.prctl(1, signal.SIGKILL, 0, 0, 0) == 0)  # PR_SET_PDEATHSIG
    anchor._require(os.getppid() == parent)
    protect_parent_memory()


def credential_helper_main(profile=None):
    """Fixed entry before profile preparation; never emit backend stdout/errors."""
    output = null = directory = None
    try:
        output = os.dup(1)
        os.set_inheritable(output, False)
        null = os.open(os.devnull, os.O_WRONLY | os.O_CLOEXEC)
        os.dup2(null, 1)
        os.dup2(null, 2)
        _lockdown()
        if profile is None:
            anchor._require(sys.platform.startswith("linux") and getattr(sys, "frozen", False) is True)
            from communityai_desktop.profiles import VolunteerProfile

            profile = VolunteerProfile.for_current_user()
            expected = _environment(profile)
            # Frozen bootloader runtime internals are not backend overrides.
            anchor._require(all(os.environ.get(k) == v for k, v in expected.items()))
            anchor._require(
                all(
                    k in expected or k.startswith("_PYI_") or k in {"LD_LIBRARY_PATH", "LD_LIBRARY_PATH_ORIG"}
                    for k in os.environ
                )
            )
        deadline, raw = time.monotonic() + 5, bytearray()
        while True:
            remaining = deadline - time.monotonic()
            anchor._require(remaining > 0 and select.select([0], [], [], remaining)[0])
            part = os.read(0, MAX_MESSAGE + 1 - len(raw))
            if not part:
                break
            raw.extend(part)
            anchor._require(len(raw) <= MAX_MESSAGE)
        request = _decode(raw)
        directory = anchor.cg._open_root(str(profile.root / "anchor"))
        _admit_helper(profile, request, directory=directory)
        store = _native_store(profile)
        digest = None
        if request["operation"] == "get":
            from communityai_desktop.credentials import CredentialMissingError

            try:
                value = store.get()
            except CredentialMissingError:
                status = "missing"
            else:
                try:
                    _key(value)
                except anchor.RecoverableStateError:
                    status = "invalid"
                else:
                    digest = _digest(value.encode())
                    status = "found"
        else:
            from communityai_desktop.credentials import CredentialMissingError

            # Refuse to overwrite any existing account, including one whose
            # value happens to match. Recovered pending intent is GET-only.
            try:
                store.get()
            except CredentialMissingError:
                pass
            else:
                raise CredentialExecutionError()
            _admit_helper(profile, request, directory=directory)
            anchor._require(not os.path.lexists(profile.data_dir / "control-api.key"))
            store.set(request["secret"])
            status = "written"
        _admit_helper(profile, request, directory=directory)
        response = _json(
            dict(version=1, nonce=request["nonce"], operation=request["operation"], status=status, digest=digest)
        )
        anchor._require(len(response) <= MAX_MESSAGE and os.write(output, response) == len(response))
        return 0
    except Exception:
        return 75
    finally:
        for descriptor in (directory, null, output):
            if descriptor is not None:
                os.close(descriptor)
