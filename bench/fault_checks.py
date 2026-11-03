#!/usr/bin/env python3
"""Opt-in narrow actual fault checks; outputs are failures, never matrix cells."""

import argparse
import json
from pathlib import Path
import signal
import sys
import time
import run


def child(mode, directory):
    directory.mkdir()
    backend = run.start(
        [run.TOOLS["echo"]], directory / "backend.txt", run.AFFINITY["backend"]
    )
    try:
        run.port_ready(19090, backend)
        run.port_ready(19091, backend)
        warmup = 15 if "warmup" in mode else 2
        run.one(
            "nginx",
            "h2" if mode == "normal-h2-warmup" else "h1",
            1000,
            0,
            20,
            warmup,
            directory,
        )
    finally:
        errors = run.cleanup()
        run.dump(
            directory / "cleanup.json",
            {
                "errors": errors,
                "owned_processes": [p.evidence() for p in run.OWNED],
                "live_pids": sorted({pid for p in run.OWNED for pid in p.snapshot()}),
                "closed_listeners": run.closed_listeners(),
            },
        )


def read_result(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def parent(directory, modes=None):
    directory.mkdir(parents=True)
    proofs = []
    for mode in modes or [
        "short-measurement",
        "short-warmup",
        "failed-warmup",
        "replace-worker",
    ]:
        if not all(run.closed_listeners().values()):
            raise RuntimeError("test ports occupied")
        out = directory / mode
        runner = run.start(
            [sys.executable, __file__, "--child", mode, "--output", out],
            directory / (mode + "-console.txt"),
            run.AFFINITY["coordinator"],
        )
        result = out / "h1-r0-1000-nginx/result.json"
        try:
            key = "warmup_pid" if "warmup" in mode else "generator_pid"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                r = read_result(result)
                if r.get(key):
                    break
                if runner.poll() is not None:
                    raise RuntimeError("fixture exited before injection")
                time.sleep(0.05)
            else:
                raise RuntimeError("fixture failed to reach injection boundary")
            time.sleep(12 if mode == "short-measurement" else 1)
            targets = runner.snapshot()
            target = r["proxy_pids"][1] if mode == "replace-worker" else r[key]
            if target not in targets:
                raise RuntimeError("target identity not owned")
            before = targets[target]
            chosen = {
                "short-measurement": signal.SIGINT,
                "short-warmup": signal.SIGINT,
                "failed-warmup": signal.SIGTERM,
                "replace-worker": signal.SIGKILL,
            }[mode]
            runner._signal(before, chosen)
            replacement = None
            if mode == "replace-worker":
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    replacement = [
                        x
                        for x in runner.snapshot().values()
                        if x["ppid"] == r["proxy_pids"][0]
                        and x["pid"] not in r["proxy_pids"]
                    ]
                    if replacement:
                        break
                    time.sleep(0.01)
            runner.wait(timeout=15)
            final = read_result(result)
            cleanup = json.loads((out / "cleanup.json").read_text())
            if runner.returncode != 1 or final.get("status") != "measurement_failed":
                raise AssertionError(final)
            expected = (
                "CPU accounting invalid"
                if mode == "replace-worker"
                else ("warmup" if "warmup" in mode else "measurement")
            )
            if expected not in final.get("failure", ""):
                raise AssertionError(final)
            if (
                cleanup["errors"]
                or cleanup["live_pids"]
                or not all(cleanup["closed_listeners"].values())
            ):
                raise AssertionError(cleanup)
            if mode == "replace-worker" and not replacement:
                raise AssertionError("no actual replacement observed")
            proof = {
                "mode": mode,
                "target_identity": before,
                "signal": int(chosen),
                "returncode": runner.returncode,
                "failure": final["failure"],
                "measurement": final.get("measurement"),
                "warmup": final.get("warmup"),
                "replacement": replacement,
                "cleanup": cleanup,
            }
            proofs.append(proof)
            print(
                json.dumps({k: v for k, v in proof.items() if k != "cleanup"}),
                flush=True,
            )
        finally:
            run.stop(runner)
            run.dump(directory / "proofs.json", proofs)
    run.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--child",
        choices=[
            "short-measurement",
            "short-warmup",
            "failed-warmup",
            "replace-worker",
            "normal-h2-warmup",
        ],
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.child:
        child(args.child, args.output)
    else:
        parent(args.output)


if __name__ == "__main__":
    try:
        main()
    finally:
        run.cleanup()
