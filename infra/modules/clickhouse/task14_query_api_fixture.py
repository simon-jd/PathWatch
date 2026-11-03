#!/usr/bin/env python3
"""Finite owned query API proof; no campaign data or external targets.

The fixture owns exactly its labelled container, child PIDs and fresh directory.
Direct Store/event/parameter fixtures are explicitly synthetic; Report traffic
uses the actual gRPC service. All advertised availability slots are retained.
"""
import argparse,datetime,hashlib,http.client,http.server,json,os,pathlib,secrets,shutil,signal,socket,ssl,struct,subprocess,sys,tempfile,threading,time,urllib.request,urllib.parse
import home
ROOT=home.ROOT

def varint(n):
    b=bytearray()
    while n>127:b.append((n&127)|128);n>>=7
    b.append(n);return bytes(b)
def field(n,v):
    if isinstance(v,int):return varint(n*8)+varint(v)
    if isinstance(v,str):v=v.encode()
    return varint(n*8+2)+varint(len(v))+v
class Cancelled(BaseException):pass

def on_time(actual_offset,scheduled_offset):
    return 0<=actual_offset-scheduled_offset<=0.25

def payload_good(status,body,expected):
    try:return status==200 and json.loads(body)==expected
    except (ValueError,TypeError):return False

def interval_summary(attempts,scheduled):
    if len(attempts)>scheduled:raise ValueError('too many observations')
    good=sum(bool(x.get('good')) for x in attempts)
    return {'scheduled':scheduled,'good':good,'failures':scheduled-good,'missed':scheduled-len(attempts),'slo_achieved':False}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',required=True);parser.add_argument('--engine-only',action='store_true');args=parser.parse_args()
    out=pathlib.Path(args.out).resolve()
    if not out.is_relative_to(ROOT/'docs/evidence/raw/task14-query-api') or out.exists():raise ValueError('fresh owned evidence path required')
    baseline=home.allocated();reserve=180000000;cap=home.DISK_SHARE-400000000-222138114 # was 5,377,861,886 under the 6 GB share
    if baseline+reserve>cap:raise ValueError(f'fixture not admitted: {baseline}+{reserve}>{cap}')
    work=pathlib.Path(tempfile.mkdtemp(prefix='run-',dir=ROOT/'.cache/task14a'));name='pathwatch-task14a-'+secrets.token_hex(6)
    ports=[]
    for _ in range(7):
        with socket.socket() as s:s.bind(('127.0.0.1',0));ports.append(s.getsockname()[1])
    if len(set(ports))!=7:raise ValueError('port allocation collision')
    report={'source':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'work':str(work),'ports':ports,'baseline':baseline,'forecast':baseline+reserve,'cap':cap,'synthetic_engine_tests':True,'engine_only':args.engine_only,'commands':[],'samples':[],'availability':[],'fault_intervals':[],'cancellations':[],'exit':1}
    for p in ['.cache/task14a/bin/ingest','.cache/task14a/bin/store.test','proxy/target/release/edge-proxy','.tools/bin/prometheus']:
        report.setdefault('binary_sha256',{})[p]=hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
    report['source_diff']=subprocess.check_output(['git','diff','--stat'],text=True)
    container=None;attempted=False;children={};logs={};started=time.monotonic();stop_guard=threading.Event();guard_error=[];save_lock=threading.Lock()
    def save():
        with save_lock:
            out.parent.mkdir(parents=True,exist_ok=True)
            temporary=out.with_suffix('.tmp')
            temporary.write_text(json.dumps(report,indent=2)+'\n');temporary.replace(out)
    def checkpoint():
        if report['cancellations']:raise Cancelled('fixture canceled')
        if guard_error:raise RuntimeError(guard_error[0])
        if time.monotonic()-started>240:raise TimeoutError('fixture 240s watchdog')
    def cmd(argv,check=True,timeout=45,env=None,input=None):
        process=subprocess.Popen([str(a) for a in argv],stdout=subprocess.PIPE,stderr=subprocess.PIPE,stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,env=env)
        children['command']=process
        try:
            stdout,stderr=process.communicate(input=input,timeout=timeout)
            r=subprocess.CompletedProcess(argv,process.returncode,stdout,stderr)
        except subprocess.TimeoutExpired:
            process.kill();stdout,stderr=process.communicate(timeout=5)
            report['commands'].append({'argv':[str(a) for a in argv],'timeout':timeout,'stdout':stdout.decode(errors='replace'),'stderr':stderr.decode(errors='replace')});raise
        finally:children.pop('command',None)
        report['commands'].append({'argv':[str(a) for a in argv],'exit':r.returncode,'stdout':r.stdout.decode(errors='replace'),'stderr':r.stderr.decode(errors='replace')});save()
        if check and r.returncode:raise RuntimeError('command failed: '+str(argv[:2])+r.stderr.decode(errors='replace')[-2000:])
        return r
    def rss(pid):
        try:return next(int(x.split()[1])*1024 for x in pathlib.Path(f'/proc/{pid}/status').read_text().splitlines() if x.startswith('VmRSS:'))
        except (OSError,StopIteration):return 0
    def sample():
        disk=home.allocated([work/'data',work/'query-journal',work/'slow-journal',work/'journal',work/'prom'])
        data=home.allocated_directory(work/'data',[work/'data']) if (work/'data').exists() else 0
        r={'elapsed':time.monotonic()-started,'allocated':disk,'ch_data':data,'native_rss':{k:rss(p.pid) for k,p in list(children.items()) if p.poll() is None}}
        r['native_rss']['fixture']=rss(os.getpid())
        if container:
            x=subprocess.run(['docker','stats','--no-stream','--format','{{.MemUsage}}',container],capture_output=True,text=True,timeout=8)
            r['ch_memory']=x.stdout.strip();v=x.stdout.split('/')[0].strip();units={'KiB':1024,'MiB':1024**2,'GiB':1024**3,'B':1}
            for u,m in units.items():
                if v.endswith(u):r['ch_memory_bytes']=int(float(v[:-len(u)])*m);break
        report['samples'].append(r)
        if disk>=cap or data>=128000000 or sum(r['native_rss'].values())+r.get('ch_memory_bytes',0)>=4000000000:raise RuntimeError('resource guard reached')
        save()
    def guard():
        while not stop_guard.wait(1):
            try:sample()
            except Exception as e:guard_error.append(str(e));return
    def interrupt(n,frame):
        if n not in report['cancellations']:report['cancellations'].append(n)
    def get(url,timeout=3):
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url,timeout=timeout) as r:return r.read(1048577).decode()
    def sql(q):
        req=urllib.request.Request(f'http://127.0.0.1:{ports[0]}/?max_threads=2&max_execution_time=5',data=q.encode())
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=7) as r:return r.read(1048577).decode().strip()
    def wait(fn,seconds=25):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            checkpoint()
            try:
                if fn():return
            except (OSError,ValueError,http.client.HTTPException):pass
            time.sleep(.1)
        raise TimeoutError('readiness timed out')
    def spawn(key,argv):
        log=(work/(key+'.log')).open('ab');logs[key]=log
        children[key]=subprocess.Popen([str(a) for a in argv],stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'GOMAXPROCS':'2','GOMEMLIMIT':'256MiB','PATHWATCH_CLICKHOUSE_PASSWORD':''})
        report.setdefault('processes',[]).append({'kind':key,'pid':children[key].pid,'argv':[str(a) for a in argv]})
    def stop(key):
        p=children.get(key)
        if p is not None:
            if p.poll() is None:
                p.send_signal(signal.SIGINT if key=='proxy' else signal.SIGTERM)
                try:p.wait(timeout=40)
                except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)
            report.setdefault('process_exits',[]).append({'kind':key,'pid':p.pid,'exit':p.returncode});del children[key]
            if key in logs:logs.pop(key).close()
    def ingest():
        spawn('ingest',[ROOT/'.cache/task14a/bin/ingest','-development-cleartext','-listen',f'127.0.0.1:{ports[1]}','-metrics',f'127.0.0.1:{ports[2]}','-query-listen',f'127.0.0.1:{ports[3]}','-clickhouse-url',f'http://127.0.0.1:{ports[0]}','-clickhouse-user','default','-journal',work/'journal','-journal-max-bytes','1048576','-anomaly-enable','-anomaly-warmup','2','-anomaly-sigma-floor-ms','5','-alertmanager-url',f'http://127.0.0.1:{ports[6]}'])
        wait(lambda:get(f'http://127.0.0.1:{ports[2]}/readyz'))
    def send(batch,ms,at):
        result=field(1,'target')+field(2,'home')+field(3,'tcp')+field(4,'v4')+field(5,'127.0.0.1')+field(6,field(1,at)+field(2,123))+field(7,field(1,'connect')+field(2,ms*1000000))
        p=field(1,batch)+field(2,'home')+field(3,field(1,int(time.time())))+field(4,result);body=b'\0'+struct.pack('>I',len(p))+p
        r=cmd(['curl','--noproxy','*','--http2-prior-knowledge','--silent','--show-error','--max-time','5','-D',work/'grpc.headers','-H','content-type: application/grpc','-H','te: trailers','--data-binary','@-',f'http://127.0.0.1:{ports[1]}/pathwatch.v1.Ingest/Report'],input=body)
        headers=(work/'grpc.headers').read_text();report.setdefault('grpc',[]).append({'batch':batch,'request_sha256':hashlib.sha256(body).hexdigest(),'headers':headers,'response_hex':r.stdout.hex()})
        if 'grpc-status: 0' not in headers.lower():raise RuntimeError('Report not acknowledged')
    def proxy_request(path,trust=True):
        ctx=ssl.create_default_context(cafile=str(work/'cert.pem')) if trust else ssl.create_default_context()
        with socket.create_connection(('127.0.0.1',ports[4]),timeout=5) as raw:
            with ctx.wrap_socket(raw,server_hostname='query.task14a.test') as secure:
                secure.sendall(('GET '+path+' HTTP/1.1\r\nHost: query.task14a.test\r\nConnection: close\r\n\r\n').encode())
                response=http.client.HTTPResponse(secure);response.begin();body=response.read(1048577);return response.status,body
    signal.signal(signal.SIGTERM,interrupt);signal.signal(signal.SIGINT,interrupt)
    watcher=None;sink=None
    try:
        bad=cmd([ROOT/'.cache/task14a/bin/ingest','-development-cleartext','-query-listen','0.0.0.0:9081','-journal',work/'must-not-exist'],check=False)
        assert bad.returncode!=0 and not (work/'must-not-exist').exists()
        report['invalid_listener_before_store_verified']=True
        for d in ['data','journal','prom']:(work/d).mkdir()
        (work/'users.xml').write_text('<clickhouse><users><default><password></password><networks><ip>::/0</ip></networks><access_management>1</access_management></default></users></clickhouse>')
        sample()
        argv=['docker','create','--pull=never','--name',name,'--label','project=pathwatch','--label','owner=codex-pathwatch','--label','purpose=task14-query-api','--label','run='+name,'--user',f'{os.getuid()}:{os.getgid()}','--memory','2g','--memory-swap','2g','--cpus','2','--cap-drop','ALL','--security-opt','no-new-privileges','--log-opt','max-size=2m','--log-opt','max-file=1','--tmpfs',f'/var/log/clickhouse-server:rw,size=16m,uid={os.getuid()},gid={os.getgid()}','-p',f'127.0.0.1:{ports[0]}:8123','-v',f'{work}/data:/var/lib/clickhouse','-v',f'{work}/users.xml:/etc/clickhouse-server/users.d/task14.xml:ro','-v',f'{ROOT}/infra/modules/clickhouse/clickhouse.xml:/etc/clickhouse-server/config.d/pathwatch.xml:ro','--entrypoint','clickhouse-server',home.IMAGE,'--config-file=/etc/clickhouse-server/config.xml']
        attempted=True;container=cmd(argv).stdout.decode().strip();report['container']=container;cmd(['docker','start',container]);watcher=threading.Thread(target=guard,daemon=True);watcher.start()
        wait(lambda:sql('SELECT 1')=='1')
        report['mounts']=json.loads(cmd(['docker','inspect',container]).stdout)[0]['Mounts'];assert all(m['Type']=='bind' for m in report['mounts'])
        report['engine_limit_codes']=sql("SELECT code,name FROM system.errors WHERE code IN (158,159,160,307,396,372,373) FORMAT JSONEachRow")
        env={**os.environ,'PATHWATCH_QUERY_TEST_URL':f'http://127.0.0.1:{ports[0]}','PATHWATCH_QUERY_TEST_CONTAINER':container,'PATHWATCH_QUERY_TEST_WORK':str(work),'GOMAXPROCS':'2','GOMEMLIMIT':'256MiB'}
        cmd([ROOT/'.cache/task14a/bin/store.test','-test.run=^(TestQueryEngine|TestQueryHTTPSlowClient)$','-test.v','-test.timeout=70s'],env=env,timeout=80);checkpoint()
        if not args.engine_only:
            class Sink(http.server.BaseHTTPRequestHandler):
                def do_POST(self):
                    body=self.rfile.read(min(int(self.headers.get('Content-Length','0')),65536));report.setdefault('owned_local_notifications',[]).append(json.loads(body));self.send_response(200);self.end_headers()
                def log_message(self,*a):pass
            sink=http.server.ThreadingHTTPServer(('127.0.0.1',ports[6]),Sink);threading.Thread(target=sink.serve_forever,daemon=True).start()
            sql('CREATE DATABASE pathwatch ENGINE=Atomic');ingest()
            at=int(time.time())//300*300-600;send('warmup-one',10,at-600);send('warmup-two',10,at-300);send('one',10,at);send('two',300,at);send('one',10,at)
            stop('ingest');ingest();wait(lambda:sql('SELECT count() FROM pathwatch.path_events FINAL')=='1')
            cmd(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',work/'key.pem','-out',work/'cert.pem','-days','1','-subj','/CN=query.task14a.test','-addext','subjectAltName=DNS:query.task14a.test']);(work/'key.pem').chmod(0o600)
            (work/'proxy.toml').write_text(f'listeners = [{{addr="127.0.0.1:{ports[4]}", tls={{certs=[{{sni="query.task14a.test", cert="{work}/cert.pem", key="{work}/key.pem"}}]}}}}]\nroutes = [{{host="query.task14a.test",path_prefix="/v1/measurements/",cluster="query"}}]\nclusters = [{{name="query",endpoints=["http://127.0.0.1:{ports[3]}"]}}]\n[pool]\nmax_connections=4\nrequest_timeout_ms=6000\ndrain_timeout_ms=5000\n')
            spawn('proxy',[ROOT/'proxy/target/release/edge-proxy',work/'proxy.toml'])
            def timestamp(n):return datetime.datetime.fromtimestamp(n,datetime.timezone.utc).isoformat().replace('+00:00','Z')
            query=urllib.parse.urlencode({'path_key':'home→target/tcp/v4','from':timestamp(at),'to':timestamp(at+300)})
            routes=['/v1/measurements/'+route+'?'+query for route in ['raw','quantiles','events']]
            wait(lambda:proxy_request(routes[0])[0]==200)
            try:proxy_request(routes[0],False);raise AssertionError('untrusted TLS accepted')
            except ssl.SSLCertVerificationError:report['untrusted_tls_rejected']=True
            expected_payloads={}
            for route,count in zip(routes,[2,1,1]):
                status,body=proxy_request(route);value=json.loads(body);assert status==200 and value['count']==count
                if '/quantiles?' in route:assert value['data'][0]['samples']=='2' and value['data'][0]['p95_ms']==285.5
                if '/raw?' in route:assert sorted(row['phases_nanoseconds']['connect'] for row in value['data'])==['10000000','300000000'] and all(row['started_at'].endswith('.000000123Z') for row in value['data'])
                if '/events?' in route:assert value['data'][0]['p95_ms']==285.5 and value['data'][0]['path_key']=='home→target/tcp/v4'
                expected_payloads[route]=value
                report.setdefault('roundtrips',[]).append({'route':route,'status':status,'body':value})
            start=time.monotonic();report['availability_start_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
            for i in range(30):
                due=start+i;delay=due-time.monotonic()
                if delay>0:time.sleep(delay)
                checkpoint();entry={'slot':i,'scheduled_offset':i,'actual_offset':time.monotonic()-start,'route':routes[i%3],'good':False}
                try:
                    if not on_time(entry['actual_offset'],i):raise TimeoutError('missed scheduled slot')
                    status,body=proxy_request(entry['route']);data=json.loads(body);entry.update(status=status,body_sha256=hashlib.sha256(body).hexdigest());entry['good']=payload_good(status,body,expected_payloads[entry['route']])
                except (OSError,ValueError,http.client.HTTPException) as e:entry['error']=str(e)
                entry['elapsed']=time.monotonic()-due
                if entry['elapsed']>5:entry['good']=False
                report['availability'].append(entry);save()
            report['availability_end_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat();report['availability_summary']=interval_summary(report['availability'],30)
            if report['availability_summary']['good']!=30:raise AssertionError('owned healthy query interval had failed/missing slots')
            (work/'prom.yaml').write_text(f'global:\n  scrape_interval: 1s\nscrape_configs:\n  - job_name: task14-query-ingest\n    sample_limit: 16\n    static_configs:\n      - targets: ["127.0.0.1:{ports[2]}"]\n')
            spawn('prom',[ROOT/'.tools/bin/prometheus','--config.file='+str(work/'prom.yaml'),'--web.listen-address=127.0.0.1:'+str(ports[5]),'--storage.tsdb.path='+str(work/'prom'),'--storage.tsdb.retention.time=10m','--storage.tsdb.wal-segment-size=10MB','--log.level=warn'])
            def prom(q):return json.loads(get(f'http://127.0.0.1:{ports[5]}/api/v1/query?query='+urllib.parse.quote(q)))['data']['result']
            wait(lambda:bool(prom('up')));report['scrape']={'up':prom('up'),'samples':prom('scrape_samples_scraped'),'metrics':get(f'http://127.0.0.1:{ports[2]}/metrics')}
            assert report['scrape']['up'][0]['value'][1]=='1' and report['scrape']['samples'][0]['value'][1]=='16'
            # Predeclared distinct engine-restart failure interval: two attempts.
            fault={'name':'actual CH restart then stale Store','start_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scheduled':2,'attempts':[]};report['fault_intervals'].append(fault)
            cmd(['docker','restart','--timeout','10',container]);wait(lambda:sql('SELECT 1')=='1')
            for _ in range(2):
                status,body=proxy_request(routes[0]);fault['attempts'].append({'status':status,'body':body.decode(),'good':status==200});assert status==503
            fault['end_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
            absent={'name':'API process absent','start_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scheduled':1,'attempts':[]};report['fault_intervals'].append(absent)
            stop('ingest');status,body=proxy_request(routes[0]);absent['attempts'].append({'status':status,'body':body.decode(),'good':status==200});assert status in (502,503)
            absent['end_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
            ingest();wait(lambda:proxy_request(routes[0])[0]==200,35);status,body=proxy_request(routes[0]);assert payload_good(status,body,expected_payloads[routes[0]])
            report['reopen_restored']=True
        report['parts']=sql("SELECT database,table,active,count() AS parts,sum(bytes_on_disk) AS bytes FROM system.parts WHERE database IN ('pathwatch','querytest','queryslow') GROUP BY database,table,active FORMAT JSONEachRow")
        report['merges']=sql("SELECT database,table,total_size_bytes_compressed FROM system.merges WHERE database IN ('pathwatch','querytest','queryslow') FORMAT JSONEachRow")
        checkpoint();sample();report['exit']=0
    except BaseException as e:
        report['error']=type(e).__name__+': '+str(e);report['exit']=1
    finally:
        stop_guard.set()
        if watcher:watcher.join(timeout=12)
        for k in list(children):stop(k)
        if sink:sink.shutdown();sink.server_close()
        for log in logs.values():log.close()
        if attempted:
            inspected=cmd(['docker','inspect',container or name],check=False)
            if inspected.returncode==0:
                actual=json.loads(inspected.stdout)[0]
                if actual['Config']['Labels'].get('run')!=name:raise RuntimeError('refuse foreign cleanup')
                report['container_logs']=cmd(['docker','logs',actual['Id']],check=False).stdout.decode(errors='replace')[-65536:]
                cmd(['docker','rm','--force',actual['Id']]);report['container_absent']=cmd(['docker','inspect',actual['Id']],check=False).returncode!=0
        report['ports_closed']=[]
        for port in ports:
            with socket.socket() as s:s.settimeout(.2);report['ports_closed'].append(s.connect_ex(('127.0.0.1',port))!=0)
        for p in work.glob('*.log'):report.setdefault('logs',{})[p.name]=p.read_text(errors='replace')[-262144:]
        report['work_allocated_before_cleanup']=home.allocated_directory(work,[work/'data'])
        # Only this run's disposable engine state/TSDB/keys/config are removed.
        report['removed_owned_work']=str(work);shutil.rmtree(work);report['work_absent']=not work.exists()
        report['final_allocated']=home.allocated();report['elapsed']=time.monotonic()-started
        if 'availability_start_utc' in report:
            report['availability_summary']=interval_summary(report['availability'],30)
            attempts=report['availability']+sum([f['attempts'] for f in report['fault_intervals']],[])
            report['combined_declared_summary']=interval_summary(attempts,30+sum(f['scheduled'] for f in report['fault_intervals']))
        if report['cancellations'] or guard_error:report['exit']=1;report['guard_errors']=guard_error
        save()
    print(json.dumps({k:report.get(k) for k in ['exit','error','elapsed','availability_summary','container_absent','work_absent','ports_closed','final_allocated']}),flush=True)
    return report['exit']
if __name__=='__main__':sys.exit(main())
