#!/usr/bin/env python3
"""HTTP front for Clef-flash: POST /v1/systemone (SystemOne request/response), GET /healthz."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 64 * 1024


def log(msg):
    print(msg, flush=True)


class Service:
    """Owns the model. `load` returns a function body -> response dict; tests pass a fake, so
    nothing here imports torch."""

    def __init__(self, load, on_fail=lambda exc: os._exit(1)):
        self._load, self._on_fail = load, on_fail
        self._answer = None
        self.ready = False
        self._lock = threading.Lock()  # one forward pass at a time; it is CPU-bound anyway

    def start(self):
        threading.Thread(target=self._boot, daemon=True).start()

    def _boot(self):
        try:
            self._answer = self._load()
        except Exception as e:  # a pod that cannot load the model must restart, not sit Ready-less
            log(f"model load failed: {type(e).__name__}: {e}")
            self._on_fail(e)
            return
        self.ready = True
        log("model loaded")

    def systemone(self, body):
        with self._lock:
            return self._answer(body)


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/healthz":
                self._reply(200 if service.ready else 503, {"ready": service.ready})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/systemone":
                return self._reply(404, {"error": "not found"})
            if not service.ready:
                return self._reply(503, {"error": "model not loaded"})
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._reply(411, {"error": "Content-Length required"})
            if not 0 < length <= MAX_BODY:
                return self._reply(413, {"error": "body size"})
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                return self._reply(400, {"error": "invalid JSON"})
            if not isinstance(body, dict) or not isinstance(body.get("questions"), dict):
                return self._reply(400, {"error": "questions object required"})
            try:
                result = service.systemone(body)
            except Exception as e:
                log(f"systemone failed: {type(e).__name__}: {e}")
                return self._reply(500, {"error": type(e).__name__})
            self._reply(200, result)

    return Handler


def load_clef(path):
    """The real loader: official weights on CPU in bf16."""
    import torch
    sys.path.insert(0, path)
    from joint_schema_model import load_release_model, systemone

    torch.set_num_threads(int(os.environ.get("CLEF_THREADS", "6")))
    model, processor = load_release_model(path, device="cpu")
    return lambda body: systemone(model, processor, body)


def main():
    path = os.environ["CLEF_SNAPSHOT"]
    service = Service(lambda: load_clef(path))
    service.start()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), make_handler(service)).serve_forever()


if __name__ == "__main__":
    main()
