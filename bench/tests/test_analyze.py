import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze import parse_wrk2_hdr


class ParseTests(unittest.TestCase):
    def test_parse_wrk2_hdr(self):
        fixture = Path(__file__).with_name("wrk2-hdr.txt").read_text()
        self.assertEqual(
            parse_wrk2_hdr(fixture),
            {"p50_us": 6670.0, "p99_us": 11180.0, "p999_us": 12300.0},
        )

    def test_reject_missing_malformed_and_empty(self):
        for text in [
            "",
            "Latency Distribution (HdrHistogram - Recorded Latency)\n 50.000% 0ms",
            "50.000% 2ms\n99.000% 3ms\n99.900% 4ms",
        ]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_wrk2_hdr(text)


if __name__ == "__main__":
    unittest.main()


class FullMatrixGateTests(unittest.TestCase):
    def test_reject_smoke_as_full_matrix(self):
        import json, tempfile
        from analyze import summarize_matrix

        with tempfile.TemporaryDirectory(prefix="pathwatch-task13-") as d:
            Path(d, "manifest.json").write_text(
                json.dumps({"full": False, "completed_cells": 6})
            )
            with self.assertRaises(ValueError):
                summarize_matrix(d)
