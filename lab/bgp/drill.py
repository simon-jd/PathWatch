"""Time BGP incident detection in the lab (docs/evidence/bgp-lab.md).

  python drill.py leak|hijack [--repeat 5]

Per repetition: detector running with a clean 5 s baseline (no alerts allowed),
inject the incident, wait for the expected alert kind, record the data-plane
effect, withdraw, and wait until nearest-PoP answers are back to normal.
Times are host wall clock; the detector polls every 0.25 s.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import lab  # noqa: E402

EXPECTED = {"leak": "route-leak", "hijack": "rpki-invalid"}


def load():
    with open("/proc/loadavg") as f:
        return {"nproc": os.cpu_count(), "loadavg": f.read().split()[:3]}


def answers():
    return {c: v["answered_by"] for c, v in lab.nearest(None).items()}


def one(incident, out, n):
    t = lab.topo()
    normal = t["expected_nearest"]
    before = answers()
    if before != normal:
        raise SystemExit(f"lab not in normal state before repetition {n}: {before}")
    log_path = os.path.join(out, f"detector-{n}.jsonl")
    log = open(log_path, "w")
    det = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "detect.py"), "--interval", "0.25"], stdout=log, stderr=subprocess.STDOUT)
    try:
        time.sleep(5)
        baseline = [json.loads(line) for line in open(log_path) if '"kind"' in line]
        node, on, off = lab.INCIDENTS[incident]
        t_inject = time.time()
        lab.vtysh(node, *on, quiet=True)
        t_applied = time.time()
        alert = None
        end = time.monotonic() + 90
        while alert is None and time.monotonic() < end:
            time.sleep(0.1)
            for line in open(log_path):
                if '"kind"' in line:
                    rec = json.loads(line)
                    if rec["kind"] == EXPECTED[incident] and rec["at"] >= t_inject:
                        alert = rec
                        break
        time.sleep(3)
        during = answers()
        rib = {c: (lab.best_path(c, t["anycast"].replace("/24", "/25") if incident == "hijack" else "192.0.2.0/26") or {}).get("aspath", {}).get("string")
               for c in ["c1", "c2", "c3"]}
        t2_c1 = (lab.best_path("t2", "192.0.2.0/26") or {}).get("aspath", {}).get("string")
        lab.vtysh(node, *off, quiet=True)
        t_withdraw = time.time()
        end = time.monotonic() + 120
        while answers() != normal and time.monotonic() < end:
            time.sleep(1)
        after = answers()
        t_restored = time.time()
    finally:
        det.terminate()
        det.wait()
        log.close()
    alerts = [json.loads(line) for line in open(log_path) if '"kind"' in line]
    return {
        "repetition": n, "incident": incident, "load": load(),
        "baseline_alerts": baseline,
        "injected_at": t_inject, "apply_seconds": round(t_applied - t_inject, 3),
        "detected": alert is not None,
        "detection_seconds": round(alert["at"] - t_inject, 3) if alert else None,
        "first_alert": alert,
        "alert_kinds": sorted({(a["kind"], a["feed"]) for a in alerts if a["at"] >= t_inject}),
        "nearest_before": before, "nearest_during": during, "nearest_after": after,
        "best_paths_during": rib, "t2_best_path_to_c1_during": t2_c1,
        "restore_seconds_after_withdraw": round(t_restored - t_withdraw, 1), "restored": after == normal,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("incident", choices=list(EXPECTED))
    ap.add_argument("--repeat", type=int, default=5)
    a = ap.parse_args()
    run = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + a.incident
    out = os.path.join(ROOT, "docs", "evidence", "task-17-bgp", run)
    os.makedirs(out)
    results = []
    for n in range(1, a.repeat + 1):
        r = one(a.incident, out, n)
        results.append(r)
        print(json.dumps({k: r[k] for k in ["repetition", "detected", "detection_seconds", "nearest_during", "restored"]}), flush=True)
        time.sleep(5)
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump({"run": run, "results": results,
                   "rpki": lab.gobgp("rpki", "table").stdout,
                   "environment": "FRR 10.7.1 containers on Docker bridge networks; GoBGP 4.10.0 collector; "
                                  "Krill 0.16.0 test TA + Routinator 0.15.3; host shared with other projects"}, f, indent=2)
    print(out)


if __name__ == "__main__":
    main()
