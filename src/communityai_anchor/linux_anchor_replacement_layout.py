"""Internal exact-layout construction for same-boot anchor replacement.

This module supplies kernel topology operations, not recovery authority. The
caller owns the durable transaction and all profile locks. Every manager-pruned
creation and the one allowed self-migration is bracketed by caller callbacks;
callback success is followed by fresh descriptor/path validation before the
kernel side effect.
"""

from __future__ import annotations

import os

from communityai_anchor import linux_anchor as anchor
from communityai_anchor.resource_recovery import RecoverableStateError

_KEY = object()


def _cancelled(callback):
    anchor._require(callback is None or callable(callback))
    if callback is not None and callback():
        raise RecoverableStateError("cleanup_pending")


def _profile(value):
    anchor._require(type(value) is anchor.cg.LinuxCgroupProfile)
    value.__post_init__()
    return value


def _expected_profiles(value):
    anchor._require(type(value) is tuple and len(value) == len(anchor._CHILDREN) + 1)
    result = tuple(_profile(item) for item in value)
    root = result[0]
    for name, child in zip(anchor._CHILDREN, result[1:]):
        anchor._require(child.root == root.root + "/" + name)
        anchor._require(
            child.root_identity[0] == root.root_identity[0]
            and child.mount_id == root.mount_id
            and child.mount_root == root.mount_root
            and child.mount_point == root.mount_point
            and child.namespaces == root.namespaces
            and child.uid == root.uid
        )
    return result


def _empty_child(descriptor):
    anchor._require(anchor.cg._read_control(descriptor, "cgroup.procs") == "")
    anchor._require(not anchor._subgroups(descriptor))
    anchor._require(
        not anchor.cg._events(
            anchor.cg._read_control(descriptor, "cgroup.events"),
            require_unfrozen=True,
        )
    )


class ReplacementAnchorLayout:
    """Pinned fixed layout prepared for one journaled self-migration."""

    def __init__(self, service, root, profiles, descriptors, *, pruned, key):
        anchor._require(key is _KEY)
        self.service = service
        self.root = root
        self.profiles = profiles
        self.root_profile = profiles[0]
        self._descriptors = descriptors
        self._pruned = pruned
        self._migration_started = False
        self._migrated = False
        self._closed = False

    def _descriptor(self, name=None):
        key = "root" if name is None else name
        descriptor = self._descriptors.get(key)
        anchor._require(not self._closed and descriptor is not None)
        return descriptor

    def _validate_pins(self, *, require_empty_children=False):
        anchor._require(anchor.cg._observe_root(self.root, self._descriptor()) == self.profiles[0])
        anchor._require(anchor._subgroups(self._descriptor()) == set(anchor._CHILDREN))
        for name, profile in zip(anchor._CHILDREN, self.profiles[1:]):
            descriptor = self._descriptor(name)
            anchor._require(anchor.cg._observe_root(profile.root, descriptor) == profile)
            reopened = anchor.cg._open_directory(name, parent=self._descriptor())
            try:
                anchor._require(anchor.cg._observe_root(profile.root, reopened) == profile)
            finally:
                os.close(reopened)
            if name == "anchor-control":
                anchor._require(not anchor._subgroups(descriptor))
            if require_empty_children:
                _empty_child(descriptor)

    def _validate_starting(self):
        anchor._require(anchor.inspect_service(starting=True) == self.service)
        anchor._require(anchor._delegated_path(self.service) == self.root)
        anchor._require(anchor.cg.validate_cgroup_profile(self.root) == self.root_profile)
        self._validate_pins(require_empty_children=self._pruned)
        anchor._require(anchor.cg._read_control(self._descriptor(), "cgroup.procs").split() == [str(self.service.pid)])
        anchor._require(anchor.cg._read_control(self._descriptor("anchor-control"), "cgroup.procs") == "")

    def _validate_active(self):
        anchor._require(anchor.inspect_service() == self.service)
        anchor._require(anchor._delegated_path(self.service) == self.root)
        self._validate_pins()
        anchor._require(anchor.cg._read_control(self._descriptor(), "cgroup.procs") == "")
        anchor._require(
            anchor.cg._read_control(self._descriptor("anchor-control"), "cgroup.procs").split()
            == [str(self.service.pid)]
        )
        anchor._require(anchor._observe_layout(self.service) == self.profiles)

    def validate_prepared(self):
        """Revalidate the exact starting topology without changing it."""
        try:
            anchor._require(not self._migrated)
            self._validate_starting()
        except RecoverableStateError:
            raise
        except Exception:
            raise RecoverableStateError() from None

    def migrate_self(self, *, begin_migration, record_migration, cancelled=None):
        """Move only this service after durable caller intent, then record it."""
        try:
            anchor._require(callable(begin_migration) and callable(record_migration))
            anchor._require(not self._closed and self.service.pid == os.getpid())
            if not self._migrated:
                _cancelled(cancelled)
                self._validate_starting()
                begin_migration(self.service, self.profiles)
                self._migration_started = True
                self._validate_starting()
                _cancelled(cancelled)
                anchor.cg._require_unfrozen(self._descriptor())
                anchor.cg._require_unfrozen(self._descriptor("anchor-control"))
                control = anchor.cg._control(self._descriptor("anchor-control"), "cgroup.procs", write=True)
                try:
                    payload = str(self.service.pid).encode("ascii")
                    anchor._require(os.write(control, payload) == len(payload))
                    self._migrated = True
                finally:
                    os.close(control)
            self._validate_active()
            # Once migration has occurred, cancellation cannot skip publication
            # of the exact observed result. The callback reconciles lost replies.
            record_migration(self.service, self.profiles)
            self._validate_active()
            return self
        except RecoverableStateError:
            raise
        except Exception:
            raise RecoverableStateError() from None

    def validate(self):
        try:
            anchor._require(self._migrated)
            self._validate_active()
        except RecoverableStateError:
            raise
        except Exception:
            raise RecoverableStateError() from None

    def receipt(self, nonce):
        self.validate()
        return anchor._receipt(self.service, self.profiles, nonce)

    def close(self):
        if self._closed:
            return
        self._closed = True
        descriptors = tuple(self._descriptors.values())
        self._descriptors = {}
        anchor.cg._close_descriptors(*descriptors)


def _open_exact_children(root, root_descriptor, profiles):
    descriptors = {}
    try:
        for name, profile in zip(anchor._CHILDREN, profiles[1:]):
            descriptor = anchor.cg._open_directory(name, parent=root_descriptor)
            descriptors[name] = descriptor
            anchor._require(anchor.cg._observe_root(profile.root, descriptor) == profile)
        return {"root": root_descriptor, **descriptors}
    except BaseException:
        anchor.cg._close_descriptors(*tuple(descriptors.values()))
        raise


def prepare_retained_layout(expected_profiles, *, cancelled=None):
    """Pin an exact retained layout without migration, deletion, or adoption."""
    root_descriptor = None
    layout = None
    try:
        _cancelled(cancelled)
        profiles = _expected_profiles(expected_profiles)
        service = anchor.inspect_service(starting=True)
        root = anchor._delegated_path(service)
        anchor._require(root == profiles[0].root)
        root_descriptor = anchor.cg._open_root(root)
        anchor._require(anchor.cg._observe_root(root, root_descriptor) == profiles[0])
        anchor._require(anchor._subgroups(root_descriptor) == set(anchor._CHILDREN))
        descriptors = _open_exact_children(root, root_descriptor, profiles)
        root_descriptor = None
        layout = ReplacementAnchorLayout(service, root, profiles, descriptors, pruned=False, key=_KEY)
        layout.validate_prepared()
        _cancelled(cancelled)
        layout.validate_prepared()
        return layout
    except BaseException as error:
        if layout is not None:
            layout.close()
        elif root_descriptor is not None:
            anchor.cg._close_descriptors(root_descriptor)
        if isinstance(error, RecoverableStateError) or not isinstance(error, Exception):
            raise
        raise RecoverableStateError() from None


def _child_profile(root_profile, name, value):
    profile = _profile(value)
    anchor._require(profile.root == root_profile.root + "/" + name)
    anchor._require(
        profile.root_identity[0] == root_profile.root_identity[0]
        and profile.mount_id == root_profile.mount_id
        and profile.mount_root == root_profile.mount_root
        and profile.mount_point == root_profile.mount_point
        and profile.namespaces == root_profile.namespaces
        and profile.uid == root_profile.uid
    )
    return profile


def _validate_pruned_prefix(service, root, root_profile, root_descriptor, descriptors, recorded, *, pending=None):
    anchor._require(anchor.inspect_service(starting=True) == service)
    anchor._require(anchor._delegated_path(service) == root)
    anchor._require(anchor.cg.validate_cgroup_profile(root) == root_profile)
    anchor._require(anchor.cg._observe_root(root, root_descriptor) == root_profile)
    anchor._require(anchor.cg._read_control(root_descriptor, "cgroup.procs").split() == [str(service.pid)])
    anchor.cg._require_unfrozen(root_descriptor)
    observed = anchor._subgroups(root_descriptor)
    anchor._require(observed == set(recorded) or (pending is not None and observed == set(recorded) | {pending}))
    for name, profile in recorded.items():
        descriptor = descriptors[name]
        anchor._require(anchor.cg._observe_root(profile.root, descriptor) == profile)
        anchor._require(anchor.cg.validate_cgroup_profile(profile.root) == profile)
        _empty_child(descriptor)
    return observed


def prepare_manager_pruned_layout(*, recorded_children, pending_name, begin_child, record_child, cancelled=None):
    """Create or resume the three fixed children under an exact empty new root."""
    root_descriptor = None
    descriptors = {}
    try:
        anchor._require(callable(begin_child) and callable(record_child))
        _cancelled(cancelled)
        service = anchor.inspect_service(starting=True)
        root = anchor._delegated_path(service)
        root_profile = anchor.cg.validate_cgroup_profile(root)
        root_descriptor = anchor.cg._open_root(root)
        descriptors["root"] = root_descriptor
        anchor._require(anchor.cg._observe_root(root, root_descriptor) == root_profile)
        anchor._require(anchor.cg._read_control(root_descriptor, "cgroup.procs").split() == [str(service.pid)])
        anchor.cg._require_unfrozen(root_descriptor)

        anchor._require(type(recorded_children) is tuple and len(recorded_children) <= len(anchor._CHILDREN))
        prefix = anchor._CHILDREN[: len(recorded_children)]
        recorded = {
            name: _child_profile(root_profile, name, recorded_children[index]) for index, name in enumerate(prefix)
        }
        expected_pending = anchor._CHILDREN[len(prefix)] if len(prefix) < len(anchor._CHILDREN) else None
        anchor._require(pending_name is None or (type(pending_name) is str and pending_name == expected_pending))
        if len(prefix) == len(anchor._CHILDREN):
            anchor._require(pending_name is None)

        actual = anchor._subgroups(root_descriptor)
        allowed = set(prefix)
        if pending_name is not None and pending_name in actual:
            allowed.add(pending_name)
        anchor._require(actual == allowed)

        for name in prefix:
            descriptor = anchor.cg._open_directory(name, parent=root_descriptor)
            descriptors[name] = descriptor
            anchor._require(anchor.cg._observe_root(recorded[name].root, descriptor) == recorded[name])
            _empty_child(descriptor)
        _validate_pruned_prefix(
            service,
            root,
            root_profile,
            root_descriptor,
            descriptors,
            recorded,
            pending=pending_name,
        )

        for name in anchor._CHILDREN[len(prefix) :]:
            resumed = name == pending_name
            observed = _validate_pruned_prefix(
                service,
                root,
                root_profile,
                root_descriptor,
                descriptors,
                recorded,
                pending=name if resumed else None,
            )
            anchor._require(resumed or name not in observed)
            begin_child(name, root_profile, tuple(recorded[key] for key in anchor._CHILDREN if key in recorded))

            # A callback may fsync and re-read external intent, but it may not
            # alter this pinned topology. Only a previously declared pending
            # name is eligible to exist before our mkdir.
            observed = _validate_pruned_prefix(
                service,
                root,
                root_profile,
                root_descriptor,
                descriptors,
                recorded,
                pending=name if resumed else None,
            )
            _cancelled(cancelled)
            if name not in observed:
                os.mkdir(name, mode=0o700, dir_fd=root_descriptor)
            descriptor = anchor.cg._open_directory(name, parent=root_descriptor)
            descriptors[name] = descriptor
            profile = _child_profile(
                root_profile,
                name,
                anchor.cg.validate_cgroup_profile(root + "/" + name),
            )
            anchor._require(anchor.cg._observe_root(profile.root, descriptor) == profile)
            _empty_child(descriptor)
            record_child(name, profile)
            recorded[name] = profile
            pending_name = None
            _validate_pruned_prefix(
                service,
                root,
                root_profile,
                root_descriptor,
                descriptors,
                recorded,
            )

        profiles = (root_profile, *(recorded[name] for name in anchor._CHILDREN))
        profiles = _expected_profiles(tuple(profiles))
        layout = ReplacementAnchorLayout(service, root, profiles, descriptors, pruned=True, key=_KEY)
        descriptors = {}
        root_descriptor = None
        layout.validate_prepared()
        _cancelled(cancelled)
        layout.validate_prepared()
        return layout
    except BaseException as error:
        if descriptors:
            anchor.cg._close_descriptors(*tuple(descriptors.values()))
        elif root_descriptor is not None:
            anchor.cg._close_descriptors(root_descriptor)
        if isinstance(error, RecoverableStateError) or not isinstance(error, Exception):
            raise
        raise RecoverableStateError() from None
