#!/usr/bin/env python3
"""Run the physical home observer in the exact owned home-A router namespace."""
import re
import hashlib
import json
import os
import subprocess
from pathlib import Path
import probe_transport_home as transport

ROOT=Path(__file__).resolve().parents[1]
LABELS={'project':'pathwatch','owner':'codex-pathwatch','purpose':'home-observer'}
IMAGE='clickhouse/clickhouse-server@sha256:456063a689194186633bb3db0862283068f4c2ee538852d3aaffa7eb66f1f841'

def exact_id(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):
        raise ValueError('Exact 64-hex Docker identity required')

def validate_parent(parent,network,expected):
    exact_id(expected)
    if not parent or not network:raise ValueError('Owned parent and bridge must exist')
    transport.owned(parent,'container');transport.owned(network,'network')
    exact_id(network.get('Id'))
    if parent.get('Id')!=expected or parent.get('Name')!='/pathwatch-probe-home-azure-a' or not parent.get('State',{}).get('Running'):
        raise ValueError('Home-A parent stopped, replaced or foreign')
    host=parent.get('HostConfig',{})
    if host.get('Privileged') is not False or host.get('NetworkMode')!=transport.NETWORK:
        raise ValueError('Only owned bridge router namespace accepted')
    networks=parent.get('NetworkSettings',{}).get('Networks',{})
    if set(networks)!={transport.NETWORK}:raise ValueError('Unexpected parent networks')
    endpoint=networks[transport.NETWORK]
    if any(endpoint.get(k)!=v for k,v in {'NetworkID':network['Id'],'IPAddress':'10.30.0.2','IPPrefixLen':16,'Gateway':'10.30.0.1'}.items()):
        raise ValueError('Wrong owned home-A bridge endpoint')
    member=network.get('Containers',{}).get(expected,{})
    if network.get('Name')!=transport.NETWORK or member.get('Name')!='pathwatch-probe-home-azure-a' or member.get('IPv4Address')!='10.30.0.2/16':
        raise ValueError('Parent is not exact owned bridge member')

def validate_child(child,parent):
    exact_id(parent)
    exact_id(child.get('Id'))
    labels=child.get('Config',{}).get('Labels',{})
    if any(labels.get(k)!=v for k,v in {**LABELS,'pathwatch.parent':parent}.items()) or child.get('HostConfig',{}).get('NetworkMode')!='container:'+parent:
        raise ValueError('Refuse foreign or reparented observer cleanup')

def check_parent(parent):
    # Resolve the fixed name as well as ID: replacement must fail, never reattach.
    current=transport.inspect('container','pathwatch-probe-home-azure-a')
    network=transport.inspect('network',transport.NETWORK)
    validate_parent(current,network,parent)

def cleanup(parent,children):
    failures=[];owned=[];retain_services=False
    for identity in children:
        try:
            exact_id(identity)
            child=transport.inspect('container',identity)
            if child is not None:
                validate_child(child,parent);owned.append(child)
        except Exception as exc:
            failures.append(str(exc));retain_services=True
    # Keep echo and the parent namespace available while the probe drains its
    # scheduler and durably flushes its final partial batch on SIGTERM.
    owned.sort(key=lambda child:child['Config']['Labels'].get('component')!='probe')
    for child in owned:
        identity=child['Id'];probe=child['Config']['Labels'].get('component')=='probe'
        stopped=False
        if not probe and retain_services:
            failures.append('Retaining service for unconfirmed probe shutdown: '+identity);continue
        try:
            current=transport.inspect('container',identity)
            if current is None:continue
            validate_child(current,parent)
            if current.get('State',{}).get('Running'):
                if probe and current['Config']['Labels'].get('pathwatch.stop-profile')!='shutdown-seconds-max5-v1':
                    raise ValueError('Probe lacks reviewed shutdown bound; retain for exact recovery')
                # Probe has two <=5s shutdown scopes. Allow 5s extra before
                # Docker's forced-stop fallback; a forced exit is never success.
                seconds=15 if probe else 5
                result=subprocess.run(['docker','stop','--signal','SIGTERM','--timeout',str(seconds),identity],capture_output=True,text=True,timeout=seconds+5)
                if result.returncode:raise RuntimeError('Cannot stop owned child: '+result.stderr)
                current=transport.inspect('container',identity)
                if current is None:raise RuntimeError('Owned child disappeared during stop: '+identity)
                validate_child(current,parent)
                if current.get('State',{}).get('Running'):raise RuntimeError('Owned child still running after stop: '+identity)
            if current.get('State',{}).get('ExitCode',0)!=0:
                failures.append('Owned child stopped with exit '+str(current['State']['ExitCode'])+': '+identity)
            stopped=True
            transport.command(['docker','rm',identity])
        except Exception as exc:
            failures.append(str(exc))
            if probe and not stopped:retain_services=True
    if failures:raise RuntimeError('Observer cleanup failed: '+'; '.join(failures))

def private_path(value):
    path=Path(value).resolve()
    if not path.is_relative_to(ROOT/'.cache') or path==ROOT/'.cache':raise ValueError('Dedicated project-private path required')
    return path

def prepare(cfg,directory):
    settings=dict(cfg['namespace_observer'])
    exact_id(settings.get('parent_id'))
    if settings.get('image_id')!=IMAGE:raise ValueError('Only the fixed already-cached runtime image is supported; no pull')
    directory=private_path(directory)
    for name in ['probe_binary','echo_binary','ca','cert','key','ingest_ca']:
        settings[name]=str(private_path(settings[name]))
        if not Path(settings[name]).is_file():raise ValueError('Missing private runtime input: '+name)
    for kind in ['probe','echo']:
        if hashlib.sha256(Path(settings[kind+'_binary']).read_bytes()).hexdigest()!=settings[kind+'_sha256']:
            raise ValueError('Reviewed '+kind+' artifact hash mismatch')
    if len({settings[n] for n in ['probe_binary','echo_binary','ca','cert','key','ingest_ca']})!=6:
        raise ValueError('Runtime inputs must be distinct')
    inputs=[Path(settings[n]) for n in ['probe_binary','echo_binary','ca','cert','key','ingest_ca']]+[private_path(cfg['probe_config'])]
    if any(path.is_relative_to(directory) for path in inputs):raise ValueError('Runtime directory must be separate from all immutable inputs and PKI')
    if directory.exists():
        if directory.stat().st_uid!=os.getuid() or directory.stat().st_mode&0o077:raise ValueError('Runtime directory must be private and owned by current user')
        if any(path.name not in {'owner.lock','probe.json','state.json','echo-ready.json'} or path.is_symlink() or not path.is_file() for path in directory.iterdir()):raise ValueError('Runtime directory contains foreign files')
    probe=json.loads(private_path(cfg['probe_config']).read_text())
    shutdown=probe.get('shutdown_timeout','')
    if shutdown in ('',None):shutdown='5s'
    if not isinstance(shutdown,str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?s',shutdown) or not 0<float(shutdown[:-1])<=5:
        raise ValueError('Namespace shutdown_timeout must be positive seconds <=5s (default 5s)')
    spool=private_path(cfg['spool_dir'])
    if probe.get('probe_id')!='home' or private_path(probe.get('spool_dir',''))!=spool:
        raise ValueError('Physical home identity and owned spool must match')
    if probe.get('metrics_addr')!='10.30.0.2:9464' or probe.get('ingest')!='10.30.0.1:19443' or probe.get('development_cleartext'):
        raise ValueError('Exact private home metrics and TLS ingest required')
    if private_path(probe.get('ingest_ca',''))!=Path(settings['ingest_ca']):raise ValueError('Explicit independent ingest trust must match')
    if directory==spool or directory.is_relative_to(spool):raise ValueError('Runtime directory must be separate from preserved spool')
    probe={**probe,'spool_dir':'/spool','ingest_ca':'/trust/ingest.pem'}
    # Only the already reviewed public target CA and existing read-only mount
    # may be reused. No additional directory/private-key mounts are introduced.
    for target in probe.get('targets',[]):
        if 'DNS' in target and 'dns' in target:raise ValueError('Ambiguous DNS CA configuration')
        dns=target.get('DNS',target.get('dns',{})) or {}
        if dns.get('ca_file'):
            if dns.get('transport')!='doh' or private_path(dns['ca_file'])!=Path(settings['ca']):raise ValueError('DoH CA must be the reviewed public target CA')
            dns['ca_file']='/trust/ca.pem'
    return {'settings':settings,'probe':probe,'spool':spool,'directory':directory}

def validate_probe_process(status):
    fields=dict(line.split(':',1) for line in status.splitlines() if ':' in line)
    if fields.get('Name','').strip()!='pathwatch-probe':raise ValueError('Probe has not execed the fixed reviewed binary')
    for key,want in [('Uid',os.getuid()),('Gid',os.getgid())]:
        if fields.get(key,'').split()!=[str(want)]*4:raise ValueError('Probe process identity changed')
    for key in ['CapEff','CapPrm','CapInh','CapAmb','CapBnd']:
        if int(fields.get(key,'0').strip(),16)!=0x2000:raise ValueError('Probe must retain only NET_RAW: '+key)
    if fields.get('NoNewPrivs','').strip()!='1':raise ValueError('Probe no-new-privileges is required')

def children_for_parent(parent):
    exact_id(parent)
    filters=[item for key,value in {**LABELS,'pathwatch.parent':parent}.items() for item in ['--filter','label='+key+'='+value]]
    values=transport.command(['docker','ps','-aq','--no-trunc',*filters]).split()
    for identity in values:exact_id(identity)
    return values

def stop_for_parent(parent):
    cleanup(parent,children_for_parent(parent))

def run(cfg,directory):
    import fcntl
    import importlib.util
    import signal
    import time
    import uuid
    prepared=prepare(cfg,directory);settings=prepared['settings'];parent=settings['parent_id'];directory=prepared['directory']
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=open(directory/'owner.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    check_parent(parent)
    if children_for_parent(parent):raise ValueError('An observer already owns this parent; use exact down first')
    image=transport.inspect('image',IMAGE)
    if image is None or image.get('Id')!='sha256:456063a689194186633bb3db0862283068f4c2ee538852d3aaffa7eb66f1f841':raise ValueError('Fixed reviewed image must already exist locally')
    prepared['spool'].mkdir(parents=True,exist_ok=True,mode=0o700)
    config=directory/'probe.json';config.write_text(json.dumps(prepared['probe']));config.chmod(0o600)
    ready=directory/'echo-ready.json';ready.unlink(missing_ok=True)
    nonce=uuid.uuid4().hex;children=[];stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    previous={s:signal.signal(s,stop) for s in [signal.SIGINT,signal.SIGTERM]}
    def persist():
        (directory/'state.json').write_text(json.dumps({'parent_id':parent,'children':children,'run':nonce,'probe_id':'home','metrics':'http://10.30.0.2:9464/metrics'},indent=2))
    def create(kind):
        name='pathwatch-home-'+kind+'-'+nonce[:12]
        labels={**LABELS,'pathwatch.parent':parent,'run':nonce,'component':kind}
        if kind=='probe':labels['pathwatch.stop-profile']='shutdown-seconds-max5-v1'
        args=['docker','create','--pull=never','--name',name,'--network','container:'+parent,'--read-only','--memory','256m' if kind=='probe' else '64m','--memory-swap','256m' if kind=='probe' else '64m','--pids-limit','64','--cap-drop','ALL','--security-opt','no-new-privileges','--log-opt','max-size=1m','--log-opt','max-file=1','--tmpfs','/var/lib/clickhouse:rw,size=1m']
        for key,value in labels.items():args+=['--label',key+'='+value]
        def mount(source,target,writable=False):args.extend(['-v',str(source)+':'+target+(':rw' if writable else ':ro')])
        mount(settings['ca'],'/trust/ca.pem')
        if kind=='probe':
            args+=['--user','0:0','-e','SSL_CERT_FILE=/trust/ca.pem','-e','GOMAXPROCS=2']
            for cap in ['NET_RAW','SETUID','SETGID','SETPCAP']:args+=['--cap-add',cap]
            mount(settings['probe_binary'],'/pathwatch-probe');mount(config,'/probe.json');mount(settings['ingest_ca'],'/trust/ingest.pem');mount(prepared['spool'],'/spool',True)
            args+=['--entrypoint','/usr/bin/setpriv',IMAGE,'--reuid='+str(os.getuid()),'--regid='+str(os.getgid()),'--clear-groups','--bounding-set=-all,+net_raw','--inh-caps=-all,+net_raw','--ambient-caps=-all,+net_raw','--no-new-privs','/pathwatch-probe','-config','/probe.json']
        else:
            args+=['--user',f'{os.getuid()}:{os.getgid()}','-e','GOMAXPROCS=2']
            mount(settings['echo_binary'],'/pathwatch-echo');mount(settings['cert'],'/trust/server.pem');mount(settings['key'],'/trust/server.key');mount(directory,'/runtime',True)
            args+=['--entrypoint','/pathwatch-echo',IMAGE,'-mode','provided','-site','home','-listen','10.30.0.2:18443','-cert','/trust/server.pem','-key','/trust/server.key','-ca','/trust/ca.pem','-ready-file','/runtime/echo-ready.json','-ready-token',nonce]
        identity=None
        try:
            identity=transport.command(args).strip();exact_id(identity);children.append(identity)
        except BaseException:
            # Creation may succeed before a client-side error: recover only the unique owned nonce.
            partial=transport.inspect('container',name)
            if partial:
                validate_child(partial,parent)
                if partial['Config']['Labels'].get('run')!=nonce:raise ValueError('Creation identity changed')
                if partial['Id'] not in children:children.append(partial['Id'])
            raise
        persist() # IDs are retained in memory before any evidence write can fail.
        check_parent(parent)
        transport.command(['docker','start',identity])
        return identity
    try:
        # Provided mode checks present validity; the separate deployment check covers 35 days.
        echo=create('echo')
        spec=importlib.util.spec_from_file_location('pathwatch_home_lifecycle',ROOT/'infra/modules/clickhouse/home.py');home=importlib.util.module_from_spec(spec);spec.loader.exec_module(home)
        def echo_alive():
            check_parent(parent)
            child=transport.inspect('container',echo)
            return bool(child and child.get('State',{}).get('Running'))
        home.wait_echo_ready(Path(settings['cert']),ready,nonce,'10.30.0.2:18443',echo_alive,ca=settings['ca'])
        probe=create('probe')
        for attempt in range(30):
            try:
                validate_probe_process(transport.command(['docker','exec',probe,'cat','/proc/1/status']));break
            except ValueError:
                if attempt==29:raise
                time.sleep(.05)
        while not stopping:
            check_parent(parent)
            for identity in children:
                child=transport.inspect('container',identity)
                if child is None:raise RuntimeError('Owned observer child disappeared')
                validate_child(child,parent)
                if not child.get('State',{}).get('Running'):raise RuntimeError('Owned observer child exited')
            validate_probe_process(transport.command(['docker','exec',probe,'cat','/proc/1/status']))
            time.sleep(.5)
    finally:
        try:cleanup(parent,children)
        finally:
            for sig,handler in previous.items():signal.signal(sig,handler)
            lock.close()

def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['run','down']);parser.add_argument('--config',type=Path);parser.add_argument('--directory',type=Path);parser.add_argument('--parent-id')
    args=parser.parse_args();os.environ['DOCKER_CONFIG']=str(ROOT/'.cache/docker')
    if args.action=='down':
        if not args.parent_id:parser.error('down requires exact --parent-id')
        stop_for_parent(args.parent_id)
    else:
        if not args.config or not args.directory:parser.error('run requires private --config and --directory')
        run(json.loads(private_path(args.config).read_text()),args.directory)

if __name__=='__main__':main()
