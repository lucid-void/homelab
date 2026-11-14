"""Daily coverage audit: which routed services have no Gatus check or Homepage tile, and
which PVCs have no backup job. Read-only. Deliberate gaps live in exemptions.json."""
import json
import os
import ssl
import sys
import traceback
import urllib.request

import audit

EXEMPTIONS_PATH = "/app/exemptions.json"
GOTIFY_URL = "http://gotify.monitoring.svc.cluster.local/message"
KUBE_URL = "https://kubernetes.default.svc"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

ROUTES = "/apis/gateway.networking.k8s.io/v1/httproutes"
PVCS = "/api/v1/persistentvolumeclaims"
CRONJOBS = "/apis/batch/v1/cronjobs"
GATUS_CM = "/api/v1/namespaces/monitoring/configmaps/gatus-config"
HOMEPAGE_CM = "/api/v1/namespaces/homepage/configmaps/homepage"


def kube_get(path):
    with open(f"{SA_DIR}/token") as f:
        token = f.read().strip()
    ctx = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
    req = urllib.request.Request(KUBE_URL + path, headers={"Authorization": f"Bearer {token}"})
    # urlopen raises HTTPError on any non-2xx, so a 403 never reads as an empty list.
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        return json.load(resp)


def notify(title, message, priority):
    token = os.environ.get("GOTIFY_TOKEN", "")
    if not token:
        print("WARN: no GOTIFY_TOKEN; not notified", file=sys.stderr)
        return
    body = json.dumps({"title": title, "message": message, "priority": priority}).encode()
    req = urllib.request.Request(GOTIFY_URL, data=body, headers={
        "X-Gotify-Key": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:  # a lost notification must not change the exit code
        print(f"WARN: Gotify notification failed: {e}", file=sys.stderr)


def collect(get=kube_get, exemptions_path=EXEMPTIONS_PATH):
    with open(exemptions_path) as f:
        exemptions = audit.load_exemptions(f.read())
    hosts = audit.route_hosts(get(ROUTES)["items"])
    pvcs = audit.pvc_ids(get(PVCS)["items"])
    backed_up = audit.backed_up_claims(get(CRONJOBS)["items"])
    # A missing key raises KeyError: an unreadable config must fail the run, not make
    # every service look uncovered.
    gatus = audit.config_hosts(get(GATUS_CM)["data"]["config.yaml"], "url")
    homepage = audit.config_hosts(get(HOMEPAGE_CM)["data"]["services.yaml"], "href")
    return audit.audit(hosts, gatus, homepage, pvcs, backed_up, exemptions)


def main(get=kube_get, send=notify, exemptions_path=EXEMPTIONS_PATH):
    try:
        findings = collect(get, exemptions_path)
    except Exception as e:
        traceback.print_exc()
        send("Coverage audit failed",
             f"{type(e).__name__}: {e}. Check the coverage-audit Job logs.", 8)
        return 1
    if not findings:
        print("No gaps.")
        return 0
    print(f"Found {len(findings)} gap(s):")
    for line in findings:
        print(f"  {line}")
    send(f"Coverage audit: {len(findings)} gap(s)",
         "\n".join(f"• {line}" for line in findings), 5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
