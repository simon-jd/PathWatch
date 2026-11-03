"""Owned endpoint execution. Remote arguments are shell quoted; no secret output."""
import ipaddress
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

class Runtime:
    def __init__(self,path):
        self.config=json.loads(Path(path).read_text())
        if self.config.get('scope')!='pathwatch-azure-home-owned':raise ValueError('Explicit owned endpoint scope required')
        for field in ('nva_public_ip','client_public_ip'):
            ip=ipaddress.ip_address(self.config[field])
            if ip.version!=4 or not ip.is_global:raise ValueError('Use actual owned Azure public IPv4')
        root=Path(__file__).resolve().parents[1]
        self.key=Path(self.config['ssh_private_key']).resolve();self.known=Path(self.config['known_hosts']).resolve()
        for p in (self.key,self.known):
            if not p.is_relative_to(root/'.cache') or not p.is_file():raise ValueError('Existing private SSH key and verified known_hosts must stay in project .cache')
        if self.key.stat().st_mode & 0o077:raise ValueError('SSH private key permissions must exclude group/other')
        self.root=root
        docker_config=root/'.cache/docker';docker_config.mkdir(parents=True,exist_ok=True)
        os.environ['DOCKER_CONFIG']=str(docker_config)
    def command(self,node,args):
        if node in ('home','home-client'):
            container='pathwatch-hybrid-home-router' if node=='home' else 'pathwatch-hybrid-home-client'
            info=json.loads(subprocess.check_output(['docker','container','inspect',container],text=True))[0]
            if info.get('Config',{}).get('Labels',{}).get('project')!='pathwatch':raise ValueError('Refuse foreign container')
            return ['docker','exec','-i',container]+list(args)
        if node not in ('azure','azure-client'):raise ValueError('Unknown endpoint')
        host=self.config['nva_public_ip' if node=='azure' else 'client_public_ip']
        return ['ssh','-F','/dev/null','-o','IdentitiesOnly=yes','-o','GlobalKnownHostsFile=/dev/null','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(self.known),'-i',str(self.key),'pathwatch@'+host,shlex.join(args)]
    def run(self,node,args,timeout=20,input=None):
        if timeout>30:args=['timeout','--signal=TERM','--kill-after=3s',str(timeout-5)+'s']+args
        return subprocess.run(self.command(node,args),input=input,text=True,capture_output=True,timeout=timeout,check=True).stdout
    def profile(self,profile):
        if profile not in ('baseline','tuned','bfd'):raise ValueError('Unknown profile')
        for node,peers in [('home',['10.255.0.1','10.255.0.5']),('azure',['10.255.0.2','10.255.0.6'])]:
            sudo=['sudo'] if node=='azure' else []
            neighbors=json.loads(self.run(node,sudo+['vtysh','-c','show bgp neighbors json']))
            if graceful_restart_modes(neighbors,peers) is None:
                raise RuntimeError(node+' GR/helper mode is unknown on the expected peers')
            asn='64512' if node=='home' else '65010'
            commands=['configure terminal']
            if profile=='bfd':commands+=['bfd','profile PATHWATCH','transmit-interval 300','receive-interval 300','detect-multiplier 3','exit','exit']
            commands+=['router bgp '+asn]
            # FRR 8.4 rejects a repeated global disable, even though already
            # disabled is the desired state. Still apply timers/BFD and reset.
            if not graceful_restart_disabled(neighbors,peers):commands+=['bgp graceful-restart-disable']
            for peer in peers:
                commands+=['no neighbor '+peer+' bfd','neighbor '+peer+' timers '+('3 9' if profile=='tuned' else '60 180')]
                if profile=='bfd':commands+=['neighbor '+peer+' bfd profile PATHWATCH']
            commands+=['end','clear bgp *']
            args=['vtysh']
            for cmd in commands:args+=['-c',cmd]
            if node=='azure':args=['sudo']+args
            self.run(node,args)
            # FRR 8.4 accepts the old space spelling as hidden disable-eor.
            # Parse success is insufficient: verify the daemon's actual modes
            # after reset; wait_ready separately requires Established sessions.
            neighbors=json.loads(self.run(node,sudo+['vtysh','-c','show bgp neighbors json']))
            if not graceful_restart_disabled(neighbors,peers):
                raise RuntimeError(node+' GR/helper is not disabled on both expected peers')
    def impair(self,enable):
        # Bidirectional primary UDP loss leaves both WG interfaces UP. One-way
        # loss can keep the local BGP hold timer alive on incoming keepalives.
        # Ports are the fixed primary ListenPort/Endpoint in both owned configs;
        # NAT occurs outside this project's home router network namespace.
        base=['iptables','-w','5']
        match=['-p','udp','--sport','51820','--dport','51820','-m','comment','--comment','pathwatch-task3-underlay','-j','DROP']
        rules=[('OUTPUT',['-d',self.config['nva_public_ip']]+match),
               ('INPUT',['-s',self.config['nva_public_ip']]+match)]
        def present(chain,rule):
            try:self.run('home',base+['-C',chain]+rule)
            except subprocess.CalledProcessError as error:
                if error.returncode==1:return False
                raise
            return True
        def restore():
            errors=[]
            for chain,rule in rules:
                try:
                    if present(chain,rule):self.run('home',base+['-D',chain]+rule)
                except BaseException as error:
                    # Always attempt the other direction, even on interruption.
                    # A failed delete must not be reported as successful cleanup.
                    errors.append(error)
            if errors:raise errors[0]
        if enable:
            try:
                for chain,rule in rules:
                    if not present(chain,rule):self.run('home',base+['-I',chain,'1']+rule)
            except BaseException:
                # Also check the failed insert: timeout/cancellation may arrive
                # after iptables committed it. Roll back both exact rules.
                restore()
                raise
        else:restore()
    def home_route(self):return json.loads(self.run('home',['ip','-j','route','get','10.10.0.4']))
    def stop_probe(self):
        try:self.run('home-client',['pkill','-f','^fping -D -l -p 100 -t 90 10.10.0.4$'])
        except subprocess.CalledProcessError as error:
            if error.returncode!=1:raise
    def snapshot(self):
        result={}
        for site,prefix in [('home','10.10.0.0/16'),('azure','10.30.0.0/16')]:
            sudo=['sudo'] if site=='azure' else []
            result[site]={}
            for key,cmd in [('bgp','show bgp ipv4 unicast summary json'),('route','show ip route '+prefix+' json'),('bfd','show bfd peers json')]:
                result[site][key]=json.loads(self.run(site,sudo+['vtysh','-c',cmd]))
            result[site]['kernel']=json.loads(self.run(site,['ip','-j','route','get','10.10.0.4' if site=='home' else '10.30.0.10']))
            handshakes=self.run(site,sudo+['wg','show','all','latest-handshakes'])
            result[site]['handshake_epochs']=[int(line.split()[-1]) for line in handshakes.splitlines() if line.strip()]
        return result
    def usage(self):
        source=(self.root/'scripts/hybrid_usage.py').read_text()
        return {node:json.loads(self.run(node,['python3','-'],input=source)) for node in ('azure','azure-client')}

def graceful_restart_modes(neighbors,expected_peers):
    """Read only recognized FRR local modes from exactly the two owned peers."""
    if not isinstance(neighbors,dict) or len(neighbors)!=2 or set(neighbors)!=set(expected_peers):return None
    modes=[]
    for peer in neighbors.values():
        if not isinstance(peer,dict):return None
        info=peer.get('gracefulRestartInfo')
        if not isinstance(info,dict):return None
        mode=info.get('localGrMode')
        if mode not in ('Disable','Disable*','Helper','Helper*','Restart','Restart*'):return None
        modes.append(mode)
    return modes

def graceful_restart_disabled(neighbors,expected_peers):
    """Require actual FRR local GR mode, including inherited global Disable*."""
    modes=graceful_restart_modes(neighbors,expected_peers)
    return modes is not None and all(mode in ('Disable','Disable*') for mode in modes)

def bgp_summary_peers(summary):
    """AF-specific FRR summaries have direct peers; all-AF summaries wrap them.

    Preserve the existing wrapped representation's priority whenever present.
    An invalid current wrapper must not be hidden by valid direct-root data.
    """
    if not isinstance(summary,dict):return None
    family=summary['ipv4Unicast'] if 'ipv4Unicast' in summary else summary
    if not isinstance(family,dict):return None
    peers=family.get('peers')
    if not isinstance(peers,dict) or any(not isinstance(peer,dict) for peer in peers.values()):return None
    return peers

def established(snapshot,require_bfd=False):
    for site in ('home','azure'):
        data=snapshot[site];peers=bgp_summary_peers(data.get('bgp'))
        if peers is None or len(peers)!=2 or any(p.get('state')!='Established' for p in peers.values()):return False
        if not data['route'] or data['kernel'][0].get('dev') not in ('pw-wg0','pw-wg1'):return False
        stamps=data.get('handshake_epochs',[])
        if len(stamps)!=2 or any(time.time()-s>180 or s<=0 for s in stamps):return False
        if require_bfd:
            bfd=data['bfd']
            if not isinstance(bfd,list) or len(bfd)!=2 or any(p.get('status')!='up' for p in bfd):return False
    return True
