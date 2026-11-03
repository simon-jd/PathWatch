#!/usr/bin/env python3
"""Render a reviewable zero-dollar proposal from an actual saved network plan.
Does not reserve funds, change the ledger, apply Terraform or call Azure.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import azure_network_scope as scope


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',required=True)
    parser.add_argument('--out',required=True)
    args=parser.parse_args()
    path=Path(args.plan).resolve()
    result=subprocess.run(['terraform','show','-json',str(path)],cwd=path.parent,text=True,capture_output=True,check=True)
    plan=json.loads(result.stdout)
    scope.validate(plan,os.environ.get('AZURE_SUBSCRIPTION_ID',''))
    now=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    rows=[]
    for resource in plan.get('resource_changes',[]):
        if resource.get('mode')!='managed' or resource['change']['actions']==['no-op']:continue
        rows.append({'address':resource['address'],'type':resource['type'],
            'region':plan['variables']['location']['value'],'start_utc':now,
            'end_utc':plan['variables']['ttl']['value'],'estimate_usd':0,'meters':[],
            'price_source':scope.PRICE_SOURCE})
    Path(args.out).write_text(json.dumps({'resources':rows},indent=2)+'\n')
    print(f'Wrote {len(rows)} zero-cost rows for review; no reservation or cloud mutation performed')


if __name__=='__main__':main()
