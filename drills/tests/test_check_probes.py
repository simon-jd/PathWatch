import importlib.util,json,pathlib,tempfile,unittest
P=pathlib.Path(__file__).resolve().parents[1]/'check_probes.py'
class CheckTests(unittest.TestCase):
 def setUp(self):
  self.assertTrue(P.exists(),'dynamic probe checker missing')
  s=importlib.util.spec_from_file_location('check_probes',P);self.m=importlib.util.module_from_spec(s);s.loader.exec_module(self.m)
 def test_exact_physical_roster(self):
  self.m.check_result(2,['home','a'],{'home':'home-ubuntu','a':'azure-westus'})
  for count,ids in [(0,[]),(2,['home','fake']),(1,['home']),(3,['home','a','b'])]:
   with self.assertRaises(ValueError):self.m.check_result(count,ids,{'home':'home-ubuntu','a':'azure-westus'})
 def test_reject_duplicate_physical_site(self):
  with self.assertRaises(ValueError):self.m.check_result(2,['home','fake'],{'home':'home-ubuntu','fake':'home-ubuntu'})
 def test_transport(self):
  self.m.validate_url('http://127.0.0.1:8123')
  self.m.validate_url('https://ingest.example:8443')
  for u in ['http://localhost:8123','http://10.1.1.1:8123','https://user:pass@a:443','https://a:443/?query=DROP']:
   with self.assertRaises(ValueError):self.m.validate_url(u)
if __name__=='__main__':unittest.main()
