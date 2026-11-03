#!/usr/bin/env python3
"""Private-only DNS and authenticated static WireGuard configs for the DNS lab."""
import argparse
import base64
import ipaddress
from pathlib import Path
import re
from azure_dns_scope import IPS, ZONE

VERSION='1.12.4'
COREDNS_SHA256={'arm64':'67fdaa45b622eed5d969aad300ce28d37b42da60010672b90fde41e1dceefcaa','amd64':'46dcee6d9b406310a2dbf793e414c4a999317ddacbdfc307215a006144592f8f'}
WG={'a':('10.255.4.1','10.255.4.2'),'b':('10.255.4.5','10.255.4.6')}
def corefile(site):
    if site not in WG:raise ValueError('DNS site a/b required')
    return f'{ZONE}:53 {{\n    bind {IPS[site]} {WG[site][0]}\n    errors\n    cache 30\n    forward . 168.63.129.16 {{\n        max_concurrent 32\n        max_fails 0\n    }}\n}}\n'
def wireguard(site,home,private,public,endpoint=None):
    if site not in WG:raise ValueError('DNS site a/b required')
    for key in (private,public):
        if not re.fullmatch(r'[A-Za-z0-9+/]{43}=',key) or len(base64.b64decode(key))!=32:raise ValueError('32-byte WG key required')
    cloud,local=WG[site];own=local if home else cloud
    lines=['[Interface]','Address = '+own+'/30','ListenPort = '+str(51824+(site=='b' and home)),'PrivateKey = '+private,'','[Peer]','PublicKey = '+public,'AllowedIPs = '+(cloud+'/32, '+IPS[site]+'/32' if home else local+'/32, '+IPS['home']+'/32'),'PersistentKeepalive = 25']
    if home:
        ip=ipaddress.ip_address(endpoint)
        if ip.version!=4 or not ip.is_global:raise ValueError('Owned Azure endpoint public IPv4 required')
        lines+=['Endpoint = '+str(ip)+':51824']
    return '\n'.join(lines)+'\n'
def render(directory,keys,public_ips):
    root=Path(__file__).resolve().parents[1];directory=directory.resolve()
    if not directory.is_relative_to(root/'.cache'):raise ValueError('Private DNS configs stay inside project .cache')
    directory.mkdir(parents=True,exist_ok=True);directory.chmod(0o700)
    for site in WG:
        for home in (False,True):
            name='home-'+site if home else site;peer=site if home else 'home-'+site
            target=directory/(name+'.conf');target.write_text(wireguard(site,home,(keys/(name+'.key')).read_text().strip(),(keys/(peer+'.pub')).read_text().strip(),public_ips[site]));target.chmod(0o600)
        (directory/(site+'.Corefile')).write_text(corefile(site))
if __name__=='__main__':
    import json
    p=argparse.ArgumentParser();p.add_argument('--keys',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--inventory',type=Path,required=True);a=p.parse_args();config=json.loads(a.inventory.read_text());render(a.out,a.keys,{site:config['sites'][site]['public_ip'] for site in WG})
