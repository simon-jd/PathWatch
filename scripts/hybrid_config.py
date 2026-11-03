#!/usr/bin/env python3
"""Render private WG and strict FRR configs; never print secret material."""
import argparse
import base64
import ipaddress
import os
from pathlib import Path
import re
SITES={'azure':{'asn':65010,'prefix':'10.10.0.0/16','router_id':'10.10.1.4','addresses':['10.255.0.1','10.255.0.5'],'peers':['10.255.0.2','10.255.0.6']},'home':{'asn':64512,'prefix':'10.30.0.0/16','router_id':'10.30.0.2','addresses':['10.255.0.2','10.255.0.6'],'peers':['10.255.0.1','10.255.0.5']}}
def site_values(site):
    if site not in SITES:raise ValueError('Site must be azure/home')
    return SITES[site],SITES['home' if site=='azure' else 'azure']
def frr_config(site,profile):
    own,remote=site_values(site)
    if profile not in ('baseline','tuned','bfd'):raise ValueError('Unknown timer profile')
    keep,hold=(60,180) if profile=='baseline' else (3,9)
    if profile=='bfd':keep,hold=60,180
    lines=['frr defaults traditional','hostname pathwatch-'+site,'service integrated-vtysh-config','log stdout informational','ip forwarding']
    if site=='azure':lines+=['! Forward aggregate to actual Azure fabric gateway.','ip route 10.10.0.0/16 10.10.1.1']
    if profile=='bfd':lines+=['bfd',' profile PATHWATCH','  transmit-interval 300','  receive-interval 300','  detect-multiplier 3',' exit','exit']
    lines+=['ip prefix-list OWN seq 5 permit '+own['prefix'],'ip prefix-list REMOTE seq 5 permit '+remote['prefix'],'ip prefix-list REMOTE seq 100 deny 0.0.0.0/0 le 32','ip prefix-list OWN seq 100 deny 0.0.0.0/0 le 32','route-map EXPORT permit 10',' match ip address prefix-list OWN','route-map EXPORT deny 100']
    for i,preference in enumerate((200,100)):
        lines+=['route-map IMPORT'+str(i)+' permit 10',' match ip address prefix-list REMOTE',' set local-preference '+str(preference),'route-map IMPORT'+str(i)+' deny 100']
    lines+=['router bgp '+str(own['asn']),' bgp router-id '+own['router_id'],' bgp log-neighbor-changes',' bgp graceful-restart-disable',' bgp network import-check']
    for i,peer in enumerate(own['peers']):
        lines+=[' neighbor '+peer+' remote-as '+str(remote['asn']),' neighbor '+peer+' update-source '+own['addresses'][i],' neighbor '+peer+' timers '+str(keep)+' '+str(hold)]
        if profile=='bfd':lines+=[' neighbor '+peer+' bfd profile PATHWATCH']
    lines+=[' address-family ipv4 unicast','  network '+own['prefix']]
    for i,peer in enumerate(own['peers']):
        lines+=['  neighbor '+peer+' activate','  neighbor '+peer+' route-map IMPORT'+str(i)+' in','  neighbor '+peer+' route-map EXPORT out','  neighbor '+peer+' maximum-prefix 1']
    lines+=[' exit-address-family','exit','line vty','! BGP179/BFD3784 stay inside WG; no default-route export.']
    return '\n'.join(lines)+'\n'
def wg_config(site,index,private_key,public_key,endpoint=None):
    own,remote=site_values(site)
    if index not in (0,1):raise ValueError('Tunnel index0/1 required')
    for key in (private_key,public_key):
        if not re.fullmatch('[A-Za-z0-9+/]{43}=',key) or len(base64.b64decode(key))!=32:raise ValueError('WG key must encode32 bytes')
    lines=['[Interface]','Address = '+own['addresses'][index]+'/30','ListenPort = '+str(51820+index),'PrivateKey = '+private_key,'Table = off','','[Peer]','PublicKey = '+public_key,'AllowedIPs = '+own['peers'][index]+'/32, '+remote['prefix'],'PersistentKeepalive = 25']
    if endpoint:
        ip=ipaddress.ip_address(endpoint)
        if ip.version!=4 or not ip.is_global:raise ValueError('Endpoint must be owned public IPv4')
        lines+=['Endpoint = '+str(ip)+':'+str(51820+index)]
    if site=='home' and not endpoint:raise ValueError('Home initiates both tunnels through NAT; Azure endpoint required')
    return '\n'.join(lines)+'\n'
def main():
    p=argparse.ArgumentParser();p.add_argument('--site',choices=SITES,required=True);p.add_argument('--profile',choices=['baseline','tuned','bfd'],required=True);p.add_argument('--keys-dir',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--azure-endpoint');a=p.parse_args()
    # .cache is ignored. Output must stay private inside the project, never /etc.
    root=Path(__file__).resolve().parents[1];out=a.out.resolve()
    if not out.is_relative_to(root/'.cache'):raise ValueError('Private rendered configs must stay in project .cache')
    out.mkdir(parents=True,exist_ok=True);out.chmod(0o700)
    remote='home' if a.site=='azure' else 'azure'
    for i in (0,1):
        private=(a.keys_dir/f'{a.site}-{i}.key').read_text().strip();public=(a.keys_dir/f'{remote}-{i}.pub').read_text().strip()
        path=out/f'pw-wg{i}.conf';path.write_text(wg_config(a.site,i,private,public,a.azure_endpoint if a.site=='home' else None));path.chmod(0o600)
    path=out/'frr.conf';path.write_text(frr_config(a.site,a.profile));path.chmod(0o600)
    (out/'profile').write_text(a.profile+'\n')
if __name__=='__main__':main()
