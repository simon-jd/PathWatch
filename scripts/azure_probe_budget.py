"""Probe billing adapter for the canonical locked ledger, exact scope and Retail meters."""
import datetime as dt
from decimal import Decimal, ROUND_CEILING
import json
import re
import importlib.util
from pathlib import Path
_spec=importlib.util.spec_from_file_location('azure_probe_scope',Path(__file__).with_name('azure_probe_scope.py'))
scope=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(scope)
FREE_SOURCE='https://azure.microsoft.com/en-us/pricing/details/virtual-network/'


def quantities(typ,hours):
    rounded=hours.to_integral_value(rounding=ROUND_CEILING)
    return {'azurerm_linux_virtual_machine':{'vm_hours':hours,'egress_gb':rounded*Decimal('0.005')},'azurerm_public_ip':{'ip_hours':rounded},'azapi_update_resource':{'disk_month':rounded/Decimal(730),'transactions':rounded*10000}}.get(typ,{})


def meter_match(selector,category,region,sku):
    vm=('Virtual Machines Bpsv2 Series','B2pts v2') if sku=='Standard_B2pts_v2' else ('Virtual Machines Basv2 Series','B2ats v2')
    expected={'vm_hours':(vm[0],vm[1],vm[1],'1 Hour'),'ip_hours':('IP Addresses','Standard','Standard IPv4 Static Public IP','1 Hour'),'disk_month':('Standard HDD Managed Disks','S4 LRS','S4 LRS Disk','1/Month'),'transactions':('Standard HDD Managed Disks','S4 LRS','S4 LRS Disk Operations','10K'),'egress_gb':('Rtn Preference: MGN','Standard','Standard Data Transfer Out','1 GB')}[category]
    return selector.get('armRegionName')==region and tuple(selector.get(k) for k in ('productName','skuName','meterName','unitOfMeasure'))==expected and selector.get('tierMinimumUnits')==(100 if category=='egress_gb' else 0)


def resource_id(resource,sub,site):
    if resource['address'] in scope.arm_ids(sub,site):return scope.arm_ids(sub,site)[resource['address']]
    typ=resource['type'];value=resource['change'].get('after') or resource['change'].get('before') or {}
    prefix=f'/subscriptions/{sub}/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Network/'
    vnet=prefix+'virtualNetworks/vnet-pathwatch-'+site
    if typ in ('azurerm_subnet','azurerm_subnet_network_security_group_association','azurerm_subnet_route_table_association'):
        key=resource['address'].split('["')[-1].split('"')[0]
        return vnet+'/subnets/snet-'+key
    paths={'azurerm_virtual_network':'virtualNetworks/','azurerm_network_security_group':'networkSecurityGroups/','azurerm_route_table':'routeTables/','azurerm_network_security_rule':'networkSecurityGroups/nsg-pathwatch-'+site+'/securityRules/','azurerm_route':'routeTables/rt-pathwatch-'+site+'/routes/'}
    return prefix+paths[typ]+value['name']


def preflight(path,variables,now,budget):
    evidence=json.loads((budget.ROOT/path).read_text())
    if evidence.get('subscription_id')!=variables['subscription_id'] or evidence.get('location')!=variables['location'] or not dt.timedelta(0)<=now-budget.utc(evidence.get('captured_at'))<=dt.timedelta(minutes=15):raise ValueError('Fresh exact subscription/region quota/SKU/image capture required')
    items=[s for s in evidence.get('skus',[]) if s.get('name')==variables['vm_size'] and s.get('resourceType')=='virtualMachines']
    if len(items)!=1:raise ValueError('Exactly one current VM SKU record required')
    sku=items[0];capabilities={c.get('name'):c.get('value') for c in sku.get('capabilities',[])}
    if len(capabilities)!=len(sku.get('capabilities',[])) or sku.get('restrictions')!=[] or variables['location'] not in sku.get('locations',[]) or capabilities.get('vCPUs')!='2' or capabilities.get('CpuArchitectureType')!=scope.SKUS[variables['vm_size']][1] or 'V2' not in capabilities.get('HyperVGenerations','').split(','):raise ValueError('SKU restricted/unavailable or wrong CPU/image architecture')
    for name in ('cores',sku.get('family')):
        usage=[u for u in evidence.get('usage',[]) if u.get('name',{}).get('value','').lower()==str(name).lower()]
        if len(usage)!=1 or budget.money(usage[0]['currentValue'])+2>min(budget.money(usage[0]['limit']),Decimal(6)):raise ValueError('Fresh regional and family quota must fit 2 CPUs under 6')
    image=evidence.get('image',{})
    urn='Canonical:ubuntu-24_04-lts:'+scope.SKUS[variables['vm_size']][0]+':'+variables['image_version']
    image_id=f"/subscriptions/{variables['subscription_id']}/providers/Microsoft.Compute/locations/{variables['location']}/publishers/Canonical/artifactTypes/VMImage/offers/ubuntu-24_04-lts/skus/{scope.SKUS[variables['vm_size']][0]}/versions/{variables['image_version']}"
    if evidence.get('image_urn')!=urn or image.get('id','').lower()!=image_id.lower() or image.get('location','').lower()!=variables['location'] or image.get('architecture')!=scope.SKUS[variables['vm_size']][1] or image.get('hyperVGeneration')!='V2' or image.get('name')!=variables['image_version'] or image.get('osDiskImage',{}).get('operatingSystem')!='Linux' or image.get('plan') or image.get('dataDiskImages'):raise ValueError('Current pinned Ubuntu image architecture/generation mismatch')


def check(plan,proposal,ledger,now,budget):
    variables={k:v.get('value') for k,v in plan['variables'].items()};sub=variables['subscription_id']
    site=scope.validate(plan,sub);campaign=variables.get('campaign_id');end=variables['ttl'];region=variables['location']
    if not re.fullmatch(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}',campaign or ''):raise ValueError('Stable UUID campaign required')
    resources=[r for r in plan['resource_changes'] if r.get('mode')=='managed']
    actions={tuple(r['change']['actions']) for r in resources}
    if actions not in ({('create',)},{('no-op',)}):raise ValueError('Probe admission requires complete create or complete no-op; use reviewed deletion after partial failure')
    changed=actions=={('create',)};rows=proposal['resources'];by={r.get('address'):r for r in rows}
    if len(rows)!=len(by) or set(by)!=(set(r['address'] for r in resources) if changed else set()):raise ValueError('Exact probe proposal rows required')
    # Both retained parents need verified canonical identity and enough lifetime.
    for address in ('azurerm_resource_group.project','azurerm_storage_account.state'):
        history=[r for r in ledger['resources'] if r.get('address')==address]
        if not history:raise ValueError('Missing canonical retained parent reservation')
        latest=max(history,key=lambda r:budget.utc(r['end_utc']))
        prefix=f'/subscriptions/{sub}/resourceGroups/rg-pathwatch-lab'
        rid=latest.get('resource_id','')
        if latest.get('subscription_id')!=sub or latest.get('status') not in ('reserved','applied') or latest.get('deleted_utc') is not None or not re.fullmatch('[0-9a-f]{64}',latest.get('plan_sha256','')) or budget.utc(latest['end_utc'])<budget.utc(end) or (rid.lower()!=prefix.lower() if address.startswith('azurerm_resource_group') else not re.fullmatch(re.escape(prefix)+r'/providers/Microsoft.Storage/storageAccounts/pw[a-z0-9]{3,22}',rid,re.I)):raise ValueError('Unverified or insufficient retained parent reservation')
    prior=[r for r in ledger['resources'] if r.get('type')=='azurerm_linux_virtual_machine']
    b_history=[r for r in prior if r.get('region')=='swedencentral' or r.get('probe_site')=='azure-b']
    if any(not r.get('campaign_id') for r in b_history):raise ValueError('Missing durable B campaign history')
    campaigns={r['campaign_id'] for r in b_history}
    if len(campaigns)>2 or (site=='azure-b' and campaign not in campaigns and len(campaigns)>=2):raise ValueError('At most two B campaigns including DNS history')
    own=[r for r in ledger['resources'] if r.get('probe_site')==site]
    known_vms={a for site_name in scope.REGIONS for a,t in scope.addresses(site_name).items() if t=='azurerm_linux_virtual_machine'}
    if any(r.get('status')!='deleted' and r.get('address') not in known_vms for r in prior):raise ValueError('Another live VM exists; no second permanent NVA/client')
    for row in prior:
        if row.get('status')=='deleted':continue
        other_site=row.get('probe_site')
        if other_site not in scope.REGIONS or row.get('subscription_id')!=sub or row.get('address') not in scope.addresses(other_site) or row.get('resource_id')!=scope.arm_ids(sub,other_site).get(row.get('address')) or row.get('region')!=scope.REGIONS[other_site] or row.get('deleted_utc') is not None or row.get('status') not in ('reserved','applied') or not re.fullmatch('[0-9a-f]{64}',row.get('plan_sha256','')):raise ValueError('Live probe VM requires exact canonical identity')
        if not budget.utc(row.get('start_utc'))<=now<budget.utc(row.get('end_utc')):raise ValueError('Expired live probe requires cleanup/reconciliation')
    if changed and any(r.get('status')=='applied' and r.get('probe_site')==site for r in prior):raise ValueError('Existing applied probe cannot be recreated from lost state')
    if site=='azure-a' and any(r.get('campaign_id')!=campaign for r in own):raise ValueError('A lifetime cannot restart under another campaign')
    same_campaign=[r for r in ledger['resources'] if r.get('campaign_id')==campaign]
    if any(r.get('probe_site')!=site or r.get('status')=='deleted' for r in same_campaign):raise ValueError('Cannot reuse ended or foreign campaign identity')
    if not changed:
        if {r.get('address') for r in own}!={r['address'] for r in resources}:raise ValueError('No-op requires complete canonical probe metadata')
        starts={r.get('start_utc') for r in own}
        if len(starts)!=1:raise ValueError('No-op campaign start missing/ambiguous')
        start=next(iter(starts))
        for resource in resources:
            for row in [r for r in own if r.get('address')==resource['address']]:
                if row.get('campaign_id')!=campaign or row.get('subscription_id')!=sub or row.get('resource_id')!=resource_id(resource,sub,site) or row.get('type')!=resource['type'] or row.get('region')!=region or row.get('end_utc')!=end or row.get('status') not in ('reserved','applied') or row.get('deleted_utc') is not None or not re.fullmatch('[0-9a-f]{64}',row.get('plan_sha256','')):raise ValueError('No-op canonical identity/window mismatch')
    else:
        starts={r.get('start_utc') for r in rows}
        if len(starts)!=1:raise ValueError('One actual campaign start required')
        start=next(iter(starts))
        if any(r.get('start_utc')!=start or r.get('end_utc')!=end for r in same_campaign):raise ValueError('Campaign retry must preserve original timebox')
        preflight(proposal.get('preflight',''),variables,now,budget)
        capture=json.loads((budget.ROOT/proposal['network_capture']).read_text())
        if not dt.timedelta(0)<=now-budget.utc(capture.get('captured_at'))<=dt.timedelta(hours=24) or not capture.get('url','').startswith('https://prices.azure.com/api/retail/prices?') or not isinstance(capture.get('response',{}).get('Items'),list) or capture.get('response',{}).get('NextPageLink') or any(item.get('armRegionName')!=region for item in capture.get('response',{}).get('Items',[])):raise ValueError('Complete fresh regional network Retail evidence required')
    hours=Decimal(str((budget.utc(end)-budget.utc(start)).total_seconds()))/3600
    if not 0<hours<=Decimal(840 if site=='azure-a' else 48) or not budget.utc(start)<=now<budget.utc(end):raise ValueError('Active A<=840h / B<=48h original campaign window required')
    total=Decimal(0)
    for resource in resources:
        address=resource['address'];typ=resource['type']
        if not changed:
            # No-op must retain every exact bound, even when no new money is reserved.
            for row in [r for r in own if r.get('address')==address]:
                if {k:budget.money(v) for k,v in row.get('bounds',{}).items()}!=quantities(typ,hours):raise ValueError('No-op usage metadata is missing or changed')
            continue
        row=by[address];expected=quantities(typ,hours)
        if row.get('type')!=typ or row.get('probe_site')!=site or row.get('campaign_id')!=campaign or row.get('subscription_id')!=sub or row.get('resource_id')!=resource_id(resource,sub,site) or row.get('region')!=region or row.get('end_utc')!=end:raise ValueError('Probe row ownership/campaign/window mismatch')
        if {k:budget.money(v) for k,v in row.get('bounds',{}).items()}!=expected:raise ValueError('Exact bounded hours/disk transactions/egress required')
        meters=row.get('meters',[])
        if len(meters)!=len(expected) or {m.get('category') for m in meters}!=set(expected):raise ValueError('Every probe billing category must be priced exactly once')
        cost=Decimal(0)
        for meter in meters:
            category=meter['category'];selector=meter['selector']
            if not meter_match(selector,category,region,variables['vm_size']):raise ValueError('Exact nonSpot Linux SKU/product/unit/tier/region required')
            price=budget.validate_meter(json.loads((budget.ROOT/meter['capture']).read_text()),selector,now)
            cost+=price*expected[category]/(10000 if category=='transactions' else 1)
        if not expected and (row.get('price_source')!=FREE_SOURCE or budget.money(row['estimate_usd'])!=0):raise ValueError('Only bounded unmetered network configuration has zero cost')
        value=resource['change']['after'];tags=value.get('body',{}).get('tags',{}) if typ=='azapi_update_resource' else value.get('tags',{})
        if tags:
            hourly=budget.money(tags['est_usd_per_hour'])
            if hourly<cost/hours or (not expected and hourly!=0):raise ValueError('Probe hourly tag understates bounded cost')
            if hourly>Decimal('0.02') and (row.get('same_session_delete') is not True or hours>12):raise ValueError('Expensive probe requires same-session deletion within12h')
        if budget.money(row['estimate_usd'])<cost:raise ValueError('Probe estimate below bounded cost')
        total+=budget.money(row['estimate_usd'])
    budget.enforce_cap(budget.total_ledger(ledger),total)
    return rows
