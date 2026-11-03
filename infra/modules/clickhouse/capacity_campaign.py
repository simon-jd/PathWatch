"""One fixed local capacity campaign; no cloud/SLI/journal state mutation."""
import json,math,os,pathlib,re,stat,tempfile,time
SITES={'home-ubuntu':35*86400,'azure-a':35*86400,'azure-b':2*86400}

def load(base):
    path=base/'capacity-campaign.json'
    try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:return None
    with os.fdopen(fd) as f:
        s=os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_mode&0o777!=0o600:raise ValueError('campaign state must be owned regular0600')
        raw=f.read(16385)
        if len(raw)>16384:raise ValueError('campaign state too large')
        try:v=json.loads(raw)
        except ValueError as e:raise ValueError('corrupt capacity campaign') from e
    if v.get('version')!=1 or set(v.get('sites',{}))!=set(SITES) or not isinstance(v.get('home_starts'),int) or not 1<=v['home_starts']<=33:raise ValueError('invalid capacity campaign state')
    for k,duration in SITES.items():
        bounds=v['sites'][k]
        if not isinstance(bounds,dict) or not isinstance(bounds.get('start'),int) or bounds.get('end')!=bounds['start']+duration:raise ValueError('invalid fixed campaign bounds')
    if not isinstance(v.get('last_wall'),(int,float)) or not math.isfinite(v['last_wall']):raise ValueError('invalid campaign clock')
    return v

def prepare(base,campaign_id,plan_hash,starts,now):
    if not isinstance(now,(int,float)) or not math.isfinite(now):raise ValueError('unavailable wall clock')
    if not re.fullmatch('[A-Za-z0-9_.-]{1,64}',campaign_id) or not re.fullmatch('[0-9a-f]{64}',plan_hash):raise ValueError('campaign identity/hash required')
    if set(starts)!=set(SITES) or any(type(x)!=int for x in starts.values()):raise ValueError('explicit UTC start for every site required')
    if starts['home-ubuntu']!=starts['azure-a'] or not starts['home-ubuntu']<=starts['azure-b']<=starts['home-ubuntu']+33*86400:raise ValueError('A/home share35d and B48h must fit inside it')
    expected={k:{'start':starts[k],'end':starts[k]+d} for k,d in SITES.items()}
    existing=load(base)
    if existing is not None:
        if existing.get('campaign_id')!=campaign_id or existing.get('plan_sha256')!=plan_hash or existing['sites']!=expected:raise ValueError('campaign plan changed; cannot reset or extend deadlines')
        if now<existing['last_wall']-5:raise ValueError('campaign clock moved backward')
        if now>=expected['home-ubuntu']['start'] and not (base/'slo-state.json').is_file():raise ValueError('claimed campaign lacks SLI history after fixed start; refuse new empty epoch')
        remaining(existing,now)
        return existing
    if (base/'journal').exists() and any((base/'journal').iterdir()):raise ValueError('existing journal without capacity campaign; refuse reset')
    if min(starts.values())<now:raise ValueError('first campaign start cannot be in the past')
    return {'version':1,'campaign_id':campaign_id,'plan_sha256':plan_hash,'sites':expected,'last_wall':now,'home_starts':0,
            'restart_budget':'32 aggregate controlled probe restarts; home starts enforced, remote restarts require operator audit'}

def remaining(state,now):
    if not math.isfinite(now) or now<state['last_wall']-5:raise ValueError('invalid/backward campaign clock')
    if now>=state['sites']['home-ubuntu']['end']:raise ValueError('fixed campaign expired')
    return [max(0,math.ceil(v['end']-max(now,v['start']))) for k in ('home-ubuntu','azure-a','azure-b') for v in [state['sites'][k]]]

def persist(base,value,initial=False):
    raw=(json.dumps(value,sort_keys=True)+'\n').encode()
    if len(raw)>16384:raise ValueError('campaign state exceeds16KiB')
    fd,name=tempfile.mkstemp(prefix='.capacity-campaign-',dir=base)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(raw);f.flush();os.fsync(f.fileno())
        path=base/'capacity-campaign.json'
        if initial:os.link(name,path) # atomic publish; existing state can never be overwritten
        else:os.replace(name,path)
        directory=os.open(base,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        try:os.unlink(name)
        except FileNotFoundError:pass

def claim(base,draft,now):
    # Caller owns home owner.lock. A failed subsequent service startup consumes
    # this start and retains its original deadline; it is never rolled back.
    current=load(base)
    if current is not None:
        if any(current.get(k)!=draft.get(k) for k in ('campaign_id','plan_sha256','sites','home_starts')):raise ValueError('campaign changed since preflight')
        draft=current
    elif draft['home_starts']!=0:raise ValueError('campaign disappeared; refuse reset')
    elif min(v['start'] for v in draft['sites'].values())<now:raise ValueError('first actual campaign start is in the past')
    remaining(draft,now)
    if draft['home_starts']>=33:raise ValueError('home controlled restart budget exhausted')
    updated={**draft,'home_starts':draft['home_starts']+1,'last_wall':now};persist(base,updated,initial=current is None);return updated

def checkpoint(base,state,wall_start,mono_start):
    now=time.time()
    if abs((now-wall_start)-(time.monotonic()-mono_start))>5:raise ValueError('campaign clock discontinuity')
    remaining(state,now)
    saved=load(base)
    if saved is None or any(saved.get(k)!=state.get(k) for k in ('campaign_id','plan_sha256','sites','home_starts')):raise ValueError('campaign state changed during run')
    state={**state,'last_wall':now};persist(base,state);return state

def arm_deadline(state):
    # Linux home runner: interrupt even a blocked du/HTTP/control operation.
    # Probe SIGTERM in the owner's finally block cancels further measurements.
    import signal
    delay=state['sites']['home-ubuntu']['end']-time.time()
    if delay<=0:raise ValueError('fixed campaign expired')
    def expired(*_):raise TimeoutError('fixed capacity campaign deadline reached')
    old=signal.signal(signal.SIGALRM,expired)
    signal.setitimer(signal.ITIMER_REAL,delay)
    return old

def disarm_deadline(old):
    import signal
    signal.setitimer(signal.ITIMER_REAL,0)
    signal.signal(signal.SIGALRM,old)
