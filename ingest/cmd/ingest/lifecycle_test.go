package main

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/tap"
	"pathwatch.local/pathwatch/ingest/internal/corerpc"
	cp "pathwatch.local/pathwatch/proto/pathwatch/core/v1"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
)

// This handler deliberately never returns an ACK. It tests lifetime ordering,
// not durability; the native fixture separately exercises the actual Store.
type heldReport struct {
	pb.UnimplementedIngestServer
	entered chan struct{}
	release chan struct{}
}

func (h *heldReport) Report(context.Context, *pb.ReportRequest) (*pb.ReportResponse, error) {
	close(h.entered)
	<-h.release
	return nil, status.Error(codes.Unavailable, "lifecycle test released")
}

func TestServeFailureAndCancellationDrain(t *testing.T) {
	for _, mode := range []string{"embedded", "core", "frontend"} {
		for _, failure := range []string{"rpc", "admin", "query", "cancel"} {
			if mode == "core" && failure == "query" {
				continue // Core has no public query listener.
			}
			t.Run(mode+"/"+failure, func(t *testing.T) {
				listen := func() net.Listener {
					l, err := net.Listen("tcp", "127.0.0.1:0")
					if err != nil {
						t.Fatal(err)
					}
					t.Cleanup(func() { l.Close() })
					return l
				}
				await := func(ch <-chan struct{}, label string) {
					t.Helper()
					select {
					case <-ch:
					case <-time.After(3 * time.Second):
						t.Fatal("timeout: " + label)
					}
				}
				ctx, cancel := context.WithCancel(context.Background())
				t.Cleanup(cancel)
				queryContext, cancelQueries := context.WithCancel(ctx)
				t.Cleanup(cancelQueries)
				var draining atomic.Bool
				var admission *corerpc.Admission
				var options []grpc.ServerOption
				if mode != "embedded" {
					admission = corerpc.NewAdmission(mode == "core", nil, nil, nil)
					options = admission.Options(nil)
				}
				g := grpc.NewServer(options...)
				t.Cleanup(g.Stop)
				held := &heldReport{entered: make(chan struct{}), release: make(chan struct{})}
				var releaseRPC sync.Once
				t.Cleanup(func() { releaseRPC.Do(func() { close(held.release) }) })
				if mode == "core" {
					corerpc.Register(g, &cp.Core_ServiceDesc, &corerpc.Service{Reporter: held})
				} else if mode == "frontend" {
					corerpc.Register(g, &pb.Ingest_ServiceDesc, held)
				} else {
					pb.RegisterIngestServer(g, held)
				}
				rpc, admin := listen(), listen()
				h := &http.Server{Handler: readyWhileServing(&draining, func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ready\n")) })}
				t.Cleanup(func() { h.Close() })
				s := lifecycle{g: g, listener: rpc, h: h, ml: admin, admission: admission, cancelQueries: cancelQueries, draining: &draining, mode: mode}
				queryEntered := make(chan struct{})
				queryDone := make(chan struct{})
				if mode != "core" {
					s.queryListener = listen()
					s.queryServer = &http.Server{BaseContext: func(net.Listener) context.Context { return queryContext }, Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						close(queryEntered)
						<-r.Context().Done()
						http.Error(w, "canceled", http.StatusServiceUnavailable)
					})}
					t.Cleanup(func() { s.queryServer.Close() })
				}
				jobStarted, jobCanceled, jobRelease := make(chan struct{}), make(chan struct{}), make(chan struct{})
				var releaseJob sync.Once
				t.Cleanup(func() { releaseJob.Do(func() { close(jobRelease) }) })
				if mode != "frontend" {
					s.runJob = func(jobContext context.Context) {
						close(jobStarted)
						<-jobContext.Done()
						close(jobCanceled)
						<-jobRelease
					}
				}
				var ownerClosed atomic.Bool
				finished := make(chan error, 1)
				go func() {
					// Same caller lifetime as run's deferred Store.Close: it must not
					// execute while the common supervisor still owns active work.
					err := func() error {
						if mode != "frontend" {
							defer ownerClosed.Store(true)
						}
						return s.serve(ctx)
					}()
					finished <- err
				}()
				if s.runJob != nil {
					await(jobStarted, "job started")
				}
				client := &http.Client{Transport: &http.Transport{Proxy: nil}, Timeout: time.Second}
				t.Cleanup(client.CloseIdleConnections)
				readyURL := "http://" + admin.Addr().String() + "/readyz"
				ready := func() (int, bool) {
					t.Helper()
					var reused bool
					r, _ := http.NewRequest("GET", readyURL, nil)
					r = r.WithContext(httptrace.WithClientTrace(r.Context(), &httptrace.ClientTrace{GotConn: func(c httptrace.GotConnInfo) { reused = c.Reused }}))
					res, err := client.Do(r)
					if err != nil {
						t.Fatal(err)
					}
					io.Copy(io.Discard, res.Body)
					res.Body.Close()
					return res.StatusCode, reused
				}
				if code, _ := ready(); code != 200 {
					t.Fatal(code)
				}
				if s.queryServer != nil {
					go func() {
						defer close(queryDone)
						res, err := client.Get("http://" + s.queryListener.Addr().String() + "/v1/raw")
						if err == nil {
							io.Copy(io.Discard, res.Body)
							res.Body.Close()
						}
					}()
					await(queryEntered, "active query")
				}
				cc, err := grpc.NewClient(rpc.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithNoProxy(), grpc.WithDisableRetry())
				if err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { cc.Close() })
				callCtx, stopCall := context.WithTimeout(context.Background(), 5*time.Second)
				t.Cleanup(stopCall)
				method := "/pathwatch.v1.Ingest/Report"
				if mode == "core" {
					method = corerpc.CorePrefix + "Report"
					callCtx = metadata.NewOutgoingContext(callCtx, metadata.Pairs("pathwatch-core-major", "1", "pathwatch-core-minor", "0"))
				}
				callDone := make(chan error, 1)
				go func() { callDone <- cc.Invoke(callCtx, method, &pb.ReportRequest{}, &pb.ReportResponse{}) }()
				await(held.entered, "active RPC")
				switch failure {
				case "rpc":
					rpc.Close()
				case "admin":
					admin.Close()
				case "query":
					s.queryListener.Close()
				case "cancel":
					cancel()
				}
				await(queryContext.Done(), "query cancellation after failure")
				// Cancellation of the outer context can precede the supervisor's
				// select. Wait for its own atomic drain marker before asserting it.
				deadline := time.Now().Add(time.Second)
				for !draining.Load() && time.Now().Before(deadline) {
					time.Sleep(time.Millisecond)
				}
				if !draining.Load() {
					t.Fatal("did not mark draining")
				}
				if code, reused := ready(); code != 503 || !reused {
					t.Fatalf("held-RPC readiness code=%d keepalive=%v", code, reused)
				}
				if admission != nil {
					_, err := admission.Tap(context.Background(), &tap.Info{FullMethodName: method})
					if status.Code(err) != codes.Unavailable {
						t.Fatalf("admission still open: %v", err)
					}
				}
				if s.queryServer != nil {
					await(queryDone, "query handler canceled")
				}
				if s.runJob != nil {
					await(jobCanceled, "job canceled")
				}
				select {
				case err := <-finished:
					t.Fatalf("returned before active RPC: %v", err)
				case <-time.After(10 * time.Millisecond):
				}
				if ownerClosed.Load() {
					t.Fatal("owner closed during active RPC")
				}
				releaseRPC.Do(func() { close(held.release) })
				select {
				case err := <-callDone:
					if status.Code(err) != codes.Unavailable {
						t.Fatal(err)
					}
				case <-time.After(time.Second):
					t.Fatal("RPC did not exit")
				}
				if s.runJob != nil {
					select {
					case err := <-finished:
						t.Fatalf("returned before owned job: %v", err)
					case <-time.After(10 * time.Millisecond):
					}
					if ownerClosed.Load() {
						t.Fatal("owner closed during job")
					}
				}
				releaseJob.Do(func() { close(jobRelease) })
				select {
				case err := <-finished:
					if failure == "cancel" {
						if err != nil {
							t.Fatal(err)
						}
					} else if !errors.Is(err, net.ErrClosed) {
						t.Fatalf("lost Serve error: %v", err)
					}
				case <-time.After(3 * time.Second):
					t.Fatal("supervisor did not exit")
				}
				if ownerClosed.Load() != (mode != "frontend") {
					t.Fatal("wrong owner close lifetime")
				}
				for _, l := range []net.Listener{rpc, admin, s.queryListener} {
					if l == nil {
						continue
					}
					c, err := net.DialTimeout("tcp", l.Addr().String(), 100*time.Millisecond)
					if err == nil {
						c.Close()
						t.Fatal("listener leaked: " + l.Addr().String())
					}
				}
			})
		}
	}
}
