package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
)

func TestRunCountsStatusesPerIntervalWithFreshConnections(t *testing.T) {
	var n, conns atomic.Int64
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		if r.Host != "api.test" || r.URL.Path != "/v1/measurements/raw" || q.Get("path_key") != "p" || q.Get("from") == "" || q.Get("to") == "" {
			w.WriteHeader(400)
			return
		}
		if n.Add(1)%2 == 0 {
			w.WriteHeader(500)
			return
		}
		w.WriteHeader(200)
	}))
	srv.Config.ConnContext = func(ctx context.Context, _ net.Conn) context.Context { conns.Add(1); return ctx }
	srv.StartTLS()
	defer srv.Close()

	var out bytes.Buffer
	cfg := config{target: srv.URL, pathKey: "p", host: "api.test", rps: 50, interval: 200 * time.Millisecond, client: client(srv.Client().Transport.(*http.Transport).TLSClientConfig)}
	ctx, cancel := context.WithTimeout(context.Background(), 1100*time.Millisecond)
	defer cancel()
	run(ctx, cfg, &out)

	var total, ok, bad int
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) < 4 {
		t.Fatalf("want one line per interval, got %q", out.String())
	}
	for _, l := range lines {
		var s summary
		if err := json.Unmarshal([]byte(l), &s); err != nil {
			t.Fatalf("%q: %v", l, err)
		}
		if _, err := time.Parse(time.RFC3339Nano, s.Time); err != nil {
			t.Fatal(err)
		}
		total += s.Sent
		ok += s.Status["200"]
		bad += s.Status["500"]
	}
	if total < 30 || ok+bad != total || ok == 0 || bad == 0 {
		t.Fatalf("total=%d 200=%d 500=%d", total, ok, bad)
	}
	if got := conns.Load(); got < int64(total) {
		t.Fatalf("each request must use a fresh connection: %d connections for %d requests", got, total)
	}
}

type fakeIngest struct {
	pb.UnimplementedIngestServer
	n atomic.Int64
}

func (f *fakeIngest) Report(_ context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
	f.n.Add(1)
	return &pb.ReportResponse{BatchId: r.BatchId, Status: pb.AckStatus_ACK_STATUS_ACCEPTED, CommittedResultCount: 1}, nil
}

func TestRunCountsReportRPCs(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(200) }))
	defer srv.Close()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	gs := grpc.NewServer()
	fake := &fakeIngest{}
	pb.RegisterIngestServer(gs, fake)
	go gs.Serve(lis)
	defer gs.Stop()
	conn, err := grpc.NewClient(lis.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	var out bytes.Buffer
	cfg := config{target: srv.URL, pathKey: "p", host: "api.test", rps: 5, reportRps: 20, interval: 200 * time.Millisecond,
		client: client(srv.Client().Transport.(*http.Transport).TLSClientConfig), reports: pb.NewIngestClient(conn)}
	ctx, cancel := context.WithTimeout(context.Background(), 700*time.Millisecond)
	defer cancel()
	run(ctx, cfg, &out)
	var reports int
	for _, l := range strings.Split(strings.TrimSpace(out.String()), "\n") {
		var s summary
		if err := json.Unmarshal([]byte(l), &s); err != nil {
			t.Fatal(err)
		}
		reports += s.ReportSent
		if s.ReportSent > 0 && s.Report["OK"] != s.ReportSent {
			t.Fatalf("report statuses %+v", s.Report)
		}
	}
	if reports < 5 || fake.n.Load() < 5 {
		t.Fatalf("report lines %d server %d", reports, fake.n.Load())
	}
}
