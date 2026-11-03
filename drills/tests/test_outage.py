import importlib.util,pathlib,tempfile,unittest
P=pathlib.Path(__file__).resolve().parents[1]/'outage.py'
class OutageTests(unittest.TestCase):
 def test_restore_after_sampling_failure(self):
  self.assertTrue(P.exists(),'outage controller missing')
  s=importlib.util.spec_from_file_location('outage',P);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
  calls=[]
  def control(cmd):
   calls.append(cmd)
   if cmd=='snapshot' and calls.count('snapshot')==2:raise RuntimeError('fixture broken sampler')
   return {'paused':cmd=='pause'}
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaises(RuntimeError):m.exercise(control,pathlib.Path(d),0.05,0.01)
  self.assertIn('pause',calls);self.assertEqual(calls[-1],'resume')
if __name__=='__main__':unittest.main()
