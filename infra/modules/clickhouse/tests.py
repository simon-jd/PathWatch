import importlib.util,pathlib,tempfile,unittest,hashlib
P=pathlib.Path(__file__).with_name('home.py')
class HomeTests(unittest.TestCase):
 def setUp(self):
  self.assertTrue(P.exists(),'home lifecycle implementation missing')
  s=importlib.util.spec_from_file_location('home',P);self.m=importlib.util.module_from_spec(s);s.loader.exec_module(self.m)
 def test_empty_manifest_and_real_backlog_snapshot(self):
  with tempfile.TemporaryDirectory() as d:
   root=pathlib.Path(d)
   self.assertEqual(self.m.spool_batches(root,{'Entries':None}),{})
   (root/'batch1.batch').write_text('{"Results":[{"ProbeID":"home"},{"ProbeID":"home"}]}')
   self.assertEqual(self.m.spool_batches(root,{'Entries':[{'ID':'batch1'}]}),{'batch1':{'probe_id':'home','results':2}})
 def test_managed_probe_paths_and_physical_identity(self):
  root=self.m.ROOT
  cfg={'spool_dir':str(root/'.cache/task10-home/spool'),'probe_metrics_port':19464}
  probe={'probe_id':'home','spool_dir':'.cache/task10-home/spool','metrics_addr':'127.0.0.1:19464'}
  self.m.validate_probe(cfg,probe,{'home':'home-ubuntu'})
  with self.assertRaises(ValueError):self.m.validate_probe(cfg,{**probe,'spool_dir':'.cache/other'}, {'home':'home-ubuntu'})
  with self.assertRaises(ValueError):self.m.validate_probe(cfg,{**probe,'probe_id':'azure-a'}, {'azure-a':'azure'})
 def test_watermarks(self):
  self.assertEqual(self.m.capacity_reason(11_699_999_999,40000,1000000,10000),'')
  self.assertTrue(self.m.capacity_reason(0,0,0,0,256*1024*1024))
  # The disk share is 12 GB since 2026-10-27; the hard stop keeps a 300 MB reserve.
  self.assertEqual(self.m.DISK_SHARE,12_000_000_000)
  for args in [(11_700_000_000,0,0,0),(0,52428,0,0),(0,0,214748364,0),(0,0,0,214748364)]:
   self.assertTrue(self.m.capacity_reason(*args))
 def test_tls_remote_config(self):
  self.m.validate_ingest({'listen':'127.0.0.1:19443','development_cleartext':True})
  for cfg in [{'listen':'0.0.0.0:9443','development_cleartext':True},{'listen':'0.0.0.0:9443'}]:
   with self.assertRaises(ValueError):self.m.validate_ingest(cfg)
  self.m.validate_ingest({'listen':'10.30.0.1:9443','tls_cert':'server.pem','tls_key':'server.key'})
 def test_namespace_probe_keeps_one_physical_identity_and_metrics(self):
  root=self.m.ROOT
  cfg={'spool_dir':str(root/'.cache/task10c2-test/spool'),'probe_metrics_port':9464,'namespace_observer':{'parent_id':'a'*64}}
  probe={'probe_id':'home','spool_dir':cfg['spool_dir'],'metrics_addr':'10.30.0.2:9464'}
  self.m.validate_probe(cfg,probe,{'home':'home-ubuntu'})
  for key,value in [('probe_id','home-copy'),('metrics_addr','127.0.0.1:9464'),('metrics_addr','0.0.0.0:9464')]:
   with self.subTest(key=key),self.assertRaises(ValueError):self.m.validate_probe(cfg,{**probe,key:value},{'home':'home-ubuntu'})
 def test_stale_readiness_never_accepts_old_certificate(self):
  self.assertTrue(hasattr(self.m,'wait_echo_ready'),'generation-aware readiness missing')
  with tempfile.TemporaryDirectory() as tmp:
   base=pathlib.Path(tmp);(base/'ready.json').write_text('{"token":"old-generation"}')
   with self.assertRaises(TimeoutError):self.m.wait_echo_ready(base/'old.pem',base/'ready.json','new-generation','127.0.0.1:18443',lambda:True,timeout=.02)
 def test_reviewed_artifact_directory_and_hashes_are_required(self):
  self.assertTrue(hasattr(self.m,'reviewed_binaries'),'reviewed lifecycle artifacts missing')
  with tempfile.TemporaryDirectory(dir=self.m.ROOT/'.cache') as tmp:
   base=pathlib.Path(tmp);hashes={}
   for kind in ['ingest','probe','echo']:
    (base/kind).write_text(kind);hashes[kind]=hashlib.sha256(kind.encode()).hexdigest()
   cfg={'binary_dir':str(base),'binary_sha256':hashes}
   self.assertEqual(self.m.reviewed_binaries(cfg),base)
   for change in [{'binary_sha256':{}},{'binary_dir':'/tmp'}]:
    with self.assertRaises(ValueError):self.m.reviewed_binaries({**cfg,**change})
   (base/'probe').write_text('replaced')
   with self.assertRaisesRegex(ValueError,'hash'):self.m.reviewed_binaries(cfg)
if __name__=='__main__':unittest.main()
