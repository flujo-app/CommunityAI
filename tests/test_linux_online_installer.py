"""Offline, inert checks for the generated Linux installer; never run sudo or APT."""

import contextlib
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

INSTALLERS = Path(__file__).resolve().parents[1] / "desktop" / "installers"
sys.path.insert(0, str(INSTALLERS))

import linux_online_root as protected
import linux_online_template as online

PAYLOAD = b"verified package fixture; no executable or Debian archive"


def artifact(**updates):
    value = {
        "platform": "linux-amd64",
        "kind": "offline-installer",
        "format": "deb",
        "version": "0.1.0~alpha.1",
        "filename": "communityai_0.1.0~alpha.1_amd64.deb",
        "url": "https://downloads.example.com/releases/alpha.1/communityai_0.1.0~alpha.1_amd64.deb",
        "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
        "size_bytes": len(PAYLOAD),
    }
    value.update(updates)
    return value


class Response(io.BytesIO):
    def __init__(self, payload=PAYLOAD, *, status=200, url=None, headers=None, failure=None):
        super().__init__(payload)
        self.status = status
        self.url = url or artifact()["url"]
        self.headers = {"Content-Length": str(len(payload))} if headers is None else headers
        self.failure = failure

    def geturl(self):
        return self.url

    def read(self, count=-1):
        if self.failure:
            raise self.failure
        assert count <= online.CHUNK_BYTES
        return super().read(count)


class Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request.full_url, timeout))
        assert request.get_header("Accept-encoding") == "identity"
        return self.response


def host(monkeypatch, tmp_path, response):
    monkeypatch.setattr(online.sys, "platform", "linux")
    monkeypatch.setattr(online.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(online.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(online.shutil, "disk_usage", lambda path: SimpleNamespace(free=10**12))
    monkeypatch.setattr(online, "download_deadline", contextlib.nullcontext)
    monkeypatch.setattr(online, "ROOT_HELPER", "inert protected-copy helper fixture")
    original_is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: path.as_posix() in ("/usr/bin/sudo", "/usr/bin/apt", "/usr/bin/python3") or original_is_file(path),
    )
    opener = Opener(response)
    monkeypatch.setattr(online.urllib.request, "build_opener", lambda *args: opener)
    sentinel = tmp_path / "unrelated.txt"
    sentinel.write_text("keep")
    return opener, sentinel


def test_verified_download_precedes_apt_and_only_owned_staging_is_removed(tmp_path, monkeypatch):
    opener, sentinel = host(monkeypatch, tmp_path, Response())
    called = []

    def install(path, pinned):
        called.append(path)
        assert path.read_bytes() == PAYLOAD
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact()["sha256"]
        command = online.install_command(path, pinned)
        assert command[:4] == ["/usr/bin/sudo", "/usr/bin/python3", "-I", "-c"]
        assert command[4] == online.ROOT_HELPER
        assert command[5:] == [str(path.resolve()), pinned["filename"], str(len(PAYLOAD)), pinned["sha256"]]
        return 0

    monkeypatch.setattr(online, "install_verified", install)
    assert online.run(artifact(), directory=tmp_path) == 0
    assert len(called) == 1 and not called[0].parent.exists()
    assert opener.calls == [(artifact()["url"], 30)]
    assert list(tmp_path.iterdir()) == [sentinel]


@pytest.mark.parametrize(
    "response,updates,error",
    [
        (lambda: Response(headers={"Content-Length": "999"}), {}, "package size"),
        (lambda: Response(headers={"Content-Length": "garbage"}), {}, "package size"),
        (lambda: Response(PAYLOAD[:-1], headers={}), {}, "incomplete"),
        (lambda: Response(PAYLOAD + b"extra", headers={}), {}, "exceeds"),
        (lambda: Response(), {"sha256": "0" * 64}, "SHA-256"),
        (lambda: Response(status=206), {}, "exact pinned"),
        (lambda: Response(url="https://elsewhere.example.org/payload.deb"), {}, "exact pinned"),
        (lambda: Response(headers={"Content-Encoding": "gzip"}), {}, "encoded"),
        (lambda: Response(failure=OSError("network failed")), {}, "network failed"),
    ],
)
def test_invalid_download_never_invokes_apt_and_cleans_only_owned_files(
    tmp_path, monkeypatch, response, updates, error
):
    _, sentinel = host(monkeypatch, tmp_path, response())

    def forbidden(path, pinned):
        raise AssertionError("APT must not see unverified bytes")

    monkeypatch.setattr(online, "install_verified", forbidden)
    with pytest.raises((online.InstallError, OSError), match=error):
        online.run(artifact(**updates), directory=tmp_path)
    assert list(tmp_path.iterdir()) == [sentinel]


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), online.Cancelled("cancelled")])
def test_cancelled_download_cleans_partial_without_installing(tmp_path, monkeypatch, failure):
    _, sentinel = host(monkeypatch, tmp_path, Response(failure=failure))
    monkeypatch.setattr(online, "install_verified", lambda *args: pytest.fail("must not invoke APT"))
    with pytest.raises(type(failure)):
        online.run(artifact(), directory=tmp_path)
    assert list(tmp_path.iterdir()) == [sentinel]


def test_monotonic_download_deadline(tmp_path):
    times = iter((0, online.DOWNLOAD_TIMEOUT_SECONDS))
    with pytest.raises(online.InstallError, match="deadline"):
        online.download(artifact(), tmp_path / "payload.deb", opener=Opener(Response()), clock=lambda: next(times))


@pytest.mark.parametrize("trigger,error", [(14, online.InstallError), (15, online.Cancelled)])
def test_download_deadline_signals_cancel_and_restore_handlers(monkeypatch, trigger, error):
    previous = {14: object(), 15: object()}
    handlers = dict(previous)
    timers = []

    def set_handler(number, handler):
        old = handlers[number]
        handlers[number] = handler
        return old

    monkeypatch.setattr(online.signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(online.signal, "SIGTERM", 15)
    monkeypatch.setattr(online.signal, "ITIMER_REAL", 0, raising=False)
    monkeypatch.setattr(online.signal, "signal", set_handler)
    monkeypatch.setattr(online.signal, "setitimer", lambda *args: timers.append(args), raising=False)
    with pytest.raises(error):
        with online.download_deadline():
            handlers[trigger](trigger, None)
    assert handlers == previous
    assert timers == [(0, online.DOWNLOAD_TIMEOUT_SECONDS), (0, 0)]


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_handler_never_follows_another_url(code):
    with pytest.raises(online.InstallError, match="redirected"):
        online.NoRedirects().redirect_request(None, None, code, "redirect", {}, "https://other.example.org/file")


def test_download_only_retains_exact_verified_package(tmp_path, monkeypatch):
    host(monkeypatch, tmp_path, Response())
    monkeypatch.setattr(online, "install_verified", lambda *args: pytest.fail("download-only cannot invoke APT"))
    assert online.run(artifact(), directory=tmp_path, download_only=True) == 0
    files = list(tmp_path.glob("communityai-online-*/*.deb"))
    assert len(files) == 1 and files[0].read_bytes() == PAYLOAD


@pytest.mark.parametrize("condition", ["root", "wrong_arch", "disk_space"])
def test_preflight_failure_never_opens_network(tmp_path, monkeypatch, condition):
    opener, sentinel = host(monkeypatch, tmp_path, Response())
    if condition == "root":
        monkeypatch.setattr(online.os, "geteuid", lambda: 0)
    elif condition == "wrong_arch":
        monkeypatch.setattr(online.platform, "machine", lambda: "aarch64")
    else:
        monkeypatch.setattr(online.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    with pytest.raises(online.InstallError):
        online.run(artifact(), directory=tmp_path)
    assert not opener.calls and list(tmp_path.iterdir()) == [sentinel]


def test_unconfirmed_apt_interruption_preserves_its_input(tmp_path, monkeypatch):
    host(monkeypatch, tmp_path, Response())

    def interrupted(path, pinned):
        raise KeyboardInterrupt()

    monkeypatch.setattr(online, "install_verified", interrupted)
    with pytest.raises(KeyboardInterrupt):
        online.run(artifact(), directory=tmp_path)
    assert len(list(tmp_path.glob("communityai-online-*/*.deb"))) == 1


def test_failed_apt_exit_is_reported_and_completed_staging_removed(tmp_path, monkeypatch):
    _, sentinel = host(monkeypatch, tmp_path, Response())
    monkeypatch.setattr(online, "install_verified", lambda *args: 100)
    with pytest.raises(online.InstallError, match="status 100"):
        online.run(artifact(), directory=tmp_path)
    assert list(tmp_path.iterdir()) == [sentinel]


def test_install_waits_for_apt_without_killing_on_interrupt(tmp_path):
    waits = iter((KeyboardInterrupt(), 130))

    class Child:
        def wait(self):
            value = next(waits)
            if isinstance(value, BaseException):
                raise value
            return value

    calls = []
    assert (
        online.install_verified(
            tmp_path / "package.deb", artifact(), popen=lambda command: calls.append(command) or Child()
        )
        == 130
    )
    assert calls == [online.install_command(tmp_path / "package.deb", artifact())]


def test_builder_embeds_pinned_manifest_and_generated_script_is_standalone(tmp_path):
    from build_linux_online import build

    manifest = tmp_path / "release.json"
    manifest.write_text(json.dumps({"schema_version": 1, "artifacts": {"linux-amd64": artifact()}}))
    output = tmp_path / "communityai-online.py"
    build(manifest, output)
    namespace = {"__name__": "installer_fixture"}
    exec(compile(output.read_text(), str(output), "exec"), namespace)
    assert namespace["ARTIFACT"] == artifact()
    assert namespace["ROOT_HELPER"] == (INSTALLERS / "linux_online_root.py").read_text()
    assert "release_downloads" not in output.read_text()
    metadata = json.loads(output.with_suffix(".py.json").read_text())
    assert metadata["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert metadata["size_bytes"] == output.stat().st_size
    assert metadata["offline_installer"] == artifact()
    assert metadata["live_download_verified"] is False
    completed = subprocess.run([sys.executable, str(output), "--help"], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0 and "--download-only" in completed.stdout
    with pytest.raises(FileExistsError):
        build(manifest, output)


def protected_host(monkeypatch):
    monkeypatch.setattr(protected.os, "geteuid", lambda: 0, raising=False)
    # Windows fixtures exercise bytes and ordering. Linux O_NOFOLLOW/O_NONBLOCK
    # and actual root ownership remain an explicit platform qualification limit.
    if sys.platform == "win32":
        monkeypatch.setattr(protected.os, "O_NOFOLLOW", 0, raising=False)
        monkeypatch.setattr(protected.os, "O_NONBLOCK", 0, raising=False)
    monkeypatch.setattr(protected, "copy_deadline", contextlib.nullcontext)
    monkeypatch.setattr(protected.shutil, "disk_usage", lambda path: SimpleNamespace(free=10**12))


def test_protected_copy_is_independent_of_user_file_after_verification(tmp_path, monkeypatch):
    protected_host(monkeypatch)
    source = tmp_path / "source.deb"
    source.write_bytes(PAYLOAD)
    calls = []

    def apt(command):
        package = Path(command[-1])
        assert command[:2] == ["/usr/bin/apt", "install"]
        assert package != source and package.read_bytes() == PAYLOAD
        source.write_bytes(b"changed by an ordinary-user writer")
        assert package.read_bytes() == PAYLOAD
        calls.append(package)
        return SimpleNamespace(wait=lambda: 0)

    assert (
        protected.run(source, artifact()["filename"], len(PAYLOAD), artifact()["sha256"], directory=tmp_path, popen=apt)
        == 0
    )
    assert len(calls) == 1 and not calls[0].parent.exists()
    assert source.exists()


@pytest.mark.parametrize("payload", [PAYLOAD[:-1], b"x" * len(PAYLOAD), PAYLOAD + b"extra"])
def test_changed_package_during_sudo_wait_never_reaches_apt(tmp_path, monkeypatch, payload):
    protected_host(monkeypatch)
    source = tmp_path / "source.deb"
    source.write_bytes(payload)
    with pytest.raises(ValueError, match="regular file|verification"):
        protected.run(
            source,
            artifact()["filename"],
            len(PAYLOAD),
            artifact()["sha256"],
            directory=tmp_path,
            popen=lambda command: pytest.fail("APT must not receive changed bytes"),
        )
    assert list(tmp_path.iterdir()) == [source]


def test_protected_helper_preserves_copy_if_apt_exit_is_unconfirmed(tmp_path, monkeypatch):
    protected_host(monkeypatch)
    source = tmp_path / "source.deb"
    source.write_bytes(PAYLOAD)

    def interrupted():
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        protected.run(
            source,
            artifact()["filename"],
            len(PAYLOAD),
            artifact()["sha256"],
            directory=tmp_path,
            popen=lambda command: SimpleNamespace(wait=interrupted),
        )
    assert len(list(tmp_path.glob("communityai-install-*/*.deb"))) == 1


def test_protected_helper_retains_input_if_spawn_is_interrupted(tmp_path, monkeypatch):
    protected_host(monkeypatch)
    source = tmp_path / "source.deb"
    source.write_bytes(PAYLOAD)

    def uncertain_spawn(command):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        protected.run(
            source,
            artifact()["filename"],
            len(PAYLOAD),
            artifact()["sha256"],
            directory=tmp_path,
            popen=uncertain_spawn,
        )
    assert len(list(tmp_path.glob("communityai-install-*/*.deb"))) == 1


@pytest.mark.skipif(sys.platform != "linux", reason="Actual Linux O_NOFOLLOW behavior")
def test_protected_helper_rejects_symlink_source_on_linux(tmp_path, monkeypatch):
    protected_host(monkeypatch)
    source = tmp_path / "source.deb"
    source.write_bytes(PAYLOAD)
    linked = tmp_path / "linked.deb"
    linked.symlink_to(source)
    with pytest.raises(OSError):
        protected.run(
            linked,
            artifact()["filename"],
            len(PAYLOAD),
            artifact()["sha256"],
            directory=tmp_path,
            popen=lambda command: pytest.fail("APT must not see a symlink source"),
        )
    assert source.read_bytes() == PAYLOAD and linked.is_symlink()
    assert not list(tmp_path.glob("communityai-install-*"))
