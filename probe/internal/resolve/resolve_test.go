package resolve

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"

	"github.com/miekg/dns"
	"pathwatch.local/pathwatch/probe/internal/testdns"
)

// Expire the context after a real A reply is read but before the next query.
// This controls a boundary that wall-clock sleeps cannot reliably place.
func TestLookup_DeadlineBetweenFamilies(t *testing.T) {
	var aaaa atomic.Int32
	server := testdns.Start(t, func(_ string, q *dns.Msg) *dns.Msg {
		if q.Question[0].Qtype == dns.TypeAAAA {
			aaaa.Add(1)
		}
		return testdns.Answer(q, "127.0.0.1", "::1")
	})
	r, err := New(server, 0)
	if err != nil {
		t.Fatal(err)
	}
	base, cancel := context.WithCancel(context.Background())
	defer cancel()
	ctx := expiredDeadlineContext{base}
	got, err := r.lookup(ctx, "owned.test", func(ctx context.Context, network string, q *dns.Msg) (*dns.Msg, error) {
		m, err := r.exchange(ctx, network, q)
		if err == nil && q.Question[0].Qtype == dns.TypeA {
			cancel()
		}
		return m, err
	})
	if !errors.Is(err, context.DeadlineExceeded) || len(got) != 2 {
		t.Fatalf("partial answers=%+v err=%v", got, err)
	}
	if got[0].Addr != "127.0.0.1" || got[0].Family != "v4" || got[0].Err != "" || got[1].Addr != "" || got[1].Family != "v6" || got[1].Err != context.DeadlineExceeded.Error() || got[1].Duration <= 0 || aaaa.Load() != 0 {
		t.Fatalf("partial response or unstarted AAAA failure lost: %+v AAAA queries=%d", got, aaaa.Load())
	}
	if completed := PreserveFailure(got, err, got[1].Duration); len(completed) != 2 {
		t.Fatalf("already represented family timeout was duplicated: %+v", completed)
	}
	t.Logf("real A elapsed=%s pending v6 failure=%q elapsed-to-cutoff=%s AAAA queries=%d", got[0].Duration, got[1].Err, got[1].Duration, aaaa.Load())
}

// Preserve the Context Done/Err contract while identifying this controlled
// transition as a deadline, without changing host time or waiting on a timer.
type expiredDeadlineContext struct{ context.Context }

func (c expiredDeadlineContext) Err() error {
	if c.Context.Err() != nil {
		return context.DeadlineExceeded
	}
	return nil
}
