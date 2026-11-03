package server_test

import (
	"bytes"
	"compress/gzip"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// requestGate pauses one request at the case-specific injection point.
// Both early test exit and the normal path use the same idempotent release.
type requestGate struct {
	reached, released       chan struct{}
	arriveOnce, releaseOnce sync.Once
}

func newRequestGate() *requestGate {
	return &requestGate{reached: make(chan struct{}), released: make(chan struct{})}
}
func (g *requestGate) wait() {
	g.arriveOnce.Do(func() { close(g.reached); <-g.released })
}
func (g *requestGate) release() {
	g.releaseOnce.Do(func() { close(g.released) })
}
func (g *requestGate) closeProxy(p *httptest.Server) {
	g.release()
	p.Close()
}

// A failed restart calls Fatal/Goexit before the normal release. The cleanup
// must still finish; an independent rescue release bounds even the RED run.
func TestFaultProxy_EarlyExitReleasesBlockedHandler(t *testing.T) {
	gate := newRequestGate()
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { gate.wait(); w.WriteHeader(http.StatusNoContent) }))
	defer gate.release()
	requestDone := make(chan error, 1)
	go func() {
		client := &http.Client{Timeout: time.Second}
		r, e := client.Post(proxy.URL, "text/plain", strings.NewReader("INSERT"))
		if e == nil {
			r.Body.Close()
		}
		requestDone <- e
	}()
	cleanupDone, exiting := make(chan struct{}), make(chan struct{})
	go func() {
		defer close(cleanupDone)
		defer gate.closeProxy(proxy)
		<-gate.reached
		close(exiting)
		runtime.Goexit()
	}()
	select {
	case <-exiting:
	case <-time.After(time.Second):
		t.Fatal("request did not reach injection point")
	}
	blocked := false
	select {
	case <-cleanupDone:
	case <-time.After(200 * time.Millisecond):
		blocked = true
	}
	gate.release() // rescue old behavior; also checks double release on GREEN
	select {
	case <-cleanupDone:
	case <-time.After(time.Second):
		t.Fatal("proxy cleanup did not finish even after rescue")
	}
	if e := <-requestDone; e != nil {
		t.Fatal(e)
	}
	if blocked {
		t.Fatal("early-exit cleanup blocked until external rescue release")
	}
}

// Read and close the upstream response before any after-response injection.
// Cases retain their query edits and fault decisions at their call sites.
func forwardFaultRequest(w http.ResponseWriter, r *http.Request, target string, body []byte, after func() bool) {
	up, err := http.NewRequestWithContext(r.Context(), r.Method, target, bytes.NewReader(body))
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	up.Header = r.Header.Clone()
	up.Header.Del("Accept-Encoding")
	resp, err := http.DefaultClient.Do(up)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	out, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	if after != nil && after() {
		return
	}
	w.WriteHeader(resp.StatusCode)
	_, _ = w.Write(out)
}

func dropFaultResponse(w http.ResponseWriter) {
	c, _, err := w.(http.Hijacker).Hijack()
	if err == nil {
		c.Close()
	}
}

func TestFaultProxy_ForwardAndResponseLossReleaseUpstream(t *testing.T) {
	var connections, requests atomic.Int32
	upstream := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		body, err := io.ReadAll(r.Body)
		if err != nil || string(body) != "INSERT" || r.Method != http.MethodPost || r.URL.RequestURI() != "/write?session_check=1" || r.Header.Get("X-Fault-Test") != "preserved" {
			t.Errorf("forwarded request: %s %s body=%q err=%v", r.Method, r.URL, body, err)
		}
		w.Header().Set("Content-Encoding", "gzip")
		w.WriteHeader(http.StatusCreated)
		z := gzip.NewWriter(w)
		_, _ = z.Write([]byte("committed"))
		_ = z.Close()
	}))
	upstream.Config.ConnState = func(_ net.Conn, state http.ConnState) {
		if state == http.StateNew {
			connections.Add(1)
		}
	}
	upstream.Start()
	defer upstream.Close()
	var dropped atomic.Bool
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		forwardFaultRequest(w, r, upstream.URL+r.URL.RequestURI(), body, func() bool {
			if dropped.CompareAndSwap(false, true) {
				dropFaultResponse(w)
				return true
			}
			return false
		})
	}))
	defer proxy.Close()
	client := &http.Client{Timeout: time.Second}
	for i := 0; i < 2; i++ {
		req, _ := http.NewRequest(http.MethodPost, proxy.URL+"/write?session_check=1", strings.NewReader("INSERT"))
		req.Header.Set("X-Fault-Test", "preserved")
		req.Header.Set("Accept-Encoding", "gzip")
		resp, err := client.Do(req)
		if i == 0 {
			if err == nil {
				resp.Body.Close()
				t.Fatal("response was not lost")
			}
			continue
		}
		if err != nil {
			t.Fatal(err)
		}
		body, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil || resp.StatusCode != http.StatusCreated || string(body) != "committed" {
			t.Fatalf("response=%d %q %v", resp.StatusCode, body, err)
		}
	}
	if requests.Load() != 2 || connections.Load() != 1 {
		t.Fatalf("requests=%d connections=%d; upstream response not drained/reused", requests.Load(), connections.Load())
	}
}

func TestFaultProxy_ForwardErrorsReleaseResources(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "100")
		_, _ = w.Write([]byte("truncated"))
	}))
	defer upstream.Close()
	closed := httptest.NewServer(http.NotFoundHandler())
	closed.Close()
	for name, target := range map[string]string{"invalid_target": ":bad", "transport": closed.URL, "truncated_response": upstream.URL} {
		t.Run(name, func(t *testing.T) {
			proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				forwardFaultRequest(w, r, target, nil, func() bool { t.Error("after callback ran on upstream error"); return false })
			}))
			defer proxy.Close()
			client := &http.Client{Timeout: time.Second}
			resp, err := client.Post(proxy.URL, "text/plain", nil)
			if err != nil {
				t.Fatal(err)
			}
			_, err = io.ReadAll(resp.Body)
			resp.Body.Close()
			if err != nil || resp.StatusCode != http.StatusBadGateway {
				t.Fatalf("response=%d err=%v", resp.StatusCode, err)
			}
		})
	}
}
