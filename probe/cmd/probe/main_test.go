package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
	"io"
	"net"
	"net/http"
	"pathwatch.local/pathwatch/probe/internal/result"
	"pathwatch.local/pathwatch/probe/internal/spool"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"strings"
	"testing"
	"time"
)

type ingestServer struct {
	pb.UnimplementedIngestServer
	received chan *pb.ReportRequest
}

func (s *ingestServer) Report(_ context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
	select {
	case s.received <- proto.Clone(r).(*pb.ReportRequest):
	default:
	}
	return &pb.ReportResponse{BatchId: r.BatchId, Status: pb.AckStatus_ACK_STATUS_ACCEPTED, CommittedResultCount: uint32(len(r.Results))}, nil
}
func TestConfigRejectsInvalidBeforeOpeningSpool(t *testing.T) {
	for _, edit := range []func(*Config){func(c *Config) { c.Interval = "0s" }, func(c *Config) { c.Targets = append(c.Targets, c.Targets[0]) }, func(c *Config) { c.Targets[0].Kinds = []result.Kind{"unknown"} }, func(c *Config) { c.DevelopmentCleartext = true; c.Ingest = "192.0.2.1:443" }, func(c *Config) { c.Targets[0].Kinds = []result.Kind{result.TCP}; c.Targets[0].Port = 0 }, func(c *Config) { c.MetricsAddr = "bad" }, func(c *Config) { c.SpoolMaxBytes = 1<<30 + 1 }} {
		c := Config{ProbeID: "p", Ingest: "127.0.0.1:5555", SpoolDir: t.TempDir(), Targets: []result.Target{{ID: "t", Host: "127.0.0.1", Kinds: []result.Kind{result.DNS}}}}
		edit(&c)
		if e := run(context.Background(), c); e == nil {
			t.Fatal("invalid configuration accepted")
		}
		s, e := spool.Open(c.SpoolDir, 1<<20)
		if e != nil {
			t.Fatal("validation leaked ownership", e)
		}
		s.Close()
	}
	for _, raw := range []string{`{"typo":1}`, `{} {}`, `{"probe_id":17}`} {
		if _, e := loadConfig(strings.NewReader(raw)); e == nil {
			t.Fatal("invalid JSON config accepted")
		}
	}
}
func TestProbe_OwnedLoopbackPipelineAndMetrics(t *testing.T) {
	tcp, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	defer tcp.Close()
	go func() {
		for {
			c, e := tcp.Accept()
			if e != nil {
				return
			}
			c.Close()
		}
	}()
	ingest, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	g := grpc.NewServer()
	srv := &ingestServer{received: make(chan *pb.ReportRequest, 16)}
	pb.RegisterIngestServer(g, srv)
	go g.Serve(ingest)
	defer g.Stop()
	// Reserve an available owned loopback port; run is the listener owner.
	metrics, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	metricsAddr := metrics.Addr().String()
	metrics.Close()
	_, port, _ := net.SplitHostPort(tcp.Addr().String())
	var n int
	fmt.Sscan(port, &n)
	cfg := Config{ProbeID: "p", Ingest: ingest.Addr().String(), DevelopmentCleartext: true, Interval: "20ms", BatchSize: 1, SpoolDir: t.TempDir(), MetricsAddr: metricsAddr, Targets: []result.Target{{ID: "owned", Host: "127.0.0.1", Port: n, Kinds: []result.Kind{result.TCP}}}}
	raw, _ := json.Marshal(cfg)
	cfg, e = loadConfig(strings.NewReader(string(raw)))
	if e != nil {
		t.Fatal(e)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- run(ctx, cfg) }()
	select {
	case req := <-srv.received:
		if req.ProbeId != "p" || len(req.Results) != 1 || req.Results[0].TargetId != "owned" || req.Results[0].Addr != "127.0.0.1" || req.Results[0].Err != "" || req.Results[0].PhasesNanoseconds["connect"] <= 0 {
			t.Fatalf("pipeline lost real TCP measurement: %+v", req)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no loopback measurement reported")
	}
	body := waitMetric(t, metricsAddr, "pathwatch_report_acknowledged_total")
	for _, name := range []string{"pathwatch_spool_dropped_total 0", "pathwatch_spool_batches ", "pathwatch_measurements_total ", "pathwatch_report_acknowledged_total "} {
		if !strings.Contains(body, name) {
			t.Fatalf("missing metrics %s: %s", name, body)
		}
	}
	cancel()
	select {
	case e := <-done:
		if e != nil {
			t.Fatal(e)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("shutdown blocked")
	}
	s, e := spool.Open(cfg.SpoolDir, 1<<30)
	if e != nil {
		t.Fatal("shutdown leaked spool owner", e)
	}
	defer s.Close()
	// Any race with shutdown is already durably represented in the queue.
	if _, _, _, e = s.Next(); e != nil && !errors.Is(e, spool.ErrEmpty) {
		t.Fatal(e)
	}
}
func TestProbe_ShutdownFlushesPendingResults(t *testing.T) {
	closed, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	port := closed.Addr().(*net.TCPAddr).Port
	closed.Close()
	reserved, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	metricsAddr := reserved.Addr().String()
	reserved.Close()
	cfg := Config{ProbeID: "p", Ingest: "127.0.0.1:1", DevelopmentCleartext: true, Interval: "1h", BatchSize: 10000, SpoolDir: t.TempDir(), MetricsAddr: metricsAddr, Targets: []result.Target{{ID: "owned", Host: "127.0.0.1", Port: port, Kinds: []result.Kind{result.TCP}}}}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- run(ctx, cfg) }()
	waitMetric(t, metricsAddr, "pathwatch_measurements_total")
	cancel()
	select {
	case e := <-done:
		if e != nil {
			t.Fatal(e)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("shutdown blocked")
	}
	s, e := spool.Open(cfg.SpoolDir, 1<<30)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	_, rs, _, e := s.Next()
	if e != nil || len(rs) != 1 || rs[0].Addr != "127.0.0.1" || rs[0].Err == "" {
		t.Fatalf("pending measurement not flushed: %v %+v", e, rs)
	}
}

func waitMetric(t *testing.T, addr, name string) string {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	client := http.Client{Timeout: 200 * time.Millisecond}
	var body string
	for time.Now().Before(deadline) {
		response, e := client.Get("http://" + addr + "/metrics")
		if e == nil {
			raw, _ := io.ReadAll(response.Body)
			response.Body.Close()
			body = string(raw)
			for _, line := range strings.Split(body, "\n") {
				var n uint64
				if count, _ := fmt.Sscanf(line, name+" %d", &n); count == 1 && n > 0 {
					return body
				}
			}
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("metric %s did not become positive: %s", name, body)
	return ""
}

func TestConfigDNSTransportIsAdditiveAndValidated(t *testing.T) {
	cfg, err := loadConfig(strings.NewReader(`{"probe_id":"p","dns_server":"127.0.0.1:53","targets":[{"ID":"owned","Host":"owned.test","Kinds":["dns"],"DNS":{"transport":"tcp"}}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = probers(cfg, time.Second); err != nil {
		t.Fatal(err)
	}
	bad, err := loadConfig(strings.NewReader(`{"probe_id":"p","dns_server":"127.0.0.1:53","targets":[{"ID":"owned","Host":"owned.test","Kinds":["dns"],"DNS":{"transport":"doh","doh_url":"http://127.0.0.1/dns-query"}}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = probers(bad, time.Second); err == nil {
		t.Fatal("invalid per-target DNS config accepted before startup")
	}
}

func TestConfigTraceMode(t *testing.T) {
	for _, mode := range []string{"", `,"trace_mode":"icmp"`, `,"trace_mode":"udp"`} {
		cfg, err := loadConfig(strings.NewReader(`{"probe_id":"p","dns_server":"127.0.0.1:53"` + mode + `,"targets":[]}`))
		if err != nil {
			t.Fatal(err)
		}
		if _, err = probers(cfg, time.Second); err != nil {
			t.Fatalf("%s: %v", mode, err)
		}
	}
	cfg, err := loadConfig(strings.NewReader(`{"probe_id":"p","dns_server":"127.0.0.1:53","trace_mode":"raw","targets":[]}`))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = probers(cfg, time.Second); err == nil {
		t.Fatal("unknown trace_mode accepted before startup")
	}
}

type waitingIngestServer struct {
	pb.UnimplementedIngestServer
	entered chan struct{}
}

func (s *waitingIngestServer) Report(ctx context.Context, _ *pb.ReportRequest) (*pb.ReportResponse, error) {
	select {
	case s.entered <- struct{}{}:
	default:
	}
	<-ctx.Done()
	return nil, ctx.Err()
}

func TestProbe_CancelDuringInflightReportIsClean(t *testing.T) {
	listener, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	server := grpc.NewServer()
	fixture := &waitingIngestServer{entered: make(chan struct{}, 1)}
	pb.RegisterIngestServer(server, fixture)
	go server.Serve(listener)
	defer server.Stop()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cfg := Config{ProbeID: "home", Ingest: listener.Addr().String(), DevelopmentCleartext: true, Interval: "1h", BatchSize: 1, SpoolDir: t.TempDir(), MetricsAddr: "127.0.0.1:0", Targets: []result.Target{{ID: "owned", Host: "127.0.0.1", Port: listener.Addr().(*net.TCPAddr).Port, Kinds: []result.Kind{result.TCP}}}}
	done := make(chan error, 1)
	go func() { done <- run(ctx, cfg) }()
	select {
	case <-fixture.entered:
	case <-time.After(3 * time.Second):
		t.Fatal("no in-flight real report")
	}
	cancel()
	select {
	case e := <-done:
		if e != nil {
			t.Fatalf("normal cancellation became worker fault: %v", e)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("report cancellation leaked worker")
	}
	s, e := spool.Open(cfg.SpoolDir, 1<<30)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	_, records, _, e := s.Next()
	if e != nil || len(records) != 1 || records[0].ProbeID != "home" {
		t.Fatalf("unacknowledged result not preserved: %v %+v", e, records)
	}
}

func TestProbe_UnexpectedReporterExitStillFails(t *testing.T) {
	dir := t.TempDir()
	s, e := spool.Open(dir, 1<<20)
	if e != nil {
		t.Fatal(e)
	}
	e = s.Append([]result.Result{{ProbeID: "different-observer", TargetID: "owned", Kind: result.TCP, Family: "v4", Addr: "127.0.0.1", StartedAt: time.Now(), Phases: map[string]time.Duration{"connect": time.Millisecond}}})
	if e != nil {
		t.Fatal(e)
	}
	s.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	e = run(ctx, Config{ProbeID: "home", Ingest: "127.0.0.1:1", DevelopmentCleartext: true, SpoolDir: dir, MetricsAddr: "127.0.0.1:0", Targets: []result.Target{{ID: "owned", Host: "127.0.0.1", Port: 1, Kinds: []result.Kind{result.TCP}}}})
	if e == nil || !strings.Contains(e.Error(), "report worker stopped") || !strings.Contains(e.Error(), "differs from reporter") || ctx.Err() != nil {
		t.Fatalf("unexpected reporter failure hidden: %v", e)
	}
}
