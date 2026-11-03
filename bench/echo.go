//go:build ignore

// Benchmark-only plaintext HTTP/1.1 singleton, built by file name in bench/setup.sh.
// The ignore tag keeps it (and bench/observe.c) out of `go list ./...`.
package main

import (
	"encoding/json"
	"log"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
)

var requests, connections, closed atomic.Uint64

func main() {
	body := strings.Repeat("x", 1024)
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		w.Header().Set("Content-Type", "application/octet-stream")
		w.Header().Set("Content-Length", "1024")
		w.Header().Set("Date", "Sun, 25 Oct 2026 00:00:00 GMT")
		w.Write([]byte(body))
	})
	admin := http.NewServeMux()
	admin.HandleFunc("/stats", func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]uint64{"requests": requests.Load(), "connections": connections.Load(), "closed": closed.Load()})
	})
	go func() { log.Fatal(http.ListenAndServe("127.0.0.1:19091", admin)) }()
	s := &http.Server{Addr: "127.0.0.1:19090", Handler: mux, ConnState: func(c net.Conn, st http.ConnState) {
		if st == http.StateNew {
			connections.Add(1)
		}
		if st == http.StateClosed {
			closed.Add(1)
		}
	}}
	log.Fatal(s.ListenAndServe())
}
