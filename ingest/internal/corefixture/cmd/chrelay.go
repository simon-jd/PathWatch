package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"
)

// chRelay is linked only into the owned laboratory helper. It forwards real
// ClickHouse responses, and never manufactures a database success or marker.
func chRelay(target, listen, control string) error {
	u, err := url.Parse(target)
	if err != nil || u.Scheme != "http" || net.ParseIP(u.Hostname()) == nil || !net.ParseIP(u.Hostname()).IsLoopback() || control == "" {
		return fmt.Errorf("owned literal-loopback target and control directory required")
	}
	if err := os.Mkdir(control, 0700); err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()
	transport := &http.Transport{Proxy: nil, DisableKeepAlives: true, MaxConnsPerHost: 4}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 30 * time.Second}
	var mu sync.Mutex
	used := map[string]bool{}
	idPattern := regexp.MustCompile(`^[a-z0-9-]{1,64}$`)
	h := &http.Server{ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: 35 * time.Second, MaxHeaderBytes: 16 << 10, BaseContext: func(net.Listener) context.Context { return ctx }}
	h.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, e := io.ReadAll(io.LimitReader(r.Body, (16<<20)+1))
		if e != nil || len(body) > 16<<20 {
			http.Error(w, "relay body bound", 413)
			return
		}
		up, e := http.NewRequestWithContext(r.Context(), "POST", target+r.URL.RequestURI(), bytes.NewReader(body))
		if e != nil {
			http.Error(w, "relay request", 502)
			return
		}
		up.Header = r.Header.Clone()
		resp, e := client.Do(up)
		if e != nil {
			http.Error(w, "relay upstream", 502)
			return
		}
		b, e := io.ReadAll(io.LimitReader(resp.Body, (1<<20)+1))
		resp.Body.Close()
		if e != nil || len(b) > 1<<20 {
			http.Error(w, "relay response bound", 502)
			return
		}
		var arm struct{ ID, Kind, Batch string }
		if raw, e := os.ReadFile(filepath.Join(control, "arm.json")); e == nil && len(raw) <= 4096 {
			json.Unmarshal(raw, &arm)
		}
		match := idPattern.MatchString(arm.ID) && ((arm.Kind == "insert" && strings.HasPrefix(string(body), "INSERT INTO probe_results") && strings.Contains(string(body), arm.Batch)) || (arm.Kind == "query" && strings.Contains(string(body), "FROM probe_results") && r.URL.Query().Get("param_path") != ""))
		mu.Lock()
		hold := match && !used[arm.ID]
		if hold {
			used[arm.ID] = true
		}
		mu.Unlock()
		if hold {
			hash := sha256.Sum256(body)
			info, _ := json.Marshal(map[string]any{"status": resp.StatusCode, "body_sha256": hex.EncodeToString(hash[:]), "response_bytes": len(b), "actual_response_received": true})
			if e := os.WriteFile(filepath.Join(control, arm.ID+".entered"), info, 0600); e != nil {
				http.Error(w, "relay marker", 500)
				return
			}
			tick := time.NewTicker(10 * time.Millisecond)
			defer tick.Stop()
			for {
				if _, e := os.Stat(filepath.Join(control, arm.ID+".release")); e == nil {
					break
				}
				select {
				case <-r.Context().Done():
					os.WriteFile(filepath.Join(control, arm.ID+".canceled"), []byte("actual upstream response withheld; caller canceled\n"), 0600)
					return
				case <-tick.C:
				}
			}
		}
		for k, vs := range resp.Header {
			if strings.EqualFold(k, "Content-Length") {
				continue
			}
			for _, v := range vs {
				w.Header().Add(k, v)
			}
		}
		w.WriteHeader(resp.StatusCode)
		if _, e := w.Write(b); e != nil {
			fmt.Fprintf(os.Stderr, "expected possible canceled relay write: %v\n", e)
		}
	})
	l, err := net.Listen("tcp", listen)
	if err != nil {
		return err
	}
	defer l.Close()
	done := make(chan error, 1)
	go func() { done <- h.Serve(l) }()
	var result error
	select {
	case <-ctx.Done():
	case result = <-done:
	}
	drain, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if e := h.Shutdown(drain); e != nil {
		h.Close()
		if result == nil {
			result = e
		}
	}
	if result == nil {
		select {
		case result = <-done:
		case <-drain.Done():
			return drain.Err()
		}
	}
	if errors.Is(result, http.ErrServerClosed) {
		return nil
	}
	return result
}
