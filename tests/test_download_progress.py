import hashlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from drift.model_manifest import ManifestArtifactVerifier, ManifestError, ModelManifest
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime
from drift.node.worker_supervisor import WorkerLaunch, WorkerSupervisor
from drift.utils.download_progress import public_progress


@pytest.mark.parametrize("corrupt", [False, True])
def test_live_resumed_http_download_reports_verification_and_failure(tmp_path, monkeypatch, corrupt):
    payload = b"verified model artifact" * 16000
    source = ModelManifest.load("tests/data/model_manifest_v1_vector.json").to_dict()
    artifact = next(item for item in source["artifacts"] if item["role"] == "weight")
    artifact.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    manifest = ModelManifest.from_dict(source)
    waiting, release = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            start, end = map(int, self.headers["Range"].removeprefix("bytes=").split("-"))
            requests.append((start, end))
            if len(requests) == 1:
                self.send_response(429)
                self.end_headers()
                return
            waiting.set()
            release.wait(10)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.end_headers()
            chunk = payload[start : end + 1]
            self.wfile.write((b"!" + chunk[1:]) if corrupt else chunk)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr("drift.utils.hub_ranges.RANGE_BYTES", 32768)
    monkeypatch.setattr(
        "huggingface_hub.hf_hub_url", lambda *a, **kw: f"http://127.0.0.1:{server.server_port}/artifact"
    )
    verifier = ManifestArtifactVerifier(
        manifest, manifest.source.repository, manifest.source.revision, cache_dir=tmp_path
    )
    partial, _, _ = verifier._resumable_paths(manifest.get_artifact(artifact["path"]))
    partial.parent.mkdir(parents=True)
    partial.write_bytes(payload[:8192])

    def loader():
        active = ManifestArtifactVerifier(
            manifest, manifest.source.repository, manifest.source.revision, cache_dir=tmp_path
        )
        active.ensure_path(artifact["path"])
        return ModelRuntime(object(), object())

    manager = ModelManager()
    manager.register(ModelDescriptor("download-test"), loader)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(manager.load, "download-test")
            try:
                assert waiting.wait(10)
                live = manager.snapshots()[0].to_dict()["download"]["progress"]
                assert live["state"] in ("downloading", "retrying")
                assert live["verified_bytes"] == 0
                assert live["resumed_bytes"] == 8192
                assert live["retries"] == 1
            finally:
                release.set()
            if corrupt:
                with pytest.raises(ManifestError):
                    future.result(timeout=10)
            else:
                future.result(timeout=10).release()
        result = manager.snapshots()[0].to_dict()["download"]["progress"]
        assert result["state"] == ("failed" if corrupt else "ready")
        assert result["verified_bytes"] == (0 if corrupt else len(payload))
        assert requests[0][0] == 8192
        if not corrupt:
            count = len(requests)
            manager.unload("download-test")
            manager.load("download-test").release()
            assert len(requests) == count
            cached = manager.snapshots()[0].to_dict()["download"]["progress"]
            assert cached["verified_bytes"] == len(payload)
            assert cached["bytes_per_second"] == 0
    finally:
        release.set()
        manager.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_worker_progress_never_exposes_private_fields_or_nonfinite_values():
    result = public_progress(
        {
            "schema_version": 1,
            "state": "failed",
            "artifact": "model.safetensors",
            "token": "private",
            "path": "private",
            "pid": 123,
            "bytes_per_second": float("nan"),
        }
    )
    assert not {"token", "path", "pid"} & result.keys()
    assert result["bytes_per_second"] is None


def test_supervised_process_reports_own_download_and_pause(tmp_path):
    code = """
import time
from types import SimpleNamespace
from drift.utils.download_progress import current_progress
progress = current_progress()
progress.event(SimpleNamespace(digest='a' * 64), SimpleNamespace(path='model.safetensors', size=1024),
               'downloading', received=512, transferred=512)
time.sleep(30)
"""
    supervisor = WorkerSupervisor([WorkerLaunch("progress-worker", "model", (sys.executable, "-c", code))])
    try:
        supervisor.start_worker("progress-worker")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = supervisor.snapshot("progress-worker")["download_progress"]
            if value is not None:
                break
            time.sleep(0.1)
        snapshot = supervisor.snapshot("progress-worker")
        assert value is not None, (snapshot["last_error"], snapshot["recent_logs"], snapshot["state"])
        assert value["state"] == "downloading"
        assert value["received_bytes"] == 512
        assert value["verified_bytes"] == 0
        assert "pid" not in value
        supervisor.pause_worker("progress-worker")
        assert supervisor.snapshot("progress-worker")["download_progress"]["state"] == "paused"
        directory = supervisor._records["progress-worker"].progress_directory.name
    finally:
        supervisor.shutdown()
    from pathlib import Path

    assert not Path(directory).exists()
