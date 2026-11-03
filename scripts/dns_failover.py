#!/usr/bin/env python3
"""Three physical observers, >=60s custom DNS rate, finite campaign and safe restore."""
import argparse
import json
import os
from pathlib import Path
import signal
import tempfile
import time
from dns_runtime import Runtime
from dns_monitor import Monitor

def atomic(path,value):
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,indent=2)+'\n');os.replace(temporary,path)
def analyze(rows,primary,backup):
    result={}
    for site in ('home','a','b'):
        samples=[r for r in rows if r['site']==site]
        if len(samples)<2:raise ValueError('Three physical observers must each observe transition')
        if any(b['monotonic_ns']-a['monotonic_ns']<60e9 for a,b in zip(samples,samples[1:])):raise ValueError('Observer cadence below60s')
        if samples[0]['answers']!=[primary]:raise ValueError('Each observer must initially resolve only primary')
        first=next((i for i,r in enumerate(samples) if r['answers']==[backup]),None)
        if first is None or any(r['answers']!=[backup] for r in samples[first:]):raise ValueError('No stable backup-only answer')
        previous=max((r for r in samples[:first] if r['answers']==[primary]),key=lambda r:r['monotonic_ns'])
        result[site]={'last_primary_monotonic_ns':previous['monotonic_ns'],'first_backup_monotonic_ns':samples[first]['monotonic_ns'],'window_seconds':(samples[first]['monotonic_ns']-previous['monotonic_ns'])/1e9}
    return result

def campaign(runtime,out,rounds=8):
    if not 1<=rounds<=8:raise ValueError('Bound 1..8 rounds at >=60s')
    parent=Path(out);parent.mkdir(parents=True,exist_ok=True);run=Path(tempfile.mkdtemp(prefix='run-',dir=parent));rows=[]
    atomic(run/'run.json',{'status':'running','started_realtime_ns':time.time_ns()})
    try:
        with Monitor(runtime,run):
            identities=runtime.attest();runtime.online('a');runtime.online('b');runtime.http_backup()
            for site in ('home','a','b'):rows.append(runtime.observe(site))
            primary=runtime.config['sites']['a']['public_ip'];backup=runtime.config['sites']['b']['public_ip']
            if any(r['answers']!=[primary] for r in rows):raise ValueError('Initial primary-only DNS prerequisite failed')
            atomic(run/'observations.json',rows)
            # Mark restoration required before stop: a timeout may lose a successful reply.
            try:
                runtime.service('stop');atomic(run/'fault.json',{'controller_monotonic_ns':time.monotonic_ns(),'realtime_ns':time.time_ns()})
                for _ in range(rounds):
                    time.sleep(60)
                    for site in ('home','a','b'):rows.append(runtime.observe(site))
                    atomic(run/'observations.json',rows)
                    if all(r['answers']==[backup] for r in rows[-3:]):break
                windows=analyze(rows,primary,backup)
                runtime.online('b');runtime.http_backup()
            finally:runtime.service('start')
        # Publish success only after service restoration AND monitor exit succeed.
        atomic(run/'summary.json',{'physical_vm_ids':identities,'windows':windows,'sampling_seconds':60,'theoretical_context_seconds':120,'uncertainty':'Per-observer monotonic windows, >=60s resolution; resolver caches and managed monitor aggregation apply. No clock subtraction across physical hosts.'})
        atomic(run/'run.json',{'status':'passed','finished_realtime_ns':time.time_ns()});return run
    except BaseException as error:
        (run/'summary.json').unlink(missing_ok=True);atomic(run/'run.json',{'status':'failed','error_type':type(error).__name__,'finished_realtime_ns':time.time_ns()});raise

def main():
    p=argparse.ArgumentParser();p.add_argument('--inventory',required=True);p.add_argument('--out',required=True);p.add_argument('--confirm-owned-endpoints',action='store_true',required=True);a=p.parse_args()
    def cancel(signum,frame):raise KeyboardInterrupt('DNS campaign cancelled')
    signal.signal(signal.SIGTERM,cancel)
    campaign(Runtime(a.inventory),a.out)
if __name__=='__main__':main()
