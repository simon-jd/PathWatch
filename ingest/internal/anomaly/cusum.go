package anomaly

import "math"

// CUSUM is the standard upper tabular CUSUM on standardized observations z.
// K and H are in sigma units, not milliseconds. Sum and Latched are durable
// detector state.
//
// After an alarm the detector latches instead of resetting: Sum keeps
// accumulating but is clamped at H, and no further alarm is raised until Sum
// returns to 0, which re-arms it. A sustained shift alarms once per episode;
// re-arming needs at least H/K baseline windows (24 for K=0.5, H=12).
// Resetting Sum to 0 instead re-alarmed every few windows while a shift lasted.
type CUSUM struct {
	K, H, Sum float64
	Latched   bool
}

func (c *CUSUM) Update(z float64) bool {
	if math.IsNaN(z) || math.IsInf(z, 0) || c.K < 0 || c.H <= 0 {
		return false
	}
	c.Sum = math.Max(0, c.Sum+z-c.K)
	if c.Latched {
		if c.Sum == 0 {
			c.Latched = false
		}
		c.Sum = math.Min(c.Sum, c.H)
		return false
	}
	if c.Sum > c.H {
		c.Sum = c.H
		c.Latched = true
		return true
	}
	return false
}
