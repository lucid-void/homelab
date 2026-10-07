# Clef triage and news digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Cloudflare's Clef-flash decision model as an in-cluster CPU service and use it so the Telegram bot (a) forwards only needed low-priority Gotify messages and (b) sends a twice-daily digest of FreshRSS articles matching the owner's topics.

**Architecture:** A new `clef` Deployment in `ai` (pinned to `llm-1`) wraps the official `joint_schema_model.systemone` behind a tiny HTTP API. The existing `flight-tracker` bot gets a `Clef` client, a fail-open `triage` step inside `alerts.py`, and a new `news` module that reads FreshRSS through its Google Reader API. All code is stdlib Python except the Clef service itself.

**Tech Stack:** Python (stdlib `unittest`, `http.server`), `torch` 2.14.1 CPU + `transformers` 5.10.2 inside the Clef pod, Kustomize, Flux, SealedSecrets.

**Spec:** `docs/superpowers/specs/2026-10-06-clef-triage-and-news-design.md` (includes the spike result; read it first).

## Global Constraints

- Test commands (repo root, each must end `OK`): `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests` and `python3 -m unittest discover -s kubernetes/apps/ai/clef/tests`.
- All k8s tooling runs as `mise exec -- kubectl|kubeconform|kubeseal ...`. `kubectl` is for diagnostics only; every config change goes through git and Flux (merge to `main` deploys).
- After editing any manifest under `kubernetes/`, run `.agents/scripts/validate-manifests.sh <path>`.
- Never write a raw `Secret` into git; never set `spec.targetNamespace` on a Kustomization whose resources span namespaces; never use a rolling image tag.
- The bot stays stdlib-only Python; one `configMapGenerator` `files:` list delivers every file, and it must keep `namespace: monitoring`.
- Priority >= 8 Gotify messages are never judged; any Clef failure forwards the message (fail-open).
- Clef request bodies must stay under 64 KiB (the service refuses larger).
- Do not record deployed version numbers in `design/` docs except as durable constraints.
- End every commit message with the attribution trailers your session instructions specify.
- Work on branch `feat/clef-triage-news`; do not push or merge without the user's explicit yes (merging to `main` deploys to the live cluster and changes `llm-1`'s memory budget).

## Review Focus

- **A burst of low-priority Gotify messages while the news poll is scoring** (both share one single-threaded Clef): triage may time out after 15 s and forward. Expected: extra noise, never a lost message. Covered by the fail-open tests in Tasks 3 and 4.
- **A Gotify message with a huge body or no title/message**: must not break the request or be dropped. Covered by `test_long_title_and_message_are_cut_before_sending` and `test_missing_title_and_message_are_sent_as_empty_strings` (Task 3).
- **Muting during a slow triage pass**: the rest of the batch must not be sent. Covered by `test_muting_during_judging_stops_the_rest` (Task 4).
- **First start with an existing unread backlog in FreshRSS (possibly thousands)**: must not be scored or sent. Covered by `test_first_run_marks_the_backlog_seen_without_scoring` (Task 6).
- **Clef or FreshRSS down for hours**: no crash, one log line per streak, nothing marked seen so nothing is lost. Covered by `test_clef_down_leaves_articles_unseen_and_logs_once` and `test_freshrss_down_is_a_failed_poll_not_a_crash` (Task 6).
- **Article text is untrusted input** (it can say "ignore previous instructions"): Clef returns probabilities, not actions, so the worst case is a wrong match in the digest. No test; noted in the docs (Task 8).

---

## File map

| File | Responsibility |
|---|---|
| `kubernetes/apps/ai/clef/app/server.py` | Clef HTTP service: loads the model, serves `POST /v1/systemone` and `/healthz` |
| `kubernetes/apps/ai/clef/app/setup.sh`, `requirements*.txt` | initContainer: pinned venv and pinned weights onto the PVC, idempotent |
| `kubernetes/apps/ai/clef/app/{pvc,service,deployment,kustomization}.yml`, `clef/ks.yml` | Deployment on `llm-1`, Flux wiring |
| `kubernetes/apps/ai/llama-swap/app/helmrelease.yml` | memory request lowered 60Gi to 46Gi so Clef schedules |
| `kubernetes/apps/monitoring/flight-tracker/app/clef.py` | bot-side client for the Clef service |
| `kubernetes/apps/monitoring/flight-tracker/app/triage.py` | judge one Gotify message; always fail-open |
| `kubernetes/apps/monitoring/flight-tracker/app/alerts.py` | uses triage, records drops, `/alerts dropped` |
| `kubernetes/apps/monitoring/flight-tracker/app/freshrss.py` | FreshRSS Google Reader API client |
| `kubernetes/apps/monitoring/flight-tracker/app/news.py` | `/topics`, polling, digest |
| `kubernetes/apps/monitoring/flight-tracker/app/main.py`, `deployment.yml`, `kustomization.yml` | wiring |
| `design/decisions/llm.md`, `flight-tracker.md`, `design/docs/services.md` | docs |

---

### Task 1: Clef HTTP service code

**Files:**
- Create: `kubernetes/apps/ai/clef/app/server.py`
- Create: `kubernetes/apps/ai/clef/tests/test_server.py`

**Interfaces:**
- Produces: `server.Service(load, on_fail)` (`load() -> (body -> dict)`), `server.make_handler(service)`, `server.MAX_BODY = 65536`. HTTP: `POST /v1/systemone` takes a SystemOne body (`{"model","state","questions"}`) and returns the `systemone()` result (`{"model","answers":{id:{"type":"noul","noul":p}},"usage"}`); `GET /healthz` is 200 only once the model is loaded.

- [ ] **Step 1: Create the branch**

```bash
git switch -c feat/clef-triage-news
```
The working tree already holds two unrelated edits (`design/decisions/jellyfin.md`, `design/docs/storage.md`). They carry over to the branch; never stage them. Every commit in this plan names its files explicitly.

- [ ] **Step 2: Write the failing test**

Create `kubernetes/apps/ai/clef/tests/test_server.py`:

```python
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
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `python3 -m unittest discover -s kubernetes/apps/ai/clef/tests`
Expected: `ModuleNotFoundError: No module named 'server'`.

- [ ] **Step 4: Write the implementation**

Create `kubernetes/apps/ai/clef/app/server.py`:

```python
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
```

- [ ] **Step 5: Run the tests**

Run: `python3 -m unittest discover -s kubernetes/apps/ai/clef/tests`
Expected: `Ran 7 tests ... OK`.

- [ ] **Step 6: Commit**

```bash
git add kubernetes/apps/ai/clef
git commit -m "feat(clef): HTTP service wrapping the Clef-flash systemone call"
```

---

### Task 2: Clef manifests and the `llama-swap` memory request

**Files:**
- Create: `kubernetes/apps/ai/clef/app/{requirements-torch.txt,requirements.txt,setup.sh,pvc.yml,service.yml,deployment.yml,kustomization.yml}`
- Create: `kubernetes/apps/ai/clef/ks.yml`
- Modify: `kubernetes/apps/ai/kustomization.yml`
- Modify: `kubernetes/apps/ai/llama-swap/app/helmrelease.yml` (memory request)

**Interfaces:**
- Consumes: `server.py` from Task 1 (delivered by the `clef-scripts` ConfigMap).
- Produces: Service `clef.ai.svc.cluster.local:8080`, used by the bot in Task 7.

Why the `llama-swap` change: `llama-swap` requests 60Gi of the node's ~68.2 GiB and the DaemonSets request ~1.6 GiB, so a pod requesting 18Gi cannot schedule. 46Gi + 18Gi + 1.6Gi = 65.6 GiB fits. The risk this creates (real memory pressure on `llm-1`) is checked in Task 9 before this counts as done.

- [ ] **Step 1: Write the pinned requirements and setup script**

Create `kubernetes/apps/ai/clef/app/requirements-torch.txt`:

```text
torch==2.14.1+cpu
torchvision==0.29.1+cpu
```

Create `kubernetes/apps/ai/clef/app/requirements.txt`:

```text
transformers==5.10.2
accelerate==1.15.0
pillow==12.3.0
```

Create `kubernetes/apps/ai/clef/app/setup.sh`:

```sh
#!/bin/sh
# Installs the pinned Python dependencies and the pinned Clef-flash weights onto the PVC.
# Idempotent: a re-run with nothing changed does no network work.
set -eu

VENV=/data/venv
WANT="$(cat /scripts/requirements-torch.txt /scripts/requirements.txt | sha256sum | cut -d' ' -f1)"
HAVE="$(cat "$VENV/.installed" 2>/dev/null || true)"

if [ "$HAVE" != "$WANT" ]; then
  echo "== installing python dependencies"
  rm -rf "$VENV"
  python -m venv "$VENV"
  # timeout on every network step: an unbounded install wedges the pod Running with empty logs.
  # torch and torchvision come from the CPU wheel index (the PyPI build is the CUDA one, ~2 GB larger).
  timeout 1800 "$VENV/bin/pip" install --no-cache-dir --timeout 30 \
    --index-url https://download.pytorch.org/whl/cpu -r /scripts/requirements-torch.txt
  timeout 900 "$VENV/bin/pip" install --no-cache-dir --timeout 30 -r /scripts/requirements.txt
  echo "$WANT" > "$VENV/.installed"
else
  echo "== python dependencies up to date"
fi

SNAP="/data/hf/hub/models--Cloudflare--clef-flash/snapshots/${CLEF_REVISION}"
if [ ! -f "$SNAP/.complete" ]; then
  echo "== fetching Cloudflare/clef-flash @ ${CLEF_REVISION} (about 19 GB)"
  timeout 3600 "$VENV/bin/python" -c "
import os
from huggingface_hub import snapshot_download
snapshot_download('Cloudflare/clef-flash', revision=os.environ['CLEF_REVISION'])"
  touch "$SNAP/.complete"
else
  echo "== weights present"
fi
echo "== ready"
```

- [ ] **Step 2: Confirm the service code is in place**

`server.py` was created in Task 1 at `kubernetes/apps/ai/clef/app/server.py`; the ConfigMap generator below picks it up from there. Confirm: `ls kubernetes/apps/ai/clef/app/server.py`.

- [ ] **Step 3: Write the manifests**

Create `kubernetes/apps/ai/clef/app/pvc.yml`:

```yaml
---
# openebs-hostpath, node-local on llm-1, like llama-models: the weights are read on every
# start and the Deployment is pinned to that node. 19 GB weights + about 3 GB venv.
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: clef-data
  namespace: ai
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: openebs-hostpath
  resources:
    requests:
      storage: 30Gi
```

Create `kubernetes/apps/ai/clef/app/service.yml`:

```yaml
---
apiVersion: v1
kind: Service
metadata:
  name: clef
  namespace: ai
spec:
  selector:
    app.kubernetes.io/name: clef
  ports:
    - name: http
      port: 8080
      targetPort: 8080
```

Create `kubernetes/apps/ai/clef/app/deployment.yml`:

```yaml
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: clef
  namespace: ai
  annotations:
    reloader.stakater.com/auto: "true"
    # Root so the files on the openebs-hostpath volume (created root-owned) are writable,
    # the same reason llama-swap and model-fetch run as root.
    ignore-check.kube-linter.io/run-as-non-root: "hostpath volume is root-owned; same as llama-swap"
spec:
  replicas: 1
  strategy:
    type: Recreate   # RWO node-local volume: a rolling update would hang on the second pod
  selector:
    matchLabels:
      app.kubernetes.io/name: clef
  template:
    metadata:
      labels:
        app.kubernetes.io/name: clef
    spec:
      nodeSelector:
        kubernetes.io/hostname: llm-1
      tolerations:
        - key: workload
          operator: Equal
          value: llm
          effect: NoSchedule
      securityContext:
        runAsUser: 0
        runAsGroup: 0
        seccompProfile:
          type: RuntimeDefault
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: clef-data
        - name: scripts
          configMap:
            name: clef-scripts
        - name: tmp
          emptyDir:
            sizeLimit: 4Gi
      initContainers:
        - name: setup
          image: python:3.12.15-slim-bookworm
          command: ["/bin/sh", "/scripts/setup.sh"]
          env:
            - name: CLEF_REVISION
              value: 17f0b0ad64efb65d273590632833508766b2aae6
            - name: HF_HOME
              value: /data/hf
            - name: HOME
              value: /tmp
            - name: TMPDIR
              value: /tmp
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - {name: data, mountPath: /data}
            - {name: scripts, mountPath: /scripts, readOnly: true}
            - {name: tmp, mountPath: /tmp}
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {memory: 2Gi}
      containers:
        - name: clef
          image: python:3.12.15-slim-bookworm
          command: ["/data/venv/bin/python", "-u", "/scripts/server.py"]
          env:
            - name: CLEF_SNAPSHOT
              value: /data/hf/hub/models--Cloudflare--clef-flash/snapshots/17f0b0ad64efb65d273590632833508766b2aae6
            - name: CLEF_THREADS
              value: "6"
            - name: HF_HOME
              value: /data/hf
            - name: HF_HUB_OFFLINE
              value: "1"
            - name: HOME
              value: /tmp
            - name: PYTHONDONTWRITEBYTECODE
              value: "1"
          ports:
            - name: http
              containerPort: 8080
          readinessProbe:
            httpGet: {path: /healthz, port: http}
            initialDelaySeconds: 10
            periodSeconds: 15
            timeoutSeconds: 5
            failureThreshold: 4
          livenessProbe:
            tcpSocket: {port: http}
            initialDelaySeconds: 120
            periodSeconds: 30
            failureThreshold: 6
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - {name: data, mountPath: /data}
            - {name: scripts, mountPath: /scripts, readOnly: true}
            - {name: tmp, mountPath: /tmp}
          resources:
            # Measured 2026-10-06: peak 20.8 GiB (includes page cache of the weights).
            # The request is what the scheduler counts; llama-swap's was lowered to fit it.
            requests: {cpu: "2", memory: 18Gi}
            limits: {memory: 24Gi}
```

Create `kubernetes/apps/ai/clef/app/kustomization.yml`:

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./pvc.yml
  - ./deployment.yml
  - ./service.yml
configMapGenerator:
  - name: clef-scripts
    namespace: ai   # must match the Deployment's namespace or the hashed name is not propagated
    files:
      - server.py
      - setup.sh
      - requirements-torch.txt
      - requirements.txt
```

Create `kubernetes/apps/ai/clef/ks.yml`:

```yaml
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: &app clef
  namespace: flux-system
spec:
  targetNamespace: ai
  commonMetadata:
    labels:
      app.kubernetes.io/name: *app
  dependsOn:
    - name: openebs
  path: ./kubernetes/apps/ai/clef/app
  prune: true
  sourceRef:
    kind: GitRepository
    name: home-kubernetes
  # wait: false, like llama-swap: the first start installs about 3 GB of Python packages and
  # downloads 19 GB of weights, and the pod stays unready until then.
  wait: false
  interval: 30m
  timeout: 15m
```

- [ ] **Step 4: Register the app and lower the `llama-swap` request**

Apply this patch (adds ./clef/ks.yml):

```bash
git apply - <<'PATCH'
diff --git a/kubernetes/apps/ai/kustomization.yml b/kubernetes/apps/ai/kustomization.yml
index 26c9698..36c982b 100644
--- a/kubernetes/apps/ai/kustomization.yml
+++ b/kubernetes/apps/ai/kustomization.yml
@@ -5,5 +5,6 @@ resources:
   - ./namespace.yml
   - ./database/ks.yml
   - ./llama-swap/ks.yml
+  - ./clef/ks.yml
   - ./litellm/ks.yml
   - ./open-webui/ks.yml
PATCH
```

Apply this patch (memory request 60Gi to 46Gi plus the comment explaining it):

```bash
git apply - <<'PATCH'
diff --git a/kubernetes/apps/ai/llama-swap/app/helmrelease.yml b/kubernetes/apps/ai/llama-swap/app/helmrelease.yml
index afb0b45..161c272 100644
--- a/kubernetes/apps/ai/llama-swap/app/helmrelease.yml
+++ b/kubernetes/apps/ai/llama-swap/app/helmrelease.yml
@@ -92,7 +92,7 @@ spec:
             resources:
               requests:
                 cpu: 4
-                memory: 60Gi
+                memory: 46Gi
               # NO memory limit, deliberately. llama.cpp mmaps the GGUF, so those
               # pages count toward the cgroup as reclaimable page cache. A limit
               # anywhere near the working set does not OOMKill cleanly — it
@@ -101,10 +101,11 @@ spec:
               # nothing in the logs. The node is dedicated and tainted, so there
               # is nothing here to protect from an overrun.
               #
-              # 60Gi request, not 64Gi: allocatable is ~68.3 GiB after the 70 GB
-              # resize and the DaemonSets already request ~1 GiB, so 64Gi sits
-              # close enough to the ceiling that one more DaemonSet would make
-              # this pod unschedulable.
+              # 46Gi request: allocatable is ~68.3 GiB and the DaemonSets already
+              # request ~1.6 GiB, so this leaves room for the clef Deployment's 18Gi
+              # request (design/decisions/llm.md). It was 60Gi before clef existed. Real
+              # use is the 34.4 GiB weights plus up to 8 GiB prompt cache and the KV
+              # cache; if llm-1 is ever evicting llama-swap, this number is the first suspect.
               #
               # No CPU limit either, matching the house style — a CPU limit is
               # throttling, and throttling llama-server is directly a token-rate
PATCH
```

- [ ] **Step 5: Render and validate**

```bash
mise exec -- kubectl kustomize kubernetes/apps/ai/clef/app | mise exec -- kubeconform -strict -summary -
for f in kubernetes/apps/ai/clef/app/*.yml kubernetes/apps/ai/clef/ks.yml kubernetes/apps/ai/kustomization.yml kubernetes/apps/ai/llama-swap/app/helmrelease.yml; do .agents/scripts/validate-manifests.sh "$f"; done
sh -n kubernetes/apps/ai/clef/app/setup.sh && echo setup-sh-ok
mise exec -- kubectl kustomize kubernetes/apps/ai/clef/app | grep -c 'name: clef-scripts-'
```
Expected: kubeconform `Invalid: 0, Errors: 0`; every validate call exits 0 silently; `setup-sh-ok`; the last command prints `2` or more (the hashed ConfigMap name appears in the Deployment volume, proving the hash propagated).

- [ ] **Step 6: Commit**

```bash
git add kubernetes/apps/ai/clef kubernetes/apps/ai/kustomization.yml kubernetes/apps/ai/llama-swap/app/helmrelease.yml
git commit -m "feat(clef): deploy Clef-flash on llm-1, lower llama-swap memory request to 46Gi"
```

---

### Task 3: Bot-side Clef client and triage

**Files:**
- Create: `kubernetes/apps/monitoring/flight-tracker/app/clef.py`
- Create: `kubernetes/apps/monitoring/flight-tracker/app/triage.py`
- Create: `kubernetes/apps/monitoring/flight-tracker/tests/test_triage.py`

**Interfaces:**
- Produces: `clef.Clef(base_url, timeout=15, opener=urlopen).noul(state, {qid: instruction}) -> {qid: float}` (raises `clef.ClefError`); `triage.Triage(clef, threshold=0.5).judge(msg) -> bool` (True = forward; never raises); `triage.is_critical(msg) -> bool`; constants `triage.CRITICAL = 8`, `triage.MAX_TITLE = 200`, `triage.MAX_MESSAGE = 2000`.

- [ ] **Step 1: Write the failing tests**

Create `kubernetes/apps/monitoring/flight-tracker/tests/test_triage.py`:

```python
import io
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import clef  # noqa: E402
import triage  # noqa: E402


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_returning(payload):
    seen = []

    def opener(req, timeout):
        seen.append((req, timeout))
        if isinstance(payload, Exception):
            raise payload
        return FakeResp(json.dumps(payload).encode())
    opener.seen = seen
    return opener


def answer(**probs):
    return {"answers": {k: {"type": "noul", "noul": v} for k, v in probs.items()}}


class ClefTests(unittest.TestCase):
    def test_request_shape_and_result(self):
        op = opener_returning(answer(a=0.9, b=0.1))
        c = clef.Clef("http://clef:8080/", timeout=7, opener=op)
        self.assertEqual(c.noul("state", {"a": "A?", "b": "B?"}), {"a": 0.9, "b": 0.1})
        req, timeout = op.seen[0]
        self.assertEqual(req.full_url, "http://clef:8080/v1/systemone")
        self.assertEqual(timeout, 7)
        body = json.loads(req.data)
        self.assertEqual(body["model"], "clef-flash")
        self.assertEqual(body["state"], "state")
        self.assertEqual(body["questions"]["a"], {"type": "noul", "instructions": "A?"})

    def test_every_failure_becomes_clef_error(self):
        bad = [OSError("down"), urllib.error.URLError("x"), TimeoutError(),
               {}, {"answers": {}}, {"answers": {"a": {}}},
               answer(a="0.9"), answer(a=1.5), answer(a=-0.1), answer(a=True), "text"]
        for payload in bad:
            with self.subTest(payload=payload):
                c = clef.Clef("http://clef", opener=opener_returning(payload))
                with self.assertRaises(clef.ClefError):
                    c.noul("s", {"a": "A?"})


class FakeClef:
    def __init__(self, p=None, error=None):
        self.p, self.error, self.calls = p, error, []

    def noul(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        return {"needed": self.p}


def msg(priority=5, id=1, title="t", message="m"):
    return {"id": id, "priority": priority, "title": title, "message": message}


class TriageTests(unittest.TestCase):
    def test_high_priority_is_forwarded_without_asking_clef(self):
        f = FakeClef(p=0.0)
        self.assertTrue(triage.Triage(f).judge(msg(priority=8)))
        self.assertTrue(triage.Triage(f).judge(msg(priority=10)))
        self.assertEqual(f.calls, [])

    def test_missing_or_odd_priority_is_forwarded_without_asking_clef(self):
        f = FakeClef(p=0.0)
        for m in ({"id": 1}, msg(priority="high"), msg(priority=None), msg(priority=True)):
            self.assertTrue(triage.Triage(f).judge(m))
        self.assertEqual(f.calls, [])

    def test_threshold_decides_low_priority(self):
        self.assertFalse(triage.Triage(FakeClef(p=0.49)).judge(msg()))
        self.assertTrue(triage.Triage(FakeClef(p=0.5)).judge(msg()))
        self.assertTrue(triage.Triage(FakeClef(p=0.99)).judge(msg()))

    def test_clef_failure_forwards(self):
        self.assertTrue(triage.Triage(FakeClef(error=clef.ClefError("OSError"))).judge(msg()))

    def test_missing_title_and_message_are_sent_as_empty_strings(self):
        f = FakeClef(p=0.9)
        triage.Triage(f).judge({"id": 1, "priority": 5})
        self.assertEqual(f.calls[0][0], {"title": "", "message": "", "priority": 5})

    def test_long_title_and_message_are_cut_before_sending(self):
        f = FakeClef(p=0.9)
        triage.Triage(f).judge(msg(title="t" * 5000, message="m" * 500000))
        state = f.calls[0][0]
        self.assertEqual((len(state["title"]), len(state["message"])), (triage.MAX_TITLE, triage.MAX_MESSAGE))

    def test_is_critical(self):
        self.assertTrue(triage.is_critical(msg(priority=8)))
        self.assertFalse(triage.is_critical(msg(priority=7)))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `ModuleNotFoundError: No module named 'clef'`.

- [ ] **Step 3: Write the implementations**

Create `kubernetes/apps/monitoring/flight-tracker/app/clef.py`:

```python
#!/usr/bin/env python3
"""Client for the in-cluster Clef service (SystemOne request shape, `noul` questions only)."""
import json
import urllib.request


class ClefError(Exception):
    """Clef could not answer. Callers fail open (alerts) or retry later (news)."""


class Clef:
    def __init__(self, base_url, timeout=15, opener=urllib.request.urlopen):
        self.base_url, self.timeout, self.opener = base_url.rstrip("/"), timeout, opener

    def noul(self, state, questions):
        """questions: {id: instruction text}. Returns {id: probability the answer is yes}."""
        body = {"model": "clef-flash", "state": state,
                "questions": {qid: {"type": "noul", "instructions": text}
                              for qid, text in questions.items()}}
        req = urllib.request.Request(
            f"{self.base_url}/v1/systemone", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                answers = json.load(resp)["answers"]
            result = {}
            for qid in questions:
                p = answers[qid]["noul"]
                if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 <= p <= 1:
                    raise ValueError("probability")
                result[qid] = float(p)
            return result
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise ClefError(type(e).__name__) from e
```

Create `kubernetes/apps/monitoring/flight-tracker/app/triage.py`:

```python
#!/usr/bin/env python3
"""Decide whether a Gotify message is worth a Telegram message. Always fails open."""
from clef import ClefError
from core import log

CRITICAL = 8
THRESHOLD = 0.5
MAX_TITLE = 200
MAX_MESSAGE = 2000   # the Clef service refuses bodies over 64 KiB, which would silently fail open
QUESTION = ("Does this notification need the owner's attention? Yes for failures, outages, "
            "security findings or anything that needs action. No for routine success reports, "
            "tests and informational events.")


def is_critical(msg):
    """Priority >= 8, or no usable priority: never judged, always forwarded."""
    p = msg.get("priority")
    return isinstance(p, bool) or not isinstance(p, int) or p >= CRITICAL


class Triage:
    def __init__(self, clef, threshold=THRESHOLD):
        self.clef, self.threshold = clef, threshold

    def judge(self, msg):
        """True = forward. False only when Clef answered and said the message is noise."""
        if is_critical(msg):
            return True
        state = {"title": str(msg.get("title") or "")[:MAX_TITLE],
                 "message": str(msg.get("message") or "")[:MAX_MESSAGE],
                 "priority": msg.get("priority")}
        try:
            p = self.clef.noul(state, {"needed": QUESTION})["needed"]
        except ClefError as e:
            log(f"triage unavailable ({e}), forwarding message {msg.get('id')}")
            return True
        keep = p >= self.threshold
        log(f"triage message {msg.get('id')} p={p:.2f} {'forward' if keep else 'drop'}")
        return keep
```

- [ ] **Step 4: Run all bot tests**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `OK` (all existing tests plus the new ones).

- [ ] **Step 5: Commit**

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/clef.py kubernetes/apps/monitoring/flight-tracker/app/triage.py kubernetes/apps/monitoring/flight-tracker/tests/test_triage.py
git commit -m "feat(telegram-bot): Clef client and fail-open triage"
```

---

### Task 4: Use triage in `alerts.py`

**Files:**
- Modify: `kubernetes/apps/monitoring/flight-tracker/app/alerts.py`
- Modify: `kubernetes/apps/monitoring/flight-tracker/tests/test_alerts.py`

**Interfaces:**
- Consumes: `triage.Triage.judge(msg) -> bool`, `triage.is_critical(msg)` (Task 3).
- Produces: `alerts.Alerts(ctx, fetch, now=..., triage=None)`; `alerts.MAX_DROPS = 20`; state key `dropped` (list of `{id, title, priority, at}`, newest first); command `/alerts dropped`. With `triage=None` behaviour is unchanged.

What changes in `poll_once`: new messages are collected under the lock, judged **outside** it (a judgement takes ~4 s and `/alerts` must stay responsive), critical messages are sent first, and a mute set during judging stops the rest of the batch.

- [ ] **Step 1: Add the tests**

Apply this patch (imports plus a TriageTests class and helpers):

```bash
git apply - <<'PATCH'
diff --git a/kubernetes/apps/monitoring/flight-tracker/tests/test_alerts.py b/kubernetes/apps/monitoring/flight-tracker/tests/test_alerts.py
index 80020f4..e5b84c5 100644
--- a/kubernetes/apps/monitoring/flight-tracker/tests/test_alerts.py
+++ b/kubernetes/apps/monitoring/flight-tracker/tests/test_alerts.py
@@ -9,7 +9,9 @@ from unittest import mock
 
 sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
 import alerts  # noqa: E402
+import clef  # noqa: E402
 import core  # noqa: E402
+import triage  # noqa: E402
 
 UTC = timezone.utc
 T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
@@ -288,5 +290,116 @@ class ModuleTests(unittest.TestCase):
         self.assertTrue(a.help and all(line.startswith("/alerts") for line in a.help))
 
 
+
+class FakeJudge:
+    """Stands in for Triage: noise titles are dropped; `on_judge` runs after each judgement."""
+
+    def __init__(self, noise=(), on_judge=None):
+        self.noise, self.on_judge, self.judged = set(noise), on_judge, []
+
+    def judge(self, msg):
+        self.judged.append(msg["id"])
+        if self.on_judge:
+            self.on_judge(msg)
+        return msg["title"] not in self.noise
+
+
+class TriageHarness(Harness):
+    def __init__(self, judge):
+        self.judge = judge
+        super().__init__()
+
+    def make(self):
+        ctx = core.Ctx("alerts", lambda text, buttons=None: self.sent.append(text), self.path)
+        return alerts.Alerts(ctx, self._fetch, lambda: self.now, triage=self.judge)
+
+
+class TriageTests(unittest.TestCase):
+    def test_noise_is_dropped_and_needed_is_forwarded(self):
+        h = TriageHarness(FakeJudge(noise={"Backup: x"}))
+        h.seed(1)
+        h.poll([m(3, title="Needed"), m(2, title="Backup: x")])
+        self.assertEqual(h.sent, ["\U0001f7e1 Needed\nb"])
+        self.assertEqual(h.state()["last_id"], 3)
+
+    def test_critical_messages_are_sent_before_judged_ones(self):
+        h = TriageHarness(FakeJudge())
+        h.seed(1)
+        h.poll([m(4, title="low2"), m(3, title="CRIT", priority=9), m(2, title="low1")])
+        self.assertEqual([x.split("\n")[0] for x in h.sent],
+                         ["\U0001f534 CRIT", "\U0001f7e1 low1", "\U0001f7e1 low2"])
+
+    def test_real_triage_forwards_when_clef_is_down(self):
+        class Down:
+            def noul(self, state, questions):
+                raise clef.ClefError("OSError")
+        h = TriageHarness(triage.Triage(Down()))
+        h.seed(1)
+        h.poll([m(2, title="anything")])
+        self.assertEqual(len(h.sent), 1)
+        self.assertEqual(h.state()["dropped"], [])
+
+    def test_a_muted_poll_never_calls_triage(self):
+        j = FakeJudge()
+        h = TriageHarness(j)
+        h.seed(1)
+        h.say("off")
+        h.poll([m(2)])
+        self.assertEqual((h.sent[-1:], j.judged), (["Alerts: OFF (until you turn them on)"], []))
+
+    def test_muting_during_judging_stops_the_rest(self):
+        h = TriageHarness(FakeJudge(on_judge=lambda msg: h.say("off") if msg["id"] == 2 else None))
+        h.seed(1)
+        h.sent.clear()
+        h.poll([m(2), m(3)])
+        self.assertEqual(h.sent, ["Alerts: OFF (until you turn them on)"])
+        self.assertEqual(h.state()["last_id"], 3)
+
+    def test_dropped_command_lists_newest_first_and_survives_restart(self):
+        h = TriageHarness(FakeJudge(noise={"n1", "n2"}))
+        h.seed(1)
+        h.poll([m(2, title="n1")])
+        h.now = T0 + timedelta(minutes=5)
+        h.poll([m(3, title="n2")])
+        h.sent.clear()
+        h.alerts = h.make()
+        h.say("dropped")
+        text = h.sent[0]
+        self.assertIn("Last 2 messages", text)
+        self.assertLess(text.index("n2"), text.index("n1"))
+        self.assertIn("10-05 12:05", text)
+
+    def test_only_the_last_twenty_drops_are_kept(self):
+        h = TriageHarness(FakeJudge(noise={"n"}))
+        h.seed(1)
+        h.poll([m(i, title="n") for i in range(2, 30)])
+        self.assertEqual(len(h.state()["dropped"]), alerts.MAX_DROPS)
+        self.assertEqual(h.state()["dropped"][0]["id"], 29)
+
+    def test_dropped_command_without_triage_or_drops(self):
+        h = Harness()
+        h.say("dropped")
+        self.assertEqual(h.sent, ["Triage is off: every message is forwarded."])
+        t = TriageHarness(FakeJudge())
+        t.say("dropped")
+        self.assertEqual(t.sent, ["Triage has dropped nothing yet."])
+
+    def test_corrupt_dropped_state_is_reset_not_fatal(self):
+        h = TriageHarness(FakeJudge())
+        h.write(json.dumps({"last_id": 1, "muted": False, "until": None, "dropped": "oops"}))
+        h.alerts = h.make()
+        self.assertEqual(h.alerts.state["dropped"], [])
+
+    def test_old_state_file_without_dropped_still_loads(self):
+        h = TriageHarness(FakeJudge())
+        h.write(json.dumps({"last_id": 5, "muted": False, "until": None}))
+        h.alerts = h.make()
+        self.assertEqual((h.alerts.state["last_id"], h.alerts.state["dropped"]), (5, []))
+
+    def test_usage_mentions_dropped(self):
+        h = Harness()
+        h.say("bogus")
+        self.assertIn("dropped", h.sent[0])
+
 if __name__ == "__main__":
     unittest.main()
PATCH
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: the new tests fail (`TypeError ... unexpected keyword argument 'triage'`); existing tests still pass.

- [ ] **Step 3: Change `alerts.py`**

Apply this patch (triage parameter, dropped state, /alerts dropped, poll_once split):

```bash
git apply - <<'PATCH'
diff --git a/kubernetes/apps/monitoring/flight-tracker/app/alerts.py b/kubernetes/apps/monitoring/flight-tracker/app/alerts.py
index 8559256..ba7968f 100644
--- a/kubernetes/apps/monitoring/flight-tracker/app/alerts.py
+++ b/kubernetes/apps/monitoring/flight-tracker/app/alerts.py
@@ -8,12 +8,14 @@ import time
 import urllib.request
 from datetime import datetime, timedelta, timezone
 
+import triage as triage_mod
 from core import log
 
 UTC = timezone.utc
 POLL_SECONDS = 10
 MAX_MUTE = timedelta(days=7)
-USAGE = "Usage: /alerts [on | off [1h|4h|24h]]"
+MAX_DROPS = 20
+USAGE = "Usage: /alerts [on | off [1h|4h|24h] | dropped]"
 DURATION_RE = re.compile(r"^(\d{1,4})([hm])$")
 
 
@@ -59,10 +61,11 @@ class Alerts:
         "/alerts - show whether Gotify alerts are on",
         "/alerts off [1h|4h|24h] - mute them (no duration = until on)",
         "/alerts on - unmute",
+        "/alerts dropped - the last messages triage kept out of Telegram",
     ]
 
-    def __init__(self, ctx, fetch, now=lambda: datetime.now(UTC)):
-        self.ctx, self.fetch, self.now = ctx, fetch, now
+    def __init__(self, ctx, fetch, now=lambda: datetime.now(UTC), triage=None):
+        self.ctx, self.fetch, self.now, self.triage = ctx, fetch, now, triage
         self.lock = threading.RLock()
         self.failing = False
         self.commands = {"alerts": self._command}
@@ -72,7 +75,7 @@ class Alerts:
     # -- persistence --
 
     def _load(self):
-        default = {"last_id": None, "muted": False, "until": None}
+        default = {"last_id": None, "muted": False, "until": None, "dropped": []}
         try:
             with open(self.ctx.state_path) as f:
                 raw = json.load(f)
@@ -87,7 +90,10 @@ class Alerts:
         if not ok:
             log("alerts state has the wrong shape, alerts stay on")
             return default
-        return {key: raw.get(key) for key in default}
+        state = {key: raw.get(key) for key in default}
+        if not isinstance(state["dropped"], list):
+            state["dropped"] = []
+        return state
 
     def _save(self):
         tmp = self.ctx.state_path + ".tmp"
@@ -126,6 +132,8 @@ class Alerts:
                 self.state["muted"], self.state["until"] = False, None
                 self._save()
                 self.ctx.send(self._status(now))
+            elif args == ["dropped"]:
+                self.ctx.send(self._dropped_text())
             elif args[0] == "off" and len(args) <= 2:
                 until = None
                 if len(args) == 2:
@@ -140,6 +148,24 @@ class Alerts:
             else:
                 self.ctx.send(USAGE)
 
+    # -- triage drops --
+
+    def _record_drop(self, msg):
+        with self.lock:
+            entry = {"id": msg["id"], "title": msg.get("title") or "", "priority": msg.get("priority"),
+                     "at": self.now().isoformat()}
+            self.state["dropped"] = ([entry] + self.state["dropped"])[:MAX_DROPS]
+            self._save()
+
+    def _dropped_text(self):
+        if self.triage is None:
+            return "Triage is off: every message is forwarded."
+        drops = self.state["dropped"]
+        if not drops:
+            return "Triage has dropped nothing yet."
+        lines = [f"- {(d.get('at') or '')[5:16].replace('T', ' ')} {d.get('title') or '(no title)'}" for d in drops]
+        return f"Last {len(drops)} messages kept out of Telegram (still in Gotify):\n" + "\n".join(lines)
+
     # -- forwarder --
 
     def poll_once(self):
@@ -163,14 +189,27 @@ class Alerts:
             elif top < last:
                 log("gotify ids went backwards, assuming a database reset")
                 last = 0
-            if not self._is_muted(self.now()):
-                for msg in valid:
-                    if msg["id"] > last:
-                        self.ctx.send(format_message(msg))
+            new = [x for x in valid if x["id"] > last]
+            muted = self._is_muted(self.now())
+        if not muted:
+            self._deliver(new)  # outside the lock: judging is slow and /alerts must stay responsive
+        with self.lock:
             self.state["last_id"] = max(last, top)
             self._save()
         return True
 
+    def _deliver(self, new):
+        if self.triage is not None:  # critical messages first, so a slow burst never delays them
+            new = sorted(new, key=lambda x: (not triage_mod.is_critical(x), x["id"]))
+        for msg in new:
+            if self.triage is not None and not self.triage.judge(msg):
+                self._record_drop(msg)
+                continue
+            with self.lock:
+                if self._is_muted(self.now()):  # muted while we were judging
+                    return
+            self.ctx.send(format_message(msg))
+
     def start(self):
         threading.Thread(target=self._loop, daemon=True).start()
 
PATCH
```

- [ ] **Step 4: Run all bot tests**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/alerts.py kubernetes/apps/monitoring/flight-tracker/tests/test_alerts.py
git commit -m "feat(telegram-bot): alerts judges low-priority Gotify messages with triage"
```

---

### Task 5: FreshRSS client

**Files:**
- Create: `kubernetes/apps/monitoring/flight-tracker/app/freshrss.py`
- Create: `kubernetes/apps/monitoring/flight-tracker/tests/test_freshrss.py`

**Interfaces:**
- Produces: `freshrss.FreshRSS(base_url, user, password, timeout=20, opener=urlopen).unread(limit=100) -> [{"id","title","summary","url"}]` newest first; raises `freshrss.FreshRSSError` for every failure. `freshrss.plain(html, limit)`; `freshrss.SUMMARY_MAX = 600`.
- The user and API password are FreshRSS's *API password* (Settings, Profile, "API password"), used with the Google Reader API at `/api/greader.php`. It bypasses OIDC and 2FA.

- [ ] **Step 1: Write the failing tests**

Create `kubernetes/apps/monitoring/flight-tracker/tests/test_freshrss.py`:

```python
import io
import json
import os
import sys
import unittest
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import freshrss  # noqa: E402


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeServer:
    """Answers ClientLogin, then the stream request."""

    def __init__(self, items=None, login="SID=x\nLSID=y\nAuth=alice/tok123\n", error=None):
        self.items, self.login, self.error, self.requests = items, login, error, []

    def __call__(self, req, timeout):
        self.requests.append(req)
        if self.error:
            raise self.error
        if req.full_url.endswith("/accounts/ClientLogin"):
            return Resp(self.login.encode())
        return Resp(json.dumps({"items": self.items}).encode())


def item(id="tag:google.com,2005:reader/item/1", title="T", content="<p>Hello &amp; <b>bye</b></p>",
         href="https://x.example/a"):
    return {"id": id, "title": title, "summary": {"content": content}, "canonical": [{"href": href}]}


class FreshRSSTests(unittest.TestCase):
    def client(self, server):
        return freshrss.FreshRSS("http://freshrss/", "alice", "pw", opener=server)

    def test_login_then_unread_request_shape(self):
        s = FakeServer([item()])
        self.client(s).unread(limit=50)
        login, stream = s.requests
        self.assertEqual(login.full_url, "http://freshrss/api/greader.php/accounts/ClientLogin")
        self.assertEqual(parse_qs(login.data.decode()), {"Email": ["alice"], "Passwd": ["pw"]})
        url = urlparse(stream.full_url)
        self.assertEqual(url.path, "/api/greader.php/reader/api/0/stream/contents/reading-list")
        self.assertEqual(parse_qs(url.query), {"xt": ["user/-/state/com.google/read"], "n": ["50"], "output": ["json"]})
        self.assertEqual(stream.get_header("Authorization"), "GoogleLogin auth=alice/tok123")

    def test_articles_are_flattened_to_plain_text(self):
        [a] = self.client(FakeServer([item()])).unread()
        self.assertEqual(a, {"id": "tag:google.com,2005:reader/item/1", "title": "T",
                             "summary": "Hello & bye", "url": "https://x.example/a"})

    def test_long_summary_is_cut_and_missing_fields_are_tolerated(self):
        long = item(content="x" * 5000)
        bare = {"id": "tag:google.com,2005:reader/item/2"}
        a, b = self.client(FakeServer([long, bare, "junk", {"title": "no id"}])).unread()
        self.assertEqual(len(a["summary"]), freshrss.SUMMARY_MAX)
        self.assertEqual((b["title"], b["summary"], b["url"]), ("(no title)", "", ""))

    def test_login_without_auth_line_is_an_error(self):
        with self.assertRaises(freshrss.FreshRSSError):
            self.client(FakeServer([], login="Error=BadAuthentication")).unread()

    def test_network_and_shape_failures_are_freshrss_errors(self):
        for s in (FakeServer(error=OSError("down")), FakeServer(items=None)):
            with self.subTest(s=s), self.assertRaises(freshrss.FreshRSSError):
                self.client(s).unread()

    def test_plain(self):
        self.assertEqual(freshrss.plain("<a href='x'>a</a>  &lt;b&gt;\n c"), "a <b> c")
        self.assertEqual(freshrss.plain(None), "")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `ModuleNotFoundError: No module named 'freshrss'`.

- [ ] **Step 3: Write the implementation**

Create `kubernetes/apps/monitoring/flight-tracker/app/freshrss.py`:

```python
#!/usr/bin/env python3
"""FreshRSS over its Google Reader compatible API (user + API password; no OIDC involved)."""
import html
import json
import re
import urllib.parse
import urllib.request

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
SUMMARY_MAX = 600


class FreshRSSError(Exception):
    pass


def plain(text, limit=SUMMARY_MAX):
    return SPACE_RE.sub(" ", html.unescape(TAG_RE.sub(" ", text or ""))).strip()[:limit]


class FreshRSS:
    def __init__(self, base_url, user, password, timeout=20, opener=urllib.request.urlopen):
        self.base, self.user, self.password = base_url.rstrip("/") + "/api/greader.php", user, password
        self.timeout, self.opener = timeout, opener

    def _login(self):
        data = urllib.parse.urlencode({"Email": self.user, "Passwd": self.password}).encode()
        with self.opener(urllib.request.Request(f"{self.base}/accounts/ClientLogin", data=data),
                         timeout=self.timeout) as resp:
            for line in resp.read().decode().splitlines():
                if line.startswith("Auth="):
                    return line[5:]
        raise FreshRSSError("login")

    def unread(self, limit=100):
        """Newest-first unread articles as {id, title, summary, url}."""
        try:
            token = self._login()
            query = urllib.parse.urlencode({"xt": "user/-/state/com.google/read", "n": limit, "output": "json"})
            req = urllib.request.Request(
                f"{self.base}/reader/api/0/stream/contents/reading-list?{query}",
                headers={"Authorization": f"GoogleLogin auth={token}"})
            with self.opener(req, timeout=self.timeout) as resp:
                items = json.load(resp)["items"]
            articles = []
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                links = item.get("canonical") or item.get("alternate") or [{}]
                articles.append({
                    "id": item["id"],
                    "title": plain(item.get("title"), 300) or "(no title)",
                    "summary": plain((item.get("summary") or {}).get("content")),
                    "url": links[0].get("href") or "",
                })
            return articles
        except FreshRSSError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as e:
            raise FreshRSSError(type(e).__name__) from e
```

- [ ] **Step 4: Run all bot tests**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/freshrss.py kubernetes/apps/monitoring/flight-tracker/tests/test_freshrss.py
git commit -m "feat(telegram-bot): FreshRSS Google Reader API client"
```

---

### Task 6: News module

**Files:**
- Create: `kubernetes/apps/monitoring/flight-tracker/app/news.py`
- Create: `kubernetes/apps/monitoring/flight-tracker/tests/test_news.py`

**Interfaces:**
- Consumes: `clef.Clef.noul`, `clef.ClefError` (Task 3); `freshrss.FreshRSS.unread`, `freshrss.FreshRSSError` (Task 5).
- Produces: `news.News(ctx, chat_id, source, clef, now=..., tz=UTC, digest_hours=(8, 18))` with `commands = {"topics": fn}`, `poll_once() -> bool`, `maybe_digest() -> bool`, `start()`. State in `/data/news.json`: `topics` keyed by chat id, `seen`, `pending`, `last_slot`.
- Behaviour: first poll only records the backlog as seen; one Clef call per article scores all topics; at most 40 articles per poll; digest sent once per 08:00 and 18:00 slot (local `tz`), nothing sent when there are no matches; the first ever digest check only starts the schedule.

- [ ] **Step 1: Write the failing tests**

Create `kubernetes/apps/monitoring/flight-tracker/tests/test_news.py`:

```python
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import clef  # noqa: E402
import core  # noqa: E402
import freshrss  # noqa: E402
import news  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
CHAT = "42"


def art(n, title=None, url=None):
    return {"id": f"a{n}", "title": title or f"title {n}", "summary": f"sum {n}", "url": url or f"https://x/{n}"}


class FakeSource:
    def __init__(self):
        self.batches = []

    def unread(self):
        r = self.batches.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeClef:
    """Scores every topic by whether the topic word appears in the article title."""

    def __init__(self):
        self.error, self.calls = None, []

    def noul(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        return {qid: 0.9 if text.split(": ", 1)[1].split()[0].rstrip("?").lower() in state["title"].lower() else 0.1
                for qid, text in questions.items()}


class Harness:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        unittest.addModuleCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "news.json")
        self.now, self.sent = T0, []
        self.source, self.clef = FakeSource(), FakeClef()
        self.news = self.make()

    def make(self):
        ctx = core.Ctx("news", lambda text, buttons=None: self.sent.append(text), self.path)
        return news.News(ctx, CHAT, self.source, self.clef, now=lambda: self.now)

    def say(self, *args):
        self.news.commands["topics"](list(args))

    def poll(self, batch):
        self.source.batches.append(batch)
        return self.news.poll_once()

    def seed(self, batch=()):
        self.poll(list(batch))

    def state(self):
        with open(self.path) as f:
            return json.load(f)


class TopicCommandTests(unittest.TestCase):
    def test_empty_list_explains_how_to_add(self):
        h = Harness()
        h.say()
        self.assertIn("/topics add", h.sent[0])

    def test_add_list_and_remove_by_name_or_number(self):
        h = Harness()
        h.say("add", "kubernetes", "security")
        h.say("add", "football")
        self.assertEqual(h.state()["topics"][CHAT], ["kubernetes security", "football"])
        h.say("rm", "FOOTBALL")
        h.say("rm", "1")
        self.assertEqual(h.state()["topics"][CHAT], [])

    def test_duplicates_overlong_and_too_many_are_refused(self):
        h = Harness()
        h.say("add", "Rust")
        h.say("add", "rust")
        self.assertIn("Already following", h.sent[-1])
        h.say("add", "x" * (news.MAX_TOPIC_LEN + 1))
        self.assertIn("too long", h.sent[-1])
        for i in range(news.MAX_TOPICS):
            h.say("add", f"t{i}")
        self.assertIn("At most", h.sent[-1])

    def test_unknown_removal_and_bad_usage(self):
        h = Harness()
        h.say("rm", "nothing")
        self.assertIn("Not following", h.sent[-1])
        h.say("rm", "9")
        self.assertIn("Not following", h.sent[-1])
        h.say("add")
        self.assertEqual(h.sent[-1], news.USAGE)
        h.say("bogus")
        self.assertEqual(h.sent[-1], news.USAGE)

    def test_topics_survive_restart(self):
        h = Harness()
        h.say("add", "rust")
        h.news = h.make()
        self.assertEqual(h.news._topics(), ["rust"])


class PollTests(unittest.TestCase):
    def test_first_run_marks_the_backlog_seen_without_scoring(self):
        h = Harness()
        h.say("add", "rust")
        h.seed([art(1, "rust news"), art(2)])
        self.assertEqual(h.clef.calls, [])
        self.assertEqual(h.state()["seen"], ["a1", "a2"])

    def test_new_matching_article_becomes_pending_once(self):
        h = Harness()
        h.say("add", "rust")
        h.seed([art(1)])
        h.poll([art(3, "Rust 2.0 released"), art(1)])
        self.assertEqual(h.state()["pending"], [{"id": "a3", "title": "Rust 2.0 released",
                                                 "url": "https://x/3", "topics": ["rust"]}])
        h.poll([art(3, "Rust 2.0 released"), art(1)])
        self.assertEqual(len(h.state()["pending"]), 1)
        self.assertEqual(len(h.clef.calls), 1)

    def test_one_call_scores_all_topics(self):
        h = Harness()
        h.say("add", "rust")
        h.say("add", "python")
        h.seed()
        h.poll([art(2, "Python and Rust")])
        self.assertEqual(len(h.clef.calls), 1)
        self.assertEqual(h.state()["pending"][0]["topics"], ["rust", "python"])

    def test_non_matching_article_is_seen_but_not_pending(self):
        h = Harness()
        h.say("add", "rust")
        h.seed()
        h.poll([art(2, "gardening")])
        self.assertEqual((h.state()["seen"], h.state()["pending"]), (["a2"], []))

    def test_clef_down_leaves_articles_unseen_and_logs_once(self):
        h = Harness()
        h.say("add", "rust")
        h.seed()
        h.clef.error = clef.ClefError("OSError")
        self.assertFalse(h.poll([art(2, "rust")]))
        self.assertFalse(h.poll([art(2, "rust")]))
        self.assertEqual(h.state()["seen"], [])
        h.clef.error = None
        self.assertTrue(h.poll([art(2, "rust")]))
        self.assertEqual(len(h.state()["pending"]), 1)

    def test_freshrss_down_is_a_failed_poll_not_a_crash(self):
        h = Harness()
        h.seed()
        self.assertFalse(h.poll(freshrss.FreshRSSError("OSError")))

    def test_no_topics_means_nothing_is_scored_and_nothing_is_held_back(self):
        h = Harness()
        h.seed()
        h.poll([art(2, "rust")])
        self.assertEqual((h.clef.calls, h.state()["seen"]), ([], ["a2"]))

    def test_a_poll_scores_at_most_max_per_poll_and_keeps_the_rest_for_next_time(self):
        h = Harness()
        h.say("add", "rust")
        h.seed()
        batch = [art(i, "rust") for i in range(2, news.MAX_PER_POLL + 12)]
        h.poll(batch)
        self.assertEqual(len(h.clef.calls), news.MAX_PER_POLL)
        h.poll(batch)
        self.assertEqual(len(h.clef.calls), len(batch))

    def test_seen_list_is_bounded(self):
        h = Harness()
        h.seed()
        h.poll([art(i) for i in range(news.MAX_PER_POLL)])
        h.news.state["seen"] = [f"old{i}" for i in range(news.MAX_SEEN)]
        h.poll([art(1000)])
        self.assertEqual(len(h.state()["seen"]), news.MAX_SEEN)
        self.assertEqual(h.state()["seen"][-1], "a1000")

    def test_corrupt_or_wrong_shape_state_starts_empty(self):
        for content in ("{nope", json.dumps({"topics": "x"}), json.dumps([1]),
                        json.dumps({"topics": {"1": [1]}, "pending": []})):
            with self.subTest(content=content):
                h = Harness()
                with open(h.path, "w") as f:
                    f.write(content)
                h.news = h.make()
                self.assertEqual(h.news.state["topics"], {})
                self.assertIsNone(h.news.state["seen"])


class DigestTests(unittest.TestCase):
    def pending(self, h, n=1):
        h.say("add", "rust")
        h.seed()
        h.poll([art(i, "rust") for i in range(2, 2 + n)])

    def test_first_check_sets_the_slot_and_sends_nothing(self):
        h = Harness()
        self.assertFalse(h.news.maybe_digest())
        self.assertEqual(h.state()["last_slot"], "2026-10-05T18")

    def test_digest_goes_out_when_the_slot_opens_and_clears_pending(self):
        h = Harness()
        self.pending(h, 2)
        h.news.maybe_digest()
        h.now = T0 + timedelta(hours=1)  # 08:00 UTC
        self.assertTrue(h.news.maybe_digest())
        self.assertIn("News digest: 2 matches", h.sent[-1])
        self.assertIn("https://x/2", h.sent[-1])
        self.assertEqual(h.state()["pending"], [])
        self.assertFalse(h.news.maybe_digest())  # same slot: once only

    def test_empty_digest_sends_nothing_but_closes_the_slot(self):
        h = Harness()
        h.news.maybe_digest()
        h.now = T0 + timedelta(hours=1)
        self.assertFalse(h.news.maybe_digest())
        self.assertEqual(h.sent, [])
        self.assertEqual(h.state()["last_slot"], "2026-10-06T08")

    def test_second_slot_of_the_day(self):
        h = Harness()
        self.pending(h)
        h.now = T0 + timedelta(hours=1)
        h.news.maybe_digest()
        h.poll([art(9, "rust again")])
        h.now = T0 + timedelta(hours=11)  # 18:00
        self.assertTrue(h.news.maybe_digest())
        self.assertEqual(h.state()["last_slot"], "2026-10-06T18")

    def test_a_late_start_still_sends_the_missed_slot(self):
        h = Harness()
        self.pending(h)
        h.news.maybe_digest()
        h.now = T0 + timedelta(hours=4)  # 11:00, slot 08 opened earlier
        self.assertTrue(h.news.maybe_digest())

    def test_long_digest_is_split_and_the_rest_stays_pending(self):
        h = Harness()
        self.pending(h, 1)
        h.news.state["pending"] = [{"id": f"p{i}", "title": "t" * 200, "url": "https://x/" + "u" * 100,
                                    "topics": ["rust"]} for i in range(30)]
        h.news.maybe_digest()
        h.now = T0 + timedelta(hours=1)
        h.news.maybe_digest()
        self.assertLessEqual(len(h.sent[-1]), 4096)
        self.assertIn("more in the next digest", h.sent[-1])
        self.assertGreater(len(h.state()["pending"]), 0)

    def test_pending_is_bounded(self):
        h = Harness()
        h.seed()
        h.news._finish([], [{"id": str(i), "title": "t", "url": "", "topics": ["x"]}
                            for i in range(news.MAX_PENDING + 20)])
        self.assertEqual(len(h.state()["pending"]), news.MAX_PENDING)

    def test_module_protocol(self):
        h = Harness()
        self.assertEqual(set(h.news.commands), {"topics"})
        self.assertEqual(h.news.callbacks, {})
        self.assertTrue(all(isinstance(x, str) for x in h.news.help))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `ModuleNotFoundError: No module named 'news'`.

- [ ] **Step 3: Write the implementation**

Create `kubernetes/apps/monitoring/flight-tracker/app/news.py`:

```python
#!/usr/bin/env python3
"""/topics and a twice-daily digest of FreshRSS articles that match the owner's topics."""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from clef import ClefError
from core import log
from freshrss import FreshRSSError

UTC = timezone.utc
POLL_SECONDS = 1800
LOOP_SECONDS = 30
MAX_PER_POLL = 40       # about 7 s each on CPU, so a poll stays under 5 minutes
MAX_TOPICS = 10
MAX_TOPIC_LEN = 80
MAX_SEEN = 5000
MAX_PENDING = 200
DIGEST_MAX_CHARS = 3500
THRESHOLD = 0.5
USAGE = "Usage: /topics [add <topic> | rm <topic or number>]"


class News:
    name = "news"
    help = [
        "/topics - the news topics you follow",
        "/topics add <topic> - follow a topic",
        "/topics rm <topic or number> - unfollow",
    ]

    def __init__(self, ctx, chat_id, source, clef, now=lambda: datetime.now(UTC), tz=UTC,
                 digest_hours=(8, 18)):
        self.ctx, self.chat, self.source, self.clef = ctx, str(chat_id), source, clef
        self.now, self.tz, self.hours = now, tz, tuple(sorted(digest_hours))
        self.lock = threading.RLock()
        self.failing = False
        self.commands = {"topics": self._command}
        self.callbacks = {}
        self.state = self._load()

    # -- persistence --

    def _load(self):
        default = {"topics": {}, "seen": None, "pending": [], "last_slot": None}
        try:
            with open(self.ctx.state_path) as f:
                raw = json.load(f)
        except FileNotFoundError:
            return default
        except ValueError:
            log("news state unreadable, starting empty")
            return default
        ok = (isinstance(raw, dict) and isinstance(raw.get("topics"), dict)
              and all(isinstance(v, list) and all(isinstance(t, str) for t in v)
                      for v in raw["topics"].values())
              and (raw.get("seen") is None or isinstance(raw["seen"], list))
              and isinstance(raw.get("pending"), list)
              and (raw.get("last_slot") is None or isinstance(raw["last_slot"], str)))
        if not ok:
            log("news state has the wrong shape, starting empty")
            return default
        return {key: raw.get(key) for key in default}

    def _save(self):
        tmp = self.ctx.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.ctx.state_path)

    # -- topics --

    def _topics(self):
        return self.state["topics"].setdefault(self.chat, [])

    def _list_text(self):
        topics = self._topics()
        if not topics:
            return "No topics yet. Add one: /topics add kubernetes security"
        return "Topics:\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(topics, 1))

    def _command(self, args):
        with self.lock:
            topics = self._topics()
            if not args:
                self.ctx.send(self._list_text())
            elif args[0] == "add" and len(args) > 1:
                text = " ".join(args[1:]).strip()
                if len(text) > MAX_TOPIC_LEN:
                    self.ctx.send(f"Topic too long (max {MAX_TOPIC_LEN} characters).")
                elif text.lower() in (t.lower() for t in topics):
                    self.ctx.send(f"Already following: {text}")
                elif len(topics) >= MAX_TOPICS:
                    self.ctx.send(f"At most {MAX_TOPICS} topics. Remove one first.")
                else:
                    topics.append(text)
                    self._save()
                    self.ctx.send(f"Following: {text}\n\n" + self._list_text())
            elif args[0] == "rm" and len(args) > 1:
                text = " ".join(args[1:]).strip()
                index = int(text) - 1 if text.isdigit() else next(
                    (i for i, t in enumerate(topics) if t.lower() == text.lower()), -1)
                if 0 <= index < len(topics):
                    removed = topics.pop(index)
                    self._save()
                    self.ctx.send(f"Unfollowed: {removed}\n\n" + self._list_text())
                else:
                    self.ctx.send(f"Not following: {text}")
            else:
                self.ctx.send(USAGE)

    # -- polling --

    def poll_once(self):
        try:
            articles = self.source.unread()
        except FreshRSSError as e:
            self._fail(f"freshrss unavailable ({e})")
            return False
        with self.lock:
            topics = list(self._topics())
            if self.state["seen"] is None:  # first run: do not replay the whole unread backlog
                self.state["seen"] = [a["id"] for a in articles]
                self._save()
                self.failing = False
                return True
            seen = set(self.state["seen"])
        fresh = [a for a in articles if a["id"] not in seen]
        if not topics:  # nothing to match against; do not hold these back for a later topic
            self._finish(fresh, [])
            return True
        scored, matches = [], []
        for article in fresh[:MAX_PER_POLL]:
            questions = {f"t{i}": f"Is this article about: {t}?" for i, t in enumerate(topics)}
            try:
                probs = self.clef.noul({"title": article["title"], "summary": article["summary"]}, questions)
            except ClefError as e:
                self._fail(f"clef unavailable ({e}), articles stay unseen")
                break
            scored.append(article)
            hit = [t for i, t in enumerate(topics) if probs[f"t{i}"] >= THRESHOLD]
            if hit:
                matches.append({"id": article["id"], "title": article["title"],
                                "url": article["url"], "topics": hit})
        else:
            self.failing = False
        self._finish(scored, matches)
        return not self.failing

    def _fail(self, text):
        if not self.failing:
            log(text)
        self.failing = True

    def _finish(self, scored, matches):
        with self.lock:
            self.state["seen"] = (self.state["seen"] + [a["id"] for a in scored])[-MAX_SEEN:]
            self.state["pending"] = (self.state["pending"] + matches)[-MAX_PENDING:]
            self._save()

    # -- digest --

    def _slot(self):
        """The most recent scheduled digest time at or before now, as 'YYYY-MM-DDTHH' in local time."""
        local = self.now().astimezone(self.tz)
        past = [h for h in self.hours if h <= local.hour]
        day, hour = (local, past[-1]) if past else (local - timedelta(days=1), self.hours[-1])
        return f"{day.date().isoformat()}T{hour:02d}"

    def maybe_digest(self):
        with self.lock:
            slot = self._slot()
            last = self.state["last_slot"]
            if last is not None and slot <= last:
                return False
            self.state["last_slot"] = slot
            if last is None:  # first ever check: start the schedule, do not send on install
                self._save()
                return False
            pending = self.state["pending"]
            if not pending:
                self._save()
                return False
            text, used = self._digest_text(pending)
            self.state["pending"] = pending[used:]
            self._save()
        self.ctx.send(text)
        return True

    def _digest_text(self, pending):
        lines, size, used = [], 0, 0
        for item in pending:
            line = f"- {item['title']} ({', '.join(item['topics'])})\n  {item['url']}".rstrip()
            if used and size + len(line) > DIGEST_MAX_CHARS:
                break
            lines.append(line)
            size += len(line) + 1
            used += 1
        head = f"News digest: {used} match{'es' if used != 1 else ''}"
        if used < len(pending):
            lines.append(f"+{len(pending) - used} more in the next digest")
        return head + "\n\n" + "\n".join(lines), used

    # -- loop --

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        next_poll = 0.0
        while True:
            try:
                if time.monotonic() >= next_poll:
                    self.poll_once()
                    next_poll = time.monotonic() + POLL_SECONDS
                self.maybe_digest()
            except Exception as e:  # keep the module alive
                log(f"news loop failed: {type(e).__name__}")
            time.sleep(LOOP_SECONDS)
```

- [ ] **Step 4: Run all bot tests**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/news.py kubernetes/apps/monitoring/flight-tracker/tests/test_news.py
git commit -m "feat(telegram-bot): news module with /topics and a twice-daily digest"
```

---

### Task 7: Wire the bot

**Files:**
- Modify: `kubernetes/apps/monitoring/flight-tracker/app/main.py`
- Modify: `kubernetes/apps/monitoring/flight-tracker/app/deployment.yml`
- Modify: `kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml`

**Interfaces:**
- Consumes: everything from Tasks 3 to 6.
- Env contract: `CLEF_URL` (default `http://clef.ai.svc.cluster.local:8080`), `FRESHRSS_URL`, `FRESHRSS_USER` + `FRESHRSS_API_PASSWORD` (from the sealed `freshrss-api-secret`; without them the news module stays off), `DIGEST_TZ` (default `Europe/Brussels`, falls back to UTC with a log line), `DIGEST_HOURS` (default `8,18`).
- The secret is `optional: true` on `envFrom`. That is honoured here because this is a plain Deployment, not the app-template chart, where it would be stripped silently.

- [ ] **Step 1: Apply the patches**

Apply this patch (imports, digest_tz(), triage and news registration):

```bash
git apply - <<'PATCH'
diff --git a/kubernetes/apps/monitoring/flight-tracker/app/main.py b/kubernetes/apps/monitoring/flight-tracker/app/main.py
index aa91949..d0cef2d 100644
--- a/kubernetes/apps/monitoring/flight-tracker/app/main.py
+++ b/kubernetes/apps/monitoring/flight-tracker/app/main.py
@@ -2,11 +2,29 @@
 """Bot entrypoint: build the core, register modules, run. A new feature is one new file plus one
 `bot.register(...)` line below."""
 import os
+from datetime import timezone
+from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
 
 import alerts
+import clef
 import core
 import flights
+import freshrss
+import news
 import status
+import triage
+
+DEFAULT_CLEF_URL = "http://clef.ai.svc.cluster.local:8080"
+DEFAULT_FRESHRSS_URL = "http://freshrss.freshrss.svc.cluster.local"
+
+
+def digest_tz():
+    name = os.environ.get("DIGEST_TZ", "Europe/Brussels")
+    try:
+        return ZoneInfo(name)
+    except ZoneInfoNotFoundError:
+        core.log(f"timezone {name} not found, digest times are UTC")
+        return timezone.utc
 
 
 def main():
@@ -22,8 +40,18 @@ def main():
     bot = core.Bot(core.Telegram(token, chat_id), chat_id, data_dir, legacy_state=state_path)
     bot.register(flights.Flights(
         bot.ctx("flights"), lambda number, day: flights.fetch_flight(number, day, key), state_path))
+    clef_client = clef.Clef(os.environ.get("CLEF_URL", DEFAULT_CLEF_URL))
     bot.register(alerts.Alerts(
-        bot.ctx("alerts"), lambda: alerts.fetch_messages(gotify_host, gotify_token)))
+        bot.ctx("alerts"), lambda: alerts.fetch_messages(gotify_host, gotify_token),
+        triage=triage.Triage(clef_client)))
+    if os.environ.get("FRESHRSS_USER"):  # the news module needs the sealed freshrss-api-secret
+        source = freshrss.FreshRSS(os.environ.get("FRESHRSS_URL", DEFAULT_FRESHRSS_URL),
+                                   os.environ["FRESHRSS_USER"], os.environ["FRESHRSS_API_PASSWORD"])
+        hours = tuple(int(h) for h in os.environ.get("DIGEST_HOURS", "8,18").split(","))
+        bot.register(news.News(bot.ctx("news"), chat_id, source, clef_client,
+                               tz=digest_tz(), digest_hours=hours))
+    else:
+        core.log("FRESHRSS_USER not set, news module off")
     bot.register(status.Status(bot.ctx("status"), lambda promql: status.vm_query(vm_url, promql)))
     bot.start()
     core.log("flight-tracker started")
PATCH
```

Apply this patch (env vars plus the optional freshrss-api-secret):

```bash
git apply - <<'PATCH'
diff --git a/kubernetes/apps/monitoring/flight-tracker/app/deployment.yml b/kubernetes/apps/monitoring/flight-tracker/app/deployment.yml
index 0265828..0ca6180 100644
--- a/kubernetes/apps/monitoring/flight-tracker/app/deployment.yml
+++ b/kubernetes/apps/monitoring/flight-tracker/app/deployment.yml
@@ -43,6 +43,14 @@ spec:
               value: /data/state.json
             - name: PYTHONDONTWRITEBYTECODE
               value: "1"
+            - name: CLEF_URL
+              value: http://clef.ai.svc.cluster.local:8080
+            - name: FRESHRSS_URL
+              value: http://freshrss.freshrss.svc.cluster.local
+            - name: DIGEST_TZ
+              value: Europe/Brussels
+            - name: DIGEST_HOURS
+              value: "8,18"
           envFrom:
             - secretRef:
                 name: telegram-secret
@@ -50,6 +58,9 @@ spec:
                 name: aerodatabox-secret
             - secretRef:
                 name: gotify-client-secret
+            - secretRef:
+                name: freshrss-api-secret
+                optional: true   # news module stays off until this is sealed; plain Deployment, so optional is honoured
           securityContext:
             allowPrivilegeEscalation: false
             readOnlyRootFilesystem: true
PATCH
```

Apply this patch (adds the new modules to the ConfigMap files list):

```bash
git apply - <<'PATCH'
diff --git a/kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml b/kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml
index 8dd1632..bc900fd 100644
--- a/kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml
+++ b/kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml
@@ -10,7 +10,11 @@ configMapGenerator:
     namespace: monitoring   # must match the Deployment's namespace or the hashed name is not propagated
     files:
       - alerts.py
+      - clef.py
       - core.py
       - flights.py
+      - freshrss.py
       - main.py
+      - news.py
       - status.py
+      - triage.py
PATCH
```

- [ ] **Step 2: Check the bot starts and everything still passes**

```bash
python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests
cd kubernetes/apps/monitoring/flight-tracker/app && TELEGRAM_BOT_TOKEN=t TELEGRAM_CHAT_ID=1 AERODATABOX_KEY=k CLIENT_TOKEN=c python3 -c "import main; print(main.digest_tz())"; cd -
mise exec -- kubectl kustomize kubernetes/apps/monitoring/flight-tracker/app | mise exec -- kubeconform -strict -summary -ignore-missing-schemas -
.agents/scripts/validate-manifests.sh kubernetes/apps/monitoring/flight-tracker/app/deployment.yml
.agents/scripts/validate-manifests.sh kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml
```
Expected: tests `OK`; `Europe/Brussels`; kubeconform `Invalid: 0, Errors: 0`; validate scripts exit 0.

- [ ] **Step 3: Commit**

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/main.py kubernetes/apps/monitoring/flight-tracker/app/deployment.yml kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml
git commit -m "feat(telegram-bot): wire triage and news into the bot"
```

---

### Task 8: Documentation

**Files:**
- Modify: `design/decisions/llm.md`
- Modify: `design/decisions/flight-tracker.md`
- Modify: `design/docs/services.md`

Design docs describe the implemented state and never record deployed version numbers (the revision hash and torch pins live in the manifests).

- [ ] **Step 1: `design/decisions/llm.md`**

Add this section after "Current state" and before "## Rules":

```markdown
## Clef (decision model)

`clef` in `ai` runs Cloudflare's open `clef-flash` (Apache 2.0) on CPU on `llm-1`, reached at
`clef.ai.svc.cluster.local:8080`. It is **not** a `llama-swap` model and **not** behind LiteLLM:
Clef is a Qwen backbone plus a trained `joint_head` and custom code, and it answers with one
probability per option in a single forward pass (no text generation). The only client is the
Telegram bot (`design/decisions/flight-tracker.md`).
```

Add these rules to the end of "## Rules":

```markdown
- **Never run Clef from a GGUF in `llama-server`** — the community GGUFs hold only the Qwen
  backbone; the `joint_head` is a separate file and the decision logic is Cloudflare's
  `joint_schema_model.py`. A GGUF gives plausible-looking, wrong answers.
- **`llama-swap` requests 46Gi, not 60Gi, so Clef (18Gi request) can schedule** — allocatable is
  ~68.2 GiB and the DaemonSets request ~1.6 GiB. Measured Clef peak was 20.8 GiB (includes page
  cache of the weights). If `llama-swap` decode slows or the pod is evicted, suspect this
  split first.
- **Clef takes one request at a time (~4 s for one question, ~7 s for three).** The bot's
  triage timeout is 15 s and fails open, so a news scoring run can make triage forward a message
  it would have dropped — extra noise, never a lost message.
- **Pinned weights revision lives in `clef/app/deployment.yml` (`CLEF_REVISION`) and is used by
  both the initContainer and the server path.** Change both env vars together; the initContainer
  re-downloads 19 GB when the revision changes.
```

- [ ] **Step 2: `design/decisions/flight-tracker.md`**

In "Current state", add after the `/status` sentence: "`alerts.py` runs Clef triage on Gotify messages below priority 8, and `news.py` sends a twice-daily FreshRSS digest of articles matching `/topics`." Add `clef.py`, `triage.py`, `freshrss.py`, `news.py` to the file list and update the count.

Add these rules before "## Verify":

```markdown
- **Triage never drops priority >= 8, a message with no usable priority, or anything when Clef
  fails** (timeout 15 s, bad answer, service down). Dropped messages stay in the Gotify UI and the
  last 20 are listed by `/alerts dropped`. Triage runs outside the alerts lock; do not move it
  inside, it would freeze `/alerts` for the length of a burst.
- **The news module needs the sealed `freshrss-api-secret`** (`FRESHRSS_USER`,
  `FRESHRSS_API_PASSWORD` = FreshRSS's API password, which bypasses OIDC/2FA). Without it the
  module is simply not registered and the log says so.
- **The first news poll marks the whole unread backlog as seen without scoring it**, and a poll
  with no topics marks articles seen too; only articles that arrive while topics exist can match.
- **A poll scores at most 40 articles** (about 7 s each); the rest wait for the next poll. A Clef
  failure leaves the articles unseen so nothing is lost.
- **Digest times are 08:00 and 18:00 in `DIGEST_TZ`**; if the zone database is missing the bot
  logs it and uses UTC. The first digest check only starts the schedule.
- **Article text is untrusted.** A crafted article can fool the classifier into a false match; the
  result is a wrong line in a digest, nothing more.
- **Topics are stored per chat id** in `/data/news.json`, but the core still answers only
  `TELEGRAM_CHAT_ID`, so there is one list today. Multi-user needs the core to pass the chat id to
  commands.
```

- [ ] **Step 3: `design/docs/services.md`**

Add a row after the `llama-swap` row, matching the table's columns:

```markdown
| Clef | ai | Deployment | — (ClusterIP `clef:8080`) | none — ClusterIP only, called by the Telegram bot | `clef-data` PVC (`openebs-hostpath`, RWO, node-local on `llm-1`): pinned venv and 19 GB weights, re-downloadable, not backed up. CPU-only decision model; requests 18Gi, limit 24Gi |
```

Update the `flight-tracker` row's description: append "Also triages Gotify messages with Clef and sends a FreshRSS news digest."

- [ ] **Step 4: Commit**

```bash
git add design/decisions/llm.md design/decisions/flight-tracker.md design/docs/services.md
git commit -m "docs: Clef service, triage and the news digest"
```

---

### Task 9: Rollout and verification

**This task changes the live cluster and the memory budget of `llm-1`. Stop and get the user's explicit yes before Step 2.**

- [ ] **Step 1: Final local check**

```bash
python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests
python3 -m unittest discover -s kubernetes/apps/ai/clef/tests
git status --short
git log --oneline main..HEAD
```
Expected: both `OK`; only the user's two unrelated modified files (`design/decisions/jellyfin.md`, `design/docs/storage.md`) may remain in `git status`, never staged; 8 commits for Tasks 1 to 8 (plus any fixups).

- [ ] **Step 2: Ask the user, then open a PR**

Warn the user first: the merge also changes `llama-swap`'s memory request (60Gi to 46Gi), which recreates the `llama-swap` pod (a 34 GiB model reload, so a short chat outage).

Ask: "Ready to push `feat/clef-triage-news` and open a PR? Merging deploys Clef to `llm-1` and lowers `llama-swap`'s memory request." On a yes:

```bash
git push -u origin feat/clef-triage-news
gh pr create --title "feat: Clef triage for Gotify and a FreshRSS news digest" --body "<3-5 line summary linking docs/superpowers/specs/2026-10-06-clef-triage-and-news-design.md, plus the llama-swap 60Gi to 46Gi request change; end with the PR attribution lines from your session instructions>"
```
Wait for the user to merge. Do not merge yourself.

- [ ] **Step 3: After the merge, watch Flux and the first start**

```bash
mise exec -- flux reconcile source git home-kubernetes -n flux-system
mise exec -- flux reconcile kustomization clef -n flux-system --with-source
mise exec -- kubectl -n ai get pods -l app.kubernetes.io/name=clef -w
mise exec -- kubectl -n ai logs deploy/clef -c setup -f
```
Expected: the `setup` initContainer installs dependencies, downloads weights (about 15 to 25 minutes in total), then the `clef` container reports `model loaded` and the pod becomes Ready. If the pod stays `Pending`, run `mise exec -- kubectl -n ai describe pod -l app.kubernetes.io/name=clef` and read the scheduler event: `Insufficient memory` means `llama-swap` still holds 60Gi (check that the HelmRelease reconciled).

- [ ] **Step 4: Prove the model answers (go/no-go for the memory split)**

```bash
mise exec -- kubectl -n monitoring exec deploy/flight-tracker -- python3 -c "
import json,time,urllib.request
b={'model':'clef-flash','state':{'title':'ALERT [warning]: KubeJobFailed','message':'Job backup failed','priority':5},'questions':{'needed':{'type':'noul','instructions':'Does this notification need the owner attention?'}}}
t=time.time()
r=urllib.request.urlopen(urllib.request.Request('http://clef.ai.svc.cluster.local:8080/v1/systemone',json.dumps(b).encode(),{'Content-Type':'application/json'}),timeout=30)
print(round(time.time()-t,1),'s',r.read().decode()[:200])"
```
Expected: about 4 s and a `noul` probability above 0.5. (The bot pod is only used here because it has `python3` and network access to `ai`; this call does not depend on the new bot code.)

Then check `llama-swap` did not slow down:

```bash
mise exec -- kubectl -n ai exec deploy/llama-swap -- curl -s localhost:8080/v1/models | head -c 200
```
and ask the user to send one chat message through `chat.blackcats.cc` or time one `local-fast` call. Baseline decode is 8.4 to 8.9 tok/s. **If decode drops by more than 20%, or `kubectl -n ai get pods` shows `llama-swap` restarted, stop and report: revert the memory change with `git revert` and keep Clef off until a smaller quantisation is chosen.**

Repeat the decode check at two more moments, with the same >20% / restart rule (and the same revert):

1. DURING a news poll. A poll starts every 30 minutes after the bot's first start, and keeps Clef busy for minutes. Trigger or observe it through the bot log lines (`kubectl -n monitoring logs deploy/flight-tracker --tail=50`), and while it runs time one `local-fast` call or one chat reply (`kubectl -n ai top` is unavailable, so the timing of a chat reply is the measure). If chat suffers, lower `CLEF_THREADS` rather than reverting first.
2. After a few days of normal chat use. The llama-swap prompt cache (up to 8 GiB) fills over days and can push its working set past its request, so re-check decode speed and `kubectl -n ai get pods` for a restart or eviction.

- [ ] **Step 5: Seal the FreshRSS API credentials (user action)**

Tell the user to run, in this repo, replacing the placeholders (the `-secret.yml` suffix is gitignored):

```bash
cat > kubernetes/apps/monitoring/flight-tracker/app/freshrss-api-secret.yml <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: freshrss-api-secret
  namespace: monitoring
stringData:
  FRESHRSS_USER: <freshrss username>
  FRESHRSS_API_PASSWORD: <freshrss API password>
EOF
mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml < kubernetes/apps/monitoring/flight-tracker/app/freshrss-api-secret.yml > kubernetes/apps/monitoring/flight-tracker/app/freshrss-api-sealed.yml
```
Then add `  - ./freshrss-api-sealed.yml` under `resources:` in `kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml`, run `.agents/scripts/validate-manifests.sh kubernetes/apps/monitoring/flight-tracker/app/freshrss-api-sealed.yml`, commit (`feat(telegram-bot): seal FreshRSS API credentials`), and merge via a second PR. Never commit the plain `-secret.yml`.

- [ ] **Step 6: Smoke test the bot (user action in Telegram)**

1. `/alerts dropped` replies "Triage has dropped nothing yet." (or a list).
2. `/topics add kubernetes security` replies "Following: kubernetes security" and lists it.
3. `kubectl -n monitoring logs deploy/flight-tracker --tail=50` shows `triage message ... p=... forward|drop` lines for new Gotify messages and no "news module off" line once the secret is sealed (after Step 5).
4. Trigger low-priority test messages: use the Gotify UI to send a priority 5 message titled `Backup: test ✓` and confirm it does not arrive, then one titled `ALERT: disk failing` and confirm it does.
5. Seeding check: once the first poll has run, publish or fetch a FreshRSS article AFTER it and confirm it shows up in a later poll's log (proves `ot`/`since` works and the backlog is not replayed).
6. After the next 08:00 or 18:00 (Brussels), a digest arrives if matching articles were found; confirm zone handling: `mise exec -- kubectl -n monitoring exec deploy/flight-tracker -- python3 -c "import zoneinfo;zoneinfo.ZoneInfo('Europe/Brussels');print('tz ok')"` (no `timezone ... not found` log line).

- [ ] **Step 7: Review the drops after a week**

Ask the user to run `/alerts dropped` after a week and tell you whether anything listed should have been forwarded. If so, lower `triage.THRESHOLD` in `triage.py` (a message is forwarded when its probability of being needed is at or above the threshold, so a lower value forwards more) in a small follow-up commit.

**Back-out:** `git revert` the merge commit. The bot reverts to forwarding everything (triage disappears) and `llama-swap` regains its 60Gi request once Clef is gone. A revert also removes the `clef-data` PVC (`prune: true`), so re-adding Clef later costs the 19 GB download again.
