"""Linux volunteer desktop ownership through the fixed anchor, never Popen/TCP."""

from __future__ import annotations

import threading
import time
from uuid import uuid4

from communityai_desktop.client import NodeClient, NodeClientError
from communityai_desktop.lifecycle import GRACEFUL_NODE_SHUTDOWN_TIMEOUT, NodeLifecycleError

from communityai_anchor.linux_anchor_control import control_anchor
from communityai_anchor.linux_anchor_entry import inspect_profile_entry
from communityai_anchor.linux_node_channel import NodeControlTransport
from communityai_anchor.resource_recovery import RecoverableStateError

SETUP_ERROR = (
    "The Linux test profile needs a verified running anchor service and intact provisioned state. "
    "Use the checked installer/recovery workflow; do not delete profile files."
)
MAINTENANCE_ERROR = (
    "Linux test-profile update/removal requires checked anchor maintenance, which is not available yet. "
    "Stopping the desktop or node does not authorize replacing application files."
)


def _stable_observation(profile, *, expected=None, sleeper=time.sleep):
    # Durable state is written before cached publication. A normal transition
    # between these reads is not corruption. No mutation is authorized by a
    # failed attempt; identity/resource changes never replace an expected proof.
    for _ in range(12):
        try:
            receipt = control_anchor()
            proof = inspect_profile_entry(profile.root, receipt)
            after = control_anchor()
            if any(after[key] != receipt[key] for key in ("service", "layout_digest", "node")):
                raise RecoverableStateError()
            if inspect_profile_entry(profile.root, after) != proof:
                raise RecoverableStateError()
        except (RecoverableStateError, OSError):
            sleeper(0.02)
            continue
        if expected is not None and (receipt["service"], receipt["layout_digest"], proof) != expected:
            raise NodeLifecycleError(SETUP_ERROR)
        return receipt, proof
    raise NodeLifecycleError(SETUP_ERROR)


def prepare_anchored_profile(profile):
    """Prove existing anchor/storage before any profile/keyring/instance mutation."""
    try:
        receipt, proof = _stable_observation(profile)
        if receipt["node"]["phase"] not in {"idle", "starting", "running", "draining"}:
            raise RecoverableStateError()
        profile.prepare(existing_anchor=True)
        after, _ = _stable_observation(profile, expected=(receipt["service"], receipt["layout_digest"], proof))
        return after, proof
    except (RecoverableStateError, OSError, ValueError):
        raise NodeLifecycleError(SETUP_ERROR) from None


def _generation(node):
    return None if node["generation"] is None else node["generation"]["id"]


def _condition(node):
    return dict(generation=_generation(node), pending_request_id=node["pending_request_id"])


class LinuxAnchorLifecycle:
    """Node Start is paused; sharing Start/Pause remain actual node API calls."""

    def __init__(
        self,
        profile,
        credential_store,
        *,
        prepared=None,
        startup_timeout=300.0,
        client_timeout=5.0,
        poll_interval=0.2,
        shutdown_timeout=GRACEFUL_NODE_SHUTDOWN_TIMEOUT + 30.0,
        client_factory=NodeClient,
        clock=time.monotonic,
        sleeper=time.sleep,
    ):
        if any(value <= 0 for value in (startup_timeout, client_timeout, poll_interval, shutdown_timeout)):
            raise ValueError("Anchor lifecycle timeouts must be positive")
        self.profile, self.credential_store = profile, credential_store
        self.node_url, self.data_dir, self.config_path = profile.node_url, profile.data_dir, profile.config_path
        self.startup_timeout, self.client_timeout = startup_timeout, client_timeout
        self.poll_interval, self.shutdown_timeout = poll_interval, shutdown_timeout
        self._client_factory, self._clock, self._sleeper = client_factory, clock, sleeper
        self._initial, self._proof = prepared if prepared is not None else prepare_anchored_profile(profile)
        self._target = None
        self._engaged = False
        self._start_command = self._drain_command = None
        self._closing = threading.Event()
        self._ensure_lock, self._drain_lock, self._lock = threading.Lock(), threading.Lock(), threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._close_error = None
        self._drain_error = None

    def _observe(self):
        receipt, _ = _stable_observation(
            self.profile,
            expected=(
                self._initial["service"],
                self._initial["layout_digest"],
                self._proof,
            ),
            sleeper=self._sleeper,
        )
        return receipt

    def _send(self, operation, command):
        # Keep the exact request ID/revision/condition on uncertain replies.
        # Retrying is idempotent; a changed condition cannot hit another Start.
        try:
            control_anchor(operation, **command)
        except RecoverableStateError:
            return False
        return True

    @staticmethod
    def _command(node):
        return dict(revision=node["revision"], request_id=uuid4().hex, condition=_condition(node))

    def ensure_client(self):
        with self._ensure_lock:
            with self._lock:
                if self._drain_error is not None:
                    raise self._drain_error
                if self._closing.is_set():
                    raise NodeLifecycleError("The anchored node supervisor is closed")
            try:
                return self._connect()
            except (RecoverableStateError, OSError) as exc:
                raise NodeLifecycleError(SETUP_ERROR) from exc

    def _connect(self):
        deadline = self._clock() + self.startup_timeout
        transition_allowance = False
        secret = None
        while self._clock() < deadline:
            if self._closing.is_set():
                raise NodeLifecycleError("The anchored node supervisor is closing")
            receipt = self._observe()
            node = receipt["node"]
            with self._lock:
                if self._closing.is_set():
                    raise NodeLifecycleError("The anchored node supervisor is closing")
                start_command = self._start_command
            # Existing/transitioning generations can be rebuilding after a
            # bounded drain. Extend once on observation, even when the first
            # snapshot preceded publication. A fresh owned Start keeps its
            # normal API deadline.
            if not transition_allowance and (
                node["phase"] == "draining" or (start_command is None and node["phase"] in {"starting", "running"})
            ):
                deadline += self.shutdown_timeout
                transition_allowance = True
            if node["phase"] == "blocked":
                raise NodeLifecycleError(SETUP_ERROR)
            if (
                start_command is not None
                and node["request_id"] == start_command["request_id"]
                and node["operation"] == "start"
                and node["phase"] == "idle"
                and node["drain_complete"]
                and node["pending_request_id"] is None
            ):
                with self._lock:
                    self._start_command = None
                    self._target = None
                    self._engaged = False
                raise NodeLifecycleError(
                    "The anchor could not complete setup. Unlock the native credential store and verify "
                    "the installed catalog package before retrying. No node was started."
                )
            if node["phase"] == "idle" and node["drain_complete"] and start_command is None:
                self.profile.validate_config()
                receipt = self._observe()
                node = receipt["node"]
                if node["phase"] != "idle" or not node["drain_complete"]:
                    raise NodeLifecycleError("The anchored node changed before Start; reconnect")
                with self._lock:
                    if self._closing.is_set():
                        raise NodeLifecycleError("The anchored node supervisor is closing")
                    self._start_command = self._command(node)
                    self._target = _generation(node)
                    self._engaged = True
                    self._send("start", self._start_command)
            elif start_command is not None and node["request_id"] != start_command["request_id"]:
                if node["pending_request_id"] != start_command["request_id"]:
                    if node["revision"] != start_command["revision"] or _condition(node) != start_command["condition"]:
                        raise NodeLifecycleError("The anchored Start was superseded; reconnect")
                    with self._lock:
                        if self._closing.is_set():
                            raise NodeLifecycleError("The anchored node supervisor is closing")
                        self._send("start", start_command)
            elif node["phase"] in {"starting", "running"}:
                generation = _generation(node)
                with self._lock:
                    if self._closing.is_set():
                        raise NodeLifecycleError("The anchored node supervisor is closing")
                    if start_command is not None:
                        self._target = generation
                    elif self._target is not None and self._target != generation:
                        raise NodeLifecycleError("The anchored node generation changed; reconnect")
                    else:
                        self._target = generation
                    self._engaged = True
                if node["phase"] == "running" and node["pending_request_id"] is None:
                    if secret is None:
                        secret = self.credential_store.get()
                    client = self._client_factory(
                        self.node_url, secret, timeout=self.client_timeout, transport=NodeControlTransport(receipt)
                    )
                    try:
                        status = client.status()
                        if status.get("node_identity") != node["api_identity"]:
                            raise NodeClientError("The node returned a different generation")
                        after = self._observe()["node"]
                        if (
                            after["api_identity"] != node["api_identity"]
                            or after["phase"] != "running"
                            or after["pending_request_id"] is not None
                        ):
                            raise NodeClientError("The node changed while connecting")
                        with self._lock:
                            if self._closing.is_set():
                                raise NodeLifecycleError("The anchored node supervisor is closing")
                            if self._start_command is start_command:
                                self._start_command = None
                            return client
                    except NodeClientError:
                        pass  # Same-generation API/reload gap, never another Start.
            self._sleeper(self.poll_interval)
        with self._lock:
            engaged = self._engaged
        if engaged:
            self._drain()
            raise NodeLifecycleError("The anchored node API did not become ready; its node was drained")
        raise NodeLifecycleError("The anchored node API did not become ready; no node was started or stopped")

    def _may_drain(self, node):
        with self._lock:
            if not self._engaged:
                return False
            known = {
                command["request_id"] for command in (self._start_command, self._drain_command) if command is not None
            }
            if (
                node["phase"] == "idle"
                and node["pending_request_id"] is not None
                and node["pending_request_id"] not in known
            ):
                return False
            if _generation(node) == self._target:
                return True
            start = self._start_command
            return start is not None and (
                node["pending_request_id"] == start["request_id"]
                or (node["request_id"] == start["request_id"] and node["operation"] == "start")
            )

    def _drain(self):
        with self._drain_lock:
            with self._lock:
                if self._drain_error is not None:
                    raise self._drain_error
                # Another authorized caller may have completed our exact Drain
                # while this caller waited. Retired ownership is idempotent,
                # never permission to observe/adopt/stop a successor node.
                if not self._engaged:
                    return
            try:
                return self._drain_owned()
            except (NodeLifecycleError, RecoverableStateError, OSError) as exc:
                error = exc if isinstance(exc, NodeLifecycleError) else NodeLifecycleError(SETUP_ERROR)
                with self._lock:
                    self._drain_error = error
                # Preserve target/commands/engagement on unverified cleanup.
                # Waiting close and later Retry cannot spend another window or
                # issue new commands from this failed ownership attempt.
                raise error from None

    def _drain_owned(self):
        deadline = self._clock() + self.shutdown_timeout
        while self._clock() < deadline:
            receipt = self._observe()
            node = receipt["node"]
            if self._drain_command is not None and node["request_id"] == self._drain_command["request_id"]:
                if node["drain_complete"] and node["pending_request_id"] is None:
                    with self._lock:
                        self._start_command = self._drain_command = None
                        self._target = None
                        self._engaged = False
                    return
                if node["phase"] == "blocked" and node["pending_request_id"] is None:
                    raise NodeLifecycleError("The anchored node retained unverified resources; recovery is required")
            if not self._may_drain(node):
                raise NodeLifecycleError("The node generation changed; refusing to stop a different node")
            if self._drain_command is None:
                if node["phase"] != "draining":
                    self._drain_command = self._command(node)
                    self._send("drain", self._drain_command)
            elif (
                node["pending_request_id"] != self._drain_command["request_id"]
                and node["request_id"] != self._drain_command["request_id"]
            ):
                if (
                    node["revision"] != self._drain_command["revision"]
                    or _condition(node) != self._drain_command["condition"]
                ):
                    # A canceled Start may publish its new identity before
                    # our Drain was accepted. Re-observe/authorize exactly.
                    self._drain_command = None
                elif not self._send("drain", self._drain_command) and node["phase"] == "blocked":
                    raise NodeLifecycleError(SETUP_ERROR)
            self._sleeper(self.poll_interval)
        raise NodeLifecycleError("The anchored node drain is unverified; recovery is required")

    def close(self):
        with self._close_lock:
            with self._lock:
                self._closing.set()
                if self._closed:
                    if self._close_error is not None:
                        raise self._close_error
                    return
                self._closed = True
                if not self._engaged:
                    return
            try:
                self._drain()
            except (RecoverableStateError, OSError):
                self._close_error = NodeLifecycleError(SETUP_ERROR)
                raise self._close_error from None
            except NodeLifecycleError as exc:
                self._close_error = exc
                raise
            # A terminal point-in-time node drain only; never maintenance.
