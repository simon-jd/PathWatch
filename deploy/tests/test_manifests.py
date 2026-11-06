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
        self.edge_config = docs["ConfigMap"]["data"]["config.toml"]

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
        hook = frontend["lifecycle"]["preStop"]["exec"]["command"]
        self.assertEqual(hook[:2], ["/adminbridge", "pre-stop"])
        wait = hook[hook.index("-wait") + 1]
        self.assertRegex(wait, r"^\d+s$")
        sleep = int(wait[:-1])
        self.assertGreaterEqual(sleep, 5)
        # Then query drain 5 s + gRPC graceful stop 35 s + metrics 5 s (ingest/cmd/ingest).
        self.assertGreaterEqual(spec["terminationGracePeriodSeconds"], sleep + 45)

    def test_deletion_drains_the_edge_proxy_before_the_frontend_stops(self):
        # A Report connection is long-lived HTTP/2: without a GOAWAY from the edge proxy it
        # stayed on the deleted pod and failed with Unavailable until the sidecar was killed.
        spec = self.rollout["spec"]["template"]["spec"]
        frontend = next(c for c in spec["containers"] if c["name"] == "frontend")
        hook = frontend["lifecycle"]["preStop"]["exec"]["command"]
        drain = hook[hook.index("-drain") + 1]
        self.assertTrue(drain.startswith("127.0.0.1:"), drain)
        self.assertIn(f'management = {{ addr = "{drain}" }}', self.edge_config)
        ports = [a.split(":")[-1] for a in frontend["args"] if a.startswith("127.0.0.1:")]
        self.assertNotIn(drain.split(":")[1], ports, "management port must not collide with the frontend's")


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


def kindlab():
    import importlib.util
    spec = importlib.util.spec_from_file_location("kindlab", os.path.join(DEPLOY, "kind", "lab.py"))
    lab = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lab)
    return lab


class PodDeletion(unittest.TestCase):
    """make kind-podkill: which pod holds the Report connection, and what one deletion cost the client."""

    def test_the_pinned_pod_is_the_peer_of_the_connection_seen_in_both_snapshots(self):
        lab = kindlab()
        row = "tcp      6 86399 ESTABLISHED src={c} dst=10.96.209.49 sport={p} dport=443 src={s} dst={c} sport=8443 dport={p} [ASSURED] mark=0 use=1"
        first = "\n".join([row.format(c="10.244.0.18", p=46070, s="10.244.0.14"),
                           row.format(c="10.244.0.18", p=38226, s="10.244.0.16"),
                           row.format(c="10.244.0.99", p=50000, s="10.244.0.15")])
        second = "\n".join([row.format(c="10.244.0.18", p=46070, s="10.244.0.14"),
                            row.format(c="10.244.0.18", p=38230, s="10.244.0.17"),
                            row.format(c="10.244.0.99", p=50000, s="10.244.0.15")])
        self.assertEqual(lab.persistent_peer(first, second, "10.244.0.18"), ("46070", "10.244.0.14"))
        # A short query connection that happens to be in both snapshots makes the answer ambiguous.
        both = second + "\n" + row.format(c="10.244.0.18", p=38226, s="10.244.0.16")
        self.assertIsNone(lab.persistent_peer(first, both, "10.244.0.18"))
        self.assertIsNone(lab.persistent_peer("", second, "10.244.0.18"))

    def test_querygen_times_keep_their_nanoseconds(self):
        lab = kindlab()
        self.assertAlmostEqual(lab.epoch("2026-11-06T04:42:48.414455888Z") - lab.epoch("2026-11-06T04:42:47Z"), 1.414455888, places=6)

    def test_deletion_impact_counts_codes_and_times_the_span_from_the_deletion(self):
        lab = kindlab()
        t0 = lab.epoch("2026-11-06T05:00:00Z")
        line = lambda s, report: '{"t":"2026-11-06T05:00:%02d.5Z","sent":20,"status":{"200":20},"report_sent":2,"report":%s}' % (s, report)
        lines = [line(1, '{"OK":2}'), line(2, '{"OK":2}'), line(12, '{"OK":1,"Unavailable":1}'),
                 line(13, '{"Unavailable":2}'), line(14, '{"OK":1,"DeadlineExceeded":1}'), line(15, '{"OK":2}'),
                 line(59, '{"Unavailable":2}')]
        got = lab.deletion_impact(lines, t0, until=t0 + 30)
        self.assertEqual(got["sent"], 12)
        self.assertEqual(got["codes"], {"Unavailable": 3, "DeadlineExceeded": 1})
        self.assertEqual(got["non_ok"], 4)
        self.assertEqual(got["seconds_with_non_ok"], 3)
        # Each line covers the second before its time stamp.
        self.assertEqual((got["first_non_ok_s"], got["last_non_ok_s"], got["window_s"]), (11.5, 14.5, 3.0))
        clean = lab.deletion_impact(lines[:2] + lines[5:6], t0, until=t0 + 30)
        self.assertEqual((clean["non_ok"], clean["window_s"], clean["first_non_ok_s"]), (0, 0, None))


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
