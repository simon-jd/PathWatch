#!/usr/bin/env python3
"""Opt-in actual owned Azure/home cross-resolution; DNS is not IP reachability proof."""
import argparse
import json
from pathlib import Path
import tempfile
from dns_runtime import Runtime
from dns_monitor import Monitor
from dns_failover import atomic

def cross_resolution(runtime,out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);run=Path(tempfile.mkdtemp(prefix='private-',dir=out))
    atomic(run/'run.json',{'status':'running'})
    try:
        with Monitor(runtime,run):
            runtime.attest();answers=[]
            for site,server in [('a','10.10.0.4'),('b','10.20.0.4'),('home','10.10.0.4'),('home','10.20.0.4')]:
                for name in ('a','b','home'):answers.append({'site':site,'server':server,'name':name,'answer':runtime.private_query(site,name,server)})
        atomic(run/'answers.json',answers);atomic(run/'run.json',{'status':'passed'});return run
    except BaseException:
        atomic(run/'run.json',{'status':'failed'});raise
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--inventory',required=True);p.add_argument('--out',required=True);p.add_argument('--confirm-owned-endpoints',action='store_true',required=True);a=p.parse_args();cross_resolution(Runtime(a.inventory),a.out)
