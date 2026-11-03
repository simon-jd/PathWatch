import configparser
import importlib.util
import tempfile
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[2]
def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
class ConfigTests(unittest.TestCase):
    def test_render(self):
        m=load('hybrid_config')
        for site in ('azure','home'):
            for profile in ('baseline','tuned','bfd'):
                frr=m.frr_config(site,profile)
                self.assertIn('network '+m.SITES[site]['prefix'],frr)
                self.assertNotIn('Null0',frr)
                self.assertIn('maximum-prefix 1',frr)
                self.assertIn('deny 0.0.0.0/0',frr)
                self.assertEqual(frr.count(' bfd profile PATHWATCH'),2 if profile=='bfd' else 0)
                for i in (0,1):
                    wg=m.wg_config(site,i,'A'*43+'=','B'*43+'=','8.8.8.8')
                    c=configparser.ConfigParser();c.read_string(wg)
                    self.assertEqual(c['Interface']['Table'],'off')
                    self.assertNotIn('0.0.0.0/0',c['Peer']['AllowedIPs'])
                    self.assertEqual(c['Peer']['PersistentKeepalive'],'25')
    def test_reject_bad_profile_site_endpoint_and_keys(self):
        m=load('hybrid_config')
        for args in [('gcp','baseline'),('home','fast')]:
            with self.assertRaises(ValueError):m.frr_config(*args)
        for endpoint in ('0.0.0.0','10.0.0.1','$(touch /tmp/no)'):
            with self.assertRaises(ValueError):m.wg_config('home',0,'A'*43+'=','B'*43+'=',endpoint)
        with self.assertRaises(ValueError):m.wg_config('home',0,'bad','bad','8.8.8.8')
if __name__=='__main__':unittest.main()
