#!/usr/bin/env python3
"""Start/stop exact owned home WG router containers; no host-global network changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import probe_transport
import hybrid_preflight
ROOT=Path(__file__).resolve().parents[1]
NETWORK='pathwatch-probe-home'
LABELS={'project':'pathwatch','owner':'codex-pathwatch','purpose':'probe-transport'}


def command(args):return subprocess.check_output(args,text=True)

def inspect(kind,name):
    result=subprocess.run(['docker',kind,'inspect',name],text=True,capture_output=True)
    if result.returncode:
        # Do not interpret daemon/permission failure as absence and create anyway.
        message=result.stderr.strip().lower()
        missing=re.fullmatch(r'(?:error: |error response from daemon: )no such (?:object|container|network|image): [^\n]+',message) or re.fullmatch(r'error response from daemon: network [^\n]+ not found',message)
        if not missing:raise ValueError('Cannot inspect owned Docker object: '+result.stderr)
        return None
    return json.loads(result.stdout)[0]

def owned(value,kind):
    labels=value.get('Labels',{}) if kind=='network' else value.get('Config',{}).get('Labels',{})
    if any(labels.get(k)!=v for k,v in LABELS.items()):raise ValueError('Refuse foreign Docker object')
    if kind=='network' and (value.get('Driver')!='bridge' or value.get('Scope')!='local' or value.get('Options',{}).get('com.docker.network.bridge.name') or value.get('IPAM',{}).get('Config')!=[{'Subnet':'10.30.0.0/16','Gateway':'10.30.0.1'}]):raise ValueError('Unexpected project bridge IPAM')

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['up','down']);parser.add_argument('--site',choices=['azure-a','azure-b'],required=True)
    parser.add_argument('--config',type=Path);parser.add_argument('--image-id');args=parser.parse_args()
    os.environ['DOCKER_CONFIG']=str(ROOT/'.cache/docker')
    container='pathwatch-probe-home-'+args.site;current=inspect('container',container);network=inspect('network',NETWORK)
    if current:owned(current,'container')
    if network:owned(network,'network')
    if args.action=='down':
        if current:
            if args.site=='azure-a':
                from probe_home_observer import stop_for_parent
                stop_for_parent(current['Id'])
            command(['docker','rm','-f',current['Id']])
        if network:
            network=inspect('network',NETWORK);owned(network,'network')
            if not network.get('Containers'):command(['docker','network','rm',network['Id']])
        return
    if current:raise ValueError('Owned router already exists; no overlapping up')
    if not args.config or not args.config.resolve().is_relative_to(ROOT/'.cache') or not re.fullmatch('sha256:[0-9a-f]{64}',args.image_id or ''):raise ValueError('Explicit private rendered config and reviewed existing image digest required')
    config=args.config.resolve();manifest=json.loads((config/'manifest.json').read_text())
    if manifest.get('scope')!='pathwatch-probe-transport' or manifest.get('site')!=args.site or set(manifest.get('files',{}))!={'frr.conf','pw-wg0.conf','pw-wg1.conf'}:raise ValueError('Exact rendered home transport manifest required')
    for name,digest in manifest['files'].items():
        if hashlib.sha256((config/name).read_bytes()).hexdigest()!=digest:raise ValueError('Rendered transport file changed')
    image=inspect('image',args.image_id)
    if not image or image.get('Config',{}).get('Labels',{}).get('project')!='pathwatch':raise ValueError('Prebuilt reviewed project routing image required; this command never pulls/builds')
    routes=json.loads(command(['ip','-j','-4','route','show','table','all']))
    if network:
        if not re.fullmatch('[0-9a-f]{64}',network.get('Id','')):raise ValueError('Exact owned bridge identity required')
        bridge='br-'+network['Id'][:12]
        owned_routes={('10.30.0.0/16','unicast'),('10.30.0.1','local'),('10.30.255.255','broadcast')}
        routes=[r for r in routes if not (r.get('dev')==bridge and (r.get('dst'),r.get('type','unicast')) in owned_routes)]
    hybrid_preflight.check_routes(routes)
    labels=[item for key,value in LABELS.items() for item in ('--label',key+'='+value)]
    created=False
    try:
        if not network:
            command(['docker','network','create',*labels,'--subnet','10.30.0.0/16','--gateway','10.30.0.1',NETWORK]);created=True
        ip='10.30.0.2' if args.site=='azure-a' else '10.30.0.3'
        caps=['NET_ADMIN','NET_RAW','NET_BIND_SERVICE','SETUID','SETGID','CHOWN','DAC_OVERRIDE','FOWNER','SYS_ADMIN']
        command(['docker','run','-d','--name',container,*labels,'--network',NETWORK,'--ip',ip,'--memory','256m','--pids-limit','100','--cap-drop','ALL',*[item for cap in caps for item in ('--cap-add',cap)],'--sysctl','net.ipv4.ip_forward=1','--sysctl','net.ipv4.conf.all.rp_filter=2','-e','PATHWATCH_ROUTER_NAMESPACE=task10-owned','-v',str(config)+':/run/pathwatch:ro','-v',str(ROOT/'lab/probe-transport/router.sh')+':/run/router.sh:ro','--entrypoint','/bin/bash',args.image_id,'/run/router.sh',args.site])
    except BaseException:
        failed=inspect('container',container)
        if failed:owned(failed,'container');command(['docker','rm','-f',container])
        if created:
            remaining=inspect('network',NETWORK)
            if remaining and not remaining.get('Containers'):owned(remaining,'network');command(['docker','network','rm',NETWORK])
        raise
    print('Started owned home router only; verify WG/BGP/TLS before cloud acceptance')
if __name__=='__main__':main()
