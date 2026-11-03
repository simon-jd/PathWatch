#!/usr/bin/env python3
"""Render single-VM WG/FRR endpoints using Task3's reviewed pure configuration API."""
import argparse
import hashlib
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
_spec=importlib.util.spec_from_file_location('hybrid_config',Path(__file__).with_name('hybrid_config.py'))
hybrid=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(hybrid)
ROOT=Path(__file__).resolve().parents[1]


def rewrite(value,site):
    mapping={} if site=='azure-a' else {'10.10':'10.20','65010':'65020','10.255.0.1':'10.255.0.9','10.255.0.2':'10.255.0.10','10.255.0.5':'10.255.0.13','10.255.0.6':'10.255.0.14','10.30.0.2':'10.30.0.3'}
    if mapping:
        value=re.sub(r'10\.255\.0\.[1256](?![0-9])|10\.30\.0\.2(?![0-9])|10\.10(?=\.)|\b65010\b',lambda m:mapping[m[0]],value)
    return value.replace('hostname pathwatch-',f'hostname pathwatch-probe-{site}-')


def render(site,endpoint,cloud_private,cloud_public,home_private,home_public):
    if site not in ('azure-a','azure-b'):raise ValueError('Exact physical cloud site required')
    ip=ipaddress.ip_address(endpoint)
    if ip.version!=4 or not ip.is_global:raise ValueError('Owned cloud public IPv4 endpoint required')
    if any(len(keys)!=2 for keys in (cloud_private,cloud_public,home_private,home_public)):raise ValueError('Two independent tunnel keys per endpoint required')
    result={'azure':{},'home':{}}
    cloud='10.10' if site=='azure-a' else '10.20'
    other='10.20' if site=='azure-a' else '10.10'
    gateway='10.30.0.3' if site=='azure-a' else '10.30.0.2'
    for local in result:
        frr=rewrite(hybrid.frr_config(local,'bfd'),site)
        if local=='home':
            frr=frr.replace('ip prefix-list OWN seq 100',f'ip prefix-list OWN seq 10 permit {other}.0.0/16\nip prefix-list OWN seq 100')
            frr=frr.replace(' address-family ipv4 unicast',f' address-family ipv4 unicast\n  network {other}.0.0/16')
            frr=frr.replace('router bgp 64512',f'ip route {other}.0.0/16 {gateway}\nrouter bgp 64512')
            source='10.30.0.2' if site=='azure-a' else '10.30.0.3'
            frr=frr.replace('line vty',f'route-map HOME_SOURCE permit 10\n match ip address prefix-list REMOTE\n set src {source}\nroute-map HOME_SOURCE deny 100\nip protocol bgp route-map HOME_SOURCE\nline vty')
        else:
            frr=frr.replace('ip prefix-list REMOTE seq 100',f'ip prefix-list REMOTE seq 10 permit {other}.0.0/16\nip prefix-list REMOTE seq 100')
            frr=frr.replace('maximum-prefix 1','maximum-prefix 2')
            frr=frr.replace('line vty',f'route-map PROBE_SOURCE permit 10\n match ip address prefix-list REMOTE\n set src {cloud}.1.4\nroute-map PROBE_SOURCE deny 100\nip protocol bgp route-map PROBE_SOURCE\nline vty')
        result[local]['frr.conf']=frr
        for index in (0,1):
            private,public=(cloud_private[index],home_public[index]) if local=='azure' else (home_private[index],cloud_public[index])
            text=rewrite(hybrid.wg_config(local,index,private,public,endpoint if local=='home' else None),site)
            if local=='azure':text=text.replace('10.30.0.0/16','10.30.0.0/16, '+other+'.0.0/16')
            result[local][f'pw-wg{index}.conf']=text.replace('[Interface]\n','[Interface]\nMTU = 1380\n')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site',choices=['azure-a','azure-b'],required=True);parser.add_argument('--endpoint',required=True)
    parser.add_argument('--keys-dir',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();keys=args.keys_dir.resolve();out=args.out.resolve()
    if not keys.is_relative_to(ROOT/'.cache') or not out.is_relative_to(ROOT/'.cache'):raise ValueError('Private keys/configs must stay inside project .cache')
    def read(local,suffix):
        result=[]
        for index in (0,1):
            path=keys/f'{args.site}-{local}-{index}.{suffix}'
            if path.stat().st_mode&0o077:raise ValueError('Private key directory files require mode0600')
            result.append(path.read_text().strip())
        return result
    data=render(args.site,args.endpoint,read('cloud','key'),read('cloud','pub'),read('home','key'),read('home','pub'))
    out.mkdir(parents=True,exist_ok=True);out.chmod(0o700)
    for local,files in data.items():
        directory=out/local;directory.mkdir(exist_ok=True);directory.chmod(0o700)
        for name,value in files.items():
            path=directory/name;path.write_text(value);path.chmod(0o600)
        manifest={'scope':'pathwatch-probe-transport','site':args.site,'endpoint':args.endpoint,'files':{name:hashlib.sha256(value.encode()).hexdigest() for name,value in files.items()}}
        (directory/'manifest.json').write_text(json.dumps(manifest)+'\n');(directory/'manifest.json').chmod(0o600)
    print('Rendered private cloud and home configs; no daemon or network started')
if __name__=='__main__':main()
