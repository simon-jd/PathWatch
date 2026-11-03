import importlib.util
import ipaddress
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("pathchange", os.path.join(HERE, "..", "pathchange", "lab.py"))
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class PathChangeTopologyTests(unittest.TestCase):
    def test_addresses_are_inside_their_networks(self):
        for node, links in lab.NODES.items():
            for net, ip in links:
                self.assertIn(ipaddress.ip_address(ip), ipaddress.ip_network(lab.SUBNETS[net]), node)

    def test_route_next_hops_are_directly_connected(self):
        owned = {n: {net for net, _ in links} for n, links in lab.NODES.items()}
        address_net = {ip: net for links in lab.NODES.values() for net, ip in links}
        for node, routes in [*lab.ROUTES.items(), (lab.SWITCH[0], [lab.SWITCH[1]])]:
            for r in routes:
                via = r.split()[2]
                self.assertIn(address_net[via], owned[node], f"{node}: {r}")

    def test_switch_moves_second_hop_from_r2_to_r3(self):
        self.assertEqual(lab.ROUTES["r1"], ["10.219.4.0/24 via 10.219.2.3"])
        self.assertEqual(lab.SWITCH, ("r1", "10.219.4.0/24 via 10.219.3.4"))

    def test_probe_is_private_unprivileged_udp_and_never_reports(self):
        cfg = lab.probe_config()
        self.assertEqual(cfg["trace_mode"], "udp")
        self.assertEqual(cfg["ingest"], "127.0.0.1:1")
        self.assertTrue(ipaddress.ip_address(cfg["targets"][0]["Host"]).is_private)
        self.assertEqual(cfg["targets"][0]["Kinds"], ["tcp", "trace"])


if __name__ == "__main__":
    unittest.main()
