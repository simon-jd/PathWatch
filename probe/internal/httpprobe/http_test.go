package httpprobe

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/miekg/dns"
	"github.com/quic-go/quic-go/http3"

	"pathwatch.local/pathwatch/probe/internal/resolve"
	"pathwatch.local/pathwatch/probe/internal/result"
	"pathwatch.local/pathwatch/probe/internal/testdns"
)

func setup(t *testing.T, h2 bool, handler http.Handler) (*httptest.Server, Config, result.Target) {
	t.Helper()
	if handler == nil {
		handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/" || r.Method != "HEAD" {
				t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
			}
			w.WriteHeader(200)
		})
	}
	s := httptest.NewUnstartedServer(handler)
	s.EnableHTTP2 = h2
	s.Config.ErrorLog = log.New(io.Discard, "", 0)
	s.StartTLS()
	t.Cleanup(s.Close)
	roots := x509.NewCertPool()
	roots.AddCert(s.Certificate())
	host, port, _ := net.SplitHostPort(s.Listener.Addr().String())
	n, _ := strconv.Atoi(port)
	return s, Config{ProbeID: "p", RootCAs: roots, Timeout: time.Second}, result.Target{ID: "t", Host: host, Port: n}
}
func check(t *testing.T, p *Probe, target result.Target, proto string) {
	t.Helper()
	start := time.Now()
	got, err := p.Probe(context.Background(), target)
	if err != nil || len(got) != 1 || got[0].Err != "" || got[0].Protocol != proto || got[0].Phases["ttfb"] <= 0 || got[0].Phases["total"] < got[0].Phases["ttfb"] || got[0].Phases["total"] > time.Since(start) || got[0].ProbeID != "p" {
		t.Fatalf("got=%+v err=%v", got, err)
	}
	t.Logf("protocol=%s ttfb=%s total=%s", got[0].Protocol, got[0].Phases["ttfb"], got[0].Phases["total"])
}
func TestHTTPProbe_H1(t *testing.T) {
	_, cfg, target := setup(t, true, nil)
	p, err := NewWithConfig("h1", cfg)
	if err != nil {
		t.Fatal(err)
	}
	check(t, p, target, "h1")
}
func TestHTTPProbe_H2(t *testing.T) {
	_, cfg, target := setup(t, true, nil)
	p, err := NewWithConfig("h2", cfg)
	if err != nil {
		t.Fatal(err)
	}
	check(t, p, target, "h2")
}
func TestHTTPProbe_H3(t *testing.T) {
	s, cfg, target := setup(t, false, nil)
	udp, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := &http3.Server{TLSConfig: &tls.Config{Certificates: s.TLS.Certificates}, Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.ProtoMajor != 3 || r.URL.Path != "/" {
			t.Errorf("request=%+v", r)
		}
		w.WriteHeader(200)
	})}
	done := make(chan error, 1)
	go func() { done <- server.Serve(udp) }()
	t.Cleanup(func() { server.Close(); udp.Close(); <-done })
	target.Port = udp.LocalAddr().(*net.UDPAddr).Port
	p, err := NewWithConfig("h3", cfg)
	if err != nil {
		t.Fatal(err)
	}
	check(t, p, target, "h3")
}
func TestHTTPProbe_NoFallbackOrRedirect(t *testing.T) {
	_, cfg, target := setup(t, false, nil)
	p, _ := NewWithConfig("h2", cfg)
	got, err := p.Probe(context.Background(), target)
	if err != nil || len(got) != 1 || got[0].Err == "" || got[0].Protocol == "h2" {
		t.Fatalf("fallback got=%+v err=%v", got, err)
	}
	hits := 0
	s, cfg, target := setup(t, false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		http.Redirect(w, r, "https://unmeasured.invalid/elsewhere", 302)
	}))
	_ = s
	p, _ = NewWithConfig("h1", cfg)
	check(t, p, target, "h1")
	if hits != 1 {
		t.Fatalf("redirect followed hits=%d", hits)
	}
}
func TestHTTPProbe_UntrustedCancelAndTimeout(t *testing.T) {
	_, cfg, target := setup(t, true, nil)
	cfg.RootCAs = nil
	p, _ := NewWithConfig("h1", cfg)
	got, err := p.Probe(context.Background(), target)
	if err != nil || len(got) != 1 || got[0].Err == "" || got[0].Protocol != "" {
		t.Fatalf("untrusted got=%+v err=%v", got, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err = p.Probe(ctx, target); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel=%v", err)
	}
	_, cfg, target = setup(t, false, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { <-r.Context().Done() }))
	cfg.Timeout = 40 * time.Millisecond
	p, _ = NewWithConfig("h1", cfg)
	start := time.Now()
	got, err = p.Probe(context.Background(), target)
	if err != nil || len(got) != 1 || got[0].Err == "" || got[0].Protocol != "" || time.Since(start) > time.Second {
		t.Fatalf("timeout got=%+v err=%v", got, err)
	}
	if _, err = New("wrong"); err == nil {
		t.Fatal("invalid protocol accepted")
	}
	if p, err = New("h1"); err != nil || p == nil {
		t.Fatalf("default constructor=%v", err)
	}
}

func TestHTTPProbe_ResolvedHostAndPartialFailure(t *testing.T) {
	_, cfg, target := setup(t, true, nil)
	queries := map[uint16]int{}
	var queryMu sync.Mutex
	cfg.Server = testdns.Start(t, func(_ string, q *dns.Msg) *dns.Msg {
		queryMu.Lock()
		queries[q.Question[0].Qtype]++
		queryMu.Unlock()
		return testdns.Answer(q, "127.0.0.1", "127.0.0.2", "::1")
	})
	p, _ := NewWithConfig("h2", cfg)
	target.Host = "example.com"
	got, err := p.Probe(context.Background(), target)
	queryMu.Lock()
	defer queryMu.Unlock()
	if err != nil || len(got) != 3 || got[0].Err != "" || got[0].Protocol != "h2" || got[1].Err == "" || got[2].Family != "v6" || queries[dns.TypeA] != 1 || queries[dns.TypeAAAA] != 1 {
		t.Fatalf("resolved=%+v err=%v queries=%v", got, err, queries)
	}
	p.resolver = partialResolver{}
	got, err = p.Probe(context.Background(), target)
	if err != nil || len(got) != 2 || got[0].Err != "" || got[1].Err != context.DeadlineExceeded.Error() {
		t.Fatalf("partial=%+v err=%v", got, err)
	}
}

type partialResolver struct{}

func (partialResolver) Timeout() time.Duration { return time.Second }
func (partialResolver) Lookup(context.Context, string) ([]resolve.Answer, error) {
	return []resolve.Answer{{Family: "v4", Addr: "127.0.0.1"}}, context.DeadlineExceeded
}

func TestHTTPProbe_InFlightCancel(t *testing.T) {
	for _, protocol := range []string{"h1", "h2", "h3"} {
		t.Run(protocol, func(t *testing.T) {
			seen := make(chan struct{})
			handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { close(seen); <-r.Context().Done() })
			s, cfg, target := setup(t, true, handler)
			if protocol == "h3" {
				udp, err := net.ListenPacket("udp4", "127.0.0.1:0")
				if err != nil {
					t.Fatal(err)
				}
				server := &http3.Server{TLSConfig: &tls.Config{Certificates: s.TLS.Certificates}, Handler: handler}
				done := make(chan error, 1)
				go func() { done <- server.Serve(udp) }()
				t.Cleanup(func() { server.Close(); udp.Close(); <-done })
				target.Port = udp.LocalAddr().(*net.UDPAddr).Port
			}
			p, _ := NewWithConfig(protocol, cfg)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			done := make(chan error, 1)
			go func() { _, err := p.Probe(ctx, target); done <- err }()
			select {
			case <-seen:
			case <-time.After(time.Second):
				t.Fatal("request did not reach owned server")
			}
			cancel()
			select {
			case err := <-done:
				if !errors.Is(err, context.Canceled) {
					t.Fatalf("cancel=%v", err)
				}
			case <-time.After(time.Second):
				t.Fatal("HTTP cancellation did not interrupt request")
			}
		})
	}
}

func TestHTTPProbe_H3IPv6Literal(t *testing.T) {
	s, cfg, target := setup(t, false, nil)
	udp, err := net.ListenPacket("udp6", "[::1]:0")
	if err != nil {
		t.Fatal(err)
	}
	server := &http3.Server{TLSConfig: &tls.Config{Certificates: s.TLS.Certificates}, Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) })}
	done := make(chan error, 1)
	go func() { done <- server.Serve(udp) }()
	t.Cleanup(func() { server.Close(); udp.Close(); <-done })
	target.Host = "[::1]"
	target.Port = udp.LocalAddr().(*net.UDPAddr).Port
	p, _ := NewWithConfig("h3", cfg)
	got, err := p.Probe(context.Background(), target)
	if err != nil || len(got) != 1 || got[0].Err != "" || got[0].Protocol != "h3" || got[0].Addr != "::1" || got[0].Family != "v6" {
		t.Fatalf("v6=%+v err=%v", got, err)
	}
}
