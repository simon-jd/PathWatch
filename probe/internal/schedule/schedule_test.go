package schedule

import (
	"context"
	"errors"
	"pathwatch.local/pathwatch/probe/internal/result"
	"sync"
	"testing"
	"time"
)

type fakeClock struct {
	mu     sync.Mutex
	now    time.Time
	waits  []time.Duration
	random float64
	cancel context.CancelFunc
}

func (f *fakeClock) Now() time.Time   { f.mu.Lock(); defer f.mu.Unlock(); return f.now }
func (f *fakeClock) Float64() float64 { return f.random }
func (f *fakeClock) Wait(ctx context.Context, d time.Duration) error {
	f.mu.Lock()
	f.waits = append(f.waits, d)
	f.now = f.now.Add(d)
	done := f.now.Sub(time.Unix(0, 0)) >= time.Hour
	f.mu.Unlock()
	if done {
		f.cancel()
	}
	return ctx.Err()
}

type proberFunc func(context.Context, result.Target) ([]result.Result, error)

func (f proberFunc) Probe(c context.Context, t result.Target) ([]result.Result, error) {
	return f(c, t)
}

// Removing the public clamp, scheduling per-kind, or catch-up after slow work
// must produce a too-early round in this one-hour monotonic simulation.
func TestSchedule_PublicTargetMinInterval(t *testing.T) {
	for _, random := range []float64{0, 1} {
		t.Run(time.Duration(random).String(), func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			clock := &fakeClock{now: time.Unix(0, 0), random: random, cancel: cancel}
			var starts []time.Time
			p := proberFunc(func(context.Context, result.Target) ([]result.Result, error) {
				starts = append(starts, clock.Now())
				return []result.Result{{Err: "remote failure"}}, nil
			})
			out := make(chan result.Result, 100)
			err := Run(ctx, []Target{{ID: "public", Host: "127.0.0.1", Public: true, Kinds: []Kind{result.DNS}}}, map[Kind]Prober{result.DNS: p}, time.Minute, clock, out)
			if !errors.Is(err, context.Canceled) {
				t.Fatal(err)
			}
			if len(starts) < 50 || len(out) != len(starts) {
				t.Fatalf("rounds=%d results=%d", len(starts), len(out))
			}
			for i := 1; i < len(starts); i++ {
				d := starts[i].Sub(starts[i-1])
				if d < 60*time.Second || d > 66*time.Second {
					t.Fatalf("interval=%v", d)
				}
			}
		})
	}
}
func TestSchedule_ValidationAndCancellation(t *testing.T) {
	p := proberFunc(func(context.Context, result.Target) ([]result.Result, error) { return nil, nil })
	good := Target{ID: "t", Host: "127.0.0.1", Kinds: []Kind{result.DNS}}
	for _, targets := range [][]Target{{good, good}, {{ID: "t", Host: "localhost", Kinds: []Kind{"unknown"}}}, {{ID: "t", Host: ""}}, {{ID: "t", Host: "127.0.0.1", Public: true, Kinds: []Kind{result.Trace}}}} {
		if err := Run(context.Background(), targets, map[Kind]Prober{result.DNS: p}, time.Second, nil, make(chan result.Result)); err == nil {
			t.Fatal("invalid target accepted")
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- Run(ctx, []Target{good}, map[Kind]Prober{result.DNS: proberFunc(func(context.Context, result.Target) ([]result.Result, error) { return []result.Result{{}}, nil })}, time.Minute, nil, make(chan result.Result))
	}()
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("blocked output did not cancel")
	}
}
func TestSchedule_ProgrammingErrorObservable(t *testing.T) {
	want := errors.New("bad configuration")
	err := Run(context.Background(), []Target{{ID: "t", Host: "127.0.0.1", Kinds: []Kind{result.DNS}}}, map[Kind]Prober{result.DNS: proberFunc(func(context.Context, result.Target) ([]result.Result, error) { return nil, want })}, time.Minute, nil, make(chan result.Result, 1))
	if !errors.Is(err, want) {
		t.Fatalf("got %v", err)
	}
}

func TestSchedule_SlowRoundDoesNotCatchUp(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	clock := &fakeClock{now: time.Unix(0, 0), cancel: cancel}
	var starts []time.Time
	dns := proberFunc(func(context.Context, result.Target) ([]result.Result, error) {
		starts = append(starts, clock.Now())
		clock.mu.Lock()
		clock.now = clock.now.Add(2 * time.Minute)
		clock.mu.Unlock()
		if len(starts) == 3 {
			cancel()
		}
		return nil, nil
	})
	tcp := proberFunc(func(context.Context, result.Target) ([]result.Result, error) {
		clock.mu.Lock()
		clock.now = clock.now.Add(time.Minute)
		clock.mu.Unlock()
		return nil, nil
	})
	Run(ctx, []Target{{ID: "t", Host: "127.0.0.1", Port: 443, Public: true, Kinds: []Kind{result.DNS, result.TCP}}}, map[Kind]Prober{result.DNS: dns, result.TCP: tcp}, time.Minute, clock, make(chan result.Result, 10))
	if len(starts) != 3 {
		t.Fatal(starts)
	}
	for i := 1; i < len(starts); i++ {
		if starts[i].Sub(starts[i-1]) != 4*time.Minute {
			t.Fatalf("slow round caught up early: %v", starts)
		}
	}
}
func TestSchedule_TimerCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	out := make(chan result.Result, 1)
	done := make(chan error, 1)
	go func() {
		done <- Run(ctx, []Target{{ID: "t", Host: "127.0.0.1", Kinds: []Kind{result.DNS}}}, map[Kind]Prober{result.DNS: proberFunc(func(context.Context, result.Target) ([]result.Result, error) { return []result.Result{{}}, nil })}, time.Hour, nil, out)
	}()
	<-out
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("timer did not cancel")
	}
}

func TestSchedule_CancellationDoesNotStartAnotherKind(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	tcpStarted := false
	dns := proberFunc(func(context.Context, result.Target) ([]result.Result, error) { cancel(); return nil, nil })
	tcp := proberFunc(func(context.Context, result.Target) ([]result.Result, error) { tcpStarted = true; return nil, nil })
	err := Run(ctx, []Target{{ID: "t", Host: "127.0.0.1", Port: 443, Kinds: []Kind{result.DNS, result.TCP}}}, map[Kind]Prober{result.DNS: dns, result.TCP: tcp}, time.Minute, nil, make(chan result.Result, 1))
	if !errors.Is(err, context.Canceled) || tcpStarted {
		t.Fatalf("canceled round started another kind: started=%v err=%v", tcpStarted, err)
	}
}
