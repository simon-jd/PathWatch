"""Task4 adapter called inside the existing canonical ledger lock and cap check."""
import datetime as dt
from decimal import Decimal
import importlib.util
from pathlib import Path
import json
import re

def load(name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(name+'.py'));module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
scope=load('azure_dns_scope')

def active_reservation(ledger,campaign,end,now,budget,sub):
    """Bind no-op/inventory controls to the complete live canonical reservation.

    Caller holds the canonical ledger lock. Repriced retries may duplicate rows,
    but may never change campaign, original window, ownership or usage bounds.
    """
    from dns_proposal import quantities
    rows=ledger['resources']
    if any(r.get('address','').startswith('module.appliance.') and r.get('status')!='deleted' for r in rows):raise ValueError('Finish and reconcile Task3 deletion before DNS activity')
    prior=[r for r in rows if r.get('type')=='azurerm_linux_virtual_machine' and r.get('region')=='swedencentral']
    campaigns={r.get('campaign_id') for r in prior}
    if None in campaigns or campaign not in campaigns or len(campaigns)>2:raise ValueError('DNS campaign requires durable B reservation history')
    history=[r for r in rows if r.get('address','').startswith(scope.PREFIX) and r.get('campaign_id')==campaign]
    starts={r.get('start_utc') for r in history}
    if len(starts)!=1 or any(r.get('end_utc')!=end for r in history):raise ValueError('DNS original reserved window mismatch')
    start=next(iter(starts));begin=budget.utc(start);expiry=budget.utc(end)
    if not begin<=now<expiry or not dt.timedelta(0)<expiry-begin<=dt.timedelta(hours=2):raise ValueError('DNS active same-session reserved window required')
    active=[r for r in rows if r.get('address','').startswith(scope.PREFIX) and r.get('status')!='deleted']
    wanted=scope.resource_ids(sub)
    if {r.get('address') for r in active}!=set(wanted):raise ValueError('DNS complete active reservation required')
    for row in active:
        typ=row['address'].split('.')[2]
        site=row['address'].split('["')[-1].split('"')[0].split('-')[0]
        region='global' if typ.startswith(('azurerm_private_dns_','azurerm_traffic_manager_')) else scope.REGIONS.get(site)
        if row.get('subscription_id')!=sub or row.get('campaign_id')!=campaign or row.get('start_utc')!=start or row.get('end_utc')!=end or row.get('type')!=typ or row.get('region')!=region or row.get('same_session_delete') is not True:raise ValueError('DNS active reservation ownership/window mismatch')
        if row.get('status') not in ('reserved','applied') or row.get('deleted_utc') is not None or not re.fullmatch(r'[0-9a-f]{64}',row.get('plan_sha256','')):raise ValueError('DNS active canonical plan reservation required')
        if {k:budget.money(v) for k,v in row.get('bounds',{}).items()}!=quantities(typ,start,end):raise ValueError('DNS active reserved usage/query bounds mismatch')
    budget.enforce_cap(budget.total_ledger(ledger),0)
    return start

def check(plan,proposal,ledger,now,budget):
    # dns_proposal imports the canonical budget module; load only after its initialization.
    import sys
    previous=list(sys.path);sys.path.insert(0,str(Path(__file__).parent))
    try:pricing=load('dns_proposal')
    finally:sys.path[:]=previous
    sub=plan['variables']['subscription_id']['value'];scope.validate(plan,sub)
    changed=[r for r in plan['resource_changes'] if r.get('mode')=='managed' and r['change']['actions']!=['no-op']]
    rows=proposal['resources'];by={r['address']:r for r in rows}
    if len(by)!=len(rows) or set(by)!={r['address'] for r in changed}:raise ValueError('Every DNS change needs exact canonical row')
    if any(r.get('mode')=='managed' and r['change']['actions']==['no-op'] for r in plan['resource_changes']):
        active_reservation(ledger,plan['variables']['campaign_id']['value'],plan['variables']['ttl']['value'],now,budget,sub)
    if not changed:return []
    end=plan['variables']['ttl']['value'];campaign=plan['variables']['campaign_id']['value']
    if len({r.get('start_utc') for r in rows})!=1:raise ValueError('DNS one actual start required')
    start=rows[0]['start_utc'];hours=Decimal(str((budget.utc(end)-budget.utc(start)).total_seconds()))/3600
    if not 0<hours<=2 or budget.utc(start)>now or budget.utc(end)<=now:raise ValueError('DNS active same-session <=2h timebox required')
    if any(r.get('address','').startswith('module.appliance.') and r.get('status')!='deleted' for r in ledger['resources']):raise ValueError('Finish and reconcile Task3 deletion before DNS creation')
    prior=[r for r in ledger['resources'] if r.get('type')=='azurerm_linux_virtual_machine' and r.get('region')=='swedencentral']
    if any(not r.get('campaign_id') for r in prior):raise ValueError('Reconcile existing B campaign history first')
    campaigns={r['campaign_id'] for r in prior}
    if campaign not in campaigns and len(campaigns)>=2:raise ValueError('At most two durable B measurement campaigns')
    if any(r.get('start_utc')!=start or r.get('end_utc')!=end for r in prior if r['campaign_id']==campaign):raise ValueError('A campaign retry must preserve its original reserved window')
    costs={};total=Decimal(0)
    for resource in changed:
        r=by[resource['address']];typ=resource['type'];location=pricing.region(resource)
        if r.get('subscription_id')!=sub or r.get('type')!=typ or r.get('region')!=location or r.get('end_utc')!=end or r.get('campaign_id')!=campaign or r.get('same_session_delete') is not True:raise ValueError('DNS row topology/campaign/TTL mismatch')
        expected=pricing.quantities(typ,start,end)
        if {k:budget.money(v) for k,v in r.get('bounds',{}).items()}!=expected:raise ValueError('DNS exact usage/time/query bounds required')
        meters=r.get('meters',[])
        if len(meters)!=len(expected) or {m.get('category') for m in meters}!=set(expected):raise ValueError('All DNS categories priced exactly once')
        cost=Decimal(0)
        for meter in meters:
            category=meter['category'];selector=meter['selector']
            if not pricing.meter_match(selector,category,location):raise ValueError('DNS exact meter product/SKU/unit/global mapping required')
            evidence=json.loads((budget.ROOT/meter['capture']).read_text())
            cost+=budget.validate_meter(evidence,selector,now)*pricing.billed_quantity(category,expected[category])
        if not expected:
            if r.get('price_source')!=pricing.FREE_SOURCE or budget.money(r['estimate_usd'])!=0:raise ValueError('DNS free config requires official component source')
            evidence=json.loads((budget.ROOT/pricing.FREE_SOURCE).read_text())
            if typ not in evidence['resource_types']:raise ValueError('Unreviewed free DNS type')
            if location!='global':
                capture=json.loads((budget.ROOT/evidence['retail_captures'][location]).read_text());age=now-budget.utc(capture['captured_at'])
                if not dt.timedelta(0)<=age<=dt.timedelta(hours=24) or capture['response'].get('NextPageLink') or not capture['url'].startswith('https://prices.azure.com/api/retail/prices?') or any(i['armRegionName']!=location for i in capture['response']['Items']):raise ValueError('Exact fresh regional network capture required')
        if budget.money(r['estimate_usd'])<cost:raise ValueError('DNS estimate below conservative bound')
        value=resource['change']['after'];tags=value.get('body',{}).get('tags',{}) if typ=='azapi_update_resource' else value.get('tags',{})
        if typ not in scope.TAGLESS:
            tag=budget.money(tags['est_usd_per_hour'])
            if tag<cost/hours:raise ValueError('DNS hourly tag understates cost')
            if not expected and tag!=0:raise ValueError('Unmetered DNS configuration tag must be zero')
        costs[resource['address']]=cost;total+=budget.money(r['estimate_usd'])
    profile=scope.address('azurerm_traffic_manager_profile')
    if profile in costs:
        value=next(r['change']['after'] for r in changed if r['address']==profile)
        children=sum(costs.get(scope.address('azurerm_traffic_manager_azure_endpoint',site),0) for site in scope.REGIONS)
        if budget.money(value['tags']['est_usd_per_hour'])*hours<costs[profile]+children:raise ValueError('TM parent hourly tag must cover untaggable endpoints')
    budget.enforce_cap(budget.total_ledger(ledger),total)
    return rows
