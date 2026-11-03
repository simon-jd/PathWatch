#!/usr/bin/env python3
"""Owned synthetic TSDB envelope, never real SLI history; no extra dependencies."""
import argparse,hashlib,http.server,json,os,pathlib,random,shutil,signal,socket,struct,subprocess,sys,tempfile,threading,time,urllib.parse,urllib.request
import capacity,home

def varint(n):
    out=bytearray()
    while n>127:out.append((n&127)|128);n>>=7
    out.append(n);return bytes(out)

def field(number,data):return varint(number*8+2)+varint(len(data))+data

def snappy_literal(raw):
    # Raw Snappy block with a single literal: compression is optional in format.
    n=len(raw)-1;size=max(1,(n.bit_length()+7)//8)
    tag=bytes([n<<2]) if n<60 else bytes([(59+size)<<2])+n.to_bytes(size,'little')
    return varint(len(raw))+tag+raw

def write_request(rng,first,steps):
    out=bytearray()
    for series in range(128):
        labels={'__name__':f'task10c3_synthetic_{series:03d}','job':'task10c3-synthetic','scope':'synthetic-not-sli','padding':'x'*128,'series_id':str(series)}
        ts=bytearray()
        for key,value in sorted(labels.items()):ts+=field(1,field(1,key.encode())+field(2,value.encode()))
        for sample in range(steps):
            bits=rng.getrandbits(52)|(rng.randrange(1,2047)<<52)
            ts+=field(2,b'\x09'+struct.pack('<Q',bits)+b'\x10'+varint((first+sample*15)*1000))
        out+=field(1,ts)
    return bytes(out)

def historical_metrics(root):
    """Replay complete historical real output; never a current health assertion."""
    name='docs/evidence/raw/task10-real-outage-20261024/operator-v2-healthy-baseline.json'
    raw=(root/name).read_bytes();snapshot=json.loads(raw)
    texts={kind:snapshot[key] for kind,key in [('observer','slo_metrics'),('ingest','ingest_metrics'),('probe','probe_metrics')]}
    counts={kind:sum(bool(line.strip()) and not line.startswith('#') for line in value.splitlines()) for kind,value in texts.items()}
    if counts!={'observer':12,'ingest':9,'probe':9}:raise ValueError('historical exporter sample count changed')
    # Formatter hashes at the commit that produced the snapshot; that commit is not in the published history.
    base='46714d60248a42cb90a39781d0a179b76edb8be1';formatters={}
    base_sha256={'probe/cmd/probe/main.go':'2b9c56677dc2f314fcb468298c8b55af7d6aa150569e8c4ff23e6f404f00faed',
                 'ingest/internal/server/server.go':'7c4f6d8b19434211465b14f0ad2d67440b2ef1a181b7dadfefb5a36c04ea3ec4',
                 'slo/observe.py':'418ae1cde91b4c62afccdb3746ad9ebcf61e7773792b7a8f96572fd717451fbf'}
    for name2,original in base_sha256.items():
        current=(root/name2).read_bytes()
        scope='whole file'
        if name2.startswith('probe/'):
            current=current[current.index(b'func metricsHandler('):current.index(b'func run(')];scope='metricsHandler function'
        # Record drift instead of freezing source: later edits must not break this replay.
        digest=hashlib.sha256(current).hexdigest()
        formatters[name2]={'scope':scope,'unchanged':digest==original,'current_sha256':digest,'base_sha256':original}
    return texts,{'snapshot':name,'snapshot_sha256':hashlib.sha256(raw).hexdigest(),'base':base,'formatters':formatters,'sample_counts':counts,'complete_texts':texts,'scope':'historical real output replay / current Prometheus calibration; not new live C3 health or SLI'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--days',type=int,choices=[1,35],default=35);a=p.parse_args()
    root=home.ROOT;out=pathlib.Path(a.out).resolve();binary=root/'.tools/bin/prometheus'
    if out.exists() or not out.is_relative_to(root):p.error('new project evidence required')
    before=home.allocated()
    if before+(512<<20)>capacity.SOFT_DISK:raise ValueError('Prometheus fixture512MiB forecast exceeds soft quota')
    for port in [19126,19127]:
        with socket.socket() as sock:sock.bind(('127.0.0.1',port))
    work=pathlib.Path(tempfile.mkdtemp(prefix='task10c3-prom-',dir=root/'.cache'));proc=None;server=None;monitor=None;done=threading.Event();scan_lock=threading.RLock();errors=[]
    report={'synthetic':True,'days':a.days,'series':128,'seed':1032026,'argv':sys.argv,'initial_project_bytes':before,'reserve_bytes':512<<20,'commands':[],'samples':[],'http_responses':{},'queries':[],'source_sha256':capacity.source_hashes(),'runtime_binary_sha256':{k:hashlib.sha256((root/'.cache/task10c3-bin'/k).read_bytes()).hexdigest() for k in ('probe','ingest')},'prometheus_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'work':str(work),'exit':1}
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));started=time.monotonic()
    def save():out.write_text(json.dumps(report,indent=2)+'\n')
    def get(path):
        try:
            with opener.open('http://127.0.0.1:19126'+path,timeout=60) as r:return r.read().decode()
        except urllib.error.HTTPError as e:
            body=e.read().decode();report.setdefault('http_errors',[]).append({'path':path,'status':e.code,'body':body})
            raise RuntimeError(f'HTTP{e.code}: {body}') from e
    def query(expression,stamp=None):
        params={'query':expression}
        if stamp is not None:params['time']=stamp
        value=json.loads(get('/api/v1/query?'+urllib.parse.urlencode(params)))
        report['queries'].append({'expression':expression,'time':stamp,'response':value})
        if value.get('status')!='success':raise RuntimeError('Prometheus query failed')
        return value['data']['result']
    def sample():
        data=work/'data';audit=[]
        v={'elapsed':time.monotonic()-started,'phase':report.get('phase'),'allocated':home.allocated_directory(data,[data],audit) if data.exists() else 0,'project_allocated':home.allocated([work],audit),'native_rss':0,'scan_audit':audit}
        for name in ['wal','chunks_head','wbl']:
            path=data/name;v[name]=home.allocated_directory(path,[path],audit) if path.exists() else 0
        for pid in [os.getpid(),proc.pid if proc and proc.poll() is None else 0]:
            if pid:v['native_rss']+=sum(int(line.split()[1])*1024 for line in pathlib.Path(f'/proc/{pid}/status').read_text().splitlines() if line.startswith('VmRSS:'))
        report['samples'].append(v)
        if v['allocated']>=capacity.PROM_CAP or v['project_allocated']>=capacity.RUNTIME_DISK or v['native_rss']>2<<30:raise RuntimeError('Prometheus fixture resource guard exceeded')
    def watch():
        while not done.wait(1):
            try:
                with scan_lock:sample()
            except Exception as e:
                errors.append(str(e))
                if proc and proc.poll() is None:proc.terminate()
                return
    def check():
        if errors:raise RuntimeError(errors[0])
        if time.monotonic()-started>1800:raise TimeoutError('Prometheus fixture exceeded30min')
    def stop():
        with scan_lock:stop_locked()
    def stop_locked():
        nonlocal proc
        if proc:
            proc.terminate()
            try:code=proc.wait(timeout=30)
            except subprocess.TimeoutExpired:proc.kill();proc.wait();raise
            report['commands'][-1]['exit']=code;proc=None
            if code:raise RuntimeError('Prometheus did not shut down cleanly')
    def start(config,receiver):
        nonlocal proc
        (work/'prometheus.yml').write_text(config)
        argv=[str(binary),'--config.file='+str(work/'prometheus.yml'),'--storage.tsdb.path='+str(work/'data'),'--web.listen-address=127.0.0.1:19126','--storage.tsdb.retention.time=35d','--storage.tsdb.retention.size=256MB','--storage.tsdb.wal-segment-size=16MB','--storage.tsdb.max-block-chunk-segment-size=16MB']
        if receiver:argv+=['--web.enable-remote-write-receiver']
        with (work/'prometheus.log').open('ab') as log:proc=subprocess.Popen(argv,stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'GOMAXPROCS':'2','GOMEMLIMIT':'1500MiB'})
        report['commands'].append({'argv':argv,'pid':proc.pid})
        for _ in range(100):
            check()
            if proc.poll() is not None:raise RuntimeError('Prometheus startup failed')
            try:
                if get('/-/ready').strip()=='Prometheus Server is Ready.':return
            except OSError:pass
            except RuntimeError as e:
                if not str(e).startswith('HTTP503:'):raise
            time.sleep(.1)
        raise TimeoutError('Prometheus readiness timed out')
    def interrupted(*_):raise InterruptedError('fixture interrupted')
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
    try:
        # Separate disposable calibration first; no current samples enter the
        # subsequent historical TSDB, so old synthetic writes cannot be rejected.
        report['phase']='declared-rule-cardinality'
        values,report['historical_metrics']=historical_metrics(root)
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                raw=values.get(self.path.lstrip('/'),'').encode()
                self.send_response(200);self.send_header('Content-Type','text/plain; version=0.0.4');self.end_headers();self.wfile.write(raw)
            def log_message(self,*_):pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',19127),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
        config=(root/'slo/prometheus.yaml').read_text().replace('/etc/pathwatch/generated.rules.yaml',str(root/'slo/generated.rules.yaml'))
        for port,kind in [(9465,'observer'),(9091,'ingest'),(9464,'probe')]:config=config.replace(f'      - targets: [127.0.0.1:{port}]',f'      - targets: [127.0.0.1:19127]\n    metrics_path: /{kind}')
        config=capacity.prometheus_config(config);report['calibration_config']=config;report['declared_series_bound']={'three_scrape_sample_limits':48,'scrape_metadata':15,'recording_rules':30,'alert_states_and_for_state':12,'total':105,'envelope':128}
        monitor=threading.Thread(target=watch,daemon=True);monitor.start();start(config,False)
        for _ in range(35):check();time.sleep(1)
        report['calibration_tsdb']=json.loads(get('/api/v1/status/tsdb'))
        report['calibration_series_query']=query('count({__name__!=""})')
        report['calibration_up']=query('up');report['calibration_scraped']=query('scrape_samples_scraped')
        observed={row['metric']['job']:int(float(row['value'][1])) for row in report['calibration_scraped']}
        expected={'pathwatch-observer':12,'pathwatch-ingest':9,'pathwatch-home-probe':9}
        if observed!=expected or {row['metric']['job']:float(row['value'][1]) for row in report['calibration_up']}!={key:1.0 for key in expected}:raise ValueError('actual exporter replay scrape limit failed')
        report['exporter_scrapes_verified']=True
        count=report['calibration_tsdb']['data']['headStats']['numSeries']
        if not 60<=count<=128:raise ValueError('declared calibration cardinality outside60..128')
        stop();server.shutdown();server.server_close();server=None
        with scan_lock:shutil.rmtree(work/'data')
        report['calibration_removed_before_history']=True
        report['phase']='synthetic35day-load';empty='global:\n  scrape_interval: 15s\nscrape_configs: []\n'
        start(empty,True);rng=random.Random(1032026);digest=hashlib.sha256();steps=a.days*86400//15;last=int(time.time())-15;first=last-(steps-1)*15;accepted=0
        report.update(first_timestamp=first,last_timestamp=last,expected_samples=128*steps,request_samples_per_series=120)
        for offset in range(0,steps,120):
            check();n=min(120,steps-offset);raw=write_request(rng,first+offset*15,n);digest.update(raw)
            request=urllib.request.Request('http://127.0.0.1:19126/api/v1/write',data=snappy_literal(raw),headers={'Content-Type':'application/x-protobuf','Content-Encoding':'snappy','X-Prometheus-Remote-Write-Version':'0.1.0'})
            try:
                with opener.open(request,timeout=60) as response:
                    status=str(response.status);response.read();report['http_responses'][status]=report['http_responses'].get(status,0)+1
            except urllib.error.HTTPError as e:
                body=e.read().decode();report.setdefault('http_errors',[]).append({'path':'/api/v1/write','status':e.code,'body':body})
                raise RuntimeError(f'remote-write HTTP{e.code}: {body}') from e
            if (offset+n)%5760==0:
                result=query('sum(count_over_time({__name__=~"task10c3_synthetic_.*"}[86400s]))',first+(offset+n-1)*15)
                counted=int(float(result[0]['value'][1]))
                if counted!=128*5760:raise ValueError('daily accepted sample count mismatch')
                accepted+=counted;report['accepted_samples']=accepted;save()
            # Bound acceleration: let normal2h head compaction catch up each4h.
            if (offset+n)%960==0:
                began=time.monotonic();target=(first+(offset+n-1)*15-3*3600)*1000
                while json.loads(get('/api/v1/status/tsdb'))['data']['headStats']['minTime']<target:
                    check()
                    if time.monotonic()-began>60:raise TimeoutError('head compaction backlog exceeded60s')
                    time.sleep(.2)
        report['request_stream_sha256']=digest.hexdigest()
        expression=f'sum(count_over_time({{__name__=~"task10c3_synthetic_.*"}}[{a.days*86400}s]))'
        report['count_before_restart']=int(float(query(expression,last)[0]['value'][1]));report['phase']='restart';stop();start(empty,True)
        report['count_after_restart']=int(float(query(expression,last)[0]['value'][1]))
        report['final_tsdb']=json.loads(get('/api/v1/status/tsdb'));sample()
        if report['count_before_restart']!=128*steps or report['count_after_restart']!=128*steps or accepted!=128*steps:raise ValueError('full accepted/restart counts mismatch')
        report['exit']=0
    except BaseException as e:
        report['error']=type(e).__name__+': '+str(e)
        if errors:report['monitor_errors']=errors
    finally:
        done.set()
        if monitor:monitor.join(timeout=25)
        try:stop()
        except Exception as e:report['cleanup_error']=str(e);report['exit']=1
        if server:server.shutdown();server.server_close()
        report['process_absent']=proc is None
        log=work/'prometheus.log'
        if log.exists():
            target=out.with_suffix('.log');shutil.copyfile(log,target);report['log_sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
        if report['process_absent']:shutil.rmtree(work);report['synthetic_directory_removed']=not work.exists()
        report['ports_closed']=[]
        for port in [19126,19127]:
            with socket.socket() as sock:report['ports_closed'].append(sock.connect_ex(('127.0.0.1',port))!=0)
        if not all(report['ports_closed']):report['exit']=1
        report['elapsed_seconds']=time.monotonic()-started;report['final_project_bytes']=home.allocated();save()
    print(json.dumps({'exit':report['exit'],'error':report.get('error'),'out':str(out)}));return report['exit']
if __name__=='__main__':sys.exit(main())
