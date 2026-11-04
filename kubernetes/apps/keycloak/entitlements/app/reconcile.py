#!/usr/bin/env python3
"""Converge Keycloak's per-service entitlement model from entitlements.json.

No CRD expresses role mappers, groups, group->role mappings or browser-flow gates,
so this Job owns them through the admin REST API. It only ever adds to or corrects
what the config names: group membership is the operator's to manage, so it is only
seeded when this run creates the group.

A missing admin permission is reported and skipped rather than failing the Job, so
the parts the service account can do still land. A write that was permitted and did
not take effect is a problem, and fails the Job.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


class Forbidden(Exception):
    pass


class Keycloak:
    def __init__(self, base_url, realm, client_id, client_secret):
        self.base_url = base_url.rstrip("/")
        self.realm = realm
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None

    def _fetch_token(self):
        data = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode()
        url = f"{self.base_url}/realms/master/protocol/openid-connect/token"
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as resp:
            return json.load(resp)["access_token"]

    def request(self, method, path, body=None, query=None):
        url = f"{self.base_url}/admin/realms/{self.realm}/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode()
        for attempt in (1, 2):
            if self.token is None:
                self.token = self._fetch_token()
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self.token}")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    if raw:
                        return json.loads(raw)
                    location = resp.headers.get("Location")
                    return location.rsplit("/", 1)[-1] if location else None
            except urllib.error.HTTPError as err:
                if err.code == 401 and attempt == 1:
                    self.token = None
                    continue
                if err.code == 403:
                    raise Forbidden(f"{method} {path}") from err
                if err.code == 404 and method == "GET":
                    return None
                detail = err.read().decode(errors="replace")[:300]
                raise RuntimeError(f"{method} {path}: HTTP {err.code}: {detail}") from err
        raise RuntimeError(f"{method} {path}: still unauthorized after a fresh token")


class Report:
    def __init__(self):
        self.changes = []
        self.blocked = []
        self.problems = []

    def change(self, message):
        print(f"  changed: {message}", flush=True)
        self.changes.append(message)

    def block(self, message):
        print(f"  blocked: {message}", flush=True)
        self.blocked.append(message)

    def problem(self, message):
        print(f"  PROBLEM: {message}", flush=True)
        self.problems.append(message)


@dataclass
class Node:
    # A step's name is its providerId; a sub-flow's name is its alias.
    name: str
    requirement: str
    children: list = None
    config_alias: str = None
    config: dict = None


def flow_spec(client_id, gate_role):
    """The login flow every gated client uses.

    The gate must be a top-level sibling of a nested authenticate sub-flow: Keycloak
    ignores ALTERNATIVE steps that share a level with a REQUIRED one, and a gate inside
    the forms sub-flow is bypassed by anyone already holding an SSO cookie.
    """
    return [
        Node(f"{client_id}-authenticate", "REQUIRED", [
            Node("auth-cookie", "ALTERNATIVE"),
            Node("identity-provider-redirector", "ALTERNATIVE"),
            Node(f"{client_id}-organization", "ALTERNATIVE", [
                Node(f"{client_id}-org-conditional", "CONDITIONAL", [
                    Node("conditional-user-configured", "REQUIRED"),
                    Node("organization", "ALTERNATIVE"),
                ]),
            ]),
            Node(f"{client_id}-forms", "ALTERNATIVE", [
                Node("auth-username-password-form", "REQUIRED"),
                Node(f"{client_id}-2fa", "CONDITIONAL", [
                    Node("conditional-user-configured", "REQUIRED"),
                    Node(
                        "conditional-credential", "REQUIRED",
                        config_alias=f"{client_id}-cond-credential",
                        config={"credentials": "webauthn-passwordless"},
                    ),
                    Node("auth-otp-form", "ALTERNATIVE"),
                ]),
            ]),
        ]),
        Node(f"{client_id}-gate", "CONDITIONAL", [
            Node(
                "conditional-user-role", "REQUIRED",
                config_alias=f"{client_id}-entitlement",
                config={"condUserRole": f"{client_id}.{gate_role}", "negate": "true"},
            ),
            Node("deny-access-authenticator", "REQUIRED"),
        ]),
    ]


def quote(value):
    return urllib.parse.quote(value, safe="")


def direct_children(kc, alias):
    executions = kc.request("GET", f"authentication/flows/{quote(alias)}/executions") or []
    return [e for e in executions if e.get("level") == 0]


def find_execution(executions, node):
    for execution in executions:
        if node.children is not None:
            if execution.get("authenticationFlow") and execution.get("displayName") == node.name:
                return execution
        elif execution.get("providerId") == node.name:
            return execution
    return None


def ensure_config(kc, report, label, execution, node):
    config_id = execution.get("authenticationConfig")
    if not config_id:
        kc.request(
            "POST",
            f"authentication/executions/{execution['id']}/config",
            {"alias": node.config_alias, "config": node.config},
        )
        report.change(f"{label}: created {node.config_alias}")
        return
    current = kc.request("GET", f"authentication/config/{config_id}")
    if any(current.get("config", {}).get(k) != v for k, v in node.config.items()):
        current.setdefault("config", {}).update(node.config)
        kc.request("PUT", f"authentication/config/{config_id}", current)
        report.change(f"{label}: corrected {node.config_alias}")


def converge_level(kc, report, label, alias, nodes):
    executions = direct_children(kc, alias)
    for node in nodes:
        if find_execution(executions, node) is not None:
            continue
        if node.children is not None:
            kc.request(
                "POST",
                f"authentication/flows/{quote(alias)}/executions/flow",
                {"alias": node.name, "type": "basic-flow", "description": ""},
            )
        else:
            kc.request(
                "POST",
                f"authentication/flows/{quote(alias)}/executions/execution",
                {"provider": node.name},
            )
        report.change(f"{label}: added {node.name} to {alias}")

    executions = direct_children(kc, alias)
    # Keycloak orders equal priorities arbitrarily, and a PUT that omits priority resets
    # it to 0. Assigning from the last sibling back keeps the order right at every
    # intermediate state, so a login mid-run can never reach the gate before authenticating.
    for index, node in reversed(list(enumerate(nodes))):
        execution = find_execution(executions, node)
        priority = (index + 1) * 10
        if execution.get("requirement") != node.requirement or execution.get("priority") != priority:
            updated = dict(execution, requirement=node.requirement, priority=priority)
            kc.request("PUT", f"authentication/flows/{quote(alias)}/executions", updated)
            report.change(f"{label}: {alias}/{node.name} -> {node.requirement}, priority {priority}")

    executions = direct_children(kc, alias)
    priorities = [find_execution(executions, node).get("priority") for node in nodes]
    if priorities != sorted(set(priorities)) or len(priorities) != len(nodes):
        report.problem(f"{label}: {alias} order did not take effect: {priorities}")

    for node in nodes:
        execution = find_execution(executions, node)
        if node.children is not None:
            converge_level(kc, report, label, node.name, node.children)
        if node.config is not None:
            ensure_config(kc, report, label, execution, node)


def ensure_mapper(kc, report, client_id, cid, claim):
    desired = {
        "claim.name": claim,
        "jsonType.label": "String",
        "multivalued": "true",
        "id.token.claim": "true",
        "access.token.claim": "true",
        "userinfo.token.claim": "true",
        "introspection.token.claim": "true",
        "lightweight.claim": "false",
        # Unset, this would emit every client's roles into one flat list, so `admin` on
        # one app would read as `admin` on all of them.
        "usermodel.clientRoleMapping.clientId": client_id,
    }
    mappers = kc.request("GET", f"clients/{cid}/protocol-mappers/models") or []
    for mapper in mappers:
        if mapper.get("config", {}).get("claim.name") != claim:
            continue
        if all(mapper["config"].get(k) == v for k, v in desired.items()):
            return
        mapper["config"].update(desired)
        kc.request("PUT", f"clients/{cid}/protocol-mappers/models/{mapper['id']}", mapper)
        report.change(f"{client_id}: corrected mapper for {claim}")
        return
    kc.request(
        "POST",
        f"clients/{cid}/protocol-mappers/models",
        {
            "name": claim,
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-client-role-mapper",
            "config": desired,
        },
    )
    report.change(f"{client_id}: created mapper for {claim}")


def ensure_group(kc, report, parent_id, path, name):
    group = kc.request("GET", f"group-by-path/{path}")
    if group is not None:
        return group, False
    if parent_id is None:
        kc.request("POST", "groups", {"name": name})
    else:
        kc.request("POST", f"groups/{parent_id}/children", {"name": name})
    report.change(f"created group /{path}")
    return kc.request("GET", f"group-by-path/{path}"), True


def wait_for_role(kc, cid, name, timeout, interval):
    # The operator creates client roles asynchronously, and this Job can run first.
    deadline = time.monotonic() + timeout
    while True:
        role = kc.request("GET", f"clients/{cid}/roles/{quote(name)}")
        if role is not None or time.monotonic() >= deadline:
            return role
        time.sleep(interval)


def ensure_groups(kc, report, client_id, cid, spec, role_timeout, role_interval):
    parent, _ = ensure_group(kc, report, None, client_id, client_id)
    for name, roles in spec["groups"].items():
        path = f"{client_id}/{name}"
        group, created = ensure_group(kc, report, parent["id"], path, name)
        mapping_path = f"groups/{group['id']}/role-mappings/clients/{cid}"
        current = {r["name"] for r in kc.request("GET", mapping_path) or []}
        missing = []
        for role_name in roles:
            if role_name in current:
                continue
            role = wait_for_role(kc, cid, role_name, role_timeout, role_interval)
            if role is None:
                report.problem(f"{client_id}: client role {role_name} does not exist")
                continue
            missing.append(role)
        if missing:
            kc.request("POST", mapping_path, missing)
            report.change(f"/{path}: granted {', '.join(r['name'] for r in missing)}")
        if created:
            for username in spec.get("initialMembers", {}).get(name, []):
                users = kc.request("GET", "users", query={"username": username, "exact": "true"}) or []
                if not users:
                    report.problem(f"/{path}: initial member {username} does not exist")
                    continue
                kc.request("PUT", f"users/{users[0]['id']}/groups/{group['id']}")
                report.change(f"/{path}: added initial member {username}")


def ensure_gate(kc, report, client_id, cid, spec, may_bind):
    alias = f"browser-{client_id}"
    nodes = flow_spec(client_id, spec["gate"])
    flows = kc.request("GET", "authentication/flows") or []
    flow = next((f for f in flows if f.get("alias") == alias), None)
    if flow is None:
        kc.request(
            "POST",
            "authentication/flows",
            {
                "alias": alias,
                "providerId": "basic-flow",
                "topLevel": True,
                "builtIn": False,
                "description": f"Browser login gated on {client_id}.{spec['gate']}",
            },
        )
        report.change(f"{client_id}: created flow {alias}")
        flows = kc.request("GET", "authentication/flows") or []
        flow = next(f for f in flows if f.get("alias") == alias)

    problems_before = len(report.problems)
    converge_level(kc, report, client_id, alias, nodes)

    client = kc.request("GET", f"clients/{cid}")
    overrides = client.get("authenticationFlowBindingOverrides") or {}
    if overrides.get("browser") == flow["id"]:
        return
    if not may_bind:
        report.block(f"{client_id}: gate not bound, its groups could not be converged first")
        return
    if len(report.problems) != problems_before:
        report.problem(f"{client_id}: gate not bound, its flow did not converge")
        return
    client["authenticationFlowBindingOverrides"] = {**overrides, "browser": flow["id"]}
    kc.request("PUT", f"clients/{cid}", client)
    report.change(f"{client_id}: bound {alias}")


def check_claim(kc, report, client_id, cid, claim, username):
    users = kc.request("GET", "users", query={"username": username, "exact": "true"}) or []
    if not users:
        report.problem(f"claim check user {username} does not exist")
        return
    uid = users[0]["id"]
    held = kc.request("GET", f"users/{uid}/role-mappings/clients/{cid}/composite") or []
    expected = sorted(r["name"] for r in held)
    query = {"scope": "openid", "userId": uid}
    base = f"clients/{cid}/evaluate-scopes"
    id_token = kc.request("GET", f"{base}/generate-example-id-token", query=query) or {}
    userinfo = kc.request("GET", f"{base}/generate-example-userinfo", query=query) or {}
    for source, claims in (("id token", id_token), ("userinfo", userinfo)):
        actual = sorted(claims.get(claim) or [])
        if actual != expected:
            report.problem(f"{client_id}: {source} has {claim}={actual}, role mappings say {expected}")
    print(f"  {client_id}: {claim}={expected}", flush=True)


def guarded(report, label, action):
    try:
        action()
        return True
    except Forbidden as err:
        report.block(f"{label}: no permission for {err}")
        return False


def reconcile(kc, config, report, role_timeout=120, role_interval=5):
    for client_id, spec in config["clients"].items():
        print(f"{client_id}:", flush=True)
        found = kc.request("GET", "clients", query={"clientId": client_id, "search": "false"}) or []
        if not found:
            report.problem(f"{client_id}: client does not exist")
            continue
        cid = found[0]["id"]
        claim = spec.get("claim")
        if claim:
            guarded(report, f"{client_id} mapper", lambda: ensure_mapper(kc, report, client_id, cid, claim))
        groups_ok = guarded(
            report, f"{client_id} groups",
            lambda: ensure_groups(kc, report, client_id, cid, spec, role_timeout, role_interval),
        )
        guarded(
            report, f"{client_id} gate",
            lambda: ensure_gate(kc, report, client_id, cid, spec, groups_ok),
        )
        username = config.get("claimCheckUser")
        if claim and username:
            guarded(
                report, f"{client_id} claim check",
                lambda: check_claim(kc, report, client_id, cid, claim, username),
            )


def notify(title, lines, priority):
    token = os.environ.get("GOTIFY_TOKEN")
    if not token:
        print("WARN: no GOTIFY_TOKEN; not notified", file=sys.stderr)
        return
    body = json.dumps({"title": title, "message": "\n".join(f"• {l}" for l in lines), "priority": priority})
    req = urllib.request.Request(
        os.environ.get("GOTIFY_URL", "http://gotify.monitoring.svc.cluster.local/message"),
        data=body.encode(),
        headers={"Content-Type": "application/json", "X-Gotify-Key": token},
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except OSError as err:
        print(f"WARN: Gotify notification failed: {err}", file=sys.stderr)


def main():
    with open(sys.argv[1], encoding="utf-8") as handle:
        config = json.load(handle)
    kc = Keycloak(
        os.environ["KEYCLOAK_URL"],
        config["realm"],
        os.environ["KEYCLOAK_CLIENT_ID"],
        os.environ["KEYCLOAK_CLIENT_SECRET"],
    )
    report = Report()
    reconcile(kc, config, report)

    print(
        f"Done: {len(report.changes)} changed, {len(report.blocked)} blocked, "
        f"{len(report.problems)} problems.",
        flush=True,
    )
    if report.blocked:
        print(
            "Blocked items need manage-realm and manage-users on the homelab-realm client "
            "for the keycloak-operator service account: see design/decisions/keycloak.md.",
            flush=True,
        )
    # ttlSecondsAfterFinished deletes this pod's logs, so Gotify is the durable record.
    if report.changes:
        notify("Keycloak entitlements changed", report.changes, 4)
    if report.problems:
        notify("Keycloak entitlements failed", report.problems, 8)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
