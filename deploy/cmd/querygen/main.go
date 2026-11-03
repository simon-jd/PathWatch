// Querygen sends a fixed rate of measurement-API GETs through the edge proxy and
// prints one JSON line per interval with the status counts. Every request uses
// a new TCP/TLS connection so kube-proxy spreads load over stable and canary pods.
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
)

type config struct {
	target, pathKey string
	host            string // Host header; the edge proxy routes on it
	rps             int
	reportRps       int
	interval        time.Duration
	client          *http.Client
	reports         pb.IngestClient
}

type summary struct {
	Time       string         `json:"t"`
	Sent       int            `json:"sent"`
	Status     map[string]int `json:"status"`
	ReportSent int            `json:"report_sent,omitempty"`
	Report     map[string]int `json:"report,omitempty"`
}

func main() {
	var cfg config
	var ca, serverName string
	flag.StringVar(&cfg.target, "target", "https://api.pathwatch-app.svc:443", "edge proxy base URL")
	flag.StringVar(&cfg.pathKey, "path-key", "home→owned/tcp/v4", "path_key query parameter")
	flag.IntVar(&cfg.rps, "rps", 20, "requests per second")
	flag.IntVar(&cfg.reportRps, "report-rps", 0, "Report RPCs per second through the edge proxy; 0 disables")
	flag.DurationVar(&cfg.interval, "interval", time.Second, "summary interval")
	flag.StringVar(&ca, "ca", "", "CA PEM for the edge proxy certificate")
	flag.StringVar(&serverName, "server-name", "api.pathwatch.local", "TLS server name (SNI)")
	flag.Parse()
	pem, err := os.ReadFile(ca)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(pem) || cfg.rps < 1 {
		fmt.Fprintln(os.Stderr, "invalid -ca or -rps")
		os.Exit(2)
	}
	cfg.host = serverName
	tlsCfg := &tls.Config{RootCAs: roots, ServerName: serverName, MinVersion: tls.VersionTLS12}
	cfg.client = client(tlsCfg)
	if cfg.reportRps > 0 {
		u, err := url.Parse(cfg.target)
		if err != nil || u.Host == "" {
			fmt.Fprintln(os.Stderr, "invalid -target for Report")
			os.Exit(2)
		}
		conn, err := grpc.NewClient(u.Host, grpc.WithTransportCredentials(credentials.NewTLS(tlsCfg)), grpc.WithAuthority(serverName))
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(2)
		}
		defer conn.Close()
		cfg.reports = pb.NewIngestClient(conn)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	run(ctx, cfg, os.Stdout)
}

func client(t *tls.Config) *http.Client {
	return &http.Client{Timeout: 3 * time.Second, Transport: &http.Transport{TLSClientConfig: t, DisableKeepAlives: true, ForceAttemptHTTP2: false}}
}

func run(ctx context.Context, cfg config, out io.Writer) {
	var mu sync.Mutex
	cur := summary{Status: map[string]int{}}
	var wg sync.WaitGroup
	tick := time.NewTicker(time.Second / time.Duration(cfg.rps))
	defer tick.Stop()
	var reportTick <-chan time.Time
	if cfg.reportRps > 0 && cfg.reports != nil {
		rt := time.NewTicker(time.Second / time.Duration(cfg.reportRps))
		defer rt.Stop()
		reportTick = rt.C
	}
	report := time.NewTicker(cfg.interval)
	defer report.Stop()
	enc := json.NewEncoder(out)
	var batch atomic.Uint64
	flush := func(now time.Time) {
		mu.Lock()
		cur.Time = now.UTC().Format(time.RFC3339Nano)
		_ = enc.Encode(cur)
		cur = summary{Status: map[string]int{}}
		mu.Unlock()
	}
	for {
		select {
		case <-ctx.Done():
			wg.Wait()
			return
		case now := <-report.C:
			flush(now)
		case now := <-reportTick:
			wg.Add(1)
			go func() {
				defer wg.Done()
				code := postReport(ctx, cfg, now, batch.Add(1))
				mu.Lock()
				cur.ReportSent++
				if cur.Report == nil {
					cur.Report = map[string]int{}
				}
				cur.Report[code]++
				mu.Unlock()
			}()
		case now := <-tick.C:
			wg.Add(1)
			go func() {
				defer wg.Done()
				code := get(ctx, cfg, now)
				mu.Lock()
				cur.Sent++
				cur.Status[code]++
				mu.Unlock()
			}()
		}
	}
}

func get(ctx context.Context, cfg config, now time.Time) string {
	q := url.Values{"path_key": {cfg.pathKey}, "from": {now.Add(-time.Hour).UTC().Format(time.RFC3339)}, "to": {now.UTC().Format(time.RFC3339)}}
	req, err := http.NewRequestWithContext(ctx, "GET", cfg.target+"/v1/measurements/raw?"+q.Encode(), nil)
	if err != nil {
		return "request_error"
	}
	if cfg.host != "" {
		req.Host = cfg.host
	}
	resp, err := cfg.client.Do(req)
	if err != nil {
		return "transport_error"
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	return strconv.Itoa(resp.StatusCode)
}

func postReport(ctx context.Context, cfg config, now time.Time, n uint64) string {
	req := &pb.ReportRequest{
		BatchId: fmt.Sprintf("querygen-%d", n),
		ProbeId: "querygen",
		SentAt:  timestamppb.New(now),
		Results: []*pb.ProbeResult{{
			ProbeId: "querygen", TargetId: "owned", Kind: "tcp", Family: "v4", Addr: "127.0.0.1",
			StartedAt: timestamppb.New(now), PhasesNanoseconds: map[string]int64{"connect": int64(time.Millisecond)},
		}},
	}
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	if _, err := cfg.reports.Report(ctx, req); err != nil {
		return status.Code(err).String()
	}
	return "OK"
}
