#!/usr/bin/env python3
"""Compare fresh read-only ARM identity captures with bootstrap state; never alter a ledger.

The controller must separately reconcile security properties, costs and state freshness.
This report is supporting identity evidence, not renewal approval or an admission token.
"""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import azure_budget as budget
import azure_parent_renewal as renewal


def build(state,current,subscription,now):
    if current.get('subscription_id')!=subscription or not dt.timedelta(0)<=now-budget.utc(current.get('captured_at'))<=dt.timedelta(minutes=15):raise ValueError('Fresh exact subscription ARM capture required')
    root=state.get('values',{}).get('root_module',{})
    resources=root.get('resources',[])
    if root.get('child_modules') or len(resources)!=4 or {r.get('address') for r in resources}!=renewal.ADDRESSES or any(r.get('mode')!='managed' for r in resources):raise ValueError('Exact bootstrap state of four managed objects required')
    records=current.get('resources',[])
    if len(records)!=4 or any(not isinstance(r.get('id'),str) for r in records):raise ValueError('Four current ARM identity records required')
    arm={r['id'].lower():r for r in records}
    if len(arm)!=4:raise ValueError('Duplicate current ARM identity')
    prefix=f'/subscriptions/{subscription}/resourceGroups/rg-pathwatch-lab'
    storage=next(r['values']['id'] for r in resources if r['address']=='azurerm_storage_account.state')
    if not re.fullmatch(re.escape(prefix)+r'/providers/Microsoft.Storage/storageAccounts/pw[a-z0-9]{3,22}',storage,re.I):raise ValueError('Exact owned bootstrap storage identity required')
    identities=[]
    for resource in resources:
        address=resource['address'];value=resource['values'];identity=value.get('id','')
        expected={'azurerm_resource_group.project':prefix,'azurerm_storage_account.state':storage,'azurerm_storage_container.state':storage+'/blobServices/default/containers/tfstate'}.get(address)
        if expected is None:
            if not re.fullmatch(re.escape(storage)+r'/providers/Microsoft.Authorization/roleAssignments/[0-9a-fA-F-]{36}',identity,re.I):raise ValueError('Exact state-scoped RBAC identity required')
        elif identity.lower()!=expected.lower():raise ValueError('Foreign bootstrap state identity')
        if identity.lower() not in arm or resource.get('type')!=address.split('.')[0]:raise ValueError('Current ARM identity and state disagree')
        if address in renewal.PARENTS:
            tags=value.get('tags',{})
            if tags!=arm[identity.lower()].get('tags') or set(tags)!=renewal.TAGS or tags.get('project')!='pathwatch' or tags.get('owner')!='codex-pathwatch':raise ValueError('Current parent ownership/TTL tags and state disagree')
            budget.utc(tags['ttl']);budget.money(tags['est_usd_per_hour'])
        identities.append(dict(address=address,type=resource['type'],subscription_id=subscription,resource_id=identity))
    return dict(requires_controller_reconciliation=True,captured_at=current['captured_at'],identities=identities)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('state-json','current-arm-json','subscription-id','out'):parser.add_argument('--'+key,required=True)
    args=parser.parse_args();state=Path(args.state_json).resolve();current=Path(args.current_arm_json).resolve();out=Path(args.out).resolve()
    if not out.is_relative_to(budget.ROOT/'.cache') or out in (state,current):raise ValueError('New identity evidence output must stay in project .cache')
    result=build(json.loads(state.read_text()),json.loads(current.read_text()),args.subscription_id,dt.datetime.now(dt.timezone.utc))
    result['inputs_sha256']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (state,current)}
    out.write_text(json.dumps(result,indent=2)+'\n');print('Wrote identity evidence only; canonical freeze and ledger remain unchanged')
if __name__=='__main__':main()
