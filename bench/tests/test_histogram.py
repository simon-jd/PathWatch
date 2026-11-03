import io, sys, unittest
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze import BINS, bin_index, bin_bounds, quantiles, parse_h2_counts, cpu_seconds
from collect import collect


class HistogramTests(unittest.TestCase):
    def test_bounds_cover_values_and_precision(self):
        for v in [
            0,
            1,
            1023,
            1024,
            2047,
            2048,
            2049,
            4095,
            4096,
            10000,
            120000000,
            (1 << 63) - 1,
        ]:
            lo, hi = bin_bounds(bin_index(v))
            self.assertLessEqual(lo, v)
            self.assertGreaterEqual(hi, v)
            if v < 2048:
                self.assertEqual((lo, hi), (v, v))
            else:
                self.assertLessEqual((hi - lo) / v, 1 / 1024)
        for v in [-1, 1 << 63]:
            with self.assertRaises(ValueError):
                bin_index(v)

    def test_collect_counts_errors_without_success_bias(self):
        hist, c = collect(
            io.StringIO(
                "1790000000000000\t200\t12\n1790000000000000\t-1\t2000\n1790000000000000\t503\t9000\n"
            )
        )
        self.assertEqual(
            (c["records"], c["success"], c["stream_failed"], c["http_failed"]),
            (3, 1, 1, 1),
        )
        self.assertEqual(sum(hist), 1)
        self.assertEqual(quantiles(hist)["p999_us_bounds"], [12, 12])

    def test_reject_bad_stream_records(self):
        for line in [
            "1\t200\t-1\n",
            "0\t200\t1\n",
            "1\t999\t1\n",
            "1\t200\n",
            "1\t200\t1\textra\n",
        ]:
            with self.subTest(line=line), self.assertRaises(ValueError):
                collect(io.StringIO(line))
        with self.assertRaises(ValueError):
            quantiles(array("Q", [0]) * BINS)

    def test_nearest_rank_quantiles(self):
        counts = array("Q", [0]) * BINS
        counts[10] = 500
        counts[20] = 490
        counts[30] = 9
        counts[40] = 1
        self.assertEqual(
            quantiles(counts),
            {
                "p50_us_bounds": [10, 10],
                "p99_us_bounds": [20, 20],
                "p999_us_bounds": [30, 30],
            },
        )

    def test_counts_and_cpu_include_workers(self):
        text = "requests: 100 total, 110 started, 100 done, 95 succeeded, 5 failed, 2 errored, 0 timeout"
        self.assertEqual(parse_h2_counts(text)["failed"], 5)
        cpu = "1790000000 1000 12 20 5 0 0 25 16 nginx\n1790000000 1000 13 30 5 0 0 35 17 nginx\n"
        self.assertAlmostEqual(cpu_seconds(cpu), 0.6)
        with self.assertRaises(ValueError):
            cpu_seconds("no samples")


if __name__ == "__main__":
    unittest.main()
