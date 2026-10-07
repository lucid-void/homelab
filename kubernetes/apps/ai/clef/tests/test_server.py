import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import server  # noqa: E402

GOOD = {"model": "clef-flash", "state": "x", "questions": {"q": {"type": "noul"}}}


def echo(body):
    return {"model": body["model"], "answers": {"q": {"type": "noul", "noul": 0.9}}}


class Running:
    def __init__(self, load, on_fail=lambda exc: None):
        self.service = server.Service(load, on_fail)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.service))
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        unittest.addModuleCleanup(self.http.shutdown)

    def wait_ready(self):
        for _ in range(100):
            if self.service.ready:
                return
            time.sleep(0.02)
        raise AssertionError("never ready")

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)


class ServerTests(unittest.TestCase):
    def test_not_ready_until_the_model_loads(self):
        gate = threading.Event()
        r = Running(lambda: (gate.wait(5), echo)[1])
        r.service.start()
        self.assertEqual(r.call("GET", "/healthz")[0], 503)
        self.assertEqual(r.call("POST", "/v1/systemone", GOOD)[0], 503)
        gate.set()
        r.wait_ready()
        self.assertEqual(r.call("GET", "/healthz"), (200, {"ready": True}))

    def test_answers_a_valid_request(self):
        r = Running(lambda: echo)
        r.service.start()
        r.wait_ready()
        code, body = r.call("POST", "/v1/systemone", GOOD)
        self.assertEqual(code, 200)
        self.assertEqual(body["answers"]["q"]["noul"], 0.9)

    def test_bad_requests_are_400_not_500(self):
        r = Running(lambda: echo)
        r.service.start()
        r.wait_ready()
        self.assertEqual(r.call("POST", "/v1/systemone", raw=b"{nope")[0], 400)
        self.assertEqual(r.call("POST", "/v1/systemone", [1, 2])[0], 400)
        self.assertEqual(r.call("POST", "/v1/systemone", {"state": "x"})[0], 400)

    def test_oversized_body_is_refused(self):
        r = Running(lambda: echo)
        r.service.start()
        r.wait_ready()
        self.assertEqual(r.call("POST", "/v1/systemone", raw=b"x" * (server.MAX_BODY + 1))[0], 413)

    def test_a_model_error_is_500_with_only_the_type(self):
        def boom(body):
            raise RuntimeError("secret detail")
        r = Running(lambda: boom)
        r.service.start()
        r.wait_ready()
        code, body = r.call("POST", "/v1/systemone", GOOD)
        self.assertEqual((code, body), (500, {"error": "RuntimeError"}))

    def test_unknown_paths_are_404(self):
        r = Running(lambda: echo)
        self.assertEqual(r.call("GET", "/nope")[0], 404)
        self.assertEqual(r.call("POST", "/nope", GOOD)[0], 404)

    def test_load_failure_calls_on_fail_and_never_becomes_ready(self):
        failed = []
        def bad():
            raise OSError("no weights")
        r = Running(bad, failed.append)
        r.service.start()
        for _ in range(100):
            if failed:
                break
            time.sleep(0.02)
        self.assertEqual(len(failed), 1)
        self.assertFalse(r.service.ready)


if __name__ == "__main__":
    unittest.main()
