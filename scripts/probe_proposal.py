#!/usr/bin/env python3
"""Price exact cloud-probe saved plans for review; no reservation/apply/cloud mutation."""
import argparse
import datetime as dt
from decimal import Decimal, ROUND_UP
import json
from pathlib import Path
import subprocess
import azure_budget as budget


def build(plan,manifest,start,preflight,same_session,ledger,now):
    adapter=budget.probe_budget;scope=adapter.scope
    variables={k:v['value'] for k,v in plan['variables'].items()};sub=variables['subscription_id'];site=scope.validate(plan,sub)
    hours=Decimal(str((budget.utc(variables['ttl'])-budget.utc(start)).total_seconds()))/3600
    rows=[]
    for resource in plan['resource_changes']:
        if resource.get('mode')!='managed' or resource['change']['actions']==['no-op']:continue
        typ=resource['type'];bounds=adapter.quantities(typ,hours);meters=[];cost=Decimal(0)
        for category,quantity in bounds.items():
            meter=dict(manifest[category],category=category);meters.append(meter)
            price=budget.validate_meter(json.loads((budget.ROOT/meter['capture']).read_text()),meter['selector'],now)
            cost+=price*quantity/(10000 if category=='transactions' else 1)
        row=dict(address=resource['address'],type=typ,probe_site=site,subscription_id=sub,resource_id=adapter.resource_id(resource,sub,site),campaign_id=variables['campaign_id'],region=variables['location'],start_utc=start,end_utc=variables['ttl'],bounds={k:str(v) for k,v in bounds.items()},meters=meters,estimate_usd=str(cost.quantize(Decimal('0.01'),rounding=ROUND_UP)),same_session_delete=same_session)
        if not bounds:row['price_source']=adapter.FREE_SOURCE
        rows.append(row)
    result=dict(purpose='cloud-probe',preflight=preflight,network_capture=manifest['network_capture'],resources=rows)
    budget.check(plan,result,ledger,now)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('plan','meter-manifest','preflight','start-utc','out'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--same-session-delete',action='store_true');args=parser.parse_args();path=Path(args.plan).resolve()
    plan=json.loads(subprocess.check_output(['terraform','show','-json',str(path)],cwd=path.parent,text=True))
    with budget.ledger_lock():
        result=build(plan,json.loads(Path(args.meter_manifest).read_text()),args.start_utc,args.preflight,args.same_session_delete,json.loads(budget.LEDGER.read_text()),dt.datetime.now(dt.timezone.utc))
    Path(args.out).write_text(json.dumps(result,indent=2)+'\n');print('Wrote probe proposal only; canonical ledger unchanged')
if __name__=='__main__':main()
