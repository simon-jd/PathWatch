#!/usr/bin/env python3
"""Fresh synthetic CH/journal volume fixture; exact-owned cleanup only."""
import argparse,base64,fcntl,hashlib,json,os,pathlib,secrets,shutil,signal,socket,subprocess,sys,tempfile,time,urllib.request
import home,capacity
ROOT=home.ROOT

def main():
 p=argparse.ArgumentParser();p.add_argument('--binary',required=True);p.add_argument('--volume',choices=['small','medium','full'],required=True);p.add_argument('--out',required=True);a=p.parse_args()
 out=pathlib.Path(a.out).resolve();binary=pathlib.Path(a.binary).resolve()
 if out.exists() or not out.is_relative_to(ROOT) or not binary.is_relative_to(ROOT/'.cache'):p.error('new project report and reviewed project binary required')
 out.parent.mkdir(parents=True,exist_ok=True)
 reserve={'small':64<<20,'medium':640<<20,'full':2<<30}[a.volume]
 before=home.allocated()
 if before+reserve>capacity.SOFT_DISK:raise ValueError(f'fixture forecast exceeds soft quota: {before}+{reserve}')
 lock=open(ROOT/'.cache/task10c3-fixture.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 port=18126
 with socket.socket() as s:s.bind(('127.0.0.1',port))
 work=pathlib.Path(tempfile.mkdtemp(prefix='task10c3-fixture-',dir=ROOT/'.cache'))
 name='pathwatch-task10c3-'+secrets.token_hex(6);identity=None;child=None;create_attempted=False
 report={'synthetic':True,'volume':a.volume,'argv':sys.argv,'initial_project_bytes':before,'reserve_bytes':reserve,'commands':[],'samples':[],'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'runtime_binary_sha256':{k:hashlib.sha256((binary.parent/k).read_bytes()).hexdigest() for k in ('probe','ingest')},'source_sha256':capacity.source_hashes(),'work':str(work)}
 labels={'project':'pathwatch','owner':'codex-pathwatch','purpose':'task10c3-capacity','run':name}
 def save():out.write_text(json.dumps(report,indent=2)+'\n')
 def command(argv,check=True):
  r=subprocess.run(argv,text=True,capture_output=True,timeout=60);report['commands'].append({'argv':argv,'exit':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
  if check and r.returncode:raise RuntimeError(f'command failed: {argv[:2]} {r.stderr}')
  return r
 def query(sql):
  report.setdefault('queries',[]).append(sql)
  req=urllib.request.Request(f'http://127.0.0.1:{port}/?max_execution_time=30&max_threads=2',data=sql.encode(),headers={'Authorization':auth})
  with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=35) as r:return r.read().decode().strip()
 def ready():
  for _ in range(60):
   try:
    if query('SELECT 1')=='1':return
   except OSError:pass
   time.sleep(.25)
  raise RuntimeError('ClickHouse readiness timed out')
 def sampled():
  atomic=[(work/'journal','journal')] if (work/'fixture-seed.json').exists() else []
  sample={'monotonic':time.monotonic(),'project_allocated':home.allocated([work/'journal',work/'data'],report.setdefault('allocation_scan_retries',[]),atomic=atomic,opened=bool(atomic)),'data_allocated':home.allocated_directory(work/'data',[work/'data'],report.setdefault('allocation_scan_retries',[])),'native_rss':0}
  if child and child.poll() is None:
   sample['native_rss']=sum(int(line.split()[1])*1024 for line in pathlib.Path(f'/proc/{child.pid}/status').read_text().splitlines() if line.startswith('VmRSS:'))
  sample['parts']=query("SELECT table,active,count() AS parts,sum(bytes_on_disk) AS bytes FROM system.parts WHERE database='task10c3' GROUP BY table,active FORMAT JSONEachRow")
  sample['merges']=query("SELECT count(),sum(total_size_bytes_compressed) FROM system.merges WHERE database='task10c3'")
  report['samples'].append(sample)
  if sample['project_allocated']>=capacity.RUNTIME_DISK or sample['data_allocated']>=768<<20 or sample['native_rss']>2<<30:raise RuntimeError('fixture resource guard exceeded')
 def interrupt(signum,frame):raise InterruptedError(f'signal {signum}')
 signal.signal(signal.SIGTERM,interrupt);signal.signal(signal.SIGINT,interrupt)
 exitcode=1;fixture_started=time.monotonic()
 report['total_timeout_seconds']=3600
 report['load_control']='full only:150k-row stages, original480s GC,≤600s each wait; sampled parts/merges; no CH lifetime/config change'
 try:
  (work/'data').mkdir();password=secrets.token_hex(24)
  (work/'users.xml').write_text('<clickhouse><users><default remove="remove"/><pathwatch_ingest><password>'+password+'</password><networks><ip>::/0</ip></networks><profile>default</profile><quota>default</quota><access_management>1</access_management></pathwatch_ingest></users></clickhouse>');(work/'users.xml').chmod(0o600)
  auth='Basic '+base64.b64encode(('pathwatch_ingest:'+password).encode()).decode()
  argv=['docker','create','--pull=never','--name',name,'--user',f'{os.getuid()}:{os.getgid()}','--memory','2g','--memory-swap','2g','--cpus','2','--cap-drop','ALL','--security-opt','no-new-privileges','--log-opt','max-size=2m','--log-opt','max-file=1','--tmpfs',f'/var/log/clickhouse-server:rw,size=16m,uid={os.getuid()},gid={os.getgid()}','-p',f'127.0.0.1:{port}:8123','-v',f'{work}/data:/var/lib/clickhouse','-v',f'{work}/users.xml:/etc/clickhouse-server/users.d/pathwatch.xml:ro','-v',f'{ROOT}/infra/modules/clickhouse/clickhouse.xml:/etc/clickhouse-server/config.d/pathwatch.xml:ro']
  for k,v in labels.items():argv+=['--label',k+'='+v]
  argv+=['--entrypoint','clickhouse-server',home.IMAGE,'--config-file=/etc/clickhouse-server/config.xml'];create_attempted=True
  identity=command(argv).stdout.strip();report['container_id']=identity
  command(['docker','start',identity]);ready();query('CREATE DATABASE task10c3 ENGINE=Atomic')
  env={**os.environ,'PW_C3_DIRECTORY':str(work),'PW_C3_URL':f'http://127.0.0.1:{port}','PW_C3_VOLUME':a.volume,'PATHWATCH_CLICKHOUSE_PASSWORD':password,'GOMAXPROCS':'2','GOMEMLIMIT':'1500MiB'}
  for stage in ['seed','verify']:
   path=out.with_suffix('.'+stage+'.txt')
   with path.open('x') as log:
    argv=[str(binary),'-test.run','^TestCapacityNativeFixture$','-test.v','-test.timeout','60m'];child=subprocess.Popen(argv,env={**env,'PW_C3_STAGE':stage},stdout=log,stderr=subprocess.STDOUT)
    started=time.monotonic()
    while child.poll() is None:
     sampled();save()
     if time.monotonic()-fixture_started>3600:raise TimeoutError('fixture total exceeded60min')
     time.sleep(2)
    report['commands'].append({'argv':argv,'stage':stage,'exit':child.returncode,'log':str(path.relative_to(ROOT))});sampled()
    if child.returncode:raise RuntimeError('fixture stage failed: '+stage)
   report[stage]=json.loads((work/f'fixture-{stage}.json').read_text())
   if stage=='seed':
    report['parts_before_restart']=query("SELECT table,active,count(),sum(bytes_on_disk) FROM system.parts WHERE database='task10c3' GROUP BY table,active FORMAT JSONEachRow")
    command(['docker','restart','--timeout','20',identity]);ready()
  report['parts_after_merge']=query("SELECT table,active,count(),sum(bytes_on_disk) FROM system.parts WHERE database='task10c3' GROUP BY table,active FORMAT JSONEachRow")
  report['journal_allocated_bytes']=int(subprocess.check_output(['du','-s','--block-size=1',str(work/'journal')],text=True).split()[0]);exitcode=0
 except BaseException as e:report['error']=type(e).__name__+': '+str(e)
 finally:
  if child and child.poll() is None:
   child.terminate()
   try:child.wait(timeout=10)
   except subprocess.TimeoutExpired:child.kill();child.wait()
  try:
   absent=not create_attempted
   if create_attempted:
    info=command(['docker','inspect',identity or name],check=False)
    if info.returncode:
     if 'no such object' not in info.stderr.lower():raise RuntimeError('cannot establish fixture absence')
     absent=True
    else:
     state=json.loads(info.stdout)[0]
     if (identity and state['Id']!=identity) or state['Name']!='/'+name or any(state['Config']['Labels'].get(k)!=v for k,v in labels.items()):raise RuntimeError('refuse foreign cleanup')
     identity=state['Id'];command(['docker','rm','--force',identity])
     removed=command(['docker','inspect',identity],check=False);absent=removed.returncode!=0 and 'no such object' in removed.stderr.lower()
     if not absent:raise RuntimeError('fixture removal not confirmed')
   report['container_absent']=absent
   if absent:shutil.rmtree(work);report['synthetic_directory_removed']=not work.exists()
  except Exception as e:report['cleanup_error']=str(e);exitcode=1
  lock.close()
  with socket.socket() as s:report['port_closed']=s.connect_ex(('127.0.0.1',port))!=0
  if not report['port_closed']:exitcode=1
  report['elapsed_seconds']=time.monotonic()-fixture_started
  report['exit']=exitcode;report['final_project_bytes']=home.allocated();report['log_sha256']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [out.with_suffix('.seed.txt'),out.with_suffix('.verify.txt')] if p.exists()};save()
 print(json.dumps({'exit':exitcode,'out':str(out),'error':report.get('error')}));return exitcode
if __name__=='__main__':sys.exit(main())
