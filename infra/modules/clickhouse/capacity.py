#!/usr/bin/env python3
"""Explicit five-week-v1 admission. A forecast is not elapsed-time/SLO evidence."""
import argparse,datetime,fcntl,hashlib,json,os,pathlib,re,stat,subprocess,sys,time
import capacity_campaign as campaign
import anomaly_config
PROFILE='five-week-v1'
RECORD_CAP=131072
JOURNAL_CAP=1<<30
RECORD_STOP=RECORD_CAP*9//10
JOURNAL_STOP=JOURNAL_CAP*9//10
CH_CAP=768<<20
PROM_CAP=384<<20
SPOOL_CAP=64<<20
NATIVE_CAP=8_000_000_000
from disk_share import DISK_SHARE,SOFT_DISK,RUNTIME_DISK
MEMORY_CAP=16_000_000_000
# Actual encodings are bounded by Go boundary tests. 512/1472 include each
# repeated-result field tag/length; 128 covers the remaining Report envelope.
def ceildiv(n,d):return (n+d-1)//d

def forecast(seconds=None,restarts=32):
    seconds=[35*86400,35*86400,2*86400] if seconds is None else seconds
    if len(seconds)!=3 or any(type(x)!=int or x<0 for x in seconds) or not 0<=restarts<=32:raise ValueError("invalid remaining campaign budget")
    runs=3+restarts
    # Each new run may immediately perform a round. Minimum wait is 108s after
    # completion; ignoring positive work is conservative. One extra complete
    # combination per restart bounds independently phased target schedules.
    rounds=sum(s//108+1 for s in seconds)+restarts
    results=rounds*15
    # Per-run timer phases and final partial flush: <= floor(elapsed/60)+2.
    # Filled batches are additional, never assumed to coincide with timer ticks.
    records=sum(s//60 for s in seconds)+2*runs+ceildiv(results,128)
    payload=rounds*(5*512+1472+9*512)+records*128
    logical=4*ceildiv(payload,3)+records*(256+3) + (12<<20) # padding + one full DNS overflow payload
    allocated=logical+records*4095+(20<<20) # directories, owner, atomic temp; overflow payload above
    reserves={'journal':allocated,'clickhouse':CH_CAP,'prometheus':PROM_CAP,'spool':SPOOL_CAP,'logs_metadata':64<<20,'atomic_scan_accounting':(40<<20)+8192+16384}
    return {'profile':PROFILE,'days':[35,35,2],'remaining_seconds':seconds,'controlled_restarts':restarts,'overflow_record_reserve':128,'rounds':rounds,'results':results,'records':records,
            'journal_logical_bytes':logical,'journal_allocated_bytes':allocated,'additional_disk_bytes':sum(reserves.values()),
            'reserves':reserves,'healthy_freshness_budget_seconds':247.25,'retry_backoff_seconds':300,
            'physical_elapsed_verified':False,'slo_achieved':False}

def anomaly_forecast(retained_events=0,retained_windows=0,restarts=32):
    """Candidate for fixed 3x15 paths, including retained unprocessed windows.

    Does not grant admission. Existing events/windows must be counted from the
    owned database before any later combined proof; no TTL credit within35d.
    """
    for key,n in [('retained_events',retained_events),('retained_windows',retained_windows),('restarts',restarts)]:
        if type(n)!=int or n<0 or (key=='restarts' and n>32):raise ValueError('invalid anomaly '+key)
    # An arbitrary phase of a half-open site interval intersects <= floor(s/300)+1
    # windows. Restart retains the cursor/baseline; no extra full history replay.
    windows=[35*86400//300+1,35*86400//300+1,2*86400//300+1]
    logical=sum(windows)*15+retained_windows+retained_events
    # Each Run ticks immediately then every5m. Per tick the first uncertain
    # insert ends delivery; subsequent ticks may repeat that one physical row.
    # Home runs <=35d and33 launches. One extra uncertain insert per tick is
    # conservative even if none produced a new logical event; no exact-once claim.
    uncertain=35*86400//300+1+restarts
    rows=logical+uncertain
    # Profile IDs <=16 ASCII each; UTF8 arrow3 + two slashes2 + http3=5 + v4=2.
    # No trace arrays are stored: ID64, key44, DateTime4, five Float64=40,
    # Bool1, UInt16=2, reason<=49 plus varints/dictionary/index allowance.
    row_bytes=256
    # No unmeasured merge-ratio credit: each engine event is its own INSERT.
    # N leaves plus <N merging nodes bound total parts in a no-TTL merge tree.
    # 32KiB is a candidate metadata allowance per part, verified separately;
    # this deliberately refuses a fit claim from a compact batched fixture.
    encoded=rows*row_bytes
    parts=2*rows
    event_disk=3*encoded+parts*32768+(16<<20)
    return {'profile':'five-week-v1-anomaly-candidate','site_windows':windows,'max_paths':45,
            'logical_events':logical,'physical_rows':rows,'uncertain_insert_reserve':uncertain,
            'retained_events':retained_events,'retained_unprocessed_windows':retained_windows,
            'event_row_binary_bound':row_bytes,'event_part_count_bound':parts,'events_disk_candidate_bytes':event_disk,
            'state_atomic_bytes':2*(4<<20)+3*4096,'additional_exporter_series':3,
            'prometheus_exporter_samples':{'pathwatch-ingest':12,'pathwatch-home-probe':9,'pathwatch-observer':12},
            'prometheus_series_cap':128,'prometheus_total_bytes':PROM_CAP,'logs_metadata_bytes':64<<20,
            'ttl_days':180,'old_parts_lifetime_seconds':480,'combined_envelope_verified':False,
            'part_envelope_is_candidate':True,'physical_elapsed_verified':False,'slo_achieved':False}


def admission(allocated,records,journal,journal_allocated,block_size=4096,seconds=None,restarts=32,existing_reserves=None):
    f=forecast(seconds,restarts);errors=[]
    existing_reserves=existing_reserves or {}
    for key,value in existing_reserves.items():
        if key not in ("clickhouse","prometheus","spool","logs_metadata") or type(value)!=int or value<0:raise ValueError("invalid owned reserve accounting")
        if value>=f["reserves"][key]:errors.append(key+" existing allocation reaches reserve")
    f["additional_disk_bytes"]-=sum(min(v,f["reserves"][k]) for k,v in existing_reserves.items())
    f["existing_reserved_allocated_bytes"]=existing_reserves
    for value in (allocated,records,journal,journal_allocated):
        if not isinstance(value,int) or value<0:raise ValueError('nonnegative actual resource counters required')
    if block_size!=4096:errors.append('only verified 4096-byte filesystem allocation supported')
    if allocated+f['additional_disk_bytes']>SOFT_DISK:errors.append(f'project forecast exceeds {SOFT_DISK/1e9:.1f}GB;retire only approved regenerable tools or reduce existing footprint')
    if records+f['records']+f['overflow_record_reserve']>=RECORD_STOP:errors.append('existing plus forecast records reaches runtime guard')
    if journal+f['journal_logical_bytes']>=JOURNAL_STOP:errors.append('existing plus forecast journal reaches runtime guard')
    if journal_allocated+f['journal_allocated_bytes']>allocated+f['additional_disk_bytes']:errors.append('inconsistent allocated journal accounting')
    # Admission of resources alone cannot discharge the empirical envelope.
    return {**f,'current_allocated_bytes':allocated,'projected_allocated_bytes':allocated+f['additional_disk_bytes'],
            'resources_fit':not errors,'errors':errors,'long_deployment_ready':False,
            'verification_pending':['full-volume ClickHouse parts/merge envelope','full-volume Store.Open within 60s','Prometheus full synthetic retention/compaction envelope','physical 35-day/SLO observation','anomaly/events integration (C5)']}

def prometheus_config(source):
    jobs=re.findall(r'^  - job_name: (.+)$',source,re.M)
    if sorted(jobs)!=['pathwatch-home-probe','pathwatch-ingest','pathwatch-observer']:raise ValueError('capacity permits only three fixed PathWatch scrapes')
    return re.sub(r'(^  - job_name: .+$)',r'\1\n    sample_limit: 16',source,flags=re.M)


def runtime_reason(disk,records,journal,spool,ch,prom,native):
    for value,limit,label in [(disk,RUNTIME_DISK,'project allocated disk'),(records,RECORD_STOP,'journal records'),(journal,JOURNAL_STOP,'journal bytes'),(spool,SPOOL_CAP,'spool bytes'),(ch,CH_CAP,'ClickHouse allocated data'),(prom,PROM_CAP,'Prometheus allocated data')]:
        if value>=limit:return label+' capacity profile guard'
    if native>NATIVE_CAP:return 'native services capacity profile memory guard'
    return ''

def plan_configs(path,probe_binary):
    plan=json.loads(pathlib.Path(path).read_text())
    observers=plan.get('observers',[])
    if len(observers)!=3 or {o.get('site') for o in observers}!={'home-ubuntu','azure-a','azure-b'}:raise ValueError('exactly home-ubuntu, azure-a and remaining azure-b campaign required')
    ids=set();hashes={};normalized=[];starts={}
    for o in observers:
        if o.get('days')!=(2 if o['site']=='azure-b' else 35):raise ValueError('campaign must be 35d+35d+remaining B48h')
        source=pathlib.Path(o['config']).resolve()
        cfg=json.loads(subprocess.check_output([str(probe_binary),'-config',str(source),'-check-config'],text=True,timeout=5))
        if cfg.get('capacity_profile')!=PROFILE or cfg['probe_id'] in ids:raise ValueError('unique physical profile identities required')
        ids.add(cfg['probe_id']);hashes[str(source)]=hashlib.sha256(source.read_bytes()).hexdigest()
        stamp=o.get('start_utc','')
        try:starts[o['site']]=int(datetime.datetime.strptime(stamp,'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc).timestamp())
        except ValueError as e:raise ValueError('explicit start_utc YYYY-MM-DDTHH:MM:SSZ required') from e
        expected_end=datetime.datetime.fromtimestamp(starts[o['site']]+o['days']*86400,datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        if cfg.get('capacity_start_utc')!=stamp or cfg.get('capacity_deadline_utc')!=expected_end:raise ValueError('probe fixed window must match approved plan start/deadline')
        normalized.append({'site':o['site'],'days':o['days'],'start':starts[o['site']],'config':cfg})
    identity={"campaign_id":plan.get("campaign_id",""),"observers":sorted(normalized,key=lambda o:o["site"])}
    digest=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()).hexdigest()
    return hashes,identity["campaign_id"],digest,starts

def verify_envelope(e,binaries):
    f=forecast()
    if e.get('volume')!='full' or e.get('exit')!=0 or not all(e.get(k) for k in ('container_absent','synthetic_directory_removed','port_closed')):raise ValueError('complete cleaned full-volume evidence required')
    seed=e.get('seed',{});verified=e.get('verify',{})
    if seed.get('records')!=f['records'] or seed.get('rows')!=f['results']:raise ValueError('fixture does not cover forecast volume')
    if not seed.get('duplicate') or not verified.get('duplicate') or not verified.get('recovered'):raise ValueError('duplicate/recovery evidence required')
    if verified.get('paths')!=45 or verified.get('probes')!=3:raise ValueError('three probes and45 actual profile paths required')
    if any(verified.get(k)!=f['results'] for k in ('raw','unique','mv')):raise ValueError('raw/unique/MV completeness mismatch')
    if not 0<seed.get('open_seconds',61)<=60 or not 0<verified.get('recovery_open_seconds',61)<=60:raise ValueError('Store.Open exceeds envelope')
    samples=e.get('samples',[])
    if not samples or max(s['data_allocated'] for s in samples)>=CH_CAP:raise ValueError('ClickHouse exceeds measured merge envelope')
    if e.get('journal_allocated_bytes',f['journal_allocated_bytes']+1)>f['journal_allocated_bytes']:raise ValueError('journal allocation exceeds model')
    if e.get('source_sha256')!=source_hashes():raise ValueError('capacity evidence source changed')
    for name in ('probe','ingest'):
        if e.get('runtime_binary_sha256',{}).get(name)!=hashlib.sha256((binaries/name).read_bytes()).hexdigest():raise ValueError('capacity evidence runtime artifact changed: '+name)

def source_hashes():
    root=pathlib.Path(__file__).resolve().parents[3]
    names=('probe/cmd/probe/main.go','probe/cmd/probe/capacity.go','ingest/cmd/ingest/main.go','scripts/probe_home_observer.py','slo/observe.py','ingest/internal/server/server.go','docs/evidence/raw/task10-real-outage-20261024/operator-v2-healthy-baseline.json','ingest/internal/store/store.go','ingest/internal/store/capacity_fixture_test.go','infra/modules/clickhouse/clickhouse.xml','infra/modules/clickhouse/capacity_fixture.py','infra/modules/clickhouse/home.py','infra/modules/clickhouse/capacity.py','infra/modules/clickhouse/anomaly_config.py','infra/modules/clickhouse/capacity_campaign.py','infra/modules/clickhouse/capacity_prometheus_fixture.py','slo/prometheus.yaml','slo/generated.rules.yaml')
    return {name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}

def verify_prometheus_envelope(e,binaries):
    expected=128*35*86400//15
    if e.get('exit')!=0 or e.get('days')!=35 or e.get('series')!=128 or not e.get('process_absent') or not e.get('synthetic_directory_removed') or e.get('ports_closed')!=[True,True]:raise ValueError('complete cleaned35d synthetic Prometheus evidence required')
    if any(e.get(k)!=expected for k in ('accepted_samples','count_before_restart','count_after_restart')):raise ValueError('Prometheus accepted/restart sample counts mismatch')
    if not 60<=e.get('calibration_tsdb',{}).get('data',{}).get('headStats',{}).get('numSeries',0)<=128:raise ValueError('declared runtime cardinality not verified')
    expected_scrapes={'pathwatch-observer':12,'pathwatch-ingest':9,'pathwatch-home-probe':9}
    if not e.get('exporter_scrapes_verified') or {r['metric']['job']:float(r['value'][1]) for r in e.get('calibration_up',[])}!={k:1.0 for k in expected_scrapes} or {r['metric']['job']:int(float(r['value'][1])) for r in e.get('calibration_scraped',[])}!=expected_scrapes:raise ValueError('actual historical exporter replay not verified')
    if not e.get('samples') or max(v['allocated'] for v in e['samples'])>=PROM_CAP:raise ValueError('Prometheus allocation exceeds envelope')
    if e.get('source_sha256')!=source_hashes():raise ValueError('Prometheus evidence source changed')
    root=pathlib.Path(__file__).resolve().parents[3]
    if e.get('prometheus_sha256')!=hashlib.sha256((root/'.tools/bin/prometheus').read_bytes()).hexdigest():raise ValueError('Prometheus artifact changed')
    for name in ('probe','ingest'):
        if e.get('runtime_binary_sha256',{}).get(name)!=hashlib.sha256((binaries/name).read_bytes()).hexdigest():raise ValueError('capacity runtime artifact changed: '+name)


def inspect_spool(directory,resume):
    # Read-only under the existing spool lock: never recover, delete or reset.
    reserve=34025472
    def admitted(total):
        import home
        allocated=home.allocated_directory(directory) if directory.exists() else 0
        if total+reserve>SPOOL_CAP or allocated+reserve>SPOOL_CAP:raise ValueError('existing logical/allocated spool plus new backlog reserve exceeds64MiB')
        return {'committed_bytes':total,'allocated_bytes':allocated,'new_backlog_reserve':reserve,'dropped':0}
    if directory.is_symlink():raise ValueError('symlink spool refused')
    if not directory.exists():
        if resume:raise ValueError('claimed active campaign lacks spool history')
        return admitted(0)
    if directory.is_symlink() or not directory.is_dir():raise ValueError('spool must be an owned directory')
    if directory.stat().st_uid!=os.getuid():raise ValueError('foreign spool directory')
    def read(path,limit):
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as f:
            info=os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o777!=0o600 or info.st_size>limit:raise ValueError('spool requires bounded owned regular0600 files')
            raw=f.read(limit+1)
            if len(raw)>limit:raise ValueError('spool file exceeds bound')
            return raw
    lock=None
    try:
        names={p.name for p in directory.iterdir()}
        if '.lock' in names:
            read(directory/'.lock',0)
            lock=os.open(directory/'.lock',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if 'manifest.json' not in names:
            if resume or names-{'.lock'}:raise ValueError('unknown spool without manifest')
            return admitted(0)
        if lock is None:raise ValueError('spool manifest without owner lock')
        manifest=json.loads(read(directory/'manifest.json',16<<20))
        if not isinstance(manifest,dict) or set(manifest)!={'Version','Dropped','Entries'} or type(manifest['Version'])!=int or manifest['Version']!=1 or type(manifest['Dropped'])!=int or manifest['Dropped']!=0:raise ValueError('invalid/dropped spool history')
        entries=manifest['Entries']
        if entries is None:entries=[]
        if not isinstance(entries,list) or len(entries)>65536:raise ValueError('invalid spool entries')
        total=0;known={'.lock','manifest.json'}
        for entry in entries:
            if not isinstance(entry,dict) or set(entry)!={'ID','Size','SHA256'} or not isinstance(entry['ID'],str) or not re.fullmatch('[0-9a-f]{64}',entry['ID']) or type(entry['Size'])!=int or not 0<entry['Size']<=16<<20 or not isinstance(entry['SHA256'],str) or not re.fullmatch('[0-9a-f]{64}',entry['SHA256']):raise ValueError('invalid committed spool metadata')
            name=entry['ID']+'.batch'
            if name in known:raise ValueError('duplicate spool batch')
            known.add(name);raw=read(directory/name,16<<20)
            if len(raw)!=entry['Size'] or hashlib.sha256(raw).hexdigest()!=entry['SHA256']:raise ValueError('committed spool size/hash mismatch')
            batch=json.loads(raw)
            if not isinstance(batch,dict) or set(batch)!={'ID','Results'} or batch['ID']!=entry['ID'] or not isinstance(batch['Results'],list) or not 1<=len(batch['Results'])<=128:raise ValueError('invalid profile spool batch')
            total+=len(raw)
        if names!=known:raise ValueError('unknown/uncommitted spool files require operator review')
        if not resume and total:raise ValueError('new campaign cannot adopt old spool backlog')
        return admitted(total)
    finally:
        if lock is not None:os.close(lock)


def inspect_state(base):
    files=list((base/'journal').glob('*.json'))
    records=sum(p.name!='owner.json' for p in files)
    logical=sum(p.stat().st_size for p in files)
    allocated=sum(p.stat().st_blocks*512 for p in files)
    return records,logical,allocated

def preflight(base,cfg,binaries,allocated,*,launch_state=None,launch_resume=None):
    if not cfg.get('prometheus_port'):raise ValueError('capacity profile requires bounded Prometheus')
    if anomaly_config.normalize(cfg)['enabled']:raise ValueError('anomaly-enabled long capacity refused: C5 bounded component proof cannot replace full combined raw/MV/events/Prometheus envelope')
    if cfg.get('namespace_observer') and pathlib.Path(cfg['namespace_observer']['probe_binary']).resolve()!=(binaries/'probe').resolve():raise ValueError('namespace must execute the capacity-reviewed probe artifact')
    hashes,campaign_id,plan_hash,starts=plan_configs(cfg['capacity_plan'],binaries/'probe')
    now=time.time()
    if launch_state is None:
        if launch_resume is not None:raise ValueError('launch resume requires the current claimed state')
        state=campaign.prepare(base,campaign_id,plan_hash,starts,now)
        resume=state['home_starts']>0 and now>=state['sites']['home-ubuntu']['start']
    else:
        # Only home.run's same invocation, under owner.lock, may recheck its
        # already claimed future-start launch before the first SLI file exists.
        # CLI/new runs always use prepare, including its missing-SLI rejection.
        if type(launch_resume)!=bool or campaign.load(base)!=launch_state:raise ValueError('claimed campaign changed before launch')
        expected={site:{'start':starts[site],'end':starts[site]+duration} for site,duration in campaign.SITES.items()}
        if launch_state.get('campaign_id')!=campaign_id or launch_state.get('plan_sha256')!=plan_hash or launch_state['sites']!=expected:raise ValueError('campaign plan changed before launch')
        if launch_resume and not (base/'slo-state.json').is_file():raise ValueError('claimed campaign lacks SLI history before launch')
        state=launch_state;resume=launch_resume
    seconds=campaign.remaining(state,now)
    spool_state=inspect_spool(pathlib.Path(cfg['spool_dir']),resume)
    restarts=32-max(0,state['home_starts']-1)
    own=str(pathlib.Path(cfg['probe_config']).resolve())
    if own not in hashes:raise ValueError('monitored home config absent from capacity plan')
    import home
    paths={'clickhouse':base/'data','prometheus':base/'prometheus','spool':pathlib.Path(cfg['spool_dir']),'logs_metadata':base/'logs'}
    existing={k:home.allocated_directory(p) for k,p in paths.items() if p.exists()}
    report=admission(allocated,*inspect_state(base),block_size=os.statvfs(base if base.exists() else base.parent).f_frsize,seconds=seconds,restarts=restarts,existing_reserves=existing)
    report['campaign']=state
    report['resume']=resume
    report['spool_admission']=spool_state
    report['campaign_plan_sha256']=plan_hash
    report['config_sha256']=hashes
    report['scope']='raw/MV ingestion; anomaly disabled; events growth unverified (separate C5)'
    report['anomaly_enabled']=False
    report['anomaly_candidate']=anomaly_forecast()
    report['envelope_verified']=False
    if cfg.get('capacity_evidence'):
        evidence=json.loads(pathlib.Path(cfg['capacity_evidence']).read_text())
        verify_envelope(evidence,binaries)
        report['envelope_verified']=True
        report['verification_pending']=['Prometheus full synthetic retention/compaction envelope','physical 35-day/SLO observation']
    report['prometheus_envelope_verified']=False
    if cfg.get('capacity_prometheus_evidence'):
        verify_prometheus_envelope(json.loads(pathlib.Path(cfg['capacity_prometheus_evidence']).read_text()),binaries)
        report['prometheus_envelope_verified']=True
        if report['envelope_verified']:
            report['long_deployment_ready']=report['resources_fit']
            report['verification_pending']=['physical 35-day/SLO observation','anomaly/events capacity and integration (C5)']
    # These bounds include both routers and C2 observer containers, not merely
    # the Python wrapper. Native includes ingest/probe/echo/Prometheus/observer.
    report['resident_budget_bytes']=NATIVE_CAP+(2<<30)+(256<<20)+(64<<20)+2*(256<<20)+(1<<30)
    if report['resident_budget_bytes']>=MEMORY_CAP:raise ValueError('resident service budget exceeds decimal16GB')
    return report

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--directory',required=True);a=p.parse_args()
    import home
    cfg=json.loads(pathlib.Path(a.config).read_text());base=pathlib.Path(a.directory).resolve()
    binaries=home.reviewed_binaries(cfg)
    report=preflight(base,cfg,binaries,home.allocated());print(json.dumps(report,indent=2))
    return 0 if report['resources_fit'] and report['long_deployment_ready'] else 2
if __name__=='__main__':sys.exit(main())
