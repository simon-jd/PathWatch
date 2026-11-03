"""Offline checks of the GitOps layout and the rendered canary contract."""
import os
import re
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEPLOY = os.path.join(ROOT, "deploy")


def load(*parts):
    with open(os.path.join(DEPLOY, *parts)) as f:
        return [d for d in yaml.safe_load_all(f) if d]


class CanaryContract(unittest.TestCase):
    def setUp(self):
        docs = {d["kind"]: d for d in load("rollouts", "ingest-rollout.yaml")}
        self.rollout, self.template = docs["Rollout"], docs["AnalysisTemplate"]

    def test_steps_shift_traffic_then_analyse_the_candidate(self):
        canary = self.rollout["spec"]["strategy"]["canary"]
        steps = canary["steps"]
        self.assertIn("setWeight", steps[0])
        analysis = steps[1]["analysis"]
        self.assertEqual(analysis["templates"][0]["templateName"], self.template["metadata"]["name"])
        self.assertEqual(analysis["args"][0], {"name": "canary-hash", "valueFrom": {"podTemplateHashValue": "Latest"}})
        self.assertEqual(canary["maxUnavailable"], 0)

    def test_analysis_fails_closed_on_candidate_error_ratio(self):
        m = self.template["spec"]["metrics"][0]
        self.assertEqual(m["failureLimit"], 0)
        q = m["provider"]["prometheus"]["query"]
        self.assertIn('outcome="failure"', q)
        self.assertEqual(q.count('rollouts_pod_template_hash="{{args.canary-hash}}"'), 3)
        # No data (empty vector) must count as failure, not pass or stall.
        self.assertTrue(m["failureCondition"].startswith("len(result) != 1 ||"))
        self.assertTrue(m["successCondition"].startswith("len(result) == 1 &&"))
        self.assertTrue(m["provider"]["prometheus"]["address"].startswith("http://prometheus.pathwatch-observe.svc"))

    def test_pods_keep_loopback_boundaries(self):
        spec = self.rollout["spec"]["template"]["spec"]
        frontend = next(c for c in spec["containers"] if c["name"] == "frontend")
        args = frontend["args"]
        for flag in ("-listen", "-query-listen", "-metrics"):
            self.assertTrue(args[args.index(flag) + 1].startswith("127.0.0.1:"), flag)
        edge = spec["initContainers"][0]
        self.assertEqual((edge["name"], edge["restartPolicy"]), ("edge-proxy", "Always"))

    def test_frontend_keeps_serving_until_its_endpoint_is_gone(self):
        # Deleting a pod removes it from the Service endpoints and sends SIGTERM at the
        # same time; the frontend closes its query listener on SIGTERM. The preStop sleep
        # delays SIGTERM until kube-proxy has dropped the endpoint. The edge-proxy native
        # sidecar is only stopped after the frontend exits, so it serves through the sleep.
        spec = self.rollout["spec"]["template"]["spec"]
        frontend = next(c for c in spec["containers"] if c["name"] == "frontend")
        sleep = frontend["lifecycle"]["preStop"]["sleep"]["seconds"]
        self.assertGreaterEqual(sleep, 5)
        # Then query drain 5 s + gRPC graceful stop 35 s + metrics 5 s (ingest/cmd/ingest).
        self.assertGreaterEqual(spec["terminationGracePeriodSeconds"], sleep + 45)


class CanaryBlastRadius(unittest.TestCase):
    def test_first_step_is_a_quarter_of_more_replicas_and_analysis_starts_early(self):
        docs = {d["kind"]: d for d in load("rollouts", "ingest-rollout.yaml")}
        spec = docs["Rollout"]["spec"]
        self.assertGreaterEqual(spec["replicas"], 4)
        self.assertLessEqual(spec["strategy"]["canary"]["steps"][0]["setWeight"], 25)
        m = docs["AnalysisTemplate"]["spec"]["metrics"][0]
        self.assertLessEqual(int(m["initialDelay"].rstrip("s")), 20)
        self.assertLessEqual(int(m["interval"].rstrip("s")), 10)


class NetworkPolicies(unittest.TestCase):
    def test_default_deny_then_only_the_used_paths(self):
        with open(os.path.join(DEPLOY, "charts", "ingest", "templates", "networkpolicy.yaml")) as f:
            docs = [d for d in yaml.safe_load_all(re.sub(r"\{\{.*?\}\}", "", f.read(), flags=re.S)) if d]
        by = {d["metadata"]["name"]: d["spec"] for d in docs}
        self.assertEqual(by["default-deny-ingress"], {"podSelector": {}, "policyTypes": ["Ingress"]})
        core = by["core-from-frontend"]
        self.assertEqual(core["podSelector"]["matchLabels"], {"app.kubernetes.io/name": "core"})
        # Numeric ports: kindnet did not resolve the named port on the native sidecar (initContainer).
        self.assertEqual(core["ingress"][0]["ports"], [{"protocol": "TCP", "port": 9444}])
        https = by["frontend-https-from-clients"]
        self.assertEqual(https["ingress"][0]["ports"], [{"protocol": "TCP", "port": 8443}])
        metrics = by["metrics-from-prometheus"]["ingress"][0]
        self.assertEqual(metrics["ports"], [{"protocol": "TCP", "port": 9445}])
        self.assertEqual(metrics["from"][0]["namespaceSelector"]["matchLabels"], {"kubernetes.io/metadata.name": "pathwatch-observe"})
        self.assertEqual(by["default-deny-egress"]["policyTypes"], ["Egress"])
        self.assertNotIn("egress", by["default-deny-egress"])
        self.assertEqual(by["dns-egress"]["egress"][0]["ports"], [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}])
        self.assertEqual(by["frontend-egress-to-core"]["egress"][0]["ports"], [{"protocol": "TCP", "port": 9444}])
        ports = by["querygen-egress-to-frontend"]["egress"][0]["ports"]
        self.assertIn({"protocol": "TCP", "port": 443}, ports)
        self.assertIn({"protocol": "TCP", "port": 8443}, ports)
        # Argo CD refuses kinds the AppProject does not list (first kind sync failed on this).
        project = next(d for d in load("argocd", "apps", "project.yaml") if d["kind"] == "AppProject")
        self.assertIn({"group": "networking.k8s.io", "kind": "NetworkPolicy"}, project["spec"]["namespaceResourceWhitelist"])


class ClientImpact(unittest.TestCase):
    def test_counts_non_200_and_the_failing_span(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("kindlab", os.path.join(DEPLOY, "kind", "lab.py"))
        lab = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lab)
        lines = ['{"t":"a","sent":20,"status":{"200":20}}', '{"t":"b","sent":20,"status":{"200":15,"500":5}}',
                 'not json', '{"t":"c","sent":20,"status":{"200":19,"transport_error":1}}', '{"t":"d","sent":20,"status":{"200":20}}']
        self.assertEqual(lab.client_impact(lines), {"sent": 80, "non_200": 6, "non_200_fraction": 0.075,
                                                    "seconds_with_non_200": 2, "first_non_200": "b", "last_non_200": "c"})

    def test_counts_report_rpcs_that_are_not_ok(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("kindlab", os.path.join(DEPLOY, "kind", "lab.py"))
        lab = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lab)
        lines = ['{"t":"a","sent":20,"status":{"200":20},"report_sent":2,"report":{"OK":2}}',
                 '{"t":"b","sent":20,"status":{"200":20},"report_sent":2,"report":{"OK":1,"Unavailable":1}}',
                 '{"t":"c","sent":20,"status":{"200":20}}']
        self.assertEqual(lab.client_impact(lines, field="report", ok="OK"),
                         {"sent": 4, "non_200": 1, "non_200_fraction": 0.25, "seconds_with_non_200": 1,
                          "first_non_200": "b", "last_non_200": "b"})
        self.assertEqual(lab.client_impact(lines)["non_200"], 0)


class LabPKI(unittest.TestCase):
    def test_reuses_a_fresh_pki_and_issues_a_new_one_near_expiry(self):
        # labpki certificates last 72 h; the lab reused one directory forever, so a
        # cluster created three days later crash-looped on expired certificates.
        import importlib.util
        import tempfile
        spec = importlib.util.spec_from_file_location("kindlab", os.path.join(DEPLOY, "kind", "lab.py"))
        lab = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lab)
        now = 1_800_000_000.0
        with tempfile.TemporaryDirectory() as cache:
            path, fresh = lab.lab_pki(cache, now)
            self.assertTrue(fresh)
            self.assertTrue(os.path.basename(path).startswith("pki-"))

            def issued(name, hours_ago):
                d = os.path.join(cache, name)
                os.makedirs(d)
                ca = os.path.join(d, "ca.pem")
                open(ca, "w").close()
                os.utime(ca, (now - hours_ago * 3600, now - hours_ago * 3600))
                return d

            old = issued("pki", 10)
            self.assertEqual(lab.lab_pki(cache, now), (old, False))
            newer = issued("pki-20261101T000000Z", 2)
            self.assertEqual(lab.lab_pki(cache, now), (newer, False))
            # Less than the 12 h margin left on the newest one: issue another, keep the old ones.
            path, fresh = lab.lab_pki(cache, now + 62 * 3600)
            self.assertTrue(fresh)
            self.assertNotIn(path, (old, newer))
            self.assertTrue(os.path.isdir(old) and os.path.isdir(newer))


class GitOpsLayout(unittest.TestCase):
    def test_root_and_children_point_at_existing_paths_in_the_project(self):
        root = load("argocd", "app-of-apps.yaml")[0]
        repo = root["spec"]["source"]["repoURL"]
        self.assertTrue(repo.startswith("git://127.0.0.1:"), "desired state must stay local")
        self.assertTrue(os.path.isdir(os.path.join(ROOT, root["spec"]["source"]["path"])))
        docs = load("argocd", "apps", "project.yaml") + load("argocd", "apps", "children.yaml")
        project = next(d for d in docs if d["kind"] == "AppProject")
        allowed = {d["namespace"] for d in project["spec"]["destinations"]}
        apps = [d for d in docs if d["kind"] == "Application"]
        self.assertEqual({a["metadata"]["name"] for a in apps}, {"pathwatch-observability", "pathwatch-ingest", "pathwatch-api"})
        for a in apps:
            src = a["spec"]["source"]
            self.assertEqual(src["repoURL"], repo)
            self.assertIn(repo, project["spec"]["sourceRepos"])
            self.assertEqual(a["spec"]["project"], project["metadata"]["name"])
            self.assertIn(a["spec"]["destination"]["namespace"], allowed)
            self.assertTrue(os.path.exists(os.path.join(ROOT, src["path"], "Chart.yaml")), src["path"])

    def test_no_mutable_or_default_application_image_tags(self):
        for chart in ("ingest", "api", "observability"):
            with open(os.path.join(DEPLOY, "charts", chart, "values.yaml")) as f:
                images = yaml.safe_load(f)["images"]
            for name, ref in images.items():
                self.assertNotRegex(ref, r":latest$")
                if name in ("ingest", "frontend", "edgeProxy", "querygen"):
                    self.assertEqual(ref, "", f"{chart}.{name} is set per desired-state revision, not by default")
        for path in ("charts/ingest/templates/core.yaml", "charts/ingest/templates/frontend.yaml", "charts/api/templates/api.yaml"):
            with open(os.path.join(DEPLOY, path)) as f:
                self.assertRegex(f.read(), r"required \"images\.")


if __name__ == "__main__":
    unittest.main()
