#!/usr/bin/env python3
"""Fail-closed local admission. Reservation is written before Terraform apply, never after.
Pricing evidence is raw Retail API JSON plus exact meter selectors, not reference rates.
"""
import argparse
import importlib.util
from contextlib import contextmanager
import fcntl
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import urllib.parse
import urllib.request
# Resolve the adjacent reviewed helper even when this script is loaded by a
# test worker outside the checkout (without changing sys.path or credentials).
_network_spec = importlib.util.spec_from_file_location('azure_network_scope', Path(__file__).with_name('azure_network_scope.py'))
network_scope = importlib.util.module_from_spec(_network_spec)
_network_spec.loader.exec_module(network_scope)
_nva_spec = importlib.util.spec_from_file_location('azure_nva_scope', Path(__file__).with_name('azure_nva_scope.py'))
nva_scope = importlib.util.module_from_spec(_nva_spec)
_nva_spec.loader.exec_module(nva_scope)
_dns_spec = importlib.util.spec_from_file_location('azure_dns_budget', Path(__file__).with_name('azure_dns_budget.py'))
dns_budget = importlib.util.module_from_spec(_dns_spec)
_dns_spec.loader.exec_module(dns_budget)
_parent_spec = importlib.util.spec_from_file_location('azure_parent_renewal', Path(__file__).with_name('azure_parent_renewal.py'))
parent_renewal = importlib.util.module_from_spec(_parent_spec)
_parent_spec.loader.exec_module(parent_renewal)
_probe_spec = importlib.util.spec_from_file_location('azure_probe_budget', Path(__file__).with_name('azure_probe_budget.py'))
probe_budget = importlib.util.module_from_spec(_probe_spec)
_probe_spec.loader.exec_module(probe_budget)

_xdp_spec = importlib.util.spec_from_file_location('azure_xdp_budget', Path(__file__).with_name('azure_xdp_budget.py'))
xdp_budget = importlib.util.module_from_spec(_xdp_spec)
_xdp_spec.loader.exec_module(xdp_budget)

ROOT=Path(__file__).resolve().parents[1]
LEDGER=ROOT/'docs/dev/azure-ledger.json'
FREE={'azurerm_resource_group','azurerm_role_assignment','azurerm_user_assigned_identity','azurerm_federated_identity_credential','azurerm_storage_container'}
FREE |= network_scope.NETWORK_TYPES
FREE |= {'azurerm_network_interface'}
ALLOWED=FREE|{'azurerm_storage_account'}|nva_scope.TYPES
def utc(s):
    if not isinstance(s,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z',s):raise ValueError('Strict UTC timestamp required')
    return dt.datetime.strptime(s,'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
def money(v):
    try:n=Decimal(str(v))
    except InvalidOperation:raise ValueError('Invalid amount')
    if not n.is_finite() or n<0:raise ValueError('Nonnegative finite amount required')
    return n
def total_ledger(ledger):return sum((money(r['estimate_usd']) for r in ledger['resources']),Decimal(0))
def enforce_cap(existing,new):
    if money(existing)+money(new)>Decimal(18):raise ValueError('PathWatch 18 USD admission threshold exceeded (20 USD total cap)')
def validate_meter(evidence,selector,now):
    age=now-utc(evidence['captured_at']) if 'captured_at' in evidence else dt.timedelta(days=999)
    if age<dt.timedelta(0) or age>dt.timedelta(hours=24):raise ValueError('Price capture missing/stale/future')
    if not evidence.get('url','').startswith('https://prices.azure.com/api/retail/prices?') or evidence.get('response',{}).get('NextPageLink'):raise ValueError('Complete official Retail API capture required')
    keys=('meterId','skuId','armRegionName','unitOfMeasure','productName','skuName','meterName','tierMinimumUnits')
    if set(selector)!=set(keys):raise ValueError('Exact meter selector required')
    items=[i for i in evidence['response']['Items'] if all(i.get(k)==selector[k] for k in keys)]
    if len(items)!=1:raise ValueError('Unknown/ambiguous/mismatched price')
    item=items[0]
    if item.get('currencyCode')!='USD' or item.get('type')!='Consumption':raise ValueError('USD consumption meter required')
    if utc(item['effectiveStartDate'].replace('+00:00','Z'))>now:raise ValueError('Future effective price')
    price=money(item['retailPrice'])
    if any(money(i['retailPrice'])>price for i in evidence['response']['Items'] if i.get('meterId')==item['meterId'] and i.get('skuId')==item['skuId'] and i.get('type')=='Consumption' and i.get('currencyCode')=='USD'):raise ValueError('Use highest published paid tier conservatively')
    if price==0:raise ValueError('Do not assume free tier; choose conservative paid meter')
    return price

def enforce_reconciliation(ledger):
    if ledger.get('reconciliation_block'):raise ValueError('Azure admission blocked pending cost reconciliation; deletion remains allowed')

def check(plan,proposal,ledger,now):
    enforce_reconciliation(ledger)
    sub=os.environ.get('AZURE_SUBSCRIPTION_ID','')
    if not re.fullmatch(r'[0-9a-fA-F-]{36}',sub) or plan.get('variables',{}).get('subscription_id',{}).get('value')!=sub:raise ValueError('Explicit subscription must match plan')
    if xdp_budget.claimed(plan,proposal):return xdp_budget.check(plan,proposal,ledger,now,SimpleNamespace(**globals()))
    if parent_renewal.matches(plan,proposal):return parent_renewal.check(plan,proposal,ledger,now,SimpleNamespace(**globals()))
    if probe_budget.scope.matches(plan):return probe_budget.check(plan,proposal,ledger,now,SimpleNamespace(**globals()))
    if dns_budget.scope.is_dns_plan(plan):return dns_budget.check(plan,proposal,ledger,now,SimpleNamespace(**globals()))
    xdp_budget.ordinary_bootstrap_cap(plan)
    return check_creation(plan,proposal,ledger,now)

def check_creation(plan,proposal,ledger,now):
    """Shared creation billing; renewal calls only after exact immutable-state validation."""
    sub=plan['variables']['subscription_id']['value']
    hybrid=nva_scope.is_nva_plan(plan)
    if hybrid: nva_scope.validate(plan,sub,network_scope)
    elif network_scope.is_network_plan(plan): network_scope.validate(plan, sub)
    changes=[x for x in plan.get('resource_changes',[]) if x.get('mode')=='managed' and x['change']['actions']!=['no-op']]
    entries=proposal['resources'];by={r['address']:r for r in entries}
    if len(by)!=len(entries) or set(by)!={r['address'] for r in changes}:raise ValueError('Every changed resource needs one exact precreation ledger entry')
    if hybrid and entries:
        if len({r.get('start_utc') for r in entries})!=1 or {r.get('end_utc') for r in entries}!={plan.get('variables',{}).get('ttl',{}).get('value')}:raise ValueError('All hybrid VM/disk/IP/network rows must share one actual start and root TTL')
        for r in entries:
            if not r.get('same_session_delete') or utc(r['end_utc'])-utc(r['start_utc'])>dt.timedelta(hours=4):raise ValueError('Entire Task3 campaign must share <=4h same-session cleanup timebox')
    total=Decimal(0)
    for change in changes:
        typ=change['type'];after=change['change'].get('after')
        if typ not in ALLOWED or change['change']['actions']!=['create'] or not after:raise ValueError('Guard allows reviewed Task1/network creates only; review updates/deletes separately')
        r=by[change['address']]
        for key,value in after.items():
            if isinstance(value,str) and value.lower().startswith('/subscriptions/'):
                allowed=f'/subscriptions/{sub}/resourceGroups/rg-pathwatch-lab'
                if value.lower()!=allowed.lower() and not value.lower().startswith(allowed.lower()+'/'):
                    raise ValueError('Resource ID outside exact project scope')
        if r['type']!=typ or r['region']!=plan['variables']['location']['value'] or after.get('location',r['region'])!=r['region']:raise ValueError('Resource type/region mismatch')
        start,end=utc(r['start_utc']),utc(r['end_utc']);hours=Decimal(str((end-start).total_seconds()))/Decimal(3600)
        if start>now or end<=now or hours<=0 or hours>840:raise ValueError('Bounded active timebox <=840h required')
        if after.get('resource_group_name', 'rg-pathwatch-lab')!='rg-pathwatch-lab' or (typ=='azurerm_resource_group' and after.get('name')!='rg-pathwatch-lab'):raise ValueError('Unauthorized resource group')
        if typ in {'azurerm_resource_group','azurerm_storage_account','azurerm_user_assigned_identity'} | network_scope.PARENTS | nva_scope.TYPES:
            tags=after.get('body',{}).get('tags',{}) if typ=='azapi_update_resource' else after.get('tags',{})
            if tags.get('project')!='pathwatch' or tags.get('owner')!='codex-pathwatch' or tags.get('ttl')!=r['end_utc']:raise ValueError('Required tags/TTL mismatch')
            hourly=money(tags.get('est_usd_per_hour','NaN'))
        else:hourly=Decimal(0)
        if typ=='azurerm_role_assignment' and after.get('role_definition_name')!='Storage Blob Data Contributor':raise ValueError('Only scoped Blob data role permitted')
        cost=Decimal(0)
        if typ in network_scope.NETWORK_TYPES:
            if r.get('price_source') != network_scope.PRICE_SOURCE or money(r['estimate_usd']) != 0 or hourly != 0:
                raise ValueError('Network-only rows require exact documented unmetered source and zero estimate')
            evidence=json.loads((ROOT/network_scope.PRICE_SOURCE).read_text())
            if evidence.get('classification') != 'unmetered_network_configuration' or evidence.get('source') != 'https://azure.microsoft.com/en-us/pricing/details/virtual-network/' or typ not in evidence.get('resource_types', []):
                raise ValueError('Missing official free-configuration evidence')
            capture=evidence.get('retail_captures', {}).get(r['region'])
            if not capture: raise ValueError('Refresh Retail API capture for this exact network region before admission')
            retail=json.loads((ROOT/capture).read_text())
            age=now-utc(retail.get('captured_at', ''))
            if age < dt.timedelta(0) or age > dt.timedelta(hours=24) or not retail.get('url','').startswith('https://prices.azure.com/api/retail/prices?') or retail.get('response',{}).get('NextPageLink'):
                raise ValueError('Network pricing evidence stale/incomplete/unofficial')
            if any(item.get('armRegionName') != r['region'] for item in retail['response']['Items']):
                raise ValueError('Network Retail capture region mismatch')
        if typ in nva_scope.TYPES:
            if not hybrid or hours>4 or not r.get('same_session_delete'):raise ValueError('Task3 resources require same-session deletion and <=4h')
            if typ=='azurerm_network_interface':
                if hourly!=0 or r.get('meters') or money(r['estimate_usd'])!=0:raise ValueError('NIC is unmetered; VM/IP/disk are separately priced')
            else:
                expected_bounds={'azurerm_linux_virtual_machine':{'vm_hours','egress_gb'},'azurerm_public_ip':{'ip_hours'},'azapi_update_resource':{'disk_month','transactions'}}[typ]
                bounds=r.get('bounds',{})
                if set(bounds)!=expected_bounds or {m.get('category') for m in r.get('meters',[])}!=expected_bounds or len(r['meters'])!=len(expected_bounds):raise ValueError('Price every VM/IP/disk/request/egress category exactly once')
                for category,quantity in bounds.items():
                    q=money(quantity)
                    if category.endswith('_hours') and q!=hours:raise ValueError('Hourly quantity must equal exact reservation timebox')
                    if category=='disk_month' and q!=hours/Decimal(730):raise ValueError('Disk month quantity must equal hours/730')
                    if category=='transactions' and q!=Decimal(1000000):raise ValueError('Task3 monitor requires exactly1M conservative16KiB disk transactions reserved')
                    if category=='egress_gb' and q!=Decimal(2):raise ValueError('Task3 monitor requires exactly2GB per-VM outbound traffic reserved')
                for m in r['meters']:
                    selector=m['selector'];category=m['category']
                    evidence=json.loads((ROOT/m['capture']).read_text());price=validate_meter(evidence,selector,now)
                    if selector['armRegionName']!=r['region']:raise ValueError('NVA price region mismatch')
                    expected={'vm_hours':('Virtual Machines Bpsv2 Series','B2pts v2','B2pts v2','1 Hour'),'ip_hours':('IP Addresses','Standard','Standard IPv4 Static Public IP','1 Hour'),'disk_month':('Standard HDD Managed Disks','S4 LRS','S4 LRS Disk','1/Month'),'transactions':('Standard HDD Managed Disks','S4 LRS','S4 LRS Disk Operations','10K'),'egress_gb':('Rtn Preference: MGN','Standard','Standard Data Transfer Out','1 GB')}[category]
                    if tuple(selector[k] for k in ('productName','skuName','meterName','unitOfMeasure'))!=expected:raise ValueError('Meter product/SKU/unit does not match bounded NVA category')
                    quantity=money(bounds[category])/(Decimal(10000) if category=='transactions' else Decimal(1))
                    cost+=price*quantity
                if hourly<cost/hours:raise ValueError('Hourly tag understates bounded VM/IP/disk costs')
        elif typ=='azurerm_storage_account':
            if after.get('account_tier')!='Standard' or after.get('account_replication_type')!='LRS':raise ValueError('Only Standard_LRS permitted')
            if set(r['bounds'])!={'storage_gb','write_requests','read_requests','other_requests','egress_gb'}:raise ValueError('Explicit storage/request/egress bounds required')
            if not (0<money(r['bounds']['storage_gb'])<=1) or any(money(r['bounds'][k])>10000 for k in ('write_requests','read_requests','other_requests')) or money(r['bounds']['egress_gb'])>1:raise ValueError('Task 1 storage bounds exceeded')
            # All priced categories are mandatory; no silently unpriced storage/requests/egress.
            meters=r['meters']
            if {m['category'] for m in meters}!=set(r['bounds']):raise ValueError('Every bound requires captured pricing')
            for m in meters:
                evidence=json.loads((ROOT/m['capture']).read_text());price=validate_meter(evidence,m['selector'],now)
                if m['selector']['armRegionName']!=r['region']:raise ValueError('Price region mismatch')
                category=m['category'];unit=m['selector']['unitOfMeasure']
                names={'storage_gb':'Hot LRS Data Stored','write_requests':'Hot LRS Write Operations','read_requests':'Hot Read Operations','other_requests':'LRS List and Create Container Operations','egress_gb':'Standard Data Transfer Out'}
                if m['selector']['meterName']!=names[category]:raise ValueError('Meter does not match resource usage category')
                if category!='egress_gb' and (m['selector']['productName']!='General Block Blob v2' or m['selector']['skuName']!='Hot LRS'):raise ValueError('Storage SKU/product mismatch')
                if category=='egress_gb' and m['selector']['productName']!='Rtn Preference: MGN':raise ValueError('Use conservative public MGN egress pricing')
                expected={'storage_gb':'1 GB/Month','write_requests':'10K','read_requests':'10K','other_requests':'10K','egress_gb':'1 GB'}[category]
                if unit!=expected:raise ValueError('Unexpected metering unit')
                quantity=money(r['bounds'][category])
                if category=='storage_gb':quantity*=hours/Decimal(730)
                elif category.endswith('requests'):quantity/=Decimal(10000)
                cost+=price*quantity
            if hourly<cost/hours:raise ValueError('Hourly tag underestimates metered reservation')
        elif r.get('meters'):raise ValueError('Free ARM child/identity resource must not invent meters')
        if money(r['estimate_usd'])<cost:raise ValueError('Reservation below bounded metered cost')
        if hourly>Decimal('0.02') and (not r.get('same_session_delete') or hours>12):raise ValueError('Expensive resource requires same-session deletion <=12h')
        total+=money(r['estimate_usd'])
    enforce_cap(total_ledger(ledger),total)
    return entries

@contextmanager
def ledger_lock():
    """Every ledger reader/writer uses this stable sidecar, never the replaced inode."""
    with Path(str(LEDGER)+'.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def persist_ledger(ledger):
    """Caller holds ledger_lock across read, cap validation and durable replacement."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=LEDGER.parent,
                prefix='.'+LEDGER.name+'.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(ledger, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, LEDGER)
        directory_fd = os.open(LEDGER.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def admit(command, plan, proposal, digest):
    # Serializes both admission readers and writers. The lock spans the latest
    # snapshot, cap calculation, append and fsynced atomic persistence.
    with ledger_lock():
        now = dt.datetime.now(dt.timezone.utc)
        ledger = json.loads(LEDGER.read_text())
        enforce_reconciliation(ledger)
        changed = any(x.get('mode') == 'managed' and x['change']['actions'] != ['no-op']
                      for x in plan.get('resource_changes', []))
        if not changed:
            check(plan, proposal, ledger, now)
            print('PASS: verified no-change plan; no new resource reservation required')
            return
        if command == 'reserve':
            if any(r.get('plan_sha256') == digest for r in ledger['resources']):
                raise ValueError('Plan already reserved')
            rows = check(plan, proposal, ledger, now)
            for row in rows:
                row.update(plan_sha256=digest, status='reserved', deleted_utc=None)
            ledger['resources'].extend(rows)
            persist_ledger(ledger)
            print('Reserved before creation; inspect and commit ledger and reviewed plan digest')
        else:
            rows = [r for r in ledger['resources']
                    if r.get('plan_sha256') == digest and r.get('status') == 'reserved']
            if not rows:
                raise ValueError('Reviewed plan has no precreation reservation')
            if [{k: v for k, v in r.items() if k not in ('plan_sha256', 'status', 'deleted_utc')}
                    for r in rows] != proposal['resources']:
                raise ValueError('Proposal differs from recorded reservation')
            remainder = {**ledger, 'resources': [r for r in ledger['resources'] if r not in rows]}
            check(plan, proposal, remainder, now)
            print(f'PASS: exact reviewed plan reserved; cumulative estimate {total_ledger(ledger)} USD <=18')

def main():
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='command',required=True)
    capture=sub.add_parser('capture');capture.add_argument('--filter',required=True);capture.add_argument('--out',required=True)
    for name in ('reserve','check'):
        a=sub.add_parser(name);a.add_argument('--plan',required=True);a.add_argument('--proposal',required=True)
    a=parser.parse_args();now=dt.datetime.now(dt.timezone.utc)
    if a.command=='capture':
        url='https://prices.azure.com/api/retail/prices?'+urllib.parse.urlencode({'$filter':a.filter,'currencyCode':"'USD'"})
        with urllib.request.urlopen(url,timeout=60) as response:data=json.load(response)
        if data.get('NextPageLink'):raise ValueError('Narrow filter; pagination not accepted')
        Path(a.out).write_text(json.dumps({'captured_at':now.strftime('%Y-%m-%dT%H:%M:%SZ'),'url':url,'response':data},indent=2)+'\n');return
    plan_path=Path(a.plan).resolve()
    rendered=subprocess.run(['terraform','show','-json',str(plan_path)],cwd=plan_path.parent,text=True,capture_output=True,check=True)
    plan=json.loads(rendered.stdout);proposal=json.loads(Path(a.proposal).read_text())
    digest=hashlib.sha256(Path(a.plan).read_bytes()).hexdigest()
    admit(a.command, plan, proposal, digest)
if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,TypeError,OSError,json.JSONDecodeError,subprocess.CalledProcessError) as e:print(f'BUDGET REFUSED: {e}',file=sys.stderr);sys.exit(1)
