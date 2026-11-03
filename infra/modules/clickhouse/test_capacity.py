import importlib.util,json,pathlib,tempfile,unittest
HERE=pathlib.Path(__file__).parent
class CapacityTests(unittest.TestCase):
 def module(self):
  p=HERE/'capacity.py'
  self.assertTrue(p.exists(),'capacity preflight missing')
  s=importlib.util.spec_from_file_location('capacity',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
 def test_jitter_flush_full_batches_restart_and_allocated_forecast(self):
  m=self.module();v=m.forecast()
  self.assertEqual(v['rounds'],57635)
  self.assertEqual(v['results'],864525)
  self.assertEqual(v['records'],110505)
  self.assertGreater(v['journal_allocated_bytes'],v['journal_logical_bytes']+400_000_000)
  self.assertLess(v['journal_logical_bytes'],966367641)
  self.assertLess(v['records'],117964)
 def test_capacity_boundary_existing_state_and_no_fake_elapsed_claim(self):
  m=self.module();f=m.forecast();reserve=f['additional_disk_bytes']
  v=m.admission(11_600_000_000-reserve,0,0,0)
  self.assertTrue(v['resources_fit']);self.assertFalse(v['long_deployment_ready'])
  self.assertFalse(m.admission(11_600_000_001-reserve,0,0,0)['resources_fit'])
  self.assertFalse(m.admission(0,117964-f['records'],0,0)['resources_fit'])
  self.assertFalse(m.admission(0,0,966367641-f['journal_logical_bytes'],0)['resources_fit'])
  self.assertFalse(m.admission(0,0,0,0,block_size=8192)['resources_fit'])
 def test_runtime_contract_uses_same_limits(self):
  m=self.module()
  self.assertEqual(m.runtime_reason(0,110505,711000000,0,0,0,0),'')
  self.assertEqual(m.runtime_reason(11_699_999_999,0,0,0,0,0,0),'')
  for index,value in [(0,11_700_000_000),(1,117964),(2,966367641),(3,64<<20),(4,768<<20),(5,384<<20),(6,8_000_000_001)]:
   values=[0]*7;values[index]=value;self.assertTrue(m.runtime_reason(*values))

class ScanTests(unittest.TestCase):
 def test_only_owned_disappearing_files_retry_and_errors_remain_visible(self):
  from unittest.mock import patch
  from types import SimpleNamespace
  s=importlib.util.spec_from_file_location('c3home',HERE/'home.py');home=importlib.util.module_from_spec(s);s.loader.exec_module(home)
  self.assertTrue(hasattr(home,'allocated_directory'),'bounded shared allocated scan missing')
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp);journal=root/'journal';journal.mkdir()
   vanished=SimpleNamespace(returncode=1,stdout=f'4096\t{root}\n',stderr=f"du: cannot access '{journal}/.pw-tmp-1': No such file or directory\n")
   stable=SimpleNamespace(returncode=0,stdout=f'8192\t{root}\n',stderr='')
   audit=[]
   with patch.object(home.subprocess,'run',side_effect=[vanished,stable]):self.assertEqual(home.allocated_directory(root,[journal],audit),8192)
   self.assertEqual(len(audit),1);self.assertIn('.pw-tmp-1',audit[0]['stderr'])
   for message in [f"du: cannot access '{root}/foreign': No such file or directory\n",f"du: cannot read directory '{journal}': Permission denied\n"]:
    with patch.object(home.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='',stderr=message)) as run:
     with self.assertRaises(RuntimeError):home.allocated_directory(root,[journal],[])
     self.assertEqual(run.call_count,1)
   with patch.object(home.subprocess,'run',return_value=vanished) as run:
    with self.assertRaises(RuntimeError):home.allocated_directory(root,[journal],[])
    self.assertEqual(run.call_count,5)

class AtomicScanTests(unittest.TestCase):
 def test_exact_owned_atomic_exclusion_counts_finals_and_nested_files(self):
  s=importlib.util.spec_from_file_location('c3home',HERE/'home.py');home=importlib.util.module_from_spec(s);s.loader.exec_module(home)
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp);journal=root/'journal';journal.mkdir();nested=journal/'nested';nested.mkdir()
   for p in [journal/'.pw-tmp-one',journal/'final.json',nested/'.pw-tmp-foreign']:
    p.write_bytes(b'x'*8192)
   full=home.allocated_directory(root)
   audit=[]
   value=home.allocated_directory(root,atomic=[(journal,'journal')],opened=True,audit=audit)
   self.assertEqual(value,full-8192+(24<<20)+4096)
   self.assertEqual(audit[0]['atomic_reserved_bytes'],(24<<20)+4096)
   with self.assertRaises(ValueError):home.allocated_directory(root,atomic=[(journal,'journal')])
   with self.assertRaises(ValueError):home.allocated_directory(root,atomic=[(root.parent,'journal')],opened=True)
 def test_mixed_error_is_not_hidden_by_owned_enoent(self):
  from unittest.mock import patch
  from types import SimpleNamespace
  s=importlib.util.spec_from_file_location('c3home',HERE/'home.py');home=importlib.util.module_from_spec(s);s.loader.exec_module(home)
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp)
   error=f"du: cannot access '{root}/vanished': No such file or directory\ndu: cannot read directory '{root}/locked': Permission denied\n"
   with patch.object(home.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='4096',stderr=error)) as run:
    with self.assertRaises(RuntimeError):home.allocated_directory(root,[root],[])
    self.assertEqual(run.call_count,1)

class ResumeAdmissionTests(unittest.TestCase):
 def test_existing_real_journal_and_remaining_deadline_do_not_reserve_another35days(self):
  import capacity
  with tempfile.TemporaryDirectory() as tmp:
   base=pathlib.Path(tmp);(base/'journal').mkdir()
   (base/'journal'/'owner.json').write_text('{}');(base/'journal'/'one.json').write_text('x'*4097)
   records,logical,blocks=capacity.inspect_state(base)
   self.assertEqual(records,1);self.assertEqual(logical,4099);self.assertGreaterEqual(blocks,8192)
   full=capacity.admission(2_800_000_000,records,logical,blocks)
   resumed=capacity.admission(2_800_000_000,records,logical,blocks,seconds=[86400,86400,0],restarts=0,existing_reserves={'clickhouse':100<<20})
   self.assertLess(resumed['projected_allocated_bytes'],full['projected_allocated_bytes'])
   self.assertLess(resumed['records'],4000)
   self.assertFalse(resumed['physical_elapsed_verified']);self.assertFalse(resumed['slo_achieved'])
 def test_plan_identity_normalizes_json_and_requires_fixed_dates(self):
  import capacity
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as tmp:
   base=pathlib.Path(tmp);observers=[]
   configs={}
   for site,days in [('home-ubuntu',35),('azure-a',35),('azure-b',2)]:
    path=base/(site+'.json');cfg={'probe_id':site,'capacity_profile':capacity.PROFILE,'capacity_start_utc':'2026-11-01T00:00:00Z','capacity_deadline_utc':('2026-11-03T00:00:00Z' if days==2 else '2026-12-06T00:00:00Z')};path.write_text(json.dumps(cfg));configs[str(path)]=cfg
    observers.append({'site':site,'days':days,'config':str(path),'start_utc':'2026-11-01T00:00:00Z'})
   plan=base/'plan.json';plan.write_text(json.dumps({'campaign_id':'test','observers':observers}))
   with patch.object(capacity.subprocess,'check_output',side_effect=lambda argv,**kw:json.dumps(configs[argv[2]])):
    first=capacity.plan_configs(plan,pathlib.Path('/reviewed/probe'))
    (base/'azure-a.json').write_text(json.dumps(configs[str(base/'azure-a.json')],indent=2))
    second=capacity.plan_configs(plan,pathlib.Path('/reviewed/probe'))
    self.assertEqual(first[2],second[2]);self.assertNotEqual(first[0],second[0])
    observers[0]['start_utc']='unspecified';plan.write_text(json.dumps({'campaign_id':'test','observers':observers}))
    with self.assertRaises(ValueError):capacity.plan_configs(plan,pathlib.Path('/reviewed/probe'))
 def test_no_evidence_or_missing_paths_never_passes_envelope(self):
  import capacity
  for evidence in [{},{'volume':'full','exit':0,'container_absent':True,'synthetic_directory_removed':True,'port_closed':True}]:
   with self.assertRaises(ValueError):capacity.verify_envelope(evidence,pathlib.Path('/nonexistent'))

class ReviewedProfileArtifactTests(unittest.TestCase):
 def test_namespace_capacity_preflight_cannot_execute_unreviewed_probe(self):
  import hashlib,home
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp);binaries=root/'.cache'/'task10c3-bin';binaries.mkdir(parents=True)
   (binaries/'ingest').write_bytes(b'ingest');(binaries/'probe').write_bytes(b'changed-probe')
   cfg={'binary_dir':str(binaries),'binary_sha256':{'ingest':hashlib.sha256(b'ingest').hexdigest()},'namespace_observer':{'probe_binary':str(binaries/'probe')},'capacity_plan':'plan.json'}
   with patch.object(home,'ROOT',root):
    with self.assertRaises(ValueError):home.reviewed_binaries(cfg)
    cfg['binary_sha256']['probe']=hashlib.sha256(b'changed-probe').hexdigest()
    self.assertEqual(home.reviewed_binaries(cfg),binaries)


class PrometheusProfileTests(unittest.TestCase):
 def test_fixed_scrapes_limit_series_and_preserve_legacy_text(self):
  import capacity
  self.assertTrue(hasattr(capacity,'prometheus_config'),'bounded Prometheus configuration missing')
  original=(HERE.parents[2]/'slo/prometheus.yaml').read_text()
  bounded=capacity.prometheus_config(original)
  self.assertEqual(bounded.count('sample_limit: 16'),3)
  self.assertNotIn('sample_limit',original)
  self.assertEqual(capacity.PROM_CAP,384<<20)
  with self.assertRaises(ValueError):capacity.prometheus_config(original+'  - job_name: extra\n')

class PrometheusEvidenceTests(unittest.TestCase):
 def test_missing_or_incomplete_prometheus_history_cannot_open_gate(self):
  import capacity
  self.assertTrue(hasattr(capacity,'verify_prometheus_envelope'),'Prometheus envelope gate missing')
  for evidence in [{},{'days':35,'series':128,'exit':0,'accepted_samples':1}]:
   with self.assertRaises(ValueError):capacity.verify_prometheus_envelope(evidence,pathlib.Path('/no-binaries'))

 def test_real_exporter_replay_reports_formatter_drift(self):
  import capacity_prometheus_fixture as fixture
  texts,proof=fixture.historical_metrics(HERE.parents[2])
  self.assertEqual(proof['sample_counts'],{'observer':12,'ingest':9,'probe':9})
  self.assertEqual(texts,proof['complete_texts'])
  for v in proof['formatters'].values():
   self.assertEqual(v['unchanged'],v['current_sha256']==v['base_sha256'])

class CommittedSpoolSnapshotTests(unittest.TestCase):
 def test_ack_removing_batch_does_not_break_committed_usage_snapshot(self):
  import home
  self.assertTrue(hasattr(home,'committed_spool'),'profile spool snapshot must use atomic manifest')
  with tempfile.TemporaryDirectory() as tmp:
   base=pathlib.Path(tmp);data={'Version':1,'Dropped':0,'Entries':[{'ID':'a'*64,'Size':1234,'SHA256':'b'*64}]}
   raw=json.dumps(data).encode();(base/'manifest.json').write_bytes(raw)
   # ACK may unlink this former batch after a snapshot reads the old manifest.
   (base/('a'*64+'.batch')).write_bytes(b'x');(base/('a'*64+'.batch')).unlink()
   got,total=home.committed_spool(base)
   self.assertEqual(got,data);self.assertEqual(total,1234+len(raw))

class SpoolAdmissionTests(unittest.TestCase):
 def write_spool(self,base,size=1000,dropped=0):
  import hashlib
  base.mkdir(exist_ok=True);(base/'.lock').touch(mode=0o600)
  entries=[];remaining=size
  while remaining:
   n=min(16<<20,remaining);ident=f'{len(entries):064x}'
   raw=json.dumps({'ID':ident,'Results':[{'Err':'short'}]}).encode()
   raw+=b' '*(n-len(raw));(base/(ident+'.batch')).write_bytes(raw);(base/(ident+'.batch')).chmod(0o600)
   entries.append({'ID':ident,'Size':len(raw),'SHA256':hashlib.sha256(raw).hexdigest()});remaining-=n
  (base/'manifest.json').write_text(json.dumps({'Version':1,'Dropped':dropped,'Entries':entries}));(base/'manifest.json').chmod(0o600)
 def test_new_resume_actual_bytes_limit_and_readonly(self):
  import capacity
  self.assertTrue(hasattr(capacity,'inspect_spool'),'strict existing spool admission missing')
  with tempfile.TemporaryDirectory() as tmp:
   base=pathlib.Path(tmp)/'spool'
   self.assertEqual(capacity.inspect_spool(base,False)['committed_bytes'],0)
   with self.assertRaises(ValueError):capacity.inspect_spool(base,True)
   self.write_spool(base,0);self.assertEqual(capacity.inspect_spool(base,False)['committed_bytes'],0)
   self.write_spool(base,1000)
   before={p.name:p.read_bytes() for p in base.iterdir()}
   with self.assertRaises(ValueError):capacity.inspect_spool(base,False)
   self.assertEqual(capacity.inspect_spool(base,True)['committed_bytes'],1000)
   self.assertEqual(before,{p.name:p.read_bytes() for p in base.iterdir()})
  for extra in [0,1]:
   with tempfile.TemporaryDirectory() as tmp:
    base=pathlib.Path(tmp)/'spool';limit=(64<<20)-34025472
    self.write_spool(base,limit+extra)
    # Even logical equality lacks room for existing directory/manifest blocks.
    with self.assertRaises(ValueError):capacity.inspect_spool(base,True)
 def test_allocated_4k_boundary_empty_manifest_and_readonly(self):
  import capacity,home
  limit=(64<<20)-34025472
  with tempfile.TemporaryDirectory() as tmp:
   base=pathlib.Path(tmp)/'spool';self.write_spool(base,0)
   empty=home.allocated_directory(base)
   self.assertGreater(empty,0)
   proof=capacity.inspect_spool(base,False)
   self.assertEqual(proof['allocated_bytes'],empty)
   self.write_spool(base,limit-empty)
   self.assertEqual(home.allocated_directory(base),limit)
   before={p.name:p.read_bytes() for p in base.iterdir()}
   self.assertEqual(capacity.inspect_spool(base,True)['allocated_bytes'],limit)
   self.assertEqual(before,{p.name:p.read_bytes() for p in base.iterdir()})
   self.write_spool(base,limit-empty+1)
   self.assertGreater(home.allocated_directory(base),limit)
   with self.assertRaises(ValueError):capacity.inspect_spool(base,True)
 def test_drop_corrupt_unknown_hash_and_live_owner_rejected(self):
  import capacity,fcntl
  self.assertTrue(hasattr(capacity,'inspect_spool'),'strict existing spool admission missing')
  for fault in ['drop','json','unknown','size','missing','owner']:
   with tempfile.TemporaryDirectory() as tmp:
    base=pathlib.Path(tmp)/'spool';self.write_spool(base,1000,1 if fault=='drop' else 0)
    if fault=='json':(base/'manifest.json').write_text('{')
    if fault=='unknown':(base/'foreign').touch()
    if fault=='size':(base/('0'*64+'.batch')).write_bytes(b'bad')
    if fault=='missing':(base/('0'*64+'.batch')).unlink()
    lock=None
    if fault=='owner':lock=(base/'.lock').open();fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
     with self.assertRaises((ValueError,OSError)):capacity.inspect_spool(base,True)
    finally:
     if lock:lock.close()

if __name__=='__main__':unittest.main()
