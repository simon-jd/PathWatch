"""Task 14 local lab: kind + Argo CD Core + Argo Rollouts (deploy/README.md).

Subcommands (run through make so the project toolchain env is set):
  up        create the kind cluster, Secrets, Argo CD Core and Argo Rollouts
  images    build and kind-load the images for the current commit
  publish   write a desired-state commit to the local GitOps repo
  drill     time a canary release (bad500: expect automatic rollback; control: expect promotion)
  podkill   delete the frontend pod holding querygen's Report connection; time the client impact
  status    print applications and the rollout
  down      delete the kind cluster
Only the project's own cluster (pathwatch-canary) and kubeconfig are used.
"""
import argparse
import datetime
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(ROOT, ".cache", "task14")
KUBECONFIG = os.path.join(ROOT, ".cache", "kind", "kubeconfig")
CLUSTER = "pathwatch-canary"
NODE = CLUSTER + "-control-plane"
NODE_IMAGE = "kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed"
GITOPS = os.path.join(CACHE, "gitops")
REPO = os.path.join(GITOPS, "pathwatch-deploy")
# labpki issues the lab CA and leaf certificates for PKI_VALIDITY seconds.
PKI_VALIDITY = 72 * 3600
PKI_MARGIN = 12 * 3600  # a cluster created now must keep working through a drill session


def lab_pki(cache=CACHE, now=None):
    """(directory, needs_issuing) for the lab PKI.

    The newest `pki` / `pki-<UTC>` directory is reused while its ca.pem (mtime = issue
    time) has more than PKI_MARGIN left; otherwise a new `pki-<UTC>` directory is named.
    Older directories are left in place."""
    now = time.time() if now is None else now
    issued = []
    if os.path.isdir(cache):
        for name in os.listdir(cache):
            ca = os.path.join(cache, name, "ca.pem")
            if (name == "pki" or name.startswith("pki-")) and os.path.exists(ca):
                issued.append((os.path.getmtime(ca), os.path.join(cache, name)))
    if issued:
        at, newest = max(issued)
        if at + PKI_VALIDITY - PKI_MARGIN > now:
            return newest, False
    stamp = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(cache, "pki-" + stamp), True
DL = os.path.join(CACHE, "dl")
APP_NS, OBS_NS = "pathwatch-app", "pathwatch-observe"
THIRD_PARTY_IMAGES = ["clickhouse/clickhouse-server:26.3.32.14", "prom/prometheus:v3.14.0"]
VARIANT_SUFFIX = {"good": "", "bad500": "-bad500", "control": "-control"}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sh(*args, capture=False, input=None, check=True, cwd=None, env=None):
    print("+", " ".join(args), file=sys.stderr, flush=True)
    r = subprocess.run(args, capture_output=capture, text=True, input=input, check=False, cwd=cwd, env=env)
    if check and r.returncode != 0:
        if capture:
            sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"command failed ({r.returncode}): {' '.join(args)}")
    return r


def kubectl(*args, **kw):
    return sh("kubectl", "--kubeconfig", KUBECONFIG, *args, **kw)


def kget(*args):
    r = kubectl("get", "-o", "json", *args, capture=True, check=False)
    return json.loads(r.stdout) if r.returncode == 0 else None


def source_sha():
    dirty = sh("git", "status", "--porcelain", "--", "deploy", "ingest", "proxy", "go.mod", "go.sum", capture=True, cwd=ROOT).stdout
    if dirty.strip():
        raise SystemExit("commit deploy/ ingest/ proxy/ first: images and desired state must map to a commit\n" + dirty)
    return sh("git", "rev-parse", "--short=12", "HEAD", capture=True, cwd=ROOT).stdout.strip()


IMAGE_INPUTS = ["ingest", "proxy", "proto", "deploy/cmd", "deploy/images", "go.mod", "go.sum"]


def image_sha():
    """Last commit that changed anything the images are built from."""
    source_sha()
    return sh("git", "log", "-1", "--format=%H", "--", *IMAGE_INPUTS, capture=True, cwd=ROOT).stdout.strip()[:12]


def tags(sha, variant="good"):
    return {
        "ingest": f"pathwatch/ingest:git-{sha}",
        "frontend": f"pathwatch/ingest:git-{sha}{VARIANT_SUFFIX[variant]}",
        "edgeProxy": f"pathwatch/edge-proxy:git-{sha}",
        "querygen": f"pathwatch/querygen:git-{sha}",
    }


# ---- up / down ---------------------------------------------------------------

def apply_yaml(text):
    kubectl("apply", "-f", "-", input=text)


def secret(ns, name, files):
    y = kubectl("create", "secret", "generic", name, "-n", ns, "--dry-run=client", "-o", "yaml",
                *[f"--from-file={k}={v}" for k, v in files.items()], capture=True).stdout
    apply_yaml(y)


def up(_):
    os.makedirs(os.path.dirname(KUBECONFIG), exist_ok=True)
    os.makedirs(GITOPS, exist_ok=True)
    cfg = os.path.join(ROOT, ".cache", "kind", "cluster.yaml")
    with open(cfg, "w") as f:
        f.write("kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\nnodes:\n- role: control-plane\n"
                f"  extraMounts:\n  - hostPath: {GITOPS}\n    containerPath: /pathwatch-gitops\n    readOnly: true\n")
    clusters = sh("kind", "get", "clusters", capture=True).stdout.split()
    if CLUSTER not in clusters:
        sh("kind", "create", "cluster", "--name", CLUSTER, "--kubeconfig", KUBECONFIG, "--config", cfg, "--image", NODE_IMAGE, "--wait", "180s")
    # The shared host's root inotify instances are exhausted by other projects'
    # containers, so kube-proxy dies at "failed complete: too many open files":
    # it only opens a watcher for --config. Same settings as kind's config.conf,
    # as flags. (Raising fs.inotify.max_user_instances needs root.)
    kubectl("patch", "daemonset", "kube-proxy", "-n", "kube-system", "--type", "json", "-p", json.dumps([{
        "op": "replace", "path": "/spec/template/spec/containers/0/command",
        "value": ["/usr/local/bin/kube-proxy", "--kubeconfig=/var/lib/kube-proxy/kubeconfig.conf", "--proxy-mode=iptables",
                  "--cluster-cidr=10.244.0.0/16", "--conntrack-max-per-core=0", "--hostname-override=$(NODE_NAME)"]}]))
    kubectl("rollout", "status", "daemonset/kube-proxy", "-n", "kube-system", "--timeout=300s")
    kubectl_bin = os.path.join(ROOT, ".tools", "bin", "kubectl")
    if not os.path.exists(kubectl_bin):
        # The node image ships the kubectl matching its Kubernetes version.
        sh("docker", "cp", f"{NODE}:/usr/bin/kubectl", kubectl_bin)
    for ns in ["argocd", "argo-rollouts", APP_NS, OBS_NS]:
        apply_yaml(kubectl("create", "namespace", ns, "--dry-run=client", "-o", "yaml", capture=True).stdout)
        kubectl("label", "namespace", ns, "project=pathwatch", "--overwrite")
    pki, fresh = lab_pki()
    if fresh:
        sh("go", "run", "./ingest/cmd/labpki", "-out", pki, "-validity", f"{PKI_VALIDITY // 3600}h", cwd=ROOT)
    pw = os.path.join(pki, "clickhouse-password")
    if not os.path.exists(pw):
        fd = os.open(pw, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, secrets.token_hex(24).encode())
        os.close(fd)
    p = lambda n: os.path.join(pki, n)
    secret(APP_NS, "pathwatch-clickhouse", {"password": pw})
    secret(APP_NS, "pathwatch-core-tls", {"ca.pem": p("ca.pem"), "core.pem": p("core.pem"), "core-key.pem": p("core-key.pem")})
    secret(APP_NS, "pathwatch-frontend-tls", {"ca.pem": p("ca.pem"), "frontend.pem": p("frontend.pem"), "frontend-key.pem": p("frontend-key.pem"),
                                              "edge.pem": p("edge.pem"), "edge-key.pem": p("edge-key.pem")})
    secret(APP_NS, "pathwatch-metrics-tls", {"ca.pem": p("ca.pem"), "metrics.pem": p("metrics.pem"), "metrics-key.pem": p("metrics-key.pem")})
    secret(APP_NS, "pathwatch-api-client", {"ca.pem": p("ca.pem")})
    secret(OBS_NS, "pathwatch-prometheus-tls", {"ca.pem": p("ca.pem"), "prometheus.pem": p("prometheus.pem"), "prometheus-key.pem": p("prometheus-key.pem")})
    kubectl("apply", "-n", "argocd", "--server-side", "--force-conflicts", "-f", os.path.join(DL, "argocd", "core-install.yaml"))
    kubectl("patch", "deployment", "argocd-repo-server", "-n", "argocd", "--type", "strategic",
            "--patch-file", os.path.join(ROOT, "deploy", "argocd", "bootstrap", "repo-server-git-daemon.yaml"))
    kubectl("apply", "-n", "argo-rollouts", "--server-side", "--force-conflicts", "-f", os.path.join(DL, "rollouts", "install.yaml"))
    for ns, kind in [("argocd", "deployment"), ("argocd", "statefulset"), ("argo-rollouts", "deployment")]:
        names = kubectl("get", kind, "-n", ns, "-o", "name", capture=True).stdout.split()
        for n in names:
            kubectl("rollout", "status", n, "-n", ns, "--timeout=600s")


def down(_):
    sh("kind", "delete", "cluster", "--name", CLUSTER, "--kubeconfig", KUBECONFIG)


# ---- images -----------------------------------------------------------------

def load_image(img):
    if sh("kind", "load", "docker-image", "--name", CLUSTER, img, check=False).returncode == 0:
        return
    # Multi-platform pulls lack other-arch content, which kind's --all-platforms
    # import requires; a linux/amd64-only archive imports cleanly.
    tar = os.path.join(CACHE, "image-load.tar")
    try:
        sh("docker", "save", "--platform", "linux/amd64", "-o", tar, img)
        sh("kind", "load", "image-archive", "--name", CLUSTER, tar)
    finally:
        if os.path.exists(tar):
            os.remove(tar)


def images(args):
    sha = image_sha()
    out = os.path.join(CACHE, "build", sha)
    env = dict(os.environ, CGO_ENABLED="0")
    for variant, extra in [("good", []), ("bad500", ["-tags", "pathwatch_lab_bad500"])]:
        d = os.path.join(out, "ingest-" + variant)
        os.makedirs(d, exist_ok=True)
        sh("go", "build", "-trimpath", *extra, "-o", os.path.join(d, "ingest"), "./ingest/cmd/ingest", cwd=ROOT, env=env)
        sh("go", "build", "-trimpath", "-o", os.path.join(d, "adminbridge"), "./ingest/cmd/adminbridge", cwd=ROOT, env=env)
    qd = os.path.join(out, "querygen")
    os.makedirs(qd, exist_ok=True)
    sh("go", "build", "-trimpath", "-o", os.path.join(qd, "querygen"), "./deploy/cmd/querygen", cwd=ROOT, env=env)
    ed = os.path.join(out, "edge-proxy")
    os.makedirs(ed, exist_ok=True)
    sh("cargo", "build", "--offline", "--locked", "--release", "--manifest-path", "proxy/Cargo.toml", "-p", "edge-proxy", cwd=ROOT)
    shutil.copy2(os.path.join(os.environ.get("CARGO_TARGET_DIR", os.path.join(ROOT, "proxy", "target")), "release", "edge-proxy"), ed)
    t, bad, control = tags(sha), tags(sha, "bad500"), tags(sha, "control")
    df = lambda n: os.path.join(ROOT, "deploy", "images", n + ".Dockerfile")
    sh("docker", "build", "-q", "-t", t["ingest"], "-t", control["frontend"], "-f", df("ingest"), os.path.join(out, "ingest-good"))
    sh("docker", "build", "-q", "-t", bad["frontend"], "-f", df("ingest"), os.path.join(out, "ingest-bad500"))
    sh("docker", "build", "-q", "-t", t["querygen"], "-f", df("querygen"), qd)
    sh("docker", "build", "-q", "-t", t["edgeProxy"], "-f", df("edge-proxy"), ed)
    # The host-built Rust binary must load in the image's glibc.
    r = sh("docker", "run", "--rm", "--name", "pathwatch-edge-smoke", t["edgeProxy"], "/nonexistent.toml", capture=True, check=False)
    if "GLIBC" in r.stderr or "error while loading shared libraries" in r.stderr:
        raise SystemExit("edge-proxy does not run in its base image:\n" + r.stderr)
    imgs = [t["ingest"], bad["frontend"], control["frontend"], t["querygen"], t["edgeProxy"]]
    for img in imgs + THIRD_PARTY_IMAGES:
        load_image(img)
    manifest = {"source": sha, "built_at": now(), "images": {}}
    for i in imgs:
        manifest["images"][i] = sh("docker", "images", "--no-trunc", "--format", "{{.ID}}", i, capture=True).stdout.strip()
    with open(os.path.join(out, "images.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    print(json.dumps(manifest, indent=2))


# ---- desired state ------------------------------------------------------------

def git_repo(*args, capture=True):
    return sh("git", "-C", REPO, *args, capture=capture)


def publish_tree(sha, variant, message):
    """Write deploy/ from commit `sha` plus values-kind.yaml into the GitOps repo and commit."""
    if not os.path.isdir(os.path.join(REPO, ".git")):
        os.makedirs(REPO, exist_ok=True)
        git_repo("init", "-q", "-b", "main")
        git_repo("config", "user.name", "pathwatch-lab")
        git_repo("config", "user.email", "pathwatch-lab@localhost")
    shutil.rmtree(os.path.join(REPO, "deploy"), ignore_errors=True)
    full = sh("git", "rev-parse", sha, capture=True, cwd=ROOT).stdout.strip()
    arch = subprocess.run(["git", "archive", "--format=tar", full, "deploy/charts", "deploy/argocd"], cwd=ROOT, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(arch)) as tf:
        tf.extractall(REPO, filter="data")
    charts = os.path.join(REPO, "deploy", "charts")
    # Vendor the library chart so rendering needs no `helm dependency build`.
    shutil.copytree(os.path.join(charts, "edge-proxy"), os.path.join(charts, "ingest", "charts", "edge-proxy"))
    t = tags(image_sha(), variant)
    with open(os.path.join(charts, "ingest", "values-kind.yaml"), "w") as f:
        f.write(f"# desired state from source {full}, frontend variant {variant}\nimages:\n"
                f"  ingest: {t['ingest']}\n  frontend: {t['frontend']}\n  edgeProxy: {t['edgeProxy']}\n")
    with open(os.path.join(charts, "api", "values-kind.yaml"), "w") as f:
        f.write(f"# desired state from source {full}\nimages:\n  querygen: {t['querygen']}\n")
    with open(os.path.join(REPO, "provenance.json"), "w") as f:
        json.dump({"source_commit": full, "image_source_commit": image_sha(), "frontend_variant": variant, "images": t}, f, indent=2)
    git_repo("add", "-A")
    if git_repo("status", "--porcelain").stdout.strip():
        git_repo("commit", "-q", "-m", message)
    return git_repo("rev-parse", "HEAD").stdout.strip()


def refresh():
    for a in (kget("applications.argoproj.io", "-n", "argocd") or {"items": []})["items"]:
        kubectl("annotate", "applications.argoproj.io", a["metadata"]["name"], "-n", "argocd", "argocd.argoproj.io/refresh=normal", "--overwrite", capture=True)


def publish(args):
    commit = publish_tree(source_sha(), args.variant, args.message or f"desired state: frontend {args.variant}")
    kubectl("apply", "-f", os.path.join(ROOT, "deploy", "argocd", "bootstrap", "root-project.yaml"))
    kubectl("apply", "-f", os.path.join(ROOT, "deploy", "argocd", "app-of-apps.yaml"))
    refresh()
    print(commit)
    return commit


# ---- observation --------------------------------------------------------------

def apps_state():
    out = {}
    for a in (kget("applications.argoproj.io", "-n", "argocd") or {"items": []})["items"]:
        st = a.get("status", {})
        out[a["metadata"]["name"]] = (st.get("sync", {}).get("status"), st.get("health", {}).get("status"), st.get("sync", {}).get("revision"))
    return out


def rollout_state():
    r = kget("rollout", "frontend", "-n", APP_NS) or {}
    st = r.get("status", {})
    pods = (kget("pods", "-n", APP_NS, "-l", "app.kubernetes.io/name=frontend") or {"items": []})["items"]
    by_hash = {}
    for p in pods:
        if p["metadata"].get("deletionTimestamp"):
            continue
        h = p["metadata"]["labels"].get("rollouts-pod-template-hash")
        ready = any(c["type"] == "Ready" and c["status"] == "True" for c in p.get("status", {}).get("conditions", []))
        by_hash.setdefault(h, [0, 0])
        by_hash[h][0] += 1
        by_hash[h][1] += int(ready)
    runs = (kget("analysisruns", "-n", APP_NS) or {"items": []})["items"]
    return {"phase": st.get("phase"), "message": st.get("message"), "abort": bool(st.get("abort")), "current": st.get("currentPodHash"),
            "stable": st.get("stableRS"), "step": st.get("currentStepIndex"), "replicas": r.get("spec", {}).get("replicas"),
            "pods": by_hash, "analysisruns": {x["metadata"]["name"]: x.get("status", {}).get("phase") for x in runs},
            "images": [c["image"] for c in r.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])][:1]}


def all_healthy():
    a = apps_state()
    return len(a) >= 4 and all(s == "Synced" and h == "Healthy" for s, h, _ in a.values())


def wait_for(pred, timeout, label):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(2)
    raise SystemExit(f"timeout waiting for {label}")


def status(_):
    print(json.dumps({"apps": apps_state(), "rollout": rollout_state()}, indent=2))


def diag(_):
    """Application conditions, non-ready containers and recent warning events."""
    for a in (kget("applications.argoproj.io", "-n", "argocd") or {"items": []})["items"]:
        st = a.get("status", {})
        print(a["metadata"]["name"], st.get("sync", {}).get("status"), st.get("health", {}).get("status"),
              json.dumps(st.get("conditions", [])), (st.get("operationState") or {}).get("message", ""))
    for ns in ["argocd", "argo-rollouts", APP_NS, OBS_NS]:
        for p in (kget("pods", "-n", ns) or {"items": []})["items"]:
            for c in p.get("status", {}).get("initContainerStatuses", []) + p.get("status", {}).get("containerStatuses", []):
                if not c.get("ready"):
                    print(ns, p["metadata"]["name"], c["name"], json.dumps(c.get("state")), "restarts", c.get("restartCount"))
        for e in (kget("events", "-n", ns, "--field-selector", "type=Warning") or {"items": []})["items"][-8:]:
            print(ns, "EVENT", e.get("involvedObject", {}).get("name"), e.get("reason"), (e.get("message") or "")[:300])


def replace_core(_):
    """The core StatefulSet is OnDelete: replacing the sole writer is an explicit, serial step."""
    kubectl("delete", "pod", "core-0", "-n", APP_NS, "--wait=true", "--timeout=120s")
    kubectl("wait", "pod/core-0", "-n", APP_NS, "--for=condition=Ready", "--timeout=300s")


def wait(args):
    end = time.monotonic() + args.timeout
    while time.monotonic() < end and not all_healthy():
        time.sleep(5)
    diag(args)
    print("all applications Synced/Healthy" if all_healthy() else f"not healthy after {args.timeout}s")


def logs(args):
    kubectl("logs", "-n", args.namespace, args.target, "-c", args.container, "--tail", str(args.tail), check=False)


def machine_load():
    with open("/proc/loadavg") as f:
        return {"nproc": os.cpu_count(), "loadavg": f.read().split()[:3], "at": now()}


def client_impact(lines, field="status", ok="200"):
    """Client-side impact from querygen's per-second JSON lines: totals and the failing span.

    field="status", ok="200" counts query GETs; field="report", ok="OK" counts Report RPCs
    (gRPC status codes). The keys keep the "non_200" name for both."""
    sent = bad = 0
    failing = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        codes = rec.get(field) or {}
        n = sum(codes.values())
        b = n - codes.get(ok, 0)
        sent += n
        bad += b
        if b:
            failing.append(rec["t"])
    return {"sent": sent, "non_200": bad, "non_200_fraction": round(bad / sent, 4) if sent else None,
            "seconds_with_non_200": len(failing), "first_non_200": failing[0] if failing else None,
            "last_non_200": failing[-1] if failing else None}


def tcp_reachable(pod_ns, pod, ip, port):
    r = kubectl("exec", "-n", pod_ns, pod, "--", "nc", "-z", "-w", "3", ip, str(port), capture=True, check=False)
    return r.returncode == 0


# Temporary probe pods (busybox nc from the already loaded Prometheus image). Argo CD
# self-heals the policies, so deleting them is no control; instead the same port is
# probed from a pod the policy admits (querygen label) and from one it does not.
PROBES = {"pw-netpol-admitted": "querygen", "pw-netpol-other": "pathwatch-netpol-probe"}


def netpol_checks():
    pods = kget("pods", "-n", APP_NS)["items"]
    ip = {p["metadata"]["labels"].get("app.kubernetes.io/name"): p["status"].get("podIP") for p in pods
          if p["metadata"]["name"] not in PROBES}
    prom = kget("pods", "-n", OBS_NS, "-l", "app.kubernetes.io/name=prometheus")["items"][0]["metadata"]["name"]
    checks = [((OBS_NS, prom), "frontend", 9445, True), ((OBS_NS, prom), "core", 9445, True),
              ((OBS_NS, prom), "frontend", 8443, False), ((OBS_NS, prom), "core", 9444, False),
              ((APP_NS, "pw-netpol-admitted"), "frontend", 8443, True), ((APP_NS, "pw-netpol-admitted"), "core", 9444, False),
              ((APP_NS, "pw-netpol-other"), "frontend", 8443, False), ((APP_NS, "pw-netpol-other"), "frontend", 9445, False),
              ((APP_NS, "pw-netpol-other"), "core", 9444, False)]
    return [{"from": f"{ns}/{pod}", "to": name, "port": port, "expected_open": want,
             "open": tcp_reachable(ns, pod, ip[name], port)} for (ns, pod), name, port, want in checks]


def netpol(args):
    """Probe the NetworkPolicies; a closed port only counts if an admitted pod reaches it."""
    out = {"at": now()}
    for pod, label in PROBES.items():
        kubectl("run", pod, "-n", APP_NS, f"--image={THIRD_PARTY_IMAGES[1]}", "--image-pull-policy=Never",
                f"--labels=app.kubernetes.io/name={label},project=pathwatch", "--restart=Never", "--command", "--", "sleep", "900")
    try:
        for pod in PROBES:
            kubectl("wait", "--for=condition=Ready", f"pod/{pod}", "-n", APP_NS, "--timeout=120s")
        time.sleep(args.settle)  # let the policy dataplane see the new pod labels
        out["settle_seconds"] = args.settle
        out["checks"] = netpol_checks()
    finally:
        # The admitted probe carries querygen's label: `logs deploy/querygen` would pick it while it lingers.
        kubectl("delete", "pod", "-n", APP_NS, "--grace-period=1", *PROBES, check=False)
    # Real traffic under the policies: querygen's last 10 per-second summaries.
    qg = next(p["metadata"]["name"] for p in kget("pods", "-n", APP_NS, "-l", "app.kubernetes.io/name=querygen")["items"]
              if p["metadata"].get("ownerReferences"))
    out["querygen_last_10s"] = client_impact(kubectl("logs", "-n", APP_NS, qg, "-c", "querygen", "--tail", "10", capture=True).stdout.splitlines())
    out["enforced"] = all(r["open"] == r["expected_open"] for r in out["checks"]) and out["querygen_last_10s"]["non_200"] == 0
    print(json.dumps(out, indent=2))
    if not out["enforced"]:
        raise SystemExit("NetworkPolicy result differs from the expected allow-list")


def drill(args):
    """Publish a frontend variant, then poll once per second and timestamp each transition."""
    sha = source_sha()
    wait_for(all_healthy, 600, "all applications Synced/Healthy before the drill")
    before = rollout_state()
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + args.variant
    out = os.path.join(ROOT, "docs", "evidence", "task-14-canary", run_id)
    os.makedirs(out)
    timeline = open(os.path.join(out, "timeline.jsonl"), "w")
    t0_wall, t0 = now(), time.monotonic()
    commit = publish_tree(sha, args.variant, f"drill {run_id}: frontend {args.variant}")
    refresh()
    events, stable_hash = {}, before["stable"]

    def mark(name, cond):
        if cond and name not in events:
            events[name] = round(time.monotonic() - t0, 1)

    deadline = t0 + args.timeout
    done = False
    while time.monotonic() < deadline and not done:
        apps, ro = apps_state(), rollout_state()
        ingest = apps.get("pathwatch-ingest", (None, None, None))
        rec = {"t": round(time.monotonic() - t0, 1), "at": now(), "ingest_app": ingest, "rollout": ro}
        timeline.write(json.dumps(rec) + "\n")
        timeline.flush()
        canary = ro["current"] if ro["current"] != stable_hash else None
        mark("argo_synced_new_revision", ingest[2] == commit)
        mark("canary_pod_ready", canary and ro["pods"].get(canary, [0, 0])[1] >= 1)
        mark("analysisrun_created", any(n.startswith("frontend-") for n in ro["analysisruns"]) and len(ro["analysisruns"]) > len(before["analysisruns"]))
        new_runs = {n: p for n, p in ro["analysisruns"].items() if n not in before["analysisruns"]}
        mark("analysisrun_failed", any(p in ("Failed", "Error") for p in new_runs.values()))
        mark("analysisrun_successful", any(p == "Successful" for p in new_runs.values()))
        mark("rollout_aborted", ro["abort"])
        if args.variant == "bad500":
            stable_ready = ro["pods"].get(stable_hash, [0, 0])[1]
            canary_pods = sum(n for h, (n, _) in ro["pods"].items() if h != stable_hash)
            mark("rollback_complete", ro["abort"] and canary_pods == 0 and stable_ready == ro["replicas"])
            done = "rollback_complete" in events
        else:
            mark("promoted", ro["stable"] not in (None, stable_hash) and ro["phase"] == "Healthy")
            done = "promoted" in events
        time.sleep(1)
    final = {"rollout": rollout_state(), "apps": apps_state()}
    recovery = None
    if args.variant == "bad500" and done:
        # Restore good desired state with a revert commit, as an operator would.
        t1 = time.monotonic()
        git_repo("revert", "--no-edit", "HEAD")
        refresh()
        wait_for(lambda: all_healthy() and rollout_state()["stable"] == stable_hash and rollout_state()["phase"] == "Healthy", 600, "recovery after revert")
        recovery = {"revert_commit": git_repo("rev-parse", "HEAD").stdout.strip(), "seconds_to_synced_healthy": round(time.monotonic() - t1, 1),
                    "apps": apps_state(), "rollout": rollout_state()}
    since = int(time.monotonic() - t0) + 5
    logs = kubectl("logs", "deploy/querygen", "-n", APP_NS, f"--since={since}s", capture=True, check=False).stdout
    with open(os.path.join(out, "querygen.jsonl"), "w") as f:
        f.write(logs)
    for kind in ["rollout/frontend", "analysisruns", "applications.argoproj.io"]:
        ns = "argocd" if kind.startswith("applications") else APP_NS
        with open(os.path.join(out, kind.split("/")[0].split(".")[0] + ".json"), "w") as f:
            f.write(kubectl("get", kind, "-n", ns, "-o", "json", capture=True, check=False).stdout)
    summary = {"run": run_id, "variant": args.variant, "source_commit": sha, "desired_state_commit": commit, "started": t0_wall,
               "completed": done, "events_seconds_after_commit": events, "client_impact": client_impact(logs.splitlines()),
               "client_impact_report": client_impact(logs.splitlines(), field="report", ok="OK"), "stable_hash_before": stable_hash,
               "final": final, "recovery": recovery, "machine_load_end": machine_load(), "machine_load_start": args.load_start,
               "environment": "kind (single node, Kubernetes v1.36.4) on home-ubuntu; not k3s or AKS; other projects were running"}
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps({k: summary[k] for k in ["run", "completed", "events_seconds_after_commit"]}, indent=2))
    print(out)
    if not done:
        raise SystemExit("drill did not reach its expected end state; see " + out)


# ---- pod deletion under Report traffic ------------------------------------------

CONNTRACK = re.compile(r"ESTABLISHED src=(\S+) dst=\S+ sport=(\d+) dport=\d+ src=(\S+) ")


def persistent_peer(first, second, client_ip):
    """(client port, pod IP) of the one client connection ESTABLISHED in both conntrack snapshots, else None.

    querygen opens a new connection per query and keeps one for Report, so the
    connection seen twice is the Report one."""
    a = {(sport, peer) for src, sport, peer in CONNTRACK.findall(first) if src == client_ip}
    b = {(sport, peer) for src, sport, peer in CONNTRACK.findall(second) if src == client_ip}
    both = a & b
    return next(iter(both)) if len(both) == 1 else None


def epoch(stamp):
    """RFC 3339 UTC time stamp (querygen writes nanoseconds) to Unix seconds."""
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z", stamp)
    base = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    return base.timestamp() + float(m.group(2) or 0)


def deletion_impact(lines, deleted_at, until, field="report", ok="OK"):
    """Client impact of one pod deletion from querygen's per-second lines in [deleted_at - 5 s, until].

    Each line counts the second before its time stamp, so the failing span runs from
    the start of the first failing line to the end of the last (seconds after the deletion)."""
    sent, codes, failing = 0, {}, []
    for line in lines:
        try:
            rec = json.loads(line)
            t = epoch(rec["t"])
        except (ValueError, KeyError, AttributeError):
            continue
        if not deleted_at - 5 <= t <= until:
            continue
        got = rec.get(field) or {}
        sent += sum(got.values())
        for code, n in got.items():
            if code != ok:
                codes[code] = codes.get(code, 0) + n
        if any(code != ok for code in got):
            failing.append(t - deleted_at)
    first = round(failing[0] - 1, 1) if failing else None
    last = round(failing[-1], 1) if failing else None
    return {"sent": sent, "non_ok": sum(codes.values()), "codes": codes, "seconds_with_non_ok": len(failing),
            "first_non_ok_s": first, "last_non_ok_s": last, "window_s": round(last - first, 1) if failing else 0}


def reports_steady(lines, seconds=10):
    """True if querygen's last `seconds` lines each carry Report RPCs and all were OK."""
    recs = []
    for line in lines:
        try:
            recs.append(json.loads(line))
        except ValueError:
            continue
    last = recs[-seconds:]
    return len(last) == seconds and all(r.get("report") and set(r["report"]) == {"OK"} for r in last)


def querygen_steady():
    logs = kubectl("logs", "deploy/querygen", "-n", APP_NS, "--tail", "10", capture=True, check=False).stdout
    return reports_steady(logs.splitlines())


def frontends_settled():
    ro = rollout_state()
    pods = (kget("pods", "-n", APP_NS, "-l", "app.kubernetes.io/name=frontend") or {"items": []})["items"]
    ready = sum(r for _, r in ro["pods"].values())
    return ro["phase"] == "Healthy" and ready == ro["replicas"] and len(pods) == ro["replicas"]


def established(client, sport=None):
    """querygen's ESTABLISHED TCP rows in the kind node's conntrack table (optionally one source port)."""
    port = ["--sport", sport] if sport else []
    return sh("docker", "exec", NODE, "conntrack", "-L", "-p", "tcp", "-s", client, *port, "--state", "ESTABLISHED",
              capture=True, check=False).stdout


def pinned_report_pod():
    """(name, IP, querygen IP, client port) of the frontend pod holding querygen's Report connection."""
    qg = kget("pods", "-n", APP_NS, "-l", "app.kubernetes.io/name=querygen")["items"]
    client = next(p["status"]["podIP"] for p in qg if not p["metadata"].get("deletionTimestamp"))
    for _ in range(10):
        first = established(client)
        time.sleep(1.5)
        peer = persistent_peer(first, established(client), client)
        for p in kget("pods", "-n", APP_NS, "-l", "app.kubernetes.io/name=frontend")["items"]:
            if peer and p["status"].get("podIP") == peer[1]:
                return p["metadata"]["name"], peer[1], client, peer[0]
    raise SystemExit("could not identify the pod holding the Report connection")


def watch_termination(pod, deleted_at, client, sport, timeout=150):
    """Poll the deleted pod every 0.5 s until it is gone: first terminated state of each container,
    and when querygen's Report connection to it was first no longer ESTABLISHED."""
    containers, closed, end = {}, None, time.monotonic() + timeout
    while time.monotonic() < end:
        if closed is None and sport not in established(client, sport):
            closed = round(time.time() - deleted_at, 1)
        p = kget("pod", pod, "-n", APP_NS)
        if p is None:
            return {"gone_s": round(time.time() - deleted_at, 1), "report_connection_closed_s": closed, "containers": containers}
        st = p.get("status", {})
        for c in st.get("initContainerStatuses", []) + st.get("containerStatuses", []):
            term = (c.get("state") or {}).get("terminated")
            if term and c["name"] not in containers:
                containers[c["name"]] = {"exit_code": term.get("exitCode"), "reason": term.get("reason"),
                                         "finished_s": round(epoch(term["finishedAt"]) - deleted_at, 1) if term.get("finishedAt") else None,
                                         "seen_s": round(time.time() - deleted_at, 1)}
        time.sleep(0.5)
    return {"gone_s": None, "report_connection_closed_s": closed, "containers": containers}


def podkill(args):
    """Delete the pod holding the Report connection `count` times; one JSON record per deletion."""
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-podkill"
    out = os.path.join(args.output or os.path.join(CACHE, "podkill"), run_id)
    os.makedirs(out)
    images = sorted({c["image"] for p in kget("pods", "-n", APP_NS, "-l", "app.kubernetes.io/name=frontend")["items"]
                     for c in p["spec"].get("initContainers", []) + p["spec"]["containers"]})
    started, records = time.time(), []
    for i in range(args.count):
        wait_for(frontends_settled, 300, "all frontend pods Ready and none terminating")
        time.sleep(args.settle)
        wait_for(querygen_steady, 120, "10 s of OK Report RPCs before the deletion")
        pod, ip, client, sport = pinned_report_pod()
        deleted_at = time.time()
        kubectl("delete", "pod", pod, "-n", APP_NS, "--wait=false")
        term = watch_termination(pod, deleted_at, client, sport)
        time.sleep(args.after)
        logs = kubectl("logs", "deploy/querygen", "-n", APP_NS, f"--since={int(time.time() - deleted_at) + 10}s", capture=True, check=False).stdout
        until = time.time()
        rec = {"deletion": i + 1, "pod": pod, "pod_ip": ip, "deleted_at": datetime.datetime.fromtimestamp(deleted_at, datetime.timezone.utc).isoformat(),
               "report": deletion_impact(logs.splitlines(), deleted_at, until),
               "query": deletion_impact(logs.splitlines(), deleted_at, until, field="status", ok="200"), **term}
        records.append(rec)
        with open(os.path.join(out, "podkill.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(json.dumps({k: rec[k] for k in ("deletion", "pod", "gone_s")} | {"report": rec["report"]}), flush=True)
    logs = kubectl("logs", "deploy/querygen", "-n", APP_NS, f"--since={int(time.time() - started) + 10}s", capture=True, check=False).stdout
    with open(os.path.join(out, "querygen.jsonl"), "w") as f:
        f.write(logs)
    podkill_summary(out, records, images, args.settle, args.load_start)
    print(out)


def podkill_summary(out, records, images, settle, load_start):
    """summary.json of a podkill run; the images (tagged by source commit) say what was measured."""
    head = sh("git", "rev-parse", "--short=12", "HEAD", capture=True, cwd=ROOT).stdout.strip()
    dirty = bool(sh("git", "status", "--porcelain", "--", "deploy", "ingest", "proxy", capture=True, cwd=ROOT).stdout.strip())
    summary = {"run": os.path.basename(out), "checkout_head": head, "checkout_dirty": dirty, "images": images, "count": len(records),
               "settle_s": settle, "report_non_ok": [r["report"]["non_ok"] for r in records],
               "report_window_s": [r["report"]["window_s"] for r in records], "query_non_200": [r["query"]["non_ok"] for r in records],
               "gone_s": [r["gone_s"] for r in records], "machine_load_start": load_start, "machine_load_end": machine_load(),
               "environment": "kind (single node, Kubernetes v1.36.4) on home-ubuntu; not k3s or AKS; other projects were running"}
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("up")
    sub.add_parser("down")
    sub.add_parser("images")
    sub.add_parser("status")
    sub.add_parser("diag")
    sub.add_parser("replace-core")
    np = sub.add_parser("netpol")
    np.add_argument("--settle", type=int, default=5, help="seconds after the probe pods are Ready")
    w = sub.add_parser("wait")
    w.add_argument("--timeout", type=int, default=300)
    lg = sub.add_parser("logs")
    lg.add_argument("namespace")
    lg.add_argument("target")
    lg.add_argument("container")
    lg.add_argument("--tail", type=int, default=40)
    p =sub.add_parser("publish")
    p.add_argument("--variant", choices=list(VARIANT_SUFFIX), default="good")
    p.add_argument("--message")
    d = sub.add_parser("drill")
    d.add_argument("--variant", choices=["bad500", "control"], required=True)
    d.add_argument("--timeout", type=int, default=600)
    pk = sub.add_parser("podkill")
    pk.add_argument("--count", type=int, default=5)
    pk.add_argument("--settle", type=int, default=20, help="seconds of steady traffic before each deletion")
    pk.add_argument("--after", type=int, default=10, help="seconds of traffic kept after the pod is gone")
    pk.add_argument("--output", help="directory for the run (default .cache/task14/podkill)")
    a = ap.parse_args()
    a.load_start = machine_load()
    {"up": up, "down": down, "images": images, "status": status, "diag": diag, "wait": wait, "replace-core": replace_core, "netpol": netpol, "logs": logs, "publish": publish, "drill": drill, "podkill": podkill}[a.cmd](a)


if __name__ == "__main__":
    main()
