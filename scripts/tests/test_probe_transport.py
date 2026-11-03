"""Pure transport rendering and scoped home command tests, no Docker/SSH execution."""
import importlib.util
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[2]

class TransportTests(unittest.TestCase):
    def setUp(self):
        path=ROOT/'scripts/probe_transport.py';self.assertTrue(path.is_file(),'missing cloud/home transport adapter')
        spec=importlib.util.spec_from_file_location('probe_transport',path);self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
    def test_home_initiates_both_tunnels_and_limits_allowed_prefixes(self):
        for site,cloud in [('azure-a','10.10'),('azure-b','10.20')]:
            result=self.module.render(site,'8.8.4.4',['A'*43+'=']*2,['B'*43+'=']*2,['C'*43+'=']*2,['D'*43+'=']*2)
            self.assertIn('Endpoint = 8.8.4.4:51820',result['home']['pw-wg0.conf'])
            self.assertIn('MTU = 1380',result['home']['pw-wg0.conf'])
            self.assertIn(cloud+'.0.0/16',result['home']['pw-wg0.conf'])
            self.assertNotIn('Endpoint =',result['azure']['pw-wg0.conf'])
            self.assertNotIn('0.0.0.0/0,',result['azure']['pw-wg0.conf'])
            self.assertIn('10.30.0.0/16',result['azure']['pw-wg1.conf'])
            other='10.10' if site=='azure-b' else '10.20'
            self.assertIn('ip prefix-list OWN seq 10 permit '+other+'.0.0/16',result['home']['frr.conf'])
            self.assertIn('set src '+cloud+'.1.4',result['azure']['frr.conf'])
            self.assertIn(other+'.0.0/16',result['azure']['pw-wg0.conf'])
    def test_b_has_distinct_transit_as_and_home_router(self):
        result=self.module.render('azure-b','8.8.4.4',['A'*43+'=']*2,['B'*43+'=']*2,['C'*43+'=']*2,['D'*43+'=']*2)
        self.assertIn('Address = 10.255.0.10/30',result['home']['pw-wg0.conf'])
        self.assertIn('router bgp 65020',result['azure']['frr.conf'])
        self.assertIn('bgp router-id 10.30.0.3',result['home']['frr.conf'])
    def test_unknown_site_private_endpoint_and_malformed_key_refused(self):
        for site,ip,key in [('other','8.8.4.4','A'*43+'='),('azure-a','127.0.0.1','A'*43+'='),('azure-a','8.8.4.4','bad')]:
            with self.subTest(site=site,ip=ip),self.assertRaises(ValueError):self.module.render(site,ip,[key]*2,['B'*43+'=']*2,['C'*43+'=']*2,['D'*43+'=']*2)
    def test_home_bgp_routes_select_owned_observer_source_without_changing_peer_static(self):
        for site,source,own,other,peer in [('azure-a','10.30.0.2','10.10','10.20','10.30.0.3'),('azure-b','10.30.0.3','10.20','10.10','10.30.0.2')]:
            frr=self.module.render(site,'8.8.4.4',['A'*43+'=']*2,['B'*43+'=']*2,['C'*43+'=']*2,['D'*43+'=']*2)['home']['frr.conf']
            self.assertIn('route-map HOME_SOURCE permit 10\n match ip address prefix-list REMOTE\n set src '+source+'\nroute-map HOME_SOURCE deny 100\nip protocol bgp route-map HOME_SOURCE',frr)
            self.assertIn('ip prefix-list REMOTE seq 5 permit '+own+'.0.0/16',frr)
            self.assertNotIn('ip prefix-list REMOTE seq 10 permit '+other+'.0.0/16',frr)
            self.assertIn('ip route '+other+'.0.0/16 '+peer,frr)
if __name__=='__main__':unittest.main()
