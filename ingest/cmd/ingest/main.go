// Ingest runs the dedicated single-writer ClickHouse gateway on home-ubuntu.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"pathwatch.local/pathwatch/ingest/internal/corerpc"
	"pathwatch.local/pathwatch/ingest/internal/frontend"
	cp "pathwatch.local/pathwatch/proto/pathwatch/core/v1"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"google.golang.org/grpc"
	"pathwatch.local/pathwatch/ingest/internal/anomaly"
	"pathwatch.local/pathwatch/ingest/internal/queryapi"
	"pathwatch.local/pathwatch/ingest/internal/server"
	"pathwatch.local/pathwatch/ingest/internal/store"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
)

// defaultAnomalyH is the CUSUM threshold (K=0.5): the smallest H in the
// measured grid with P(no false alarm in 10,000 windows) >= 0.95 when the
// baseline is known; H=5 averages ~10.6 false alarms per 10,000 windows.
// Reproduce with TestCUSUM_ThresholdExperiment (internal/anomaly); results
// are summarized in docs/evidence/README.md.
const defaultAnomalyH = 12.0

func main() {
	if e := run(); e != nil {
		log.Print(e)
		os.Exit(1)
	}
}
func run() error {
	var mode, coreListen, coreTarget, identities string
	var tf corerpc.TLSFiles
	flag.StringVar(&mode, "mode", "embedded", "embedded, core or frontend")
	flag.StringVar(&coreListen, "core-listen", "127.0.0.1:9444", "private mTLS listener (core only)")
	flag.StringVar(&coreTarget, "core-target", "", "private core host:port (frontend only)")
	flag.StringVar(&tf.CA, "core-ca", "", "explicit private RPC CA")
	flag.StringVar(&tf.Cert, "core-cert", "", "private RPC certificate")
	flag.StringVar(&tf.Key, "core-key", "", "private RPC key")
	flag.StringVar(&tf.ServerName, "core-server-name", "", "exact core DNS/IP SAN")
	flag.StringVar(&identities, "core-frontends", "", "comma-separated exact frontend URI SANs (core only)")
	var transport server.TransportConfig
	var cfg store.Config
	var metrics, queryListen string
	var anomalyEnabled bool
	var ac anomaly.Config
	flag.StringVar(&transport.Listen, "listen", "127.0.0.1:9443", "gRPC host:port")
	flag.StringVar(&transport.CertFile, "tls-cert", "", "PEM server certificate")
	flag.StringVar(&transport.KeyFile, "tls-key", "", "PEM private key")
	flag.BoolVar(&transport.DevelopmentCleartext, "development-cleartext", false, "explicit literal-loopback development only")
	flag.StringVar(&queryListen, "query-listen", "", "optional literal-loopback measurement HTTP listener")
	flag.StringVar(&metrics, "metrics", "127.0.0.1:9091", "loopback metrics host:port")
	flag.StringVar(&cfg.URL, "clickhouse-url", "http://127.0.0.1:8123", "dedicated local ClickHouse HTTP endpoint")
	flag.StringVar(&cfg.Database, "database", "pathwatch", "precreated Atomic database")
	flag.StringVar(&cfg.Username, "clickhouse-user", "pathwatch_ingest", "dedicated database user; password via PATHWATCH_CLICKHOUSE_PASSWORD")
	flag.StringVar(&cfg.JournalDir, "journal", ".cache/ingest-journal", "durable journal; never remove to clear an error")
	flag.IntVar(&cfg.MaxRecords, "journal-max-records", 65536, "journal record capacity (1..262144); never evicts keys")
	flag.Int64Var(&cfg.MaxJournalBytes, "journal-max-bytes", 256<<20, "journal capacity; full rejects new batches, never evicts keys")
	flag.BoolVar(&anomalyEnabled, "anomaly-enable", false, "run durable five-minute latency detection and local Alertmanager delivery")
	flag.IntVar(&ac.Warmup, "anomaly-warmup", 20, "initial actual p95 windows per path for fixed baseline (2..1000)")
	flag.Float64Var(&ac.SigmaFloor, "anomaly-sigma-floor-ms", 1, "positive minimum baseline sigma in milliseconds")
	flag.Float64Var(&ac.K, "anomaly-k", .5, "upper CUSUM reference in sigma units")
	flag.Float64Var(&ac.H, "anomaly-h", defaultAnomalyH, "upper CUSUM threshold in sigma units; false alarms are possible")
	flag.IntVar(&ac.MaxPaths, "anomaly-max-paths", 1024, "durable path cardinality limit (1..4096); no eviction")
	flag.IntVar(&ac.MaxWindows, "anomaly-max-windows", 128, "maximum actual windows processed per tick (1..256)")
	flag.IntVar(&ac.MaxPending, "anomaly-max-pending", 64, "durable pending event capacity (1..256)")
	flag.StringVar(&ac.AlertmanagerURL, "alertmanager-url", "http://127.0.0.1:9093", "literal loopback HTTP base URL; only used with anomaly-enable")
	flag.DurationVar(&ac.Timeout, "anomaly-alert-timeout", 5*time.Second, "local alert request deadline (up to 30s)")
	flag.DurationVar(&ac.RetryMin, "anomaly-retry-min", 5*time.Second, "initial persistent retry backoff; retries occur at five-minute ticks")
	flag.DurationVar(&ac.RetryMax, "anomaly-retry-max", 5*time.Minute, "maximum persistent backoff (up to 1h)")
	flag.Parse()
	cfg.Password = os.Getenv("PATHWATCH_CLICKHOUSE_PASSWORD")
	explicit := map[string]bool{}
	flag.Visit(func(f *flag.Flag) { explicit[f.Name] = true })
	if err := validateMode(mode, explicit, cfg.Password); err != nil {
		return err
	}
	var opts []grpc.ServerOption
	var trust *corerpc.Trust
	var e error
	if mode != "core" {
		opts, e = server.Options(transport)
		if e != nil {
			return e
		}
	}
	if mode != "embedded" {
		if identities != "" {
			tf.Frontends = strings.Split(identities, ",")
		}
		trust, e = corerpc.LoadTrust(tf, mode == "core")
		if e != nil {
			return e
		}
		if mode == "core" {
			if e = corerpc.ValidateEndpoint(coreListen); e != nil {
				return e
			}
		} else {
			if e = corerpc.ValidateEndpoint(coreTarget); e != nil {
				return e
			}
		}
	}
	host, _, e := net.SplitHostPort(metrics)
	if e != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		return fmt.Errorf("metrics requires literal loopback")
	}
	if queryListen != "" {
		if e := queryapi.ValidateAddress(queryListen); e != nil {
			return e
		}
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	var st *store.Store
	var coreClient *corerpc.Client
	var reader store.MeasurementReader
	var owner *server.Server
	var front *frontend.Server
	var admission *corerpc.Admission
	var job *anomaly.Job
	if mode == "frontend" {
		startup, cancel := context.WithTimeout(ctx, 2*time.Second)
		coreClient, e = corerpc.Dial(startup, coreTarget, trust)
		cancel()
		if e != nil {
			return e
		}
		defer coreClient.Close()
		reader = coreClient
		front = frontend.New(coreClient, time.Now)
		admission = corerpc.NewAdmission(false, nil, front.Overloaded, front.SemanticRejected)
		opts = append(opts, admission.Options(nil)...)
	} else {
		st, e = store.Open(ctx, cfg)
		if e != nil {
			return e
		}
		defer st.Close()
		reader = st
		owner = server.New(st, time.Now)
		if anomalyEnabled {
			job, e = anomaly.New(st, ac)
			if e != nil {
				return e
			}
		}
		if mode == "core" {
			admission = corerpc.NewAdmission(true, trust.Authenticate, owner.Overloaded, owner.SemanticRejected)
			opts = admission.Options(trust.Credentials())
			transport.Listen = coreListen
		}
	}
	listener, e := net.Listen("tcp", transport.Listen)
	if e != nil {
		return e
	}
	defer listener.Close()
	if admission != nil {
		n := 16
		if mode == "core" {
			n = 10
		}
		listener = corerpc.LimitListener(listener, n)
	}
	ml, e := net.Listen("tcp", metrics)
	if e != nil {
		return e
	}
	defer ml.Close()
	var queryHandler *queryapi.Handler
	var queryServer *http.Server
	var queryListener net.Listener
	queryContext, cancelQueries := context.WithCancel(ctx)
	defer cancelQueries()
	if queryListen != "" {
		ql, err := net.Listen("tcp", queryListen)
		if err != nil {
			return err
		}
		queryListener = queryapi.LimitListener(ql)
		defer queryListener.Close()
		queryHandler = queryapi.New(reader)
		queryServer = &http.Server{Handler: queryHandler, ReadTimeout: 2 * time.Second, ReadHeaderTimeout: 2 * time.Second, WriteTimeout: 5 * time.Second, IdleTimeout: 15 * time.Second, MaxHeaderBytes: 16 << 10, BaseContext: func(net.Listener) context.Context { return queryContext }}
		defer queryServer.Close()
	}
	g := grpc.NewServer(opts...)
	if mode == "core" {
		corerpc.Register(g, &cp.Core_ServiceDesc, &corerpc.Service{Reporter: owner, Reader: st, Ready: func(ctx context.Context) error { _, _, e := st.MonitoringSnapshot(ctx); return e }})
	} else if mode == "frontend" {
		corerpc.Register(g, &pb.Ingest_ServiceDesc, front)
	} else {
		pb.RegisterIngestServer(g, owner)
	}
	var draining atomic.Bool
	mux := http.NewServeMux()
	mux.HandleFunc("/readyz", readyWhileServing(&draining, func(w http.ResponseWriter, r *http.Request) {
		if mode != "frontend" {
			owner.Ready(w, r)
			return
		}
		if r.Method != "GET" {
			w.WriteHeader(405)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if err := coreClient.Ready(ctx); err != nil {
			http.Error(w, "core unavailable", 503)
			return
		}
		w.Write([]byte("ready\n"))
	}))
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		if owner != nil {
			owner.Metrics(w, r)
		} else {
			front.Metrics(w, r)
		}
		if queryHandler != nil {
			queryHandler.Metrics(w)
		}
		if job != nil {
			job.Metrics(w)
		}
	})
	h := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16 << 10}
	var runJob func(context.Context)
	if job != nil {
		runJob = func(ctx context.Context) { job.Run(ctx, func(err error) { log.Printf("anomaly job: %v", err) }) }
	}
	return (lifecycle{g: g, listener: listener, h: h, ml: ml, queryServer: queryServer, queryListener: queryListener, admission: admission, cancelQueries: cancelQueries, runJob: runJob, draining: &draining, mode: mode}).serve(ctx)
}

// The caller owns Store/ClientConn lifetime and closes it only after this
// supervisor has stopped admission, drained handlers and joined owned work.
type lifecycle struct {
	g             *grpc.Server
	listener      net.Listener
	h             *http.Server
	ml            net.Listener
	queryServer   *http.Server
	queryListener net.Listener
	admission     *corerpc.Admission
	cancelQueries context.CancelFunc
	runJob        func(context.Context)
	draining      *atomic.Bool
	mode          string
}

func readyWhileServing(draining *atomic.Bool, ready http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if draining.Load() {
			http.Error(w, "draining", http.StatusServiceUnavailable)
			return
		}
		ready(w, r)
	}
}

func (s lifecycle) serve(ctx context.Context) error {
	g, listener, h, ml := s.g, s.listener, s.h, s.ml
	queryServer, queryListener := s.queryServer, s.queryListener
	admission, cancelQueries, mode := s.admission, s.cancelQueries, s.mode
	errs := make(chan error, 3)
	go func() { errs <- g.Serve(listener) }()
	go func() { errs <- h.Serve(ml) }()
	if queryServer != nil {
		go func() { errs <- queryServer.Serve(queryListener) }()
	}
	var stopJob context.CancelFunc
	var jobDone chan struct{}
	if s.runJob != nil {
		jobCtx, cancelJob := context.WithCancel(ctx)
		jobDone = make(chan struct{})
		stopJob = cancelJob
		go func() { defer close(jobDone); s.runJob(jobCtx) }()
		defer func() { cancelJob(); <-jobDone }()
	}
	log.Printf("ingest ready: mode=%s grpc=%s metrics=%s", mode, listener.Addr(), ml.Addr())
	var result error
	select {
	case <-ctx.Done():
	case e := <-errs:
		if !errors.Is(e, http.ErrServerClosed) && !errors.Is(e, grpc.ErrServerStopped) {
			result = e
		}
	}
	s.draining.Store(true)
	if admission != nil {
		admission.Stop()
	}
	if stopJob != nil {
		stopJob()
	}
	cancelQueries()
	if queryServer != nil {
		drain, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		if err := queryServer.Shutdown(drain); err != nil {
			queryServer.Close()
			if result == nil {
				result = err
			}
		}
		cancel()
	}
	done := make(chan struct{})
	go func() { g.GracefulStop(); close(done) }()
	select {
	case <-done:
	case <-time.After(35 * time.Second):
		g.Stop()
		<-done
	}
	if admission != nil {
		admission.Wait()
	}
	if jobDone != nil {
		<-jobDone
	}
	shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if e := h.Shutdown(shutdown); e != nil {
		h.Close()
		if result == nil {
			result = e
		}
	}
	return result
}

// Explicit incompatible options fail before opening either Store or listeners.
func validateMode(mode string, explicit map[string]bool, password string) error {
	if mode != "embedded" && mode != "core" && mode != "frontend" {
		return fmt.Errorf("unknown mode")
	}
	for n := range explicit {
		if mode == "embedded" && strings.HasPrefix(n, "core-") {
			return fmt.Errorf("core options require split mode")
		}
		if mode == "frontend" && (strings.HasPrefix(n, "clickhouse-") || strings.HasPrefix(n, "journal") || strings.HasPrefix(n, "anomaly-") || strings.HasPrefix(n, "alertmanager-") || n == "database" || n == "core-listen" || n == "core-frontends") {
			return fmt.Errorf("owner option forbidden in frontend: %s", n)
		}
		if mode == "core" && (n == "listen" || n == "tls-cert" || n == "tls-key" || n == "development-cleartext" || n == "query-listen" || n == "core-target") {
			return fmt.Errorf("public/frontend option forbidden in core: %s", n)
		}
	}
	if mode == "frontend" && password != "" {
		return fmt.Errorf("frontend must not receive ClickHouse credentials")
	}
	return nil
}
