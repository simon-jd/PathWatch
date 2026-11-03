"""Detect ignored anomaly configuration, capacity bypass and failed startup."""
import pathlib,tempfile,unittest
import json,os,signal,socket,subprocess,sys,time
from unittest.mock import patch,Mock
import home,capacity

class WiringTests(unittest.TestCase):
 def test_invalid_or_ambiguous_anomaly_is_rejected_before_runtime(self):
  base={'listen':'127.0.0.1:19443','development_cleartext':True}
  invalid=[{'anomaly_enabled':False},{'anomaly_enable':True},{'anomaly':True},{'anomaly':{'enabled':'false'}},{'anomaly':{'enabled':1}},{'anomaly':{'enabled':True,'warmup':True}},{'anomaly':{'enabled':True,'h':float('nan')}},{'anomaly':{'enabled':True,'alertmanager_url':'http://example.com:9093'}},{'anomaly':{'enabled':False,'warmup':2}},{'anomaly':{'enabled':True,'unknown':0}}]
  for change in invalid:
   with self.subTest(change=change),self.assertRaises(ValueError):home.validate_ingest({**base,**change})
 def test_enabled_command_and_default_disabled(self):
  self.assertTrue(hasattr(home,'ingest_args'),'home needs a shared validated command builder')
  cfg={'listen':'127.0.0.1:19443','development_cleartext':True,'clickhouse_port':18123,'metrics_port':19091,'anomaly':{'enabled':True,'warmup':2,'alertmanager_url':'http://127.0.0.1:19993'}}
  argv=home.ingest_args(pathlib.Path('/owned'),cfg,pathlib.Path('/bin'))
  self.assertIn('-anomaly-enable',argv)
  self.assertEqual(argv[argv.index('-anomaly-warmup')+1],'2')
  self.assertEqual(argv[argv.index('-alertmanager-url')+1],'http://127.0.0.1:19993')
  del cfg['anomaly'];self.assertNotIn('-anomaly-enable',home.ingest_args(pathlib.Path('/owned'),cfg,pathlib.Path('/bin')))
 def test_capacity_same_parser_never_admits_enabled_or_alias(self):
  for change in ({'anomaly':{'enabled':True}},{'anomaly_enable':False},{'anomaly':{'enabled':'false'}}):
   with self.subTest(change=change),self.assertRaisesRegex(ValueError,'anomaly'):
    capacity.preflight(pathlib.Path('/absent'),{'prometheus_port':19090,**change},pathlib.Path('/absent'),0)
 def test_failed_ingest_startup_never_reports_ready(self):
  self.assertTrue(hasattr(home,'wait_ingest_ready'))
  proc=Mock();proc.poll.return_value=1
  with self.assertRaisesRegex(RuntimeError,'ingest exited'):home.wait_ingest_ready(proc,19091,True,timeout=.1)
  proc.poll.return_value=None
  with patch.object(home,'fetch',side_effect=['ok','pathwatch_ingest_batches_total 0\n']):
   with self.assertRaisesRegex(ValueError,'anomaly metrics'):home.wait_ingest_ready(proc,19091,True,timeout=.1)

class EventCapacityTests(unittest.TestCase):
 def test_boundary_history_restart_bytes_and_no_capacity_claim(self):
  self.assertTrue(hasattr(capacity,'anomaly_forecast'))
  f=capacity.anomaly_forecast()
  self.assertEqual(f['logical_events'],311085) # (10081+10081+577)*15, no warmup credit
  self.assertEqual(f['max_paths'],45)
  self.assertGreaterEqual(f['state_atomic_bytes'],2*(4<<20))
  self.assertGreater(f['physical_rows'],f['logical_events'])
  self.assertFalse(f['combined_envelope_verified'])
  self.assertEqual(f['additional_exporter_series'],3)
  history=capacity.anomaly_forecast(retained_events=7,retained_windows=2,restarts=0)
  self.assertEqual(history['logical_events'],311094)
  self.assertGreaterEqual(history['physical_rows'],history['logical_events'])
  for kwargs in ({'retained_events':-1},{'retained_windows':True},{'restarts':33}):
   with self.assertRaises(ValueError):capacity.anomaly_forecast(**kwargs)

@unittest.skipUnless(os.environ.get('PW_C5_SIGNAL_TEST')=='1','explicit owned native signal fixture only')
class NativeSignalTests(unittest.TestCase):
 def test_sigterm_cleans_actual_owned_runtime(self):
  # This catches default SIGTERM exiting before the fixture's finally block.
  out=home.ROOT/os.environ['PW_C5_SIGNAL_OUT'];port=int(os.environ['PW_C5_SIGNAL_PORT'])
  receipt=out.with_suffix('.signal.json');proc=None;runtime=None;fallback=[]
  self.assertFalse(out.exists())
  try:
   args=[sys.executable,str(home.ROOT/'infra/modules/clickhouse/live_anomaly_fixture.py'),'--port-base',str(port),'--out',str(out)]
   with out.with_suffix('.process.txt').open('x') as log:proc=subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
   end=time.monotonic()+40
   while time.monotonic()<end:
    if out.exists():
     try:runtime=json.loads(out.read_text())
     except json.JSONDecodeError:continue
     if runtime.get('samples'):break
    if proc.poll() is not None:self.fail('fixture exited before live cancellation boundary')
    time.sleep(.1)
   self.assertIsNotNone(runtime,'no owned runtime observed')
   self.assertTrue(runtime.get('samples'),'no live sample observed')
   self.assertIn(str(out),pathlib.Path(f'/proc/{proc.pid}/cmdline').read_bytes().decode())
   inspected=json.loads(subprocess.check_output(['docker','inspect',runtime['container_id']],text=True))[0]
   self.assertEqual(inspected['Config']['Labels']['purpose'],'task10c5-live-anomaly')
   self.assertTrue(inspected['State']['Running'])
   # The first sample is immediately followed by a planned restart; wait
   # for a currently live listener, rather than asserting a stale snapshot.
   end=time.monotonic()+20
   listening=False
   while time.monotonic()<end and proc.poll() is None:
    with socket.socket() as sock:listening=sock.connect_ex(('127.0.0.1',port+2))==0
    if listening:break
    time.sleep(.05)
   self.assertTrue(listening,'no live ingest listener at cancellation')
   cancelled=time.monotonic();proc.send_signal(signal.SIGTERM);rc=proc.wait(timeout=50)
   final=json.loads(out.read_text())
   result={'fixture_exit':rc,'cancel_cleanup_seconds':time.monotonic()-cancelled,'container_id':runtime['container_id'],'before_running':True,'before_ingest_listener':True,'report_exit':final.get('exit'),'report_error':final.get('error'),'container_absent':final.get('container_absent',False),'directory_removed':final.get('synthetic_directory_removed',False),'ports_closed':final.get('ports_closed',[])}
   receipt.write_text(json.dumps(result,indent=2)+'\n')
   self.assertEqual(rc,1,'SIGTERM must flow through bounded finally and return failure')
   self.assertTrue(result['container_absent'])
   self.assertTrue(result['directory_removed'])
   self.assertEqual(result['ports_closed'],[True]*5)
   self.assertIn('FixtureCancelled',result['report_error'])
  finally:
   # RED deliberately exposes a leaked owned runtime. Test-only fallback is
   # separate evidence and never counted as successful fixture cleanup.
   if proc:
    if proc.poll() is None:proc.kill();proc.wait(timeout=5)
    try:os.killpg(proc.pid,signal.SIGTERM)
    except ProcessLookupError:pass
   if runtime:
    r=subprocess.run(['docker','inspect',runtime['container_id']],capture_output=True,text=True,timeout=10)
    if r.returncode==0:
     actual=json.loads(r.stdout)[0]
     if actual['Id']!=runtime['container_id'] or actual['Config']['Labels'].get('purpose')!='task10c5-live-anomaly':raise RuntimeError('refuse foreign test cleanup')
     subprocess.run(['docker','rm','--force',runtime['container_id']],check=True,capture_output=True,timeout=20);fallback.append('exact owned container removed by test fallback')
    elif 'no such object' not in r.stderr.lower():raise RuntimeError('test cleanup absence unconfirmed')
    work=pathlib.Path(runtime['work'])
    if work.exists():
     if work.parent!=home.ROOT/'.cache' or not work.name.startswith('task10c5-native-'):raise RuntimeError('foreign directory refused')
     import shutil
     shutil.rmtree(work);fallback.append('fresh owned directory removed by test fallback')
   out.with_suffix('.fallback.json').write_text(json.dumps({'actions':fallback,'fixture_cleanup_pass':not fallback},indent=2)+'\n')

def boundary_worker(mode,out,port):
 import live_anomaly_fixture as fixture
 target=pathlib.Path(fixture.__file__).resolve();lines=target.read_text().splitlines()
 start_line=next(i+1 for i,line in enumerate(lines) if line.strip()=="query('CREATE DATABASE pathwatch ENGINE=Atomic')")
 success_line=next(i+1 for i,line in enumerate(lines) if line.strip()=='exitcode=0')
 reached=False;skipped=False
 def observe(frame,event,arg):
  nonlocal reached,skipped
  if reached:return observe
  if mode=='cleanup-success' and event=='line' and pathlib.Path(frame.f_code.co_filename).resolve()==target and frame.f_code.co_name=='main' and frame.f_lineno==start_line:
   # Explicit controlled test skip: actual container/HTTP readiness succeeded;
   # bypass the unchanged normal workload, then execute its real success/finally.
   frame.f_lineno=success_line;skipped=True
  is_network=event=='call' and frame.f_code.co_name=='open' and frame.f_code.co_filename.endswith('/urllib/request.py')
  is_cleanup=mode=='cleanup-success' and skipped and event=='call' and frame.f_code.co_name=='stop' and pathlib.Path(frame.f_code.co_filename).resolve()==target
  stack=[];cursor=frame;owner=None
  while cursor:
   stack.append(cursor.f_code.co_name)
   if pathlib.Path(cursor.f_code.co_filename).resolve()==target and cursor.f_code.co_name=='main':owner=cursor
   cursor=cursor.f_back
  match=is_cleanup or (is_network and ((mode=='ch-readiness' and 'query' in stack) or (mode=='ingest-readiness' and 'wait_ingest_ready' in stack)))
  if match and owner is not None:
   reached=True;state=owner.f_locals;proof={'mode':mode,'stack':stack,'container_id':state['container'],'work':str(state['work']),'before_exitcode':state['exitcode'],'workload_skipped':skipped,'report_before_cancel':state['report']}
   marker=out.with_suffix('.boundary.json');tmp=marker.with_suffix('.tmp');tmp.write_text(json.dumps(proof,indent=2)+'\n');tmp.replace(marker)
   deadline=time.monotonic()+15
   while not out.with_suffix('.release').exists():
    if time.monotonic()>deadline:raise TimeoutError('test boundary release timed out')
    time.sleep(.02)
  return observe
 sys.argv=[str(target),'--port-base',str(port),'--out',str(out)]
 sys.settrace(observe)
 try:return fixture.main()
 finally:sys.settrace(None)

@unittest.skipUnless(os.environ.get('PW_C5_BOUNDARY_TEST')=='1','explicit native cancellation boundaries only')
class CancellationBoundaryTests(unittest.TestCase):
 def test_real_readiness_and_success_cleanup_cancellation(self):
  root=home.ROOT/pathlib.Path(os.environ['PW_C5_BOUNDARY_OUT']);root.mkdir(parents=True,exist_ok=True)
  portbase=int(os.environ['PW_C5_BOUNDARY_PORT'])
  for index,mode in enumerate(('ch-readiness','ingest-readiness','cleanup-success')):
   with self.subTest(mode=mode):self.boundary(mode,root,portbase+10*index)
 def boundary(self,mode,root,port):
  out=root/(mode+'.json');proof=None;proc=None;fallback=[];observed={}
  self.assertFalse(out.exists());self.assertFalse(out.with_suffix('.boundary.json').exists())
  try:
   args=[sys.executable,str(pathlib.Path(__file__).resolve()),'boundary-worker',mode,str(out),str(port)]
   with out.with_suffix('.process.txt').open('x') as log:proc=subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
   deadline=time.monotonic()+35
   while time.monotonic()<deadline:
    marker=out.with_suffix('.boundary.json')
    if marker.exists():proof=json.loads(marker.read_text());break
    if proc.poll() is not None:self.fail('worker exited before requested actual boundary')
    time.sleep(.05)
   self.assertIsNotNone(proof)
   self.assertIn(str(out),pathlib.Path(f'/proc/{proc.pid}/cmdline').read_bytes().decode())
   inspected=json.loads(subprocess.check_output(['docker','inspect',proof['container_id']],text=True))[0]
   self.assertEqual(inspected['Id'],proof['container_id']);self.assertEqual(inspected['Config']['Labels']['purpose'],'task10c5-live-anomaly');self.assertTrue(inspected['State']['Running'])
   observed={'mode':mode,'before_running':True,'before_exitcode':proof['before_exitcode'],'workload_skipped':proof['workload_skipped'],'mounts':inspected['Mounts'],'project_sample':home.allocated([pathlib.Path(proof['work'])/'data',pathlib.Path(proof['work'])/'journal']),'signals_sent':[]}
   if mode=='cleanup-success':self.assertEqual(proof['before_exitcode'],0)
   began=time.monotonic()
   for signum in (signal.SIGTERM,signal.SIGINT):
    if proc.poll() is None:proc.send_signal(signum);observed['signals_sent'].append(signum)
    time.sleep(.05)
   out.with_suffix('.release').write_text('release real operation after external signals\n')
   try:observed['returncode']=proc.wait(timeout=4)
   except subprocess.TimeoutExpired:observed['returncode']='still running after cancellation'
   observed['cancel_wait_seconds']=time.monotonic()-began
   final=json.loads(out.read_text()) if out.exists() else {}
   observed['final']={k:final.get(k) for k in ('exit','error','cancellation_signals','container_absent','synthetic_directory_removed','ports_closed')}
   out.with_suffix('.result.json').write_text(json.dumps(observed,indent=2)+'\n')
   self.assertEqual(observed['returncode'],1,'cancellation must finish with failure, not continue workload')
   self.assertEqual(final.get('exit'),1)
   self.assertEqual(final.get('cancellation_signals'),[15,2])
   self.assertTrue(final.get('container_absent'));self.assertTrue(final.get('synthetic_directory_removed'))
   self.assertEqual(final.get('ports_closed'),[True]*5)
  finally:
   # Deliberately broken RED paths are separately cleaned; never count this as
   # production cleanup success. Only exact fresh resources from the boundary.
   if proc:
    if proc.poll() is None:proc.kill();proc.wait(timeout=5);fallback.append('killed exact worker still running')
    try:os.killpg(proc.pid,signal.SIGTERM)
    except ProcessLookupError:pass
   if proof:
    r=subprocess.run(['docker','inspect',proof['container_id']],capture_output=True,text=True,timeout=10)
    if r.returncode==0:
     actual=json.loads(r.stdout)[0]
     if actual['Id']!=proof['container_id'] or actual['Config']['Labels'].get('purpose')!='task10c5-live-anomaly':raise RuntimeError('foreign cleanup refused')
     subprocess.run(['docker','rm','--force',proof['container_id']],capture_output=True,check=True,timeout=20);fallback.append('removed exact owned container')
    elif 'no such object' not in r.stderr.lower():raise RuntimeError('container absence uncertain')
    work=pathlib.Path(proof['work'])
    if work.exists():
     if work.parent!=home.ROOT/'.cache' or not work.name.startswith('task10c5-native-'):raise RuntimeError('foreign directory refused')
     import shutil
     shutil.rmtree(work);fallback.append('removed fresh owned directory')
   out.with_suffix('.fallback.json').write_text(json.dumps({'actions':fallback},indent=2)+'\n')

if __name__=='__main__':
 if len(sys.argv)>1 and sys.argv[1]=='boundary-worker':sys.exit(boundary_worker(sys.argv[2],pathlib.Path(sys.argv[3]),int(sys.argv[4])))
 unittest.main()
