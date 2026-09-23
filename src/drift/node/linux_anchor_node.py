"""Single asynchronous owner for exact node birth and checked whole-tree drain.

No model readiness or installer permission follows from this channel. The fixed
launcher factory is internal; callers never supply executable, path or env data.
"""

from __future__ import annotations

import copy
import os
import threading
import time
from pathlib import Path
from uuid import uuid4

from drift.node import linux_anchor as anchor, linux_anchor_resources as resources, linux_cgroup_process as native
from drift.node.linux_anchor_entry import NODE_TOKEN_ENV
from drift.node.linux_anchor_state import AnchorState, PrivateLease, node_lease
from drift.node.linux_node_identity import make_identity
from drift.node.resource_recovery import RecoverableStateError
from drift.node.resource_reservations import ResourceReservationManager

GRACEFUL_NODE_DRAIN_SECONDS = 3030.0


class AnchorNode:
    def __init__(
        self,
        layout,
        profile_root,
        launch_factory,
        *,
        initialize=False,
        bootstrap=None,
        credential_executor=None,
        retirement_hook=None,
        quiescence_hook=None,
        activate=True,
    ):
        anchor._require(type(initialize) is bool and type(activate) is bool)
        self._initialize_fields(
            layout,
            profile_root,
            launch_factory,
            bootstrap=bootstrap,
            credential_executor=credential_executor,
            retirement_hook=retirement_hook,
            quiescence_hook=quiescence_hook,
        )
        try:
            layout.validate()
            anchor._require(layout.service.pid == os.getpid())
            self._state = AnchorState(self.root, layout, initialize=initialize)
            self._lease = node_lease(self.root, create=initialize)
            if initialize:
                resources.create_directories(self.root, self._state.binding)
            else:
                self._resources = resources.read_resources(self.root, self._state.binding)
            identities = None if self._resources is None else self._resources[0]["identities"]
            self._manager = ResourceReservationManager(
                self.root / "node" / "resource-reservations",
                loading_protocol=True,
                recovery_protocol=True,
                worker_cgroup_root=self.layout.profiles[3].root,
                storage_binding=None
                if identities is None
                else dict(directory=tuple(identities["journal"]), lease=tuple(identities["admission"])),
            )
            if initialize:
                anchor.cg.verify_cgroup_tree_empty(self.layout.profiles[2].root)
                anchor._require(self._manager.recover())
                with self._manager.drain_guard():
                    self._resources = resources.create_resources(self.root, self._state.binding)
                    if self._bootstrap is not None:
                        self._bootstrap.bind(self._state, self._ownership, initialize=True)
            elif self._bootstrap is not None:
                self._bootstrap.bind(self._state, self._ownership)
            self._publish(blocked=True)
            if activate:
                self.activate_owner()
        except BaseException:
            if self._state is not None:
                self._state.close()
            if self._lease is not None:
                self._lease.close()
            raise

    def _initialize_fields(
        self,
        layout,
        profile_root,
        launch_factory,
        *,
        bootstrap,
        credential_executor,
        retirement_hook,
        quiescence_hook,
    ):
        anchor._require(
            (retirement_hook is None or callable(retirement_hook))
            and (quiescence_hook is None or callable(quiescence_hook))
        )
        self.layout, self.root, self.launch_factory = layout, Path(profile_root), launch_factory
        self._bootstrap = bootstrap
        self._credential_executor = credential_executor
        self._retirement_hook = retirement_hook
        self._quiescence_hook = quiescence_hook
        self._credential_process = None
        if credential_executor is not None:
            from communityai_anchor.linux_anchor_credentials import protect_parent_memory

            # The parent generates/serializes the pending key. Protect it
            # before any owner thread or credential operation starts.
            protect_parent_memory()
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._wake, self._cancel, self.finished = threading.Event(), threading.Event(), threading.Event()
        self._stop = self._closed = self._fatal = False
        self._adopted = False
        self._cleanup_attempted = False
        self._pending = self._accepted = None
        self._active = ("checking", 0, None)
        self._resources = None
        self._process = self._reader = self._manager = self._lease = self._state = None
        self._leaf_fd = self._leaf = None
        self._runner = None
        self._activation_attempted = False
        self._clean_adopt = False
        self._cached = dict(
            revision=0,
            phase="checking",
            generation=None,
            request_id=None,
            operation=None,
            drain_complete=False,
            api_ready=False,
            api_identity=None,
            maintenance=False,
            pending_request_id=None,
        )

    @classmethod
    def adopt_inactive(
        cls,
        layout,
        profile_root,
        launch_factory,
        *,
        state,
        node_lifetime_lease,
        reservation_manager,
        reservation_guard,
        resources_record,
        bootstrap=None,
        credential_executor=None,
        retirement_hook=None,
        quiescence_hook=None,
    ):
        """Adopt one complete existing-profile authority bundle without starting work.

        Ownership transfers only when this method returns successfully. The caller
        must keep the admission guard active for the complete call and release its
        short-lived locks before invoking :meth:`activate_owner`.
        """
        anchor._require(type(state) is AnchorState)
        anchor._require(type(node_lifetime_lease) is PrivateLease and node_lifetime_lease.created is False)
        anchor._require(type(reservation_manager) is ResourceReservationManager)
        anchor._require(type(resources_record) is tuple and len(resources_record) == 2)
        reservation_guard.validate(reservation_manager)
        reservation_guard.require_empty()
        layout.validate()
        anchor._require(layout.service.pid == os.getpid() and state.layout is layout)
        state.validate()
        anchor._require(type(state.lease) is PrivateLease and state.lease.created is False)
        node_lifetime_lease.validate()
        root = Path(profile_root)
        anchor._require(state.lease.root == root and node_lifetime_lease.root == root)
        anchor._require(state.lease.path == root / "anchor-state.lock")
        anchor._require(node_lifetime_lease.path == root / "node-lifetime.lock")
        anchor._require(resources.read_resources(root, state.binding) == resources_record)
        identities = resources_record[0]["identities"]
        expected_storage = dict(directory=tuple(identities["journal"]), lease=tuple(identities["admission"]))
        anchor._require(reservation_manager._directory == root / "node" / "resource-reservations")
        anchor._require(reservation_manager._storage_binding == expected_storage)
        anchor._require(reservation_manager._loading_protocol and reservation_manager._recovery_protocol)
        anchor._require(reservation_manager._worker_cgroup_root == layout.profiles[3].root)

        owner = cls.__new__(cls)
        owner._initialize_fields(
            layout,
            root,
            launch_factory,
            bootstrap=bootstrap,
            credential_executor=credential_executor,
            retirement_hook=retirement_hook,
            quiescence_hook=quiescence_hook,
        )
        owner._state = state
        owner._lease = node_lifetime_lease
        owner._manager = reservation_manager
        owner._resources = resources_record
        owner._ownership()
        if bootstrap is not None:
            bootstrap.bind(state, owner._ownership)
        if quiescence_hook is not None:
            value = state.value
            anchor._require(
                value["schema_version"] == 2 and value["phase"] == "idle" and value["quiescence"] is not None
            )
            owner._adopt_empty_record()
            observed = quiescence_hook(owner, validate_only=True)
            anchor._require(observed == state.value)
            owner._clean_adopt = True
        owner._publish(blocked=True)
        reservation_guard.require_empty()
        reservation_guard.validate(reservation_manager)
        return owner

    def activate_owner(self):
        """Start the asynchronous owner exactly once after short guards are released."""
        with self._close_lock:
            anchor._require(
                not self._closed and not self._stop and not self._activation_attempted and self._runner is None
            )
            self._activation_attempted = True
            self._integrity()
            runner = threading.Thread(target=self._run, name="anchor-node-owner", daemon=True)
            self._runner = runner
            try:
                runner.start()
            except BaseException:
                self._fatal = True
                self._runner = None
                raise

    def snapshot(self):
        # Point-in-time completion of the last drain, never a continuing proof
        # of emptiness or a maintenance lease. No filesystem/process I/O here.
        with self._lock:
            result = copy.deepcopy(self._cached)
            command = self._pending or self._active
            result["pending_request_id"] = None if command is None else command[2]
            result["drain_complete"] = result["drain_complete"] and command is None and not self._closed
            return result

    def _publish(self, *, blocked=False):
        value = self._state.value
        generation = value["generation"]
        sealed = self._quiescence_hook is None or (value["schema_version"] == 2 and value["quiescence"] is not None)
        result = dict(
            revision=value["revision"],
            phase="blocked" if blocked else value["phase"],
            generation=None if generation is None else {key: generation[key] for key in ("id", "pid", "start_ticks")},
            request_id=value["request_id"],
            operation=value["operation"],
            drain_complete=value["phase"] == "idle"
            and sealed
            and not blocked
            and not self._fatal
            and not self._state.poisoned,
            api_ready=False,
            api_identity=make_identity(value["binding"], generation),
            maintenance=False,
            pending_request_id=None,
        )
        with self._lock:
            self._cached = result

    def _write(self, **changes):
        self._state.write(self._state.value["revision"], **changes)
        self._publish()

    def submit(self, operation, revision, request_id, *, condition=None):
        anchor._require(type(operation) is str and operation in {"start", "drain"})
        anchor._require(type(revision) is int and 0 <= revision < 2**63)
        anchor._require(type(request_id) is str and anchor.re.fullmatch("[0-9a-f]{32}", request_id) is not None)
        anchor._require(type(condition) is dict and set(condition) == {"generation", "pending_request_id"})
        anchor._require(
            all(
                value is None or (type(value) is str and anchor.re.fullmatch("[0-9a-f]{32}", value) is not None)
                for value in condition.values()
            )
        )
        with self._lock:
            anchor._require(not self._stop and not self._fatal and not self._state.poisoned)
            command = (operation, revision, request_id)
            if command in (self._accepted, self._active, self._pending):
                return
            # Retrying a durably recorded operation cannot execute it twice
            # after a same-invocation owner reopen.
            if request_id == self._cached["request_id"]:
                anchor._require(operation == self._cached["operation"])
                return
            if condition is not None:
                generation = self._cached["generation"]
                current = self._pending or self._active
                anchor._require(
                    condition
                    == dict(
                        generation=None if generation is None else generation["id"],
                        pending_request_id=None if current is None else current[2],
                    )
                )
            anchor._require(revision == self._cached["revision"] and self._pending is None)
            anchor._require(self._active is None or (self._active[0] == "start" and operation == "drain"))
            anchor._require(
                operation != "start" or (self._cached["phase"] == "idle" and self._cached["drain_complete"])
            )
            if operation == "drain":
                self._cancel.set()
            self._accepted = self._pending = command
            self._cached["drain_complete"] = False
            self._wake.set()

    def _integrity(self):
        try:
            self._ownership()
            self._state.validate()
            if self._bootstrap is not None:
                self._bootstrap.validate()
        except Exception:
            self._fatal = True
            raise

    def _ownership(self):
        """Independent exclusion/storage proof usable after state write poison."""
        self.layout.validate()
        self._lease.validate()
        anchor._require(resources.read_resources(self.root, self._state.binding) == self._resources)

    def _clean_proof(self):
        self._integrity()
        anchor._require(not self._fatal and not self._state.poisoned)
        if self._leaf is not None:
            self._validate_leaf()
        anchor.cg.verify_cgroup_tree_empty(self.layout.profiles[2].root)

    def _has_quiescent_idle(self):
        if self._quiescence_hook is None:
            return False
        value = self._state.value
        return (
            value["schema_version"] == 2
            and value["phase"] == "idle"
            and value["quiescence"] is not None
            and self._process is None
            and self._reader is None
            and self._credential_process is None
        )

    def _quiescent_idle(self, *, validate_only=False, request_id=None):
        """Revalidate or atomically refresh idle under a fresh empty guard."""
        anchor._require(self._quiescence_hook is not None and type(validate_only) is bool)
        deadline = time.monotonic() + 10
        with self._manager.drain_guard(cancelled=lambda: time.monotonic() >= deadline):
            self._clean_proof()
            if validate_only:
                anchor._require(self._has_quiescent_idle() and request_id is None)
                observed = self._quiescence_hook(self, validate_only=True)
            else:
                observed = self._quiescence_hook(self, request_id=request_id)
            anchor._require(observed == self._state.value and self._has_quiescent_idle())
            self._clean_proof()
            self._publish()

    def _validate_leaf(self):
        try:
            self.layout.validate()
            self._lease.validate()
            anchor._require(self._leaf is not None and self._leaf_fd is not None)
            anchor._require(anchor.cg._observe_root(self._leaf.root, self._leaf_fd) == self._leaf)
            anchor._require(anchor.cg.validate_cgroup_profile(self._leaf.root) == self._leaf)
        except Exception:
            self._fatal = True
            raise

    def _adopt_empty_record(self):
        self._integrity()
        anchor.cg.verify_cgroup_tree_empty(self.layout.profiles[2].root)
        generation = self._state.value["generation"]
        if generation is None:
            descriptor = anchor.cg._open_root(self.layout.profiles[2].root)
            try:
                anchor._require(not anchor._subgroups(descriptor))
            finally:
                os.close(descriptor)
            self._adopted = True
            return
        # A birth intent without a bound native leaf cannot adopt a pathname
        # that might have appeared during an interrupted creation.
        if generation["cgroup"] is None:
            descriptor = anchor.cg._open_root(self.layout.profiles[2].root)
            try:
                if "node-" + generation["id"] in anchor._subgroups(descriptor):
                    self._fatal = True
                    raise RecoverableStateError()
            finally:
                os.close(descriptor)
            self._adopted = True
            return
        self._leaf = anchor.cg.LinuxCgroupProfile.from_json(generation["cgroup"])
        self._leaf_fd = anchor.cg._open_root(self._leaf.root)
        self._validate_leaf()
        self._adopted = True

    def _start(self, request_id):
        with self._lock:
            if self._stop or (self._pending is not None and self._pending[0] == "drain"):
                return
            self._cancel.clear()
        with self._manager.drain_guard(cancelled=self._cancel.is_set):
            self._integrity()
            anchor._require(self._state.value["phase"] == "idle")
            anchor.cg.verify_cgroup_tree_empty(self.layout.profiles[2].root)
            with self._lock:
                if self._stop or (self._pending is not None and self._pending[0] == "drain"):
                    return
            # A clean-retirement seal belongs to a completed prior lifecycle.
            # Only checked replacement recovery may consume it; Start never
            # deletes or resets that authority to make new work possible.
            anchor._require(not os.path.lexists(self.root / "anchor" / "retirement.json"))
            # Retain proved-empty directories. The fresh journal proof remains
            # locked through durable Start intent, never inferred from cache.
            self._release_leaf()
            generation = dict(id=uuid4().hex, token=uuid4().hex, cgroup=None, pid=None, start_ticks=None)
            self._write(phase="starting", generation=generation, request_id=request_id, operation="start")
            # The same generation leaf contains credential helpers and, only
            # after checked empty cleanup/readiness, the actual node payload.
            parent = anchor.cg._open_root(self.layout.profiles[2].root)
            try:
                anchor._require(
                    anchor.cg._observe_root(self.layout.profiles[2].root, parent) == self.layout.profiles[2]
                )
                name = "node-" + generation["id"]
                os.mkdir(name, mode=0o700, dir_fd=parent)
                self._leaf_fd = anchor.cg._open_directory(name, parent=parent)
                self._leaf = anchor.cg.validate_cgroup_profile(self.layout.profiles[2].root + "/" + name)
            except Exception:
                self._fatal = True
                raise
            finally:
                os.close(parent)
            generation["cgroup"] = self._leaf.to_json()
            self._write(generation=generation)
            if self._bootstrap is not None:
                try:
                    options = {}
                    if self._credential_executor is not None:
                        options["credential_call"] = lambda *args, **kwargs: self._credential_executor(
                            self, *args, **kwargs
                        )
                    self._bootstrap.prepare(self._state.value, cancelled=self._cancel.is_set, **options)
                except Exception:
                    if self._bootstrap.poisoned:
                        self._fatal = True
                    raise
                self._integrity()
                anchor.cg.verify_cgroup_tree_empty(self.layout.profiles[2].root)
            self._validate_leaf()
            anchor._require(self._credential_process is None and not anchor._subgroups(self._leaf_fd))
            if self._cancel.is_set():
                raise RecoverableStateError("cleanup_pending")
        command, environment, directory = self.launch_factory(self.layout.profiles[3].root)
        environment = dict(environment, **{NODE_TOKEN_ENV: generation["token"]})
        self._validate_leaf()
        if self._cancel.is_set():
            raise RecoverableStateError("cleanup_pending")
        process = native.spawn(self._leaf_fd, command, env=environment, cwd=directory)
        self._process = process
        self._cleanup_attempted = False
        anchor._require(process.cgroup_identity == self._leaf.root_identity)
        ticks, group = anchor._process(process.pid)
        anchor._require(group == self.layout.service.control_group + "/nodes/node-" + generation["id"])
        generation.update(pid=process.pid, start_ticks=ticks)
        self._write(generation=generation)  # durable exact binding BEFORE gate
        self._reader = threading.Thread(target=self._discard_output, args=(process,), daemon=True)
        self._reader.start()
        self._integrity()
        self._validate_leaf()
        with self._lock:
            if self._cancel.is_set() or self._stop:
                raise RecoverableStateError("cleanup_pending")
            process.release_gate()
        process.await_exec(cancel=self._cancel)
        if self._cancel.is_set():
            raise RecoverableStateError("cleanup_pending")
        self._write(phase="running")

    @staticmethod
    def _discard_output(process):
        try:
            while process.stdout.read(4096):
                pass
        except (OSError, ValueError):
            pass

    def _release_leaf(self):
        if self._leaf_fd is not None:
            os.close(self._leaf_fd)
            self._leaf_fd = None
        self._leaf = None

    def _kill_profile(self, profile, *, deadline=None):
        # A damaged journal withholds acknowledgement, not Stop. The exact
        # service-owned subtree and lifetime lease are independent authority.
        self.layout.validate()
        self._lease.validate()
        descriptor = anchor.cg._open_root(profile.root)
        try:
            anchor._require(anchor.cg._observe_root(profile.root, descriptor) == profile)
            control = anchor.cg._control(descriptor, "cgroup.kill", write=True)
            try:
                anchor._require(os.write(control, b"1\n") == 2)
            finally:
                os.close(control)
            if deadline is None:
                deadline = time.monotonic() + 5
            while anchor.cg._events(anchor.cg._read_control(descriptor, "cgroup.events")):
                anchor._require(time.monotonic() < deadline)
                time.sleep(0.05)
            anchor._require(anchor.cg._observe_root(profile.root, descriptor) == profile)
            self.layout.validate()
        finally:
            os.close(descriptor)

    def _cleanup(self, *, graceful=False, final_phase="idle", request_id=None):
        self._cleanup_attempted = True

        def attempt(action):
            try:
                action()
            except Exception:
                self._fatal = True

        if not self._adopted:
            attempt(self._adopt_empty_record)
        if not self._fatal and not self._state.poisoned:
            try:
                self._write(phase="draining")
            except Exception:
                # A lost state acknowledgement cannot cancel independent,
                # exact tree/journal recovery. It only withholds a clean ack.
                self._fatal = True
        process = self._process
        credential_process = self._credential_process

        def graceful_stop():
            if process.poll() is None:
                process.terminate()
                deadline = time.monotonic() + GRACEFUL_NODE_DRAIN_SECONDS
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)

        if process is not None and graceful:
            attempt(graceful_stop)
        # Must-run independent stages. Handle/leaf/journal faults never bypass
        # either whole-root stop attempt; each proves its own live authority.
        for profile in self.layout.profiles[2:]:
            attempt(lambda: self._kill_profile(profile))
        if credential_process is not None:
            attempt(credential_process.kill)
            attempt(lambda: credential_process.wait(timeout=5))
            if credential_process.stdout is not None:
                attempt(credential_process.stdout.close)
        if process is not None:
            attempt(lambda: process.wait(timeout=5))
        if self._leaf is not None:
            attempt(self._validate_leaf)
        anchor.cg.verify_cgroup_tree_empty(self.layout.profiles[2].root)
        self._ownership()
        deadline = time.monotonic() + 10
        anchor._require(self._manager.recover(cancelled=lambda: time.monotonic() >= deadline))
        with self._manager.drain_guard(cancelled=lambda: time.monotonic() >= deadline):
            self._clean_proof()
            if self._reader is not None:
                self._reader.join(2)
                anchor._require(not self._reader.is_alive())
            if process is not None and process.stdout is not None:
                process.stdout.close()
            self._process = self._reader = None
            self._credential_process = None
            if final_phase == "idle" and self._quiescence_hook is not None:
                observed = self._quiescence_hook(self, request_id=request_id)
                anchor._require(observed == self._state.value and self._has_quiescent_idle())
                self._clean_proof()
                self._publish()
            else:
                self._write(phase=final_phase)
        # Keep the empty journal recovery owner and exact leaf alive. Their
        # lifetime ownership ends only after final checked shutdown.

    def _run(self):
        try:
            try:
                if self._clean_adopt:
                    try:
                        self._quiescent_idle(validate_only=True)
                    except Exception:
                        # A damaged/stale seal never skips independent containment
                        # cleanup merely because adoption began from recorded idle.
                        try:
                            self._cleanup()
                        except Exception:
                            self._publish(blocked=True)
                    finally:
                        self._clean_adopt = False
                else:
                    try:
                        self._cleanup()
                    except Exception:
                        self._publish(blocked=True)
            finally:
                with self._lock:
                    self._active = None
            while True:
                self._wake.wait(0.1)
                self._wake.clear()
                with self._lock:
                    command, self._pending = self._pending, None
                    stopping = self._stop
                    if stopping or command is not None:
                        self._active = command or ("checking", 0, None)
                if not stopping and command is None:
                    dead = False
                    if self._process is not None and not self._cleanup_attempted:
                        try:
                            dead = self._process.poll() is not None
                        except Exception:
                            self._fatal = dead = True
                    if not dead:
                        continue
                    with self._lock:
                        self._active = ("checking", 0, None)
                try:
                    if stopping:
                        if self._has_quiescent_idle():
                            self._quiescent_idle(validate_only=True)
                        else:
                            self._cleanup(graceful=True)
                        return
                    if command is not None:
                        operation, _revision, request_id = command
                        if operation == "start":
                            self._start(request_id)
                        elif self._has_quiescent_idle():
                            # Repeated idle Drain is one fresh proof plus one
                            # atomic metadata+seal write, never an unsealed
                            # draining transition.
                            self._quiescent_idle(request_id=request_id)
                        else:
                            self._write(request_id=request_id, operation="drain")
                            self._cleanup(graceful=True, request_id=request_id)
                    else:
                        self._cleanup()
                except Exception:
                    try:
                        failed_start = command is not None and command[0] == "start" and not self._cancel.is_set()
                        if self._bootstrap is not None and self._bootstrap.retryable and not self._bootstrap.poisoned:
                            failed_start = False
                        drain_request = command[2] if command is not None and command[0] == "drain" else None
                        self._cleanup(
                            final_phase="blocked" if failed_start else "idle",
                            request_id=drain_request,
                        )
                    except Exception:
                        self._publish(blocked=True)
                    if stopping:
                        return
                finally:
                    with self._lock:
                        self._active = None
                        if self._pending is not None:
                            self._wake.set()
        finally:
            self.finished.set()

    def request_shutdown(self):
        with self._lock:
            self._stop = True
            self._cancel.set()
            self._wake.set()

    def abort_inactive(self, timeout=2.0):
        """Release an owner that never started; no retirement receipt is created."""
        with self._close_lock:
            return self._abort_inactive(timeout)

    def _abort_inactive(self, timeout):
        if self._closed:
            return True
        anchor._require(self._runner is None)
        self.request_shutdown()
        try:
            anchor._require(self._manager.close(timeout))
            self._release_leaf()
            self._state.close()
            self._lease.close()
            self._closed = True
            self.finished.set()
            return True
        except Exception:
            self._publish(blocked=True)
            return False

    def close(self, timeout=0):
        with self._close_lock:
            return self._close(timeout)

    def _close(self, timeout):
        if self._closed:
            return True
        if self._runner is None:
            return self._abort_inactive(timeout)
        self.request_shutdown()
        self._runner.join(timeout)
        if self._runner.is_alive():
            return False
        try:
            anchor._require(self._cached["drain_complete"] and self._pending is None and self._active is None)
            self._clean_proof()
            anchor._require(self._manager.close())
            # Final identity and empty-tree/journal proof extends through lease
            # release, not merely through the earlier cached idle publication.
            with self._manager.drain_guard():
                self._clean_proof()
                if self._retirement_hook is not None:
                    self._retirement_hook(self)
                    self._clean_proof()
                self._release_leaf()
                self._state.close()
                self._lease.close()
            self._closed = True
            return True
        except Exception:
            self._publish(blocked=True)
            return False
