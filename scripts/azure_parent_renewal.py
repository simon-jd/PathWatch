"""Exact retained-bootstrap TTL renewal; no arbitrary update or pricing bypass."""
import copy
import datetime as dt
import ipaddress
import re

PARENTS={'azurerm_resource_group.project','azurerm_storage_account.state'}
CHILDREN={'azurerm_storage_container.state','azurerm_role_assignment.operator_state'}
ADDRESSES=PARENTS|CHILDREN
TAGS={'project','owner','ttl','est_usd_per_hour'}


def matches(plan,proposal):
    # Explicit purpose retains renewal validation on its no-op retry. Ordinary
    # bootstrap no-op and initial creation retain their existing semantics.
    return proposal.get('purpose')=='bootstrap-renewal' or any(r.get('address') in ADDRESSES and r.get('change',{}).get('actions') not in (['create'],['no-op'])
               for r in plan.get('resource_changes',[]) if r.get('mode')=='managed')


def unknown(value):
    if isinstance(value,dict):return any(unknown(v) for v in value.values())
    if isinstance(value,list):return any(unknown(v) for v in value)
    return value is True


def check(plan,proposal,ledger,now,budget):
    variables={k:v.get('value') for k,v in plan.get('variables',{}).items()}
    sub=variables['subscription_id'];location=variables.get('location');end=variables.get('ttl')
    name=variables.get('storage_account_name','')
    if not re.fullmatch(r'pw[a-z0-9]{3,22}',name):raise ValueError('Exact bootstrap storage name required')
    prefix=f'/subscriptions/{sub}/resourceGroups/rg-pathwatch-lab'
    storage=prefix+'/providers/Microsoft.Storage/storageAccounts/'+name
    ids={'azurerm_resource_group.project':prefix,'azurerm_storage_account.state':storage,
         'azurerm_storage_container.state':storage+'/blobServices/default/containers/tfstate'}
    resources=plan.get('resource_changes',[])
    if plan.get('deferred_changes') or plan.get('resource_drift') or any(r.get('mode')!='managed' for r in resources):raise ValueError('Renewal requires current managed state without deferred reads/drift')
    if len(resources)!=4 or {r.get('address') for r in resources}!=ADDRESSES:raise ValueError('Renewal requires exact four retained bootstrap resources')
    rows=proposal['resources'];by={r.get('address'):r for r in rows}
    changed={r['address'] for r in resources if r['change']['actions']!=['no-op']}
    if len(by)!=len(rows) or set(by)!=changed or changed not in (set(),PARENTS):raise ValueError('Renew both parents together with exact rows; children must remain no-op')
    previous=set();current=set()
    for resource in resources:
        address=resource['address'];typ=address.split('.')[0];change=resource['change']
        before=change.get('before');after=change.get('after')
        if resource.get('type')!=typ or not isinstance(before,dict) or not isinstance(after,dict) or unknown(change.get('after_unknown',{})):raise ValueError('Current known before/after bootstrap values required')
        if change['actions'] not in ([['update'],['no-op']] if address in PARENTS else [['no-op']]):raise ValueError('Bootstrap only supports parent tags update and child no-op')
        if change['actions']==['no-op'] and before!=after:raise ValueError('No-op requires identical current before and after values')
        if address in PARENTS:
            if {k:v for k,v in before.items() if k!='tags'}!={k:v for k,v in after.items() if k!='tags'}:raise ValueError('Renewal cannot modify parent configuration')
            for value in (before,after):
                tags=value.get('tags',{})
                if set(tags)!=TAGS or tags['project']!='pathwatch' or tags['owner']!='codex-pathwatch' or value.get('location')!=location:raise ValueError('Exact parent ownership, four tags and location required')
                budget.money(tags['est_usd_per_hour']);budget.utc(tags['ttl'])
            previous.add(before['tags']['ttl']);current.add(after['tags']['ttl'])
        elif before!=after:raise ValueError('Bootstrap child state is immutable')
        actual=after.get('id','')
        if address in ids:
            if actual.lower()!=ids[address].lower() or before.get('id','').lower()!=ids[address].lower():raise ValueError('Bootstrap exact current ARM identity mismatch')
        else:
            if not re.fullmatch(re.escape(storage)+r'/providers/Microsoft.Authorization/roleAssignments/[0-9a-fA-F-]{36}',actual,re.I):raise ValueError('Role assignment outside exact state account')
            if after.get('scope','').lower()!=storage.lower() or after.get('role_definition_name')!='Storage Blob Data Contributor' or after.get('principal_id')!=variables.get('operator_principal_id'):raise ValueError('Bootstrap RBAC identity mismatch')
        if typ=='azurerm_resource_group' and after.get('name')!='rg-pathwatch-lab':raise ValueError('Exact project group required')
        if typ=='azurerm_storage_container' and (after.get('name')!='tfstate' or after.get('storage_account_id','').lower()!=storage.lower() or after.get('container_access_type')!='private'):raise ValueError('Private exact state container required')
        if typ=='azurerm_storage_account':
            expected=dict(name=name,resource_group_name='rg-pathwatch-lab',account_tier='Standard',account_replication_type='LRS',account_kind='StorageV2',min_tls_version='TLS1_2',https_traffic_only_enabled=True,shared_access_key_enabled=False,default_to_oauth_authentication=True,allow_nested_items_to_be_public=False,public_network_access_enabled=True,cross_tenant_replication_enabled=False)
            if any(after.get(k)!=v for k,v in expected.items()):raise ValueError('Retained state account security/budget policy mismatch')
            rules=after.get('network_rules',[])
            if len(rules)!=1 or rules[0].get('default_action')!='Deny' or rules[0].get('bypass')!=['None'] or not rules[0].get('ip_rules'):raise ValueError('Retained state default-deny ACL required')
            for ip in rules[0]['ip_rules']:
                net=ipaddress.ip_network(ip,strict=True)
                if net.version!=4 or net.prefixlen!=32 or not net.network_address.is_global:raise ValueError('State ACL requires owned public IPv4 /32')
        history=[r for r in ledger['resources'] if r.get('address')==address]
        if not history:raise ValueError('Missing canonical bootstrap identity history')
        for row in history:
            if row.get('subscription_id')!=sub or row.get('resource_id','').lower()!=actual.lower() or row.get('type')!=typ or row.get('region')!=location or row.get('status') not in ('applied','reserved') or row.get('deleted_utc') is not None or not re.fullmatch('[0-9a-f]{64}',row.get('plan_sha256','')):raise ValueError('Unverified/deleted bootstrap canonical identity history')
        latest=max(history,key=lambda r:budget.utc(r['end_utc']))
        if address in PARENTS and latest['end_utc']!=before['tags']['ttl']:raise ValueError('Current parent TTL does not match latest canonical reservation')
        if address in PARENTS and budget.utc(latest['end_utc'])<=now:raise ValueError('Expired parent requires reconciliation, not TTL reset')
    if len(previous)!=1 or current!={end}:raise ValueError('Parents must share exact current and proposed TTL')
    old=next(iter(previous))
    if not changed:
        budget.enforce_cap(budget.total_ledger(ledger),0)
        return []
    if not budget.utc(old)<budget.utc(end) or budget.utc(end)-now>dt.timedelta(hours=840):raise ValueError('Parent TTL must extend monotonically within 840 hours')
    if len({r.get('start_utc') for r in rows})!=1 or len({r.get('renewal_id') for r in rows})!=1:raise ValueError('One bounded renewal identity and start required')
    renewal=rows[0].get('renewal_id','')
    if not re.fullmatch('[a-zA-Z0-9-]{8,80}',renewal) or any(r.get('renewal_id')==renewal for r in ledger['resources']):raise ValueError('Renewal identity already used or invalid')
    for row in rows:
        if row.get('subscription_id')!=sub or row.get('resource_id','').lower()!=ids[row['address']].lower() or row.get('previous_end_utc')!=old or row.get('end_utc')!=end:raise ValueError('Renewal proposal identity/window mismatch')
        if not dt.timedelta(0)<=now-budget.utc(row['start_utc'])<=dt.timedelta(minutes=15):raise ValueError('Renewal requires current actual start; conservatively recharge overlap')
        if row['address']=='azurerm_storage_account.state':
            meters=row.get('meters',[])
            if len(meters)!=5 or {m.get('category') for m in meters}!={'storage_gb','write_requests','read_requests','other_requests','egress_gb'}:raise ValueError('Price each storage renewal category exactly once')
    # Reuse original storage billing and total-ledger cap. Only the two validated
    # tags-only updates become synthetic creates; no public bypass flag exists.
    priced=copy.deepcopy(plan)
    priced['resource_changes']=[r for r in priced['resource_changes'] if r['address'] in PARENTS]
    for resource in priced['resource_changes']:resource['change']['actions']=['create']
    return budget.check_creation(priced,proposal,ledger,now)
