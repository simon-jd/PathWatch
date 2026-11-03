#!/usr/bin/env python3
"""Build a reviewable Task3 priced proposal from the exact saved plan; never reserve/apply."""
import argparse
import datetime as dt
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import azure_budget as budget

KEYS=('meterId','skuId','armRegionName','unitOfMeasure','productName','skuName','meterName','tierMinimumUnits')
METERS={'vm_hours':('vm-b2pts','Virtual Machines Bpsv2 Series','B2pts v2'),'egress_gb':('bandwidth','Rtn Preference: MGN','Standard Data Transfer Out'),'ip_hours':('network','IP Addresses','Standard IPv4 Static Public IP'),'disk_month':('hdd-s4','Standard HDD Managed Disks','S4 LRS Disk'),'transactions':('hdd-s4','Standard HDD Managed Disks','S4 LRS Disk Operations')}
def proposal(plan,start):
    rows=[];location=plan['variables']['location']['value'];end=plan['variables']['ttl']['value'];hours=Decimal(str((budget.utc(end)-budget.utc(start)).total_seconds()))/Decimal(3600)
    if not 0<hours<=4:raise ValueError('Task3 reservation must be positive <=4h')
    for resource in plan['resource_changes']:
        if resource.get('mode')!='managed' or resource['change']['actions']==['no-op']:continue
        typ=resource['type'];row={'address':resource['address'],'type':typ,'region':location,'start_utc':start,'end_utc':end,'estimate_usd':'0','meters':[],'same_session_delete':True}
        if typ in budget.network_scope.NETWORK_TYPES:row['price_source']=budget.network_scope.PRICE_SOURCE
        elif typ in budget.nva_scope.TYPES:
            bounds={'azurerm_linux_virtual_machine':{'vm_hours':str(hours),'egress_gb':'2'},'azurerm_public_ip':{'ip_hours':str(hours)},'azapi_update_resource':{'disk_month':str(hours/Decimal(730)),'transactions':'1000000'},'azurerm_network_interface':{}}[typ]
            row['bounds']=bounds;cost=Decimal(0)
            for category,q in bounds.items():
                capture,product,meter=METERS[category];path=f'docs/azure-prices/2026-10-23-{capture}-{location}.json'
                evidence=json.loads((budget.ROOT/path).read_text())
                candidates=[x for x in evidence['response']['Items'] if x.get('productName')==product and x.get('meterName')==meter and x.get('type')=='Consumption' and x.get('currencyCode')=='USD' and x.get('armRegionName')==location and Decimal(str(x.get('retailPrice',0)))>0]
                item=max(candidates,key=lambda x:Decimal(str(x['retailPrice'])))
                row['meters'].append({'category':category,'capture':path,'selector':{k:item[k] for k in KEYS}})
                cost+=Decimal(str(item['retailPrice']))*Decimal(q)/(10000 if category=='transactions' else 1)
            if cost:row['estimate_usd']=str(cost.quantize(Decimal('0.01'),rounding='ROUND_UP'))
        else:raise ValueError('Unexpected type in hybrid saved plan')
        rows.append(row)
    return {'resources':rows}
def main():
    p=argparse.ArgumentParser();p.add_argument('--plan',required=True);p.add_argument('--start-utc',required=True);p.add_argument('--out',required=True);a=p.parse_args()
    path=Path(a.plan).resolve();plan=json.loads(subprocess.check_output(['terraform','show','-json',str(path)],cwd=path.parent,text=True));result=proposal(plan,a.start_utc)
    # Read+check under same stable canonical lock; writes only proposal, no journal.
    with budget.ledger_lock():budget.check(plan,result,json.loads(budget.LEDGER.read_text()),dt.datetime.now(dt.timezone.utc))
    Path(a.out).write_text(json.dumps(result,indent=2)+'\n');print('Wrote proposed priced rows only; review then reserve separately')
if __name__=='__main__':main()
