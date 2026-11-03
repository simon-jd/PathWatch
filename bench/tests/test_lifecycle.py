"""Actual owned Linux process/FIFO lifecycle checks on private ephemeral ports."""

import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run


def await_file(path):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.01)
    raise AssertionError("fixture did not publish identity")


class LifecycleTests(unittest.TestCase):
    def tearDown(self):
        self.assertEqual(run.cleanup(), [])

    def test_exited_leader_surviving_listener_is_cleaned(self):
        with tempfile.TemporaryDirectory(prefix="task13-lifecycle-") as folder:
            directory = Path(folder)
            info = directory / "child.json"
            code = """import os,socket,time,json,sys
pid=os.fork()
if pid:
    os._exit(0)
s=socket.socket();s.bind(('127.0.0.1',0));s.listen()
open(sys.argv[1],'w').write(json.dumps({'pid':os.getpid(),'port':s.getsockname()[1]}))
time.sleep(30)
"""
            leader = run.start(
                [sys.executable, "-c", code, info],
                directory / "stdout",
                run.AFFINITY["coordinator"],
            )
            child = await_file(info)
            leader.wait(timeout=5)
            with socket.create_connection(("127.0.0.1", child["port"]), 1):
                pass
            before = leader.snapshot()
            self.assertIn(child["pid"], before)
            self.assertNotIn(leader.pid, before)
            started = time.monotonic()
            run.stop(leader)
            self.assertEqual(leader.snapshot(), {})
            with self.assertRaises(ConnectionRefusedError):
                socket.create_connection(("127.0.0.1", child["port"]), 0.2)
            print(
                "ORPHAN_PROOF "
                + json.dumps(
                    {
                        "leader": leader.leader,
                        "child": child,
                        "before": before,
                        "cleanup_seconds": time.monotonic() - started,
                        "after": leader.evidence(),
                    }
                ),
                flush=True,
            )

    def test_worker_replacement_invalidates_process_set(self):
        with tempfile.TemporaryDirectory(prefix="task13-replacement-") as folder:
            directory = Path(folder)
            info = directory / "child.json"
            code = """import subprocess,sys,time,json,signal,os
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
def replace(*args):
    global child
    child.terminate();child.wait()
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    open(sys.argv[1],'w').write(json.dumps({'pid':child.pid,'replacement':True}))
signal.signal(signal.SIGUSR1,replace)
open(sys.argv[1],'w').write(json.dumps({'pid':child.pid,'replacement':False}))
time.sleep(30)
"""
            leader = run.start(
                [sys.executable, "-c", code, info],
                directory / "stdout",
                run.AFFINITY["coordinator"],
            )
            first = await_file(info)
            before = leader.snapshot()
            self.assertIn(first["pid"], before)
            leader._signal(before[leader.pid], signal.SIGUSR1)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                second = await_file(info)
                if second["replacement"]:
                    break
                time.sleep(0.01)
            self.assertNotEqual(first["pid"], second["pid"])
            with self.assertRaisesRegex(RuntimeError, "CPU accounting invalid"):
                leader.require_same(before)
            print(
                "REPLACEMENT_PROOF "
                + json.dumps({"before": before, "after": leader.snapshot()}),
                flush=True,
            )

    def test_successful_fifo_eof_can_precede_generator_exit(self):
        with tempfile.TemporaryDirectory(prefix="task13-eof-") as folder:
            directory = Path(folder)
            fifo = directory / "fifo"
            os.mkfifo(fifo)
            collector = run.start(
                [sys.executable, "bench/collect.py", fifo, directory / "h2"],
                directory / "collector",
                run.AFFINITY["collector"],
            )
            code = "import sys,time;f=open(sys.argv[1],'w');f.write('1790000000000000\\t200\\t12\\n');f.close();time.sleep(1)"
            load = run.start(
                [sys.executable, "-c", code, fifo],
                directory / "load",
                run.AFFINITY["generator"],
            )
            self.assertEqual(run.wait_load(load, collector, 4, directory, []), 0)
            collector.wait(timeout=2)
            self.assertEqual(
                json.loads((directory / "h2.json").read_text())["records"], 1
            )
            self.assertTrue(
                any(
                    x["collector_eof_pending"]
                    for x in json.loads((directory / "monitor.json").read_text())
                )
            )

    def test_stale_identity_does_not_signal_live_process(self):
        with tempfile.TemporaryDirectory(prefix="task13-identity-") as folder:
            leader = run.start(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                Path(folder) / "stdout",
                run.AFFINITY["coordinator"],
            )
            stale = dict(leader.leader)
            stale["starttime"] -= 1
            leader._signal(stale, signal.SIGKILL)
            self.assertIsNone(leader.poll())
            self.assertEqual(leader.signal_events, [])
