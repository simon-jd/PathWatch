#!/usr/bin/env python3
"""Watch one owned probe's boot-inclusive IO/egress; fail closed through reviewed exact down."""
import argparse
import datetime as dt
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shlex
import subprocess
import time
import azure_budget as budget
import azure_teardown
import hybrid_usage
ROOT=Path(__file__).resolve().parents[1]


def totals(sample):
    if not isinstance(sample,dict) or not isinstance(sample.get('boot_id'),str) or not sample['boot_id'] or not sample.get('disks') or not sample.get('tx_bytes'):raise ValueError('Missing actual boot/disk/network counters')
    ops=[d['bounded_transactions'] for d in sample['disks'].values()];sent=list(sample['tx_bytes'].values())
    if any(not isinstance(v,int) or isinstance(v,bool) or v<0 for v in ops+sent):raise ValueError('Invalid actual counters')
    return sum(ops),sum(sent)


def watch(sample,cleanup,clock,sleep,transactions,egress_gb,end,output):
    last=None;previous=None;reason=None;error=None
    while reason is None:
        try:
            current=sample();counts=totals(current)
            if last is not None and (current['boot_id']!=last['boot_id'] or any(a<b for a,b in zip(counts,previous))):raise ValueError('Boot/counter reset requires reconciliation')
            last=current;previous=counts
            if clock()>=end:reason='ttl'
            else:
                try:hybrid_usage.enforce(current,egress_gb=egress_gb,transactions=transactions)
                except ValueError:reason='usage_limit'
            if reason is None:sleep(5)
        except BaseException as exc:
            reason='sampling_failure';error=type(exc).__name__+': '+str(exc)
    record=dict(reason=reason,error=error,last_sample=last,deleted=False,actual_billed_usd=None)
    log_error=None
    try:output.write_text(json.dumps(record,indent=2)+'\n')
    except OSError as exc:log_error=exc
    try:cleanup()
    except BaseException as exc:
        record['cleanup_error']=type(exc).__name__+': '+str(exc)
        try:output.write_text(json.dumps(record,indent=2)+'\n')
        except OSError:pass
        raise
    record['deleted']=True
    try:output.write_text(json.dumps(record,indent=2)+'\n')
    except OSError as exc:log_error=exc
    if log_error:raise log_error
    return reason


def render(path):return json.loads(subprocess.check_output(['terraform','show','-json',str(path)],cwd=path.parent,text=True))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('current-plan','destroy-plan','reviewed-destroy-sha256','ssh-key','known-hosts','terminal-output'):parser.add_argument('--'+key,required=True)
    args=parser.parse_args();current=Path(args.current_plan).resolve();destroy=Path(args.destroy_plan).resolve()
    key=Path(args.ssh_key).resolve();known=Path(args.known_hosts).resolve();output=Path(args.terminal_output).resolve()
    if len({current,destroy,key,known,output})!=5:raise ValueError('Current/destroy plans, key, known_hosts and terminal output must be distinct')
    if any(not p.is_relative_to(ROOT/'.cache') for p in (key,known,output)) or not key.is_file() or not known.is_file() or key.stat().st_mode&0o077:raise ValueError('Existing private SSH identity/verified known_hosts/output must stay in project .cache')
    if not re.fullmatch('[0-9a-f]{64}',args.reviewed_destroy_sha256):raise ValueError('Exact reviewed destroy plan digest required')
    def verify_destroy():
        if hashlib.sha256(destroy.read_bytes()).hexdigest()!=args.reviewed_destroy_sha256:raise ValueError('Reviewed destroy plan changed')
        value=render(destroy);azure_teardown.validate(value,sub)
        if any(value['variables'].get(k)!=plan['variables'].get(k) for k in ('probe_site','campaign_id','ttl')):raise ValueError('Destroy plan is for another probe campaign')
        changes=[r for r in value['resource_changes'] if r.get('mode')=='managed']
        if {r['address'] for r in changes}!={r['address'] for r in resources} or any(r['change']['actions']!=['delete'] for r in changes):raise ValueError('Reviewed down must delete exactly this complete probe root')
        return value
    plan=render(current);sub=plan['variables']['subscription_id']['value']
    resources=[r for r in plan['resource_changes'] if r.get('mode')=='managed']
    if any(r['change']['actions']!=['no-op'] for r in resources):raise ValueError('Watch start requires a fresh complete no-op current plan')
    with budget.ledger_lock():
        ledger=json.loads(budget.LEDGER.read_text());budget.check(plan,{'resources':[]},ledger,dt.datetime.now(dt.timezone.utc))
        site=plan['variables']['probe_site']['value'];rows=[r for r in ledger['resources'] if r.get('probe_site')==site and r.get('campaign_id')==plan['variables']['campaign_id']['value']]
        vm=next(r for r in rows if r['type']=='azurerm_linux_virtual_machine');disk=next(r for r in rows if r['type']=='azapi_update_resource')
        end=budget.utc(vm['end_utc']);transactions=float(budget.money(disk['bounds']['transactions']));egress=float(budget.money(vm['bounds']['egress_gb']))
    verify_destroy()
    pip=next(r for r in resources if r['type']=='azurerm_public_ip')['change']['after']['ip_address'];ip=ipaddress.ip_address(pip)
    if ip.version!=4 or not ip.is_global:raise ValueError('Actual owned public IPv4 required')
    source=(ROOT/'scripts/hybrid_usage.py').read_text()
    remote="ns={'__name__':'usage_library'}\nexec("+repr(source)+",ns)\ns=ns['snapshot']()\ns['boot_id']=ns['Path']('/proc/sys/kernel/random/boot_id').read_text().strip()\nprint(ns['json'].dumps(s))\n"
    def sample():
        cmd=['ssh','-F','/dev/null','-o','IdentitiesOnly=yes','-o','GlobalKnownHostsFile=/dev/null','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(known),'-i',str(key),'pathwatch@'+str(ip),shlex.join(['python3','-'])]
        return json.loads(subprocess.run(cmd,input=remote,text=True,capture_output=True,check=True,timeout=15).stdout)
    def cleanup():
        verify_destroy()
        subprocess.run(['terraform','-chdir='+str(destroy.parent),'apply','-input=false',str(destroy)],check=True,timeout=600)
    output.parent.mkdir(parents=True,exist_ok=True)
    reason=watch(sample,cleanup,lambda:dt.datetime.now(dt.timezone.utc),time.sleep,transactions,egress,end,output)
    print('Probe cleanup completed: '+reason+'; terminal sample retained, billing remains unreconciled')
    if reason=='sampling_failure':raise SystemExit(1)
if __name__=='__main__':main()
