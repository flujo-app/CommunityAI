"""Same-user shutdown handshake used before replacing installed application files."""

import hashlib
import time
from pathlib import Path


def prepare_update(*, timeout=45.0, instance_name=None):
    from communityai_desktop.pyside_shell import _single_instance_server_name
    from communityai_desktop.startup import SingleInstanceError
    from PySide6.QtCore import QCoreApplication, QLockFile, QStandardPaths
    from PySide6.QtNetwork import QLocalSocket

    application = QCoreApplication.instance() or QCoreApplication([])
    application.setApplicationName("CommunityAI")
    application.setOrganizationName("CommunityAI")
    root = QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)
    if not root:
        raise SingleInstanceError("The application-data location is unavailable")
    name = instance_name or _single_instance_server_name(root)
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
        raise SingleInstanceError("CommunityAI is starting or cannot acknowledge shutdown; retry the installer")
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
        raise SingleInstanceError("CommunityAI could not finish shutting down; installation was not started")
    while time.monotonic() < deadline:
        if lock.tryLock(0):
            lock.unlock()
            return 0
        application.processEvents()
        time.sleep(0.01)
    raise SingleInstanceError("CommunityAI has not released its instance lock; retry the installer")
