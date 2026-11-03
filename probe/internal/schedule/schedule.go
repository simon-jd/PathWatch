// Package schedule runs target rounds with bounded, cancellable waits.
package schedule

import (
	"context"
	"fmt"
	"math/rand/v2"
	"pathwatch.local/pathwatch/probe/internal/resolve"
	"pathwatch.local/pathwatch/probe/internal/result"
	"strings"
	"time"
)

type Target = result.Target
type Kind = result.Kind
type Prober = result.Prober

// Clock.Now must retain monotonic elapsed time, even if wall time changes.
// Wait must return on cancellation. Float64 supplies a value in [0,1].
type Clock interface {
	Now() time.Time
	Wait(context.Context, time.Duration) error
	Float64() float64
}
type RealClock struct{}

func (RealClock) Now() time.Time   { return time.Now() }
func (RealClock) Float64() float64 { return rand.Float64() }
func (RealClock) Wait(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// Validate rejects unusable configuration before any network operation.
func Validate(targets []Target, probers map[Kind]Prober, every time.Duration) error {
	if every <= 0 {
		return fmt.Errorf("interval must be positive")
	}
	if len(targets) == 0 {
		return fmt.Errorf("at least one target is required")
	}
	seen := map[string]bool{}
	for _, t := range targets {
		if strings.TrimSpace(t.ID) == "" || seen[t.ID] {
			return fmt.Errorf("empty or duplicate target ID %q", t.ID)
		}
		seen[t.ID] = true
		if _, err := resolve.Host(t.Host); err != nil {
			return fmt.Errorf("target %s: %w", t.ID, err)
		}
		if len(t.Kinds) == 0 {
			return fmt.Errorf("target %s has no kinds", t.ID)
		}
		kinds := map[Kind]bool{}
		for _, k := range t.Kinds {
			switch k {
			case result.DNS, result.TCP, result.TLS, result.HTTP1, result.HTTP2, result.HTTP3, result.Trace:
			default:
				return fmt.Errorf("unknown kind %q", k)
			}
			if kinds[k] || probers[k] == nil {
				return fmt.Errorf("duplicate or unavailable kind %q", k)
			}
			kinds[k] = true
			if k == result.Trace {
				if t.Public || t.Port < 0 || t.Port > 65535-63 {
					return fmt.Errorf("invalid private trace target %s", t.ID)
				}
			} else if k != result.DNS && (t.Port < 1 || t.Port > 65535) {
				return fmt.Errorf("target %s requires port in 1..65535", t.ID)
			}
		}
	}
	return nil
}

// Run serializes logical rounds, preserving each prober's ordered addresses.
// Every next round is based on completion, with ±10% jitter and a 60s public
// floor. Slow rounds never cause catch-up runs. The caller owns and closes out.
func Run(ctx context.Context, targets []Target, probers map[Kind]Prober, every time.Duration, clock Clock, out chan<- result.Result) error {
	if err := Validate(targets, probers, every); err != nil {
		return err
	}
	if out == nil {
		return fmt.Errorf("output channel is required")
	}
	if clock == nil {
		clock = RealClock{}
	}
	due := make([]time.Time, len(targets))
	for i := range due {
		due[i] = clock.Now()
	}
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		next := 0
		for i := range due {
			if due[i].Before(due[next]) {
				next = i
			}
		}
		if wait := due[next].Sub(clock.Now()); wait > 0 {
			if err := clock.Wait(ctx, wait); err != nil {
				return err
			}
			continue
		}
		target := targets[next]
		for _, kind := range target.Kinds {
			if err := ctx.Err(); err != nil {
				return err
			}
			rs, err := probers[kind].Probe(ctx, target)
			for _, r := range rs {
				select {
				case out <- r:
				case <-ctx.Done():
					return ctx.Err()
				}
			}
			if err != nil {
				return fmt.Errorf("target %s kind %s: %w", target.ID, kind, err)
			}
		}
		sample := clock.Float64()
		if sample < 0 || sample > 1 {
			return fmt.Errorf("clock random sample outside [0,1]")
		}
		interval := time.Duration(float64(every) * (0.9 + 0.2*sample))
		if interval <= 0 {
			interval = time.Nanosecond
		}
		if target.Public && interval < time.Minute {
			interval = time.Minute
		}
		due[next] = clock.Now().Add(interval)
	}
}
