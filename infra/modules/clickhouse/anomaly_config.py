"""One strict home/capacity anomaly configuration; ingest owns durable state."""
import ipaddress,math,urllib.parse

DEFAULTS={'warmup':20,'sigma_floor_ms':1.0,'k':0.5,'h':5.0,'max_paths':1024,'max_windows':128,'max_pending':64,'alertmanager_url':'http://127.0.0.1:9093'}

def normalize(cfg):
    if any(k in cfg for k in ('anomaly_enable','anomaly_enabled')):
        raise ValueError('anomaly aliases refused; use anomaly.enabled boolean')
    value=cfg.get('anomaly',{'enabled':False})
    if not isinstance(value,dict) or type(value.get('enabled')) is not bool or set(value)-({'enabled'}|set(DEFAULTS)):
        raise ValueError('anomaly requires enabled boolean and known options')
    if not value['enabled']:
        if set(value)!={'enabled'}:raise ValueError('disabled anomaly cannot contain ignored options')
        return {'enabled':False}
    value={**DEFAULTS,**value}
    for key,low,high in [('warmup',2,1000),('max_paths',1,4096),('max_windows',1,256),('max_pending',1,256)]:
        if type(value[key]) is not int or not low<=value[key]<=high:raise ValueError('invalid anomaly '+key)
    for key in ('sigma_floor_ms','k','h'):
        n=value[key]
        if type(n) not in (int,float) or not math.isfinite(n) or n<0 or (key!='k' and n==0):raise ValueError('invalid anomaly '+key)
    try:
        url=urllib.parse.urlsplit(value['alertmanager_url'])
        if url.scheme!='http' or url.username is not None or url.password is not None or url.path not in ('','/') or url.query or url.fragment or not ipaddress.ip_address(url.hostname).is_loopback or not url.port:raise ValueError()
    except (ValueError,TypeError,AttributeError):raise ValueError('anomaly alertmanager_url requires literal loopback http://IP:port base URL') from None
    return value

def arguments(cfg):
    value=normalize(cfg)
    if not value['enabled']:return []
    args=['-anomaly-enable']
    for key in DEFAULTS:
        flag='alertmanager-url' if key=='alertmanager_url' else 'anomaly-'+key.replace('_','-')
        args += ['-'+flag,str(value[key])]
    return args
