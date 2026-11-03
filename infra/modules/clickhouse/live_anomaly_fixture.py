#!/usr/bin/env python3
"""Owned native anomaly component proof. Historical inputs/storage are synthetic.

No clock manipulation, direct event INSERT in the engine database, extra Store
owner, downloads, or global cleanup. A separate storage table is explicitly a
synthetic encoding experiment, never engine event evidence or full admission.
"""
import argparse,base64,hashlib,http.server,json,os,pathlib,secrets,shutil,signal,socket,struct,subprocess,sys,tempfile,threading,time,urllib.request
import home,capacity
ROOT=home.ROOT
BIN=ROOT/'.cache/task10c3-bin/ingest'
EXPECTED='6f8e7090f13964c9a894eff06b8546148dcb070fd73d6a2c1cced24f3fbf8360'

class FixtureCancelled(BaseException):
    """Cooperative cancellation must bypass OSError network-retry handlers."""


def varint(n):
    out=bytearray()
    while n>127:out.append((n&127)|128);n>>=7
    out.append(n);return bytes(out)
def field(n,value):
    if isinstance(value,int):return varint(n*8)+varint(value)
    if isinstance(value,str):value=value.encode()
    return varint(n*8+2)+varint(len(value))+value

def result(at,ms,kind='tcp',router=''):
    b=field(1,'owned')+field(2,'home')+field(3,kind)+field(4,'v4')+field(5,'127.0.0.1')+field(6,field(1,at))
    if kind=='tcp':b+=field(7,field(1,'connect')+field(2,ms*1_000_000))
    if router:
        for ttl in range(1,5):b+=field(10,field(1,ttl)+field(2,router if ttl==4 else '192.0.2.1'))
    return b

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--port-base',type=int,required=True);a=p.parse_args()
    out=pathlib.Path(a.out).resolve()
    if out.exists() or not out.is_relative_to(ROOT):raise ValueError('new project output required')
    out.parent.mkdir(parents=True,exist_ok=True)
    if hashlib.sha256(BIN.read_bytes()).hexdigest()!=EXPECTED:raise ValueError('reviewed binary mismatch')
    old=json.loads((ROOT/'docs/evidence/raw/task10-capacity/verification-summary.json').read_text())['final_ch']
    if old['runtime_binary_sha256']['ingest']!=EXPECTED:raise ValueError('historical binary provenance mismatch')
    if subprocess.check_output(['git','diff','40b629e','HEAD','--','ingest','proto']):raise ValueError('ingest/proto changed from binary source')
    before=home.allocated();reserve=(512+16+16+16)*1024**2
    if before+reserve>capacity.SOFT_DISK:raise ValueError('fixture staged forecast exceeds soft limit')
    if not 1024<=a.port_base<=65531:raise ValueError('five high owned ports required')
    ports=tuple(range(a.port_base,a.port_base+5))
    for port in ports:
        with socket.socket() as s:s.bind(('127.0.0.1',port))
    work=pathlib.Path(tempfile.mkdtemp(prefix='task10c5-native-',dir=ROOT/'.cache'))
    name='pathwatch-task10c5-'+secrets.token_hex(6);container=None;attempted=False;child=None;sink=None;prom=None
    labels={'project':'pathwatch','owner':'codex-pathwatch','purpose':'task10c5-live-anomaly','run':name}
    report={'synthetic_history':True,'actual_elapsed_seconds':None,'source_base':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'binary_sha256':EXPECTED,'binary_source':'40b629e','binary_provenance':'docs/evidence/raw/task10-capacity/verification-summary.json:final_ch','ingest_proto_diff_empty':True,'initial_project_bytes':before,'reserve_bytes':reserve,'forecast_peak_bytes':before+reserve,'work':str(work),'commands':[],'samples':[],'alerts':[],'event_capacity':capacity.anomaly_forecast(),'phases':[]}
    started=time.monotonic();exitcode=1
    cancellations=[];report['cancellation_signals']=cancellations
    def checkpoint():
        if cancellations:raise FixtureCancelled(f'signal {cancellations[0]}')
    def save():out.write_text(json.dumps(report,indent=2)+'\n')
    def command(argv,check=True,**kw):
        r=subprocess.run(argv,capture_output=True,text=True,timeout=60,**kw)
        report['commands'].append({'argv':argv,'exit':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
        if check and r.returncode:raise RuntimeError('command failed: '+str(argv[:2])+r.stderr)
        return r
    password=secrets.token_hex(24);auth='Basic '+base64.b64encode(('pathwatch_ingest:'+password).encode()).decode()
    def query(sql):
        checkpoint()
        req=urllib.request.Request(f'http://127.0.0.1:{ports[0]}/?max_execution_time=30&max_threads=2',data=sql.encode(),headers={'Authorization':auth})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=35) as r:return r.read().decode().strip()
        except urllib.error.HTTPError as e:
            report.setdefault('sql_errors',[]).append({'sql':sql,'status':e.code,'body':e.read().decode()});raise
        finally:checkpoint()
    def sample(phase):
        values={'phase':phase,'elapsed':time.monotonic()-started,'project':home.allocated([work/'data',work/'journal']),'work':home.allocated_directory(work,[work/'data',work/'journal']),'data':home.allocated_directory(work/'data',[work/'data']),'parts':query("SELECT database,table,active,count() AS parts,sum(bytes_on_disk) AS bytes FROM system.parts WHERE database IN ('pathwatch','synthetic_events') GROUP BY database,table,active FORMAT JSONEachRow"),'merges':query("SELECT database,table,total_size_bytes_compressed FROM system.merges WHERE database IN ('pathwatch','synthetic_events') FORMAT JSONEachRow")}
        stats=json.loads(command(['docker','stats','--no-stream','--format','{{json .}}',container]).stdout)
        values['container_memory']=stats['MemUsage']
        if child and child.poll() is None:values['ingest_rss']=sum(int(line.split()[1])*1024 for line in pathlib.Path(f'/proc/{child.pid}/status').read_text().splitlines() if line.startswith('VmRSS:'))
        report['samples'].append(values);save()
        if values['project']>=capacity.SOFT_DISK or values['work']>=reserve or values['data']>=512<<20:raise RuntimeError('fixture guard reached, not capacity success')
        if time.monotonic()-started>900:raise TimeoutError('fixture exceeded15min')
    def stop():
        nonlocal child
        if child:
            child.terminate()
            try:child.wait(timeout=40)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
            report.setdefault('ingest_exits',[]).append(child.returncode);child=None
    cfg={'listen':f'127.0.0.1:{ports[1]}','development_cleartext':True,'clickhouse_port':ports[0],'metrics_port':ports[2],'anomaly':{'enabled':True,'warmup':2,'sigma_floor_ms':5,'alertmanager_url':f'http://127.0.0.1:{ports[3]}'}}
    def start(enabled=True):
        nonlocal child
        current={**cfg,'anomaly':cfg['anomaly'] if enabled else {'enabled':False}}
        args=home.ingest_args(work,current,BIN.parent)
        with (work/'ingest.log').open('ab') as log:child=subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'PATHWATCH_CLICKHOUSE_PASSWORD':password,'GOMAXPROCS':'2','GOMEMLIMIT':'256MiB'})
        try:home.wait_ingest_ready(child,ports[2],enabled)
        finally:checkpoint()
        report['phases'].append({'enabled':enabled,'pid':child.pid,'argv':args,'proc_cmdline':pathlib.Path(f'/proc/{child.pid}/cmdline').read_bytes().replace(b'\x00',b' ').decode()})
    def metrics():return home.fetch(f'http://127.0.0.1:{ports[2]}/metrics')
    def await_condition(fn,timeout=20):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            checkpoint()
            if child and child.poll() is not None:raise RuntimeError('ingest exited')
            if fn():return
            time.sleep(.2)
        raise TimeoutError('condition timed out')
    def send(batch,results):
        checkpoint()
        payload=field(1,batch)+field(2,'home')+field(3,field(1,int(time.time())))+b''.join(field(4,x) for x in results)
        body=b'\0'+struct.pack('>I',len(payload))+payload
        r=subprocess.run(['curl','--noproxy','*','--http2-prior-knowledge','--silent','--show-error','--max-time','20','-D',str(work/'grpc.headers'),'-H','content-type: application/grpc','-H','te: trailers','--data-binary','@-',f'http://127.0.0.1:{ports[1]}/pathwatch.v1.Ingest/Report'],input=body,capture_output=True,timeout=25)
        headers=(work/'grpc.headers').read_text();report.setdefault('grpc',[]).append({'batch':batch,'request_sha256':hashlib.sha256(body).hexdigest(),'request_bytes':len(body),'exit':r.returncode,'headers':headers,'response_hex':r.stdout.hex(),'stderr':r.stderr.decode()})
        if r.returncode or 'grpc-status: 0' not in headers.lower() or not r.stdout:raise RuntimeError('actual gRPC Report failed')
        checkpoint()
    class Sink(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
            code=503 if len(report['alerts'])==0 else 200
            report['alerts'].append({'path':self.path,'status':code,'body':json.loads(raw),'utc':time.time()})
            self.send_response(code);self.end_headers()
        def log_message(self,*args):pass
    def interrupt(signum,frame):
        # Never raise asynchronously: even the first signal may arrive inside
        # finally. Latch at most the two supported signal kinds; body-only
        # checkpoints abort after bounded I/O, while cleanup always completes.
        nonlocal exitcode
        if signum not in cancellations:cancellations.append(signum)
        exitcode=1;report['exit']=1
        report.setdefault('error',f'FixtureCancelled: signal {signum}')
    try:
        signal.signal(signal.SIGTERM,interrupt);signal.signal(signal.SIGINT,interrupt)
        (work/'data').mkdir();(work/'journal').mkdir()
        (work/'users.xml').write_text('<clickhouse><users><default remove="remove"/><pathwatch_ingest><password>'+password+'</password><networks><ip>::/0</ip></networks><profile>default</profile><quota>default</quota><access_management>1</access_management></pathwatch_ingest></users></clickhouse>');(work/'users.xml').chmod(0o600)
        argv=['docker','create','--pull=never','--name',name,'--user',f'{os.getuid()}:{os.getgid()}','--memory','2g','--memory-swap','2g','--cpus','2','--cap-drop','ALL','--security-opt','no-new-privileges','--log-opt','max-size=2m','--log-opt','max-file=1','--tmpfs',f'/var/log/clickhouse-server:rw,size=16m,uid={os.getuid()},gid={os.getgid()}','-p',f'127.0.0.1:{ports[0]}:8123','-v',f'{work}/data:/var/lib/clickhouse','-v',f'{work}/users.xml:/etc/clickhouse-server/users.d/pathwatch.xml:ro','-v',f'{ROOT}/infra/modules/clickhouse/clickhouse.xml:/etc/clickhouse-server/config.d/pathwatch.xml:ro']
        for k,v in labels.items():argv+=['--label',k+'='+v]
        argv+=['--entrypoint','clickhouse-server',home.IMAGE,'--config-file=/etc/clickhouse-server/config.xml'];attempted=True
        container=command(argv).stdout.strip();report['container_id']=container;command(['docker','start',container])
        for _ in range(60):
            try:
                if query('SELECT 1')=='1':break
            except OSError:time.sleep(.25)
        query('CREATE DATABASE pathwatch ENGINE=Atomic')
        report['old_parts_lifetime']=query("SELECT value FROM system.merge_tree_settings WHERE name='old_parts_lifetime'")
        if report['old_parts_lifetime']!='480':raise RuntimeError('durability setting changed')
        sink=http.server.HTTPServer(('127.0.0.1',ports[3]),Sink);threading.Thread(target=sink.serve_forever,daemon=True).start()
        start(False)
        base=int(time.time())//300*300-1800
        report['synthetic_window_start']=base
        results=[result(base+60,50),result(base+360,50),result(base+660,80),result(base+540,0,'trace','192.0.2.4'),result(base+660,0,'trace','192.0.2.9')]
        send('c5-synthetic-history',results)
        send('c5-synthetic-second-batch',[result(base+670,80)])
        report['raw_before_enable']=query('SELECT count(),uniqExact(tuple(batch_key,result_index)) FROM pathwatch.probe_results')
        stop();start()
        await_condition(lambda:len(report['alerts'])==1)
        report['metrics_failed_delivery']=metrics();report['state_pending']=json.loads((work/'journal/anomaly/state.json').read_text())
        report['engine_events']=json.loads('['+query('SELECT * FROM pathwatch.path_events FINAL FORMAT JSONEachRow').replace('\n',',')+']')
        if len(report['engine_events'])!=1 or report['engine_events'][0]['first_divergent_ttl']!=4 or not report['engine_events'][0]['path_changed']:raise RuntimeError('engine event/correlation mismatch')
        if not report['state_pending']['Pending'] or not report['state_pending']['Pending'][0]['Written']:raise RuntimeError('failed delivery not durably queued')
        sample('engine-pending');stop()
        time.sleep(6) # real retryMin=5s elapsed, no clock substitution
        start();await_condition(lambda:len(report['alerts'])==2)
        await_condition(lambda:not json.loads((work/'journal/anomaly/state.json').read_text())['Pending'])
        report['metrics_delivered']=metrics();report['state_delivered']=json.loads((work/'journal/anomaly/state.json').read_text())
        if report['alerts'][0]['body']!=report['alerts'][1]['body']:raise RuntimeError('restart changed notification identity')
        report['engine_count_after_restart']=query('SELECT count(),uniqExact(event_id) FROM pathwatch.path_events FINAL')
        stop();command(['docker','restart','--timeout','20',container])
        for _ in range(60):
            try:
                if query('SELECT 1')=='1':break
            except OSError:time.sleep(.25)
        start();await_condition(lambda:'pathwatch_anomaly_ticks_total 1' in metrics())
        report['state_second_restart']=json.loads((work/'journal/anomaly/state.json').read_text());report['metrics_second_restart']=metrics()
        if len(report['alerts'])!=2 or report['state_second_restart']!=report['state_delivered']:raise RuntimeError('restart replayed or changed committed cursor')
        send('c5-synthetic-history',results)
        report['raw_after_restart_duplicate']=query('SELECT count(),uniqExact(tuple(batch_key,result_index)) FROM pathwatch.probe_results')
        if report['raw_after_restart_duplicate']!=report['raw_before_enable']:raise RuntimeError('duplicate replay added raw rows')
        report['engine_event_ddl']=query('SHOW CREATE TABLE pathwatch.path_events FORMAT TabSeparatedRaw')
        report['engine_state_bytes']=(work/'journal/anomaly/state.json').stat().st_size
        sample('engine-restarted')
        # A short real scrape validates actual exporter cardinality and metric
        # presence. Historical full35d storage proof remains a separate gate.
        promcfg=work/'prometheus.yaml'
        promcfg.write_text('global:\n  scrape_interval: 1s\nscrape_configs:\n  - job_name: pathwatch-ingest\n    sample_limit: 16\n    static_configs:\n      - targets: ["127.0.0.1:'+str(ports[2])+'"]\n')
        promargs=[str(ROOT/'.tools/bin/prometheus'),'--config.file='+str(promcfg),'--web.listen-address=127.0.0.1:'+str(ports[4]),'--storage.tsdb.path='+str(work/'prometheus'),'--storage.tsdb.retention.time=35d','--storage.tsdb.retention.size=256MB','--storage.tsdb.wal-segment-size=16MB','--storage.tsdb.max-block-chunk-segment-size=16MB']
        with (work/'prometheus.log').open('wb') as log:prom=subprocess.Popen(promargs,stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'GOMAXPROCS':'2','GOMEMLIMIT':'256MiB'})
        home.wait_prometheus_ready(prom,ports[4],lambda:None,timeout=30)
        def promquery(q):return json.loads(home.fetch(f'http://127.0.0.1:{ports[4]}/api/v1/query?query='+urllib.parse.quote(q)))
        await_condition(lambda:bool(promquery('up')['data']['result']),timeout=30)
        report['prometheus']={'argv':promargs,'scraped':promquery('scrape_samples_scraped'),'up':promquery('up'),'anomaly':promquery('{__name__=~"pathwatch_anomaly_.*"}'),'tsdb':json.loads(home.fetch(f'http://127.0.0.1:{ports[4]}/api/v1/status/tsdb')),'allocated_bytes':home.allocated_directory(work/'prometheus',[work/'prometheus']),'wal_bytes':home.allocated_directory(work/'prometheus/wal',[work/'prometheus/wal']),'head_bytes':home.allocated_directory(work/'prometheus/chunks_head',[work/'prometheus/chunks_head']),'rss_bytes':sum(int(line.split()[1])*1024 for line in pathlib.Path(f'/proc/{prom.pid}/status').read_text().splitlines() if line.startswith('VmRSS:'))}
        if report['prometheus']['scraped']['data']['result'][0]['value'][1]!='12' or report['prometheus']['up']['data']['result'][0]['value'][1]!='1' or len(report['prometheus']['anomaly']['data']['result'])!=3:raise RuntimeError('actual anomaly scrape failed')
        sample('live-prometheus');prom.terminate();prom.wait(timeout=20);report['prometheus']['exit']=prom.returncode;prom=None
        shutil.rmtree(work/'prometheus');stop()
        original_state=(work/'journal/anomaly/state.json').read_bytes()
        cfg['anomaly']['h']=6
        try:
            start()
        except RuntimeError as e:
            report['startup_failure_propagated']=str(e)
            report['startup_failure_exit']=child.wait(timeout=5)
            if report['startup_failure_exit']!=1:raise RuntimeError('unexpected startup failure exit')
        else:raise RuntimeError('changed detector configuration incorrectly accepted')
        if (work/'journal/anomaly/state.json').read_bytes()!=original_state:raise RuntimeError('failed startup modified durable state')
        stop();cfg['anomaly']['h']=5
        # Storage-only table uses exact production schema, but high-entropy
        # values generated below are explicitly synthetic and not detector output.
        query('CREATE DATABASE synthetic_events ENGINE=Atomic')
        ddl=query('SHOW CREATE TABLE pathwatch.path_events FORMAT TabSeparatedRaw').replace('pathwatch.path_events','synthetic_events.path_events',1)
        query(ddl)
        n=capacity.anomaly_forecast()['physical_rows'];report['synthetic_storage_rows']=n
        for offset in range(0,n,20000):
            count=min(20000,n-offset)
            # Three physical identities, six literalv4 kinds and three DNS
            # targets each withv4/v6/unknown family:45 actual semantic keys.
            probes=['home'.ljust(16,'x'),'azure-a'.ljust(16,'x'),'azure-b'.ljust(16,'x')]
            targets=['owned'.ljust(16,'x')]*6+sum(([('dns-'+mode).ljust(16,'x')]*3 for mode in ('udp','tcp','doh')),[])
            kinds=['tcp','tls','http1','http2','http3','trace']+['dns']*9
            families=['v4']*6+['v4','v6','']*3
            def array(values):return '['+','.join("'"+v+"'" for v in values)+']'
            key=f"concat({array(probes)}[intDiv(number,15)%3+1],'→',{array(targets)}[number%15+1],'/',{array(kinds)}[number%15+1],'/',{array(families)}[number%15+1])"
            sql=f"""INSERT INTO synthetic_events.path_events SELECT hex(SHA256(toString(number))),{key},toDateTime({base}-number%if(intDiv(number,15)%3=2,577,10081)*300), reinterpretAsFloat64(bitOr(bitAnd(cityHash64(number),4503599627370495),4636737291354636288)),reinterpretAsFloat64(bitOr(bitAnd(cityHash64(number+1),4503599627370495),4636737291354636288)),reinterpretAsFloat64(bitOr(bitAnd(cityHash64(number+2),4503599627370495),4636737291354636288)),0.5,5,number%2,number%31,['trace latency has no independent latency path','missing same-destination trace snapshot','multiple or unknown destination addresses','missing or invalid trace snapshot','ambiguous path identity',''][number%6+1] FROM numbers({offset},{count})"""
            query(sql);report.setdefault('storage_sql',[]).append(sql);report.setdefault('storage_sql_sha256',[]).append(hashlib.sha256(sql.encode()).hexdigest());sample('storage-load-'+str(offset+count))
        report['storage_before_merge']=query('SELECT count(),uniqExact(event_id),max(length(path_key)),uniqExact(path_key) FROM synthetic_events.path_events')
        if report['storage_before_merge']!=f'{n}\t{n}\t44\t45':raise RuntimeError('synthetic 3x15 path coverage/encoding mismatch')
        report['storage_path_windows']=query('SELECT path_key,count(),min(window_start),max(window_start) FROM synthetic_events.path_events GROUP BY path_key ORDER BY path_key FORMAT JSONEachRow')
        query('OPTIMIZE TABLE synthetic_events.path_events FINAL');sample('storage-after-merge')
        report['storage_after_merge']=query('SELECT count(),uniqExact(event_id) FROM synthetic_events.path_events FINAL')
        if report['storage_after_merge']!=f'{n}\t{n}':raise RuntimeError('synthetic storage count mismatch')
        command(['docker','restart','--timeout','20',container])
        for _ in range(60):
            try:
                if query('SELECT 1')=='1':break
            except OSError:time.sleep(.25)
        report['storage_after_restart']=query('SELECT count(),uniqExact(event_id) FROM synthetic_events.path_events FINAL')
        if report['storage_after_restart']!=report['storage_after_merge']:raise RuntimeError('storage restart count mismatch')
        sample('storage-restarted')
        report['engine_count_final']=query('SELECT count(),uniqExact(event_id) FROM pathwatch.path_events FINAL')
        if report['engine_count_final']!='1\t1':raise RuntimeError('engine event persistence mismatch')
        exitcode=0
    except BaseException as e:report['error']=type(e).__name__+': '+str(e)
    finally:
        stop()
        if prom:
            prom.terminate()
            try:prom.wait(timeout=20)
            except subprocess.TimeoutExpired:prom.kill();prom.wait()
        if sink:sink.shutdown();sink.server_close()
        if (work/'ingest.log').exists():report['ingest_log']=(work/'ingest.log').read_text()
        if (work/'prometheus.log').exists():report['prometheus_log']=(work/'prometheus.log').read_text()
        try:
            absent=not attempted
            if attempted:
                inspected=command(['docker','inspect',container or name],check=False)
                if inspected.returncode:
                    if 'no such object' not in inspected.stderr.lower():raise RuntimeError('container absence unconfirmed')
                    absent=True
                else:
                    actual=json.loads(inspected.stdout)[0]
                    if (container and actual['Id']!=container) or actual['Name']!='/'+name or any(actual['Config']['Labels'].get(k)!=v for k,v in labels.items()):raise RuntimeError('foreign cleanup refused')
                    container=actual['Id'];command(['docker','rm','--force',container]);r=command(['docker','inspect',container],check=False)
                    absent=r.returncode!=0 and 'no such object' in r.stderr.lower()
            report['container_absent']=absent
            if absent:shutil.rmtree(work);report['synthetic_directory_removed']=not work.exists()
        except Exception as e:report['cleanup_error']=str(e);exitcode=1
        report['ports_closed']=[]
        for port in ports:
            with socket.socket() as s:report['ports_closed'].append(s.connect_ex(('127.0.0.1',port))!=0)
        if not all(report['ports_closed']) or cancellations:exitcode=1
        report['actual_elapsed_seconds']=time.monotonic()-started;report['exit']=exitcode;report['final_project_bytes']=home.allocated();save()
    print(json.dumps({'exit':exitcode,'output':str(out),'error':report.get('error')}));return exitcode
if __name__=='__main__':sys.exit(main())
