#!/usr/bin/env python3
"""Project-owned home lifecycle; no host services, cloud calls or shared cleanup."""
import re
import argparse,base64,fcntl,hashlib,ipaddress,json,os,pathlib,signal,socket,ssl,subprocess,sys,time,urllib.request,uuid
# Sibling import must work when this file is loaded by path from another directory (drills/outage.py).
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import anomaly_config
from disk_share import DISK_SHARE,RUNTIME_DISK
ROOT=pathlib.Path(__file__).resolve().parents[3]
IMAGE='clickhouse/clickhouse-server@sha256:456063a689194186633bb3db0862283068f4c2ee538852d3aaffa7eb66f1f841'
IMAGE_BYTES=1183513909
BUILDER_BOUND=550000000
PROM_TOOL_IMAGE_BYTES=371925065
NAMESPACE_RUNTIME_BYTES=6*1024*1024 # two 1MiB tmpfs, two 1MiB logs, 2MiB daemon metadata reserve
JOURNAL_LIMIT=268435456

def capacity_reason(disk,records,journal,spool,ch_data=0):
    if ch_data>=256*1024*1024:return 'dedicated ClickHouse data hard-stop 256MiB'
    if disk>=RUNTIME_DISK:return f'project disk hard-stop {RUNTIME_DISK/1e9:.1f}GB (300MB reserve)'
    if records>=52428:return 'journal record hard-stop 80% of 65536'
    if journal>=214748364:return 'journal byte hard-stop 80% of 256MiB'
    if spool>=214748364:return 'spool byte hard-stop 80% of 256MiB'
    return ''

def validate_ingest(cfg):
    anomaly_config.normalize(cfg)
    host=cfg['listen'].rsplit(':',1)[0].strip('[]')
    if cfg.get('development_cleartext'):
        if not ipaddress.ip_address(host).is_loopback or cfg.get('tls_cert') or cfg.get('tls_key'):
            raise ValueError('development plaintext requires literal loopback and no TLS files')
    elif not cfg.get('tls_cert') or not cfg.get('tls_key'):raise ValueError('trusted TLS certificate and key required')

def ingest_args(base,cfg,binaries,capacity=None):
    validate_ingest(cfg)
    args=[str(binaries/'ingest'),'-listen',cfg['listen'],'-clickhouse-url',f'http://127.0.0.1:{cfg["clickhouse_port"]}','-journal',str(base/'journal'),'-metrics',f'127.0.0.1:{cfg["metrics_port"]}']
    if capacity:args += ['-journal-max-records',str(capacity.RECORD_CAP),'-journal-max-bytes',str(capacity.JOURNAL_CAP)]
    if cfg.get('development_cleartext'):args+=['-development-cleartext']
    else:args+=['-tls-cert',cfg['tls_cert'],'-tls-key',cfg['tls_key']]
    return args+anomaly_config.arguments(cfg)

def wait_ingest_ready(proc,port,enabled,timeout=60):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if proc.poll() is not None:raise RuntimeError('ingest exited before readiness; inspect ingest.log')
        try:
            fetch(f'http://127.0.0.1:{port}/readyz',timeout=min(2,max(.01,deadline-time.monotonic())))
            metrics=fetch(f'http://127.0.0.1:{port}/metrics',timeout=min(2,max(.01,deadline-time.monotonic())))
            expected={'pathwatch_anomaly_ticks_total','pathwatch_anomaly_failures_total','pathwatch_anomaly_delivered_total'}
            names={line.split()[0] for line in metrics.splitlines() if line and not line.startswith('#')}
            if enabled and not expected<=names:raise ValueError('enabled ingest lacks anomaly metrics')
            if proc.poll() is not None:raise RuntimeError('ingest exited during readiness')
            return
        except urllib.error.HTTPError as e:
            if e.code!=503:raise
        except OSError:pass
        time.sleep(.1)
    raise TimeoutError('ingest readiness timed out')


def validate_probe(cfg,probe,roster):
    if probe.get('probe_id')!='home' or roster.get('home')!='home-ubuntu':
        raise ValueError('native home process must retain actual home physical identity')
    if (ROOT/pathlib.Path(probe.get('spool_dir',''))).resolve()!=pathlib.Path(cfg['spool_dir']).resolve():
        raise ValueError('probe spool must match monitored owned spool')
    expected='10.30.0.2:9464' if cfg.get('namespace_observer') else f'127.0.0.1:{cfg["probe_metrics_port"]}'
    if probe.get('metrics_addr')!=expected or (cfg.get('namespace_observer') and cfg['probe_metrics_port']!=9464):
        raise ValueError('probe metrics must match owned observer configuration')
    if cfg.get('namespace_observer') and cfg.get('owned_echo'):raise ValueError('Namespace observer owns its private echo; loopback echo cannot overlap')

def reviewed_binaries(cfg):
    binaries=pathlib.Path(cfg.get('binary_dir',ROOT/'.cache/task10-bin')).resolve()
    if not binaries.is_relative_to(ROOT/'.cache') or binaries==ROOT/'.cache':raise ValueError('Binaries must be reviewed project-local artifacts')
    kinds=['ingest'] if cfg.get('namespace_observer') else ['ingest','probe','echo']
    if cfg.get('capacity_plan') and 'probe' not in kinds:kinds.append('probe')
    hashes=cfg.get('binary_sha256',{})
    for kind in kinds:
        if hashlib.sha256((binaries/kind).read_bytes()).hexdigest()!=hashes.get(kind):raise ValueError('Explicit reviewed artifact hash required: '+kind)
    return binaries

def wait_echo_ready(cert,ready,token,address,alive,timeout=10,ca=None):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if not alive():raise RuntimeError('echo exited before generation readiness')
        try:
            state=json.loads(pathlib.Path(ready).read_text())
            if state.get('token')==token:
                if state.get('address')!=address or state.get('cert_sha256')!=hashlib.sha256(pathlib.Path(cert).read_bytes()).hexdigest():raise ValueError('echo readiness certificate/address mismatch')
                context=ssl.create_default_context(cafile=str(ca or cert));context.minimum_version=ssl.TLSVersion.TLSv1_3
                host,port=address.rsplit(':',1)
                with socket.create_connection((host,int(port)),timeout=1) as raw:
                    with context.wrap_socket(raw,server_hostname=host) as conn:
                        conn.sendall(b'HEAD / HTTP/1.1\r\nHost: '+host.encode()+b'\r\nConnection: close\r\n\r\n')
                        response=b''
                        while b'\r\n\r\n' not in response and len(response)<16384:
                            data=conn.recv(4096)
                            if not data:break
                            response+=data
                        if ('x-pathwatch-generation: '+token).encode() not in response.lower():raise ValueError('echo server generation mismatch')
                return
        except (FileNotFoundError,json.JSONDecodeError,ConnectionError,ssl.SSLError):pass
        time.sleep(.02)
    raise TimeoutError('echo generation readiness timed out')

def request(base, command):
    s=socket.socket(socket.AF_UNIX);s.settimeout(45)
    try:
        s.connect(str(base/'control.sock'));s.sendall((command+'\n').encode());s.shutdown(socket.SHUT_WR)
        data=b''
        while True:
            chunk=s.recv(65536)
            if not chunk:break
            data+=chunk
            if len(data)>1<<20:raise ValueError('control response too large')
        result=json.loads(data)
        if result.get('error'):raise RuntimeError(result['error'])
        return result
    finally:s.close()

def allocated_directory(directory,transient_roots=(),audit=None,atomic=(),opened=False):
    # du is not atomic: an owned journal rename or CH merge can remove a file
    # after readdir. Retry only ENOENT inside explicit owned mutable directories.
    # Every failed scan is recorded; never trust its partial byte total.
    roots=[pathlib.Path(p).resolve() for p in transient_roots]
    argv=['du','-s','--block-size=1']
    reserve=0;patterns=[]
    if atomic and not opened:raise ValueError('atomic exclusion requires successful Open and owned single writer')
    for parent,kind in atomic:
        parent=pathlib.Path(parent).resolve()
        if not parent.is_relative_to(pathlib.Path(directory).resolve()):raise ValueError('foreign atomic exclusion')
        prefix,bound={'journal':('.pw-tmp-',(24<<20)+4096),'spool':('.pw-tmp-',(16<<20)+4096),'campaign':('.capacity-campaign-',16384)}[kind]
        pattern=str(parent/prefix)+'*';patterns.append(pattern);argv+=['--exclude='+pattern];reserve+=bound
    argv.append(str(directory))
    if patterns:
        event={'atomic_patterns':patterns,'atomic_reserved_bytes':reserve,'successful_open_single_owner':True}
        if audit is not None:audit.append(event)
        else:print(json.dumps({'allocation_scan_reserve':event}),file=sys.stderr)
    for attempt in range(5):
        result=subprocess.run(argv,capture_output=True,text=True,timeout=20,env={**os.environ,'LC_ALL':'C'})
        if result.returncode==0 and not result.stderr:
            return int(result.stdout.split()[0])+reserve
        event={'attempt':attempt+1,'argv':argv,'exit':result.returncode,'stderr':result.stderr}
        if audit is not None:audit.append(event)
        else:print(json.dumps({'allocation_scan_retry':event}),file=sys.stderr)
        lines=result.stderr.splitlines();owned_missing=bool(lines) and result.returncode==1
        for line in lines:
            match=re.fullmatch(r"du: cannot access '(.+)': No such file or directory",line)
            if not match or not any(pathlib.Path(match[1]).resolve().is_relative_to(root) for root in roots):owned_missing=False
        if not owned_missing:raise RuntimeError('allocated scan failed: '+result.stderr.strip())
        if attempt<4:time.sleep(.01)
    raise RuntimeError('allocated scan still unstable after five owned-ENOENT retries')

def allocated(transient_roots=(),audit=None,atomic=(),opened=False):
    # GNU du counts hardlinked cache/provider files once. All live data is here.
    return allocated_directory(ROOT,transient_roots,audit,atomic,opened)+IMAGE_BYTES+BUILDER_BOUND+PROM_TOOL_IMAGE_BYTES+18*1024*1024

def fetch(url,timeout=4):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url,timeout=timeout) as r:return r.read(1<<20).decode()

def prometheus_series(port,timeout=4):
    value=json.loads(fetch(f'http://127.0.0.1:{port}/api/v1/status/tsdb',timeout=timeout))
    try:series=value['data']['headStats']['numSeries']
    except (KeyError,TypeError) as e:raise ValueError('malformed Prometheus TSDB response') from e
    if value.get('status')!='success' or type(series)!=int or not 0<=series<=128:raise ValueError('Prometheus profile requires0..128series')
    return series

def wait_prometheus_ready(proc,port,check_campaign,timeout=60):
    deadline=time.monotonic()+timeout
    def check():
        check_campaign()
        if proc.poll() is not None:raise RuntimeError('Prometheus exited before readiness')
        left=deadline-time.monotonic()
        if left<=0:raise TimeoutError('Prometheus readiness timed out')
        return min(4,left)
    while True:
        left=check()
        try:
            ready=fetch(f'http://127.0.0.1:{port}/-/ready',timeout=left)
            left=check()
            if ready.strip()!='Prometheus Server is Ready.':raise ValueError('malformed Prometheus readiness response')
            series=prometheus_series(port,timeout=left)
            check()
            return series
        except urllib.error.HTTPError as e:
            if e.code!=503:raise
        except OSError:pass # bounded connection/replay startup only
        check();time.sleep(min(.25,max(0,deadline-time.monotonic())))

def spool_batches(directory,manifest):
    batches={}
    for entry in ((manifest or {}).get('Entries') or []):
        try:
            batch=json.loads((directory/(entry['ID']+'.batch')).read_text())
            batches[entry['ID']]={'probe_id':batch['Results'][0]['ProbeID'],'results':len(batch['Results'])}
        except FileNotFoundError:pass # normal ACK between manifest and snapshot
    return batches

def committed_spool(directory):
    try:raw=(directory/'manifest.json').read_bytes()
    except FileNotFoundError:return None,0
    manifest=json.loads(raw)
    # Atomic manifest identifies one committed point. ACK may already have
    # unlinked a batch; counting its recorded bytes is safely conservative.
    return manifest,len(raw)+sum(entry['Size'] for entry in manifest.get('Entries') or [])


def run(base,cfg):
    validate_ingest(cfg)
    binaries=reviewed_binaries(cfg)
    lock=open(base/'owner.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    capacity=None;campaign_state=None;deadline_handler=None
    if cfg.get("capacity_plan"):
        import capacity as capacity_module
        capacity=capacity_module
        report=capacity.preflight(base,cfg,binaries,allocated())
        (base/"capacity-preflight.json").write_text(json.dumps(report,indent=2)+"\n")
        if not report["resources_fit"] or not report["long_deployment_ready"]:raise ValueError("Capacity preflight refused; inspect capacity-preflight.json")
    echo_artifact=None
    if cfg.get('namespace_observer'):
        if str(ROOT/'scripts') not in sys.path:sys.path.insert(0,str(ROOT/'scripts'))
        from probe_home_observer import prepare
        echo_artifact=prepare(cfg,base/'namespace-observer')['settings']['echo_binary']
    elif cfg.get('owned_echo'):echo_artifact=str(binaries/'echo')
    if echo_artifact and subprocess.check_output([echo_artifact,'-version'],text=True,timeout=3).strip()!='pathwatch-echo-private-v1':raise ValueError('Rebuild reviewed echo for generation-aware lifecycle')
    password=os.environ.get('PATHWATCH_CLICKHOUSE_PASSWORD','')
    if len(password)<16:raise ValueError('PATHWATCH_CLICKHOUSE_PASSWORD must contain at least 16 characters')
    namespace_bytes=NAMESPACE_RUNTIME_BYTES if cfg.get('namespace_observer') else 0
    if allocated()+namespace_bytes+256*1024*1024>DISK_SHARE:raise ValueError('256MiB startup data reserve would exceed project quota')
    # This module never imports an external database or removes an existing one.
    (base/'data').mkdir(exist_ok=True);(base/'journal').mkdir(exist_ok=True);(base/'logs').mkdir(exist_ok=True)
    from xml.sax.saxutils import escape
    users='<clickhouse><users><default remove="remove"/><pathwatch_ingest><password>'+escape(password)+'</password><networks><ip>::/0</ip></networks><profile>default</profile><quota>default</quota><access_management>1</access_management></pathwatch_ingest></users></clickhouse>'
    reader=os.environ.get('PATHWATCH_GRAFANA_CH_PASSWORD','')
    if reader:
        if len(reader)<16:raise ValueError('Grafana reader password must contain at least 16 characters')
        # clickhouse-go adds five seconds to the Grafana context deadline.
        # Permit only that bounded setting change while retaining readonly=1.
        users=users.replace('</users>', '<pathwatch_grafana><password>'+escape(reader)+'</password><networks><ip>::/0</ip></networks><profile>pathwatch_readonly</profile><quota>default</quota></pathwatch_grafana></users><profiles><pathwatch_readonly><readonly>1</readonly><max_execution_time>10</max_execution_time><max_memory_usage>67108864</max_memory_usage><constraints><max_execution_time><min>1</min><max>15</max><changeable_in_readonly/></max_execution_time></constraints></pathwatch_readonly></profiles>')
    (base/'users.xml').write_text(users);(base/'users.xml').chmod(0o600)
    name='pathwatch-task10-'+uuid.uuid4().hex[:12]
    docker=['docker'];container=False;children={};paused=False;stopping=False
    paths=[pathlib.Path(__file__),pathlib.Path(anomaly_config.__file__),ROOT/'slo/observe.py',ROOT/'slo/generated.rules.yaml',binaries/'ingest',pathlib.Path(cfg['probe_config']),pathlib.Path(cfg['roster'])]
    if cfg.get('namespace_observer'):
        paths += [ROOT/'scripts/probe_home_observer.py',pathlib.Path(cfg['namespace_observer']['probe_binary']),pathlib.Path(cfg['namespace_observer']['echo_binary'])]
    else:paths += [binaries/'probe',binaries/'echo']
    source_hashes={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    (base/(name+'-sources.json')).write_text(json.dumps(source_hashes,indent=2)+'\n')
    sock=socket.socket(socket.AF_UNIX)
    if (base/'control.sock').exists():(base/'control.sock').unlink()
    sock.bind(str(base/'control.sock'));os.chmod(base/'control.sock',0o600);sock.listen(4);sock.settimeout(1)
    def spawn(kind,args):
        with open(base/'logs'/f'{kind}.log','ab',buffering=0) as log:
            env=dict(os.environ)
            if kind=='probe' and cfg.get('owned_echo'):env['SSL_CERT_FILE']=str(base/'echo-cert.pem')
            children[kind]=subprocess.Popen(args,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    def stop(kind):
        proc=children.pop(kind,None)
        if proc is None:return
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=40)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
    def start_ingest():
        spawn('ingest',ingest_args(base,cfg,binaries,capacity))
        wait_ingest_ready(children['ingest'],cfg['metrics_port'],anomaly_config.normalize(cfg)['enabled'])
    def snapshot():
        result={'utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'monotonic_seconds':time.monotonic(),'owner':'codex-pathwatch','root':str(ROOT),'container':name,'paused':paused,'pid_namespace':os.readlink('/proc/self/ns/pid'),'children':{k:{'pid':v.pid,'returncode':v.poll(),'args':v.args} for k,v in children.items()}}
        for key,port in [('ingest_metrics',cfg['metrics_port']),('probe_metrics',cfg['probe_metrics_port']),('slo_metrics',cfg['observer_port'])]:
            host='10.30.0.2' if key=='probe_metrics' and cfg.get('namespace_observer') else '127.0.0.1'
            try:result[key]=fetch(f'http://{host}:{port}/metrics')
            except OSError as e:result[key]={'error':str(e)}
        if capacity:result['spool_manifest'],result['spool_bytes']=committed_spool(pathlib.Path(cfg['spool_dir']))
        else:result['spool_manifest']=json.loads((pathlib.Path(cfg['spool_dir'])/'manifest.json').read_text()) if (pathlib.Path(cfg['spool_dir'])/'manifest.json').exists() else None
        result['spool_batches']=spool_batches(pathlib.Path(cfg['spool_dir']),result['spool_manifest'])

        result['journal_records']=len(list((base/'journal').glob('*.json')))-int((base/'journal'/'owner.json').exists())
        result['journal_bytes']=sum(p.stat().st_size for p in (base/'journal').glob('*.json'))
        if not capacity:result['spool_bytes']=sum(p.stat().st_size for p in pathlib.Path(cfg['spool_dir']).glob('*') if p.is_file())
        result['source_hashes']=source_hashes
        mutable=[base/'data',base/'journal',base/'prometheus',pathlib.Path(cfg['spool_dir'])]
        result['allocation_scan_retries']=[]
        result['clickhouse_data_allocated_bytes']=allocated_directory(base/'data',[base/'data'],result['allocation_scan_retries'])
        result['prometheus_allocated_bytes']=allocated_directory(base/'prometheus',[base/'prometheus'],result['allocation_scan_retries']) if (base/'prometheus').exists() else 0
        atomic=[]
        if capacity and campaign_state:
            atomic.append((base,'campaign'))
            if isinstance(result['ingest_metrics'],str):atomic.append((base/'journal','journal'))
            if isinstance(result['probe_metrics'],str):atomic.append((pathlib.Path(cfg['spool_dir']),'spool'))
        result['project_allocated_bytes']=allocated(mutable,result['allocation_scan_retries'],atomic=atomic,opened=bool(atomic))+namespace_bytes
        if campaign_state:result['capacity_campaign']=campaign_state
        result['native_rss_bytes']=sum(int(line.split()[1])*1024 for proc in children.values() if proc.poll() is None for line in pathlib.Path(f'/proc/{proc.pid}/status').read_text().splitlines() if line.startswith('VmRSS:'))
        if cfg.get('prometheus_port'):
            if capacity:
                result['prometheus_series']=prometheus_series(cfg['prometheus_port'])
            try:result['prometheus_alerts']=json.loads(fetch(f'http://127.0.0.1:{cfg["prometheus_port"]}/api/v1/alerts'))
            except (OSError,ValueError) as e:result['prometheus_alerts']={'error':str(e)}
        return result
    def shutdown(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGINT,shutdown);signal.signal(signal.SIGTERM,shutdown)
    try:
        if capacity:
            campaign_state=capacity.campaign.claim(base,report['campaign'],time.time())
            deadline_handler=capacity.campaign.arm_deadline(campaign_state)
            wall_start=time.time();mono_start=time.monotonic();last_checkpoint=mono_start
            while time.time()<campaign_state['sites']['home-ubuntu']['start'] and not stopping:
                if time.monotonic()-last_checkpoint>=30:
                    campaign_state=capacity.campaign.checkpoint(base,campaign_state,wall_start,mono_start);last_checkpoint=time.monotonic()
                time.sleep(1)
            if stopping:return
            campaign_state=capacity.campaign.checkpoint(base,campaign_state,wall_start,mono_start)
            # Future starts may have waited days. Re-admit the actual remaining
            # campaign immediately before creating any service, without claim
            # or SLI reset. All allocation scans here include temporary files.
            binaries=reviewed_binaries(cfg)
            if any(hashlib.sha256((ROOT/path).read_bytes()).hexdigest()!=digest for path,digest in source_hashes.items()):raise ValueError('runtime inputs changed during capacity wait')
            validate_probe(cfg,json.loads(pathlib.Path(cfg['probe_config']).read_text()),json.loads(pathlib.Path(cfg['roster']).read_text()))
            if cfg.get('namespace_observer'):prepare(cfg,base/'namespace-observer')
            report=capacity.preflight(base,cfg,binaries,allocated(),launch_state=campaign_state,launch_resume=report['resume'])
            (base/'capacity-launch-preflight.json').write_text(json.dumps(report,indent=2)+'\n')
            if not report['resources_fit'] or not report['long_deployment_ready']:raise ValueError('Capacity launch preflight refused; inspect capacity-launch-preflight.json')
            if allocated()+namespace_bytes+256*1024*1024>DISK_SHARE:raise ValueError('256MiB startup data reserve would exceed project quota')
        cmd=docker+['create','--pull=never','--name',name,'--label','project=pathwatch','--label','owner=codex-pathwatch','--label','task=10','--user',f'{os.getuid()}:{os.getgid()}','--memory','2g','--memory-swap','2g','--cpus','2','--cap-drop','ALL','--security-opt','no-new-privileges','--log-opt','max-size=2m','--log-opt','max-file=1','--tmpfs',f'/var/log/clickhouse-server:rw,size=16m,uid={os.getuid()},gid={os.getgid()}', '-p',f'127.0.0.1:{cfg["clickhouse_port"]}:8123','-v',f'{base}/data:/var/lib/clickhouse','-v',f'{base}/users.xml:/etc/clickhouse-server/users.d/pathwatch.xml:ro','-v',f'{ROOT}/infra/modules/clickhouse/clickhouse.xml:/etc/clickhouse-server/config.d/pathwatch.xml:ro','--entrypoint','clickhouse-server',IMAGE,'--config-file=/etc/clickhouse-server/config.xml']
        subprocess.run(cmd,check=True);container=True
        subprocess.run(docker+['start',name],check=True)
        auth='Basic '+base64.b64encode(('pathwatch_ingest:'+password).encode()).decode()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(60):
            try:
                req=urllib.request.Request(f'http://127.0.0.1:{cfg["clickhouse_port"]}',data=b'CREATE DATABASE IF NOT EXISTS pathwatch ENGINE=Atomic',headers={'Authorization':auth})
                with opener.open(req,timeout=2) as response:response.read()
                break
            except OSError:
                if attempt==59:raise
                time.sleep(1)
        start_ingest()
        if cfg.get('owned_echo'):
            token=uuid.uuid4().hex;ready=base/'echo-ready.json';ready.unlink(missing_ok=True)
            spawn('echo',[str(binaries/'echo'),'-listen','127.0.0.1:18443','-cert',str(base/'echo-cert.pem'),'-key',str(base/'echo-key.pem'),'-ready-file',str(ready),'-ready-token',token])
            wait_echo_ready(base/'echo-cert.pem',ready,token,'127.0.0.1:18443',lambda:children['echo'].poll() is None)
        if cfg.get('namespace_observer'):
            namespace_config=base/'namespace-observer.json';namespace_config.write_text(json.dumps(cfg));namespace_config.chmod(0o600)
            spawn('probe',[sys.executable,str(ROOT/'scripts/probe_home_observer.py'),'run','--config',str(namespace_config),'--directory',str(base/'namespace-observer')])
        else:spawn('probe',[str(binaries/'probe'),'-config',cfg['probe_config']])
        spawn('observer',[sys.executable,str(ROOT/'slo/observe.py'),'--roster',cfg['roster'],'--state',str(base/'slo-state.json'),'--endpoint',f'http://127.0.0.1:{cfg["metrics_port"]}/readyz','--port',str(cfg['observer_port'])])
        if cfg.get('prometheus_port'):
            config=(ROOT/'slo/prometheus.yaml').read_text().replace('/etc/pathwatch/generated.rules.yaml',str(ROOT/'slo/generated.rules.yaml'))
            for old,new in [(9465,cfg['observer_port']),(9091,cfg['metrics_port']),(9464,cfg['probe_metrics_port'])]:config=config.replace(f'127.0.0.1:{old}',f'127.0.0.1:{new}')
            if cfg.get('namespace_observer'):config=config.replace(f'127.0.0.1:{cfg["probe_metrics_port"]}','10.30.0.2:9464')
            if capacity:config=capacity.prometheus_config(config)
            (base/'prometheus.yaml').write_text(config)
            spawn('prometheus',[str(ROOT/'.tools/bin/prometheus'),f'--config.file={base}/prometheus.yaml',f'--web.listen-address=127.0.0.1:{cfg["prometheus_port"]}',f'--storage.tsdb.path={base}/prometheus','--storage.tsdb.retention.time=35d','--storage.tsdb.retention.size='+('256MB' if capacity else '64MB')]+(['--storage.tsdb.wal-segment-size=16MB','--storage.tsdb.max-block-chunk-segment-size=16MB'] if capacity else []))
            if capacity:
                def check_startup():
                    if stopping:raise InterruptedError('capacity startup stopped')
                    capacity.campaign.remaining(campaign_state,time.time())
                    if abs((time.time()-wall_start)-(time.monotonic()-mono_start))>5:raise ValueError('campaign clock discontinuity')
                wait_prometheus_ready(children['prometheus'],cfg['prometheus_port'],check_startup)
        last_check=0
        while not stopping:
            if capacity:
                capacity.campaign.remaining(campaign_state,time.time())
                if abs((time.time()-wall_start)-(time.monotonic()-mono_start))>5:raise ValueError('campaign clock discontinuity')
            if any(proc.poll() is not None for proc in children.values()):raise RuntimeError('owned child exited; inspect retained logs')
            if time.monotonic()-last_check>=30:
                if capacity:campaign_state=capacity.campaign.checkpoint(base,campaign_state,wall_start,mono_start)
                state=snapshot();last_check=time.monotonic()
                (base/'latest.json').write_text(json.dumps(state,indent=2))
                reason=capacity_reason(state['project_allocated_bytes'],state['journal_records'],state['journal_bytes'],state['spool_bytes'],state['clickhouse_data_allocated_bytes'])
                if capacity:reason=capacity.runtime_reason(state['project_allocated_bytes'],state['journal_records'],state['journal_bytes'],state['spool_bytes'],state['clickhouse_data_allocated_bytes'],state['prometheus_allocated_bytes'],state['native_rss_bytes'])
                if reason:raise RuntimeError(reason)
                if not capacity and state['native_rss_bytes']>12*1024**3:raise RuntimeError('native service memory hard-stop 12GiB')
                container_bounds=(2<<30)+(832<<20 if cfg.get('namespace_observer') else 0)
                if state['native_rss_bytes']+container_bounds+(1<<30)>16_000_000_000:raise RuntimeError('aggregate services exceed decimal16GB budget including CH, namespace observers, routers and reserve')
                for path in (base/'logs').glob('*.log'):
                    if path.stat().st_size>4*1024*1024:
                        with open(path,'r+b') as f:f.seek(-1024*1024,2);tail=f.read();f.seek(0);f.write(tail);f.truncate()
            try:conn,_=sock.accept()
            except TimeoutError:continue
            with conn:
                conn.settimeout(3)
                command=conn.recv(64).decode().strip()
                try:
                    if command=='pause':stop('ingest');paused=True
                    elif command=='resume':
                        if paused:start_ingest();paused=False
                    elif command=='shutdown':stopping=True
                    elif command not in ('status','snapshot'):raise ValueError('unknown control command')
                    conn.sendall(json.dumps(snapshot()).encode())
                except Exception as e:conn.sendall(json.dumps({'error':str(e)}).encode())
    finally:
        if deadline_handler is not None:capacity.campaign.disarm_deadline(deadline_handler)
        for kind in ('probe','observer','ingest','echo','prometheus'):stop(kind)
        sock.close();(base/'control.sock').unlink(missing_ok=True)
        if container:
            info=subprocess.run(docker+['inspect',name],capture_output=True,text=True)
            if info.returncode==0:
                labels=json.loads(info.stdout)[0]['Config']['Labels']
                if any(labels.get(k)!=v for k,v in {'project':'pathwatch','owner':'codex-pathwatch','task':'10'}.items()):raise RuntimeError('container ownership changed; refuse cleanup')
                subprocess.run(docker+['stop','--timeout','30',name],check=True)
                subprocess.run(docker+['rm',name],check=True)
            elif 'No such object' not in info.stderr and 'No such container' not in info.stderr:
                raise RuntimeError('cannot verify owned container cleanup')
        # data, journal, spool and raw logs deliberately survive cleanup.

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['run','pause','resume','status','snapshot','shutdown']);p.add_argument('--directory',default=str(ROOT/'.cache/task10-home'));p.add_argument('--config');a=p.parse_args()
    base=pathlib.Path(a.directory).resolve()
    if not base.is_relative_to(ROOT/'.cache') or base==ROOT/'.cache':p.error('state must be a dedicated directory inside project .cache')
    base.mkdir(parents=True,exist_ok=True,mode=0o700)
    if a.command=='run':
        if not a.config:p.error('--config required')
        cfg=json.loads(pathlib.Path(a.config).read_text())
        for key in ('probe_config','roster','spool_dir'):
            cfg[key]=str(pathlib.Path(cfg[key]).resolve())
            if not pathlib.Path(cfg[key]).is_relative_to(ROOT):p.error('all runtime paths must remain inside project')
        validate_probe(cfg,json.loads(pathlib.Path(cfg['probe_config']).read_text()),json.loads(pathlib.Path(cfg['roster']).read_text()))
        run(base,cfg)
    else:print(json.dumps(request(base,a.command),indent=2))
if __name__=='__main__':main()
