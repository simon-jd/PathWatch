#!/usr/bin/env python3
"""Owned loopback benchmark: smoke first, then immutable full 120-cell matrix."""

import argparse, hashlib, json, os, re, signal, socket, ssl, subprocess, sys, time, urllib.request
from pathlib import Path
from analyze import (
    parse_wrk2_hdr,
    parse_wrk_counts,
    parse_h2_counts,
    cpu_seconds,
    duration_evidence,
    validate_duration,
)
from processes import OwnedProcess, enable_subreaper

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache/task13"
RAW = ROOT / "docs/evidence/raw/task13"
TOOLS = {
    # `make bench-build` builds into the project's CARGO_TARGET_DIR.
    "pathwatch": Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "proxy/target"))
    / "release/edge-proxy",
    "envoy": CACHE / "envoy",
    "nginx": CACHE / "nginx/sbin/nginx",
    "echo": CACHE / "echo",
    "h2load": CACHE / "nghttp2-build/src/h2load",
    "wrk2": CACHE / "wrk2-44a94c17d8e6a0bac8559b53da76848e430cb7a7/wrk",
    "observe": CACHE / "observe.so",
    "wrk_module": CACHE / "wrk2-44a94c17d8e6a0bac8559b53da76848e430cb7a7/src/wrk.lua",
}
AFFINITY = {
    "proxy": "16,17",
    "generator": "18,19,20,21",
    "backend": "22,23",
    "collector": "24",
    "coordinator": "25",
}
ENV = dict(
    os.environ,
    LC_ALL="C",
    TZ="UTC",
    LD_LIBRARY_PATH=str(CACHE / "deps/usr/lib/x86_64-linux-gnu"),
    # PathWatch's default multi-thread runtime: 2 tokio workers (NGINX has 2 workers,
    # Envoy --concurrency 2). The per-core prototype is a config variant below.
    TOKIO_WORKER_THREADS="2",
    GOMAXPROCS="2",
    LUA_PATH=str(CACHE / "wrk2-44a94c17d8e6a0bac8559b53da76848e430cb7a7/src/?.lua"),
)
PROXIES = ["pathwatch", "envoy", "nginx"]
# Variants: one changed variable each, relative to the proxy's default, as
# (proxy, extra environment, config path or None for the default config).
VARIANTS = {
    "pathwatch-w1": ("pathwatch", {"TOKIO_WORKER_THREADS": "1"}, None),
    # Two current-thread runtimes (SO_REUSEPORT, one upstream pool each).
    "pathwatch-percore": ("pathwatch", {}, "bench/pathwatch-proxy-percore.toml"),
}


def variant(name):
    if name in PROXIES:
        return name, {}, None
    proxy, env, config = VARIANTS[name]
    return proxy, dict(env), config
OWNED = []
HANDLES = []


def dump(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def start(command, output, cpu, extra=None):
    f = output.open("wb")
    HANDLES.append(f)
    enable_subreaper()
    p = subprocess.Popen(
        ["taskset", "-c", cpu, *map(str, command)],
        cwd=ROOT,
        env=dict(ENV, **(extra or {})),
        stdout=f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    owned = OwnedProcess(p)
    OWNED.append(owned)
    return owned


def stop(p, sig=signal.SIGTERM):
    p.stop(sig)


def cleanup():
    errors = []
    for p in reversed(OWNED):
        try:
            stop(p, signal.SIGINT)
        except Exception as e:
            errors.append(repr(e))
    for f in HANDLES:
        f.close()
    return errors


def closed_listeners():
    result = {}
    for port in [18443, 19090, 19091, 19092]:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            result[str(port)] = sock.connect_ex(("127.0.0.1", port)) == 111
    return result


def port_ready(port, p):
    for _ in range(100):
        if p.poll() is not None:
            raise RuntimeError("server exited; inspect raw stdout")
        try:
            with socket.create_connection(("127.0.0.1", port), 0.1):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("listener startup timeout")


def stats():
    with urllib.request.urlopen("http://127.0.0.1:19091/stats", timeout=2) as r:
        return json.load(r)


def proc_snapshot(ids, strict=False):
    result = {}
    for pid in ids:
        try:
            base = Path(f"/proc/{pid}")
            fields = (base / "stat").read_text().rsplit(")", 1)[1].split()
            io = dict(
                line.split(": ") for line in (base / "io").read_text().splitlines()
            )
            switches = 0
            for task in (base / "task").iterdir():
                try:
                    for line in (task / "status").read_text().splitlines():
                        if "ctxt_switches:" in line:
                            switches += int(line.split()[1])
                except FileNotFoundError:
                    pass
            result[str(pid)] = {
                "syscr": int(io["syscr"]),
                "syscw": int(io["syscw"]),
                # Live threads only (voluntary + involuntary); exited threads drop out.
                "ctx_switches": switches,
                "cpu_ticks": int(fields[11]) + int(fields[12]),
                "starttime": int(fields[19]),
                "session": int(fields[3]),
                "pgrp": int(fields[2]),
                "threads": len(list((base / "task").iterdir())),
                "affinity": sorted(os.sched_getaffinity(pid)),
                "rss_bytes": int((base / "statm").read_text().split()[1])
                * os.sysconf("SC_PAGE_SIZE"),
                "command": (base / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(),
            }
        # An exiting process answers EACCES on /proc/<pid>/io: also a disappearance.
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            if strict:
                raise RuntimeError("proxy process disappeared during CPU snapshot")
    return result


def validate_tls(proxy, directory):
    checks = []
    for protocol in ["http/1.1", "h2"]:
        ctx = ssl.create_default_context(cafile=str(CACHE / "runtime/cert.pem"))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.maximum_version = ssl.TLSVersion.TLSv1_3
        ctx.set_alpn_protocols([protocol])
        with socket.create_connection(("127.0.0.1", 18443), 2) as tcp:
            with ctx.wrap_socket(tcp, server_hostname="bench.pathwatch.test") as tls:
                assert (
                    tls.version() == "TLSv1.3"
                    and tls.selected_alpn_protocol() == protocol
                )
                checks.append(
                    {
                        "proxy": proxy,
                        "tls": tls.version(),
                        "alpn": tls.selected_alpn_protocol(),
                        "certificate_sha256": hashlib.sha256(
                            tls.getpeercert(binary_form=True)
                        ).hexdigest(),
                        "verified_trust": True,
                        "sni": "bench.pathwatch.test",
                    }
                )
                if protocol == "http/1.1":
                    tls.sendall(
                        b"GET / HTTP/1.1\r\nHost: bench.pathwatch.test\r\nConnection: close\r\n\r\n"
                    )
                    data = b""
                    while True:
                        part = tls.recv(65536)
                        if not part:
                            break
                        data += part
                    headers, body = data.split(b"\r\n\r\n", 1)
                    assert headers.startswith(b"HTTP/1.1 200") and body == b"x" * 1024
                    checks[-1]["body_sha256"] = hashlib.sha256(body).hexdigest()
    dump(directory / "protocol-check.json", checks)


def load_command(protocol, rate, duration, fifo=None):
    if protocol == "h1":
        return [
            TOOLS["wrk2"],
            "-t4",
            "-c100",
            "-d" + str(duration) + "s",
            "-R" + str(rate),
            "--timeout",
            "10s",
            "--latency",
            "-s",
            "bench/wrk.lua",
            "https://bench.pathwatch.test:18443/",
        ]
    cmd = [
        TOOLS["h2load"],
        "-t4",
        "-c100",
        "-m16",
        "-D" + str(duration),
        "--rps=" + str(rate / 100),
        "--connect-to=127.0.0.1:18443",
        "--connection-active-timeout=" + str(duration + 10),
        "--connection-inactivity-timeout=10",
        "https://bench.pathwatch.test:18443/",
    ]
    if fifo:
        cmd.extend(["--log-file=" + str(fifo)])
    return cmd


def wait_load(
    p,
    collector,
    timeout,
    directory,
    ids,
    server=None,
    expected=None,
    name="monitor.json",
):
    deadline = time.monotonic() + timeout
    samples = []
    try:
        while p.poll() is None:
            if collector and collector.poll() not in (None, 0):
                raise RuntimeError("collector failed before generator")
            # Exit0 may mean the writer closed FIFO just before process exit.
            # Keep waiting; reconcile the drained histogram after generator exit.
            if time.monotonic() > deadline:
                raise RuntimeError("generator exceeded bounded deadline")
            if server:
                server.require_same(expected)
            blocked = 0
            for task in Path(f"/proc/{p.pid}/task").glob("*"):
                try:
                    blocked += int("pipe_write" in (task / "wchan").read_text())
                except FileNotFoundError:
                    pass
            members = set(ids)
            for job in OWNED:
                members.update(job.snapshot())
            snapshot = proc_snapshot(sorted(members))
            rss = sum(x["rss_bytes"] for x in snapshot.values())
            if rss > 16000000000:
                raise RuntimeError("resident budget exceeded")
            samples.append(
                {
                    "monotonic": time.monotonic(),
                    "fifo_write_blocked_threads": blocked,
                    "rss_bytes": rss,
                    "collector_eof_pending": bool(collector and collector.poll() == 0),
                    "loadavg": Path("/proc/loadavg").read_text().strip(),
                }
            )
            time.sleep(0.25)
        if server:
            server.require_same(expected)
        return p.returncode
    finally:
        dump(directory / name, samples)


def per_completed(before, after, completed):
    if not completed:
        return None
    return {
        key: sum(after[k][key] - before[k][key] for k in before) / completed
        for key in ("syscr", "syscw", "ctx_switches")
    }


def one(name, protocol, rate, round_no, duration, warmup, out):
    proxy, proxy_env, proxy_config = variant(name)
    d = out / f"{protocol}-r{round_no}-{rate}-{name}"
    d.mkdir()
    began = time.monotonic()
    result = {
        "proxy": proxy,
        "variant": name,
        "proxy_env": proxy_env,
        "proxy_config": proxy_config,
        "protocol": protocol,
        "rate": rate,
        "round": round_no,
        "duration_s": duration,
        "warmup_s": warmup,
        "start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "offered_requests": rate * duration,
        "affinity": AFFINITY,
        "status": "running",
    }
    dump(d / "result.json", result)
    cmd = {
        "pathwatch": [TOOLS["pathwatch"], proxy_config or "bench/pathwatch-proxy.toml"],
        "envoy": [
            TOOLS["envoy"],
            "-c",
            "bench/envoy.yaml",
            "--concurrency",
            "2",
            "--disable-hot-restart",
            "--log-level",
            "error",
        ],
        "nginx": [
            TOOLS["nginx"],
            "-e",
            "stderr",
            "-p",
            str(ROOT) + "/",
            "-c",
            "bench/nginx.conf",
        ],
    }[proxy]
    server = start(cmd, d / "proxy.txt", AFFINITY["proxy"], proxy_env)
    collector = None
    load = None
    sampler = None
    warm = None
    measurement_start = None
    result["cpu_accounting_valid"] = False
    try:
        port_ready(18443, server)
        time.sleep(0.3)
        expected = server.snapshot()
        ids = sorted(expected)
        result["proxy_pids"] = ids
        result["proxy_identities"] = list(expected.values())
        validate_tls(proxy, d)
        warmcmd = load_command(protocol, rate, warmup)
        warm_start = time.monotonic()
        warm = start(warmcmd, d / "warmup.txt", AFFINITY["generator"])
        result["warmup_pid"] = warm.pid
        dump(d / "result.json", result)
        try:
            warm_rc = wait_load(
                warm, None, warmup + 15, d, ids, server, expected, "warmup-monitor.json"
            )
            result["warmup"] = duration_evidence(
                protocol,
                (d / "warmup.txt").read_text(),
                warmup,
                time.monotonic() - warm_start,
                warm_rc,
            )
            result["warmup_returncode"] = warm_rc
            validate_duration(protocol, result["warmup"], warmup, "warmup")
        finally:
            stop(warm)
        before = stats()
        server.require_same(expected)
        beforeproc = proc_snapshot(ids, strict=True)
        dump(d / "process-before.json", beforeproc)
        fifo = None
        if protocol == "h2":
            fifo = CACHE / "runtime/requests.fifo"
            if fifo.exists():
                raise RuntimeError("stale FIFO requires inspection")
            os.mkfifo(fifo, 0o600)
            collector = start(
                [sys.executable, "bench/collect.py", fifo, d / "h2"],
                d / "collector.txt",
                AFFINITY["collector"],
            )
        cmd = load_command(protocol, rate, duration, fifo)
        result["command"] = list(map(str, cmd))
        sampler = start(
            ["pidstat", "-h", "-H", "-u", "-p", ",".join(map(str, ids)), "1"],
            d / "pidstat.txt",
            AFFINITY["coordinator"],
        )
        measurement_start = time.monotonic()
        load = start(
            cmd,
            d / "load.txt",
            AFFINITY["generator"],
            {
                "LD_PRELOAD": str(TOOLS["observe"]),
                "BENCH_OBSERVE": str(d / "observer.json"),
            },
        )
        result["generator_pid"] = load.pid
        result["collector_pid"] = collector.pid if collector else None
        dump(d / "result.json", result)
        rc = wait_load(load, collector, duration + 20, d, ids, server, expected)
        result["wall_elapsed_s"] = time.monotonic() - measurement_start
        result["returncode"] = rc
        result["measurement"] = duration_evidence(
            protocol,
            (d / "load.txt").read_text(),
            duration,
            result["wall_elapsed_s"],
            rc,
        )
        validate_duration(protocol, result["measurement"], duration, "measurement")
        server.require_same(expected)
        afterproc = proc_snapshot(ids, strict=True)
        if any(
            beforeproc[k]["starttime"] != afterproc[k]["starttime"] for k in beforeproc
        ):
            raise RuntimeError("proxy identity changed during CPU accounting")
        result["cpu_accounting_valid"] = True
        after = stats()
        dump(d / "process-after.json", afterproc)
        stop(sampler, signal.SIGINT)
        result["backend_delta"] = {k: after[k] - before[k] for k in before}
        result["cpu_pidstat_seconds"] = cpu_seconds((d / "pidstat.txt").read_text())
        ticks = sum(
            afterproc[k]["cpu_ticks"] - beforeproc[k]["cpu_ticks"] for k in beforeproc
        )
        result["cpu_proc_seconds"] = ticks / os.sysconf("SC_CLK_TCK")
        result["cpu_boundary_delta_seconds"] = (
            result["cpu_proc_seconds"] - result["cpu_pidstat_seconds"]
        )
        if collector:
            collector.wait(timeout=5)
            if collector.returncode:
                raise RuntimeError("collector failed")
            hist = json.loads((d / "h2.json").read_text())
            counts = parse_h2_counts((d / "load.txt").read_text())
            result["counts"] = counts
            result["latency"] = hist["quantiles"]
            body = re.search(r"\((\d+)\) data", (d / "load.txt").read_text())
            if not body:
                raise RuntimeError("h2 body byte count unavailable")
            result["received_body_bytes"] = int(body[1])
            result["completed_success_body_bytes_expected"] = counts["succeeded"] * 1024
            result["body_bytes_delta"] = int(body[1]) - counts["succeeded"] * 1024
            if (
                hist["records"] != counts["done"]
                or hist["success"] != counts["succeeded"]
            ):
                raise RuntimeError("h2 collector/count mismatch")
            completed = counts["done"]
            result["h2_count_reconciliation"] = "matched"
        else:
            result["counts"] = parse_wrk_counts((d / "load.txt").read_text())
            completed = result["counts"]["completed"]
            try:
                result["latency"] = parse_wrk2_hdr((d / "load.txt").read_text())
            except ValueError as e:
                result["latency"] = None
                result["latency_unavailable"] = str(e)
        result["proxy_per_completed"] = per_completed(beforeproc, afterproc, completed)
        result["configured_window_rps"] = completed / duration
        result["achieved_rps"] = completed / result["measurement"]["reported_elapsed_s"]
        result["cpu_us_per_completed"] = (
            result["cpu_pidstat_seconds"] * 1e6 / completed if completed else None
        )
        result["upstream_requests_per_new_connection"] = (
            result["backend_delta"]["requests"] / result["backend_delta"]["connections"]
            if result["backend_delta"]["connections"]
            else None
        )
        observer = json.loads((d / "observer.json").read_text())
        result["observer"] = observer
        if protocol == "h1":
            result["counts"]["started"] = observer["http1_requests_started"]
        result["unfinished_at_end"] = result["counts"]["started"] - completed
        if observer["fifo_write_errors"] or observer["fifo_partial_writes"]:
            raise RuntimeError("FIFO write lost records")
        if protocol == "h2" and observer["fifo_write_calls"] != counts["done"]:
            raise RuntimeError("FIFO write count mismatch")
        tls = [
            l
            for l in (d / "load.txt").read_text().splitlines()
            if l.startswith("BENCH_TLS\t")
        ]
        if not tls or any(
            not l.startswith("BENCH_TLS\tTLSv1.3\tbench.pathwatch.test\t") for l in tls
        ):
            raise RuntimeError("actual generator TLS/SNI mismatch")
        if protocol == "h2" and any(not l.endswith("\th2") for l in tls):
            raise RuntimeError("actual generator ALPN mismatch")
        result["actual_tls_handshakes"] = len(tls)
        result["status"] = "measured" if rc == 0 else "generator_failed"
    except BaseException as e:
        result["status"] = (
            "aborted"
            if isinstance(e, (KeyboardInterrupt, SystemExit))
            else "measurement_failed"
        )
        result["failure"] = repr(e)
        if measurement_start is not None and "measurement" not in result:
            result["measurement_failure_elapsed_s"] = (
                time.monotonic() - measurement_start
            )
        raise
    finally:
        cell_cleanup_errors = []
        for p in [load, sampler, collector, warm]:
            if p:
                try:
                    stop(p, signal.SIGINT)
                except Exception as e:
                    cell_cleanup_errors.append(repr(e))
        try:
            stop(
                server,
                {
                    "nginx": signal.SIGQUIT,
                    "pathwatch": signal.SIGINT,
                    "envoy": signal.SIGTERM,
                }[proxy],
            )
        except Exception as e:
            cell_cleanup_errors.append(repr(e))
        result["cleanup_errors"] = cell_cleanup_errors
        if load and "measurement" not in result:
            result["measurement"] = duration_evidence(
                protocol,
                (d / "load.txt").read_text(),
                duration,
                result.get("measurement_failure_elapsed_s", 0),
                load.returncode,
            )
            result["measurement"]["incomplete_at_failure"] = True
        result["proxy_cleanup"] = server.evidence()
        fifo = CACHE / "runtime/requests.fifo"
        if fifo.exists():
            fifo.unlink()
        result["total_elapsed_s"] = time.monotonic() - began
        result["end_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        dump(d / "result.json", result)
        if cell_cleanup_errors:
            raise RuntimeError("cell descendant cleanup failed; see result.json")
    print(
        json.dumps(
            {
                "cell": d.name,
                "status": result["status"],
                "achieved_rps": result["achieved_rps"],
                "latency": result["latency"],
            }
        ),
        flush=True,
    )
    return result


# Project disk share, measured on the checkout; doubled from 6 GB on 2026-10-27.
DISK_SHARE = 12_000_000_000


def resources():
    checkout = int(subprocess.check_output(["du", "-s", "-B1", ROOT]).split()[0])
    return {
        "checkout_allocated": checkout,
        "total": checkout,
        "soft_limit": DISK_SHARE,
    }


def plan(full, protocol, proxy, rate, matrix, rounds=None):
    """Cells as (protocol, round, rate, proxy, duration_s, warmup_s).

    --full: both protocols, every proxy. --matrix: the same 4 rates x 5 rounds at
    full duration, restricted by --protocol/--proxy and optionally --rounds
    (each round keeps its proxy rotation). Otherwise one 20 s smoke cell.
    """
    repeated = full or matrix
    protocols = ["h1", "h2"] if full or not protocol else [protocol]
    cells = []
    for proto in protocols:
        for round_no in (rounds if rounds is not None else range(5)) if repeated else range(1):
            for r in [1000, 5000, 10000, 20000] if repeated else [rate]:
                order = PROXIES[round_no % 3 :] + PROXIES[: round_no % 3]
                for p in order if full or not proxy else [proxy]:
                    cells.append((proto, round_no, r, p, *((120, 15) if repeated else (20, 2))))
    return cells


def plan_diag(variants, rates, rounds):
    """Short H2 cells (30 s, 5 s warmup); variants rotate each round as proxies do."""
    cells = []
    for round_no in range(rounds):
        for r in rates:
            k = round_no % len(variants)
            for v in variants[k:] + variants[:k]:
                cells.append(("h2", round_no, r, v, 30, 5))
    return cells


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--full", action="store_true")
    p.add_argument("--matrix", action="store_true")
    p.add_argument(
        "--diag",
        help="comma-separated proxies/variants for a short H2 diagnostic (see VARIANTS)",
    )
    p.add_argument("--diag-rates", default="1000,10000")
    p.add_argument("--diag-rounds", type=int, default=3)
    p.add_argument("--rate", type=int, default=20000)
    p.add_argument("--protocol", choices=["h1", "h2"])
    p.add_argument("--proxy", choices=PROXIES + list(VARIANTS))
    p.add_argument("--rounds", help="comma-separated round numbers 0-4 (matrix only)")
    p.add_argument("--output", required=True)
    a = p.parse_args()
    a.rounds = [int(x) for x in a.rounds.split(",")] if a.rounds else None
    if a.rounds and (not a.matrix or not set(a.rounds) <= set(range(5))):
        raise SystemExit("--rounds needs --matrix and rounds in 0-4")
    os.chdir(ROOT)
    os.sched_setaffinity(0, {25})
    out = RAW / a.output
    if out.exists():
        raise SystemExit("output must be fresh; preserve previous attempts")
    if not all(closed_listeners().values()):
        raise SystemExit("benchmark ports already occupied; no foreign process touched")
    out.mkdir(parents=True)
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    tracked = [
        str(p.relative_to(ROOT))
        for p in sorted((ROOT / "bench").rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    ]
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--", "bench"], text=True
    )
    if (a.full or a.matrix or a.diag) and dirty:
        raise SystemExit("full matrix and diagnostics require committed clean bench source")
    files = {str(v.relative_to(ROOT)): sha(v) for v in TOOLS.values()}
    files.update({f: sha(ROOT / f) for f in tracked})
    files[".cache/task13/runtime/cert.pem"] = sha(CACHE / "runtime/cert.pem")
    # Resolve actual ELF dependencies, including the owned nghttp2 shared library.
    libraries = set()
    for binary in [
        *TOOLS.values(),
        Path(sys.executable),
        Path("/usr/bin/pidstat"),
        Path("/usr/bin/taskset"),
    ]:
        if binary.suffix == ".lua":
            continue
        listing = subprocess.run(
            ["ldd", str(binary)], capture_output=True, text=True, env=ENV
        )
        for line in listing.stdout.splitlines():
            match = re.search(r"(?:=> )?(/[^ ]+) \(", line)
            if match:
                libraries.add(Path(match[1]).resolve())
    for lib in libraries | {
        Path(sys.executable).resolve(),
        Path("/usr/bin/pidstat"),
        Path("/usr/bin/taskset"),
    }:
        files[str(lib)] = sha(lib)
    r = resources()
    if r["total"] + 80000000 >= r["soft_limit"]:
        raise SystemExit("resource headroom inadequate")
    manifest = {
        "source": source,
        "input_sha256": files,
        "affinity": AFFINITY,
        "resources_before": r,
        "full": a.full,
        "pid": os.getpid(),
        "uname": list(os.uname()),
        "environment": {
            key: ENV[key]
            for key in [
                "LC_ALL",
                "TZ",
                "LD_LIBRARY_PATH",
                "TOKIO_WORKER_THREADS",
                "GOMAXPROCS",
                "LUA_PATH",
            ]
        },
        "cpuinfo": subprocess.check_output(["lscpu"], text=True),
        "start_monotonic": time.monotonic(),
    }
    dump(out / "manifest.json", manifest)
    backend = start([TOOLS["echo"]], out / "backend.txt", AFFINITY["backend"])
    results = []
    try:
        port_ready(19090, backend)
        port_ready(19091, backend)
        if a.diag:
            names = a.diag.split(",")
            for n in names:
                variant(n)
            rates = [int(x) for x in a.diag_rates.split(",")]
            cells = plan_diag(names, rates, a.diag_rounds)
        else:
            cells = plan(a.full, a.protocol, a.proxy, a.rate, a.matrix, a.rounds)
        manifest["planned_cells"] = len(cells)
        for protocol, round_no, rate, proxy, duration, warmup in cells:
            results.append(one(proxy, protocol, rate, round_no, duration, warmup, out))
            dump(
                out / "progress.json",
                {
                    "completed_cells": len(results),
                    "last": results[-1],
                    "owned_live_pids": [x.pid for x in OWNED if x.poll() is None],
                },
            )
            if resources()["total"] + 80000000 >= r["soft_limit"]:
                raise RuntimeError("resource soft limit reached")
        manifest["measurement_wall_elapsed_s"] = sum(
            r["measurement"]["wall_elapsed_s"] for r in results
        )
        manifest["measurement_reported_elapsed_s"] = sum(
            r["measurement"]["reported_elapsed_s"] for r in results
        )
        manifest["actual_elapsed_s"] = time.monotonic() - manifest["start_monotonic"]
        needed = sum(c[4] for c in cells)
        if (a.full or a.matrix) and (
            len(results) != len(cells)
            or manifest["measurement_wall_elapsed_s"] < needed
            or manifest["measurement_reported_elapsed_s"] < needed
        ):
            raise RuntimeError("matrix incomplete")
        manifest["completed_cells"] = len(results)
    finally:
        manifest["cleanup_errors"] = cleanup()
        manifest["owned_processes"] = [p.evidence() for p in OWNED]
        manifest["cleanup_listeners"] = closed_listeners()
        manifest["input_hashes_unchanged"] = all(
            sha(ROOT / path) == digest for path, digest in files.items()
        )
        manifest["resources_after"] = resources()
        manifest["cleanup_live_pids"] = sorted(
            {pid for p in OWNED for pid in p.snapshot()}
        )
        dump(out / "manifest.json", manifest)
        if (
            manifest["cleanup_errors"]
            or manifest["cleanup_live_pids"]
            or not all(manifest["cleanup_listeners"].values())
        ):
            raise RuntimeError("owned process/listener cleanup incomplete")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        main()
    finally:
        cleanup()
