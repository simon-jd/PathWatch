import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run


class PlanTests(unittest.TestCase):
    def test_full_matrix(self):
        cells = run.plan(full=True, protocol="h2", proxy="nginx", rate=1000, matrix=False)
        self.assertEqual(len(cells), 120)
        self.assertEqual({c[0] for c in cells}, {"h1", "h2"})
        self.assertTrue(all(c[4:] == (120, 15) for c in cells))

    def test_smoke_single_cell(self):
        self.assertEqual(
            run.plan(full=False, protocol="h2", proxy="pathwatch", rate=5000, matrix=False),
            [("h2", 0, 5000, "pathwatch", 20, 2)],
        )

    def test_matrix_one_protocol_one_proxy(self):
        # Task 13 H2 rerun: 4 rates x 5 rounds at full duration, filters honoured.
        cells = run.plan(full=False, protocol="h2", proxy="pathwatch", rate=20000, matrix=True)
        self.assertEqual(len(cells), 20)
        self.assertEqual({c[0] for c in cells}, {"h2"})
        self.assertEqual({c[3] for c in cells}, {"pathwatch"})
        self.assertEqual(sorted({c[2] for c in cells}), [1000, 5000, 10000, 20000])
        self.assertEqual(sorted({c[1] for c in cells}), [0, 1, 2, 3, 4])
        self.assertTrue(all(c[4:] == (120, 15) for c in cells))

    def test_matrix_all_proxies_rotates_order(self):
        cells = run.plan(full=False, protocol="h2", proxy=None, rate=1000, matrix=True)
        self.assertEqual(len(cells), 60)
        self.assertEqual([c[3] for c in cells[:3]], ["pathwatch", "envoy", "nginx"])
        round1 = [c[3] for c in cells if c[1] == 1 and c[2] == 1000]
        self.assertEqual(round1, ["envoy", "nginx", "pathwatch"])

    def test_matrix_round_subset_keeps_each_rounds_rotation(self):
        # A protocol's 60 cells split into chunks that fit one background job.
        first = run.plan(full=False, protocol="h1", proxy=None, rate=1000, matrix=True, rounds=[0, 1, 2])
        rest = run.plan(full=False, protocol="h1", proxy=None, rate=1000, matrix=True, rounds=[3, 4])
        whole = run.plan(full=False, protocol="h1", proxy=None, rate=1000, matrix=True)
        self.assertEqual((len(first), len(rest)), (36, 24))
        self.assertEqual(first + rest, whole)


class DiagnosticPlanTests(unittest.TestCase):
    def test_variants_rotate_within_each_round(self):
        cells = run.plan_diag(["pathwatch", "pathwatch-w1", "nginx"], [1000, 10000], 3)
        self.assertEqual(len(cells), 18)
        self.assertTrue(all(c[0] == "h2" and c[4:] == (30, 5) for c in cells))
        first = [c[3] for c in cells if c[1] == 0 and c[2] == 1000]
        second = [c[3] for c in cells if c[1] == 1 and c[2] == 1000]
        self.assertEqual(first, ["pathwatch", "pathwatch-w1", "nginx"])
        self.assertEqual(second, ["pathwatch-w1", "nginx", "pathwatch"])

    def test_variant_resolves_to_proxy_environment_and_config(self):
        self.assertEqual(run.variant("nginx"), ("nginx", {}, None))
        self.assertEqual(
            run.variant("pathwatch-w1"), ("pathwatch", {"TOKIO_WORKER_THREADS": "1"}, None)
        )
        self.assertEqual(
            run.variant("pathwatch-percore"),
            ("pathwatch", {}, "bench/pathwatch-proxy-percore.toml"),
        )
        with self.assertRaises(KeyError):
            run.variant("unknown")

    def test_percore_config_differs_only_in_runtime(self):
        # The prototype must be the default bench config plus a [runtime] section.
        root = Path(__file__).resolve().parents[2]
        base = (root / "bench/pathwatch-proxy.toml").read_text()
        percore = (root / "bench/pathwatch-proxy-percore.toml").read_text()
        self.assertTrue(percore.startswith(base))
        self.assertEqual(
            percore[len(base):].strip().splitlines(),
            ["[runtime]", 'mode = "per_core"', "workers = 2"],
        )

    def test_matrix_accepts_a_variant_as_the_proxy(self):
        cells = run.plan(full=False, protocol="h2", proxy="pathwatch-percore", rate=1000, matrix=True)
        self.assertEqual(len(cells), 20)
        self.assertEqual({c[3] for c in cells}, {"pathwatch-percore"})


class ProcCountersTests(unittest.TestCase):
    def test_snapshot_has_syscall_and_context_switch_counters(self):
        import os
        snap = run.proc_snapshot([os.getpid()])[str(os.getpid())]
        for key in ("syscr", "syscw", "ctx_switches"):
            self.assertIsInstance(snap[key], int)
        os.write(os.open(os.devnull, os.O_WRONLY), b"x")
        later = run.proc_snapshot([os.getpid()])[str(os.getpid())]
        self.assertGreater(later["syscw"], snap["syscw"])

    def test_exiting_process_io_permission_error_is_a_disappearance(self):
        # An exiting process returns EACCES for /proc/<pid>/io (seen in the first diag run).
        import os
        from unittest import mock
        real = Path.read_text

        def read_text(self, *a, **k):
            if self.name == "io":
                raise PermissionError(13, "Permission denied")
            return real(self, *a, **k)

        with mock.patch.object(Path, "read_text", read_text):
            self.assertEqual(run.proc_snapshot([os.getpid()]), {})
            with self.assertRaises(RuntimeError):
                run.proc_snapshot([os.getpid()], strict=True)

    def test_per_completed_counters(self):
        before = {"1": {"syscr": 10, "syscw": 20, "ctx_switches": 5}}
        after = {"1": {"syscr": 110, "syscw": 220, "ctx_switches": 55}}
        self.assertEqual(
            run.per_completed(before, after, 100),
            {"syscr": 1.0, "syscw": 2.0, "ctx_switches": 0.5},
        )
        self.assertIsNone(run.per_completed(before, after, 0))


class DiskShareTests(unittest.TestCase):
    def test_soft_limit_is_doubled_share(self):
        # PathWatch disk share doubled to 12 GB on 2026-10-27.
        self.assertEqual(run.DISK_SHARE, 12_000_000_000)
        r = run.resources()
        self.assertEqual(r["soft_limit"], 12_000_000_000)
        self.assertGreater(r["total"], 0)

    def test_setup_stage_gate_tracks_share(self):
        setup = (Path(run.__file__).parent / "setup.sh").read_text()
        self.assertIn("allocated+reserve>=11600000000", setup)
        self.assertNotIn("5600000000", setup)


if __name__ == "__main__":
    unittest.main()
