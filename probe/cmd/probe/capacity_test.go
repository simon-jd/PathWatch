package main

import (
	"context"
	"crypto/sha256"
	"encoding/pem"
	"fmt"
	"github.com/miekg/dns"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"pathwatch.local/pathwatch/probe/internal/dnsprobe"
	"pathwatch.local/pathwatch/probe/internal/result"
	"pathwatch.local/pathwatch/probe/internal/spool"
	"pathwatch.local/pathwatch/probe/internal/testdns"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"strings"
	"testing"
	"time"
	"unicode/utf8"
)

func TestCapacityProfileAdmission(t *testing.T) {
	raw := `{"probe_id":"home","spool_dir":"unused","capacity_profile":"five-week-v1","capacity_start_utc":"2026-11-01T00:00:00Z","capacity_deadline_utc":"2026-12-06T00:00:00Z","capacity_dns_addresses":["10.0.0.1","fd00::1"],"interval":"120s","batch_size":128,"spool_max_bytes":67108864,"targets":[{"ID":"private","Host":"10.0.0.1","Port":443,"Kinds":["tcp","tls","http1","http2","http3","trace"]},{"ID":"udp","Host":"owned.test","Kinds":["dns"],"DNS":{"transport":"udp","server":"127.0.0.1:53"}},{"ID":"tcp","Host":"owned.test","Kinds":["dns"],"DNS":{"transport":"tcp","server":"127.0.0.1:53"}},{"ID":"doh","Host":"owned.test","Kinds":["dns"],"DNS":{"transport":"doh","doh_url":"https://127.0.0.1/dns-query"}}]}`
	cfg, err := loadConfig(strings.NewReader(raw))
	if err != nil {
		t.Fatalf("explicit bounded full-protocol profile rejected: %v", err)
	}
	if _, _, _, _, err = cfg.validate(); err != nil {
		t.Fatal(err)
	}
	for _, change := range [][2]string{{"2026-12-06T00:00:00Z", "2026-12-07T00:00:00Z"}, {"2026-11-01T00:00:00Z", "bad"}, {"five-week-v1", "unknown"}, {"120s", "119s"}, {"10.0.0.1\",\"Port", "example.test\",\"Port"}, {"fd00::1", "10.0.0.2"}, {"67108864", "1024"}, {"\"batch_size\":128", "\"batch_size\":1"}} {
		bad, err := loadConfig(strings.NewReader(strings.Replace(raw, change[0], change[1], 1)))
		if err == nil {
			_, _, _, _, err = bad.validate()
		}
		if err == nil {
			t.Errorf("unsupported profile accepted: %v", change)
		}
	}
}

func TestBoundedDiagnostic(t *testing.T) {
	for _, raw := range []string{"refused", strings.Repeat("a", 256), strings.Repeat("界", 200), "bad\xffUTF8"} {
		got := boundedDiagnostic(raw)
		if len(raw) <= 256 && utf8.ValidString(raw) {
			if got != raw {
				t.Fatal("short diagnostic changed")
			}
			continue
		}
		hash := sha256.Sum256([]byte(raw))
		if len(got) > 256 || !utf8.ValidString(got) || !strings.Contains(got, fmt.Sprintf("original_bytes=%d sha256=%x", len(raw), hash)) {
			t.Fatalf("invalid diagnostic %q", got)
		}
	}
}

type suppliedResults struct{ results []result.Result }

func (s suppliedResults) Probe(context.Context, result.Target) ([]result.Result, error) {
	return s.results, nil
}
func TestCapacityViolationPreservesEveryResult(t *testing.T) {
	rs := []result.Result{}
	for i := 0; i < 5; i++ {
		rs = append(rs, result.Result{Addr: fmt.Sprintf("10.0.0.%d", i+1), Family: "v4", Err: strings.Repeat("x", 300)})
	}
	p := capacityProber{inner: suppliedResults{rs}, addresses: []string{"10.0.0.1", "fd00::1"}}
	got, err := p.Probe(context.Background(), result.Target{DNS: &result.DNSConfig{Transport: "udp"}})
	if err == nil || len(got) != 5 {
		t.Fatalf("overflow lost results or continued: %v %d", err, len(got))
	}
	for i, r := range got {
		if r.Addr != rs[i].Addr || len(r.Err) > 256 || !strings.Contains(r.Err, "truncated") {
			t.Fatalf("result %d not preserved with bounded diagnostic: %+v", i, r)
		}
	}
}
func TestCapacityFlushPreservesLegacyDefault(t *testing.T) {
	if (Config{}).flushInterval() != time.Second {
		t.Fatal("legacy flush changed")
	}
	if (Config{CapacityProfile: capacityProfile}).flushInterval() != 60*time.Second {
		t.Fatal("capacity collector still splits every second")
	}
}

func TestCapacityWireBoundary(t *testing.T) {
	at := &timestamppb.Timestamp{Seconds: 4294967295 - 180*86400, Nanos: 999999999}
	max := int64(9223372036854775807)
	normal := &pb.ProbeResult{ProbeId: strings.Repeat("p", 16), TargetId: strings.Repeat("t", 24), Kind: "http3", Family: "v6", Addr: "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", StartedAt: at, Err: strings.Repeat("x", 256), Protocol: "h3", TlsVersion: 65535, TlsResumed: true, PhasesNanoseconds: map[string]int64{"dns": max, "connect": max, "tls_handshake": max, "ttfb": max, "total": max}}
	n := proto.Size(normal) + 3
	if n > 512 {
		t.Fatalf("normal result+frame=%d exceeds512", n)
	}
	trace := proto.Clone(normal).(*pb.ProbeResult)
	trace.PhasesNanoseconds = map[string]int64{"total": max}
	trace.Kind = "trace"
	trace.Family = "v4"
	trace.Addr = "255.255.255.255"
	trace.Protocol = ""
	trace.TlsVersion = 0
	trace.TlsResumed = false
	for i := 1; i <= 30; i++ {
		trace.Hops = append(trace.Hops, &pb.Hop{Ttl: uint32(i), Addr: "255.255.255.255", RttNanoseconds: max})
	}
	if proto.Size(trace)+3 > 1472 {
		t.Fatalf("trace+frame=%d exceeds1472", proto.Size(trace)+3)
	}
	req := &pb.ReportRequest{ProbeId: strings.Repeat("p", 16), BatchId: strings.Repeat("b", 64), SentAt: at}
	for i := 0; i < 128; i++ {
		req.Results = append(req.Results, normal)
	}
	envelope := proto.Size(req) - 128*n
	if envelope > 128 {
		t.Fatalf("request envelope=%d exceeds128", envelope)
	}
	t.Logf("normal_with_frame=%d trace_with_frame=%d request_extra=%d", n, proto.Size(trace)+3, envelope)
}

func TestCapacityActualDNSModesAndPrivateCA(t *testing.T) {
	server := testdns.Start(t, func(_ string, q *dns.Msg) *dns.Msg { return testdns.Answer(q, "10.0.0.1", "fd00::1") })
	doh := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(io.LimitReader(r.Body, 65536))
		q := new(dns.Msg)
		if err := q.Unpack(raw); err != nil {
			t.Error(err)
			return
		}
		wire, _ := testdns.Answer(q, "10.0.0.1", "fd00::1").Pack()
		w.Header().Set("Content-Type", "application/dns-message")
		w.Write(wire)
	}))
	defer doh.Close()
	ca := filepath.Join(t.TempDir(), "public-ca.pem")
	if err := os.WriteFile(ca, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: doh.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	for _, mode := range []string{"udp", "tcp", "doh"} {
		target := result.Target{ID: "owned", Host: "owned.test", Kinds: []result.Kind{result.DNS}, DNS: &result.DNSConfig{Transport: mode, Server: server}}
		if mode == "doh" {
			target.DNS.Server = ""
			target.DNS.DoHURL = doh.URL
			target.DNS.CAFile = ca
		}
		raw, err := dnsprobe.New(dnsprobe.Config{ProbeID: "home", Server: server, Timeout: time.Second, Targets: []result.Target{target}})
		if err != nil {
			t.Fatal(err)
		}
		p := capacityProber{inner: raw, addresses: []string{"10.0.0.1", "fd00::1"}}
		rs, err := p.Probe(context.Background(), target)
		if err != nil || len(rs) != 2 {
			t.Fatalf("%s: %+v %v", mode, rs, err)
		}
		for _, r := range rs {
			if r.Err != "" || r.TargetID != "owned:dns:"+mode {
				t.Fatalf("%s: %+v", mode, r)
			}
		}
	}
}
func TestCapacityOverflowRunDurablyFlushesAllAddresses(t *testing.T) {
	server := testdns.Start(t, func(_ string, q *dns.Msg) *dns.Msg {
		return testdns.Answer(q, "10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4", "fd00::1")
	})
	start := time.Now().UTC().Add(-24 * time.Hour).Truncate(time.Second)
	cfg := Config{CapacityStartUTC: start.Format(time.RFC3339), CapacityDeadlineUTC: start.Add(35 * 24 * time.Hour).Format(time.RFC3339), ProbeID: "home", CapacityProfile: capacityProfile, CapacityDNSAddresses: []string{"10.0.0.1", "fd00::1"}, Interval: "120s", BatchSize: 128, SpoolMaxBytes: 64 << 20, SpoolDir: t.TempDir(), Ingest: "127.0.0.1:1", DevelopmentCleartext: true, MetricsAddr: "127.0.0.1:0", DNSServer: server}
	for _, mode := range []string{"udp", "tcp", "doh"} {
		d := &result.DNSConfig{Transport: mode, Server: server}
		if mode == "doh" {
			d.Server = ""
			d.DoHURL = "https://127.0.0.1:1/dns-query"
		}
		cfg.Targets = append(cfg.Targets, result.Target{ID: mode, Host: "owned.test", Kinds: []result.Kind{result.DNS}, DNS: d})
	}
	cfg.Targets = append(cfg.Targets, result.Target{ID: "private", Host: "10.0.0.1", Port: 443, Kinds: []result.Kind{result.TCP, result.TLS, result.HTTP1, result.HTTP2, result.HTTP3, result.Trace}})
	if err := run(context.Background(), cfg); err == nil || !strings.Contains(err.Error(), "all produced results retained") {
		t.Fatalf("overflow continued: %v", err)
	}
	q, err := spool.Open(cfg.SpoolDir, 64<<20)
	if err != nil {
		t.Fatal(err)
	}
	defer q.Close()
	_, rs, _, err := q.Next()
	if err != nil || len(rs) != 5 || q.Stats().Dropped != 0 {
		t.Fatalf("overflow data lost: %+v %+v %v", q.Stats(), rs, err)
	}
	for i, addr := range []string{"10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4", "fd00::1"} {
		if rs[i].Addr != addr {
			t.Fatal("result order/address changed")
		}
	}
}

func TestCapacityWindowWaitExpiryAndResume(t *testing.T) {
	now := time.Now()
	c := Config{CapacityProfile: capacityProfile, CapacityStartUTC: now.Add(30 * time.Millisecond).UTC().Format(time.RFC3339Nano), CapacityDeadlineUTC: now.Add(120 * time.Millisecond).UTC().Format(time.RFC3339Nano)}
	ctx, cancel, err := c.capacityWindow(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer cancel()
	if time.Since(now) < 25*time.Millisecond {
		t.Fatal("started before fixed window")
	}
	<-ctx.Done()
	if time.Since(now) > time.Second {
		t.Fatal("deadline extended")
	}
	if _, _, err = c.capacityWindow(context.Background()); err == nil {
		t.Fatal("expired resume accepted")
	}
	c.CapacityStartUTC = now.Add(-time.Hour).UTC().Format(time.RFC3339Nano)
	c.CapacityDeadlineUTC = time.Now().Add(50 * time.Millisecond).UTC().Format(time.RFC3339Nano)
	ctx, stop, err := c.capacityWindow(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer stop()
	<-ctx.Done()
	legacy, stopLegacy, err := (Config{}).capacityWindow(context.Background())
	defer stopLegacy()
	if err != nil || legacy.Err() != nil {
		t.Fatal("legacy acquired campaign limit", err)
	}
}

func TestCapacityDeadlineDurablyFlushesCollectedResults(t *testing.T) {
	server := testdns.Start(t, func(network string, q *dns.Msg) *dns.Msg {
		if network == "tcp" {
			return nil
		}
		return testdns.Answer(q, "10.0.0.1", "fd00::1")
	})
	end := time.Now().UTC().Truncate(time.Second).Add(2 * time.Second)
	cfg := Config{CapacityProfile: capacityProfile, CapacityStartUTC: end.Add(-35 * 24 * time.Hour).Format(time.RFC3339), CapacityDeadlineUTC: end.Format(time.RFC3339), CapacityDNSAddresses: []string{"10.0.0.1", "fd00::1"}, ProbeID: "home", Interval: "120s", BatchSize: 128, SpoolMaxBytes: 64 << 20, SpoolDir: t.TempDir(), Ingest: "127.0.0.1:1", DevelopmentCleartext: true, MetricsAddr: "127.0.0.1:0", DNSServer: server}
	for _, mode := range []string{"udp", "tcp", "doh"} {
		d := &result.DNSConfig{Transport: mode, Server: server}
		if mode == "doh" {
			d.Server = ""
			d.DoHURL = "https://127.0.0.1:1/dns-query"
		}
		cfg.Targets = append(cfg.Targets, result.Target{ID: mode, Host: "owned.test", Kinds: []result.Kind{result.DNS}, DNS: d})
	}
	cfg.Targets = append(cfg.Targets, result.Target{ID: "private", Host: "10.0.0.1", Port: 443, Kinds: []result.Kind{result.TCP, result.TLS, result.HTTP1, result.HTTP2, result.HTTP3, result.Trace}})
	began := time.Now()
	err := run(context.Background(), cfg)
	if time.Since(began) > 3*time.Second {
		t.Fatal("deadline did not stop scheduler", err)
	}
	q, err := spool.Open(cfg.SpoolDir, 64<<20)
	if err != nil {
		t.Fatal(err)
	}
	defer q.Close()
	_, rs, _, err := q.Next()
	if err != nil || len(rs) < 2 || rs[0].TargetID != "udp:dns:udp" || rs[0].Addr != "10.0.0.1" || rs[1].Addr != "fd00::1" || q.Stats().Dropped != 0 {
		t.Fatalf("deadline final flush lost collected results: %+v %v", rs, err)
	}
	t.Logf("fixed_deadline_elapsed=%s retained=%d dropped=%d", time.Since(began), len(rs), q.Stats().Dropped)
}
