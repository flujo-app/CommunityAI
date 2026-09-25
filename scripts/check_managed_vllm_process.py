"""One short fake-server process test; no vLLM package, weights or GPU."""

import asyncio
import hashlib
import socket
import sys
import tempfile
import time
import types
from pathlib import Path

PACKAGE = types.ModuleType("drift")
PACKAGE.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / "drift")]
sys.modules["drift"] = PACKAGE

from drift.inference_provider import (  # noqa: E402
    Availability,
    EventKind,
    InferenceLimits,
    InferenceRequest,
    ProviderIdentity,
    ProviderProfile,
)
from drift.managed_vllm import ManagedVllmBinding  # noqa: E402
from drift.managed_vllm_process import ManagedVllmProcessError, ManagedVllmProcessOwner  # noqa: E402


FAKE_SERVE = r'''
import argparse
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument('model_directory')
parser.add_argument('--served-model-name')
parser.add_argument('--host')
parser.add_argument('--port', type=int)
parser.add_argument('--tensor-parallel-size')
parser.add_argument('--pipeline-parallel-size')
parser.add_argument('--distributed-executor-backend')
parser.add_argument('--max-model-len', type=int)
parser.add_argument('--no-enable-log-requests', action='store_true')
args = parser.parse_args()
worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])

class Server(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get('Authorization') != 'Bearer secret':
            self.send_error(401)
            return
        if self.path == '/health':
            body = b'OK'
        elif self.path == '/version':
            body = json.dumps({'version': '0.30.0'}).encode()
        elif self.path == '/v1/models':
            body = json.dumps({'object': 'list', 'data': [
                {'id': args.served_model_name, 'max_model_len': args.max_model_len}
            ]}).encode()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != '/v1/completions' or self.headers.get('Authorization') != 'Bearer secret':
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        model = 'wrong/model' if request['prompt'] == 'bad' else args.served_model_name
        frames = [
            {'id': 'req-1', 'model': model, 'choices': [{'index': 0, 'text': 'hi', 'finish_reason': None}]},
            {'id': 'req-1', 'model': model, 'choices': [{'index': 0, 'text': '', 'finish_reason': 'stop'}]},
            {'id': 'req-1', 'model': model, 'choices': [], 'usage': {
                'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}},
        ]
        body = b''.join(b'data: ' + json.dumps(frame).encode() + b'\n\n' for frame in frames)
        body += b'data: [DONE]\n\n'
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass

ThreadingHTTPServer((args.host, args.port), Server).serve_forever()
'''


def request(prompt: str) -> InferenceRequest:
    now = time.monotonic()
    return InferenceRequest(
        ProviderIdentity("test/provider", "test/instance"),
        "test/profile",
        "test/model",
        "a" * 32,
        "b" * 32,
        now,
        now + 5,
        prompt,
        InferenceLimits(100, 20, 8, 100),
    )


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_for_server(binding: ManagedVllmBinding) -> None:
    deadline = time.monotonic() + 2
    port = int(binding.base_url.rsplit(":", 1)[1])
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.1):
                return
        except OSError:
            await asyncio.sleep(0.02)
    raise AssertionError("fake backend did not start")


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="communityai-vllm-fake-") as temporary:
        directory = Path(temporary)
        (directory / "serve").write_text(FAKE_SERVE, encoding="utf-8")
        profile = ProviderProfile("test/profile", "test/model", Availability.AVAILABLE, qualification_id="e" * 64)
        binding = ManagedVllmBinding(
            profile, "test/model", f"http://127.0.0.1:{unused_port()}", "secret", (0, 1), 2, 1
        )
        executable = Path(sys.executable)
        owner = ManagedVllmProcessOwner(
            binding,
            model_directory=directory,
            executable=executable,
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            max_model_len=2048,
        )
        wrong_hash = ManagedVllmProcessOwner(
            binding,
            model_directory=directory,
            executable=executable,
            executable_sha256="0" * 64,
            max_model_len=2048,
        )
        try:
            wrong_hash.start()
        except ManagedVllmProcessError:
            assert wrong_hash.pid is None
        else:
            raise AssertionError("unverified executable launched")
        try:
            owner.start()
            await wait_for_server(binding)
            probe = await owner.probe(seconds=2)
            assert probe.backend_version == "0.30.0" and owner.ready
            adapter = owner.new_adapter()
            events = [event async for event in adapter.stream(request("good"))]
            assert [event.kind for event in events] == [EventKind.STARTED, EventKind.OUTPUT, EventKind.COMPLETED]
            assert owner.ready
            events = [event async for event in adapter.stream(request("bad"))]
            assert events[-1].kind is EventKind.FAILED and adapter.quarantined
            assert owner.pid is None and not owner.ready, "quarantine must confirm complete process-tree exit"
            try:
                owner.new_adapter()
            except ManagedVllmProcessError:
                pass
            else:
                raise AssertionError("quarantined owner admitted a new route")
            await asyncio.sleep(0.2)
            owner.start()
            await wait_for_server(binding)
            await owner.probe(seconds=2)
            adapter = owner.new_adapter()
            iterator = adapter.stream(request("cancel"))
            assert (await anext(iterator)).kind is EventKind.STARTED
            await iterator.aclose()
            assert adapter.quarantined and owner.pid is None and not owner.ready
            print("PASS: pinned launch, probe, accepted output, malformed and cancelled stream teardown")
        finally:
            owner.stop()
            # Windows releases a killed child's working-directory handle shortly
            # after its job reports zero active processes.
            await asyncio.sleep(0.2)


if __name__ == "__main__":
    asyncio.run(main())
