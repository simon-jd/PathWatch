package main

import (
	"context"
	"encoding/pem"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/miekg/dns"
	"pathwatch.local/pathwatch/probe/internal/dnsprobe"
	"pathwatch.local/pathwatch/probe/internal/report"
	"pathwatch.local/pathwatch/probe/internal/result"
	"pathwatch.local/pathwatch/probe/internal/spool"
	"pathwatch.local/pathwatch/probe/internal/testdns"
)

// An opt-in fixture uses an owned ingest process and a dedicated ClickHouse DB.
// It checks the unchanged deployed validator/schema, rather than substituting a
// test gRPC server that accepts identities the real ingest would reject.
func TestDNSOwnedIngestPersistence(t *testing.T) {
	endpoint := os.Getenv("PW_DNS_INGEST_ENDPOINT")
	ch := os.Getenv("PW_DNS_CLICKHOUSE_URL")
	if endpoint == "" || ch == "" {
		t.Skip("requires dedicated owned ingest and ClickHouse fixture")
	}
	server := testdns.Start(t, func(_ string, q *dns.Msg) *dns.Msg { return testdns.Answer(q, "127.0.0.1", "::1") })
	doh := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, err := io.ReadAll(io.LimitReader(r.Body, 65536))
		if err != nil {
			t.Error(err)
			return
		}
		q := new(dns.Msg)
		if err = q.Unpack(raw); err != nil {
			t.Error(err)
			return
		}
		wire, err := testdns.Answer(q, "127.0.0.1", "::1").Pack()
		if err != nil {
			t.Error(err)
			return
		}
		w.Header().Set("Content-Type", "application/dns-message")
		_, _ = w.Write(wire)
	}))
	defer doh.Close()
	ca := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(ca, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: doh.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	var results []result.Result
	for _, mode := range []string{"", "udp", "tcp", "doh"} {
		target := result.Target{ID: "owned", Host: "owned.test", Kinds: []result.Kind{result.DNS}}
		if mode != "" {
			target.DNS = &result.DNSConfig{Transport: mode}
			if mode == "doh" {
				target.DNS.DoHURL = doh.URL
				target.DNS.CAFile = ca
			}
		}
		p, err := dnsprobe.New(dnsprobe.Config{ProbeID: "dns-fixture", Server: server, Timeout: time.Second, Targets: []result.Target{target}})
		if err != nil {
			t.Fatal(err)
		}
		got, err := p.Probe(context.Background(), target)
		if err != nil || len(got) != 2 {
			t.Fatalf("%s: %+v %v", mode, got, err)
		}
		for _, r := range got {
			if r.Err != "" {
				t.Fatalf("%s measurement: %+v", mode, r)
			}
		}
		results = append(results, got...)
	}
	dir := t.TempDir()
	queue, err := spool.Open(dir, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if err = queue.Append(results); err != nil {
		t.Fatal(err)
	}
	if err = queue.Close(); err != nil {
		t.Fatal(err)
	}
	// Reopen from disk to exercise existing spool JSON replay before protobuf.
	queue, err = spool.Open(dir, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	defer queue.Close()
	client, err := report.New(report.Config{ProbeID: "dns-fixture", Endpoint: endpoint, DevelopmentCleartext: true, RPCTimeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- client.Run(ctx, queue) }()
	deadline := time.Now().Add(10 * time.Second)
	for client.Stats().Acknowledged < 1 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	<-done
	if client.Stats().Acknowledged != 1 {
		t.Fatalf("real ingest did not acknowledge DNS results: %+v", client.Stats())
	}
	query := func(sql string) string {
		req, err := http.NewRequest(http.MethodPost, ch, strings.NewReader(sql))
		if err != nil {
			t.Fatal(err)
		}
		req.SetBasicAuth("default", "task10c-owned")
		response, err := (&http.Client{Timeout: 3 * time.Second}).Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		raw, err := io.ReadAll(response.Body)
		if err != nil || response.StatusCode != 200 {
			t.Fatalf("CH query: status=%d raw=%s err=%v", response.StatusCode, raw, err)
		}
		return strings.TrimSpace(string(raw))
	}
	got := query("SELECT count(),uniqExact(path_key),countIf(protocol != '') FROM probe_results WHERE probe_id='dns-fixture'")
	if got != "8\t8\t0" {
		t.Fatalf("transport path collision or Protocol misuse: %q", got)
	}
	paths := query("SELECT path_key FROM probe_results WHERE probe_id='dns-fixture' ORDER BY path_key")
	want := "dns-fixture→owned/dns/v4\ndns-fixture→owned/dns/v6\ndns-fixture→owned:dns:doh/dns/v4\ndns-fixture→owned:dns:doh/dns/v6\ndns-fixture→owned:dns:tcp/dns/v4\ndns-fixture→owned:dns:tcp/dns/v6\ndns-fixture→owned:dns:udp/dns/v4\ndns-fixture→owned:dns:udp/dns/v6"
	if paths != want {
		t.Fatalf("unexpected persisted paths: %s", paths)
	}
	if got := query("SELECT uniqExact(path_key),sum(samples) FROM (SELECT path_key,countMerge(samples) AS samples FROM path_quantiles_5m GROUP BY path_key)"); got != "8\t8" {
		t.Fatalf("aggregate collision: %s", got)
	}
	t.Logf("real DNS UDP/TCP/DoH + legacy → reopened spool → gRPC → unchanged ingest → ClickHouse: raw=8, distinct paths=8, aggregate paths=8, samples=8\n%s", paths)
}
