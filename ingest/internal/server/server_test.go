package server_test

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/go-connections/nat"
	tc "github.com/testcontainers/testcontainers-go"
	tcexec "github.com/testcontainers/testcontainers-go/exec"
	chc "github.com/testcontainers/testcontainers-go/modules/clickhouse"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"pathwatch.local/pathwatch/ingest/internal/server"
	"pathwatch.local/pathwatch/ingest/internal/store"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
)

const image = "clickhouse/clickhouse-server:26.3.32.14@sha256:456063a689194186633bb3db0862283068f4c2ee538852d3aaffa7eb66f1f841"

var ch *chc.ClickHouseContainer
var endpoint string
var seq int
var now = time.Now().UTC().Truncate(time.Second)

func freePort() string {
	l, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		panic(e)
	}
	defer l.Close()
	return fmt.Sprint(l.Addr().(*net.TCPAddr).Port)
}
func TestMain(m *testing.M) {
	if os.Getenv("PW_INGEST_WORKER") == "1" || os.Getenv("PW_INGEST_FIXTURE_ONLY") == "1" {
		os.Exit(m.Run())
	}
	os.Setenv("TESTCONTAINERS_RYUK_DISABLED", "true")
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	var err error
	ch, err = chc.Run(ctx, image, chc.WithDatabase("default"), chc.WithPassword("task08-local-test"), chc.WithConfigFile("testdata/clickhouse.xml"), tc.WithHostConfigModifier(func(h *container.HostConfig) {
		h.Resources.Memory = 2 << 30
		h.Resources.MemorySwap = 2 << 30
		h.Resources.NanoCPUs = 2e9
		h.PortBindings = nat.PortMap{"8123/tcp": {{HostIP: "127.0.0.1", HostPort: freePort()}}, "9000/tcp": {{HostIP: "127.0.0.1", HostPort: freePort()}}}
		h.Tmpfs = map[string]string{"/var/log/clickhouse-server": "rw,size=16m"}
		h.LogConfig = container.LogConfig{Type: "json-file", Config: map[string]string{"max-size": "2m", "max-file": "1"}}
	}), tc.CustomizeRequest(tc.GenericContainerRequest{ContainerRequest: tc.ContainerRequest{Name: fmt.Sprintf("pathwatch-task08-clickhouse-%d", os.Getpid()), Labels: map[string]string{"project": "pathwatch", "owner": "codex-pathwatch", "task": "08"}}}))
	cancel()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		if ch != nil {
			_ = ch.Terminate(context.Background())
		}
		os.Exit(1)
	}
	p, err := ch.MappedPort(context.Background(), "8123/tcp")
	if err != nil {
		panic(err)
	}
	endpoint = "http://127.0.0.1:" + p.Port()
	fmt.Println("真实 ClickHouse", image, "容器", ch.GetContainerID())
	if info, e := ch.Inspect(context.Background()); e == nil {
		fmt.Printf("资源配置 memory=%d swap=%d nanoCPU=%d ports=%v volumes=%v\n", info.HostConfig.Memory, info.HostConfig.MemorySwap, info.HostConfig.NanoCPUs, info.HostConfig.PortBindings, info.Mounts)
	}
	code := m.Run()
	if code != 0 {
		dumpServerState()
	}
	if exit, r, e := ch.Exec(context.Background(), []string{"du", "-sb", "/var/lib/clickhouse", "/var/log/clickhouse-server"}, tcexec.Multiplexed()); e == nil && exit == 0 {
		b, _ := io.ReadAll(r)
		fmt.Printf("真实容器磁盘占用\n%s\n", b)
		var total int64
		for _, line := range strings.Split(string(b), "\n") {
			f := strings.Fields(line)
			if len(f) == 2 {
				n, _ := strconv.ParseInt(f[0], 10, 64)
				total += n
			}
		}
		if total > 256<<20 {
			fmt.Fprintln(os.Stderr, "test dataset exceeded 256MiB")
			code = 1
		}
	} else {
		fmt.Fprintln(os.Stderr, "resource accounting failed", e)
		code = 1
	}
	if err := ch.Terminate(context.Background()); err != nil {
		fmt.Fprintln(os.Stderr, err)
		code = 1
	}
	os.Exit(code)
}

// dumpServerState prints the ClickHouse error log and running queries; the log
// lives on tmpfs and is lost once the container is terminated.
func dumpServerState() {
	for _, cmd := range [][]string{
		{"tail", "-n", "200", "/var/log/clickhouse-server/clickhouse-server.err.log"},
		{"clickhouse-client", "--password", "task08-local-test", "--receive_timeout", "10", "-q",
			"SELECT elapsed, query_id, left(query, 200) FROM system.processes FORMAT TSV"},
	} {
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		exit, r, e := ch.Exec(ctx, cmd, tcexec.Multiplexed())
		var b []byte
		if e == nil {
			b, _ = io.ReadAll(r)
		}
		cancel()
		fmt.Fprintf(os.Stderr, "== %s (exit=%d err=%v)\n%s\n", cmd[0], exit, e, b)
	}
}

func sql(t *testing.T, db, q string) string {
	t.Helper()
	req, _ := http.NewRequest("POST", endpoint+"/?database="+db, strings.NewReader(q))
	req.SetBasicAuth("default", "task08-local-test")
	r, e := http.DefaultClient.Do(req)
	if e != nil {
		t.Fatal(e)
	}
	defer r.Body.Close()
	b, _ := io.ReadAll(r.Body)
	if r.StatusCode != 200 {
		t.Fatalf("SQL %s: %s", q, b)
	}
	return strings.TrimSpace(string(b))
}
func fixture(t *testing.T) (store.Config, *store.Store, *server.Server) {
	return fixtureURL(t, endpoint)
}
func fixtureURL(t *testing.T, url string) (store.Config, *store.Store, *server.Server) {
	t.Helper()
	seq++
	db := fmt.Sprintf("task08_%d", seq)
	sql(t, "default", "CREATE DATABASE "+db)
	cfg := store.Config{URL: url, Database: db, Username: "default", Password: "task08-local-test", JournalDir: t.TempDir(), MaxJournalBytes: 32 << 20}
	st, e := store.Open(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { st.Close() })
	return cfg, st, server.New(st, func() time.Time { return now })
}
func batch(id string, n int) *pb.ReportRequest {
	r := &pb.ReportRequest{ProbeId: "home", BatchId: id, SentAt: timestamppb.New(now)}
	for i := 1; i <= n; i++ {
		r.Results = append(r.Results, &pb.ProbeResult{ProbeId: "home", TargetId: "owned", Kind: "tcp", Family: "v4", Addr: "127.0.0.1", StartedAt: timestamppb.New(now), PhasesNanoseconds: map[string]int64{"connect": int64(i) * 1e6}})
	}
	return r
}
func counts(t *testing.T, db string, n int) {
	t.Helper()
	if got := sql(t, db, "SELECT count() FROM probe_results"); got != fmt.Sprint(n) {
		t.Fatalf("raw=%s want=%d", got, n)
	}
	if got := sql(t, db, "SELECT countMerge(samples) FROM path_quantiles_5m"); got != fmt.Sprint(n) {
		t.Fatalf("MV=%s want=%d", got, n)
	}
}
func TestIngest_DedupBatch(t *testing.T) {
	cfg, _, s := fixture(t)
	r := batch("dedup", 3)
	a, e := s.Report(context.Background(), r)
	if e != nil || a.Status != pb.AckStatus_ACK_STATUS_ACCEPTED || a.CommittedResultCount != 3 {
		t.Fatalf("first=%v %v", a, e)
	}
	r.SentAt = timestamppb.New(now.Add(time.Second))
	a, e = s.Report(context.Background(), r)
	if e != nil || a.Status != pb.AckStatus_ACK_STATUS_DUPLICATE || a.CommittedResultCount != 3 {
		t.Fatalf("retry=%v %v", a, e)
	}
	counts(t, cfg.Database, 3)
}
func TestIngest_RejectsClockSkew(t *testing.T) {
	cfg, _, s := fixture(t)
	r := batch("skew", 1)
	r.SentAt = timestamppb.New(now.Add(-10 * time.Minute))
	_, e := s.Report(context.Background(), r)
	if status.Code(e) != codes.FailedPrecondition {
		t.Fatal(e)
	}
	found := false
	for _, d := range status.Convert(e).Details() {
		if x, ok := d.(*errdetails.ErrorInfo); ok && x.Reason == "clock_skew" {
			found = true
		}
	}
	if !found {
		t.Fatalf("missing clock_skew detail: %v", e)
	}
	counts(t, cfg.Database, 0)
}
func TestQuantilesMV(t *testing.T) {
	cfg, _, s := fixture(t)
	if _, e := s.Report(context.Background(), batch("quantiles", 1000)); e != nil {
		t.Fatal(e)
	}
	var q []float64
	if e := json.Unmarshal([]byte(sql(t, cfg.Database, "SELECT quantilesMerge(0.5,0.95,0.99)(phase_ms) FROM path_quantiles_5m")), &q); e != nil {
		t.Fatal(e)
	}
	if len(q) != 3 || q[1] < 940 || q[1] > 960 {
		t.Fatal(q)
	}
	t.Logf("真实p50/p95/p99=%v", q)
	t.Log("原始SQL样本:", sql(t, cfg.Database, "SELECT path_key,started_at,phases_nanoseconds,phase_ms FROM probe_results ORDER BY result_index LIMIT 3 FORMAT JSONEachRow"))
	t.Log("raw SHOW CREATE:", sql(t, cfg.Database, "SHOW CREATE TABLE probe_results"))
	t.Log("aggregate SHOW CREATE:", sql(t, cfg.Database, "SHOW CREATE TABLE path_quantiles_5m"))
	t.Log("版本/default fsync:", sql(t, cfg.Database, "SELECT version(),name,value FROM system.merge_tree_settings WHERE name IN ('fsync_after_insert','fsync_part_directory','min_rows_to_fsync_after_merge') FORMAT JSONEachRow"))
	counts(t, cfg.Database, 1000)
}

func TestIngest_ConcurrentConflictAndRestart(t *testing.T) {
	cfg, st, s := fixture(t)
	r := batch("concurrent", 7)
	var wg sync.WaitGroup
	errs := make(chan error, 12)
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, e := s.Report(context.Background(), proto.Clone(r).(*pb.ReportRequest))
			errs <- e
		}()
	}
	wg.Wait()
	close(errs)
	for e := range errs {
		if e != nil {
			t.Fatal(e)
		}
	}
	counts(t, cfg.Database, 7)
	st.Close()
	st2, e := store.Open(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer st2.Close()
	s = server.New(st2, func() time.Time { return now })
	a, e := s.Report(context.Background(), r)
	if e != nil || a.Status != pb.AckStatus_ACK_STATUS_DUPLICATE {
		t.Fatalf("%v %v", a, e)
	}
	r.Results[0].PhasesNanoseconds["connect"]++
	if _, e = s.Report(context.Background(), r); status.Code(e) != codes.AlreadyExists {
		t.Fatalf("conflict %v", e)
	}
	counts(t, cfg.Database, 7)
}

func TestIngest_ValidationNoWrites(t *testing.T) {
	cfg, _, s := fixture(t)
	cases := []struct {
		name   string
		change func(*pb.ReportRequest)
	}{
		{"missing_sent", func(r *pb.ReportRequest) { r.SentAt = nil }}, {"invalid_sent", func(r *pb.ReportRequest) { r.SentAt.Nanos = -1 }}, {"identity", func(r *pb.ReportRequest) { r.Results[0].ProbeId = "other" }}, {"negative", func(r *pb.ReportRequest) { r.Results[0].PhasesNanoseconds["connect"] = -1 }}, {"missing_start", func(r *pb.ReportRequest) { r.Results[0].StartedAt = nil }},
		{"future_range", func(r *pb.ReportRequest) {
			r.Results[0].StartedAt = timestamppb.New(time.Date(2200, 1, 1, 0, 0, 0, 0, time.UTC))
		}}, {"family", func(r *pb.ReportRequest) { r.Results[0].Family = "v6" }}, {"oversize", func(r *pb.ReportRequest) { r.Results = make([]*pb.ProbeResult, 10001) }}, {"empty", func(r *pb.ReportRequest) { r.Results = nil }}, {"kind", func(r *pb.ReportRequest) { r.Results[0].Kind = "arbitrary" }},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := batch(c.name, 1)
			c.change(r)
			if _, e := s.Report(context.Background(), r); status.Code(e) != codes.InvalidArgument {
				t.Fatal(e)
			}
		})
	}
	counts(t, cfg.Database, 0)
	for i, d := range []time.Duration{-5 * time.Minute, 5 * time.Minute} {
		r := batch(fmt.Sprint("edge", i), 1)
		r.SentAt = timestamppb.New(now.Add(d))
		if _, e := s.Report(context.Background(), r); e != nil {
			t.Fatal(e)
		}
	}
	counts(t, cfg.Database, 2)
}

// Loss after the real INSERT response must freeze the writer until CH restarts.
func TestIngest_AckLossRequiresRestartFence(t *testing.T) {
	var lost atomic.Bool
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		forwardFaultRequest(w, r, endpoint+r.URL.RequestURI(), b, func() bool {
			if strings.HasPrefix(string(b), "INSERT INTO probe_results") && lost.CompareAndSwap(false, true) {
				dropFaultResponse(w)
				return true
			}
			return false
		})
	}))
	defer proxy.Close()
	// Endpoint identity is the real database UUID, URL changes are prohibited in normal operation.
	cfg, st, s := fixtureURL(t, proxy.URL)
	var e error
	if _, e = s.Report(context.Background(), batch("lost", 4)); status.Code(e) != codes.Unavailable {
		t.Fatalf("expected ambiguous error: %v", e)
	}
	counts(t, cfg.Database, 4)
	st.Close()
	if x, e := store.Open(context.Background(), cfg); e == nil {
		x.Close()
		t.Fatal("same epoch incorrectly recovered")
	}
	restart(t)
	st, e = store.Open(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer st.Close()
	s = server.New(st, func() time.Time { return now })
	a, e := s.Report(context.Background(), batch("lost", 4))
	if e != nil || a.Status != pb.AckStatus_ACK_STATUS_DUPLICATE {
		t.Fatalf("recovered=%v %v", a, e)
	}
	counts(t, cfg.Database, 4)
}
func restart(t *testing.T) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	seconds := time.Second
	if e := ch.Stop(ctx, &seconds); e != nil {
		t.Fatal(e)
	}
	// /ping can return 200 while the new process is still coming up, and a query
	// then sits until the 30s client timeout. Wait until the old port is gone,
	// then until SELECT 1 actually returns.
	down := time.NewTimer(5 * time.Second)
	defer down.Stop()
	ping := &http.Client{Timeout: time.Second}
	for {
		r, e := ping.Get(endpoint + "/ping")
		if e != nil {
			break
		}
		r.Body.Close()
		select {
		case <-down.C:
			goto start
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		case <-time.After(50 * time.Millisecond):
		}
	}
start:
	if e := ch.Start(ctx); e != nil {
		t.Fatal(e)
	}
	client := &http.Client{Timeout: 2 * time.Second}
	for ctx.Err() == nil {
		req, _ := http.NewRequestWithContext(ctx, "POST", endpoint+"/?database=default", strings.NewReader("SELECT 1"))
		req.SetBasicAuth("default", "task08-local-test")
		resp, e := client.Do(req)
		if e == nil {
			b, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			if resp.StatusCode == 200 && strings.TrimSpace(string(b)) == "1" {
				return
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatal(ctx.Err())
}

func TestIngest_RunningWriterRejectsCHRestart(t *testing.T) {
	cfg, st, s := fixture(t)
	restart(t)
	if _, e := s.Report(context.Background(), batch("old-epoch", 2)); status.Code(e) != codes.Unavailable {
		t.Fatalf("old epoch writer accepted after CH restart: %v", e)
	}
	counts(t, cfg.Database, 0)
	st.Close()
	st, e := store.Open(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer st.Close()
	s = server.New(st, func() time.Time { return now })
	if _, e = s.Report(context.Background(), batch("old-epoch", 2)); e != nil {
		t.Fatal(e)
	}
	counts(t, cfg.Database, 2)
}

func TestIngest_PartialMVFailureRecovery(t *testing.T) {
	var inject atomic.Bool
	inject.Store(true)
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		u := *r.URL
		if inject.Load() && strings.HasPrefix(string(b), "INSERT INTO probe_results") {
			q := u.Query()
			q.Set("materialized_views_ignore_errors", "1")
			u.RawQuery = q.Encode()
		}
		forwardFaultRequest(w, r, endpoint+u.RequestURI(), b, nil)
	}))
	defer proxy.Close()
	cfg, st, s := fixtureURL(t, proxy.URL)
	sql(t, cfg.Database, "DROP VIEW probe_quantiles_mv SYNC")
	sql(t, cfg.Database, "CREATE MATERIALIZED VIEW probe_quantiles_mv TO path_quantiles_5m AS SELECT path_key,toStartOfFiveMinutes(started_at) AS window_start,batch_key,quantilesState(0.5,0.95,0.99)(phase_ms) AS phase_ms,countState() AS samples FROM probe_results WHERE throwIf(result_index >= 0,'injected MV failure')=0 GROUP BY path_key,window_start,batch_key")
	if _, e := s.Report(context.Background(), batch("partial", 3)); status.Code(e) != codes.Unavailable {
		t.Fatalf("partial insert ACKed: %v", e)
	}
	raw := sql(t, cfg.Database, "SELECT count() FROM probe_results")
	agg := sql(t, cfg.Database, "SELECT countMerge(samples) FROM path_quantiles_5m")
	t.Logf("真实MV执行异常后的raw=%s aggregate_samples=%s", raw, agg)
	if raw != "3" || agg != "0" {
		t.Fatalf("fixture did not produce partial commit: %s/%s", raw, agg)
	}
	st.Close()
	inject.Store(false)
	sql(t, cfg.Database, "DROP VIEW probe_quantiles_mv SYNC")
	restart(t)
	st, e := store.Open(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer st.Close()
	s = server.New(st, func() time.Time { return now })
	a, e := s.Report(context.Background(), batch("partial", 3))
	if e != nil || a.Status != pb.AckStatus_ACK_STATUS_DUPLICATE {
		t.Fatalf("%v %v", a, e)
	}
	counts(t, cfg.Database, 3)
}
func TestIngest_OwnershipAndDBReplacement(t *testing.T) {
	cfg, st, _ := fixture(t)
	if other, e := store.Open(context.Background(), cfg); e == nil {
		other.Close()
		t.Fatal("two OS owners")
	}
	otherCfg := cfg
	otherCfg.JournalDir = t.TempDir()
	if other, e := store.Open(context.Background(), otherCfg); e == nil {
		other.Close()
		t.Fatal("second journal claimed database")
	}
	st.Close()
	sql(t, "default", "DROP DATABASE "+cfg.Database+" SYNC")
	sql(t, "default", "CREATE DATABASE "+cfg.Database)
	if other, e := store.Open(context.Background(), cfg); e == nil {
		other.Close()
		t.Fatal("database UUID replacement accepted")
	}
}
func TestIngest_RawFidelityAndMissingPrimary(t *testing.T) {
	cfg, _, s := fixture(t)
	r := batch("fidelity", 1)
	v := r.Results[0]
	v.Kind = "tls"
	v.Family = "v6"
	v.Addr = "::1"
	v.StartedAt = timestamppb.New(now.Add(123456789))
	v.PhasesNanoseconds = map[string]int64{"tls_handshake": 9223372036854775807, "dns": 123}
	v.Protocol = "h2"
	v.TlsVersion = 0x0304
	v.TlsResumed = true
	v.Err = "remote failure retained"
	v.Hops = []*pb.Hop{{Ttl: 1, Addr: "::1", RttNanoseconds: 9007199254740993}, {Ttl: 2}}
	failure := proto.Clone(v).(*pb.ProbeResult)
	failure.Kind = "tcp"
	failure.Addr = ""
	failure.Family = ""
	failure.PhasesNanoseconds = map[string]int64{"resolve": 15}
	failure.Hops = nil
	failure.TlsVersion = 0
	failure.TlsResumed = false
	failure.Protocol = ""
	r.Results = append(r.Results, failure)
	if _, e := s.Report(context.Background(), r); e != nil {
		t.Fatal(e)
	}
	got := sql(t, cfg.Database, "SELECT path_key, toUnixTimestamp64Nano(started_at), phases_nanoseconds['tls_handshake'], hop_rtt_nanoseconds[1], tls_version, tls_resumed, protocol, err FROM probe_results WHERE kind='tls'")
	want := fmt.Sprintf("home→owned/tls/v6\t%d\t9223372036854775807\t9007199254740993\t772\ttrue\th2\tremote failure retained", now.Add(123456789).UnixNano())
	if got != want {
		t.Fatalf("got=%q want=%q", got, want)
	}
	if got := sql(t, cfg.Database, "SELECT count() FROM probe_results"); got != "2" {
		t.Fatal(got)
	}
	if got := sql(t, cfg.Database, "SELECT countMerge(samples) FROM path_quantiles_5m"); got != "1" {
		t.Fatal(got)
	}
	recorder := httptest.NewRecorder()
	s.Metrics(recorder, httptest.NewRequest("GET", "/metrics", nil))
	if !strings.Contains(recorder.Body.String(), "pathwatch_ingest_accepted_batches_total 1") {
		t.Fatal(recorder.Body.String())
	}
}

func TestCrashWorker(t *testing.T) {
	if os.Getenv("PW_INGEST_WORKER") != "1" {
		t.Skip("only subprocess crash worker")
	}
	var cfg store.Config
	if e := json.Unmarshal([]byte(os.Getenv("PW_INGEST_CONFIG")), &cfg); e != nil {
		t.Fatal(e)
	}
	st, e := store.Open(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer st.Close()
	b, _ := base64.StdEncoding.DecodeString(os.Getenv("PW_INGEST_BATCH"))
	var req pb.ReportRequest
	if e = proto.Unmarshal(b, &req); e != nil {
		t.Fatal(e)
	}
	s := server.New(st, func() time.Time { return req.SentAt.AsTime() })
	if _, e = s.Report(context.Background(), &req); e != nil {
		t.Fatal(e)
	}
}
func TestIngest_SIGKILLAfterRawAndMVBeforeACK(t *testing.T) {
	gate := newRequestGate()
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		forwardFaultRequest(w, r, endpoint+r.URL.RequestURI(), b, func() bool {
			if strings.HasPrefix(string(b), "INSERT INTO probe_results") {
				gate.wait()
			}
			return false
		})
	}))
	defer gate.closeProxy(proxy)
	cfg, st, _ := fixtureURL(t, proxy.URL)
	st.Close()
	req := batch("sigkill", 5)
	cfgJSON, _ := json.Marshal(cfg)
	wire, _ := proto.Marshal(req)
	exe, _ := os.Executable()
	cmd := exec.Command(exe, "-test.run=^TestCrashWorker$")
	cmd.Env = append(os.Environ(), "PW_INGEST_WORKER=1", "PW_INGEST_CONFIG="+string(cfgJSON), "PW_INGEST_BATCH="+base64.StdEncoding.EncodeToString(wire))
	if e := cmd.Start(); e != nil {
		t.Fatal(e)
	}
	defer cmd.Process.Kill()
	select {
	case <-gate.reached:
	case <-time.After(15 * time.Second):
		t.Fatal("worker failed to reach committed INSERT")
	}
	counts(t, cfg.Database, 5)
	if e := cmd.Process.Kill(); e != nil {
		t.Fatal(e)
	}
	gate.release()
	if e := cmd.Wait(); e == nil {
		t.Fatal("worker did not die")
	}
	if other, e := store.Open(context.Background(), cfg); e == nil {
		other.Close()
		t.Fatal("SIGKILL pending accepted without CH restart")
	}
	restart(t)
	st, e := store.Open(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer st.Close()
	s := server.New(st, func() time.Time { return now })
	a, e := s.Report(context.Background(), req)
	if e != nil || a.Status != pb.AckStatus_ACK_STATUS_DUPLICATE {
		t.Fatalf("%v %v", a, e)
	}
	counts(t, cfg.Database, 5)
}
func TestIngest_LateSessionWriteRejectedAfterRestart(t *testing.T) {
	gate := newRequestGate()
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		if strings.HasPrefix(string(b), "INSERT INTO probe_results") {
			gate.wait()
		}
		forwardFaultRequest(w, r, endpoint+r.URL.RequestURI(), b, nil)
	}))
	defer gate.closeProxy(proxy)
	cfg, st, s := fixtureURL(t, proxy.URL)
	done := make(chan error, 1)
	go func() { _, e := s.Report(context.Background(), batch("late", 2)); done <- e }()
	select {
	case <-gate.reached:
	case <-time.After(10 * time.Second):
		t.Fatal("not reached")
	}
	restart(t)
	gate.release()
	if e := <-done; status.Code(e) != codes.Unavailable {
		t.Fatalf("late write=%v", e)
	}
	counts(t, cfg.Database, 0)
	st.Close()
	st, e := store.Open(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer st.Close()
	s = server.New(st, func() time.Time { return now })
	a, e := s.Report(context.Background(), batch("late", 2))
	if e != nil || a.Status != pb.AckStatus_ACK_STATUS_DUPLICATE {
		t.Fatalf("%v %v", a, e)
	}
	counts(t, cfg.Database, 2)
}

func TestIngest_RecoveryMutationResponseLoss(t *testing.T) {
	var dropInsert, dropDelete atomic.Bool
	dropInsert.Store(true)
	dropDelete.Store(true)
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		forwardFaultRequest(w, r, endpoint+r.URL.RequestURI(), b, func() bool {
			drop := strings.HasPrefix(string(b), "INSERT INTO probe_results") && dropInsert.CompareAndSwap(true, false)
			drop = drop || (strings.HasPrefix(string(b), "ALTER TABLE probe_results DELETE") && dropDelete.CompareAndSwap(true, false))
			if drop {
				dropFaultResponse(w)
			}
			return drop
		})
	}))
	defer proxy.Close()
	cfg, st, s := fixtureURL(t, proxy.URL)
	if _, e := s.Report(context.Background(), batch("mutation-loss", 3)); status.Code(e) != codes.Unavailable {
		t.Fatal(e)
	}
	st.Close()
	restart(t)
	if other, e := store.Open(context.Background(), cfg); e == nil {
		other.Close()
		t.Fatal("mutation response loss unexpectedly succeeded")
	}
	if got := sql(t, cfg.Database, "SELECT count() FROM probe_results"); got != "0" {
		t.Fatal(got)
	}
	if got := sql(t, cfg.Database, "SELECT countMerge(samples) FROM path_quantiles_5m"); got != "3" {
		t.Fatal(got)
	}
	if other, e := store.Open(context.Background(), cfg); e == nil {
		other.Close()
		t.Fatal("same-epoch interrupted recovery accepted")
	}
	restart(t)
	st, e := store.Open(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer st.Close()
	s = server.New(st, func() time.Time { return now })
	a, e := s.Report(context.Background(), batch("mutation-loss", 3))
	if e != nil || a.Status != pb.AckStatus_ACK_STATUS_DUPLICATE {
		t.Fatalf("%v %v", a, e)
	}
	counts(t, cfg.Database, 3)
}

func TestIngest_CapacityRejectsWithoutEviction(t *testing.T) {
	cfg, st, _ := fixture(t)
	st.Close()
	cfg.MaxJournalBytes = 1024
	st, e := store.Open(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer st.Close()
	s := server.New(st, func() time.Time { return now })
	if _, e = s.Report(context.Background(), batch("capacity", 100)); status.Code(e) != codes.ResourceExhausted {
		t.Fatalf("capacity=%v", e)
	}
	counts(t, cfg.Database, 0)
	a, e := s.Report(context.Background(), batch("small", 1))
	if e != nil || a.Status != pb.AckStatus_ACK_STATUS_ACCEPTED {
		t.Fatalf("%v %v", a, e)
	}
	if _, e = s.Report(context.Background(), batch("capacity", 100)); status.Code(e) != codes.ResourceExhausted {
		t.Fatal(e)
	}
	a, e = s.Report(context.Background(), batch("small", 1))
	if e != nil || a.Status != pb.AckStatus_ACK_STATUS_DUPLICATE {
		t.Fatalf("evicted original: %v %v", a, e)
	}
	counts(t, cfg.Database, 1)
}
