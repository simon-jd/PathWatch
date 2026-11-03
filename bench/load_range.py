"""Host 1-minute loadavg range sampled during benchmark cells (shared machine context).

Usage: python -I load_range.py RUN_DIR...
"""
import json
import os
import statistics
import sys
from pathlib import Path

values = []
for run in sys.argv[1:]:
    for f in Path(run).glob("*/*.json"):
        try:
            data = json.loads(f.read_text())
        except ValueError:
            continue
        if isinstance(data, list):
            values += [float(s["loadavg"].split()[0]) for s in data if isinstance(s, dict) and "loadavg" in s]
if values:
    print(json.dumps({"samples": len(values), "min": min(values), "median": statistics.median(values), "max": max(values), "nproc": os.cpu_count()}))
