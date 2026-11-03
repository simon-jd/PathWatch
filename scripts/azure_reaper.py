"""Explicit-subscription, exact-project-only Azure cleanup. No global enumeration."""
import datetime as dt
import json
import os
import re
import subprocess
import sys
GROUP='rg-pathwatch-lab'
def az(*args):
    r=subprocess.run(['az',*args,'--subscription',SUB,'--only-show-errors','-o','json'],text=True,capture_output=True)
    if r.returncode: raise RuntimeError(f'az {args[0]} {args[1]} failed ({r.returncode}): {r.stderr.strip()}')
    return json.loads(r.stdout) if r.stdout.strip() else None
def owned(tags):return isinstance(tags,dict) and tags.get('project')=='pathwatch' and tags.get('owner')=='codex-pathwatch'
def main(mode):
    if mode not in ('reap','down'):raise ValueError('Only reap or down supported; resource group cannot be overridden')
    account=az('account','show')
    if account.get('id','').lower()!=SUB.lower() or account.get('state')!='Enabled' or account.get('name')!='Azure for Students':raise ValueError('Expected explicit enabled Azure for Students subscription')
    exists=az('group','exists','--name',GROUP)
    if exists is False:print('Project group absent; nothing to delete');return
    if exists is not True:raise ValueError('Invalid group existence response')
    if mode=='down':
        if not owned(az('group','show','--name',GROUP).get('tags')):raise ValueError('Group ownership mismatch; refusing deletion')
        az('group','delete','--name',GROUP,'--yes')
        print('Deleted exact project group');return
    resources=az('resource','list','--resource-group',GROUP)
    if not isinstance(resources,list):raise ValueError('Invalid resource list')
    now=dt.datetime.now(dt.timezone.utc);failed=False
    for resource in resources:
        rid=resource.get('id','');tags=resource.get('tags',{})
        prefix=f'/subscriptions/{SUB}/resourceGroups/{GROUP}/providers/'
        # Full canonical ARM identifier; reject scope escapes, URLs, query strings and malformed nested IDs.
        pattern=re.escape(prefix)+r'[A-Za-z0-9.]+(?:/[A-Za-z0-9_.()-]+){2}(?:/[A-Za-z0-9_.()-]+/[A-Za-z0-9_.()-]+)*'
        ttl=tags.get('ttl','') if isinstance(tags,dict) else ''
        if not re.fullmatch(pattern,rid,re.IGNORECASE) or not owned(tags):
            print('SKIP unsafe scope or ownership',file=sys.stderr);continue
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z',ttl):print('SKIP missing/invalid UTC ttl',file=sys.stderr);continue
        try:expiry=dt.datetime.strptime(ttl,'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
        except ValueError:print('SKIP invalid calendar ttl',file=sys.stderr);continue
        if expiry>now:continue
        try:az('resource','delete','--ids',rid);print(f'Deleted expired {rid}')
        except RuntimeError as e:print(str(e),file=sys.stderr);failed=True
    if failed:raise RuntimeError('One or more deletions failed; inspect remaining resources and reconcile ledger')
if __name__=='__main__':
    try:
        if len(sys.argv) > 1 and sys.argv[1] == 'xdp-check':
            from azure_xdp_lifecycle import main as xdp_main
            sys.exit(xdp_main(sys.argv[1:]))
        SUB=os.environ.get('AZURE_SUBSCRIPTION_ID','')
        if not re.fullmatch(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}',SUB):raise ValueError('AZURE_SUBSCRIPTION_ID UUID required')
        main(sys.argv[1] if len(sys.argv)==2 else '')
    except (ValueError,RuntimeError,json.JSONDecodeError) as e:print(f'REFUSED: {e}',file=sys.stderr);sys.exit(1)
