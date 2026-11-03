"""Lifecycle tests use synthetic admission proofs; no service/network is started."""
import contextlib,hashlib,json,pathlib,tempfile,unittest,urllib.error
from unittest.mock import Mock,patch
import capacity,capacity_campaign as campaign,home


class DeferredLaunchTests(unittest.TestCase):
 def test_wait_changes_refuse_before_any_service_and_keep_claim(self):
  for change in ('disk','binary','config','plan','evidence','state','unchanged'):
   with self.subTest(change=change),tempfile.TemporaryDirectory(prefix='task10c3-startup-',dir=home.ROOT/'.cache') as tmp,contextlib.ExitStack() as stack:
    base=pathlib.Path(tmp);bins=base/'bin';bins.mkdir();hashes={}
    for kind in ('probe','ingest','echo'):
     (bins/kind).write_text(kind);hashes[kind]=hashlib.sha256(kind.encode()).hexdigest()
    probe=base/'probe.json';roster=base/'roster.json';proof=base/'proof.json';plan=base/'plan.json'
    probe.write_text(json.dumps({'probe_id':'home','spool_dir':str(base/'spool'),'metrics_addr':'127.0.0.1:19464'}))
    roster.write_text('{"home":"home-ubuntu"}');proof.write_text('{}');plan.write_text('approved')
    cfg={'listen':'127.0.0.1:19443','development_cleartext':True,'binary_dir':str(bins),'binary_sha256':hashes,'probe_config':str(probe),'roster':str(roster),'spool_dir':str(base/'spool'),'capacity_plan':str(plan),'capacity_evidence':str(proof),'capacity_prometheus_evidence':str(proof),'prometheus_port':19090,'clickhouse_port':18126,'metrics_port':19091,'probe_metrics_port':19464,'observer_port':19465}
    clock=[1000.0];disk=[2_800_000_000];starts={site:1001 for site in campaign.SITES}
    def plans(*_):return {str(probe):hashlib.sha256(probe.read_bytes()).hexdigest()},'test',hashlib.sha256(plan.read_bytes()).hexdigest(),starts
    def sleep(seconds):
     clock[0]+=seconds
     if change=='disk':disk[0]=10_000_000_000 # 4 GB under the 6 GB share; shifted with the 12 GB share
     elif change=='binary':(bins/'ingest').write_text('changed')
     elif change=='config':probe.write_text(probe.read_text()+' ')
     elif change=='plan':plan.write_text('changed')
     elif change=='evidence':proof.write_text('{"changed":true}')
     elif change=='state':
      state=campaign.load(base);state['home_starts']+=1;campaign.persist(base,state)
    def verify(e,*_):
     if e:raise ValueError('changed proof')
    stack.enter_context(patch.dict(home.os.environ,{'PATHWATCH_CLICKHOUSE_PASSWORD':'synthetic-test-password','PATHWATCH_GRAFANA_CH_PASSWORD':''}))
    stack.enter_context(patch.object(home.time,'time',side_effect=lambda:clock[0]));stack.enter_context(patch.object(home.time,'monotonic',side_effect=lambda:clock[0]))
    stack.enter_context(patch.object(home.time,'sleep',side_effect=sleep));stack.enter_context(patch.object(home,'allocated',side_effect=lambda:disk[0]))
    stack.enter_context(patch.object(capacity,'plan_configs',side_effect=plans));stack.enter_context(patch.object(capacity,'verify_envelope',side_effect=verify));stack.enter_context(patch.object(capacity,'verify_prometheus_envelope',side_effect=verify))
    stack.enter_context(patch.object(campaign,'arm_deadline',return_value=None));stack.enter_context(patch.object(home.signal,'signal'))
    # The command normally exits after run; keep test-owned lock descriptors
    # explicit so exception tracebacks cannot delay their release in this suite.
    real_open=open
    def owned_open(path,*args,**kwargs):
     f=real_open(path,*args,**kwargs)
     if path==base/'owner.lock':stack.callback(f.close)
     return f
    stack.enter_context(patch.object(home,'open',side_effect=owned_open,create=True))
    # Capture only the service-launch boundary; actual du remains available.
    original_run=home.subprocess.run;launches=[]
    def command(argv,**kwargs):
     if argv[0]=='docker':launches.append(argv);raise RuntimeError('launch boundary reached')
     return original_run(argv,**kwargs)
    stack.enter_context(patch.object(home.subprocess,'run',side_effect=command))
    real_popen=home.subprocess.Popen;children=[]
    def child(argv,**kwargs):
     if kwargs.get('cwd')==home.ROOT:children.append(argv);raise AssertionError('unexpected child')
     return real_popen(argv,**kwargs)
    stack.enter_context(patch.object(home.subprocess,'Popen',side_effect=child))
    with self.assertRaises((ValueError,RuntimeError)) as raised:home.run(base,cfg)
    self.assertEqual(len(launches),int(change=='unchanged'),str(raised.exception));self.assertEqual(children,[])
    saved=campaign.load(base);self.assertEqual(saved['home_starts'],2 if change=='state' else 1);self.assertEqual(saved['sites']['home-ubuntu'],{'start':1001,'end':1001+35*86400})
    self.assertFalse((base/'slo-state.json').exists());self.assertFalse((base/'control.sock').exists())
    # A second invocation does not inherit the in-memory launch exception.
    plan.write_text('approved')
    with self.assertRaisesRegex(ValueError,'SLI history'):capacity.preflight(base,cfg,bins,disk[0])

 def test_launch_state_requires_full_equality_and_fixed_plan(self):
  with tempfile.TemporaryDirectory() as tmp:
   base=pathlib.Path(tmp);starts={site:1001 for site in campaign.SITES}
   state=campaign.claim(base,campaign.prepare(base,'test','a'*64,starts,1000),1000)
   cfg={'prometheus_port':19090,'capacity_plan':'synthetic','spool_dir':str(base/'spool'),'probe_config':str(base/'probe')}
   for mutation in ('clock','counter','plan','start'):
    saved=json.loads(json.dumps(state));expected_starts=dict(starts);digest='a'*64
    if mutation=='clock':saved['last_wall']+=1
    elif mutation=='counter':saved['home_starts']+=1
    elif mutation=='plan':digest='b'*64
    else:expected_starts['azure-b']+=1
    campaign.persist(base,saved)
    with self.subTest(mutation=mutation),patch.object(capacity,'plan_configs',return_value=({},'test',digest,expected_starts)),patch.object(capacity.time,'time',return_value=1001):
     with self.assertRaisesRegex(ValueError,'changed'):capacity.preflight(base,cfg,base,0,launch_state=state,launch_resume=False)


class PrometheusReadinessTests(unittest.TestCase):
 def wait(self,responses,timeout=3,poll=None,check=None):
  clock=[0.0];proc=Mock();proc.poll.side_effect=poll or (lambda:None)
  def sleep(seconds):clock[0]+=seconds
  with patch.object(home,'fetch',side_effect=responses) as fetch,patch.object(home.time,'monotonic',side_effect=lambda:clock[0]),patch.object(home.time,'sleep',side_effect=sleep):
   home.wait_prometheus_ready(proc,19090,check or (lambda:None),timeout=timeout)
  return fetch
 def test_connection_then_503_then_ready(self):
  response=urllib.error.HTTPError('local',503,'starting',{},None)
  try:fetch=self.wait([ConnectionRefusedError(),response,'Prometheus Server is Ready.\n','{"status":"success","data":{"headStats":{"numSeries":128}}}'])
  finally:response.close()
  self.assertEqual(fetch.call_count,4)
 def test_ready_requires_readable_tsdb_and_checks_exit_after_request(self):
  ready='Prometheus Server is Ready.';tsdb='{"status":"success","data":{"headStats":{"numSeries":128}}}'
  fetch=self.wait([ready,ConnectionRefusedError(),ready,tsdb]);self.assertEqual(fetch.call_count,4)
  with self.assertRaisesRegex(RuntimeError,'exited'):self.wait([ready],poll=Mock(side_effect=[None,9]))
  with self.assertRaises(ValueError):self.wait([ready,'{"status":"success","data":{"headStats":{"numSeries":129}}}'])
 def test_timeout_and_child_exit_fail_closed(self):
  with self.assertRaises(TimeoutError):self.wait([ConnectionRefusedError()]*10,timeout=1)
  with self.assertRaisesRegex(RuntimeError,'exited'):self.wait([],poll=lambda:7)
 def test_expiry_before_and_during_request(self):
  with self.assertRaisesRegex(ValueError,'expired'):self.wait([],check=Mock(side_effect=ValueError('expired')))
  with self.assertRaisesRegex(ValueError,'expired'):self.wait(['Prometheus Server is Ready.'],check=Mock(side_effect=[None,ValueError('expired')]))
 def test_bad_ready_response_and_http_400_are_not_retried(self):
  with self.assertRaises(ValueError):self.wait(['unexpected'])
  response=urllib.error.HTTPError('local',400,'bad request',{},None)
  try:
   with self.assertRaises(urllib.error.HTTPError):self.wait([response])
  finally:response.close()
 def test_cardinality_malformed_or_over_limit_fail_closed(self):
  for response in ('bad json','{}','{"status":"success","data":{"headStats":{"numSeries":129}}}','{"status":"success","data":{"headStats":{"numSeries":true}}}'):
   with self.subTest(response=response),patch.object(home,'fetch',return_value=response),self.assertRaises(ValueError):home.prometheus_series(19090)
  with patch.object(home,'fetch',return_value='{"status":"success","data":{"headStats":{"numSeries":128}}}'):self.assertEqual(home.prometheus_series(19090),128)
