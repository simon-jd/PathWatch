"""Read-only audit of the completed matrix; writes derived evidence only."""
import array
import datetime
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "bench"))
import analyze

BASE = Path(__file__).with_name("matrix01")
manifest = json.loads((BASE / "manifest.json").read_text())
summary = analyze.summarize_matrix(BASE)
cells = []
for directory in BASE.iterdir():
    if not directory.is_dir():
        continue
    r = json.loads((directory / "result.json").read_text())
    before = json.loads((directory / "process-before.json").read_text())
    after = json.loads((directory / "process-after.json").read_text())
    assert set(before) == set(after) == set(map(str, r["proxy_pids"]))
    assert len(before) == (3 if r["proxy"] == "nginx" else 1)
    assert all(before[p]["starttime"] == after[p]["starttime"] for p in before)
    assert all(x["affinity"] == [16, 17] for x in [*before.values(), *after.values()])
    cpu = 0.0
    sample_pids = set()
    for line in (directory / "pidstat.txt").read_text().splitlines():
        fields = line.split()
        if len(fields) >= 10 and all(x.isdigit() for x in fields[:3]):
            sample_pids.add(fields[2])
            cpu += float(fields[7]) / 100
    assert sample_pids == set(before)
    assert abs(cpu - r["cpu_pidstat_seconds"]) < 1e-8
    proc_cpu = sum(after[p]["cpu_ticks"] - before[p]["cpu_ticks"] for p in before) / os.sysconf("SC_CLK_TCK")
    assert abs(proc_cpu - r["cpu_proc_seconds"]) < 1e-8
    for phase, seconds in [("load", 120), ("warmup", 15)]:
        key = "measurement" if phase == "load" else "warmup"
        parsed = analyze.duration_evidence(r["protocol"], (directory / f"{phase}.txt").read_text(), seconds, r[key]["wall_elapsed_s"], r[key]["returncode"])
        assert parsed == r[key]
        analyze.validate_duration(r["protocol"], parsed, seconds, phase)
    assert not r["cleanup_errors"] and not r["proxy_cleanup"]["live_descendants"]
    checks = json.loads((directory / "protocol-check.json").read_text())
    assert all(x["tls"] == "TLSv1.3" and x["verified_trust"] and x["sni"] == "bench.pathwatch.test" for x in checks)
    assert r["actual_tls_handshakes"] == r["observer"]["tls_handshakes"] == 100
    assert json.loads((directory / "observer.json").read_text()) == r["observer"]
    load_text = (directory / "load.txt").read_text()
    if r["protocol"] == "h1":
        raw_counts = analyze.parse_wrk_counts(load_text)
        raw_counts["started"] = r["observer"]["http1_requests_started"]
        assert raw_counts == r["counts"]
        assert analyze.parse_wrk2_hdr(load_text) == r["latency"]
        assert not any(raw_counts["errors"].values())
    else:
        assert analyze.parse_h2_counts(load_text) == r["counts"]
        assert int(re.search(r"\((\d+)\) data", load_text)[1]) == r["received_body_bytes"]
        assert not any(r["counts"][k] for k in ["failed", "errored", "timeout"])
    monitor = json.loads((directory / "monitor.json").read_text())
    warm_monitor = json.loads((directory / "warmup-monitor.json").read_text())
    extra = {
        "cell": directory.name,
        "owned_peak_rss_bytes": max(x["rss_bytes"] for x in monitor + warm_monitor),
        "fifo_blocked_sample_count": sum(x["fifo_write_blocked_threads"] > 0 for x in monitor),
        "fifo_max_blocked_threads": max(x["fifo_write_blocked_threads"] for x in monitor),
        "proxy_sampled_cpu_pids": sorted(sample_pids, key=int),
        "proxy_threads_before": {p: x["threads"] for p, x in before.items()},
        "proxy_rss_boundary_max_bytes": max(sum(x["rss_bytes"] for x in snap.values()) for snap in [before, after]),
        "certificate_sha256": checks[0]["certificate_sha256"],
    }
    if r["protocol"] == "h2":
        h = json.loads((directory / "h2.json").read_text())
        binary = (directory / "h2.hist").read_bytes()
        assert len(binary) == 65536 * 8
        counts = array.array("Q", binary)
        if sys.byteorder != "little": counts.byteswap()
        assert sum(counts) == h["success"] == r["counts"]["succeeded"]
        assert h["records"] == r["counts"]["done"] == r["observer"]["fifo_write_calls"]
        assert h["quantiles"] == analyze.quantiles(counts) == r["latency"]
        assert h["statuses"] == {"200": h["success"]}
        assert h["http_failed"] == h["stream_failed"] == 0
        assert r["received_body_bytes"] == 1024 * h["success"] and r["body_bytes_delta"] == 0
        assert r["h2_count_reconciliation"] == "matched"
        assert r["observer"]["fifo_write_errors"] == r["observer"]["fifo_partial_writes"] == 0
        extra["collector_max_completion_lag_us"] = h["max_completion_lag_us"]
        extra["histogram_sha256"] = hashlib.sha256(binary).hexdigest()
    cells.append(dict(r, audit=extra))

ordered = sorted(cells, key=lambda r: r["start_utc"])
proxies = ["pathwatch", "envoy", "nginx"]
expected = [(h, rnd, rate, proxy) for h in ["h1", "h2"] for rnd in range(5) for rate in [1000, 5000, 10000, 20000] for proxy in proxies[rnd % 3:] + proxies[:rnd % 3]]
assert [(r["protocol"], r["round"], r["rate"], r["proxy"]) for r in ordered] == expected
assert len({r["audit"]["certificate_sha256"] for r in cells}) == 1
for path, digest in manifest["input_sha256"].items():
    assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path
subprocess.run(["git", "merge-base", "--is-ancestor", manifest["source"], "HEAD"], cwd=ROOT, check=True)
subprocess.run(["git", "diff", "--exit-code", manifest["source"], "HEAD", "--", "bench"], cwd=ROOT, check=True)
assert not subprocess.check_output(["git", "status", "--porcelain", "--", "bench"], cwd=ROOT)

groups = []
for protocol in ["h1", "h2"]:
    for proxy in proxies:
        rs = [r for r in cells if r["protocol"] == protocol and r["proxy"] == proxy]
        done_key = "completed" if protocol == "h1" else "done"
        group = {
            "protocol": protocol, "proxy": proxy, "cells": len(rs),
            "offered": sum(r["offered_requests"] for r in rs),
            "started": sum(r["counts"]["started"] for r in rs),
            "completed": sum(r["counts"][done_key] for r in rs),
            "unfinished_at_end": sum(r["unfinished_at_end"] for r in rs),
            "backend_requests": sum(r["backend_delta"]["requests"] for r in rs),
            "backend_new_connections": sum(r["backend_delta"]["connections"] for r in rs),
            "backend_closed_connections": sum(r["backend_delta"]["closed"] for r in rs),
            "cells_without_new_backend_connections": sum(r["backend_delta"]["connections"] == 0 for r in rs),
            "tls_handshakes": sum(r["actual_tls_handshakes"] for r in rs),
            "cpu_pidstat_seconds": sum(r["cpu_pidstat_seconds"] for r in rs),
            "cpu_proc_seconds": sum(r["cpu_proc_seconds"] for r in rs),
            "max_absolute_cpu_boundary_delta_seconds": max(abs(r["cpu_boundary_delta_seconds"]) for r in rs),
            "owned_peak_rss_bytes": max(r["audit"]["owned_peak_rss_bytes"] for r in rs),
            "proxy_rss_boundary_max_bytes": max(r["audit"]["proxy_rss_boundary_max_bytes"] for r in rs),
        }
        if protocol == "h2":
            group.update(fifo_write_calls=sum(r["observer"]["fifo_write_calls"] for r in rs),fifo_write_total_ns=sum(r["observer"]["fifo_write_ns"] for r in rs),fifo_max_write_ns=max(r["observer"]["fifo_max_write_ns"] for r in rs),fifo_blocked_samples=sum(r["audit"]["fifo_blocked_sample_count"] for r in rs),fifo_max_blocked_threads=max(r["audit"]["fifo_max_blocked_threads"] for r in rs),collector_max_completion_lag_us=max(r["audit"]["collector_max_completion_lag_us"] for r in rs))
        groups.append(group)
report = {
    "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "source_commit": "24f13eddaf2235beebb65bcb5ae05e9de80b2280",
    "run_head": manifest["source"], "accepted_summary_rows": len(summary),
    "checks_passed": "120 unique ordered cells; native/wall durations; all original input hashes; same certificate; worker identity/CPU PID coverage; pidstat and proc tick sums; raw load/observer counts and H1 HDR; all60 H2 binary histogram totals/quantiles/counts/200statuses/bodybytes; all reported error categories zero; all cleanup records",
    "first_cell_start_utc": ordered[0]["start_utc"], "last_cell_end_utc": ordered[-1]["end_utc"],
    "measurement_reported_sum_s": sum(r["measurement"]["reported_elapsed_s"] for r in cells),
    "measurement_wall_sum_s": sum(r["measurement"]["wall_elapsed_s"] for r in cells),
    "warmup_reported_sum_s": sum(r["warmup"]["reported_elapsed_s"] for r in cells),
    "warmup_wall_sum_s": sum(r["warmup"]["wall_elapsed_s"] for r in cells),
    "measurement_reported_range_s": [min(r["measurement"]["reported_elapsed_s"] for r in cells),max(r["measurement"]["reported_elapsed_s"] for r in cells)],
    "warmup_reported_range_s": [min(r["warmup"]["reported_elapsed_s"] for r in cells),max(r["warmup"]["reported_elapsed_s"] for r in cells)],
    "groups": groups,
    "cell_audit": [r["audit"] for r in ordered],
}
Path(__file__).with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({k:v for k,v in report.items() if k != "cell_audit"}, indent=2))
