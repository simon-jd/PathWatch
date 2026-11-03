package tcpprobe

import (
	"context"
	"errors"
	"net"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/miekg/dns"
	"pathwatch.local/pathwatch/probe/internal/resolve"
	"pathwatch.local/pathwatch/probe/internal/result"
	"pathwatch.local/pathwatch/probe/internal/testdns"
)

func TestTCPProbe_UsesMonotonicClock(t *testing.T) {
	listener := listen(t, "tcp4", "127.0.0.1:0")
	wall := time.Date(2026, 10, 23, 12, 0, 0, 0, time.UTC)
	calls := 0
	p, err := New(Config{ProbeID: "p", Server: "127.0.0.1:53", WallNow: func() time.Time { v := wall.Add(-time.Duration(calls) * time.Hour); calls++; return v }})
	if err != nil {
		t.Fatal(err)
	}
	var previous time.Time
	for i := 0; i < 2; i++ {
		start := time.Now()
		got, err := p.Probe(context.Background(), target(listener, "127.0.0.1"))
		elapsed := time.Since(start)
		if err != nil || len(got) != 1 || got[0].Err != "" {
			t.Fatalf("results=%+v err=%v", got, err)
		}
		if duration := got[0].Phases["connect"]; duration <= 0 || duration > elapsed || elapsed > time.Second {
			t.Fatalf("connect=%s real elapsed=%s", duration, elapsed)
		}
		if got[0].StartedAt != wall.Add(-time.Duration(i)*time.Hour) || (!previous.IsZero() && !got[0].StartedAt.Before(previous)) {
			t.Fatalf("wall timestamp: %s previous=%s", got[0].StartedAt, previous)
		}
		previous = got[0].StartedAt
		t.Logf("wall=%s connect=%s real-monotonic-elapsed=%s", got[0].StartedAt, got[0].Phases["connect"], elapsed)
	}
}

func TestTCPProbe_Refused(t *testing.T) {
	l, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	target := target(l, "127.0.0.1")
	if err := l.Close(); err != nil {
		t.Fatal(err)
	}
	p, err := New(Config{ProbeID: "p", Server: "127.0.0.1:53"})
	if err != nil {
		t.Fatal(err)
	}
	got, err := p.Probe(context.Background(), target)
	if err != nil || len(got) != 1 || !strings.Contains(got[0].Err, "refused") || got[0].Phases["connect"] <= 0 {
		t.Fatalf("results=%+v err=%v", got, err)
	}
}

func TestTCPProbe_PartialAnswersRetainDeadline(t *testing.T) {
	l := listen(t, "tcp4", "127.0.0.1:0")
	p, err := New(Config{ProbeID: "p", Server: "127.0.0.1:53"})
	if err != nil {
		t.Fatal(err)
	}
	// This dependency boundary deliberately returns an unrepresented resolution
	// deadline. The successful address is still exercised on a real listener.
	p.resolver = partialDeadlineResolver{}
	got, err := p.Probe(context.Background(), target(l, "owned.test"))
	if err != nil || len(got) != 2 {
		t.Fatalf("partial results=%+v err=%v", got, err)
	}
	if got[0].Addr != "127.0.0.1" || got[0].Err != "" || got[0].Phases["connect"] <= 0 || got[1].Err != context.DeadlineExceeded.Error() || got[1].Phases["resolve"] <= 0 {
		t.Fatalf("partial connection or unrepresented timeout lost: %+v", got)
	}
}

type partialDeadlineResolver struct{}

func (partialDeadlineResolver) Timeout() time.Duration { return time.Second }
func (partialDeadlineResolver) Lookup(context.Context, string) ([]resolve.Answer, error) {
	return []resolve.Answer{{Family: "v4", Addr: "127.0.0.1"}}, context.DeadlineExceeded
}

func TestTCPProbe_EachAddressAndFamily(t *testing.T) {
	v4 := listen(t, "tcp4", "127.0.0.1:0")
	port := v4.Addr().(*net.TCPAddr).Port
	v6 := listen(t, "tcp6", net.JoinHostPort("::1", strconv.Itoa(port)))
	_ = v6
	server := testdns.Start(t, func(_ string, q *dns.Msg) *dns.Msg { return testdns.Answer(q, "127.0.0.1", "127.0.0.2", "::1") })
	p, err := New(Config{ProbeID: "p", Server: server})
	if err != nil {
		t.Fatal(err)
	}
	got, err := p.Probe(context.Background(), result.Target{ID: "t", Host: "owned.test", Port: port})
	if err != nil || len(got) != 3 {
		t.Fatalf("results=%+v err=%v", got, err)
	}
	for i, addr := range []string{"127.0.0.1", "127.0.0.2", "::1"} {
		if got[i].Addr != addr || got[i].Family != []string{"v4", "v4", "v6"}[i] || got[i].ProbeID != "p" || got[i].TargetID != "t" || got[i].Kind != result.TCP {
			t.Fatalf("result %d: %+v", i, got[i])
		}
		if i == 1 {
			if !strings.Contains(got[i].Err, "refused") {
				t.Fatalf("expected refused: %+v", got[i])
			}
		} else if got[i].Err != "" {
			t.Fatalf("expected success: %+v", got[i])
		}
	}
	got, err = p.Probe(context.Background(), target(v6, "[::1]"))
	if err != nil || len(got) != 1 || got[0].Err != "" || got[0].Addr != "::1" {
		t.Fatalf("bracketed literal results=%+v err=%v", got, err)
	}
}

func TestTCPProbe_InvalidAndCanceled(t *testing.T) {
	if _, err := New(Config{}); err == nil {
		t.Fatal("missing probe ID accepted")
	}
	p, err := New(Config{ProbeID: "p", Server: "127.0.0.1:53"})
	if err != nil {
		t.Fatal(err)
	}
	for _, port := range []int{-1, 0, 65536} {
		if _, err := p.Probe(context.Background(), result.Target{ID: "t", Host: "127.0.0.1", Port: port}); err == nil {
			t.Fatalf("accepted port %d", port)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := p.Probe(ctx, result.Target{ID: "t", Host: "127.0.0.1", Port: 80}); !errors.Is(err, context.Canceled) {
		t.Fatalf("err=%v", err)
	}
}

func listen(t *testing.T, network, address string) net.Listener {
	t.Helper()
	l, err := net.Listen(network, address)
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			conn, err := l.Accept()
			if err != nil {
				return
			}
			conn.Close()
		}
	}()
	t.Cleanup(func() { l.Close(); <-done })
	return l
}

func target(l net.Listener, host string) result.Target {
	return result.Target{ID: "t", Host: host, Port: l.Addr().(*net.TCPAddr).Port}
}
