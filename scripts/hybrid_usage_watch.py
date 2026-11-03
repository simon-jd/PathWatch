#!/usr/bin/env python3
"""Protect controller configuration commands with the same live usage stop threshold."""
import argparse
from pathlib import Path
import signal
import subprocess
from hybrid_monitor import Monitor
from hybrid_runtime import Runtime

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--inventory',required=True);parser.add_argument('--out',required=True);parser.add_argument('--confirm-owned-private-endpoints',action='store_true',required=True);parser.add_argument('command',nargs=argparse.REMAINDER);args=parser.parse_args()
    command=args.command[1:] if args.command[:1]==['--'] else args.command
    if not command:raise ValueError('Explicit bounded controller command required')
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    def cancel(signum,frame):raise KeyboardInterrupt('Cancelled configuration monitor')
    signal.signal(signal.SIGTERM,cancel)
    # Monitored subprocess.run kills its child on failure/signal. Controller must
    # still run reviewed down on limit/failure; wrapper never bypasses cloud guards.
    with Monitor(Runtime(args.inventory),out):subprocess.run(command,check=True,timeout=900)
if __name__=='__main__':main()
