"""BGP incident detector over the GoBGP collector (lab/bgp/topology.yml).

Rules, applied to every path in every feed's Adj-RIB-In:
  route-leak  the AS path is not valley-free (Gao), or a stub feed announces a
              prefix outside its own cone
  rpki        Routinator says the (origin, prefix) pair is invalid (ROV)
Each alert is printed once as a JSON line with the host wall-clock time.
Usage: python detect.py [--interval 0.25] [--seconds N]
"""
import argparse
import ipaddress
import json
import os
import subprocess
import sys
import time
import urllib.request

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def _rel(a, b, rel):
    """Relationship of b as seen from a: 'provider', 'peer', 'customer' or None."""
    sa, sb = rel.get(a, {}), rel.get(b, {})
    if b in sa.get("providers", []):
        return "provider"
    if a in sb.get("providers", []):
        return "customer"
    if b in sa.get("peers", []):
        return "peer"
    return None


def valley(path, rel):
    """First valley in an AS path (collector order, origin last).

    Returns (leaker, learned_from): the AS that exported a route it had learned
    from a provider or peer to another provider or peer. None if valley-free.
    """
    hops = [a for i, a in enumerate(path) if i == 0 or a != path[i - 1]]
    hops.reverse()  # origin first
    descending = False
    for i in range(len(hops) - 1):
        x, y = hops[i], hops[i + 1]
        r = _rel(x, y, rel)
        if r in ("provider", "peer") and descending:
            return (x, hops[i - 1])
        if r in ("peer", "customer"):
            descending = True
    return None


def outside_cone(prefix, feed_asn, rel):
    """A client (stub) AS may only announce its own prefix."""
    spec = rel.get(feed_asn, {})
    if spec.get("role") != "client":
        return False
    own = ipaddress.ip_network(spec["prefix"])
    return not ipaddress.ip_network(prefix).subnet_of(own)


def rov(origin, prefix, routinator, cache):
    key = (origin, prefix)
    if key not in cache:
        with urllib.request.urlopen(f"http://{routinator}:8323/api/v1/validity/AS{origin}/{prefix}", timeout=2) as r:
            cache[key] = json.load(r)["validated_route"]["validity"]["state"]
    return cache[key]


def adj_in(collector, neighbor):
    """[(prefix, [asns])] from `gobgp neighbor <ip> adj-in -j`."""
    r = subprocess.run([os.path.join(ROOT, ".tools", "bin", "gobgp"), "-u", collector, "neighbor", neighbor, "adj-in", "-j"],
                       capture_output=True, text=True)
    if r.returncode:
        return []
    out = []
    for prefix, paths in json.loads(r.stdout or "{}").items():
        for p in paths:
            asns = []
            for attr in p.get("attrs", []):
                for seg in attr.get("as_paths", []) if attr.get("type") == 2 else []:
                    asns += seg.get("asns", [])
            out.append((prefix, asns))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=0.25)
    ap.add_argument("--seconds", type=float, default=0)
    a = ap.parse_args()
    with open(os.path.join(HERE, "topology.yml")) as f:
        t = yaml.safe_load(f)
    rel = t["asns"]
    m = t["monitor"]
    feeds = {m["members"][n]: (n, t["nodes"][n]["asn"]) for n in m["feeds"]}
    seen, cache = set(), {}
    end = time.monotonic() + a.seconds if a.seconds else None
    print(json.dumps({"detector": "started", "at": time.time(), "feeds": sorted(n for n, _ in feeds.values())}), flush=True)
    while end is None or time.monotonic() < end:
        for ip, (name, asn) in feeds.items():
            for prefix, path in adj_in(m["members"]["collector"], ip):
                full = path if path[:1] == [asn] else [asn] + path
                alerts = []
                v = valley(full, rel)
                if v:
                    alerts.append(("route-leak", f"valley: AS{v[0]} re-exported a route learned from AS{v[1]}"))
                if outside_cone(prefix, asn, rel):
                    alerts.append(("route-leak", f"stub AS{asn} announced {prefix} outside its cone"))
                try:
                    if rov(full[-1], prefix, m["members"]["routinator"], cache) == "invalid":
                        alerts.append(("rpki-invalid", f"origin AS{full[-1]} not authorized for {prefix}"))
                except OSError as e:
                    alerts.append(("rpki-unavailable", str(e)))
                for kind, why in alerts:
                    key = (kind, name, prefix, tuple(full))
                    if key not in seen:
                        seen.add(key)
                        print(json.dumps({"at": time.time(), "kind": kind, "feed": name, "prefix": prefix,
                                          "as_path": full, "why": why}), flush=True)
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
