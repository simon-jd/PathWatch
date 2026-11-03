"""Linux ownership of benchmark sessions, including adopted orphan descendants.

Signals use pidfds opened after checking PID/starttime, never a reused numeric PID
or an unverified process group. Subreaper status is local to this runner process.
"""

import ctypes
import os
from pathlib import Path
import signal
import time


def enable_subreaper():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot enable local child subreaper")


def process_table():
    result = {}
    for path in Path("/proc").glob("[0-9]*"):
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            pid = int(path.name)
            result[pid] = {
                "pid": pid,
                "state": fields[0],
                "ppid": int(fields[1]),
                "pgrp": int(fields[2]),
                "session": int(fields[3]),
                "starttime": int(fields[19]),
            }
        except (FileNotFoundError, ProcessLookupError):
            pass
    return result


def identity(record):
    return (record["pid"], record["starttime"], record["session"], record["pgrp"])


class OwnedProcess:
    def __init__(self, process):
        self.process = process
        self.pid = process.pid
        self.owner_pid = os.getpid()
        self.known = {}
        self.pidfds = {}
        self.retired = False
        self.signal_events = []
        table = process_table()
        leader = table.get(self.pid)
        if (
            leader is None
            or leader["session"] != self.pid
            or leader["pgrp"] != self.pid
        ):
            raise RuntimeError("owned process did not establish its new session")
        self.leader = dict(leader)
        self._remember(leader)
        self.snapshot()

    @property
    def returncode(self):
        return self.process.returncode

    def poll(self):
        return self.process.poll()

    def wait(self, timeout=None):
        return self.process.wait(timeout=timeout)

    def _remember(self, record):
        pid = record["pid"]
        old = self.known.get(pid)
        if old and old["starttime"] == record["starttime"]:
            return
        try:
            fd = os.pidfd_open(pid)
            current = process_table().get(pid)
            if current is None or identity(current) != identity(record):
                os.close(fd)
                return
        except ProcessLookupError:
            return
        if pid in self.pidfds:
            os.close(self.pidfds[pid])
        self.pidfds[pid] = fd
        self.known[pid] = dict(record)

    def snapshot(self):
        if self.retired:
            return {}
        self.poll()  # reap only the Popen leader, preserving its exit status
        table = process_table()
        owned = {
            pid
            for pid, saved in self.known.items()
            if pid in table and saved["starttime"] == table[pid]["starttime"]
        }
        # Known parent lineage establishes ownership even if a descendant moves
        # into another process group. Adopted same-session children are ours even
        # when the leader exited before we first observed the fork.
        changed = True
        while changed:
            changed = False
            for pid, record in table.items():
                if pid in owned or record["starttime"] < self.leader["starttime"]:
                    continue
                parent_owned = record["ppid"] in owned
                adopted = (
                    record["ppid"] == self.owner_pid
                    and record["session"] == self.leader["session"]
                    and pid != self.pid
                )
                if parent_owned or adopted:
                    self._remember(record)
                    if (
                        pid in self.known
                        and self.known[pid]["starttime"] == record["starttime"]
                    ):
                        owned.add(pid)
                        changed = True
        for pid in owned:
            if table[pid]["state"] == "Z" and pid != self.pid:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
        return {pid: table[pid] for pid in owned if table[pid]["state"] != "Z"}

    def require_same(self, expected):
        current = self.snapshot()
        if {identity(x) for x in current.values()} != {
            identity(x) for x in expected.values()
        }:
            raise RuntimeError(
                "proxy process membership changed; CPU accounting invalid: "
                f"expected={list(expected.values())}, actual={list(current.values())}"
            )
        return current

    def _signal(self, record, sig):
        # pidfd identifies the original task even if it exits between verification
        # and the signal; numeric PID reuse cannot redirect this operation.
        current = process_table().get(record["pid"])
        if current is None or identity(current) != identity(record):
            return
        try:
            signal.pidfd_send_signal(self.pidfds[record["pid"]], sig)
            self.signal_events.append({"identity": dict(record), "signal": int(sig)})
        except ProcessLookupError:
            pass

    def stop(self, sig=signal.SIGTERM):
        if self.retired:
            return
        for chosen, timeout in [(sig, 6), (signal.SIGKILL, 3)]:
            deadline = time.monotonic() + timeout
            signaled = set()
            while True:
                members = self.snapshot()
                if not members:
                    self.retired = True
                    for fd in self.pidfds.values():
                        os.close(fd)
                    self.pidfds.clear()
                    self.poll()
                    return
                for record in members.values():
                    key = identity(record)
                    if key not in signaled:
                        self._signal(record, chosen)
                        signaled.add(key)
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        raise RuntimeError(f"owned descendants survived cleanup: {self.snapshot()}")

    def evidence(self):
        return {
            "leader": self.leader,
            "observed_identities": list(self.known.values()),
            "live_descendants": list(self.snapshot().values()),
            "returncode": self.returncode,
            "retired": self.retired,
            "signals": self.signal_events,
        }
