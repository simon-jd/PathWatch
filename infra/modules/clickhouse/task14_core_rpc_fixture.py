#!/usr/bin/env python3
"""Finite native split proof. Only exact owned children/container/work are touched."""
import argparse,base64,datetime,hashlib,http.client,http.server,json,os,pathlib,secrets,shutil,signal,socket,ssl,subprocess,threading,time,urllib.parse,urllib.request
ROOT=pathlib.Path(__file__).resolve().parents[3]
IMAGE='clickhouse/clickhouse-server@sha256:456063a689194186633bb3db0862283068f4c2ee538852d3aaffa7eb66f1f841'
OUTSIDE=2140313342
import sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
from disk_share import SOFT_DISK
CAP=SOFT_DISK-120126210 # was 5,479,873,790 under the 6 GB share

SCAN_RETRIES=[]
def allocated(path=ROOT):
    for attempt in range(3):
        r=subprocess.run(['du','-x','-B1','-s',str(path)],capture_output=True,text=True,timeout=5)
        if not r.returncode and not r.stderr:return int(r.stdout.split()[0])+(OUTSIDE if path==ROOT else 0)
        # Only disappearing files inside this fixture's fresh run tree may retry.
        owned=all('No such file or directory' in line and str(ROOT/'.cache/task14b/run-') in line for line in r.stderr.splitlines())
        SCAN_RETRIES.append({'attempt':attempt,'exit':r.returncode,'stderr':r.stderr})
        if not owned:raise RuntimeError('allocation scan failed: '+r.stderr)
        time.sleep(.02)
    raise RuntimeError('allocation scan retry bound: '+r.stderr)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--out',required=True);ap.add_argument('--bin',required=True);ap.add_argument('--compat-bin');args=ap.parse_args()
    out=pathlib.Path(args.out).resolve();binary=pathlib.Path(args.bin).resolve();helper=binary.parent/'fixture'
    if not out.is_relative_to(ROOT/'docs/evidence/raw/task14-core-rpc') or out.exists():raise ValueError('fresh owned evidence path required')
    baseline=allocated()
    if baseline+200000000>CAP:raise ValueError(f'not admitted: {baseline}+200MB>{CAP}')
    report={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'baseline':baseline,'forecast':baseline+200000000,'cap':CAP,'outside':OUTSIDE,'native_resident_increment_bound':4000000000,'series_forecast':39,'commands':[],'samples':[],'exit':1,'faults':[],'allocation_scan_retries':SCAN_RETRIES}
    name='pathwatch-task14b-'+secrets.token_hex(6);work=None;container=None;attempted=False;children={};logs={};ports=[];stop_guard=threading.Event();guard_errors=[];watcher=None;sink=None;started=time.monotonic();cancellations=[];query_threads=[];control=None
    def save():
        out.parent.mkdir(parents=True,exist_ok=True);tmp=out.with_suffix('.tmp');tmp.write_text(json.dumps(report,indent=2)+'\n');tmp.replace(out)
    def check():
        if guard_errors or cancellations:raise RuntimeError('guard/cancel: '+str(guard_errors+cancellations))
        if time.monotonic()-started>285:raise TimeoutError('native fixture watchdog')
    def command(argv,check_exit=True,timeout=45,env=None):
        argv=[str(x) for x in argv];p=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,start_new_session=True);children['command']=p
        try:
            try:stdout,stderr=p.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid,signal.SIGKILL);stdout,stderr=p.communicate(timeout=5);raise TimeoutError('command '+str(argv))
        finally:
            if p.poll() is None:os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=5)
            children.pop('command',None)
        item={'argv':argv,'exit':p.returncode,'stdout':stdout.decode(errors='replace'),'stderr':stderr.decode(errors='replace')};report['commands'].append(item)
        if check_exit and p.returncode:raise RuntimeError('command failed '+str(item))
        return item
    def fetch(url,data=None):
        req=urllib.request.Request(url,data=data)
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=5) as r:return r.read(1048577).decode()
    def sql(q):return fetch(f'http://127.0.0.1:{ports[0]}/?max_threads=2&max_execution_time=5',q.encode()).strip()
    def wait(fn,seconds=25):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            check()
            try:
                if fn():return
            except (OSError,ValueError,http.client.HTTPException) as e:report['last_wait_error']=repr(e)
            time.sleep(.1)
        raise TimeoutError('readiness; last proxy response='+str(report.get('last_proxy_response'))+'; '+str(report.get('last_wait_error')))
    def sample():
        n=allocated();rss={}
        for k,p in list(children.items()):
            if p.poll() is None:
                try:rss[k]=next(int(line.split()[1])*1024 for line in pathlib.Path(f'/proc/{p.pid}/status').read_text().splitlines() if line.startswith('VmRSS:'))
                except (OSError,StopIteration):pass
        data=allocated(work/'data') if work and (work/'data').exists() else 0
        report['samples'].append({'seconds':time.monotonic()-started,'allocated':n,'ch_data':data,'owned_native_rss':rss})
        # CH enforced2GiB plus native RSS leaves explicit margin inside4GB.
        if n>=CAP-5000000 or data>=128000000 or sum(rss.values())+2147483648>=4000000000:raise RuntimeError('native allocation guard')
    def guard():
        while not stop_guard.wait(.35):
            try:sample()
            except Exception as e:guard_errors.append(str(e));return
    def spawn(k,argv):
        check();log=(work/(k+'.log')).open('ab');logs[k]=log;p=subprocess.Popen([str(x) for x in argv],stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'GOMAXPROCS':'2','GOMEMLIMIT':'256MiB','PATHWATCH_CLICKHOUSE_PASSWORD':''},start_new_session=True);children[k]=p;report.setdefault('processes',[]).append({'kind':k,'pid':p.pid,'starttime':pathlib.Path(f'/proc/{p.pid}/stat').read_text().split()[21],'argv':[str(x) for x in argv]})
    def stop(k):
        p=children.pop(k,None)
        if p:
            if p.poll() is None:
                os.killpg(p.pid,signal.SIGINT if k=='proxy' else signal.SIGTERM)
                try:p.wait(timeout=40)
                except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=5)
            report.setdefault('process_exits',[]).append({'kind':k,'pid':p.pid,'exit':p.returncode})
        log=logs.pop(k,None)
        if log:log.close()
    def tls_args(role):return ['-core-ca',keys/'ca.pem','-core-cert',keys/(role+'.pem'),'-core-key',keys/(role+'.key'),'-core-server-name','core.test']
    def core_args(bin):
        return [bin,'-mode','core','-core-listen',f'127.0.0.1:{ports[1]}','-core-frontends','spiffe://pathwatch.local/frontend/stable,spiffe://pathwatch.local/frontend/candidate',*tls_args('core'),'-metrics',f'127.0.0.1:{ports[2]}','-clickhouse-url',f'http://127.0.0.1:{ports[13]}','-clickhouse-user','default','-journal',work/'journal','-journal-max-bytes','4194304','-anomaly-enable','-anomaly-warmup','2','-anomaly-sigma-floor-ms','5','-alertmanager-url',f'http://127.0.0.1:{ports[11]}']
    def core(bin=binary):
        spawn('core',core_args(bin))
        wait(lambda:fetch(f'http://127.0.0.1:{ports[2]}/readyz'))
    def front(i,bin=binary,target=None):
        base=3+i*3
        spawn('frontend'+str(i),[bin,'-mode','frontend','-listen',f'127.0.0.1:{ports[base]}','-tls-cert',keys/'frontend.pem','-tls-key',keys/'frontend.key','-metrics',f'127.0.0.1:{ports[base+1]}','-query-listen',f'127.0.0.1:{ports[base+2]}','-core-target',target or f'127.0.0.1:{ports[1]}',*tls_args('stable' if i==0 else 'candidate')])
        wait(lambda:fetch(f'http://127.0.0.1:{ports[base+1]}/readyz'))
    def send(batch,ms,at,i=0,sent=None):
        x=command([helper,'report','-dir',keys,'-target',f'127.0.0.1:{ports[3+i*3]}','-batch',batch,'-ms',ms,'-at',at]+(['-sent-at',sent] if sent else []));v=json.loads(x['stdout']);report.setdefault('reports',[]).append(v);return v
    def proxy_request(route):
        ctx=ssl.create_default_context(cafile=keys/'ca.pem')
        with socket.create_connection(('127.0.0.1',ports[9]),timeout=5) as raw:
            with ctx.wrap_socket(raw,server_hostname='query.test') as s:
                s.sendall(f'GET {route} HTTP/1.1\r\nHost: query.test\r\nConnection: close\r\n\r\n'.encode());response=http.client.HTTPResponse(s);response.begin();body=response.read(1048577);report['last_proxy_response']={'status':response.status,'body':body.decode(errors='replace')};return response.status,json.loads(body)
    def owner():return json.loads((work/'journal'/'owner.json').read_text())
    try:
        # Cleanup protection begins before the first owned allocation.
        work=pathlib.Path(__import__('tempfile').mkdtemp(prefix='run-',dir=ROOT/'.cache/task14b'));report['work']=str(work)
        signal.signal(signal.SIGTERM,lambda n,f:cancellations.append(n));signal.signal(signal.SIGINT,lambda n,f:cancellations.append(n))
        for _ in range(14):
            with socket.socket() as s:s.bind(('127.0.0.1',0));ports.append(s.getsockname()[1])
        if len(set(ports))!=len(ports):raise ValueError('port collision')
        report['ports']=ports
        for p in [binary,helper,ROOT/'proxy/target/release/edge-proxy',ROOT/'.tools/bin/prometheus']+([pathlib.Path(args.compat_bin)] if args.compat_bin else []):report.setdefault('binary_sha256',{})[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
        report['source']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
        keys=ROOT/'.cache/task14b/keys'/name;command([helper,'pki','-dir',keys]);report['private_key_backup']=str(keys)
        report['pki_hashes']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in keys.iterdir()}
        for d in ['data','journal','prom']:(work/d).mkdir()
        (work/'users.xml').write_text('<clickhouse><users><default><password></password><networks><ip>::/0</ip></networks><access_management>1</access_management></default></users></clickhouse>')
        attempted=True
        x=command(['docker','create','--pull=never','--name',name,'--label','project=pathwatch','--label','owner=codex-pathwatch','--label','purpose=task14-core-rpc','--label','run='+name,'--user',f'{os.getuid()}:{os.getgid()}','--memory','2g','--memory-swap','2g','--cpus','2','--cap-drop','ALL','--security-opt','no-new-privileges','--log-opt','max-size=2m','--log-opt','max-file=1','--tmpfs',f'/var/log/clickhouse-server:rw,size=16m,uid={os.getuid()},gid={os.getgid()}','-p',f'127.0.0.1:{ports[0]}:8123','-v',f'{work}/data:/var/lib/clickhouse','-v',f'{work}/users.xml:/etc/clickhouse-server/users.d/task14.xml:ro','-v',f'{ROOT}/infra/modules/clickhouse/clickhouse.xml:/etc/clickhouse-server/config.d/pathwatch.xml:ro','--entrypoint','clickhouse-server',IMAGE,'--config-file=/etc/clickhouse-server/config.xml'])
        container=x['stdout'].strip();report['container']=container;command(['docker','start',container]);watcher=threading.Thread(target=guard,daemon=True);watcher.start();wait(lambda:sql('SELECT 1')=='1')
        info=json.loads(command(['docker','inspect',container])['stdout'])[0];report['mounts']=info['Mounts'];assert all(m['Type']=='bind' for m in info['Mounts'])
        env={**os.environ,'PATHWATCH_CORE_CH':f'http://127.0.0.1:{ports[0]}','PATHWATCH_CORE_CONTAINER':container,'PATHWATCH_CORE_WORK':str(work),'GOMAXPROCS':'2','GOMEMLIMIT':'256MiB'}
        report['pending']=json.loads(command([helper,'pending','-dir',keys],env=env,timeout=80)['stdout'])
        control=work/'ch-relay-control'
        spawn('ch-relay',[helper,'ch-relay','-dir',keys,'-target',f'http://127.0.0.1:{ports[0]}','-listen',f'127.0.0.1:{ports[13]}','-marker',control])
        wait(lambda:socket_check(ports[13]))
        class Sink(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body=self.rfile.read(min(int(self.headers.get('Content-Length','0')),65536));report.setdefault('local_notifications',[]).append(json.loads(body));self.send_response(200);self.end_headers()
            def log_message(self,*a):pass
        sink=http.server.ThreadingHTTPServer(('127.0.0.1',ports[11]),Sink);threading.Thread(target=sink.serve_forever,daemon=True).start()
        sql('CREATE DATABASE pathwatch ENGINE=Atomic')
        bad=command([binary,'-mode','frontend','-journal',work/'forbidden-journal'],check_exit=False)
        assert bad['exit']!=0 and not (work/'forbidden-journal').exists()
        # A late metrics bind failure occurs after Store.Open and gRPC bind.
        # The subsequent real core must reopen the same owner and bind both ports.
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1',ports[2]));occupied.listen()
            bad=command(core_args(binary),check_exit=False)
            assert bad['exit']!=0 and not socket_check(ports[1])
        partial_owner=owner();core();assert owner()==partial_owner
        report['partial_startup_cleanup']={'frontend_owner_options_rejected':True,'core_metrics_bind_failure_closed_rpc_and_store':True,'same_owner_reopened':True}
        original=owner();front(0);front(1)
        at=int(time.time())//300*300-600
        for batch,ms,stamp,i in [('warmup-one',10,at-600,0),('warmup-two',10,at-300,1),('one',10,at,0),('two',300,at,1)]:assert send(batch,ms,stamp,i)['code']=='OK'
        def report_metrics():
            result={}
            for role,port in [('core',ports[2]),('frontend-0',ports[4]),('frontend-1',ports[7])]:
                result[role]={line.rsplit(' ',1)[0]:int(line.rsplit(' ',1)[1]) for line in fetch(f'http://127.0.0.1:{port}/metrics').splitlines() if line.startswith(('pathwatch_ingest_','pathwatch_frontend_report_'))}
            return result
        before=report_metrics()
        report['concurrent']=json.loads(command([helper,'exercise','-dir',keys,'-target',','.join(f'127.0.0.1:{ports[i]}' for i in [3,6,1]),'-at',at+300])['stdout'])
        after=report_metrics()
        semantic=[v for v in report['concurrent']['validation_matrix'] if v.get('semantic_wire')]
        expected={'pathwatch_ingest_accepted_batches_total':2,'pathwatch_ingest_duplicate_batches_total':3,'pathwatch_ingest_recovered_batches_total':0}
        for reason,count in [('invalid_request',2+sum(v['core_code']=='InvalidArgument' for v in semantic)),('clock_skew',1+sum(v['core_code']=='FailedPrecondition' for v in semantic)),('conflict',3),('capacity',0),('unavailable',0),('overloaded',0)]:expected['pathwatch_ingest_rejected_batches_total{reason="'+reason+'"}']=count
        deltas={role:{key:value-before[role][key] for key,value in metrics.items()} for role,metrics in after.items()}
        assert deltas['core']==expected,(deltas,expected)
        for role in ['frontend-0','frontend-1']:assert deltas[role]=={'pathwatch_frontend_report_requests_total{outcome="acknowledged"}':2,'pathwatch_frontend_report_requests_total{outcome="error"}':4+len(semantic)},deltas
        report['rejection_counter_ownership']={'before':before,'after':after,'deltas':deltas,'semantic_matrix_cases':len(semantic),'frontend_local_rejections_never_counted_core':True}

        stop('core');core();assert owner()==original;report['clean_serial_owner_preserved']=True;wait(lambda:sql('SELECT count() FROM pathwatch.path_events FINAL')=='1')
        (work/'proxy.toml').write_text(f'listeners = [{{addr="127.0.0.1:{ports[9]}", tls={{certs=[{{sni="query.test", cert="{keys}/query.pem", key="{keys}/query.key"}}]}}}}]\nroutes = [{{host="query.test",path_prefix="/v1/measurements/",cluster="query"}}]\nclusters = [{{name="query",endpoints=["http://127.0.0.1:{ports[5]}"]}}]\n[pool]\nmax_connections=4\nrequest_timeout_ms=6000\ndrain_timeout_ms=5000\n')
        spawn('proxy',[ROOT/'proxy/target/release/edge-proxy',work/'proxy.toml'])
        stamp=lambda n:datetime.datetime.fromtimestamp(n,datetime.timezone.utc).isoformat().replace('+00:00','Z')
        query=urllib.parse.urlencode({'path_key':'home→target/tcp/v4','from':stamp(at),'to':stamp(at+300)})
        routes=['/v1/measurements/'+x+'?'+query for x in ['raw','quantiles','events']];wait(lambda:proxy_request(routes[0])[0]==200)
        for route,n in zip(routes,[2,1,1]):
            status,body=proxy_request(route);assert status==200 and body['count']==n
            if '/raw?' in route:assert all(x['started_at'].endswith('.000000123Z') for x in body['data'])
            if '/quantiles?' in route:assert body['data'][0]['samples']=='2' and body['data'][0]['p95_ms']==285.5
            report.setdefault('query_roundtrips',[]).append({'route':route,'status':status,'body':body})
        # Hold real CH responses outside production code. The relay endpoint is
        # stable for the entire new fixture owner lifetime.
        def arm(ident,kind,batch=''):
            (control/'arm.json').write_text(json.dumps({'ID':ident,'Kind':kind,'Batch':batch}))
        def response(url):
            try:return {'status':200,'body':fetch(url)}
            except urllib.error.HTTPError as e:return {'status':e.code,'body':e.read(1048577).decode()}
        def unready(port):return response(f'http://127.0.0.1:{port}/readyz')['status']==503
        query_url=f'http://127.0.0.1:{ports[5]}'+routes[0]
        for kind,process,metric in [('frontend','frontend0',ports[4]),('core','core',ports[2])]:
            ident='drain-'+kind;arm(ident,'insert',ident)
            spawn(ident,[helper,'report','-dir',keys,'-target',f'127.0.0.1:{ports[3]}','-batch',ident,'-at',at+300])
            wait(lambda:(control/(ident+'.entered')).exists())
            pending_records={p.name:json.loads(p.read_text()) for p in (work/'journal').glob('*.json') if p.name!='owner.json' and not json.loads(p.read_text())['Committed']}
            assert len(pending_records)==1 and children[ident].poll() is None and (work/(ident+'.log')).stat().st_size==0
            gate_started=time.monotonic();gate=response(query_url);gate_elapsed=time.monotonic()-gate_started
            assert gate['status']==504 and 1.5<=gate_elapsed<3.5
            pid=children[process].pid;os.killpg(pid,signal.SIGTERM);wait(lambda:unready(metric),seconds=1)
            assert children[process].poll() is None and children[ident].poll() is None
            assert all(not json.loads((work/'journal'/name).read_text())['Committed'] for name in pending_records)
            contender=command(core_args(binary),check_exit=False)
            assert contender['exit']!=0 and 'journal already owned' in contender['stderr']
            (control/(ident+'.release')).write_text('release actual response\n')
            wait(lambda:children[ident].poll() is not None,seconds=3)
            stop(ident);ack=json.loads((work/(ident+'.log')).read_text());assert ack['code']=='OK' and ack['response']['status']==1
            assert all(json.loads((work/'journal'/name).read_text())['Committed'] for name in pending_records)
            stop(process)
            if process=='core':core()
            else:front(0)
            assert owner()==original
            assert response(query_url)['status']==200
            report.setdefault('durable_shutdown',[]).append({'mode':kind,'held_actual_response':json.loads((control/(ident+'.entered')).read_text()),'pending_before_release':pending_records,'gate_status':gate['status'],'gate_seconds':gate_elapsed,'no_ack_before_marker':True,'unready_while_alive':True,'owner_lock_retained':True,'ack':ack,'same_owner_reopened':True,'query_gate_reused':True})
        # A real read already executing through the core is canceled on SIGTERM.
        ident='drain-query';arm(ident,'query');answer={}
        def reading():
            try:answer.update(response(query_url))
            except Exception as e:answer.update(error=repr(e))
        thread=threading.Thread(target=reading);query_threads.append(thread);thread.start()
        wait(lambda:(control/(ident+'.entered')).exists());os.killpg(children['core'].pid,signal.SIGTERM)
        thread.join(timeout=3);assert not thread.is_alive() and answer.get('status') in [499,503,504]
        wait(lambda:(control/(ident+'.canceled')).exists());stop('core');core();assert owner()==original and response(query_url)['status']==200
        report['query_shutdown']={'actual_response_held':json.loads((control/(ident+'.entered')).read_text()),'answer':answer,'relay_observed_cancel':True,'same_owner_reopened':True,'query_gate_reused':True}
        # Copied new-fixture metadata cannot authorize another canonical path.
        alternate=work/'alternate-journal';alternate.mkdir();shutil.copyfile(work/'journal'/'owner.json',alternate/'owner.json')
        alternate_args=core_args(binary);alternate_args[alternate_args.index('-journal')+1]=alternate
        denied=command(alternate_args,check_exit=False);assert denied['exit']!=0 and 'owner mismatch' in denied['stderr']
        report['canonical_path_rejected']={'exit':denied['exit'],'stderr':denied['stderr'],'copied_owner':json.loads((alternate/'owner.json').read_text())}
        # Actual durable core ACK is lost at an explicitly lab-only mTLS relay.
        spawn('relay',[helper,'relay','-dir',keys,'-target',f'127.0.0.1:{ports[1]}','-listen',f'127.0.0.1:{ports[12]}','-marker',work/'lost-ack.marker'])
        wait(lambda:socket_check(ports[12]));stop('frontend1');front(1,target=f'127.0.0.1:{ports[12]}')
        sent=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
        first=send('lost-ack',10,at+300,1,sent);assert first['code']=='Unavailable' and (work/'lost-ack.marker').exists()
        retry=send('lost-ack',10,at+300,1,sent);assert retry['request']==first['request'];assert retry['code']=='OK' and retry['response']['status']==2
        assert sql("SELECT count() FROM pathwatch.probe_results WHERE batch_id='lost-ack'")=='1';report['durable_ack_loss_same_batch_duplicate']=True
        stop('frontend1');stop('relay');front(1)
        # Three real exporters, no self/process scrape or rules, exactly39 forecast.
        config='global:\n  scrape_interval: 1s\nscrape_configs:\n'
        for name_,port,limit in [('core',ports[2],12),('frontend-0',ports[4],6),('frontend-1',ports[7],6)]:config+=f'  - job_name: {name_}\n    sample_limit: {limit}\n    static_configs:\n      - targets: ["127.0.0.1:{port}"]\n'
        (work/'prom.yaml').write_text(config);spawn('prom',[ROOT/'.tools/bin/prometheus','--config.file='+str(work/'prom.yaml'),'--web.listen-address=127.0.0.1:'+str(ports[10]),'--storage.tsdb.path='+str(work/'prom'),'--storage.tsdb.retention.time=10m','--storage.tsdb.wal-segment-size=10MB','--log.level=warn'])
        prom=lambda q:json.loads(fetch(f'http://127.0.0.1:{ports[10]}/api/v1/query?query='+urllib.parse.quote(q)))['data']['result']
        wait(lambda:len(prom('up'))==3 and all(x['value'][1]=='1' for x in prom('up')))
        report['scrape']={'samples':prom('scrape_samples_scraped'),'series':prom('count({__name__=~".+"})'),'metrics':{name_:fetch(f'http://127.0.0.1:{port}/metrics') for name_,port in [('core',ports[2]),('frontend-0',ports[4]),('frontend-1',ports[7])]}}
        assert sorted(int(float(x['value'][1])) for x in report['scrape']['samples'])==[6,6,12]
        assert int(float(report['scrape']['series'][0]['value'][1]))==39
        # Stale core rejects after actual dedicated CH epoch change.
        command(['docker','restart','--timeout','5',container]);wait(lambda:sql('SELECT 1')=='1');status,body=proxy_request(routes[0]);assert status==503;report['faults'].append({'name':'stale core after CH restart','status':status,'body':body});stop('core');core();assert owner()==original;wait(lambda:proxy_request(routes[0])[0]==200)
        if args.compat_bin:
            compat=pathlib.Path(args.compat_bin).resolve();stop('frontend1');front(1,compat);assert send('newfront-oldcore',10,at+300,1)['code']=='OK';assert json.loads(fetch(f'http://127.0.0.1:{ports[8]}'+routes[0]))['count']==2
            stop('core');core(compat);assert owner()==original;assert send('oldfront-newcore',10,at+300,0)['code']=='OK';assert proxy_request(routes[0])[0]==200;report['compatibility']={'new_frontend_old_core':True,'old_frontend_new_core':True,'actual_reports_and_queries':True}
        report['owner']=owner();report['journal_records']={p.name:json.loads(p.read_text()) for p in (work/'journal').glob('*.json') if p.name!='owner.json'}
        report['parts']=sql("SELECT database,table,active,count() parts,sum(bytes_on_disk) bytes FROM system.parts WHERE database IN ('pathwatch','pending') GROUP BY database,table,active FORMAT JSONEachRow")
        report['docker_stats']=command(['docker','stats','--no-stream','--format','{{.MemUsage}}',container])['stdout'];check();sample();report['exit']=0
    except BaseException as e:report['error']=type(e).__name__+': '+str(e)
    finally:
        stop_guard.set()
        if watcher:watcher.join(timeout=6)
        if control and (control/'arm.json').exists():
            active=json.loads((control/'arm.json').read_text());(control/(active['ID']+'.release')).write_text('cleanup release\n')
        for k in list(children):stop(k)
        for thread in query_threads:
            thread.join(timeout=6)
            if thread.is_alive():guard_errors.append('owned query thread survived cleanup')
        if sink:sink.shutdown();sink.server_close()
        if attempted:
            x=command(['docker','inspect',container or name],check_exit=False)
            if x['exit']==0:
                info=json.loads(x['stdout'])[0]
                if info['Config']['Labels'].get('run')!=name:raise RuntimeError('foreign container cleanup refused')
                report['container_logs']=command(['docker','logs',info['Id']],check_exit=False)['stdout'][-65536:];command(['docker','rm','--force',info['Id']]);report['container_absent']=command(['docker','inspect',info['Id']],check_exit=False)['exit']!=0
        report['ports_closed']=[not socket_check(p) for p in ports]
        if work:
            for p in work.glob('*.log'):report.setdefault('logs',{})[p.name]=p.read_text(errors='replace')[-262144:]
            # Save fresh journal contents/provenance before disposing owned data.
            report['journal_snapshots']={str(p.relative_to(work)):p.read_text() for d in ['journal','pending-journal','alternate-journal'] for p in (work/d).glob('*.json')}
            report['work_allocated_before_cleanup']=allocated(work);shutil.rmtree(work);report['work_absent']=not work.exists()
        report['final_allocated']=allocated();report['elapsed']=time.monotonic()-started;report['cancellations']=cancellations;report['guard_errors']=guard_errors
        if cancellations or guard_errors:report['exit']=1
        save()
    print(json.dumps({k:report.get(k) for k in ['exit','error','elapsed','container_absent','work_absent','ports_closed','final_allocated']}));return report['exit']

def socket_check(port):
    with socket.socket() as s:s.settimeout(.1);return s.connect_ex(('127.0.0.1',port))==0
if __name__=='__main__':raise SystemExit(main())
