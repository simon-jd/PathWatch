package adminbridge

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

func TestPreStopDrainsTheEdgeProxyThenWaits(t *testing.T) {
	var mu sync.Mutex
	var seen []string
	edge := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		seen = append(seen, r.Method+" "+r.URL.Path)
		mu.Unlock()
		w.Write([]byte("draining\n"))
	}))
	defer edge.Close()
	start := time.Now()
	if e := PreStop(context.Background(), edge.Listener.Addr().String(), 200*time.Millisecond); e != nil {
		t.Fatal(e)
	}
	if time.Since(start) < 200*time.Millisecond {
		t.Fatal("returned before the wait")
	}
	mu.Lock()
	defer mu.Unlock()
	if len(seen) != 1 || seen[0] != "POST /drain" {
		t.Fatalf("edge proxy saw %v", seen)
	}
}

// The wait keeps the pod serving until kube-proxy has dropped its endpoint, so a failed
// drain request must not cut it short: the error is returned only after the wait.
func TestPreStopWaitsInFullWhenTheDrainFails(t *testing.T) {
	l, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	refused := l.Addr().String()
	l.Close()
	rejected := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(404) }))
	defer rejected.Close()
	for _, address := range []string{refused, rejected.Listener.Addr().String(), "10.0.0.1:9092", "localhost:9092"} {
		start := time.Now()
		if PreStop(context.Background(), address, 100*time.Millisecond) == nil {
			t.Fatalf("%s: drain failure not reported", address)
		}
		if time.Since(start) < 100*time.Millisecond {
			t.Fatalf("%s: wait cut short", address)
		}
	}
}
