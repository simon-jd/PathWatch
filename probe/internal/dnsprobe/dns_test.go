package dnsprobe

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"github.com/miekg/dns"
	"pathwatch.local/pathwatch/probe/internal/resolve"
	"pathwatch.local/pathwatch/probe/internal/result"
	"pathwatch.local/pathwatch/probe/internal/testdns"
)

func TestDNSProbe_MultipleAnswers(t *testing.T) {
	server := testdns.Start(t, func(_ string, q *dns.Msg) *dns.Msg { return testdns.Answer(q, "127.0.0.2", "127.0.0.1", "::1") })
	p, err := New(Config{ProbeID: "probe-a", Server: server})
	if err != nil {
		t.Fatal(err)
	}
	got, err := p.Probe(context.Background(), result.Target{ID: "test", Host: "owned.test"})
	if err != nil || len(got) != 3 {
		t.Fatalf("results=%+v err=%v", got, err)
	}
	for i, addr := range []string{"127.0.0.2", "127.0.0.1", "::1"} {
		family := []string{"v4", "v4", "v6"}[i]
		if got[i].Addr != addr || got[i].Family != family || got[i].Err != "" || got[i].ProbeID != "probe-a" || got[i].TargetID != "test" || got[i].Kind != result.DNS || got[i].Phases["resolve"] <= 0 {
			t.Fatalf("result %d: %+v", i, got[i])
		}
	}
}

func TestDNSProbe_IPv6Only(t *testing.T) {
	server := testdns.Start(t, func(_ string, q *dns.Msg) *dns.Msg { return testdns.Answer(q, "::1") })
	p, err := New(Config{ProbeID: "probe-a", Server: server})
	if err != nil {
		t.Fatal(err)
	}
	got, err := p.Probe(context.Background(), result.Target{ID: "test", Host: "owned.test"})
	if err != nil || len(got) != 1 || got[0].Family != "v6" || got[0].Err != "" {
		t.Fatalf("results=%+v err=%v", got, err)
	}
}

// A resolver may return valid partial answers and an error for work it could
// not start. The consumer must retain both, even though it already has results.
func TestDNSProbe_PartialAnswersRetainDeadline(t *testing.T) {
	p, err := New(Config{ProbeID: "probe-a", Server: "127.0.0.1:53"})
	if err != nil {
		t.Fatal(err)
	}
	p.resolver = partialDeadlineResolver{}
	got, err := p.Probe(context.Background(), result.Target{ID: "test", Host: "owned.test"})
	if err != nil || len(got) != 2 {
		t.Fatalf("partial results=%+v err=%v", got, err)
	}
	if got[0].Addr != "127.0.0.1" || got[0].Family != "v4" || got[0].Err != "" || got[1].Err != context.DeadlineExceeded.Error() || got[1].Phases["resolve"] <= 0 {
		t.Fatalf("partial answer or unrepresented timeout lost: %+v", got)
	}
}

type partialDeadlineResolver struct{}

func (partialDeadlineResolver) Timeout() time.Duration { return time.Second }
func (partialDeadlineResolver) Lookup(context.Context, string) ([]resolve.Answer, error) {
	return []resolve.Answer{{Family: "v4", Addr: "127.0.0.1"}}, context.DeadlineExceeded
}

func TestDNSProbe_TruncatedUDPUsesTCP(t *testing.T) {
	var udp, tcp atomic.Int32
	server := testdns.Start(t, func(network string, q *dns.Msg) *dns.Msg {
		if network == "udp" {
			udp.Add(1)
			m := new(dns.Msg).SetReply(q)
			m.Truncated = true
			return m
		}
		tcp.Add(1)
		return testdns.Answer(q, "127.0.0.1", "::1")
	})
	p, err := New(Config{ProbeID: "probe-a", Server: server})
	if err != nil {
		t.Fatal(err)
	}
	got, err := p.Probe(context.Background(), result.Target{ID: "test", Host: "owned.test"})
	if err != nil || len(got) != 2 || got[0].Err != "" || got[1].Err != "" || udp.Load() != 2 || tcp.Load() != 2 {
		t.Fatalf("results=%+v err=%v UDP=%d TCP=%d", got, err, udp.Load(), tcp.Load())
	}
}

func TestDNSProbe_CancelDuringQuery(t *testing.T) {
	received := make(chan struct{}, 1)
	server := testdns.Start(t, func(_ string, _ *dns.Msg) *dns.Msg {
		select {
		case received <- struct{}{}:
		default:
		}
		return nil
	})
	p, err := New(Config{ProbeID: "probe-a", Server: server, Timeout: 3 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { <-received; cancel() }()
	start := time.Now()
	_, err = p.Probe(ctx, result.Target{ID: "test", Host: "owned.test"})
	if !errors.Is(err, context.Canceled) || time.Since(start) > time.Second {
		t.Fatalf("cancel err=%v elapsed=%s", err, time.Since(start))
	}
}

func TestDNSProbe_TimeoutAndRemoteFailure(t *testing.T) {
	for _, mode := range []string{"timeout", "servfail", "nodata"} {
		t.Run(mode, func(t *testing.T) {
			server := testdns.Start(t, func(_ string, q *dns.Msg) *dns.Msg {
				if mode == "timeout" {
					return nil
				}
				m := new(dns.Msg).SetReply(q)
				if mode == "servfail" {
					m.Rcode = dns.RcodeServerFailure
				}
				return m
			})
			p, err := New(Config{ProbeID: "probe-a", Server: server, Timeout: 50 * time.Millisecond})
			if err != nil {
				t.Fatal(err)
			}
			start := time.Now()
			got, err := p.Probe(context.Background(), result.Target{ID: "test", Host: "owned.test"})
			if err != nil || len(got) == 0 || got[0].Err == "" || time.Since(start) > time.Second {
				t.Fatalf("results=%+v err=%v elapsed=%s", got, err, time.Since(start))
			}
			if mode == "nodata" && got[0].Phases["resolve"] <= 0 {
				t.Fatalf("real NODATA DNS queries lost elapsed time: %+v", got[0])
			}
			if mode == "nodata" {
				t.Logf("real dual-NODATA resolve=%s", got[0].Phases["resolve"])
			}
		})
	}
}

func TestDNSProbe_InvalidInput(t *testing.T) {
	for _, cfg := range []Config{{}, {ProbeID: "p", Timeout: -1}, {ProbeID: "p", Server: "invalid"}} {
		if _, err := New(cfg); err == nil {
			t.Fatalf("accepted config %+v", cfg)
		}
	}
	p, err := New(Config{ProbeID: "p", Server: "127.0.0.1:53"})
	if err != nil {
		t.Fatal(err)
	}
	for _, target := range []result.Target{{ID: "x"}, {Host: "owned.test"}, {ID: "x", Host: "127.0.0.1:80"}, {ID: "x", Host: "a..test"}} {
		if got, err := p.Probe(context.Background(), target); err == nil || len(got) != 0 {
			t.Fatalf("accepted target %+v: %+v %v", target, got, err)
		}
	}
}
