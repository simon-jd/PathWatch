package anomaly

import (
	"math"
	"math/rand"
	"testing"
	"time"
)

// Catches removing accumulation, upper-only reset, or sigma normalization.
func TestCUSUM_DetectsStep(t *testing.T) {
	for _, h := range []float64{5, 12} {
		c := CUSUM{K: .5, H: h}
		for i := 0; i < 20; i++ {
			if c.Update((50 - 50) / 5) {
				t.Fatal("baseline alarm")
			}
		}
		alarm := false
		for i := 0; i < 3; i++ {
			alarm = c.Update((80-50)/5) || alarm
		}
		if !alarm {
			t.Fatalf("H=%g: no alarm within 3 windows", h)
		}
	}
}
func TestCUSUM_H5NoiseDiagnostic(t *testing.T) {
	for _, seed := range []int64{1, 42, 20261024} {
		r := rand.New(rand.NewSource(seed))
		c := CUSUM{K: .5, H: 5}
		alarms := 0
		for i := 0; i < 10000; i++ {
			if c.Update(((50 + 5*r.NormFloat64()) - 50) / 5) {
				alarms++
			}
		}
		t.Logf("predeclared seed=%d H5 alarms=%d/10000", seed, alarms)
		if alarms == 0 {
			t.Fatal("H5 diagnostic unexpectedly has no alarms; investigate, never select another seed")
		}
	}
}
func TestCUSUM_NoAlarmOnNoise_H12FixedFixture(t *testing.T) {
	for _, seed := range []int64{1, 42, 20261024} {
		r := rand.New(rand.NewSource(seed))
		c := CUSUM{K: .5, H: 12}
		for i := 0; i < 10000; i++ {
			if c.Update(r.NormFloat64()) {
				t.Fatalf("fixed seed=%d H12 alarm at %d", seed, i)
			}
		}
	}
}
func TestCUSUM_UpperOnlyAndInvalid(t *testing.T) {
	c := CUSUM{K: .5, H: 5}
	for _, v := range []float64{-10, -10, math.NaN(), math.Inf(1), 3, 3} {
		if c.Update(v) {
			t.Fatalf("unexpected alarm %g", v)
		}
	}
	if !c.Update(1) {
		t.Fatal("lost upper accumulation")
	}
	if c.Update(0) {
		t.Fatal("alarm did not reset")
	}
}

// Rule (decided before the 2026-10-31 measurement): after an alarm the detector
// latches. Sum keeps accumulating, clamped at H, with no new alarm, and re-arms
// only when Sum returns to 0. A sustained shift therefore alarms once.
func TestCUSUM_SustainedShiftAlarmsOnceThenRearms(t *testing.T) {
	c := CUSUM{K: .5, H: 12}
	alarms, first := 0, 0
	for i := 1; i <= 100; i++ { // z=3: a 3 ms shift over the 1 ms sigma floor
		if c.Update(3) {
			alarms++
			if first == 0 {
				first = i
			}
		}
		if c.Sum > c.H {
			t.Fatalf("window %d: Sum %g above H", i, c.Sum)
		}
	}
	if alarms != 1 || first != 5 || !c.Latched {
		t.Fatalf("alarms=%d first=%d latched=%v; want one alarm at window 5, latched", alarms, first, c.Latched)
	}
	// Back at baseline: H/K = 24 windows bring Sum to 0 and re-arm, silently.
	for i := 1; i <= 24; i++ {
		if c.Update(0) {
			t.Fatal("alarm while recovering")
		}
		if (i < 24) != c.Latched {
			t.Fatalf("baseline window %d: latched=%v", i, c.Latched)
		}
	}
	// A new episode alarms again.
	alarm := false
	for i := 0; i < 5; i++ {
		alarm = c.Update(3) || alarm
	}
	if !alarm {
		t.Fatal("re-armed detector missed the next shift")
	}
}
func TestCorrelate_HopChange(t *testing.T) {
	at := time.Unix(1800000000, 0)
	before := []Hop{}
	after := []Hop{}
	for i := 1; i <= 5; i++ {
		before = append(before, Hop{TTL: i, Addr: "192.0.2.1", At: at.Add(-time.Minute)})
		after = append(after, Hop{TTL: i, Addr: "192.0.2.1", At: at.Add(time.Minute)})
	}
	after[3].Addr = "192.0.2.4"
	// Input order is irrelevant: compare TTL keys.
	after[0], after[4] = after[4], after[0]
	got := Correlate(at, before, after)
	if !got.Valid || !got.Changed || got.FirstDivergentTTL != 4 {
		t.Fatal(got)
	}
}
func TestCorrelate_UnknownInvalidAndTemporal(t *testing.T) {
	at := time.Unix(1800000000, 0)
	b := []Hop{{1, "2001:db8::1", at.Add(-time.Minute)}, {2, "", at.Add(-time.Minute)}}
	a := []Hop{{1, "2001:0db8:0:0::1", at.Add(time.Minute)}, {2, "2001:db8::2", at.Add(time.Minute)}}
	if got := Correlate(at, b, a); !got.Valid || got.Changed {
		t.Fatal(got)
	}
	for _, bad := range [][]Hop{{a[0], a[0]}, {{3, "2001:db8::1", at.Add(time.Minute)}}, {{1, "192.0.2.1", at.Add(time.Minute)}}} {
		if got := Correlate(at, b, bad); got.Valid || got.Changed {
			t.Fatal(got)
		}
	}
	a[0].At = at.Add(-2 * time.Minute)
	if got := Correlate(at, b, a); got.Valid || got.Changed {
		t.Fatal("prior after snapshot", got)
	}
	b[0].At = at.Add(time.Minute)
	if got := Correlate(at, b, a); got.Valid || got.Changed {
		t.Fatal("future before snapshot", got)
	}
}
