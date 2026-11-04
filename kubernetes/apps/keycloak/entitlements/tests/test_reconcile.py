import copy
import itertools
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import reconcile as rc  # noqa: E402


class FakeKeycloak:
    """In-memory admin API. Models only what reconcile.py calls."""

    def __init__(self, can_manage_realm=True, can_manage_users=True):
        self.can_manage_realm = can_manage_realm
        self.can_manage_users = can_manage_users
        self.ids = (f"id-{n}" for n in itertools.count())
        self.clients = {}
        self.mappers = {}
        self.roles = {}
        self.groups = {}
        self.group_roles = {}
        self.users = {"void": "user-void"}
        self.memberships = set()
        self.flows = {}
        self.executions = {}
        self.configs = {}
        self.top_level_orders = []
        self.ignore_priority = False

    def add_client(self, client_id, roles):
        cid = f"cid-{client_id}"
        self.clients[client_id] = {"id": cid, "clientId": client_id, "authenticationFlowBindingOverrides": {}}
        self.roles[cid] = {r: {"id": f"{cid}-{r}", "name": r} for r in roles}
        self.mappers[cid] = []
        return cid

    def add_execution(self, flow_id, provider=None, sub_flow=None, requirement="DISABLED", priority=0):
        execution = {
            "id": next(self.ids),
            "parent": flow_id,
            "requirement": requirement,
            "priority": priority,
        }
        if sub_flow is not None:
            execution.update(authenticationFlow=True, displayName=sub_flow, flowId=self.flows[sub_flow]["id"])
        else:
            execution.update(authenticationFlow=False, providerId=provider)
        self.executions[execution["id"]] = execution
        return execution

    def add_flow(self, alias):
        self.flows[alias] = {"id": next(self.ids), "alias": alias}
        return self.flows[alias]

    def add_tied_flow(self, client_id, gate_role):
        """A flow as hand-built in the console: right shape, every priority 0."""
        top = self.add_flow(f"browser-{client_id}")

        def build(flow_id, nodes):
            for node in nodes:
                if node.children is not None:
                    sub = self.add_flow(node.name)
                    self.add_execution(flow_id, sub_flow=node.name, requirement=node.requirement)
                    build(sub["id"], node.children)
                else:
                    execution = self.add_execution(flow_id, provider=node.name, requirement=node.requirement)
                    if node.config is not None:
                        config_id = next(self.ids)
                        self.configs[config_id] = {"id": config_id, "alias": node.config_alias, "config": dict(node.config)}
                        execution["authenticationConfig"] = config_id

        build(top["id"], rc.flow_spec(client_id, gate_role))
        return top

    def sorted_children(self, flow_id):
        children = [e for e in self.executions.values() if e["parent"] == flow_id]
        return sorted(children, key=lambda e: e["priority"])

    def record_top_level_orders(self):
        for alias, flow in self.flows.items():
            if alias.startswith("browser-"):
                names = [e.get("displayName") for e in self.sorted_children(flow["id"])]
                self.top_level_orders.append((alias, names))

    def request(self, method, path, body=None, query=None):
        query = query or {}
        parts = path.split("/")
        writing = method != "GET"

        if parts[0] == "clients" and len(parts) == 1:
            found = self.clients.get(query.get("clientId"))
            return [copy.deepcopy(found)] if found else []
        if parts[0] == "clients" and len(parts) == 2:
            client = next(c for c in self.clients.values() if c["id"] == parts[1])
            if method == "GET":
                return copy.deepcopy(client)
            client.update(body)
            return None
        if parts[0] == "clients" and parts[2] == "protocol-mappers":
            if method == "GET":
                return copy.deepcopy(self.mappers[parts[1]])
            if method == "POST":
                self.mappers[parts[1]].append(dict(body, id=next(self.ids)))
                return None
            mapper = next(m for m in self.mappers[parts[1]] if m["id"] == parts[4])
            mapper.update(body)
            return None
        if parts[0] == "clients" and parts[2] == "roles":
            return copy.deepcopy(self.roles[parts[1]].get(parts[3]))
        if parts[0] == "clients" and parts[2] == "evaluate-scopes":
            return self.example_token(parts[1], query["userId"])

        if parts[0] in ("group-by-path", "groups") or (parts[0] == "users" and "groups" in parts):
            if not self.can_manage_users:
                raise rc.Forbidden(f"{method} {path}")
        if parts[0] == "group-by-path":
            return copy.deepcopy(self.groups.get("/".join(parts[1:])))
        if parts[0] == "groups":
            return self.request_groups(method, parts, body)
        if parts[0] == "users" and len(parts) == 1:
            uid = self.users.get(query.get("username"))
            return [{"id": uid}] if uid else []
        if parts[0] == "users" and parts[2] == "groups":
            self.memberships.add((parts[1], parts[3]))
            return None
        if parts[0] == "users" and parts[2] == "role-mappings":
            return [{"name": r} for r in sorted(self.effective_roles(parts[1], parts[4]))]

        if parts[0] == "authentication":
            if writing and not self.can_manage_realm:
                raise rc.Forbidden(f"{method} {path}")
            return self.request_authentication(method, parts[1:], body)
        raise AssertionError(f"unhandled {method} {path}")

    def request_groups(self, method, parts, body):
        if len(parts) == 1:
            self.groups[body["name"]] = {"id": next(self.ids), "name": body["name"]}
            return None
        if parts[2] == "children":
            parent = next(p for p, g in self.groups.items() if g["id"] == parts[1])
            self.groups[f"{parent}/{body['name']}"] = {"id": next(self.ids), "name": body["name"]}
            return None
        key = (parts[1], parts[4])
        if method == "GET":
            return [{"name": n} for n in sorted(self.group_roles.get(key, set()))]
        self.group_roles.setdefault(key, set()).update(r["name"] for r in body)
        return None

    def request_authentication(self, method, parts, body):
        if parts == ["flows"]:
            if method == "GET":
                return copy.deepcopy(list(self.flows.values()))
            self.add_flow(body["alias"])
            return None
        if parts[0] == "flows":
            alias = rc.urllib.parse.unquote(parts[1])
            flow_id = self.flows[alias]["id"]
            if parts[2:] == ["executions", "flow"]:
                self.add_flow(body["alias"])
                self.add_execution(flow_id, sub_flow=body["alias"])
                return None
            if parts[2:] == ["executions", "execution"]:
                self.add_execution(flow_id, provider=body["provider"])
                return None
            if method == "GET":
                return [dict(copy.deepcopy(e), level=0) for e in self.sorted_children(flow_id)]
            execution = self.executions[body["id"]]
            execution["requirement"] = body["requirement"]
            if not self.ignore_priority:
                execution["priority"] = body["priority"]
            self.record_top_level_orders()
            return None
        if parts[0] == "executions":
            config_id = next(self.ids)
            self.configs[config_id] = {"id": config_id, **copy.deepcopy(body)}
            self.executions[parts[1]]["authenticationConfig"] = config_id
            return None
        if parts[0] == "config":
            if method == "GET":
                return copy.deepcopy(self.configs[parts[1]])
            self.configs[parts[1]] = copy.deepcopy(body)
            return None
        raise AssertionError(f"unhandled authentication {method} {parts}")

    def effective_roles(self, uid, cid):
        roles = set()
        for user, group_id in self.memberships:
            if user == uid:
                roles |= self.group_roles.get((group_id, cid), set())
        return roles

    def example_token(self, cid, uid):
        mapper = next((m for m in self.mappers[cid] if m["config"]["multivalued"] == "true"), None)
        return {mapper["config"]["claim.name"]: sorted(self.effective_roles(uid, cid))} if mapper else {}


def crosswatch_config():
    return {
        "realm": "homelab",
        "clients": {
            "crosswatch": {
                "gate": "admin",
                "groups": {"admin": ["admin"]},
                "initialMembers": {"admin": ["void"]},
            }
        },
    }


def tree(fake, alias):
    """Return [(name, requirement, priority, children)] as Keycloak would evaluate it."""
    result = []
    for execution in fake.sorted_children(fake.flows[alias]["id"]):
        name = execution.get("displayName") or execution.get("providerId")
        children = tree(fake, name) if execution["authenticationFlow"] else None
        result.append((name, execution["requirement"], execution["priority"], children))
    return result


class NewClient(unittest.TestCase):
    def setUp(self):
        self.fake = FakeKeycloak()
        self.cid = self.fake.add_client("crosswatch", ["admin"])
        self.report = rc.Report()
        rc.reconcile(self.fake, crosswatch_config(), self.report, role_timeout=0)

    def test_gate_runs_after_authentication(self):
        names = [n[0] for n in tree(self.fake, "browser-crosswatch")]
        self.assertEqual(names, ["crosswatch-authenticate", "crosswatch-gate"])

    def test_required_and_alternative_never_share_a_login_level(self):
        # Conditional sub-flows legitimately mix a REQUIRED condition with ALTERNATIVE
        # actions, so the trap only applies to these two levels.
        authenticate = tree(self.fake, "crosswatch-authenticate")
        self.assertEqual({n[1] for n in authenticate}, {"ALTERNATIVE"})
        self.assertEqual({n[1] for n in tree(self.fake, "browser-crosswatch")}, {"REQUIRED", "CONDITIONAL"})

    def test_priorities_are_strict_at_every_level(self):
        def check(nodes):
            priorities = [n[2] for n in nodes]
            self.assertEqual(priorities, sorted(set(priorities)))
            self.assertNotIn(0, priorities)
            for node in nodes:
                if node[3] is not None:
                    check(node[3])

        check(tree(self.fake, "browser-crosswatch"))

    def test_gate_requires_the_configured_role(self):
        gate = next(c for c in self.fake.configs.values() if c["alias"] == "crosswatch-entitlement")
        self.assertEqual(gate["config"], {"condUserRole": "crosswatch.admin", "negate": "true"})

    def test_flow_is_bound_and_member_seeded(self):
        overrides = self.fake.clients["crosswatch"]["authenticationFlowBindingOverrides"]
        self.assertEqual(overrides["browser"], self.fake.flows["browser-crosswatch"]["id"])
        group_id = self.fake.groups["crosswatch/admin"]["id"]
        self.assertIn(("user-void", group_id), self.fake.memberships)
        self.assertEqual(self.fake.group_roles[(group_id, self.cid)], {"admin"})

    def test_second_run_changes_nothing(self):
        report = rc.Report()
        rc.reconcile(self.fake, crosswatch_config(), report, role_timeout=0)
        self.assertEqual(report.changes, [])
        self.assertEqual(report.problems, [])


class ExistingFlow(unittest.TestCase):
    def test_tied_priorities_get_fixed_without_ever_putting_the_gate_first(self):
        fake = FakeKeycloak()
        fake.add_client("gitea", ["user", "admin"])
        fake.add_tied_flow("gitea", "user")
        config = {
            "realm": "homelab",
            "clients": {"gitea": {"gate": "user", "groups": {"user": ["user"]}}},
        }
        report = rc.Report()
        rc.reconcile(fake, config, report, role_timeout=0)
        self.assertEqual(report.problems, [])
        self.assertTrue(fake.top_level_orders)
        for alias, names in fake.top_level_orders:
            if alias == "browser-gitea":
                self.assertEqual(names, ["gitea-authenticate", "gitea-gate"])

    def test_existing_group_does_not_get_members_seeded(self):
        fake = FakeKeycloak()
        fake.add_client("crosswatch", ["admin"])
        fake.groups["crosswatch"] = {"id": "p", "name": "crosswatch"}
        fake.groups["crosswatch/admin"] = {"id": "g", "name": "admin"}
        rc.reconcile(fake, crosswatch_config(), rc.Report(), role_timeout=0)
        self.assertEqual(fake.memberships, set())

    def test_wrong_gate_role_is_corrected(self):
        fake = FakeKeycloak()
        fake.add_client("gitea", ["user"])
        fake.add_tied_flow("gitea", "wrong")
        config = {"realm": "homelab", "clients": {"gitea": {"gate": "user", "groups": {"user": ["user"]}}}}
        rc.reconcile(fake, config, rc.Report(), role_timeout=0)
        gate = next(c for c in fake.configs.values() if c["alias"] == "gitea-entitlement")
        self.assertEqual(gate["config"]["condUserRole"], "gitea.user")

    def test_ignored_priority_is_a_problem_and_new_gate_stays_unbound(self):
        fake = FakeKeycloak()
        fake.ignore_priority = True
        fake.add_client("crosswatch", ["admin"])
        report = rc.Report()
        rc.reconcile(fake, crosswatch_config(), report, role_timeout=0)
        self.assertTrue(report.problems)
        self.assertEqual(fake.clients["crosswatch"]["authenticationFlowBindingOverrides"], {})


class LimitedPermissions(unittest.TestCase):
    def config(self):
        return {
            "realm": "homelab",
            "clients": {
                "gitea": {"gate": "user", "claim": "gitea_roles", "groups": {"user": ["user"]}},
                "crosswatch": crosswatch_config()["clients"]["crosswatch"],
            },
        }

    def test_mappers_land_and_everything_else_is_reported_not_fatal(self):
        fake = FakeKeycloak(can_manage_realm=False, can_manage_users=False)
        fake.add_client("gitea", ["user"])
        fake.add_client("crosswatch", ["admin"])
        fake.add_tied_flow("gitea", "user")
        report = rc.Report()
        rc.reconcile(fake, self.config(), report, role_timeout=0)
        self.assertEqual([m["config"]["claim.name"] for m in fake.mappers["cid-gitea"]], ["gitea_roles"])
        self.assertEqual(report.problems, [])
        self.assertTrue(report.blocked)
        self.assertNotIn("browser-crosswatch", fake.flows)
        self.assertEqual(fake.clients["crosswatch"]["authenticationFlowBindingOverrides"], {})

    def test_gate_never_binds_when_its_group_could_not_be_created(self):
        fake = FakeKeycloak(can_manage_realm=True, can_manage_users=False)
        fake.add_client("crosswatch", ["admin"])
        report = rc.Report()
        rc.reconcile(fake, crosswatch_config(), report, role_timeout=0)
        self.assertEqual(fake.clients["crosswatch"]["authenticationFlowBindingOverrides"], {})
        self.assertTrue(any("not bound" in b for b in report.blocked))


class Mapper(unittest.TestCase):
    def config(self, claim="gitea_roles"):
        return {
            "realm": "homelab",
            "claimCheckUser": "void",
            "clients": {"gitea": {"gate": "user", "claim": claim, "groups": {"admin": ["user", "admin"]}}},
        }

    def test_mapper_is_scoped_to_its_own_client(self):
        fake = FakeKeycloak()
        fake.add_client("gitea", ["user", "admin"])
        rc.reconcile(fake, self.config(), rc.Report(), role_timeout=0)
        config = fake.mappers["cid-gitea"][0]["config"]
        self.assertEqual(config["usermodel.clientRoleMapping.clientId"], "gitea")
        for target in ("id.token.claim", "access.token.claim", "userinfo.token.claim", "multivalued"):
            self.assertEqual(config[target], "true")

    def test_existing_mapper_under_another_name_is_reused(self):
        fake = FakeKeycloak()
        fake.add_client("gitea", ["user", "admin"])
        fake.mappers["cid-gitea"].append(
            {"id": "m", "name": "handmade", "config": {"claim.name": "gitea_roles", "multivalued": "false"}}
        )
        rc.reconcile(fake, self.config(), rc.Report(), role_timeout=0)
        self.assertEqual(len(fake.mappers["cid-gitea"]), 1)
        self.assertEqual(fake.mappers["cid-gitea"][0]["config"]["multivalued"], "true")

    def test_claim_matching_the_role_mappings_passes(self):
        fake = FakeKeycloak()
        cid = fake.add_client("gitea", ["user", "admin"])
        report = rc.Report()
        rc.reconcile(fake, self.config(), report, role_timeout=0)
        group_id = fake.groups["gitea/admin"]["id"]
        fake.memberships.add(("user-void", group_id))
        self.assertEqual(fake.group_roles[(group_id, cid)], {"user", "admin"})
        report = rc.Report()
        rc.reconcile(fake, self.config(), report, role_timeout=0)
        self.assertEqual(report.problems, [])

    def test_claim_differing_from_role_mappings_is_a_problem(self):
        fake = FakeKeycloak()
        fake.add_client("gitea", ["user", "admin"])
        fake.example_token = lambda cid, uid: {"gitea_roles": ["wrong"]}
        report = rc.Report()
        rc.reconcile(fake, self.config(), report, role_timeout=0)
        self.assertTrue(any("gitea_roles" in p for p in report.problems))

    def test_missing_role_is_a_problem_not_a_crash(self):
        fake = FakeKeycloak()
        fake.add_client("gitea", ["user"])
        report = rc.Report()
        rc.reconcile(fake, self.config(), report, role_timeout=0)
        self.assertTrue(any("admin" in p for p in report.problems))


if __name__ == "__main__":
    unittest.main()
