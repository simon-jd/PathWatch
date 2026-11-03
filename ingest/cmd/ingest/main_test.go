package main

import (
	"math/rand"
	"testing"

	"pathwatch.local/pathwatch/ingest/internal/anomaly"
)

// The default H must give no alarm on 10,000 N(0,1) points for the seeds the
// anomaly tests predeclared, and still catch a 6σ step within 3 windows
// (TestCUSUM_ThresholdExperiment; docs/evidence/README.md).
func TestDefaultAnomalyH(t *testing.T) {
	for _, seed := range []int64{1, 42, 20261024} {
		r := rand.New(rand.NewSource(seed))
		c := anomaly.CUSUM{K: .5, H: defaultAnomalyH}
		for i := 0; i < 10000; i++ {
			if c.Update(r.NormFloat64()) {
				t.Fatalf("seed %d: alarm at point %d with default H=%g", seed, i, defaultAnomalyH)
			}
		}
	}
	c := anomaly.CUSUM{K: .5, H: defaultAnomalyH}
	for i := 1; ; i++ {
		if c.Update(6) {
			if i > 3 {
				t.Fatalf("6σ step needed %d windows", i)
			}
			break
		}
	}
}

func TestModeRejectsOwnerOptionsBeforeOpen(t *testing.T) {
	for _, n := range []string{"journal", "database", "clickhouse-url", "anomaly-enable", "core-listen", "core-frontends"} {
		if e := validateMode("frontend", map[string]bool{n: true}, ""); e == nil {
			t.Fatal(n)
		}
	}
	if validateMode("frontend", nil, "credential") == nil {
		t.Fatal("credential accepted")
	}
	for _, n := range []string{"listen", "tls-cert", "development-cleartext", "query-listen", "core-target"} {
		if validateMode("core", map[string]bool{n: true}, "") == nil {
			t.Fatal(n)
		}
	}
	if validateMode("embedded", map[string]bool{"core-ca": true}, "") == nil {
		t.Fatal("mixed modes")
	}
	for _, m := range []string{"embedded", "frontend", "core"} {
		if e := validateMode(m, nil, ""); e != nil {
			t.Fatal(e)
		}
	}
}
