"""Task 17 lab tests.

Offline: FRR configs agree with lab/bgp/topology.yml (addresses, ASNs, policy hooks).
Live (PATHWATCH_BGP_LAB=1, after `make lab-up`): test_anycast_nearest_pop.
"""
import os
import re
import sys
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BGP = os.path.join(ROOT, "lab", "bgp")
sys.path.insert(0, BGP)


def topo():
    with open(os.path.join(BGP, "topology.yml")) as f:
        return yaml.safe_load(f)


def conf(node):
    with open(os.path.join(BGP, "frr", node + ".conf")) as f:
        return f.read()


class ConfigMatchesTopology(unittest.TestCase):
    def test_every_link_is_a_bgp_session_with_the_right_asns(self):
        t = topo()
        for a, aip, b, bip in t["links"].values():
            for (x, xip), (y, yip) in (((a, aip), (b, bip)), ((b, bip), (a, aip))):
                text = conf(x)
                self.assertIn(f"router bgp {t['nodes'][x]['asn']}", text, x)
                self.assertIn(f"neighbor {yip} remote-as {t['nodes'][y]['asn']}", text, f"{x} -> {y}")

    def test_collector_feeds_peer_with_the_collector_and_nothing_else_does(self):
        t = topo()
        m = t["monitor"]
        for node in t["nodes"]:
            peers = f"neighbor {m['members']['collector']} remote-as {m['collector_asn']}" in conf(node)
            self.assertEqual(peers, node in m["feeds"], node)
        with open(os.path.join(BGP, "gobgp", "gobgpd.toml")) as f:
            gobgp = f.read()
        for node in m["feeds"]:
            self.assertIn(f'neighbor-address = "{m["members"][node]}"', gobgp)

    def test_every_ebgp_neighbor_has_policy_both_ways(self):
        t = topo()
        for node, spec in t["nodes"].items():
            text = conf(node)
            for ip, asn in re.findall(r"neighbor (\S+) remote-as (\d+)", text):
                if int(asn) == spec["asn"]:
                    continue
                self.assertRegex(text, rf"neighbor {re.escape(ip)} route-map \S+ in", f"{node} {ip}")
                self.assertRegex(text, rf"neighbor {re.escape(ip)} route-map \S+ out", f"{node} {ip}")

    def test_clients_and_pops_export_only_their_own_prefix(self):
        t = topo()
        for node, spec in t["nodes"].items():
            if node.startswith("c") or node.startswith("pop"):
                outs = set(re.findall(r"route-map (\S+) out", conf(node)))
                self.assertLessEqual(outs, {"OWN-ONLY", "ANYCAST-OUT", "ANYCAST-OUT-BACKUP"}, node)


class LeakDetector(unittest.TestCase):
    def test_valley_free_paths(self):
        from detect import valley
        rel = topo()["asns"]
        self.assertIsNone(valley([64601, 64500], rel))                  # provider -> customer
        self.assertIsNone(valley([64602, 64601, 64701], rel))           # peer, then down
        self.assertIsNone(valley([64702], rel))                         # own prefix
        self.assertIsNone(valley([64602, 64500, 64500, 64500], rel))     # prepending collapses
        # c2 re-exports its provider AS64601's route to its other provider AS64602.
        self.assertEqual(valley([64602, 64702, 64601, 64500], rel), (64702, 64601))
        self.assertEqual(valley([64602, 64702, 64601, 64701], rel), (64702, 64601))

    def test_stub_feed_announcing_foreign_prefix(self):
        from detect import outside_cone
        rel = topo()["asns"]
        self.assertFalse(outside_cone("192.0.2.64/26", 64702, rel))
        self.assertTrue(outside_cone("198.51.100.0/24", 64702, rel))
        self.assertFalse(outside_cone("198.51.100.0/24", 64602, rel))   # transits carry others' prefixes


@unittest.skipUnless(os.environ.get("PATHWATCH_BGP_LAB") == "1", "set PATHWATCH_BGP_LAB=1 after make lab-up")
class LiveLab(unittest.TestCase):
    def test_anycast_nearest_pop(self):
        import lab
        t = topo()
        got = lab.nearest(None)
        expected_transit = {"c1": "10.117.6.3", "c2": "10.117.7.3", "c3": "10.117.9.3"}
        for client, pop in t["expected_nearest"].items():
            self.assertEqual(got[client]["answered_by"], pop, got[client])
            hops = got[client]["traceroute_hops"]
            self.assertEqual(hops[0], expected_transit[client], hops)
            self.assertEqual(hops[-1], "198.51.100.1", hops)


if __name__ == "__main__":
    unittest.main()
