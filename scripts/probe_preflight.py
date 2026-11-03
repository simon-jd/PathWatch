#!/usr/bin/env python3
"""Capture current read-only quota, SKU restrictions and exact Ubuntu image evidence."""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import azure_probe_scope as scope


def capture(subscription,site,sku,version):
    if not re.fullmatch(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}',subscription) or site not in scope.REGIONS or sku not in scope.SKUS or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',version):raise ValueError('Explicit subscription/site/approved CPU SKU/pinned Ubuntu version required')
    commands=[]
    def read(args):
        command=['az',*args,'--subscription',subscription,'--output','json'];commands.append(command)
        return json.loads(subprocess.check_output(command,text=True,timeout=60))
    account=read(['account','show'])
    if account.get('id')!=subscription or account.get('state')!='Enabled':raise ValueError('Active exact subscription required')
    region=scope.REGIONS[site];urn='Canonical:ubuntu-24_04-lts:'+scope.SKUS[sku][0]+':'+version
    usage=read(['vm','list-usage','--location',region])
    skus=read(['vm','list-skus','--location',region,'--resource-type','virtualMachines','--size',sku,'--all'])
    image=read(['vm','image','show','--location',region,'--urn',urn])
    return dict(subscription_id=subscription,location=region,captured_at=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),image_urn=urn,usage=usage,skus=skus,image=image,commands=commands)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('subscription-id','site','sku','image-version','out'):parser.add_argument('--'+key,required=True)
    args=parser.parse_args();value=capture(args.subscription_id,args.site,args.sku,args.image_version)
    Path(args.out).write_text(json.dumps(value,indent=2)+'\n');print('Saved read-only evidence; no allocation guarantee or admission performed')
if __name__=='__main__':main()
