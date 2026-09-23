"""Same-user shutdown handshake used before replacing installed application files."""

import hashlib
import time
from pathlib import Path

from communityai_desktop.lifecycle import GRACEFUL_NODE_SHUTDOWN_TIMEOUT


def prepare_update(
    *,
    timeout=GRACEFUL_NODE_SHUTDOWN_TIMEOUT + 20.0,
    instance_name=None,
    application_name="CommunityAI",
    instance_data_dir=None,
):
    from PySide6.QtCore import QCoreApplication, QLockFile, QStandardPaths
    from PySide6.QtNetwork import QLocalSocket

    from communityai_desktop.pyside_shell import _instance_data_root, _instance_server_name, _validate_application_name
    from communityai_desktop.startup import SingleInstanceError

    application_name = _validate_application_name(application_name)
    application = QCoreApplication.instance() or QCoreApplication([])
    application.setApplicationName(application_name)
    application.setOrganizationName("CommunityAI")
    default_location = (
        QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation) if instance_data_dir is None else None
    )
    root = _instance_data_root(instance_data_dir, default_location, create=False)
    name = _instance_server_name(root, instance_name, profile_scoped=instance_data_dir is not None)
    lock_digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]
    lock = QLockFile(str(Path(root) / f"instance-{lock_digest}.lock"))
    lock.setStaleLockTime(0)
    if not Path(root).exists():
        return 0
    if lock.tryLock(0):
        lock.unlock()
        return 0
    socket = QLocalSocket(application)
    socket.connectToServer(name)
    deadline = time.monotonic() + timeout
    while socket.state() != QLocalSocket.ConnectedState and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.01)
    if socket.state() != QLocalSocket.ConnectedState:
        socket.abort()
        raise SingleInstanceError(f"{application_name} is starting or cannot acknowledge shutdown; retry the installer")
    socket.write(b"shutdown\n")
    response = bytearray()
    while time.monotonic() < deadline:
        socket.flush()
        application.processEvents()
        response.extend(bytes(socket.read(64 - len(response))))
        if b"\n" in response or len(response) >= 64:
            break
        time.sleep(0.01)
    socket.abort()
    if bytes(response) != b"stopped\n":
        raise SingleInstanceError(f"{application_name} could not finish shutting down; installation was not started")
    while time.monotonic() < deadline:
        if lock.tryLock(0):
            lock.unlock()
            return 0
        application.processEvents()
        time.sleep(0.01)
    raise SingleInstanceError(f"{application_name} has not released its instance lock; retry the installer")
