package main

import (
	"bytes"
	"context"
	"fmt"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"pathwatch.local/pathwatch/ingest/internal/corerpc"
	"pathwatch.local/pathwatch/ingest/internal/frontend"
	"pathwatch.local/pathwatch/ingest/internal/server"
	"pathwatch.local/pathwatch/ingest/internal/store"
	cp "pathwatch.local/pathwatch/proto/pathwatch/core/v1"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"strings"
	"sync/atomic"
	"time"
)

func pending(dir string) error {
	endpoint, container, work := os.Getenv("PATHWATCH_CORE_CH"), os.Getenv("PATHWATCH_CORE_CONTAINER"), os.Getenv("PATHWATCH_CORE_WORK")
	if endpoint == "" || container == "" || work == "" {
		return fmt.Errorf("owned pending fixture environment required")
	}
	label, e := exec.Command("docker", "inspect", "--format", `{{index .Config.Labels "purpose"}}`, container).Output()
	if e != nil || strings.TrimSpace(string(label)) != "task14-core-rpc" {
		return fmt.Errorf("owned container required")
	}
	client := &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{Proxy: nil, DisableKeepAlives: true}}
	sql := func(q string) (string, error) {
		r, e := client.Post(endpoint, "text/plain", strings.NewReader(q))
		if e != nil {
			return "", e
		}
		defer r.Body.Close()
		b, e := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if e != nil || r.StatusCode != 200 {
			return "", fmt.Errorf("query: %d %s %v", r.StatusCode, b, e)
		}
		return strings.TrimSpace(string(b)), nil
	}
	if _, e = sql("CREATE DATABASE pending ENGINE=Atomic"); e != nil {
		return e
	}
	var dropped atomic.Bool
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, e := io.ReadAll(io.LimitReader(r.Body, 16<<20))
		if e != nil {
			http.Error(w, "body", 502)
			return
		}
		up, e := http.NewRequestWithContext(r.Context(), r.Method, endpoint+r.URL.RequestURI(), bytes.NewReader(b))
		if e != nil {
			http.Error(w, "request", 502)
			return
		}
		up.Header = r.Header.Clone()
		resp, e := client.Do(up)
		if e != nil {
			http.Error(w, "upstream", 502)
			return
		}
		out, e := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		resp.Body.Close()
		if e != nil {
			http.Error(w, "response", 502)
			return
		}
		if strings.HasPrefix(string(b), "INSERT INTO probe_results") && dropped.CompareAndSwap(false, true) {
			c, _, e := w.(http.Hijacker).Hijack()
			if e == nil {
				c.Close()
			}
			return
		}
		for k, v := range resp.Header {
			w.Header()[k] = v
		}
		w.WriteHeader(resp.StatusCode)
		w.Write(out)
	}))
	defer proxy.Close()
	cfg := store.Config{URL: proxy.URL, Database: "pending", Username: "default", JournalDir: filepath.Join(work, "pending-journal"), MaxJournalBytes: 4 << 20}
	st, e := store.Open(context.Background(), cfg)
	if e != nil {
		return e
	}
	defer func() {
		if st != nil {
			st.Close()
		}
	}()
	identity := st.OwnerIdentity()
	own := server.New(st, time.Now)
	tr, e := trust(dir, "core", true)
	if e != nil {
		return e
	}
	a := corerpc.NewAdmission(true, tr.Authenticate, own.Overloaded, own.SemanticRejected)
	g := grpc.NewServer(a.Options(tr.Credentials())...)
	corerpc.Register(g, &cp.Core_ServiceDesc, &corerpc.Service{Reporter: own, Reader: st, Ready: func(ctx context.Context) error { _, _, e := st.MonitoringSnapshot(ctx); return e }})
	l, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		return e
	}
	done := make(chan struct{})
	go func() { defer close(done); g.Serve(corerpc.LimitListener(l, 10)) }()
	stopped := false
	stop := func() {
		if !stopped {
			a.Stop()
			g.Stop()
			<-done
			a.Wait()
			stopped = true
		}
	}
	defer stop()
	ct, e := trust(dir, "stable", false)
	if e != nil {
		return e
	}
	rpc, e := corerpc.Dial(context.Background(), l.Addr().String(), ct)
	if e != nil {
		return e
	}
	defer rpc.Close()
	front := frontend.New(rpc, time.Now)
	r := request("pending-loss", 10, time.Now().Add(-10*time.Minute).Unix())
	if _, e = front.Report(context.Background(), r); status.Code(e) != codes.Unavailable {
		return fmt.Errorf("pending loss must unavailable: %v", e)
	}
	b := store.QueryBounds{PathKey: "home→target/tcp/v4", From: time.Unix(0, 0), To: time.Unix(300, 0), Limit: 1}
	if _, e = rpc.ReadRaw(context.Background(), b); e != store.ErrQueryUnavailable {
		return fmt.Errorf("pending read accepted: %v", e)
	}
	stop()
	rpc.Close()
	st.Close()
	st = nil
	if reopened, e := store.Open(context.Background(), cfg); e == nil {
		reopened.Close()
		return fmt.Errorf("same epoch recovered uncertain write")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	out, e := exec.CommandContext(ctx, "docker", "restart", "--timeout", "5", container).CombinedOutput()
	if e != nil {
		return fmt.Errorf("owned restart: %s %w", out, e)
	}
	for {
		if _, e = sql("SELECT 1"); e == nil {
			break
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		time.Sleep(100 * time.Millisecond)
	}
	st, e = store.Open(ctx, cfg)
	if e != nil {
		return e
	}
	if st.OwnerIdentity() != identity {
		return fmt.Errorf("recovery changed owner identity")
	}
	ack, e := server.New(st, time.Now).Report(ctx, r)
	if e != nil || ack.Status != pb.AckStatus_ACK_STATUS_DUPLICATE {
		return fmt.Errorf("recovery duplicate: %v %v", ack, e)
	}
	count, e := sql("SELECT count() FROM pending.probe_results")
	if e != nil || count != "1" {
		return fmt.Errorf("recovery count=%s %v", count, e)
	}
	emit(map[string]any{"pending_unavailable": true, "same_epoch_rejected": true, "recovered_duplicate": true, "owner_identity": identity, "raw_count": count, "recoveries": st.Recoveries()})
	return nil
}
