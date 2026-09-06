import hashlib
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from drift.model_manifest import ManifestArtifactVerifier, ManifestError, ModelManifest


@pytest.mark.parametrize(
    "mode",
    [
        "ranges",
        "interrupt",
        "transient",
        "429",
        "401",
        "ignore",
        "ignored_then_ranges",
        "wrong_range",
        "corrupt",
        "oversize",
    ],
)
def test_large_artifact_ranges_preserve_contiguous_resume_and_verify_hash(tmp_path, monkeypatch, mode):
    chunk_size = 64 * 1024
    payload = bytes(index % 251 for index in range(600_000))
    source = ModelManifest.load(Path("tests/data/model_manifest_v1_vector.json")).to_dict()
    artifact = next(a for a in source["artifacts"] if a["role"] == "weight")
    artifact.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    manifest = ModelManifest.from_dict(source)
    verifier = ManifestArtifactVerifier(
        manifest, manifest.source.repository, manifest.source.revision, cache_dir=tmp_path
    )
    declared = manifest.get_artifact(artifact["path"])
    requests, failures = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            start, end = map(int, self.headers["Range"].removeprefix("bytes=").split("-"))
            requests.append((start, end))
            if mode == "ignored_then_ranges" and not failures:
                failures.append(start)
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload[:100])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if mode in ("429", "401") and not failures:
                failures.append(start)
                self.send_response(int(mode))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if mode == "ignore":
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            body = payload[start : end + 1]
            if mode == "corrupt":
                body = bytes([body[0] ^ 255]) + body[1:]
            if mode == "oversize":
                body += b"!"
            self.send_response(206)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Range", f"bytes {start + (mode == 'wrong_range')}-{end}/{len(payload)}")
            self.end_headers()
            if (mode == "interrupt" and start == 2 * chunk_size and len(failures) < 3) or (
                mode == "transient" and start in (0, 2 * chunk_size) and start not in failures
            ):
                failures.append(start)
                self.wfile.write(body[:100])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr("drift.utils.hub_ranges.RANGE_BYTES", chunk_size)
    monkeypatch.setattr("huggingface_hub.hf_hub_url", lambda *a, **kw: f"http://127.0.0.1:{server.server_port}/weights")
    partial, final, _ = verifier._resumable_paths(declared)
    try:
        if mode == "ignored_then_ranges":
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(payload[: 2 * chunk_size])
        if mode in ("wrong_range", "corrupt", "oversize"):
            with pytest.raises(ManifestError):
                verifier._resumable_hub_download(declared)
            assert not final.exists()
            assert not partial.exists() or partial.stat().st_size < len(payload)
            return
        if mode == "401":
            with pytest.raises(ManifestError, match="Interrupted download"):
                verifier._resumable_hub_download(declared)
            assert len(requests) == 1
            assert not final.exists()
            return
        if mode == "interrupt":
            with pytest.raises(ManifestError, match="Interrupted download"):
                verifier._resumable_hub_download(declared)
            assert not final.exists()
            assert partial.read_bytes() == payload[: 2 * chunk_size]
            assert len(failures) == 3
            count = len(requests)
            verifier._resumable_hub_download(declared)
            assert requests[count][0] == 2 * chunk_size
        else:
            verifier._resumable_hub_download(declared)
        assert final.read_bytes() == payload
        assert not partial.exists()
        assert all(end - start + 1 <= chunk_size for start, end in requests)
        if mode == "transient":
            assert failures == [0, 2 * chunk_size]
        if mode == "ignored_then_ranges":
            assert requests[0][0] == 2 * chunk_size
            assert requests[1][0] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
