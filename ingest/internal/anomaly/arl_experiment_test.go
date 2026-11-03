package anomaly

import (
	"fmt"
	"math"
	"math/rand"
	"os"
	"sort"
	"testing"
)

// TestCUSUM_ThresholdExperiment measures the real CUSUM's false alarms and
// detection delay for a grid of H (K=0.5). It only runs when
// PATHWATCH_ARL_EXPERIMENT=1 and prints a TSV table.
//
// Decision rule, fixed before the run: adopt the smallest H in the grid whose
// P(no alarm in 10,000 N(0,1) points, known baseline) >= 0.95, provided a 6σ
// step is still detected within 3 windows on average.
func TestCUSUM_ThresholdExperiment(t *testing.T) {
	if os.Getenv("PATHWATCH_ARL_EXPERIMENT") != "1" {
		t.Skip("set PATHWATCH_ARL_EXPERIMENT=1")
	}
	const k, points, trials, delayTrials, warmup = 0.5, 10000, 2000, 5000, 20
	r := rand.New(rand.NewSource(20261027))
	fmt.Printf("seed\t20261027\tK\t%g\tpoints\t%d\ttrials\t%d\twarmup\t%d\n", k, points, trials, warmup)
	fmt.Println("H\tsiegmund_arl0\tp_zero_known\tmean_alarms_known\tp_zero_warmup20\tmean_alarms_warmup20\tdelay_1sigma\tdelay_2sigma\tdelay_3sigma\tdelay_6sigma")
	for _, h := range []float64{4, 5, 6, 8, 10, 12, 15} {
		zeroKnown, alarmsKnown := noise(r, k, h, points, trials, 0)
		zeroWarm, alarmsWarm := noise(r, k, h, points, trials, warmup)
		fmt.Printf("%g\t%.0f\t%.4f\t%.3f\t%.4f\t%.3f", h, siegmund(k, h), zeroKnown, alarmsKnown, zeroWarm, alarmsWarm)
		for _, shift := range []float64{1, 2, 3, 6} {
			fmt.Printf("\t%.2f", delay(r, k, h, shift, delayTrials))
		}
		fmt.Println()
	}
}

// noise runs trials of `points` N(0,1) observations. With warmup > 0 the
// baseline mean and sigma are estimated from the first `warmup` points and then
// fixed, as Job does; those points are not scored.
func noise(r *rand.Rand, k, h float64, points, trials, warmup int) (pZero, meanAlarms float64) {
	zero, total := 0, 0
	for i := 0; i < trials; i++ {
		mean, sigma := 0.0, 1.0
		if warmup > 0 {
			var m, m2 float64
			for n := 1; n <= warmup; n++ {
				x := r.NormFloat64()
				d := x - m
				m += d / float64(n)
				m2 += d * (x - m)
			}
			mean, sigma = m, math.Sqrt(m2/float64(warmup-1))
		}
		c, alarms := CUSUM{K: k, H: h}, 0
		for n := 0; n < points; n++ {
			if c.Update((r.NormFloat64() - mean) / sigma) {
				alarms++
			}
		}
		if alarms == 0 {
			zero++
		}
		total += alarms
	}
	return float64(zero) / float64(trials), float64(total) / float64(trials)
}

// delay is the mean number of windows until alarm after a step of `shift` σ, from Sum=0.
func delay(r *rand.Rand, k, h, shift float64, trials int) float64 {
	total := 0
	for i := 0; i < trials; i++ {
		c := CUSUM{K: k, H: h}
		n := 1
		for !c.Update(r.NormFloat64() + shift) {
			n++
		}
		total += n
	}
	return float64(total) / float64(trials)
}

// The 21 TCP connect p95s (ms) of the Task 9 lab's 5-minute windows before the
// route change, 13:15–14:55Z on 2026-10-28 (docs/evidence/task-09-replay/replay.json).
var labBaselineP95 = []float64{0.42, 0.53, 0.337, 0.407, 0.28, 0.274, 0.36, 0.305, 0.533, 0.444, 0.533,
	0.299, 0.281, 0.275, 0.304, 0.413, 0.34, 0.273, 0.26, 0.297, 0.324}

// resetCUSUM is the rule before 2026-10-31 (reset Sum to 0 on alarm), kept only
// to count repeated alarms next to the latching rule.
func resetCUSUM(c *CUSUM, z float64) bool {
	c.Sum = math.Max(0, c.Sum+z-c.K)
	if c.Sum > c.H {
		c.Sum = 0
		return true
	}
	return false
}

// TestCUSUM_SmallShiftExperiment measures windows-to-alarm for step shifts in
// milliseconds with the ingest defaults (warmup 20, sigma floor 1 ms, K=0.5,
// H=12), and alarms during a 36-window (3 h) sustained shift under the
// latching rule and the old reset rule. Noise models: "lab" resamples the real
// lab baseline windows above; "gauss1" is N(0, 1 ms), noise as large as the
// sigma floor. Only runs when PATHWATCH_ARL_EXPERIMENT=1; prints TSV.
func TestCUSUM_SmallShiftExperiment(t *testing.T) {
	if os.Getenv("PATHWATCH_ARL_EXPERIMENT") != "1" {
		t.Skip("set PATHWATCH_ARL_EXPERIMENT=1")
	}
	const k, h, floor, warmup, after, trials, noisePoints = 0.5, 12.0, 1.0, 20, 36, 5000, 10000
	r := rand.New(rand.NewSource(20261031))
	lab := func() float64 { return labBaselineP95[r.Intn(len(labBaselineP95))] }
	gauss := func() float64 { return 0.358 + r.NormFloat64() }
	models := []struct {
		name string
		draw func() float64
	}{{"lab", lab}, {"gauss1", gauss}}
	baseline := func(draw func() float64) (mean, sigma float64) {
		var m, m2 float64
		for n := 1; n <= warmup; n++ {
			x := draw()
			d := x - m
			m += d / float64(n)
			m2 += d * (x - m)
		}
		return m, math.Max(floor, math.Sqrt(m2/float64(warmup-1)))
	}
	fmt.Printf("seed\t20261031\tK\t%g\tH\t%g\tsigma_floor_ms\t%g\twarmup\t%d\twindows_after_shift\t%d\ttrials\t%d\n", k, h, floor, warmup, after, trials)
	fmt.Println("model\tshift_ms\tdetected_frac\tdelay_mean\tdelay_p50\tdelay_p95\tdelay_max\talarms_latch_mean\talarms_reset_mean")
	for _, m := range models {
		for _, shift := range []float64{1, 2, 3, 6, 20} {
			delays := []int{}
			latchTotal, resetTotal := 0, 0
			for i := 0; i < trials; i++ {
				mean, sigma := baseline(m.draw)
				latch, reset := CUSUM{K: k, H: h}, CUSUM{K: k, H: h}
				first := 0
				for n := 1; n <= after; n++ {
					z := (m.draw() + shift - mean) / sigma
					if latch.Update(z) {
						latchTotal++
						if first == 0 {
							first = n
						}
					}
					if resetCUSUM(&reset, z) {
						resetTotal++
					}
				}
				if first > 0 {
					delays = append(delays, first)
				}
			}
			sort.Ints(delays)
			q := func(p float64) int {
				if len(delays) == 0 {
					return 0
				}
				return delays[int(math.Ceil(p*float64(len(delays))))-1]
			}
			sum := 0
			for _, d := range delays {
				sum += d
			}
			meanDelay := math.NaN()
			if len(delays) > 0 {
				meanDelay = float64(sum) / float64(len(delays))
			}
			fmt.Printf("%s\t%g\t%.4f\t%.2f\t%d\t%d\t%d\t%.3f\t%.3f\n", m.name, shift, float64(len(delays))/trials, meanDelay, q(.5), q(.95), q(1), float64(latchTotal)/trials, float64(resetTotal)/trials)
		}
	}
	// False-alarm side on the lab noise: 10,000 windows after the warmup, no shift.
	fmt.Println("model\tnoise_windows\ttrials\tp_zero_alarms\tmean_alarms_latch\tmean_alarms_reset")
	for _, m := range models {
		zero, latchTotal, resetTotal := 0, 0, 0
		for i := 0; i < 200; i++ {
			mean, sigma := baseline(m.draw)
			latch, reset := CUSUM{K: k, H: h}, CUSUM{K: k, H: h}
			a := 0
			for n := 0; n < noisePoints; n++ {
				z := (m.draw() - mean) / sigma
				if latch.Update(z) {
					a++
				}
				if resetCUSUM(&reset, z) {
					resetTotal++
				}
			}
			if a == 0 {
				zero++
			}
			latchTotal += a
		}
		fmt.Printf("%s\t%d\t%d\t%.4f\t%.3f\t%.3f\n", m.name, noisePoints, 200, float64(zero)/200, float64(latchTotal)/200, float64(resetTotal)/200)
	}
}

// siegmund is Siegmund's approximation of the in-control ARL of a one-sided CUSUM.
func siegmund(k, h float64) float64 {
	b := h + 1.166
	return (math.Exp(2*k*b) - 2*k*b - 1) / (2 * k * k)
}
