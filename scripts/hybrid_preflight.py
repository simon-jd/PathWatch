#!/usr/bin/env python3
"""Read-only current host overlap check; owned existing home network is allowed."""
import argparse
import ipaddress
import json
import subprocess
RESERVED=[ipaddress.ip_network(s) for s in ('10.10.0.0/16','10.20.0.0/16','10.30.0.0/16','10.255.0.0/24')]
def check_routes(routes,own_network=False):
    for route in routes:
        dst=route.get('dst','default')
        if dst=='default':continue
        net=ipaddress.ip_network(dst,strict=False)
        if net.version!=4 or net.prefixlen==0:continue
        if any(net.overlaps(r) for r in RESERVED):
            if own_network and net==ipaddress.ip_network('10.30.0.0/16'):continue
            raise ValueError('Reserved overlap with existing host route '+dst)
def main():
    p=argparse.ArgumentParser();p.add_argument('--host-routes',action='store_true',required=True);p.parse_args()
    routes=json.loads(subprocess.check_output(['ip','-j','-4','route','show','table','all']))
    owned=False
    probe=subprocess.run(['docker','network','inspect','pathwatch-hybrid-home'],capture_output=True,text=True)
    if probe.returncode==0:
        network=json.loads(probe.stdout)[0];owned=network.get('Labels',{}).get('project')=='pathwatch' and network.get('IPAM',{}).get('Config',[{}])[0].get('Subnet')=='10.30.0.0/16'
    check_routes(routes,owned);print('PASS: current host routes do not conflict with assigned project ranges')
if __name__=='__main__':main()
