// Command probe wires bounded scheduling, durable batching and gRPC reporting.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"pathwatch.local/pathwatch/probe/internal/dnsprobe"
	"pathwatch.local/pathwatch/probe/internal/httpprobe"
	"pathwatch.local/pathwatch/probe/internal/report"
	"pathwatch.local/pathwatch/probe/internal/result"
	"pathwatch.local/pathwatch/probe/internal/schedule"
	"pathwatch.local/pathwatch/probe/internal/spool"
	"pathwatch.local/pathwatch/probe/internal/tcpprobe"
	"pathwatch.local/pathwatch/probe/internal/tlsprobe"
	"pathwatch.local/pathwatch/probe/internal/traceprobe"
)

type Config struct {
	CapacityStartUTC     string          `json:"capacity_start_utc,omitempty"`
	CapacityDeadlineUTC  string          `json:"capacity_deadline_utc,omitempty"`
	CapacityProfile      string          `json:"capacity_profile,omitempty"`
	CapacityDNSAddresses []string        `json:"capacity_dns_addresses,omitempty"`
	ProbeID              string          `json:"probe_id"`
	Ingest               string          `json:"ingest"`
	IngestCA             string          `json:"ingest_ca,omitempty"`
	DevelopmentCleartext bool            `json:"development_cleartext,omitempty"`
	DNSServer            string          `json:"dns_server,omitempty"`
	TraceMode            string          `json:"trace_mode,omitempty"` // "icmp" (CAP_NET_RAW, default) or "udp" (IP_RECVERR, unprivileged)
	Interval             string          `json:"interval,omitempty"`
	ProbeTimeout         string          `json:"probe_timeout,omitempty"`
	RPCTimeout           string          `json:"rpc_timeout,omitempty"`
	ShutdownTimeout      string          `json:"shutdown_timeout,omitempty"`
	SpoolDir             string          `json:"spool_dir"`
	SpoolMaxBytes        int64           `json:"spool_max_bytes,omitempty"`
	BatchSize            int             `json:"batch_size,omitempty"`
	MetricsAddr          string          `json:"metrics_addr,omitempty"`
	Targets              []result.Target `json:"targets"`
}

func loadConfig(r io.Reader) (Config, error) {
	var cfg Config
	limited := io.LimitReader(r, (1<<20)+1)
	raw, err := io.ReadAll(limited)
	if err != nil {
		return cfg, err
	}
	if len(raw) > 1<<20 {
		return cfg, fmt.Errorf("config exceeds 1MiB")
	}
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err = dec.Decode(&cfg); err != nil {
		return cfg, err
	}
	var extra any
	if err = dec.Decode(&extra); err != io.EOF {
		return cfg, fmt.Errorf("config must contain exactly one JSON value")
	}
	return cfg, nil
}
func duration(value string, fallback time.Duration) (time.Duration, error) {
	if value == "" {
		return fallback, nil
	}
	d, e := time.ParseDuration(value)
	if e != nil || d <= 0 {
		return 0, fmt.Errorf("duration %q must be positive", value)
	}
	return d, nil
}
func (c *Config) validate() (every, probeTimeout, rpcTimeout, shutdown time.Duration, err error) {
	if strings.TrimSpace(c.ProbeID) == "" || strings.TrimSpace(c.SpoolDir) == "" {
		err = fmt.Errorf("probe_id and spool_dir are required")
		return
	}
	if every, err = duration(c.Interval, time.Minute); err != nil {
		return
	}
	if probeTimeout, err = duration(c.ProbeTimeout, 5*time.Second); err != nil {
		return
	}
	if rpcTimeout, err = duration(c.RPCTimeout, 10*time.Second); err != nil {
		return
	}
	if shutdown, err = duration(c.ShutdownTimeout, 5*time.Second); err != nil {
		return
	}
	if c.SpoolMaxBytes == 0 {
		c.SpoolMaxBytes = spool.MaxBytes
	}
	if c.SpoolMaxBytes < 1 || c.SpoolMaxBytes > spool.MaxBytes {
		err = fmt.Errorf("spool_max_bytes must be in 1..1GiB")
		return
	}
	if c.BatchSize == 0 {
		c.BatchSize = 128
	}
	if c.BatchSize < 1 || c.BatchSize > 10000 {
		err = fmt.Errorf("batch_size must be in 1..10000")
		return
	}
	if err = c.validateCapacity(every, probeTimeout, rpcTimeout); err != nil {
		return
	}
	if c.MetricsAddr == "" {
		c.MetricsAddr = "127.0.0.1:9464"
	}
	var host, port string
	host, port, err = net.SplitHostPort(c.MetricsAddr)
	if err != nil {
		return
	}
	if host != "" && net.ParseIP(host) == nil {
		err = fmt.Errorf("metrics_addr must use a literal IP")
		return
	}
	var n int
	n, err = strconv.Atoi(port)
	if err != nil || n < 0 || n > 65535 {
		err = fmt.Errorf("invalid metrics port")
		return
	}
	return
}
func probers(cfg Config, timeout time.Duration) (map[result.Kind]result.Prober, error) {
	out := map[result.Kind]result.Prober{}
	d, e := dnsprobe.New(dnsprobe.Config{ProbeID: cfg.ProbeID, Server: cfg.DNSServer, Timeout: timeout, Targets: cfg.Targets})
	if e != nil {
		return nil, e
	}
	out[result.DNS] = d
	tcp, e := tcpprobe.New(tcpprobe.Config{ProbeID: cfg.ProbeID, Server: cfg.DNSServer, Timeout: timeout})
	if e != nil {
		return nil, e
	}
	out[result.TCP] = tcp
	tls, e := tlsprobe.New(tlsprobe.Config{ProbeID: cfg.ProbeID, Server: cfg.DNSServer, Timeout: timeout})
	if e != nil {
		return nil, e
	}
	out[result.TLS] = tls
	for _, pair := range []struct {
		kind     result.Kind
		protocol string
	}{{result.HTTP1, "h1"}, {result.HTTP2, "h2"}, {result.HTTP3, "h3"}} {
		p, e := httpprobe.NewWithConfig(pair.protocol, httpprobe.Config{ProbeID: cfg.ProbeID, Server: cfg.DNSServer, Timeout: timeout})
		if e != nil {
			return nil, e
		}
		out[pair.kind] = p
	}
	trace, e := traceprobe.New(traceprobe.Config{ProbeID: cfg.ProbeID, Server: cfg.DNSServer, Timeout: timeout, Mode: cfg.TraceMode})
	if e != nil {
		return nil, e
	}
	out[result.Trace] = trace
	if cfg.CapacityProfile == capacityProfile {
		for kind, p := range out {
			out[kind] = capacityProber{inner: p, addresses: cfg.CapacityDNSAddresses}
		}
	}
	return out, nil
}

type counters struct{ measurements, operational atomic.Uint64 }

func metricsHandler(s *spool.Spool, c *report.Client, counts *counters) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
		q := s.Stats()
		r := c.Stats()
		fmt.Fprintf(w, "pathwatch_up 1\npathwatch_spool_dropped_total %d\npathwatch_spool_bytes %d\npathwatch_spool_batches %d\npathwatch_report_attempts_total %d\npathwatch_report_acknowledged_total %d\npathwatch_report_failures_total %d\npathwatch_measurements_total %d\npathwatch_operational_errors_total %d\n", q.Dropped, q.Bytes, q.Batches, r.Attempts, r.Acknowledged, r.Failures, counts.measurements.Load(), counts.operational.Load())
	})
	return mux
}

func run(parent context.Context, cfg Config) (retErr error) {
	every, probeTimeout, rpcTimeout, shutdown, err := cfg.validate()
	if err != nil {
		return err
	}
	window, stopWindow, err := cfg.capacityWindow(parent)
	if err != nil {
		return err
	}
	defer stopWindow()
	parent = window
	ps, err := probers(cfg, probeTimeout)
	if err != nil {
		return err
	}
	if err = schedule.Validate(cfg.Targets, ps, every); err != nil {
		return err
	}
	var counts counters
	client, err := report.New(report.Config{ProbeID: cfg.ProbeID, Endpoint: cfg.Ingest, CAFile: cfg.IngestCA, DevelopmentCleartext: cfg.DevelopmentCleartext, RPCTimeout: rpcTimeout, OnError: func(error) { counts.operational.Add(1) }})
	if err != nil {
		return err
	}
	defer client.Close()
	s, err := spool.Open(cfg.SpoolDir, cfg.SpoolMaxBytes)
	if err != nil {
		return err
	}
	defer func() { retErr = errors.Join(retErr, s.Close()) }()
	listener, err := net.Listen("tcp", cfg.MetricsAddr)
	if err != nil {
		return err
	}
	server := &http.Server{Handler: metricsHandler(s, client, &counts), ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16 << 10}
	defer server.Close()
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	out := make(chan result.Result, 128)
	scheduleDone := make(chan error, 1)
	reportDone := make(chan error, 1)
	httpDone := make(chan error, 1)
	go func() { e := schedule.Run(ctx, cfg.Targets, ps, every, nil, out); close(out); scheduleDone <- e }()
	go func() { reportDone <- client.Run(ctx, s) }()
	go func() { httpDone <- server.Serve(listener) }()
	// The single collector bounds memory to batch_size plus the channel.
	// The opt-in capacity profile uses a bounded 60s timer; legacy uses 1s.
	batch := make([]result.Result, 0, cfg.BatchSize)
	ticker := time.NewTicker(cfg.flushInterval())
	defer ticker.Stop()
	flush := func() error {
		if len(batch) == 0 {
			return nil
		}
		if e := s.Append(batch); e != nil {
			counts.operational.Add(1)
			return fmt.Errorf("append measurements: %w", e)
		}
		batch = make([]result.Result, 0, cfg.BatchSize)
		return nil
	}
	collect := func(r result.Result) error {
		counts.measurements.Add(1)
		batch = append(batch, r)
		if len(batch) >= cfg.BatchSize {
			return flush()
		}
		return nil
	}
	schedulerFinished, reporterFinished := false, false
loop:
	for {
		select {
		case <-parent.Done():
			break loop
		case r, ok := <-out:
			if !ok {
				schedulerFinished = true
				err = <-scheduleDone
				if !errors.Is(err, context.Canceled) {
					retErr = errors.Join(retErr, err)
				}
				break loop
			}
			if err = collect(r); err != nil {
				retErr = err
				break loop
			}
		case err = <-reportDone:
			reporterFinished = true
			// Both channels can be ready after parent cancellation. Selecting the
			// reporter first must not turn an ordinary shutdown into a fault.
			if parent.Err() == nil || !errors.Is(err, context.Canceled) {
				retErr = fmt.Errorf("report worker stopped: %w", err)
			}
			break loop
		case err = <-httpDone:
			if !errors.Is(err, http.ErrServerClosed) {
				retErr = fmt.Errorf("metrics listener: %w", err)
			}
			break loop
		case <-ticker.C:
			if err = flush(); err != nil {
				retErr = err
				break loop
			}
		}
	}
	// Cancellation interrupts scheduler output/timers/probes and the in-flight
	// RPC. Drain only already produced results, then persist the final partial batch.
	cancel()
	client.Close()
	deadline, stopDeadline := context.WithTimeout(context.Background(), shutdown)
	defer stopDeadline()
	if !schedulerFinished {
	drain:
		for {
			select {
			case r, ok := <-out:
				if !ok {
					schedulerFinished = true
					err = <-scheduleDone
					if err != nil && !errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded) {
						retErr = errors.Join(retErr, err)
					}
					break drain
				}
				if e := collect(r); e != nil {
					retErr = errors.Join(retErr, e)
					break drain
				}
			case <-deadline.Done():
				retErr = errors.Join(retErr, fmt.Errorf("scheduler shutdown timed out"))
				break drain
			}
		}
	}
	retErr = errors.Join(retErr, flush())
	if !reporterFinished {
		select {
		case err = <-reportDone:
			if err != nil && !errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded) {
				retErr = errors.Join(retErr, err)
			}
		case <-deadline.Done():
			retErr = errors.Join(retErr, fmt.Errorf("reporter shutdown timed out"))
		}
	}
	shutdownCtx, stop := context.WithTimeout(context.Background(), shutdown)
	defer stop()
	retErr = errors.Join(retErr, server.Shutdown(shutdownCtx))
	return retErr
}
func main() {
	path := flag.String("config", "", "path to probe JSON config")
	check := flag.Bool("check-config", false, "validate without starting workers; emit normalized configuration")
	flag.Parse()
	if *path == "" {
		log.Fatal("-config is required")
	}
	f, err := os.Open(*path)
	if err != nil {
		log.Fatal(err)
	}
	cfg, err := loadConfig(f)
	f.Close()
	if err != nil {
		log.Fatal(err)
	}
	if *check {
		every, timeout, _, _, err := cfg.validate()
		if err != nil {
			log.Fatal(err)
		}
		ps, err := probers(cfg, timeout)
		if err != nil {
			log.Fatal(err)
		}
		if err = schedule.Validate(cfg.Targets, ps, every); err != nil {
			log.Fatal(err)
		}
		client, e := report.New(report.Config{ProbeID: cfg.ProbeID, Endpoint: cfg.Ingest, CAFile: cfg.IngestCA, DevelopmentCleartext: cfg.DevelopmentCleartext})
		if e != nil {
			log.Fatal(e)
		}
		client.Close()
		if err = json.NewEncoder(os.Stdout).Encode(cfg); err != nil {
			log.Fatal(err)
		}
		return
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err = run(ctx, cfg); err != nil {
		log.Fatal(err)
	}
}
