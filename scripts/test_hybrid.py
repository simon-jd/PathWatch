#!/usr/bin/env python3
"""Opt-in real acceptance; rejects NVA-only ping as workload data-plane proof."""
import argparse
import json
import signal
from pathlib import Path
import time
from hybrid_runtime import Runtime,established
from vpn_failover import Drill,stamp
from hybrid_monitor import Monitor
from hybrid_evidence import evidence_run

def main():
    p=argparse.ArgumentParser();p.add_argument('--inventory',required=True);p.add_argument('--out',required=True);p.add_argument('--confirm-owned-private-endpoints',action='store_true',required=True);a=p.parse_args()
    def cancel(signum,frame):raise KeyboardInterrupt('Cancelled baseline; preserving failed run evidence')
    signal.signal(signal.SIGTERM,cancel)
    run_baseline(Runtime(a.inventory),a.out)

def run_baseline(runtime,out):
    with evidence_run(out) as run:
        drill=Drill(runtime,run)
        with Monitor(runtime,run):baseline(runtime,drill,run)
    return run

def baseline(runtime,drill,out):
    ready=drill.wait_ready('baseline');(out/'routing.json').write_text(json.dumps(stamp()|ready,indent=2))
    # Real bidirectional workload traffic; Azure workload path depends on owned UDR.
    for source,target in [('home-client','10.10.0.4'),('azure-client','10.30.0.10')]:
        drill.usage();raw=runtime.run(source,['ping','-n','-c','100','-i','0.2','-W','2',target],timeout=230)
        (out/(source+'-ping100.txt')).write_text(raw)
    # Rate-limited baseline:5Mbps per stream/P4 (~20Mbps aggregate), not peak capacity.
    for source,target in [('home-client','10.10.0.4'),('azure-client','10.30.0.10')]:
        drill.usage();raw=runtime.run(source,['iperf3','-c',target,'-t','30','-P','4','-b','5M','-J'],timeout=45)
        result=json.loads(raw)
        if result.get('error'):raise RuntimeError(result['error'])
        (out/(source+'-iperf30s-p4.json')).write_text(raw);drill.usage()
    (out/'acceptance.json').write_text(json.dumps({'status':'PASS','scope':'actual Azure A/home private workload endpoint traffic','timestamp':stamp(),'public_probe_min_interval_s':60,'iperf_per_stream_bps':5000000,'iperf_parallel_streams':4,'not_peak_capacity':True},indent=2)+'\n')
if __name__=='__main__':main()
