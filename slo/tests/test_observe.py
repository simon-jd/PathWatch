import importlib.util, pathlib, tempfile, unittest
P=pathlib.Path(__file__).resolve().parents[1]/'observe.py'
class ObserverTests(unittest.TestCase):
 def setUp(self):
  self.assertTrue(P.exists(), 'SLO observer implementation missing')
  spec=importlib.util.spec_from_file_location('observe',P);self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
 def new(self,roster=None,now=300):return self.m.Observer(pathlib.Path(self.tmp.name)/'state.json', roster or {'home':'home-ubuntu','azure-a':'azure-westus'},now)
 def test_closed_window_roster_and_late_backfill(self):
  o=self.new();o.tick(315,{'windows':{'300':['home']}});o.tick(600,None)
  self.assertEqual((o.state['windows_total'],o.state['windows_bad']),(1,0))
  o.tick(900,{'windows':{'600':['home']}})
  self.assertEqual((o.state['windows_total'],o.state['windows_bad']),(2,1),'late receipt cannot repair closed window')
 def test_outage_restart_missing_slots_fail_closed(self):
  o=self.new();o.tick(315,None);o.tick(330,None)
  resumed=self.new(now=930);resumed.tick(930,None)
  self.assertEqual(resumed.state['windows_total'],2)
  self.assertEqual(resumed.state['windows_bad'],2)
  self.assertEqual(resumed.state['api_total'],42)
  self.assertEqual(resumed.state['api_bad'],42)
 def test_home_only_nonvacuous_and_roster_immutable(self):
  o=self.new({'home':'home-ubuntu'});o.tick(600,{'windows':{}})
  self.assertEqual(o.required,1);self.assertEqual(o.state['windows_bad'],1)
  with self.assertRaises(ValueError):self.new({'home':'home-ubuntu','fake':'home-ubuntu'})
  with self.assertRaises(ValueError):self.new({'else':'azure-a'})
 def test_three_physical_sites_need_two_fresh(self):
  o=self.new({'home':'home-ubuntu','a':'azure-a','b':'azure-b'})
  o.tick(315,{'windows':{'300':['home','a']}});o.tick(600,None)
  self.assertEqual(o.state['windows_bad'],0)
  o.tick(615,{'windows':{'600':['home']}});o.tick(900,None)
  self.assertEqual(o.state['windows_bad'],1)
 def test_unknown_observer_fails_request(self):
  o=self.new();o.tick(315,{'windows':{'300':['invented']}})
  self.assertEqual(o.state['api_bad'],1)
  self.assertNotIn('invented',str(o.state['fresh']))
if __name__=='__main__':unittest.main()
