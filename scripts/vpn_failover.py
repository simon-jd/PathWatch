#!/usr/bin/env python3
"""Opt-in15 real underlay-loss trials. A failed/skipped run never passes cloud acceptance."""
import argparse
import json
from pathlib import Path
import re
import signal
import statistics
import subprocess
import threading
import time
from hybrid_runtime import Runtime,established
from hybrid_usage import enforce
from hybrid_monitor import Monitor
from hybrid_evidence import evidence_run

PROFILES=('baseline','tuned','bfd')
def stamp():return {'monotonic_ns':time.monotonic_ns(),'realtime_ns':time.time_ns()}
def fping_samples(rows):
    samples=[]
    for row in rows:
        line=row['line'];match=re.search(r':\s*\[(\d+)\],',line)
        if not match:continue
        if 'timed out' in line:ok=False
        elif re.search(r'\b\d+(?:\.\d+)? ms\b',line):ok=True
        else:continue
        samples.append({'seq':int(match[1]),'ok':ok,'monotonic_ns':row['monotonic_ns'],'realtime_ns':row['realtime_ns']})
    if not samples:raise ValueError('No actual fping samples')
    samples.sort(key=lambda s:s['seq'])
    if len({s['seq'] for s in samples})!=len(samples):raise ValueError('Duplicate fping sample')
    if any(b['seq']!=a['seq']+1 for a,b in zip(samples,samples[1:])):raise ValueError('Missing fping packet sequence; incomplete raw data')
    if any(b['monotonic_ns']<a['monotonic_ns'] for a,b in zip(samples,samples[1:])):raise ValueError('Nonmonotonic receive timestamps')
    return samples

def parse_fping(rows,impair_ns):
    samples=fping_samples(rows)
    intervals=[];first=None
    for sample in samples:
        if not sample['ok'] and first is None:first=sample
        if sample['ok'] and first is not None:
            intervals.append({'first_lost_seq':first['seq'],'recovered_seq':sample['seq'],'loss_interval_ms':(sample['monotonic_ns']-first['monotonic_ns'])/1e6,'recovery_from_impair_ms':(sample['monotonic_ns']-impair_ns)/1e6});first=None
    if first:intervals.append({'first_lost_seq':first['seq'],'recovered_seq':None,'loss_interval_ms':None,'recovery_from_impair_ms':None})
    return {'samples':len(samples),'loss_count':sum(not s['ok'] for s in samples),'loss_intervals':intervals,'sample_resolution_ms':100,'timestamp_semantics':'monotonic/realtime at fping line receipt; raw fping unix timestamp preserved'}


def recovery_before_restore(rows,impair_ns,switch_ns,cutoff_ns):
    # Freeze the observation window before restoring UDP. A later response must
    # never turn a broken alternate path into a successful trial.
    window=[row for row in rows if row['monotonic_ns']<=cutoff_ns]
    traffic=parse_fping(window,impair_ns)
    if any(interval['recovered_seq'] is None for interval in traffic['loss_intervals']):
        raise RuntimeError('Traffic has an unrecovered loss tail while impairment remains active')
    samples=fping_samples(window)
    terminal=[]
    for sample in reversed(samples):
        if not sample['ok'] or sample['monotonic_ns']<switch_ns:break
        terminal.append(sample)
    if len(terminal)<3:
        raise RuntimeError('Need three terminal successful packets after route switch and before restoring impairment')
    if cutoff_ns-terminal[0]['monotonic_ns']>500000000:
        raise RuntimeError('Post-switch traffic evidence is stale before restoring impairment')
    traffic['recovery_proof']={'cutoff_monotonic_ns':cutoff_ns,'route_switch_monotonic_ns':switch_ns,
                               'terminal_success_samples':len(terminal),'last_success_monotonic_ns':terminal[0]['monotonic_ns'],
                               'minimum_terminal_success_samples':3,'maximum_sample_age_ms':500}
    return traffic

class Drill:
    def __init__(self,runtime,out,trial_timeout=230,poll_timeout=240):
        if not 1<=trial_timeout<=300 or not 1<=poll_timeout<=900:raise ValueError('Bound trial<=300s readiness polling<=15min')
        self.runtime=runtime;self.out=Path(out);self.out.mkdir(parents=True,exist_ok=True);self.trial_timeout=trial_timeout;self.poll_timeout=poll_timeout;self.last_usage=0
    def usage(self):
        if time.monotonic()-self.last_usage<5:return
        sample=self.runtime.usage()
        with (self.out/'usage.jsonl').open('a') as log:log.write(json.dumps(stamp()|{'guests':sample})+'\n')
        for value in sample.values():enforce(value)
        self.last_usage=time.monotonic()
    def wait_ready(self,profile):
        end=time.monotonic()+self.poll_timeout
        while time.monotonic()<end:
            self.usage();s=self.runtime.snapshot()
            if established(s,profile=='bfd') and s['home']['kernel'][0].get('dev')=='pw-wg0':return s
            time.sleep(1)
        raise TimeoutError('Actual two peers/handshakes/routes/primary did not become ready')
    def trial(self,profile,index):
        trial=self.out/f'{profile}-{index}';trial.mkdir(exist_ok=False);rows=[];process=None;reader=None;impair=None;result={}
        def capture():
            for line in process.stdout:
                row=stamp()|{'line':line.rstrip('\n')};rows.append(row)
                with (trial/'fping.jsonl').open('a') as log:log.write(json.dumps(row)+'\n')
        def stop_capture():
            if process is None:return
            try:self.runtime.stop_probe()
            finally:process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            if reader:
                reader.join(timeout=5)
                if reader.is_alive():raise RuntimeError('fping capture did not stop before restoring impairment')
        stopped=False
        try:
            self.runtime.impair(False)
            before=self.wait_ready(profile);(trial/'before.json').write_text(json.dumps(stamp()|before,indent=2))
            args=self.runtime.command('home-client',['timeout','--signal=TERM','--kill-after=3s','300s','fping','-D','-l','-p','100','-t','90','10.10.0.4'])
            process=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
            reader=threading.Thread(target=capture,daemon=True);reader.start();time.sleep(2)
            if process.poll() is not None:raise RuntimeError('fping exited before impairment')
            impair=stamp();(trial/'impair.json').write_text(json.dumps(impair));self.runtime.impair(True)
            deadline=time.monotonic()+self.trial_timeout
            while time.monotonic()<deadline:
                route=self.runtime.home_route();observed=stamp()
                with (trial/'routing.jsonl').open('a') as log:log.write(json.dumps(observed|{'kernel':route})+'\n')
                if route[0].get('dev')=='pw-wg1':
                    result={'route_switch':observed,'impair':impair,'route_convergence_ms':(observed['monotonic_ns']-impair['monotonic_ns'])/1e6};time.sleep(2)
                    (trial/'after.json').write_text(json.dumps(stamp()|self.runtime.snapshot(),indent=2))
                    if process.poll() is not None:raise RuntimeError('fping exited before recovery observation ended')
                    stop_capture();stopped=True;cutoff=stamp()
                    result['traffic']=recovery_before_restore(rows,impair['monotonic_ns'],observed['monotonic_ns'],cutoff['monotonic_ns'])
                    result['capture_cutoff']=cutoff
                    break
                time.sleep(.25)
            else:raise TimeoutError('No observed alternate route within trial deadline')
        finally:
            # Cleanup runs on command failure, timeout, KeyboardInterrupt and SIGTERM.
            restore_started=stamp()
            try:self.runtime.impair(False)
            finally:
                if not stopped:stop_capture()
            (trial/'restore.json').write_text(json.dumps({'started':restore_started,'finished':stamp()},indent=2)+'\n')
        if impair is None:raise RuntimeError('No impairment was executed')
        result['profile']=profile;result['trial']=index
        (trial/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        return result
    def run(self):
        results=[]
        try:
            for profile in PROFILES:
                self.runtime.profile(profile)
                for trial in range(1,6):results.append(self.trial(profile,trial))
        finally:
            try:self.runtime.impair(False)
            finally:self.runtime.profile('baseline')
        summary={'scope':'Azure A ↔ home; logical tunnels share NVA and physical path','trials':results,'medians_route_convergence_ms':{p:statistics.median(r['route_convergence_ms'] for r in results if r['profile']==p) for p in PROFILES},'profiles':{'baseline':{'keepalive_s':60,'hold_s':180},'tuned':{'keepalive_s':3,'hold_s':9},'bfd':{'keepalive_s':60,'hold_s':180,'min_tx_ms':300,'min_rx_ms':300,'detect_multiplier':3}},'acceptance':'actual endpoint drill only; controller must separately verify Azure ARM scope, UDR and teardown'}
        (self.out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n');return summary

def run_campaign(runtime,out):
    with evidence_run(out) as run:
        with Monitor(runtime,run):Drill(runtime,run).run()
    return run

def main():
    p=argparse.ArgumentParser();p.add_argument('--inventory',required=True);p.add_argument('--out',required=True);p.add_argument('--confirm-owned-private-endpoints',action='store_true',required=True);a=p.parse_args()
    def cancel(signum,frame):raise KeyboardInterrupt('Cancelled; restoring own impairment')
    signal.signal(signal.SIGTERM,cancel)
    # Total hard limit; 5default trials alone take~15min, per readiness<=15min.
    signal.signal(signal.SIGALRM,cancel);signal.alarm(3600)
    run_campaign(Runtime(a.inventory),a.out)
if __name__=='__main__':main()
