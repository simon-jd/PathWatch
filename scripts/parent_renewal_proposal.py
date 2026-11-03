#!/usr/bin/env python3
"""Prepare exact parent TTL renewal rows; never writes canonical ledger or Azure.

Pricing input supplies bounds, exact captured meters and a conservative estimate;
canonical validation computes the actual lower bound using the original biller.
"""
import argparse
import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import azure_budget as budget


def build(plan,ledger,pricing,start,renewal_id,now):
    variables={k:v['value'] for k,v in plan['variables'].items()}
    rows=[]
    for resource in sorted(plan['resource_changes'],key=lambda r:r['address']):
        if resource.get('mode')!='managed' or resource['change']['actions']==['no-op']:continue
        before=resource['change'].get('before') or {}
        row=dict(address=resource['address'],type=resource['type'],region=variables['location'],
                 subscription_id=variables['subscription_id'],resource_id=before.get('id'),
                 start_utc=start,end_utc=variables['ttl'],previous_end_utc=before.get('tags',{}).get('ttl'),
                 estimate_usd='0',meters=[],renewal_id=renewal_id)
        if resource['type']=='azurerm_storage_account':
            if set(pricing)!={'bounds','meters','estimate_usd'}:raise ValueError('Exact bounded storage pricing object required')
            row.update(copy.deepcopy(pricing))
        rows.append(row)
    result=dict(purpose='bootstrap-renewal',resources=rows)
    budget.check(plan,result,ledger,now)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for argument in ('plan','pricing','start-utc','renewal-id','out'):parser.add_argument('--'+argument,required=True)
    args=parser.parse_args();path=Path(args.plan).resolve()
    plan=json.loads(subprocess.check_output(['terraform','show','-json',str(path)],cwd=path.parent,text=True))
    pricing=json.loads(Path(args.pricing).read_text())
    with budget.ledger_lock():
        result=build(plan,json.loads(budget.LEDGER.read_text()),pricing,args.start_utc,args.renewal_id,dt.datetime.now(dt.timezone.utc))
    Path(args.out).write_text(json.dumps(result,indent=2)+'\n')
    print('Wrote reviewed renewal proposal only; historical estimates and ledger unchanged')

if __name__=='__main__':main()
