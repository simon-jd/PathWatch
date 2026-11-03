"""Synthetic UNIT fixtures only; never evidence of an actual benchmark."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze import summarize_matrix


class AcceptanceTests(unittest.TestCase):
    def fixture(self, directory, protocols=("h1", "h2"), proxies=("pathwatch", "envoy", "nginx"), rounds=range(5)):
        cells = len(protocols) * len(proxies) * 4 * len(rounds)
        full = len(protocols) == 2 and len(proxies) == 3 and len(rounds) == 5
        manifest = {
            "full": full,
            "planned_cells": cells,
            "completed_cells": cells,
            "actual_elapsed_s": 150 * cells,
            "measurement_wall_elapsed_s": 120.25 * cells,
            "measurement_reported_elapsed_s": 120 * cells,
            "input_hashes_unchanged": True,
            "cleanup_live_pids": [],
            "cleanup_listeners": dict.fromkeys(
                ["18443", "19090", "19091", "19092"], True
            ),
            "cleanup_errors": [],
        }
        Path(directory, "manifest.json").write_text(json.dumps(manifest))
        rows = []
        for protocol in protocols:
            for proxy in proxies:
                for rate in [1000, 5000, 10000, 20000]:
                    for round_no in rounds:
                        r = {
                            "protocol": protocol,
                            "proxy": proxy,
                            "rate": rate,
                            "round": round_no,
                            "duration_s": 120,
                            "warmup_s": 15,
                            "status": "measured",
                            "cpu_accounting_valid": True,
                            "measurement": {
                                "configured_s": 120,
                                "wall_elapsed_s": 120.25,
                                "reported_elapsed_s": 120,
                                "returncode": 0,
                                "timer_threads": [0, 1, 2, 3],
                            },
                            "warmup": {
                                "configured_s": 15,
                                "wall_elapsed_s": 15.25,
                                "reported_elapsed_s": 15,
                                "returncode": 0,
                                "timer_threads": [0, 1, 2, 3],
                            },
                            "counts": {
                                "completed": 1,
                                "done": 1,
                                "errors": dict.fromkeys(
                                    ["http", "connect", "read", "write", "timeout"], 0
                                ),
                                "failed": 0,
                                "errored": 0,
                                "timeout": 0,
                            },
                            "cpu_pidstat_seconds": 1,
                            "unfinished_at_end": 0,
                            "latency": None,
                        }
                        d = Path(directory, f"{protocol}-{proxy}-{rate}-{round_no}")
                        d.mkdir()
                        p = d / "result.json"
                        p.write_text(json.dumps(r))
                        rows.append(p)
        return rows

    def test_short_cell_rejected_despite_long_aggregate(self):
        with tempfile.TemporaryDirectory(prefix="task13-unit-") as d:
            rows = self.fixture(d)
            r = json.loads(rows[0].read_text())
            r["measurement"]["reported_elapsed_s"] = 12
            r["measurement"]["wall_elapsed_s"] = 12.25
            rows[0].write_text(json.dumps(r))
            with self.assertRaisesRegex(ValueError, "duration"):
                summarize_matrix(d)

    def test_short_or_failed_warmup_rejected(self):
        for change in [
            {"reported_elapsed_s": 2, "wall_elapsed_s": 2.25},
            {"returncode": 1},
        ]:
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory(prefix="task13-unit-") as d,
            ):
                rows = self.fixture(d)
                r = json.loads(rows[0].read_text())
                r["warmup"].update(change)
                rows[0].write_text(json.dumps(r))
                with self.assertRaisesRegex(ValueError, "warmup"):
                    summarize_matrix(d)

    def test_replaced_worker_accounting_rejected(self):
        with tempfile.TemporaryDirectory(prefix="task13-unit-") as d:
            rows = self.fixture(d)
            r = json.loads(rows[0].read_text())
            r["cpu_accounting_valid"] = False
            rows[0].write_text(json.dumps(r))
            with self.assertRaisesRegex(ValueError, "CPU"):
                summarize_matrix(d)

    def test_complete_unit_fixture_is_accepted(self):
        with tempfile.TemporaryDirectory(prefix="task13-unit-") as d:
            self.fixture(d)
            self.assertEqual(len(summarize_matrix(d)), 24)

    def test_single_protocol_proxy_matrix_is_accepted(self):
        with tempfile.TemporaryDirectory(prefix="task13-unit-") as d:
            self.fixture(d, protocols=("h2",), proxies=("pathwatch",))
            rows = summarize_matrix(d)
            self.assertEqual([(r["protocol"], r["proxy"]) for r in rows], [("h2", "pathwatch")] * 4)
            self.assertEqual([r["rate"] for r in rows], [1000, 5000, 10000, 20000])

    def test_single_matrix_missing_round_rejected(self):
        with tempfile.TemporaryDirectory(prefix="task13-unit-") as d:
            rows = self.fixture(d, protocols=("h2",), proxies=("pathwatch",))
            rows[-1].unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                summarize_matrix(d)

    def test_round_chunks_merge_into_one_matrix(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self.fixture(a, protocols=("h1",), rounds=(0, 1, 2))
            self.fixture(b, protocols=("h1",), rounds=(3, 4))
            with self.assertRaisesRegex(ValueError, "missing"):
                summarize_matrix(a)
            rows = summarize_matrix(a, b)
            self.assertEqual(len(rows), 12)
            self.assertTrue(all(r["completed"] == 5 for r in rows))

    def test_overlapping_chunks_rejected(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self.fixture(a, protocols=("h1",), rounds=(0, 1, 2))
            self.fixture(b, protocols=("h1",), rounds=(2, 3, 4))
            with self.assertRaisesRegex(ValueError, "missing or duplicated"):
                summarize_matrix(a, b)

    def test_actual_measurement_sum_cannot_borrow_warmup_time(self):
        with tempfile.TemporaryDirectory(prefix="task13-unit-") as d:
            rows = self.fixture(d)
            for p in rows:
                r = json.loads(p.read_text())
                r["measurement"]["wall_elapsed_s"] = 119.96
                p.write_text(json.dumps(r))
            with self.assertRaisesRegex(ValueError, "actual measured duration"):
                summarize_matrix(d)

    def test_pinned_duration_formats_and_native_h2_completion(self):
        from analyze import duration_evidence, validate_duration

        h1 = duration_evidence("h1", "BENCH_WRK_DURATION_US 119999000\n", 120, 120.1, 0)
        self.assertEqual(h1["reported_elapsed_s"], 119.999)
        validate_duration("h1", h1, 120, "measurement")
        text = "finished in 120.00s, 1000 req/s\n" + "\n".join(
            f"Main benchmark duration is over for thread #{i}." for i in range(4)
        )
        h2 = duration_evidence("h2", text, 120, 120.2, 0)
        validate_duration("h2", h2, 120, "measurement")
        h2["timer_threads"].pop()
        with self.assertRaisesRegex(ValueError, "timer completion"):
            validate_duration("h2", h2, 120, "measurement")
