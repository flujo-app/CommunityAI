from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "gate13_download_artifact.py"
SPEC = importlib.util.spec_from_file_location("gate13_download_artifact", HELPER)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CONFIG_NAME = "gate13_download_windows.json"
ARCHIVE_NAME = "communityai-desktop-windows.zip"
ALLOWED_HOST = "productionresultssa7.blob.core.windows.net"
SIGNED_URL = f"https://{ALLOWED_HOST}/actions-results/package.zip?sig=private-test-value"


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, payload: bytes, url: str, *, content_length: int | None = None):
        super().__init__(payload)
        self._url = url
        self.headers = {"Content-Length": str(len(payload) if content_length is None else content_length)}

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class FakeOpener:
    def __init__(self, payload: bytes, *, response_url: str = SIGNED_URL, content_length: int | None = None):
        self.payload = payload
        self.response_url = response_url
        self.content_length = content_length
        self.calls = 0

    def open(self, request: urllib.request.Request, timeout: int):
        assert timeout == 60
        assert request.full_url == SIGNED_URL
        self.calls += 1
        return FakeResponse(self.payload, self.response_url, content_length=self.content_length)


class FailingOpener:
    def __init__(self):
        self.calls = 0

    def open(self, request: urllib.request.Request, timeout: int):
        self.calls += 1
        raise AssertionError("network must not be reached")


def _wrapper(
    payload: bytes,
    *,
    name: str = ARCHIVE_NAME,
    compression: int = zipfile.ZIP_STORED,
    extra_member: bool = False,
    unix_mode: int | None = None,
) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=compression, allowZip64=True) as archive:
        if unix_mode is None:
            archive.writestr(name, payload)
        else:
            member = zipfile.ZipInfo(name)
            member.create_system = 3
            member.external_attr = unix_mode << 16
            archive.writestr(member, payload, compress_type=compression)
        if extra_member:
            archive.writestr("unexpected.txt", b"unexpected")
    return result.getvalue()


def _config(payload: bytes, wrapper: bytes, **updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": 1,
        "allowed_host": ALLOWED_HOST,
        "wrapper_bytes": len(wrapper),
        "archive_name": ARCHIVE_NAME,
        "archive_bytes": len(payload),
        "archive_sha256": hashlib.sha256(payload).hexdigest(),
    }
    result.update(updates)
    return result


def _write_config(tmp_path: Path, config: dict[str, object], *, raw: str | None = None) -> None:
    text = raw if raw is not None else json.dumps(config, sort_keys=True)
    (tmp_path / CONFIG_NAME).write_text(text, encoding="utf-8", newline="\n")


def _assert_no_partial_files(tmp_path: Path) -> None:
    assert not (tmp_path / ("." + ARCHIVE_NAME + ".wrapper.partial")).exists()
    assert not (tmp_path / ("." + ARCHIVE_NAME + ".partial")).exists()


def test_download_extracts_one_exact_inner_archive_atomically(tmp_path):
    payload = b"exact install archive bytes"
    wrapper = _wrapper(payload)
    _write_config(tmp_path, _config(payload, wrapper))
    opener = FakeOpener(wrapper)

    result = MODULE.download(tmp_path, CONFIG_NAME, io.BytesIO((SIGNED_URL + "\n").encode()), opener=opener)

    assert result == {
        "archive_bytes": len(payload),
        "archive_sha256": hashlib.sha256(payload).hexdigest(),
        "clean": True,
        "result": "passed",
        "url_retained": False,
    }
    assert opener.calls == 1
    assert (tmp_path / ARCHIVE_NAME).read_bytes() == payload
    _assert_no_partial_files(tmp_path)


@pytest.mark.parametrize(
    ("wrapper_factory", "updates"),
    [
        (lambda payload: _wrapper(payload, extra_member=True), {}),
        (lambda payload: _wrapper(payload, name="wrong.zip"), {}),
        (lambda payload: _wrapper(payload, compression=zipfile.ZIP_DEFLATED), {}),
        (lambda payload: _wrapper(payload, unix_mode=stat.S_IFLNK | 0o777), {}),
        (lambda payload: _wrapper(payload, unix_mode=stat.S_IFIFO | 0o600), {}),
        (lambda payload: _wrapper(payload), {"archive_sha256": "0" * 64}),
        (lambda payload: _wrapper(payload), {"archive_bytes": 1}),
    ],
)
def test_wrapper_or_inner_mismatch_fails_closed(tmp_path, wrapper_factory, updates):
    payload = b"exact install archive bytes"
    wrapper = wrapper_factory(payload)
    _write_config(tmp_path, _config(payload, wrapper, **updates))

    with pytest.raises(MODULE.Gate13DownloadError):
        MODULE.download(tmp_path, CONFIG_NAME, io.BytesIO(SIGNED_URL.encode()), opener=FakeOpener(wrapper))

    assert not (tmp_path / ARCHIVE_NAME).exists()
    _assert_no_partial_files(tmp_path)


def test_wrapper_content_length_and_stream_size_are_both_exact(tmp_path):
    payload = b"archive"
    wrapper = _wrapper(payload)
    config = _config(payload, wrapper)
    _write_config(tmp_path, config)

    with pytest.raises(MODULE.Gate13DownloadError):
        MODULE.download(
            tmp_path,
            CONFIG_NAME,
            io.BytesIO(SIGNED_URL.encode()),
            opener=FakeOpener(wrapper[:-1], content_length=len(wrapper)),
        )

    assert not (tmp_path / ARCHIVE_NAME).exists()
    _assert_no_partial_files(tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "http://productionresultssa7.blob.core.windows.net/a?sig=x",
        "https://example.com/a?sig=x",
        "https://user@productionresultssa7.blob.core.windows.net/a?sig=x",
        "https://productionresultssa7.blob.core.windows.net:444/a?sig=x",
        "https://productionresultssa7.blob.core.windows.net/a",
        "https://productionresultssa7.blob.core.windows.net/a?sig=x#fragment",
        "https://productionresultssa7.blob.core.windows.net/a?sig=x\nsecond",
        "",
    ],
)
def test_signed_url_validation_fails_before_network(tmp_path, url):
    payload = b"archive"
    wrapper = _wrapper(payload)
    _write_config(tmp_path, _config(payload, wrapper))
    opener = FailingOpener()

    with pytest.raises(MODULE.Gate13DownloadError):
        MODULE.download(tmp_path, CONFIG_NAME, io.BytesIO(url.encode()), opener=opener)

    assert opener.calls == 0
    assert not (tmp_path / ARCHIVE_NAME).exists()
    _assert_no_partial_files(tmp_path)


def test_changed_final_response_url_is_rejected_without_output(tmp_path, capsys):
    payload = b"archive"
    wrapper = _wrapper(payload)
    _write_config(tmp_path, _config(payload, wrapper))

    with pytest.raises(MODULE.Gate13DownloadError):
        MODULE.download(
            tmp_path,
            CONFIG_NAME,
            io.BytesIO(SIGNED_URL.encode()),
            opener=FakeOpener(wrapper, response_url=SIGNED_URL + "&changed=true"),
        )

    assert capsys.readouterr() == ("", "")
    assert not (tmp_path / ARCHIVE_NAME).exists()
    _assert_no_partial_files(tmp_path)


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"allowed_host": "PRODUCTIONRESULTSSA7.blob.core.windows.net"},
        {"allowed_host": "example.com"},
        {"wrapper_bytes": True},
        {"wrapper_bytes": 0},
        {"wrapper_bytes": 2 * 1024 * 1024},
        {"archive_bytes": True},
        {"archive_bytes": 0},
        {"archive_name": "../package.zip"},
        {"archive_name": "folder/package.zip"},
        {"archive_name": "CON.zip"},
        {"archive_name": "LPT9.tar.gz"},
        {"archive_name": "package.zip."},
        {"archive_name": "páckage.zip"},
        {"archive_sha256": "A" * 64},
        {"archive_sha256": "0" * 63},
    ],
)
def test_config_schema_is_strict_and_platform_safe(tmp_path, updates):
    payload = b"archive"
    wrapper = _wrapper(payload)
    config = _config(payload, wrapper)
    config.update(updates)
    _write_config(tmp_path, config)

    with pytest.raises(MODULE.Gate13DownloadError):
        MODULE.download(tmp_path, CONFIG_NAME, io.BytesIO(SIGNED_URL.encode()), opener=FailingOpener())

    assert not (tmp_path / ARCHIVE_NAME).exists()
    _assert_no_partial_files(tmp_path)


def test_config_rejects_duplicate_and_extra_keys(tmp_path):
    duplicate = (
        '{"schema_version":1,"schema_version":1,'
        f'"allowed_host":"{ALLOWED_HOST}","wrapper_bytes":100,'
        f'"archive_name":"{ARCHIVE_NAME}","archive_bytes":1,"archive_sha256":"{"0" * 64}"}}'
    )
    _write_config(tmp_path, {}, raw=duplicate)

    with pytest.raises(MODULE.Gate13DownloadError):
        MODULE._load_config(tmp_path, CONFIG_NAME)

    payload = b"archive"
    wrapper = _wrapper(payload)
    config = _config(payload, wrapper, extra=True)
    _write_config(tmp_path, config)
    with pytest.raises(MODULE.Gate13DownloadError):
        MODULE._load_config(tmp_path, CONFIG_NAME)


def test_config_symlink_is_rejected_when_supported(tmp_path):
    payload = b"archive"
    wrapper = _wrapper(payload)
    real_config = tmp_path / "real.json"
    real_config.write_text(json.dumps(_config(payload, wrapper)), encoding="utf-8")
    try:
        (tmp_path / CONFIG_NAME).symlink_to(real_config)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(MODULE.Gate13DownloadError):
        MODULE.download(tmp_path, CONFIG_NAME, io.BytesIO(SIGNED_URL.encode()), opener=FailingOpener())


def test_commit_race_never_deletes_competing_target(tmp_path, monkeypatch):
    partial = tmp_path / ".package.partial"
    target = tmp_path / "package.zip"
    partial.write_bytes(b"verified")
    real_link = os.link

    def race_link(source, destination):
        Path(destination).write_bytes(b"competing")
        return real_link(source, destination)

    monkeypatch.setattr(MODULE.os, "link", race_link)
    with pytest.raises(MODULE.Gate13DownloadError):
        MODULE._commit_no_replace(partial, target)

    assert target.read_bytes() == b"competing"
    assert partial.read_bytes() == b"verified"


def test_existing_target_is_never_overwritten(tmp_path):
    payload = b"archive"
    wrapper = _wrapper(payload)
    _write_config(tmp_path, _config(payload, wrapper))
    target = tmp_path / ARCHIVE_NAME
    target.write_bytes(b"operator-owned")

    with pytest.raises(MODULE.Gate13DownloadError):
        MODULE.download(tmp_path, CONFIG_NAME, io.BytesIO(SIGNED_URL.encode()), opener=FailingOpener())

    assert target.read_bytes() == b"operator-owned"
    _assert_no_partial_files(tmp_path)


def test_redirect_handler_never_follows_location():
    handler = MODULE._NoRedirect()
    request = urllib.request.Request(SIGNED_URL)

    with pytest.raises(MODULE.Gate13DownloadError) as error:
        handler.redirect_request(request, None, 302, "Found", {}, SIGNED_URL + "&redirected=true")

    assert str(error.value) == ""


def test_cli_failure_output_cannot_retain_signed_url():
    secret_url = SIGNED_URL + "&secret=do-not-print"
    result = subprocess.run(
        [sys.executable, str(HELPER), "not-an-allowed-config.json"],
        input=secret_url,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert result.stderr == ""
    assert result.stdout == '{"result":"failed","url_retained":false}\n'
    assert secret_url not in result.stdout


def test_helper_has_no_subprocess_proxy_environment_or_url_argument_surface():
    source = HELPER.read_text(encoding="utf-8")

    assert "subprocess" not in source
    assert "os.environ" not in source
    assert "ProxyHandler({})" in source
    assert "sys.stdin.buffer" in source
    assert "sys.argv[2]" not in source
    assert "print(url" not in source
