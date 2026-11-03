import importlib.util,json,pathlib,tempfile,unittest
from unittest import mock
spec=importlib.util.spec_from_file_location('fixture',pathlib.Path(__file__).with_name('task14_core_rpc_fixture.py'));fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
class CleanupTests(unittest.TestCase):
 def test_first_allocation_cleanup_on_port_setup_failure(self):
  with tempfile.TemporaryDirectory() as d:
   root=pathlib.Path(d);(root/'.cache/task14b').mkdir(parents=True);out=root/'docs/evidence/raw/task14-core-rpc/failure.json'
   with mock.patch.object(fixture,'ROOT',root),mock.patch.object(fixture,'allocated',return_value=1),mock.patch.object(fixture.socket,'socket',side_effect=RuntimeError('injected first port allocation failure')),mock.patch('sys.argv',['fixture','--out',str(out),'--bin',str(root/'ingest')]):
    self.assertEqual(fixture.main(),1)
   result=json.loads(out.read_text());self.assertTrue(result['work_absent']);self.assertIn('injected',result['error']);self.assertEqual(result['commands'],[])
 def test_refuses_forecast_before_allocation(self):
  with tempfile.TemporaryDirectory() as d:
   root=pathlib.Path(d);out=root/'docs/evidence/raw/task14-core-rpc/refused.json'
   with mock.patch.object(fixture,'ROOT',root),mock.patch.object(fixture,'allocated',return_value=fixture.CAP),mock.patch('sys.argv',['fixture','--out',str(out),'--bin',str(root/'ingest')]):
    with self.assertRaisesRegex(ValueError,'not admitted'):fixture.main()
   self.assertFalse(out.exists())
if __name__=='__main__':unittest.main()
