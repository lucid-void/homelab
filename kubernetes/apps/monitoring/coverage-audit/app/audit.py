"""Coverage comparison. Pure functions over Kubernetes objects, no I/O."""
import json
import re
from fnmatch import fnmatchcase

CHECKS = ("gatus", "homepage", "backup")
GATEWAY = ("gateway", "shared")


def route_hosts(routes):
    """Hostname -> "namespace/name" for every HTTPRoute on the shared Gateway."""
    hosts = {}
    for r in routes:
        ns = r["metadata"]["namespace"]
        parents = r.get("spec", {}).get("parentRefs") or []
        # A parentRef without a namespace means the route's own namespace.
        if not any((p.get("namespace", ns), p.get("name")) == GATEWAY for p in parents):
            continue
        for host in r["spec"].get("hostnames") or []:
            hosts[host] = f'{ns}/{r["metadata"]["name"]}'
    return hosts


def config_hosts(text, key):
    """Hostnames on `<key>: <scheme>://<host>` lines. Not a YAML parse: the image has no
    parser, and both files keep one URL per line. A commented line does not match."""
    pattern = re.compile(
        r"""^\s*(?:-\s*)?%s:\s*["']?[a-z][a-z0-9+.-]*://([^/:"'\s]+)""" % re.escape(key),
        re.MULTILINE)
    return {m.group(1).lower() for m in pattern.finditer(text)}


def backed_up_claims(cronjobs):
    """"namespace/claim" for every PVC mounted by a CronJob with `backup` in its name."""
    claims = set()
    for cj in cronjobs:
        if "backup" not in cj["metadata"]["name"]:
            continue
        pod = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        for vol in pod.get("volumes") or []:
            claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                claims.add(f'{cj["metadata"]["namespace"]}/{claim}')
    return claims


def pvc_ids(pvcs):
    return {f'{p["metadata"]["namespace"]}/{p["metadata"]["name"]}' for p in pvcs}


def load_exemptions(text):
    """Parses {"gatus": {pattern: reason}, "homepage": {...}, "backup": {...}}. Raises
    ValueError on anything else, so a broken file fails the run instead of silently
    exempting nothing (or everything)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"exemptions file is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("exemptions file must be a JSON object")
    unknown = sorted(set(data) - set(CHECKS))
    if unknown:
        raise ValueError(f"exemptions file has unknown check(s): {', '.join(unknown)}")
    out = {}
    for check in CHECKS:
        entries = data.get(check, {})
        if not isinstance(entries, dict):
            raise ValueError(f'exemptions "{check}" must be an object of pattern: reason')
        for pattern, reason in entries.items():
            if not isinstance(reason, str):
                raise ValueError(f'exemption {check} "{pattern}" has a non-string reason')
        out[check] = dict(entries)
    return out


def _check(check, label, uncovered, describe, exemptions):
    findings = []
    excused = set()
    for pattern, reason in exemptions.get(check, {}).items():
        if not reason.strip():
            findings.append(f'exemption without a reason: {check} "{pattern}"')
            continue
        matched = {item for item in uncovered if fnmatchcase(item, pattern)}
        if not matched:
            findings.append(f'stale exemption: {check} "{pattern}" excuses nothing')
        excused |= matched
    findings += [f"{label}: {describe(item)}" for item in uncovered - excused]
    return findings


def audit(hosts, gatus, homepage, pvcs, backed_up, exemptions):
    """Sorted finding lines; empty when every gap is covered or deliberately exempt."""
    def with_route(host):
        return f"{host} ({hosts[host]})"

    findings = []
    findings += _check("gatus", "no Gatus check", set(hosts) - gatus, with_route, exemptions)
    findings += _check("homepage", "no Homepage tile", set(hosts) - homepage, with_route,
                       exemptions)
    findings += _check("backup", "no backup job", pvcs - backed_up, str, exemptions)
    return sorted(findings)
