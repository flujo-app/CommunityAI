"""Deny HTTP downloads in a qualification child without blocking swarm RPC."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HttpDownloadBlocker:
    def __init__(self):
        self.denied_requests = 0
        self._lock = threading.Lock()
        owner = self

        class Reject(BaseHTTPRequestHandler):
            def deny(self):
                with owner._lock:
                    owner.denied_requests += 1
                self.send_error(403, "HTTP downloads disabled for cache qualification")

            do_CONNECT = do_GET = do_HEAD = do_POST = deny

            def log_message(self, *args):
                pass  # Do not retain URLs, headers or credentials.

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Reject)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def environment(self, original):
        names = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
        env = {key: value for key, value in original.items() if key.lower() not in names}
        env.update(HTTP_PROXY=self.url, HTTPS_PROXY=self.url, ALL_PROXY=self.url, NO_PROXY="127.0.0.1,localhost,::1")
        return env

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
