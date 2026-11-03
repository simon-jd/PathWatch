"""Per-rule detection times of a drill run: first alert of each (kind, feed) after injection."""
import json
import statistics
import sys

run = sys.argv[1]
s = json.load(open(f"{run}/summary.json"))
per = {}
for r in s["results"]:
    first = {}
    for line in open(f"{run}/detector-{r['repetition']}.jsonl"):
        if '"kind"' not in line:
            continue
        a = json.loads(line)
        if a["at"] >= r["injected_at"]:
            first.setdefault((a["kind"], a["feed"]), round(a["at"] - r["injected_at"], 3))
    for k, v in first.items():
        per.setdefault(k, []).append(v)
    print(r["repetition"], "baseline_alerts", len(r["baseline_alerts"]), "first", r["detection_seconds"], dict(sorted(first.items())),
          "during", r["nearest_during"], "restored", r["restored"], r["restore_seconds_after_withdraw"], "load", r["load"]["loadavg"])
for k, v in sorted(per.items()):
    print(k, "n", len(v), "min", min(v), "median", statistics.median(v), "max", max(v))
