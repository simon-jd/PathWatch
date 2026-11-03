"""Task 9 path-change lab on plain Docker networks (no root, no CAP_NET_RAW for the probe).

  probe ── r1 ──(b)── r2 ──(d)── target          primary path, 3 hops
             └──(c)── r3 ──(d)──┘                 alternate path, r3 adds netem delay

  up       networks, routers, target (TCP listener), probe network namespace
  start    run the probe (unprivileged UDP/IP_RECVERR traceroute + TCP connect)
  switch   r1: route the target prefix via r3 instead of r2 (the real path change)
  status   routes and the probe's spool size
  down     remove every pathwatch-t9-* container and network
Only containers/networks named pathwatch-t9-* (label project=pathwatch) are touched.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
RUN = os.path.join(ROOT, ".cache", "task09")
IMAGE = "quay.io/frrouting/frr:10.7.1"
PREFIX = "pathwatch-t9"
LABELS = ["--label", "project=pathwatch", "--label", "owner=codex-pathwatch", "--label", "task=09"]
SUBNETS = {"a": "10.219.1.0/24", "b": "10.219.2.0/24", "c": "10.219.3.0/24", "d": "10.219.4.0/24"}
# node: [(network, address)], first network is the container's primary one.
NODES = {
    "pnet": [("a", "10.219.1.10")],
    "r1": [("a", "10.219.1.2"), ("b", "10.219.2.2"), ("c", "10.219.3.2")],
    "r2": [("b", "10.219.2.3"), ("d", "10.219.4.3")],
    "r3": [("c", "10.219.3.4"), ("d", "10.219.4.4")],
    "target": [("d", "10.219.4.10")],
}
TARGET = "10.219.4.10"
TARGET_PORT = 8080
DELAY_MS = 20
ROUTES = {
    "pnet": ["10.219.0.0/16 via 10.219.1.2"],
    "r1": ["10.219.4.0/24 via 10.219.2.3"],
    "r2": ["10.219.1.0/24 via 10.219.2.2", "10.219.3.0/24 via 10.219.2.2"],
    "r3": ["10.219.1.0/24 via 10.219.3.2", "10.219.2.0/24 via 10.219.3.2"],
    "target": ["10.219.0.0/16 via 10.219.4.3"],
}
SWITCH = ("r1", "10.219.4.0/24 via 10.219.3.4")


def probe_config():
    return {
        "probe_id": "home-t9",
        "ingest": "127.0.0.1:1",  # unreachable on purpose: every result stays in the spool
        "development_cleartext": True,
        "dns_server": "127.0.0.1:53",
        "trace_mode": "udp",
        "interval": "5s",
        "probe_timeout": "3s",
        "spool_dir": "/spool",
        "spool_max_bytes": 256 << 20,
        "batch_size": 128,
        "metrics_addr": "127.0.0.1:9464",
        "targets": [{"ID": "t9-target", "Host": TARGET, "Port": TARGET_PORT, "Kinds": ["tcp", "trace"]}],
    }


def name(node):
    return f"{PREFIX}-{node}"


def sh(*args, check=True):
    print("+", " ".join(args), file=sys.stderr, flush=True)
    r = subprocess.run(args, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"failed: {' '.join(args)}\n{r.stdout}{r.stderr}")
    return r


def dexec(node, *cmd, check=True):
    return sh("docker", "exec", name(node), *cmd, check=check)


def iface(node, address):
    for line in dexec(node, "ip", "-o", "-4", "addr", "show").stdout.splitlines():
        f = line.split()
        if f[3].split("/")[0] == address:
            return f[1]
    raise SystemExit(f"{node} has no interface with {address}")


def up():
    os.makedirs(os.path.join(RUN, "spool"), exist_ok=True)
    for net, subnet in SUBNETS.items():
        sh("docker", "network", "create", *LABELS, "--subnet", subnet, f"{PREFIX}-{net}")
    for node, links in NODES.items():
        caps = ["--cap-drop", "ALL", "--cap-add", "NET_ADMIN"]
        sysctl = []
        if node.startswith("r"):
            sysctl = ["--sysctl", "net.ipv4.ip_forward=1", "--sysctl", "net.ipv4.conf.all.rp_filter=0", "--sysctl", "net.ipv4.conf.default.rp_filter=0"]
        first, ip = links[0]
        sh("docker", "run", "-d", "--name", name(node), *LABELS, *caps, *sysctl, "--memory", "128m",
           "--network", f"{PREFIX}-{first}", "--ip", ip, "--entrypoint", "sleep", IMAGE, "infinity")
        for net, ip in links[1:]:
            sh("docker", "network", "connect", "--ip", ip, f"{PREFIX}-{net}", name(node))
    for node, routes in ROUTES.items():
        for r in routes:
            dexec(node, "ip", "route", "replace", *r.split())
    dev = iface("r3", "10.219.4.4")
    dexec("r3", "tc", "qdisc", "replace", "dev", dev, "root", "netem", "delay", f"{DELAY_MS}ms")
    listener = (
        "import socket\n"
        "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
        f"s.bind(('0.0.0.0',{TARGET_PORT}));s.listen(128)\n"
        "while True:\n c,_=s.accept();c.close()\n"
    )
    sh("docker", "exec", "-d", name("target"), "python3", "-c", listener)


def start(binary):
    cfg = os.path.join(RUN, "probe.json")
    with open(cfg, "w") as f:
        json.dump(probe_config(), f, indent=2)
    uid = f"{os.getuid()}:{os.getgid()}"
    # Shares pnet's network namespace (routes set there); the probe itself has no capabilities.
    sh("docker", "run", "-d", "--name", name("probe"), *LABELS, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
       "--user", uid, "--memory", "256m", "--network", f"container:{name('pnet')}",
       "-v", f"{os.path.abspath(binary)}:/probe:ro", "-v", f"{cfg}:/probe.json:ro", "-v", f"{os.path.join(RUN, 'spool')}:/spool",
       "--entrypoint", "/probe", IMAGE, "-config", "/probe.json")


def switch():
    node, route = SWITCH
    dexec(node, "ip", "route", "replace", *route.split())
    print(json.dumps({"switched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "node": node, "route": route}))


def status():
    out = {node: dexec(node, "ip", "route", "show", check=False).stdout.splitlines() for node in ROUTES}
    out["probe"] = sh("docker", "inspect", "-f", "{{.State.Status}} {{.State.StartedAt}}", name("probe"), check=False).stdout.strip()
    out["probe_caps"] = sh("docker", "inspect", "-f", "{{.HostConfig.CapDrop}} {{.HostConfig.CapAdd}} {{.Config.User}}", name("probe"), check=False).stdout.strip()
    print(json.dumps(out, indent=2))


def down():
    for node in ["probe", *NODES]:
        sh("docker", "rm", "-f", name(node), check=False)
    for net in SUBNETS:
        sh("docker", "network", "rm", f"{PREFIX}-{net}", check=False)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["up", "start", "switch", "status", "down"])
    p.add_argument("--binary", default=os.path.join(RUN, "probe"))
    a = p.parse_args()
    {"up": up, "start": lambda: start(a.binary), "switch": switch, "status": status, "down": down}[a.command]()


if __name__ == "__main__":
    main()
