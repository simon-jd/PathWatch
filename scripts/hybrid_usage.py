#!/usr/bin/env python3
"""Guest read-only accounting upper bound, including boot and package installation.

Standard HDD transactions of up to16KiB (S4/S6): completed ops PLUS rounded transferred
bytes/16KiB is a deliberate conservative upper bound for split large IO. Samples
are absolute since boot, never reset at drill start. Monitor at5s and stop at80%.
"""
import json
from pathlib import Path

def snapshot(sys=Path('/sys')):
    disks={}
    for device in (sys/'class/block').iterdir():
        if device.name.startswith(('loop','ram','sr','dm-')) or (device/'partition').exists():continue
        fields=[int(x) for x in (device/'stat').read_text().split()]
        # Include all physical guest disks conservatively; no data disk is allowed.
        ops=fields[0]+fields[4]+(fields[11] if len(fields)>11 else 0)+(fields[15] if len(fields)>15 else 0);sectors=fields[2]+fields[6]+(fields[13] if len(fields)>13 else 0)
        disks[device.name]={'completed_ops':ops,'sectors':sectors,'bounded_transactions':ops+(sectors+31)//32}
    tx={}
    for nic in (sys/'class/net').iterdir():
        if nic.name=='lo' or nic.name.startswith(('pw-wg','wg','docker','veth')):continue
        tx[nic.name]=int((nic/'statistics/tx_bytes').read_text())
    if not disks or not tx:raise ValueError('Missing real guest disk/network counters')
    return {'disks':disks,'tx_bytes':tx}

def enforce(sample,egress_gb=2,transactions=1000000):
    ops=sum(d['bounded_transactions'] for d in sample['disks'].values())
    tx=sum(sample['tx_bytes'].values())
    if ops>=transactions*0.8 or tx>=egress_gb*1000000000*0.8:raise ValueError('80% usage stop: reserve20% for restore/teardown, do not continue')
    return {'disk_transactions_upper_bound':ops,'outbound_bytes_upper_bound':tx}
if __name__=='__main__':print(json.dumps(snapshot()))
