package adminbridge

import (
	"bufio"
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestUpstreamResponses(t *testing.T) {
	_, cfg, c := standard(t)
	for _, tc := range []struct {
		name, wire string
		status     int
		body       string
	}{{"error", "HTTP/1.1 503 Service Unavailable\r\nContent-Length: 6\r\n\r\nerror\n", 503, "error\n"}, {"chunked", "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nm 1\n\r\n0\r\n\r\n", 200, "m 1\n"}, {"truncated", "HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\nshort", 502, ""}, {"compressed", "HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 4\r\n\r\nbody", 502, ""}, {"ambiguous", "HTTP/1.1 200 OK\r\nContent-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nbody\r\n0\r\n\r\n", 502, ""}, {"equal-length", "HTTP/1.1 200 OK\r\nContent-Length: 4\r\nContent-Length: 4\r\n\r\nbody", 502, ""}, {"actual-trailers", "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nbody\r\n0\r\nX-Hide: bad\r\n\r\n", 502, ""}, {"conflicting-length", "HTTP/1.1 200 OK\r\nContent-Length: 4\r\nContent-Length: 5\r\n\r\nbody", 502, ""}, {"upgrade", "HTTP/1.1 101 Switching Protocols\r\nUpgrade: other\r\nConnection: upgrade\r\n\r\n", 502, ""}, {"informational", "HTTP/1.1 103 Early Hints\r\n\r\nHTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nbody", 502, ""}, {"large-header", "HTTP/1.1 200 OK\r\nX-Large: " + strings.Repeat("x", 8200) + "\r\nContent-Length: 4\r\n\r\nbody", 502, ""}, {"trailers", "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nTrailer: X-Hide\r\n\r\n4\r\nbody\r\n0\r\nX-Hide: bad\r\n\r\n", 502, ""}} {
		t.Run(tc.name, func(t *testing.T) {
			u := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				conn, _, e := w.(http.Hijacker).Hijack()
				if e != nil {
					t.Error(e)
					return
				}
				defer conn.Close()
				io.WriteString(conn, tc.wire)
			}))
			defer u.Close()
			cfg.Upstream = u.Listener.Addr().String()
			addr, _, _ := serveDirect(t, cfg)
			s, b, e := request(t, addr, c, "/metrics")
			if e != nil || s != tc.status {
				t.Fatalf("status %d err=%v body=%q", s, e, b)
			}
			if tc.body != "" && string(b) != tc.body {
				t.Fatalf("body changed: %q", b)
			}
		})
	}
	var redirected atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { redirected.Add(1) }))
	defer target.Close()
	addr, _, _ := serve(t, cfg, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Location", target.URL)
		w.WriteHeader(302)
	}))
	s, _, e := request(t, addr, c, "/metrics")
	if e != nil || s != 502 || redirected.Load() != 0 {
		t.Fatal("followed redirect", s, e)
	}
}
func serveDirect(t *testing.T, cfg Config) (string, *Bridge, context.CancelFunc) {
	t.Helper()
	b, e := New(cfg)
	if e != nil {
		t.Fatal(e)
	}
	l, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- b.Serve(ctx, l) }()
	t.Cleanup(func() {
		cancel()
		select {
		case e := <-done:
			if e != nil {
				t.Error(e)
			}
		case <-time.After(3 * time.Second):
			t.Error("server cleanup timeout")
		}
	})
	return l.Addr().String(), b, cancel
}
func TestConnectionFetchLimitsAndCancel(t *testing.T) {
	_, cfg, c := standard(t)
	entered := make(chan struct{}, 4)
	var active atomic.Int32
	addr, b, _ := serve(t, cfg, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		active.Add(1)
		defer active.Add(-1)
		entered <- struct{}{}
		<-r.Context().Done()
	}))
	conns := make([]*tls.Conn, 2)
	for i := range conns {
		v, e := tls.Dial("tcp", addr, c)
		if e != nil {
			t.Fatal(e)
		}
		conns[i] = v
		t.Cleanup(func() { v.Close() })
		fmt.Fprintf(v, "GET /metrics HTTP/1.1\r\nHost: local\r\n\r\n")
		select {
		case <-entered:
		case <-time.After(time.Second):
			t.Fatal("fetch not entered")
		}
	}
	start := time.Now()
	s, _, e := request(t, addr, c, "/metrics")
	if e != nil || s != 503 || time.Since(start) > time.Second {
		t.Fatal("nonwaiting fetch refusal", s, e)
	}
	for _, v := range conns {
		v.Close()
	}
	until := time.Now().Add(time.Second)
	for len(b.fetches) != 0 && time.Now().Before(until) {
		time.Sleep(time.Millisecond)
	}
	if len(b.fetches) != 0 {
		t.Fatal("fetch permits leaked")
	}
	// All four idle/handshaking connections consume the connection budget.
	raw := make([]net.Conn, 4)
	for i := range raw {
		v, e := net.Dial("tcp", addr)
		if e != nil {
			t.Fatal(e)
		}
		raw[i] = v
		t.Cleanup(func() { v.Close() })
	}
	time.Sleep(30 * time.Millisecond)
	fifth, e := net.Dial("tcp", addr)
	if e != nil {
		t.Fatal(e)
	}
	fifth.SetReadDeadline(time.Now().Add(time.Second))
	n, e := fifth.Read(make([]byte, 1))
	fifth.Close()
	if n != 0 || e == nil {
		t.Fatal("fifth connection accepted")
	}
	if ne, ok := e.(net.Error); ok && ne.Timeout() {
		t.Fatal("fifth connection queued")
	}
	for _, v := range raw {
		v.Close()
	}
	time.Sleep(30 * time.Millisecond)
	v, e := tls.Dial("tcp", addr, c)
	if e != nil {
		t.Fatal("connection permit not reusable", e)
	}
	v.Close()
	t.Logf("fetch and connection permits reused, active_upstream=%d", active.Load())
}
func TestSlowPhasesHeaderBoundAndShutdown(t *testing.T) {
	_, cfg, c := standard(t)
	var calls atomic.Int32
	addr, _, cancel := serve(t, cfg, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); w.Write([]byte("ok")) }))
	for _, kind := range []string{"handshake", "headers", "body"} {
		t.Run(kind, func(t *testing.T) {
			var conn net.Conn
			var e error
			if kind == "handshake" {
				conn, e = net.Dial("tcp", addr)
			} else {
				conn, e = tls.Dial("tcp", addr, c)
			}
			if e != nil {
				t.Fatal(e)
			}
			defer conn.Close()
			conn.SetDeadline(time.Now().Add(3 * time.Second))
			start := time.Now()
			switch kind {
			case "headers":
				io.WriteString(conn, "GET /metrics HTTP/1.1\r\nHost:")
			case "body":
				io.WriteString(conn, "POST /metrics HTTP/1.1\r\nHost: local\r\nContent-Length: 1000000\r\n\r\nx")
			}
			buf := make([]byte, 1024)
			conn.Read(buf)
			if time.Since(start) > 2800*time.Millisecond {
				t.Fatal("slow phase not bounded")
			}
		})
	}
	for _, size := range []int{8166, 8167, 13000} {
		v, e := tls.Dial("tcp", addr, c)
		if e != nil {
			t.Fatal(e)
		}
		v.SetDeadline(time.Now().Add(3 * time.Second))
		fmt.Fprintf(v, "GET /metrics HTTP/1.1\r\nHost: local\r\nX-Large: %s\r\n\r\n", strings.Repeat("x", size))
		r, e := http.ReadResponse(bufio.NewReader(v), nil)
		if size == 8166 {
			if e != nil || r.StatusCode != 200 {
				t.Fatal("normalized boundary valid rejected", e)
			}
		} else if e == nil && r.StatusCode == 200 {
			t.Fatal("large headers accepted")
		}
		if r != nil {
			r.Body.Close()
		}
		v.Close()
	}
	cancel()
	t.Logf("only valid header request reached upstream: %d", calls.Load())
	if calls.Load() != 1 {
		t.Fatal("invalid phase forwarded")
	}
}

type failListener struct {
	net.Listener
	once sync.Once
	err  error
}

func (l *failListener) Accept() (net.Conn, error) {
	l.once.Do(func() { l.Listener.Close() })
	return nil, l.err
}
func TestServeFailureAndCancellationJoin(t *testing.T) {
	_, cfg, c := standard(t)
	u := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { <-r.Context().Done() }))
	defer u.Close()
	cfg.Upstream = u.Listener.Addr().String()
	b, e := New(cfg)
	if e != nil {
		t.Fatal(e)
	}
	l, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	wanted := errors.New("owned accept failure")
	if e = b.Serve(context.Background(), &failListener{Listener: l, err: wanted}); !errors.Is(e, wanted) {
		t.Fatal("lost accept error", e)
	}
	conn, e := net.DialTimeout("tcp", l.Addr().String(), time.Second)
	if e == nil {
		conn.Close()
		t.Fatal("failed listener survived")
	}
	entered := make(chan struct{})
	addr, b, cancel := serve(t, cfg, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { close(entered); <-r.Context().Done() }))
	done := make(chan struct{})
	go func() { defer close(done); request(t, addr, c, "/metrics") }()
	<-entered
	start := time.Now()
	cancel()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("client survived shutdown")
	}
	until := time.Now().Add(time.Second)
	for len(b.fetches) != 0 && time.Now().Before(until) {
		time.Sleep(time.Millisecond)
	}
	if len(b.fetches) != 0 {
		t.Fatal("user survived cancel")
	}
	t.Logf("shutdown joined after %s", time.Since(start))
}
func TestCoreReadyContract(t *testing.T) {
	positives := []string{"{\"windows\":{},\"journal_records\":0,\"journal_bytes\":0}\n", "{\"windows\":{\"-300\":[\"a\u0026b\"],\"300\":[\"b\",\"a\"]},\"journal_records\":12,\"journal_bytes\":4096}\n"}
	positives[1] = strings.Replace(positives[1], "a&b", "a\\u0026b", 1)
	for _, body := range positives {
		if !validCoreReady([]byte(body)) {
			t.Fatalf("independent producer-shaped literal rejected %q", body)
		}
		u := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, body) }))
		e := CheckReady(context.Background(), u.Listener.Addr().String(), "core")
		u.Close()
		if e != nil {
			t.Fatal(e)
		}
	}
	invalid := []string{"ready\n", "{}\n", "{\"windows\":null,\"journal_records\":0,\"journal_bytes\":0}\n", "{\"windows\":{},\"journal_records\":-1,\"journal_bytes\":0}\n", "{\"windows\":{},\"journal_records\":0,\"journal_records\":0,\"journal_bytes\":0}\n", "{\"windows\":{\"301\":[\"a\"]},\"journal_records\":0,\"journal_bytes\":0}\n", "{\"windows\":{\"0300\":[\"a\"]},\"journal_records\":0,\"journal_bytes\":0}\n", "{\"windows\":{\"300\":[]},\"journal_records\":0,\"journal_bytes\":0}\n", "{\"windows\":{\"300\":[\"a\",\"a\"]},\"journal_records\":0,\"journal_bytes\":0}\n", positives[0] + "{}", strings.TrimSpace(positives[0]), strings.Replace(positives[0], "0", "null", 1), strings.Replace(positives[0], "0", "0.0", 1), strings.Replace(positives[0], "{}", "{\"300\":[\"\"]}", 1)}
	for _, body := range invalid {
		if validCoreReady([]byte(body)) {
			t.Fatalf("bad body accepted %q", body)
		}
	}
	for _, n := range []int{1048576, 1048577} {
		prefix := "{\"windows\":{\"300\":[\""
		suffix := "\"]},\"journal_records\":0,\"journal_bytes\":0}\n"
		body := prefix + strings.Repeat("x", n-len(prefix)-len(suffix)) + suffix
		u := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, body) }))
		e := CheckReady(context.Background(), u.Listener.Addr().String(), "core")
		u.Close()
		if (e == nil) != (n == 1048576) {
			t.Fatalf("core inclusive limit n=%d err=%v", n, e)
		}
	}
}
func TestReadyTimeoutTruncatedAndAddress(t *testing.T) {
	for _, address := range []string{"localhost:9091", "127.0.0.2:9091", "127.0.0.1:09091", "http://127.0.0.1:9091", "127.0.0.1:0", "[::1%lo]:9091", "[::ffff:127.0.0.1]:9091"} {
		if CheckReady(context.Background(), address, "frontend") == nil {
			t.Fatal("bad endpoint accepted", address)
		}
	}
	for _, kind := range []string{"timeout", "truncated"} {
		u := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if kind == "timeout" {
				<-r.Context().Done()
				return
			}
			conn, _, _ := w.(http.Hijacker).Hijack()
			defer conn.Close()
			io.WriteString(conn, "HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nrea")
		}))
		start := time.Now()
		e := CheckReady(context.Background(), u.Listener.Addr().String(), "frontend")
		u.Close()
		if e == nil || time.Since(start) > 3*time.Second {
			t.Fatal("readiness failure not bounded", kind, e)
		}
	}
}

func TestRawHeaderBoundaryReplayAndCancellation(t *testing.T) {
	_, cfg, c := standard(t)
	for _, kind := range []string{"exact", "excess", "fragmented", "incomplete"} {
		t.Run(kind, func(t *testing.T) {
			closed := make(chan struct{})
			u := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				conn, _, e := w.(http.Hijacker).Hijack()
				if e != nil {
					t.Error(e)
					close(closed)
					return
				}
				defer close(closed)
				defer conn.Close()
				conn.SetDeadline(time.Now().Add(3 * time.Second))
				if kind == "incomplete" {
					io.WriteString(conn, "HTTP/1.1 200 OK\r\nX-Slow: unfinished")
					_, e = conn.Read(make([]byte, 1))
					if ne, ok := e.(net.Error); ok && ne.Timeout() {
						t.Error("upstream connection not cancelled")
					}
					return
				}
				prefix, suffix := "HTTP/1.1 200 OK\r\nX-Pad: ", "\r\nContent-Length: 4\r\n\r\n"
				n := 8192 - len(prefix) - len(suffix)
				if kind == "excess" {
					n++
				}
				wire := prefix + strings.Repeat("x", n) + suffix + "m 1\n"
				if kind == "fragmented" {
					for _, part := range []string{wire[:7], wire[7:3000], wire[3000:8191], wire[8191:]} {
						io.WriteString(conn, part)
						time.Sleep(time.Millisecond)
					}
				} else {
					io.WriteString(conn, wire)
				}
			}))
			defer u.Close()
			cfg.Upstream = u.Listener.Addr().String()
			addr, _, _ := serveDirect(t, cfg)
			start := time.Now()
			s, body, e := request(t, addr, c, "/metrics")
			if kind == "exact" || kind == "fragmented" {
				if e != nil || s != 200 || string(body) != "m 1\n" {
					t.Fatalf("bounded replay status=%d body=%q err=%v", s, body, e)
				}
			} else if e == nil && s == 200 {
				t.Fatal("invalid raw header succeeded")
			}
			select {
			case <-closed:
			case <-time.After(time.Second):
				t.Fatal("raw upstream survived request")
			}
			if time.Since(start) > 2800*time.Millisecond {
				t.Fatal("raw header deadline exceeded")
			}
		})
	}
}
