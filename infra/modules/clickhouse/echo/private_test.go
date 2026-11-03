package main

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"flag"
	"fmt"
	"github.com/quic-go/quic-go/http3"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// Exercise the actual flag/parser/file boundary, including refusal to overwrite keys.
func TestEchoCLIProcess(t *testing.T) {
	if os.Getenv("PATHWATCH_ECHO_TEST_PROCESS") != "1" {
		return
	}
	for i, arg := range os.Args {
		if arg == "--" {
			os.Args = append([]string{"echo"}, os.Args[i+1:]...)
			break
		}
	}
	flag.CommandLine = flag.NewFlagSet("echo", flag.ExitOnError)
	main()
	os.Exit(0)
}

func echoCLI(args ...string) ([]byte, error) {
	cmd := exec.Command(os.Args[0], append([]string{"-test.run=^TestEchoCLIProcess$", "--"}, args...)...)
	cmd.Env = append(os.Environ(), "PATHWATCH_ECHO_TEST_PROCESS=1")
	return cmd.CombinedOutput()
}

func TestPrivatePKILifecycle(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "pki")
	out, err := echoCLI("-mode", "issue", "-pki-dir", dir, "-valid-days", "45")
	if err != nil {
		t.Fatalf("offline private PKI unavailable: %v: %s", err, out)
	}
	original, err := os.ReadFile(filepath.Join(dir, "ca-key.pem"))
	if err != nil {
		t.Fatal(err)
	}
	if out, err = echoCLI("-mode", "issue", "-pki-dir", dir); err == nil {
		t.Fatalf("overwrote existing PKI: %s", out)
	}
	after, _ := os.ReadFile(filepath.Join(dir, "ca-key.pem"))
	if !bytes.Equal(original, after) {
		t.Fatal("CA key changed on refusal")
	}
	for site, ip := range map[string]string{"home": "10.30.0.2", "azure-a": "10.10.1.4", "azure-b": "10.20.1.4"} {
		cert, key := filepath.Join(dir, site+"-cert.pem"), filepath.Join(dir, site+"-key.pem")
		pair, e := tls.LoadX509KeyPair(cert, key)
		if e != nil {
			t.Fatal(e)
		}
		leaf, e := x509.ParseCertificate(pair.Certificate[0])
		if e != nil {
			t.Fatal(e)
		}
		if leaf.IsCA || leaf.VerifyHostname(ip) != nil || time.Until(leaf.NotAfter) < 35*24*time.Hour {
			t.Fatal("invalid deployment leaf")
		}
		if len(leaf.AuthorityKeyId) == 0 {
			t.Fatal("deployment leaf lacks issuer key identifier required by strict system TLS")
		}
		info, e := os.Stat(key)
		if e != nil || info.Mode().Perm() != 0600 {
			t.Fatal("key is not private")
		}
		args := []string{"-mode", "check", "-site", site, "-listen", ip + ":18443", "-cert", cert, "-key", key, "-ca", filepath.Join(dir, "ca.pem")}
		before, _ := os.ReadFile(key)
		if out, e := echoCLI(args...); e != nil {
			t.Fatalf("valid deployment refused: %v %s", e, out)
		}
		changed, _ := os.ReadFile(key)
		if !bytes.Equal(before, changed) {
			t.Fatal("check modified supplied key")
		}
		for _, change := range []struct{ flag, value string }{{"-listen", "0.0.0.0:18443"}, {"-listen", "10.30.0.99:18443"}, {"-key", filepath.Join(dir, "ca-key.pem")}, {"-ca", cert}, {"-min-valid-days", "60"}} {
			bad := append([]string{}, args...)
			found := false
			for n := 0; n < len(bad)-1; n++ {
				if bad[n] == change.flag {
					bad[n+1] = change.value
					found = true
					break
				}
			}
			if !found {
				bad = append(bad, change.flag, change.value)
			}
			if out, e := echoCLI(bad...); e == nil {
				t.Fatalf("accepted invalid %s=%s: %s", change.flag, change.value, out)
			}
		}
	}
	block, _ := pem.Decode(original)
	if block == nil || block.Type != "PRIVATE KEY" {
		t.Fatal("CA key format")
	}
}

func TestFixtureRestartWaitsForNewIdentityAndServesAllProtocols(t *testing.T) {
	dir := t.TempDir()
	cert, key := filepath.Join(dir, "cert.pem"), filepath.Join(dir, "key.pem")
	ready := filepath.Join(dir, "ready.json")
	listener, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	addr := listener.Addr().String()
	listener.Close()
	var previous []byte
	for generation := 0; generation < 2; generation++ {
		token := fmt.Sprintf("generation-%d", generation)
		args := []string{"-test.run=^TestEchoCLIProcess$", "--", "-listen", addr, "-cert", cert, "-key", key, "-ready-file", ready, "-ready-token", token}
		cmd := exec.Command(os.Args[0], args...)
		cmd.Env = append(os.Environ(), "PATHWATCH_ECHO_TEST_PROCESS=1")
		var output bytes.Buffer
		cmd.Stdout = &output
		cmd.Stderr = &output
		if e := cmd.Start(); e != nil {
			t.Fatal(e)
		}
		done := make(chan error, 1)
		go func() { done <- cmd.Wait() }()
		stopped := false
		stop := func() {
			if !stopped {
				stopped = true
				_ = cmd.Process.Signal(syscall.SIGTERM)
				select {
				case <-done:
				case <-time.After(5 * time.Second):
					_ = cmd.Process.Kill()
					<-done
				}
			}
		}
		t.Cleanup(stop)
		found := false
		for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); {
			raw, _ := os.ReadFile(ready)
			var state map[string]string
			_ = json.Unmarshal(raw, &state)
			if state["token"] == token {
				found = true
				break
			}
			select {
			case e := <-done:
				stopped = true
				t.Fatalf("echo exited before readiness: %v %s", e, output.String())
			default:
			}
			time.Sleep(10 * time.Millisecond)
		}
		if !found {
			stop()
			t.Fatalf("new generation never ready: %s", output.String())
		}
		ca, _ := os.ReadFile(cert)
		if bytes.Equal(ca, previous) {
			t.Fatal("fixture restart reused stale CA")
		}
		previous = ca
		roots := x509.NewCertPool()
		if !roots.AppendCertsFromPEM(ca) {
			t.Fatal("invalid CA")
		}
		wireProtocols(t, addr, "127.0.0.1", roots, token)
		stop()
		tcp, e := net.Listen("tcp", addr)
		if e != nil {
			t.Fatalf("TCP leaked after exit: %v", e)
		}
		tcp.Close()
		udp, e := net.ListenPacket("udp", addr)
		if e != nil {
			t.Fatalf("UDP leaked after exit: %v", e)
		}
		udp.Close()
	}
}

func wireProtocols(t *testing.T, addr, identity string, roots *x509.CertPool, token string) {
	t.Helper()
	for _, protocol := range []string{"h1", "h2", "h3"} {
		cfg := &tls.Config{RootCAs: roots, ServerName: identity, MinVersion: tls.VersionTLS13}
		var tr http.RoundTripper
		if protocol == "h3" {
			q := &http3.Transport{TLSClientConfig: cfg}
			defer q.Close()
			tr = q
		} else {
			h := &http.Transport{TLSClientConfig: cfg, ForceAttemptHTTP2: protocol == "h2", Proxy: nil}
			defer h.CloseIdleConnections()
			tr = h
		}
		client := &http.Client{Transport: tr, Timeout: 3 * time.Second}
		request, _ := http.NewRequestWithContext(context.Background(), "GET", "https://"+addr+"/", nil)
		response, e := client.Do(request)
		if e != nil {
			t.Fatalf("%s trusted wire: %v", protocol, e)
		}
		response.Body.Close()
		want := map[string]int{"h1": 1, "h2": 2, "h3": 3}[protocol]
		if response.ProtoMajor != want || response.TLS.Version != tls.VersionTLS13 || response.Header.Get("X-PathWatch-Generation") != token {
			t.Fatalf("%s wrong negotiated identity/protocol: %+v", protocol, response)
		}
		if source := os.Getenv("PATHWATCH_EXPECT_SOURCE"); source != "" && !strings.HasPrefix(response.Header.Get("X-PathWatch-Remote"), source+":") {
			t.Fatalf("source identity changed: %s", response.Header.Get("X-PathWatch-Remote"))
		}
	}
}

func TestProvidedOwnedNamespaceWire(t *testing.T) {
	addr := os.Getenv("PATHWATCH_PRIVATE_ADDR")
	if addr == "" {
		t.Skip("explicit owned namespace integration profile required")
	}
	raw, e := os.ReadFile(os.Getenv("PATHWATCH_PRIVATE_CA"))
	if e != nil {
		t.Fatal(e)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(raw) {
		t.Fatal("CA parse")
	}
	wireProtocols(t, addr, "10.30.0.2", roots, os.Getenv("PATHWATCH_PRIVATE_TOKEN"))
	for _, cfg := range []*tls.Config{{RootCAs: x509.NewCertPool(), ServerName: "10.30.0.2", MinVersion: tls.VersionTLS13}, {RootCAs: roots, ServerName: "10.20.1.4", MinVersion: tls.VersionTLS13}} {
		conn, e := tls.DialWithDialer(&net.Dialer{Timeout: time.Second}, "tcp", addr, cfg)
		if e == nil {
			conn.Close()
			t.Fatal("wrong trust or identity accepted on real wire")
		}
	}
}

func TestProvidedRejectsExpiredFutureWrongSANAndReadinessAlias(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "pki")
	if e := issuePKI(dir, 45); e != nil {
		t.Fatal(e)
	}
	caRaw, _ := os.ReadFile(filepath.Join(dir, "ca.pem"))
	block, _ := pem.Decode(caRaw)
	ca, e := x509.ParseCertificate(block.Bytes)
	if e != nil {
		t.Fatal(e)
	}
	keyRaw, _ := os.ReadFile(filepath.Join(dir, "ca-key.pem"))
	block, _ = pem.Decode(keyRaw)
	parsed, e := x509.ParsePKCS8PrivateKey(block.Bytes)
	if e != nil {
		t.Fatal(e)
	}
	caKey := parsed.(*ecdsa.PrivateKey)
	cert, key := filepath.Join(dir, "home-cert.pem"), filepath.Join(dir, "home-key.pem")
	pair, e := tls.LoadX509KeyPair(cert, key)
	if e != nil {
		t.Fatal(e)
	}
	leaf, e := x509.ParseCertificate(pair.Certificate[0])
	if e != nil {
		t.Fatal(e)
	}
	for _, kind := range []string{"expired", "future", "wrong-san"} {
		copy := *leaf
		switch kind {
		case "expired":
			copy.NotBefore = time.Now().Add(-48 * time.Hour)
			copy.NotAfter = time.Now().Add(-time.Hour)
		case "future":
			copy.NotBefore = time.Now().Add(time.Hour)
		case "wrong-san":
			copy.IPAddresses = []net.IP{net.ParseIP("10.20.1.4")}
		}
		der, e := x509.CreateCertificate(rand.Reader, &copy, ca, leaf.PublicKey, caKey)
		if e != nil {
			t.Fatal(e)
		}
		path := filepath.Join(dir, kind+".pem")
		if e = os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0644); e != nil {
			t.Fatal(e)
		}
		if _, e = privateCertificate(path, key, filepath.Join(dir, "ca.pem"), "10.30.0.2:18443", "home", 0); e == nil {
			t.Fatalf("accepted %s leaf", kind)
		}
	}
	alias := filepath.Join(dir, "alias")
	if e = os.Symlink(dir, alias); e != nil {
		t.Fatal(e)
	}
	out, e := echoCLI("-mode", "check", "-site", "home", "-listen", "10.30.0.2:18443", "-cert", cert, "-key", key, "-ca", filepath.Join(dir, "ca.pem"), "-ready-file", filepath.Join(alias, "home-key.pem"), "-ready-token", "test")
	if e == nil {
		t.Fatalf("readiness alias could replace a supplied private key: %s", out)
	}
}

func TestFixtureKeyRemainsPrivateOnRetainedFile(t *testing.T) {
	dir := t.TempDir()
	key := filepath.Join(dir, "key.pem")
	if e := os.WriteFile(key, []byte("old"), 0644); e != nil {
		t.Fatal(e)
	}
	if e := certificate(filepath.Join(dir, "cert.pem"), key); e != nil {
		t.Fatal(e)
	}
	stat, _ := os.Stat(key)
	if stat.Mode().Perm() != 0600 {
		t.Fatal("retained fixture key kept unsafe permissions")
	}
}

func TestEchoReadOnlyCapabilityVersion(t *testing.T) {
	out, e := echoCLI("-version")
	if e != nil || strings.TrimSpace(string(out)) != "pathwatch-echo-private-v1" {
		t.Fatalf("cannot verify compatible echo artifact: %v %s", e, out)
	}
}
