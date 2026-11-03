package dnsprobe

import (
	"context"
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"github.com/miekg/dns"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"pathwatch.local/pathwatch/probe/internal/testdns"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func dohFixture(t *testing.T, handle func(http.ResponseWriter, *http.Request, *dns.Msg)) (string, string) {
	t.Helper()
	s := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.URL.Path != "/dns-query" || r.Header.Get("Content-Type") != "application/dns-message" || r.Header.Get("Accept") != "application/dns-message" {
			t.Errorf("incorrect DoH request: %s %s %v", r.Method, r.URL, r.Header)
			w.WriteHeader(400)
			return
		}
		wire, err := io.ReadAll(io.LimitReader(r.Body, 65536))
		if err != nil {
			t.Error(err)
			return
		}
		q := new(dns.Msg)
		if err = q.Unpack(wire); err != nil {
			t.Error(err)
			return
		}
		if q.Id != 0 || q.Response || len(q.Question) != 1 || q.Question[0].Qclass != dns.ClassINET || q.Question[0].Name != "owned.test." {
			t.Errorf("invalid wire query: %+v", q)
		}
		w.Header().Set("Content-Type", "application/dns-message")
		handle(w, r, q)
	}))
	s.Config.ErrorLog = log.New(io.Discard, "", 0) // Expected untrusted TLS handshake failures.
	s.EnableHTTP2 = true
	s.StartTLS()
	t.Cleanup(s.Close)
	cert, err := x509.ParseCertificate(s.TLS.Certificates[0].Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	file := filepath.Join(t.TempDir(), "ca.pem")
	if err = os.WriteFile(file, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert.Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	return s.URL + "/dns-query", file
}
func writeDNS(w http.ResponseWriter, q *dns.Msg) { wire, _ := q.Pack(); _, _ = w.Write(wire) }
func dohSettings(url, ca string) string {
	return fmt.Sprintf(`{"transport":"doh","doh_url":%q,"ca_file":%q}`, url, ca)
}

func TestDNSDoHWireAndPartialAnswers(t *testing.T) {
	for _, partial := range []bool{false, true} {
		t.Run(fmt.Sprint(partial), func(t *testing.T) {
			var calls atomic.Int32
			url, ca := dohFixture(t, func(w http.ResponseWriter, r *http.Request, q *dns.Msg) {
				calls.Add(1)
				m := testdns.Answer(q, "127.0.0.1", "::1")
				if partial && q.Question[0].Qtype == dns.TypeAAAA {
					m.Rcode = dns.RcodeServerFailure
					m.Answer = nil
				}
				writeDNS(w, m)
			})
			p, target := configuredProbe(t, "127.0.0.1:1", dohSettings(url, ca))
			got, err := p.Probe(context.Background(), target)
			if err != nil || len(got) != 2 || calls.Load() != 2 {
				t.Fatalf("results=%+v err=%v calls=%d", got, err, calls.Load())
			}
			if got[0].Addr != "127.0.0.1" || got[0].Err != "" || got[0].TargetID != "owned:dns:doh" || got[0].Protocol != "" || got[0].Phases["resolve"] <= 0 {
				t.Fatalf("A result=%+v", got[0])
			}
			if partial {
				if !strings.Contains(got[1].Err, "SERVFAIL") {
					t.Fatalf("lost partial failure: %+v", got)
				}
			} else if got[1].Addr != "::1" || got[1].Err != "" {
				t.Fatalf("AAAA result=%+v", got[1])
			}
		})
	}
}

func TestDNSDoHRejectsBadResponses(t *testing.T) {
	for _, mode := range []string{"status", "type", "missing-type", "malformed", "oversized", "id", "question", "qtype", "no-question", "class", "opcode", "qr", "truncated", "redirect", "untrusted", "hostname"} {
		t.Run(mode, func(t *testing.T) {
			var calls atomic.Int32
			url, ca := dohFixture(t, func(w http.ResponseWriter, r *http.Request, q *dns.Msg) {
				calls.Add(1)
				m := testdns.Answer(q, "127.0.0.1", "::1")
				switch mode {
				case "status":
					w.WriteHeader(503)
					return
				case "type":
					w.Header().Set("Content-Type", "text/html")
				case "missing-type":
					w.Header()["Content-Type"] = nil
				case "malformed":
					_, _ = w.Write([]byte{0, 1, 2})
					return
				case "oversized":
					_, _ = w.Write(make([]byte, 65536))
					return
				case "id":
					m.Id++
				case "question":
					m.Question[0].Name = "wrong.test."
				case "qtype":
					m.Question[0].Qtype = dns.TypeTXT
				case "no-question":
					m.Question = nil
				case "class":
					m.Question[0].Qclass = dns.ClassCHAOS
				case "opcode":
					m.Opcode = dns.OpcodeUpdate
				case "qr":
					m.Response = false
				case "truncated":
					m.Truncated = true
				case "redirect":
					w.Header().Set("Location", "https://127.0.0.1:1/unconfigured")
					w.WriteHeader(307)
					return
				}
				writeDNS(w, m)
			})
			if mode == "untrusted" {
				ca = ""
			}
			if mode == "hostname" {
				url = strings.Replace(url, "127.0.0.1", "localhost", 1)
			}
			p, target := configuredProbe(t, "127.0.0.1:1", dohSettings(url, ca))
			got, err := p.Probe(context.Background(), target)
			if err != nil || len(got) != 2 || got[0].Err == "" || got[1].Err == "" || got[0].Addr != "" || got[1].Addr != "" {
				t.Fatalf("bad response accepted: %+v err=%v", got, err)
			}
			if mode == "untrusted" || mode == "hostname" {
				if calls.Load() != 0 || !strings.Contains(got[0].Err, "certificate") {
					t.Fatalf("TLS verification bypassed or unrelated failure: requests=%d result=%+v", calls.Load(), got[0])
				}
			} else if calls.Load() != 2 {
				t.Fatalf("unexpected retries: %d", calls.Load())
			}
			if mode == "redirect" && !strings.Contains(got[0].Err, "307") {
				t.Fatalf("redirect followed: %+v", got)
			}
		})
	}
}

func TestDNSDoHCancellationAndDeadlinePreserveA(t *testing.T) {
	for _, mode := range []string{"cancel", "deadline"} {
		t.Run(mode, func(t *testing.T) {
			received := make(chan struct{})
			url, ca := dohFixture(t, func(w http.ResponseWriter, r *http.Request, q *dns.Msg) {
				if q.Question[0].Qtype == dns.TypeA {
					writeDNS(w, testdns.Answer(q, "127.0.0.1"))
					return
				}
				close(received)
				<-r.Context().Done()
			})
			p, target := configuredProbe(t, "127.0.0.1:1", dohSettings(url, ca))
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if mode == "deadline" {
				ctx, cancel = context.WithTimeout(context.Background(), 150*time.Millisecond)
				defer cancel()
			} else {
				go func() {
					select {
					case <-received:
						cancel()
					case <-ctx.Done():
					}
				}()
			}
			start := time.Now()
			got, err := p.Probe(ctx, target)
			if err == nil || len(got) != 2 || got[0].Addr != "127.0.0.1" || got[1].Err == "" || time.Since(start) > time.Second {
				t.Fatalf("partial/cancellation: %+v %v elapsed=%s", got, err, time.Since(start))
			}
		})
	}
}

func TestDNSDoHProbeTimeoutBoundsResponseBody(t *testing.T) {
	url, ca := dohFixture(t, func(w http.ResponseWriter, r *http.Request, q *dns.Msg) {
		if q.Question[0].Qtype == dns.TypeA {
			writeDNS(w, testdns.Answer(q, "127.0.0.1"))
			return
		}
		w.WriteHeader(200)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	})
	p, target := configuredProbeTimeout(t, "127.0.0.1:1", dohSettings(url, ca), 100*time.Millisecond)
	start := time.Now()
	got, err := p.Probe(context.Background(), target)
	if err != nil || len(got) != 2 || got[0].Addr != "127.0.0.1" || !strings.Contains(got[1].Err, "deadline exceeded") || time.Since(start) > time.Second {
		t.Fatalf("probe timeout/partial body: %+v err=%v elapsed=%s", got, err, time.Since(start))
	}
}
