#!/usr/bin/env python3
"""Price exact DNS saved plan. Output only; canonical admission owns reservation."""
import argparse
import datetime as dt
from decimal import Decimal, ROUND_UP
import json
from pathlib import Path
import subprocess
import azure_budget as budget
import azure_dns_scope as scope

KEYS=('meterId','skuId','armRegionName','unitOfMeasure','productName','skuName','meterName','tierMinimumUnits')
METERS={
 'vm_hours':('vm-b2pts','Virtual Machines Bpsv2 Series','B2pts v2','B2pts v2','1 Hour',None),
 'egress_gb':('bandwidth','Rtn Preference: MGN','Standard','Standard Data Transfer Out','1 GB',None),
 'ip_hours':('network','IP Addresses','Standard','Standard IPv4 Static Public IP','1 Hour',None),
 'disk_month':('hdd-s4','Standard HDD Managed Disks','S4 LRS','S4 LRS Disk','1/Month',None),
 'transactions':('hdd-s4','Standard HDD Managed Disks','S4 LRS','S4 LRS Disk Operations','10K',None),
 'zone_month':('dns','Azure DNS','Private','Private Zone','1',''),
 'private_queries':('dns','Azure DNS','Private','Private Queries','1M','Zone 1'),
 'endpoint_month':('traffic-manager','Traffic Manager','Azure Endpoint','Azure Endpoint Health Checks','1','Global'),
 'public_queries':('traffic-manager','Traffic Manager','Azure Endpoint','DNS Queries','1M','Global')}
FREE_SOURCE='docs/azure-prices/dns-components.json'
def quantities(typ,start,end):
    hours=Decimal(str((budget.utc(end)-budget.utc(start)).total_seconds()))/3600
    # Reserve every touched UTC calendar day, including either partial day.
    days=Decimal((budget.utc(end).date()-budget.utc(start).date()).days+1)
    return {'azurerm_linux_virtual_machine':{'vm_hours':hours,'egress_gb':Decimal('.25')},'azurerm_public_ip':{'ip_hours':hours},'azapi_update_resource':{'disk_month':hours/730,'transactions':Decimal(1000000)},'azurerm_private_dns_zone':{'zone_month':days/28,'private_queries':Decimal(10000)},'azurerm_traffic_manager_profile':{'public_queries':Decimal(10000)},'azurerm_traffic_manager_azure_endpoint':{'endpoint_month':Decimal(1)}}.get(typ,{})
def region(resource):
    value=resource['change'].get('after') or resource['change'].get('before') or {}
    if resource['type'].startswith(('azurerm_private_dns_','azurerm_traffic_manager_')):return 'global'
    if value.get('location'):return value['location']
    key=resource['address'].split('["')[-1].split('"')[0].split('-')[0]
    return scope.REGIONS[key]
def meter_match(item,category,location):
    _,product,sku,meter,unit,meter_region=METERS[category]
    return all(item.get(k)==v for k,v in {'productName':product,'skuName':sku,'meterName':meter,'unitOfMeasure':unit,'armRegionName':location if meter_region is None else meter_region}.items())
def billed_quantity(category,quantity):return quantity/(10000 if category=='transactions' else 1000000 if category.endswith('_queries') else 1)
def proposal(plan,start,capture_date='2026-10-23'):
    end=plan['variables']['ttl']['value'];campaign=plan['variables']['campaign_id']['value'];rows=[]
    for r in plan['resource_changes']:
        if r.get('mode')!='managed' or r['change']['actions']==['no-op']:continue
        typ=r['type'];location=region(r);bounds=quantities(typ,start,end)
        row={'address':r['address'],'subscription_id':plan['variables']['subscription_id']['value'],'type':typ,'region':location,'start_utc':start,'end_utc':end,'estimate_usd':'0','bounds':{k:str(v) for k,v in bounds.items()},'meters':[],'same_session_delete':True,'campaign_id':campaign}
        cost=Decimal(0)
        for category,q in bounds.items():
            path=f'docs/azure-prices/{capture_date}-{METERS[category][0]}-{location}.json'
            evidence=json.loads((budget.ROOT/path).read_text())
            candidates=[i for i in evidence['response']['Items'] if meter_match(i,category,location) and i.get('currencyCode')=='USD' and i.get('type')=='Consumption' and Decimal(str(i['retailPrice']))>0]
            if not candidates:raise ValueError('Missing exact paid DNS meter '+category)
            item=max(candidates,key=lambda i:Decimal(str(i['retailPrice'])))
            row['meters'].append({'category':category,'capture':path,'selector':{k:item[k] for k in KEYS}})
            cost+=Decimal(str(item['retailPrice']))*billed_quantity(category,q)
        if not bounds:row['price_source']=FREE_SOURCE
        row['estimate_usd']=str(cost.quantize(Decimal('.01'),rounding=ROUND_UP)) if cost else '0'
        rows.append(row)
    return {'resources':rows}
def main():
    p=argparse.ArgumentParser();p.add_argument('--plan',required=True);p.add_argument('--start-utc',required=True);p.add_argument('--capture-date',default='2026-10-23');p.add_argument('--out',required=True);a=p.parse_args()
    path=Path(a.plan).resolve();plan=json.loads(subprocess.check_output(['terraform','show','-json',str(path)],cwd=path.parent,text=True));result=proposal(plan,a.start_utc,a.capture_date)
    with budget.ledger_lock():budget.check(plan,result,json.loads(budget.LEDGER.read_text()),dt.datetime.now(dt.timezone.utc))
    Path(a.out).write_text(json.dumps(result,indent=2)+'\n')
if __name__=='__main__':main()
