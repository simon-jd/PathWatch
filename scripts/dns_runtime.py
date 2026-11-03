"""Strict owned DNS inventory, physical-origin attestation, bounded SSH execution."""
import datetime as dt
import fcntl
import os
import ipaddress
import json
from pathlib import Path
import re
import shlex
import subprocess
import time
from azure_dns_scope import REGIONS,IPS,ZONE,label

class RateLimit:
    def __init__(self,clock=time.monotonic,path=None,campaign=None,pending_seconds=0):self.clock=clock;self.last={};self.path=path;self.campaign=campaign;self.pending_seconds=pending_seconds
    def take(self,site,target):
        if self.path is not None:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with Path(str(self.path)+'.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                state=json.loads(self.path.read_text()) if self.path.exists() else {'last':{},'counts':{}}
                key=site+'|'+target;now=time.time();last=state['last'].get(key)
                if last is not None and now-last<60:raise ValueError('Public observer/target interval must be >=60 seconds across runs')
                count=state['counts'].get(self.campaign,0)
                if count>=8000:raise ValueError('80% DNS query reservation stop')
                state['last'][key]=now+self.pending_seconds;state['counts'][self.campaign]=count+1
                tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(state));os.replace(tmp,self.path)
            return
        now=self.clock();key=(site,target)
        if now-self.last.get(key,float('-inf'))<60:raise ValueError('Public observer/target interval must be >=60 seconds')
        self.last[key]=now
    def finish(self,site,target):
        if self.path is None:return
        with Path(str(self.path)+'.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            state=json.loads(self.path.read_text());state['last'][site+'|'+target]=time.time()
            tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(state));os.replace(tmp,self.path)
class Runtime:
    def __init__(self,path):
        self.root=Path(__file__).resolve().parents[1]
        path=Path(path).resolve()
        if not path.is_relative_to(self.root/'.cache'):raise ValueError('Private inventory must stay in project .cache')
        self.config=json.loads(path.read_text());c=self.config
        if c.get('scope')!='pathwatch-dns-owned' or c.get('task3_cleanup_confirmed') is not True or set(c.get('sites',{}))!={'a','b'}:raise ValueError('Exact owned DNS inventory required')
        sub=c.get('subscription_id','')
        if not re.fullmatch(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}',sub) or c.get('zone')!=ZONE or c.get('tm_name')!=label(sub) or c.get('tm_fqdn')!=label(sub)+'.trafficmanager.net':raise ValueError('Foreign DNS/TM inventory')
        self.expiry=dt.datetime.strptime(c['ttl'],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
        owner=ipaddress.ip_network(c['owner_cidr'],strict=True)
        if owner.version!=4 or owner.prefixlen!=32 or not owner.network_address.is_global:raise ValueError('Owned home global /32 required')
        for site,region in REGIONS.items():
            v=c['sites'][site];ip=ipaddress.ip_address(v['public_ip'])
            if not ip.is_global or ip.version!=4 or v.get('region')!=region or v.get('private_ip')!=IPS[site] or v.get('vm_id')!=f'/subscriptions/{sub}/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Compute/virtualMachines/vm-pathwatch-dns-{site}':raise ValueError('Foreign site inventory')
        if len({v['public_ip'] for v in c['sites'].values()}|{str(owner.network_address)})!=3:raise ValueError('Three distinct physical observer addresses required')
        self.key=self.private_path(c['ssh_private_key']);self.known=self.private_path(c['known_hosts'])
        if self.key.stat().st_mode&0o077:raise ValueError('Private key must exclude group/other')
        campaign=c.get('campaign_id','')
        if not re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}',campaign):raise ValueError('Reserved campaign UUID required')
        self.require_reservation()
        self.rate=RateLimit(path=self.root/'.cache/dns-public-rate.json',campaign=campaign,pending_seconds=15)
        os.environ['DOCKER_CONFIG']=str(self.root/'.cache/docker')
    def require_reservation(self):
        import azure_budget as budget
        if os.environ.get('AZURE_SUBSCRIPTION_ID')!=self.config['subscription_id']:raise ValueError('Explicit subscription must match DNS inventory')
        with budget.ledger_lock():
            ledger=json.loads(budget.LEDGER.read_text())
            budget.dns_budget.active_reservation(ledger,self.config['campaign_id'],self.config['ttl'],dt.datetime.now(dt.timezone.utc),budget,self.config['subscription_id'])
    def private_path(self,path):
        p=Path(path).resolve()
        if not p.is_relative_to(self.root/'.cache') or not p.is_file():raise ValueError('Existing credentials/config must stay in project .cache')
        return p
    def run(self,site,args,timeout=15,input=None,cleanup=False):
        if not 0<timeout<=900:raise ValueError('Bound command timeout <=900s')
        if site=='home':command=list(args)
        elif site in REGIONS:
            if cleanup and list(args) not in (['sudo','systemctl','start','pathwatch-dns-http.service'],['sudo','systemctl','is-active','--quiet','pathwatch-dns-http.service']):raise ValueError('Usage bypass only exact primary restore')
            # A guest-side watchdog kills the full command process group if usage
            # crosses the bound, even if SSH is interrupted or the controller dies.
            if cleanup:
                remote=['timeout','--signal=TERM','--kill-after=3s',str(max(1,timeout-4))+'s',*args]
            else:
                usage=(self.root/'scripts/hybrid_usage.py').read_text()
                guard="import datetime,json,os,signal,subprocess,sys,time\nns={'__name__':'dns_usage'}\nexec("+repr(usage)+",ns)\nexpiry=datetime.datetime.fromisoformat("+repr(self.config['ttl'].replace('Z','+00:00'))+")\ndef check():\n ns['enforce'](ns['snapshot'](),egress_gb=.25,transactions=1000000)\n if datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=90)>=expiry:raise ValueError('TTL cleanup margin')\ndef cancelled(signum,frame):raise KeyboardInterrupt('Guest command cancelled')\nfor sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):signal.signal(sig,cancelled)\ncheck()\np=subprocess.Popen("+repr(list(args))+",start_new_session=True)\ntry:\n deadline=time.monotonic()+"+str(max(1,timeout-4))+"\n while p.poll() is None:\n  check()\n  if time.monotonic()>=deadline:raise TimeoutError('Guest command deadline')\n  time.sleep(1)\n if p.returncode==0:check()\n sys.exit(p.returncode)\nfinally:\n if p.poll() is None:\n  os.killpg(p.pid,signal.SIGTERM);time.sleep(1)\n  if p.poll() is None:os.killpg(p.pid,signal.SIGKILL)\n"
                remote=['sudo','-n','python3','-c',guard]
            command=['ssh','-F','/dev/null','-o','IdentitiesOnly=yes','-o','GlobalKnownHostsFile=/dev/null','-o','BatchMode=yes','-o','ConnectTimeout=5','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(self.known),'-i',str(self.key),'pathwatch@'+self.config['sites'][site]['public_ip'],shlex.join(remote)]
        else:raise ValueError('Unknown physical observer')
        return subprocess.run(command,text=True,input=input,capture_output=True,timeout=timeout,check=True).stdout
    def attest(self):
        source="import json,urllib.request\nr=urllib.request.Request('http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01',headers={'Metadata':'true'})\nopener=urllib.request.build_opener(urllib.request.ProxyHandler({}))\nwith opener.open(r,timeout=3) as response: print(response.read().decode())\n"
        identities={}
        for site in REGIONS:
            actual=json.loads(self.run(site,['python3','-'],input=source))
            if actual.get('resourceId','').lower()!=self.config['sites'][site]['vm_id'].lower() or actual.get('location')!=REGIONS[site] or not actual.get('vmId'):raise ValueError('Physical observer IMDS mismatch')
            identities[site]=actual['vmId']
        if identities['a']==identities['b']:raise ValueError('Observers must be distinct VMs')
        return identities
    def usage(self):
        source=(self.root/'scripts/hybrid_usage.py').read_text()
        return {site:json.loads(self.run(site,['python3','-'],timeout=10,input=source)) for site in REGIONS}
    def observe(self,site):
        self.rate.take(site,self.config['tm_fqdn'])
        source=(self.root/'scripts/dns_observe.py').read_text()
        try:value=json.loads(self.run(site,['python3','-',site,self.config['tm_fqdn']],input=source))
        finally:self.rate.finish(site,self.config['tm_fqdn'])
        if value.get('site')!=site or value.get('target')!=self.config['tm_fqdn']:raise ValueError('Observation origin/target mismatch')
        owned={v['public_ip'] for v in self.config['sites'].values()}
        if not set(value.get('answers',[]))<=owned:raise ValueError('TM answer outside owned endpoints')
        return value
    def online(self,site):
        p=subprocess.run(['az','network','traffic-manager','endpoint','show','--subscription',self.config['subscription_id'],'--resource-group','rg-pathwatch-lab','--profile-name',self.config['tm_name'],'--name',site,'--type','azureEndpoints','--only-show-errors','-o','json'],capture_output=True,text=True,timeout=15,check=True)
        value=json.loads(p.stdout)
        target=f"/subscriptions/{self.config['subscription_id']}/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Network/publicIPAddresses/pip-pathwatch-dns-{site}"
        if value.get('targetResourceId','').lower()!=target.lower() or value.get('endpointStatus')!='Enabled' or value.get('endpointMonitorStatus')!='Online':raise ValueError('Owned backup endpoint must be Online')
        return value['endpointMonitorStatus']
    def http_backup(self):
        self.rate.take('home',self.config['sites']['b']['public_ip'])
        # Exact owned backup root only; no redirect or user-supplied path.
        try:output=self.run('home',['curl','--noproxy','*','--fail','--silent','--show-error','--connect-timeout','3','--max-time','5','--max-filesize','128','http://'+self.config['sites']['b']['public_ip']+'/'],timeout=8)
        finally:self.rate.finish('home',self.config['sites']['b']['public_ip'])
        if output!='pathwatch-dns-b\n':raise ValueError('Backup HTTP marker mismatch')
        return output
    def service(self,action):
        if action not in ('stop','start'):raise ValueError('Owned service action only')
        self.run('a',['sudo','systemctl',action,'pathwatch-dns-http.service'],timeout=20,cleanup=action=='start')
        if action=='start':self.run('a',['sudo','systemctl','is-active','--quiet','pathwatch-dns-http.service'],cleanup=True)
    def count_private_query(self):
        path=self.root/'.cache/dns-private-counts.json'
        with Path(str(path)+'.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            counts=json.loads(path.read_text()) if path.exists() else {}
            campaign=self.config['campaign_id'];count=counts.get(campaign,0)
            if count>=8000:raise ValueError('80% private DNS query reservation stop')
            counts[campaign]=count+1;tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(counts));os.replace(tmp,path)
    def private_query(self,site,name,server):
        if name not in IPS or server not in (IPS['a'],IPS['b']) or site not in ('home','a','b'):raise ValueError('Owned private DNS names/server only')
        self.count_private_query()
        args=['dig','+tries=1','+time=3','+short','@'+server,name+'.'+ZONE,'A']
        if site=='home':
            info=json.loads(self.run('home',['docker','inspect','pathwatch-dns-home']))[0]
            labels=info.get('Config',{}).get('Labels',{})
            if any(labels.get(k)!=v for k,v in {'project':'pathwatch','owner':'codex-pathwatch','purpose':'dns-lab'}.items()):raise ValueError('Refuse foreign home container')
            args=['docker','exec','pathwatch-dns-home',*args]
        result=self.run(site,args).splitlines()
        if result!=[IPS[name]]:raise ValueError('Actual cross-resolution returned wrong owned IP')
        return result
