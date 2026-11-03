#!/usr/bin/env python3
"""A real 1200s outage of ONLY the owned home supervisor's ingest child.

Short --fixture-seconds runs are explicitly labelled fixtures, never acceptance.
The caller runs this asynchronously and polls progress.json/raw logs.
"""
import argparse,base64,hashlib,importlib.util,json,os,pathlib,signal,time,urllib.request
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('pathwatch_home',ROOT/'infra/modules/clickhouse/home.py');home=importlib.util.module_from_spec(spec);spec.loader.exec_module(home)

def exercise(control,out,seconds=1200,interval=30):
    out.mkdir(parents=True,exist_ok=True)
    samples=[]
    def capture():
        value=control('snapshot');value['elapsed_seconds']=round(time.monotonic()-start,3)
        samples.append(value);(out/f'sample-{len(samples):04}.json').write_text(json.dumps(value,indent=2)+'\n')
        (out/'progress.json').write_text(json.dumps({'elapsed_seconds':value['elapsed_seconds'],'required_seconds':seconds,'fixture':seconds!=1200,'samples':len(samples)})+'\n')
        print(json.dumps({'outage_seconds':value['elapsed_seconds'],'sample':len(samples)}),flush=True)
    start=time.monotonic()
    capture()
    try:
        control('pause')
        start=time.monotonic()  # starts AFTER stop has completed, not at signal send
        while True:
            capture()
            elapsed=time.monotonic()-start
            if elapsed>=seconds:break
            time.sleep(min(interval,seconds-elapsed))
    finally:
        control('resume') # restore even if sampling, sleep, SIGINT or SIGTERM fails
    return samples,time.monotonic()-start

def verify_recovery(base,cfg,samples,out):
    expected={};baseline=samples[0].get('spool_manifest') or {'Dropped':0}
    for sample in samples[1:]:expected.update(sample.get('spool_batches',{}))
    if not expected:raise RuntimeError('no actual spool backlog observed')
    deadline=time.monotonic()+660
    while True:
        snap=home.request(base,'snapshot')
        (out/'recovery.json').write_text(json.dumps(snap,indent=2)+'\n')
        manifest=snap.get('spool_manifest') or {}
        if manifest.get('Dropped',0)!=baseline.get('Dropped',0):raise RuntimeError('spool dropped batches')
        if not manifest.get('Entries'):break
        if time.monotonic()>deadline:raise RuntimeError('spool did not drain within 11 minutes')
        print('recovery waiting for durable ACK and spool drain',flush=True);time.sleep(15)
    auth='Basic '+base64.b64encode(('pathwatch_ingest:'+os.environ['PATHWATCH_CLICKHOUSE_PASSWORD']).encode()).decode()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def query(sql):
        req=urllib.request.Request(f'http://127.0.0.1:{cfg["clickhouse_port"]}/?database=pathwatch&max_execution_time=10&max_memory_usage=67108864&max_threads=1',data=sql.encode(),headers={'Authorization':auth})
        with opener.open(req,timeout=12) as r:return r.read(65536).decode().strip()
    for batch_id,batch in expected.items():
        key=hashlib.sha256((batch['probe_id']+'\0'+batch_id).encode()).hexdigest()
        record=json.loads((base/'journal'/f'{key}.json').read_text())
        if not record['Committed']:raise RuntimeError('batch not durably committed')
        count=int(query(f"SELECT count() FROM probe_results WHERE batch_key='{key}'"))
        if count!=batch['results']:raise RuntimeError('backlog row count mismatch')
    duplicates=int(query('SELECT count()-uniqExact(tuple(batch_key,result_index)) FROM probe_results'))
    if duplicates:raise RuntimeError('physical duplicate measurement rows')
    return {'backlog_batches_verified':len(expected),'physical_duplicate_rows':duplicates,'spool_drops':manifest.get('Dropped',0),'scope':'home-only; fixture alert presence does not prove healthy-to-firing transition; no external delivery'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--directory',default=str(ROOT/'.cache/task10-home'));p.add_argument('--config',required=True);p.add_argument('--out',required=True);p.add_argument('--fixture-seconds',type=float);a=p.parse_args()
    seconds=1200 if a.fixture_seconds is None else a.fixture_seconds
    if not 0<seconds<=1200:p.error('fixture seconds must be in (0,1200]')
    base=pathlib.Path(a.directory).resolve();out=pathlib.Path(a.out).resolve()
    if not base.is_relative_to(ROOT/'.cache') or not out.is_relative_to(ROOT/'.cache'):p.error('owned .cache runtime and evidence directory required')
    if out.exists() and any(out.iterdir()):p.error('use a new evidence directory')
    cfg=json.loads(pathlib.Path(a.config).read_text())
    def interrupted(*_):raise KeyboardInterrupt('outage interrupted; restoring ingest')
    signal.signal(signal.SIGTERM,interrupted)
    baseline=home.request(base,'snapshot')
    def firing(snapshot):
        return {x['labels']['alertname'] for x in snapshot.get('prometheus_alerts',{}).get('data',{}).get('alerts',[]) if x.get('state')=='firing' and x.get('labels',{}).get('severity')=='page'}
    required={'PathWatchFreshnessBudgetBurn','PathWatchReadinessBudgetBurn'}
    if a.fixture_seconds is None:
        if not cfg.get('prometheus_port') or baseline.get('prometheus_alerts',{}).get('status')!='success':p.error('real drill requires actual Prometheus alert observations')
        if firing(baseline)&required:p.error('wait for healthy baseline: target burn alert already firing')
    samples,elapsed=exercise(lambda cmd:home.request(base,cmd),out,seconds)
    proof=verify_recovery(base,cfg,samples,out)
    observed=set().union(*(firing(x) for x in samples[1:]))
    proof['burn_alerts_observed']=sorted(observed)
    if a.fixture_seconds is None and not required<=observed:raise RuntimeError('real outage did not observe both fast burn alerts firing')
    proof.update(actual_outage_seconds=elapsed,fixture=a.fixture_seconds is not None,real_20_minute_outage=elapsed>=1200 and a.fixture_seconds is None)
    (out/'result.json').write_text(json.dumps(proof,indent=2)+'\n');print(json.dumps(proof),flush=True)
if __name__=='__main__':main()
