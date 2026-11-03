import json, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze import summarize_diag


def cell(directory, variant, rate, round_no, p50, syscw, load="4.00 3.00 2.00 1/100 1"):
    d = directory / f"h2-r{round_no}-{rate}-{variant}"
    d.mkdir()
    done = 1000
    result = {
        "proxy": variant.split("-")[0],
        "variant": variant,
        "protocol": "h2",
        "rate": rate,
        "round": round_no,
        "status": "measured",
        "cpu_accounting_valid": True,
        "counts": {"done": done, "succeeded": done, "failed": 0, "errored": 0, "timeout": 0},
        "measurement": {"reported_elapsed_s": 30.0},
        "cpu_pidstat_seconds": 0.05,
        "latency": {"p50_us_bounds": [p50, p50 + 10], "p99_us_bounds": [2 * p50, 2 * p50 + 10]},
        "proxy_per_completed": {"syscr": 1.0, "syscw": syscw, "ctx_switches": 2.0},
    }
    (d / "result.json").write_text(json.dumps(result))
    (d / "monitor.json").write_text(json.dumps([{"loadavg": load}]))


class DiagSummaryTests(unittest.TestCase):
    def test_medians_per_variant_and_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text(json.dumps({"planned_cells": 6, "completed_cells": 6}))
            for r, (a, b) in enumerate([(500, 100), (520, 110), (900, 120)]):
                cell(root, "pathwatch", 1000, r, a, 3.0)
                cell(root, "nginx", 1000, r, b, 1.0)
            rows = summarize_diag(root)
        by = {(x["variant"], x["rate"]): x for x in rows}
        self.assertEqual(by[("pathwatch", 1000)]["p50_us_bounds"], [520, 530])
        self.assertEqual(by[("nginx", 1000)]["p99_us_bounds"], [220, 230])
        self.assertEqual(by[("pathwatch", 1000)]["rounds"], 3)
        self.assertAlmostEqual(by[("pathwatch", 1000)]["cpu_us_per_completed"], 50.0)
        self.assertEqual(by[("pathwatch", 1000)]["per_completed"]["syscw"], 3.0)
        self.assertEqual(by[("nginx", 1000)]["loadavg_1m_median"], 4.0)

    def test_incomplete_run_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text(json.dumps({"planned_cells": 2, "completed_cells": 1}))
            cell(root, "nginx", 1000, 0, 100, 1.0)
            with self.assertRaises(ValueError):
                summarize_diag(root)


if __name__ == "__main__":
    unittest.main()
