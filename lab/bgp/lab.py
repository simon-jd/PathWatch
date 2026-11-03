"""Task 17 BGP lab on plain Docker networks (lab/bgp/topology.yml).

  up         networks, FRR routers, PoP services, GoBGP collector
  rpki       Krill test TA + ROA, Routinator (RTR for GoBGP, HTTP validity API)
  nearest    traceroute + HTTP from every client to the anycast address (JSON)
  inject     leak | hijack ; withdraw leak | hijack
  status     BGP summaries
  down       remove every pathwatch-bgp container and network
Only containers/networks named pathwatch-bgp-* (label project=pathwatch) are touched.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
RUN = os.path.join(ROOT, ".cache", "task17", "run")
GOBGP_BIN = os.path.join(ROOT, ".cache", "task17", "gobgp-image")
FRR_IMAGE = "quay.io/frrouting/frr:10.7.1"
PREFIX = "pathwatch-bgp"
MON_NET = PREFIX + "-mon"


def topo():
    with open(os.path.join(HERE, "topology.yml")) as f:
        return yaml.safe_load(f)


def sh(*args, check=True, capture=True, quiet=False):
    if not quiet:
        print("+", " ".join(args), file=sys.stderr, flush=True)
    r = subprocess.run(args, capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"failed: {' '.join(args)}\n{r.stdout}{r.stderr}")
    return r


def dexec(node, *cmd, check=True, quiet=False):
    return sh("docker", "exec", f"{PREFIX}-{node}", *cmd, check=check, quiet=quiet)


def vtysh(node, *lines, check=True, quiet=False):
    args = []
    for line in lines:
        args += ["-c", line]
    return dexec(node, "vtysh", *args, check=check, quiet=quiet)


def link_networks(t):
    """{node: [(network, ip), ...]} in link order; the first one is the container's primary network."""
    nets = {}
    for n, (a, aip, b, bip) in sorted(t["links"].items()):
        for node, ip in ((a, aip), (b, bip)):
            nets.setdefault(node, []).append((f"{PREFIX}-l{n}", ip))
    for node, ip in t["monitor"]["members"].items():
        if node in t["nodes"]:
            nets[node].append((MON_NET, ip))
    return nets


def up(_):
    t = topo()
    for n in t["links"]:
        r = sh("docker", "network", "create", "--driver", "bridge", "--subnet", f"10.117.{n}.0/29", "--gateway", f"10.117.{n}.1",
               "--label", "project=pathwatch", f"{PREFIX}-l{n}", check=False)
        if r.returncode and "already exists" not in r.stderr:
            raise SystemExit(r.stderr)
    r = sh("docker", "network", "create", "--driver", "bridge", "--subnet", t["monitor"]["network"], "--gateway", "10.117.100.1",
           "--label", "project=pathwatch", MON_NET, check=False)
    if r.returncode and "already exists" not in r.stderr:
        raise SystemExit(r.stderr)
    for node, nets in link_networks(t).items():
        d = os.path.join(RUN, node)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        shutil.copy(os.path.join(HERE, "frr", node + ".conf"), os.path.join(d, "frr.conf"))
        shutil.copy(os.path.join(HERE, "frr", "daemons"), os.path.join(d, "daemons"))
        with open(os.path.join(d, "vtysh.conf"), "w") as f:
            f.write("service integrated-vtysh-config\n")
        (net0, ip0), rest = nets[0], nets[1:]
        sh("docker", "run", "-d", "--name", f"{PREFIX}-{node}", "--hostname", node, "--label", "project=pathwatch",
           # FRR 10.x daemons refuse to start without CAP_SYS_ADMIN (netns support);
           # container-scoped, not --privileged; default seccomp/AppArmor stay on.
           "--cap-add", "NET_ADMIN", "--cap-add", "SYS_ADMIN", "--sysctl", "net.ipv4.ip_forward=1",
           "--sysctl", "net.ipv4.conf.all.rp_filter=0", "--sysctl", "net.ipv4.conf.default.rp_filter=0",
           "--network", net0, "--ip", ip0, "-v", f"{d}:/etc/frr", "-v", f"{HERE}:/lab:ro", FRR_IMAGE)
        for net, ip in rest:
            sh("docker", "network", "connect", "--ip", ip, net, f"{PREFIX}-{node}")
        lo = t["nodes"][node].get("loopback")
        if lo:
            dexec(node, "ip", "addr", "add", lo, "dev", "lo")
        if node.startswith("pop"):
            sh("docker", "exec", "-d", f"{PREFIX}-{node}", "python3", "/lab/whoami.py", node)
    m = t["monitor"]["members"]
    sh("docker", "run", "-d", "--name", f"{PREFIX}-collector", "--label", "project=pathwatch", "--network", MON_NET, "--ip", m["collector"],
       "-v", f"{GOBGP_BIN}:/gobgp:ro", "-v", f"{HERE}/gobgp:/etc/gobgp:ro", "--entrypoint", "/gobgp/gobgpd",
       FRR_IMAGE, "-f", "/etc/gobgp/gobgpd.toml", "--api-hosts", "0.0.0.0:50051", "-l", "info")
    wait_converged(t)


PKI = os.path.join(ROOT, ".cache", "task17", "pki")
KRILL_DATA = os.path.join(ROOT, ".cache", "task17", "krill-data")
TALS = os.path.join(ROOT, ".cache", "task17", "tals")
KRILL_IMAGE = "nlnetlabs/krill:v0.16.0"
ROUTINATOR_IMAGE = "nlnetlabs/routinator:v0.15.3"
KRILL_CONF = """# Test trust anchor + 'testbed' CA with an embedded repository (lab only).
data_dir = "/var/krill/data"
ip = "0.0.0.0"
port = 3000
https_mode = "existing"
service_uri = "https://krill:3000/"
# Inline table: the image entrypoint appends top-level keys to the end of this file.
testbed = { rrdp_base_uri = "https://krill:3000/rrdp/", rsync_jail = "rsync://krill/repo/", ta_aia = "rsync://krill/ta/ta.cer", ta_uri = "https://krill:3000/ta/ta.cer" }
"""


def https_get(ip, path, port=3000, name="krill"):
    import http.client
    import ssl
    ctx = ssl.create_default_context(cafile=os.path.join(PKI, "ca.pem"))
    conn = http.client.HTTPSConnection(ip, port, context=ctx, timeout=5)
    conn.sock = ctx.wrap_socket(__import__("socket").create_connection((ip, port), 5), server_hostname=name)
    conn.request("GET", path)
    r = conn.getresponse()
    return r.status, r.read()


def rpki(_):
    import secrets
    t = topo()
    m = t["monitor"]["members"]
    if not os.path.isdir(PKI):
        sh("go", "run", "./ingest/cmd/labpki", "-out", PKI, "-validity", "72h", "-extra-servers", "krill")
    shutil.rmtree(KRILL_DATA, ignore_errors=True)
    os.makedirs(os.path.join(KRILL_DATA, "ssl"))
    with open(os.path.join(KRILL_DATA, "krill.conf"), "w") as f:
        f.write(KRILL_CONF)
    shutil.copy(os.path.join(PKI, "krill.pem"), os.path.join(KRILL_DATA, "ssl", "cert.pem"))
    shutil.copy(os.path.join(PKI, "krill-key.pem"), os.path.join(KRILL_DATA, "ssl", "key.pem"))
    shutil.copy(os.path.join(PKI, "ca.pem"), os.path.join(KRILL_DATA, "ssl", "lab-ca.pem"))
    token = secrets.token_hex(16)
    for old in ("krill", "routinator"):
        sh("docker", "rm", "-f", f"{PREFIX}-{old}", check=False, quiet=True)
    sh("docker", "run", "-d", "--name", f"{PREFIX}-krill", "--label", "project=pathwatch", "--user", f"{os.getuid()}:{os.getgid()}",
       "--network", MON_NET, "--ip", m["krill"], "--network-alias", "krill", "-e", f"KRILL_ADMIN_TOKEN={token}",
       "-v", f"{KRILL_DATA}:/var/krill/data", KRILL_IMAGE, "krill", "--config", "/var/krill/data/krill.conf")
    end = time.monotonic() + 120
    while True:
        try:
            status, tal = https_get(m["krill"], "/ta/ta.tal")
            if status == 200:
                break
        except OSError:
            pass
        if time.monotonic() > end:
            raise SystemExit("Krill testbed TAL not served:\n" + sh("docker", "logs", "--tail", "30", f"{PREFIX}-krill", check=False).stderr)
        time.sleep(2)
    os.makedirs(TALS, exist_ok=True)
    with open(os.path.join(TALS, "pathwatch-testbed.tal"), "wb") as f:
        f.write(tal)
    roa = t["roa"]
    # krillc over HTTPS with the lab CA; the token comes from the container's environment.
    sh("docker", "exec", "-e", "SSL_CERT_FILE=/var/krill/data/ssl/lab-ca.pem", f"{PREFIX}-krill", "sh", "-c",
       'krillc -s https://krill:3000/ -t "$KRILL_ADMIN_TOKEN" roas update --ca testbed --add "$0"',
       f"{roa['prefix']}-{roa['max_length']} => {roa['asn']}")
    sh("docker", "run", "-d", "--name", f"{PREFIX}-routinator", "--label", "project=pathwatch", "--network", MON_NET, "--ip", m["routinator"],
       "-v", f"{TALS}:/tals:ro", "-v", f"{PKI}/ca.pem:/certs/ca.pem:ro", ROUTINATOR_IMAGE,
       "--no-rir-tals", "--extra-tals-dir", "/tals", "--disable-rsync", "--rrdp-root-cert", "/certs/ca.pem", "--allow-dubious-hosts",
       "server", "--rtr", "0.0.0.0:3323", "--http", "0.0.0.0:8323", "--refresh", "10")
    import urllib.request
    end = time.monotonic() + 180
    url = f"http://{m['routinator']}:8323/api/v1/validity/AS{roa['asn']}/{roa['prefix']}"
    while True:
        try:
            state = json.load(urllib.request.urlopen(url, timeout=3))["validated_route"]["validity"]["state"]
            if state == "valid":
                # GoBGP was started before Routinator; reconnect its RTR session now.
                gobgp("rpki", "server", m["routinator"], "reset")
                print(json.dumps({"rpki": "ready", "roa": roa, "state": state}))
                return
        except (OSError, KeyError, ValueError):
            state = None
        if time.monotonic() > end:
            raise SystemExit(f"Routinator never validated the ROA (last state {state}):\n"
                             + sh("docker", "logs", "--tail", "40", f"{PREFIX}-routinator", check=False).stderr)
        time.sleep(3)


def best_path(node, prefix):
    r = vtysh(node, f"show ip bgp {prefix} json", check=False, quiet=True)
    try:
        paths = json.loads(r.stdout).get("paths", [])
    except ValueError:
        return None
    for p in paths:
        if p.get("bestpath", {}).get("overall") or p.get("bestpath") is True:
            return p
    return None


def wait_converged(t, timeout=180):
    end = time.monotonic() + timeout
    clients = [n for n in t["nodes"] if n.startswith("c")]
    while time.monotonic() < end:
        if all(best_path(c, t["anycast"]) for c in clients) and collector_peers_up(t):
            print("converged", file=sys.stderr)
            return True
        time.sleep(2)
    raise SystemExit("BGP did not converge")


def gobgp(*args):
    return sh(os.path.join(ROOT, ".tools", "bin", "gobgp"), "-u", topo()["monitor"]["members"]["collector"], *args, quiet=True, check=False)


def collector_peers_up(t):
    r = gobgp("neighbor", "-j")
    try:
        peers = json.loads(r.stdout)
    except ValueError:
        return False
    states = [p.get("state", {}).get("session_state") for p in peers]
    return len(peers) == len(t["monitor"]["feeds"]) and all(s in (6, "ESTABLISHED", "established") for s in states)


def nearest(_):
    t = topo()
    out = {}
    service = t["anycast"].split("/")[0][:-1] + "1"
    for c in sorted(t["expected_nearest"]):
        src = t["nodes"][c]["loopback"].split("/")[0]
        tr = dexec(c, "traceroute", "-n", "-q", "1", "-w", "1", "-m", "8", "-s", src, service, check=False, quiet=True).stdout
        hops = [line.split()[1] for line in tr.splitlines()[1:] if len(line.split()) > 1 and line.split()[1] != "*"]
        who = dexec(c, "python3", "-c",
                    "import socket,sys;s=socket.create_connection((sys.argv[1],80),3,(sys.argv[2],0));"
                    "s.sendall(b'GET / HTTP/1.0\\r\\n\\r\\n');b=b''\n"
                    "while c:=s.recv(4096): b+=c\n"
                    "print(b.decode().split('\\r\\n\\r\\n',1)[1].strip())",
                    service, src, check=False, quiet=True).stdout.strip()
        bp = best_path(c, t["anycast"]) or {}
        out[c] = {"traceroute_hops": hops, "answered_by": who, "as_path": bp.get("aspath", {}).get("string")}
    print(json.dumps(out, indent=2))
    return out


INCIDENTS = {
    # c2 re-exports what it learned from its providers to everyone (a type-1 leak).
    "leak": ("c2", ["configure terminal", "router bgp 64702", "address-family ipv4 unicast",
                    "neighbor 10.117.7.3 route-map LEAK out", "neighbor 10.117.8.3 route-map LEAK out",
                    "neighbor 10.117.100.2 route-map LEAK out", "end", "clear ip bgp * soft out"],
                   ["configure terminal", "router bgp 64702", "address-family ipv4 unicast",
                    "neighbor 10.117.7.3 route-map OWN-ONLY out", "neighbor 10.117.8.3 route-map OWN-ONLY out",
                    "neighbor 10.117.100.2 route-map OWN-ONLY out", "end", "clear ip bgp * soft out"]),
    # c3 originates a more-specific /25 of the anycast prefix.
    "hijack": ("c3", ["configure terminal", "ip route 198.51.100.0/25 blackhole", "ip prefix-list OWN seq 10 permit 198.51.100.0/25",
                      "router bgp 64703", "address-family ipv4 unicast", "network 198.51.100.0/25", "end"],
                     ["configure terminal", "router bgp 64703", "address-family ipv4 unicast", "no network 198.51.100.0/25", "exit", "exit",
                      "no ip prefix-list OWN seq 10 permit 198.51.100.0/25", "no ip route 198.51.100.0/25 blackhole", "end"]),
}


def inject(args):
    node, on, off = INCIDENTS[args.incident]
    t0 = time.time()
    vtysh(node, *(off if args.withdraw else on))
    print(json.dumps({"incident": args.incident, "withdraw": args.withdraw, "applied_at": t0, "applied_done": time.time()}))


def status(_):
    t = topo()
    for node in t["nodes"]:
        print(node, vtysh(node, "show bgp summary", check=False, quiet=True).stdout)
    print(gobgp("neighbor").stdout)


def down(_):
    names = sh("docker", "ps", "-a", "--filter", f"name=^{PREFIX}-", "--format", "{{.Names}}").stdout.split()
    if names:
        sh("docker", "rm", "-f", *names)
    nets = sh("docker", "network", "ls", "--filter", f"name=^{PREFIX}-", "--format", "{{.Name}}").stdout.split()
    if nets:
        sh("docker", "network", "rm", *nets)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ["up", "rpki", "nearest", "status", "down"]:
        sub.add_parser(c)
    i = sub.add_parser("inject")
    i.add_argument("incident", choices=list(INCIDENTS))
    i.add_argument("--withdraw", action="store_true")
    a = ap.parse_args()
    {"up": up, "rpki": rpki, "nearest": nearest, "status": status, "down": down, "inject": inject}[a.cmd](a)


if __name__ == "__main__":
    main()
