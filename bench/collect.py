"""Bounded synchronous FIFO collector. No samples are dropped or clipped."""

import argparse, json, sys, time
from array import array
from pathlib import Path
from analyze import BINS, bin_index, quantiles


def collect(lines):
    hist = array("Q", [0]) * BINS
    counts = {
        "records": 0,
        "success": 0,
        "http_failed": 0,
        "stream_failed": 0,
        "statuses": {},
        "max_completion_lag_us": 0,
    }
    for line in lines:
        fields = line.rstrip("\n").split("\t")
        if len(fields) != 3:
            raise ValueError("malformed h2load record")
        start, status, latency = map(int, fields)
        if start <= 0 or not (status == -1 or 100 <= status < 600):
            raise ValueError("invalid time/status")
        index = bin_index(latency)
        counts["records"] += 1
        counts["statuses"][str(status)] = counts["statuses"].get(str(status), 0) + 1
        counts["max_completion_lag_us"] = max(
            counts["max_completion_lag_us"], time.time_ns() // 1000 - start - latency
        )
        if 200 <= status < 400:
            hist[index] += 1
            counts["success"] += 1
        elif status == -1:
            counts["stream_failed"] += 1
        else:
            counts["http_failed"] += 1
    return hist, counts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("fifo")
    p.add_argument("output")
    a = p.parse_args()
    started = time.monotonic()
    with open(a.fifo) as f:
        hist, counts = collect(f)
    counts.update(
        {
            "format": "65536 little-endian uint64 counts, success responses only",
            "units": "us",
            "collector_elapsed_s": time.monotonic() - started,
            "quantiles": quantiles(hist) if counts["success"] else None,
        }
    )
    if sys.byteorder != "little":
        hist.byteswap()
    Path(a.output + ".hist").write_bytes(hist.tobytes())
    Path(a.output + ".json").write_text(json.dumps(counts, indent=2) + "\n")


if __name__ == "__main__":
    main()
