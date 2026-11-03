"""Summarize a canary drill directory (docs/evidence/task-14-canary/<run>): analysis
measurements, client-visible status counts per second, and recovery."""
import datetime
import json
import sys

run = sys.argv[1]
s = json.load(open(f"{run}/summary.json"))
print("run", s["run"], "completed", s["completed"], "events", s["events_seconds_after_commit"])
print("load start", s["machine_load_start"], "end", s["machine_load_end"])
for r in json.load(open(f"{run}/analysisruns.json"))["items"]:
    for m in r["status"].get("metricResults", []):
        print("analysisrun", r["metadata"]["name"], r["status"]["phase"], [(x["phase"], x.get("value"), x.get("finishedAt")) for x in m.get("measurements", [])])
start = datetime.datetime.fromisoformat(s["started"])
first_bad = last_bad = None
total = {}
for line in open(f"{run}/querygen.jsonl"):
    try:
        rec = json.loads(line)
    except ValueError:
        continue
    t = (datetime.datetime.fromisoformat(rec["t"].replace("Z", "+00:00")[:26] + "+00:00") - start).total_seconds()
    if t < 0:
        continue
    for code, n in rec["status"].items():
        total[code] = total.get(code, 0) + n
        if code != "200":
            first_bad = t if first_bad is None else first_bad
            last_bad = t
print("client statuses after commit", total, "first non-200 at", first_bad, "last non-200 at", last_bad)
if s.get("recovery"):
    print("recovery", s["recovery"]["seconds_to_synced_healthy"], "s; apps", s["recovery"]["apps"])
