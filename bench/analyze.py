"""Parse retained benchmark outputs; all latency values use microseconds."""

import math
import re

BINS = 65536


def parse_wrk2_hdr(text):
    marker = "Latency Distribution (HdrHistogram - Recorded Latency)"
    if marker not in text:
        raise ValueError("missing corrected HdrHistogram")
    section = text.split(marker, 1)[1].split("Detailed Percentile spectrum:", 1)[0]
    count = re.search(r"Total count\s*=\s*(\d+)", text.split(marker, 1)[1])
    if not count or int(count[1]) == 0:
        raise ValueError("missing or empty histogram")
    result = {}
    for label, key in [
        ("50.000", "p50_us"),
        ("99.000", "p99_us"),
        ("99.900", "p999_us"),
    ]:
        m = re.search(
            r"^\s*" + re.escape(label) + r"%\s+([0-9]+(?:\.[0-9]+)?)(us|ms|s)\s*$",
            section,
            re.M,
        )
        if not m:
            raise ValueError("missing/unsupported percentile " + label)
        result[key] = float(m[1]) * {"us": 1, "ms": 1000, "s": 1000000}[m[2]]
    if not result["p50_us"] <= result["p99_us"] <= result["p999_us"]:
        raise ValueError("non-monotone percentiles")
    return result


def bin_index(value):
    if not isinstance(value, int) or value < 0 or value >= 1 << 63:
        raise ValueError("latency outside histogram range")
    shift = max(0, value.bit_length() - 11)
    return value if shift == 0 else (shift + 1) * 1024 + (value >> shift) - 1024


def bin_bounds(index):
    if not 0 <= index < BINS:
        raise ValueError("invalid histogram index")
    if index < 2048:
        return index, index
    shift = index // 1024 - 1
    lower = (1024 + index % 1024) << shift
    return lower, lower + (1 << shift) - 1


def quantiles(counts):
    total = sum(counts)
    if total == 0:
        raise ValueError("empty histogram")
    result = {}
    for p, name in [(0.5, "p50"), (0.99, "p99"), (0.999, "p999")]:
        target = math.ceil(total * p)
        cumulative = 0
        for i, count in enumerate(counts):
            cumulative += count
            if cumulative >= target:
                lo, hi = bin_bounds(i)
                result[name + "_us_bounds"] = [lo, hi]
                break
    return result


def parse_wrk_counts(text):
    m = re.search(r"(\d+) requests in ([\d.]+)([smh]),", text)
    if not m:
        raise ValueError("missing wrk request count")
    errors = {k: 0 for k in ["connect", "read", "write", "timeout", "http"]}
    line = re.search(r"Socket errors: (.+)", text)
    if line:
        for key, number in re.findall(r"(connect|read|write|timeout) (\d+)", line[1]):
            errors[key] = int(number)
    status = re.search(r"Non-2xx or 3xx responses: (\d+)", text)
    if status:
        errors["http"] = int(status[1])
    return {
        "completed": int(m[1]),
        "elapsed_s": float(m[2]) * {"s": 1, "m": 60, "h": 3600}[m[3]],
        "errors": errors,
    }


def parse_h2_counts(text):
    m = re.search(
        r"requests: (\d+) total, (\d+) started, (\d+) done, (\d+) succeeded, (\d+) failed, (\d+) errored, (\d+) timeout",
        text,
    )
    if not m:
        raise ValueError("missing h2load counts")
    return dict(
        zip(
            ["total", "started", "done", "succeeded", "failed", "errored", "timeout"],
            map(int, m.groups()),
        )
    )


def cpu_seconds(text):
    """Sum non-Average pidstat rows, one-second sampling, across process PIDs.

    %CPU is not normalized by -I. Each interval contributes percent/100 CPU s.
    Initial/final partial intervals are excluded; /proc ticks independently bound it.
    """
    total = 0.0
    samples = 0
    for line in text.splitlines():
        cols = line.split()
        if (
            len(cols) >= 10
            and cols[0].isdigit()
            and cols[1].isdigit()
            and cols[2].isdigit()
        ):
            total += float(cols[7]) / 100.0
            samples += 1
    if not samples:
        raise ValueError("no pidstat CPU samples")
    return total


# Pinned wrk2 stop_at precedes worker creation and its reported runtime starts
# afterwards. 50ms covers that observed startup skew; it is not minute rounding.
# h2load's dtos seconds rounds to 0.01s. Independent monotonic time and native
# h2 duration-completion callbacks must also agree; total warmup cannot compensate.
DURATION_TOLERANCE_S = 0.05


def duration_evidence(protocol, text, configured, wall_elapsed, returncode):
    if protocol == "h1":
        match = re.search(r"^BENCH_WRK_DURATION_US (\d+)$", text, re.M)
        reported = int(match[1]) / 1e6 if match else None
        timer_threads = None
    else:
        match = re.search(r"finished in ([0-9.]+)(us|ms|s),", text)
        reported = (
            float(match[1]) * {"us": 1e-6, "ms": 1e-3, "s": 1}[match[2]]
            if match
            else None
        )
        timer_threads = sorted(
            map(
                int,
                re.findall(
                    r"Main benchmark duration is over for thread #(\d+)\.", text
                ),
            )
        )
    return {
        "configured_s": configured,
        "wall_elapsed_s": wall_elapsed,
        "reported_elapsed_s": reported,
        "returncode": returncode,
        "timer_threads": timer_threads,
        "tolerance_s": DURATION_TOLERANCE_S,
    }


def validate_duration(protocol, evidence, configured, stage):
    if evidence.get("configured_s") != configured or evidence.get("returncode") != 0:
        raise ValueError(stage + " duration/exit incomplete")
    for key in ["wall_elapsed_s", "reported_elapsed_s"]:
        elapsed = evidence.get(key, float("nan"))
        if (
            not isinstance(elapsed, (int, float))
            or not math.isfinite(elapsed)
            or not configured - DURATION_TOLERANCE_S <= elapsed <= configured + 20
        ):
            raise ValueError(
                stage + " duration incomplete: " + key + "=" + str(elapsed)
            )
    if protocol == "h2" and evidence.get("timer_threads") != [0, 1, 2, 3]:
        raise ValueError(stage + " duration timer completion missing")


def summarize_matrix(*directories):
    """Validate acceptance and summarize medians of five round quantiles.

    A --full run must hold all 120 cells. A --matrix run holds 4 rates x 5 rounds
    for each (protocol, proxy) present; every such group must be complete. Several
    directories (--rounds chunks of one matrix) are validated each against its own
    manifest and must together form complete groups without duplicates.
    """
    import json
    import statistics
    from pathlib import Path

    runs, full = [], False
    for directory in map(Path, directories):
        manifest = json.loads((directory / "manifest.json").read_text())
        part = [json.loads(p.read_text()) for p in sorted(directory.glob("*/result.json"))]
        full = full or bool(manifest.get("full"))
        n = 120 if manifest.get("full") else len(part)
        need = 120 * n
        if not (
            n
            and manifest.get("completed_cells") == n
            and manifest.get("planned_cells", n) == n
            and manifest.get("actual_elapsed_s", 0) >= need
            and manifest.get("measurement_wall_elapsed_s", 0) >= need
            and manifest.get("measurement_reported_elapsed_s", 0) >= need
            and manifest.get("input_hashes_unchanged")
            and manifest.get("cleanup_live_pids") == []
            and manifest.get("cleanup_errors") == []
            and set(manifest.get("cleanup_listeners", {}))
            == {"18443", "19090", "19091", "19092"}
            and all(manifest["cleanup_listeners"].values())
        ):
            raise ValueError("missing cells or incomplete/unbound matrix; no acceptance summary")
        runs += part
    groups = sorted({(r["protocol"], r["proxy"]) for r in runs})
    if full:
        groups = [(p, x) for p in ["h1", "h2"] for x in ["pathwatch", "envoy", "nginx"]]
    cells = 20 * len(groups)
    needed = 120 * cells
    expected = {
        (protocol, proxy, rate, round_no)
        for protocol, proxy in groups
        for rate in [1000, 5000, 10000, 20000]
        for round_no in range(5)
    }
    keys = [(r["protocol"], r["proxy"], r["rate"], r["round"]) for r in runs]
    if len(keys) != cells or set(keys) != expected:
        raise ValueError("missing or duplicated matrix cell")
    for run in runs:
        validate_duration(
            run["protocol"], run.get("measurement", {}), 120, "measurement"
        )
        validate_duration(run["protocol"], run.get("warmup", {}), 15, "warmup")
        if run.get("status") != "measured" or not run.get("cpu_accounting_valid"):
            raise ValueError("incomplete measurement / CPU accounting")
    if any(
        sum(r["measurement"][key] for r in runs) < needed
        for key in ["wall_elapsed_s", "reported_elapsed_s"]
    ):
        raise ValueError("actual measured duration below 120 s per cell")
    rows = []
    for protocol in ["h1", "h2"]:
        for rate in [1000, 5000, 10000, 20000]:
            for proxy in ["pathwatch", "envoy", "nginx"]:
                if (protocol, proxy) not in groups:
                    continue
                group = [
                    r
                    for r in runs
                    if (r["protocol"], r["rate"], r["proxy"]) == (protocol, rate, proxy)
                ]
                if any(r["duration_s"] != 120 for r in group):
                    raise ValueError("shortened matrix measurement")
                completed = sum(
                    r["counts"]["completed" if protocol == "h1" else "done"]
                    for r in group
                )
                row = {
                    "protocol": protocol,
                    "rate": rate,
                    "proxy": proxy,
                    "completed": completed,
                    "achieved_rps_mean": completed
                    / sum(r["measurement"]["reported_elapsed_s"] for r in group),
                    "cpu_us_per_completed": sum(r["cpu_pidstat_seconds"] for r in group)
                    * 1e6
                    / completed
                    if completed
                    else None,
                    "unfinished_at_end": sum(r["unfinished_at_end"] for r in group),
                    "round_statuses": [r["status"] for r in group],
                }
                if protocol == "h1":
                    row["errors"] = {
                        k: sum(r["counts"]["errors"][k] for r in group)
                        for k in ["http", "connect", "read", "write", "timeout"]
                    }
                else:
                    row["errors"] = {
                        k: sum(r["counts"][k] for r in group)
                        for k in ["failed", "errored", "timeout"]
                    }
                # A missing round stays undefined; never silently select good rounds.
                for q in ["p50", "p99", "p999"]:
                    if any(r.get("latency") is None for r in group):
                        row[q + "_us_bounds"] = None
                    elif protocol == "h1":
                        m = statistics.median(r["latency"][q + "_us"] for r in group)
                        row[q + "_us_bounds"] = [m, m]
                    else:
                        row[q + "_us_bounds"] = [
                            statistics.median(
                                r["latency"][q + "_us_bounds"][i] for r in group
                            )
                            for i in [0, 1]
                        ]
                rows.append(row)
    return rows


def summarize_diag(directory):
    """Per (variant, rate): medians of round quantiles, CPU and per-request counters.

    A diagnostic run (run.py --diag) is short (30 s cells) and is only compared
    within itself; it is never acceptance evidence.
    """
    import json
    import statistics
    from pathlib import Path

    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    runs = [json.loads(p.read_text()) for p in sorted(directory.glob("*/result.json"))]
    if not runs or manifest.get("completed_cells") != manifest.get("planned_cells") or len(runs) != manifest["planned_cells"]:
        raise ValueError("incomplete diagnostic run")
    if any(r.get("status") != "measured" or not r.get("cpu_accounting_valid") for r in runs):
        raise ValueError("diagnostic cell not measured")
    rows = []
    for name, rate in sorted({(r["variant"], r["rate"]) for r in runs}, key=lambda k: (k[1], k[0])):
        group = [r for r in runs if (r["variant"], r["rate"]) == (name, rate)]
        done = sum(r["counts"]["done"] for r in group)
        loads = []
        for r in group:
            monitor = directory / f"h2-r{r['round']}-{rate}-{name}" / "monitor.json"
            loads += [float(s["loadavg"].split()[0]) for s in json.loads(monitor.read_text())]
        rows.append(
            {
                "variant": name,
                "rate": rate,
                "rounds": len(group),
                "completed": done,
                "errors": sum(r["counts"][k] for r in group for k in ["failed", "errored", "timeout"]),
                "cpu_us_per_completed": sum(r["cpu_pidstat_seconds"] for r in group) * 1e6 / done,
                "per_completed": {
                    k: statistics.median(r["proxy_per_completed"][k] for r in group)
                    for k in ["syscr", "syscw", "ctx_switches"]
                },
                "loadavg_1m_median": statistics.median(loads) if loads else None,
                **{
                    q + "_us_bounds": [
                        statistics.median(r["latency"][q + "_us_bounds"][i] for r in group)
                        for i in [0, 1]
                    ]
                    for q in ["p50", "p99"]
                },
            }
        )
    return rows


def main():
    import argparse, json
    from pathlib import Path

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("matrix", type=Path, nargs="+", help="one run, or the --rounds chunks of one matrix")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--diag", action="store_true", help="summarize one run.py --diag directory")
    a = p.parse_args()
    if a.diag:
        rows = summarize_diag(*a.matrix)
        a.output.write_text(json.dumps(rows, indent=2) + "\n")
        print("|RPS|变体|轮数|p50 ms|p99 ms|CPU us/完成|read 调用/完成|write 调用/完成|上下文切换/完成|错误|loadavg 1m 中位数|\n|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for r in rows:
            c = r["per_completed"]
            print(
                f"|{r['rate']}|{r['variant']}|{r['rounds']}|{r['p50_us_bounds'][0] / 1000:.3f}–{r['p50_us_bounds'][1] / 1000:.3f}|{r['p99_us_bounds'][0] / 1000:.3f}–{r['p99_us_bounds'][1] / 1000:.3f}|{r['cpu_us_per_completed']:.1f}|{c['syscr']:.2f}|{c['syscw']:.2f}|{c['ctx_switches']:.2f}|{r['errors']}|{r['loadavg_1m_median']:.1f}|"
            )
        return
    rows = summarize_matrix(*a.matrix)
    a.output.write_text(json.dumps(rows, indent=2) + "\n")
    print(
        "延迟为五轮分位数的中位数（不是合并所有样本的分位数），单位ms；H2保留量化上下界。CPU为五轮总CPU秒/总完成数，单位us/请求。错误各类别可能重叠，不能直接相加。"
    )
    print(
        "\n|协议|RPS|代理|实际RPS|p50 ms|p99 ms|p99.9 ms|CPU us/完成|错误类别|期末未完成|\n|---|---:|---|---:|---:|---:|---:|---:|---|---:|"
    )
    for r in rows:

        def latency(q):
            v = r[q + "_us_bounds"]
            if v is None:
                return "未定义"
            return (
                f"{v[0] / 1000:.3f}"
                if v[0] == v[1]
                else f"{v[0] / 1000:.3f}–{v[1] / 1000:.3f}"
            )

        cpu = (
            "未定义"
            if r["cpu_us_per_completed"] is None
            else f"{r['cpu_us_per_completed']:.2f}"
        )
        errors = ", ".join(f"{k}={v}" for k, v in r["errors"].items())
        print(
            f"|{r['protocol']}|{r['rate']}|{r['proxy']}|{r['achieved_rps_mean']:.2f}|{latency('p50')}|{latency('p99')}|{latency('p999')}|{cpu}|{errors}|{r['unfinished_at_end']}|"
        )


if __name__ == "__main__":
    main()
