package main

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestCLIHelper(t *testing.T) {
	if os.Getenv("PATHWATCH_BRIDGE_TEST_HELPER") != "1" {
		return
	}
	for i, a := range os.Args {
		if a == "--" {
			os.Args = append([]string{"adminbridge"}, os.Args[i+1:]...)
			main()
			os.Exit(0)
		}
	}
	os.Exit(90)
}
func cli(t *testing.T, args ...string) *exec.Cmd {
	t.Helper()
	exe, e := os.Executable()
	if e != nil {
		t.Fatal(e)
	}
	cmd := exec.Command(exe, append([]string{"-test.run=^TestCLIHelper$", "--"}, args...)...)
	cmd.Env = append(os.Environ(), "PATHWATCH_BRIDGE_TEST_HELPER=1")
	return cmd
}
func TestCLIReadyExitAndStartupFailure(t *testing.T) {
	for _, tc := range []struct {
		body string
		code int
		ok   bool
	}{{"ready\n", 200, true}, {"not ready\n", 200, false}, {"ready\n", 503, false}} {
		u := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(tc.code); io.WriteString(w, tc.body) }))
		cmd := cli(t, "check-ready", "-upstream", u.Listener.Addr().String())
		out, e := cmd.CombinedOutput()
		u.Close()
		t.Logf("checker code=%d expected_ok=%v exit=%v output=%q", tc.code, tc.ok, e, out)
		if (e == nil) != tc.ok {
			t.Fatal("checker exit mismatch")
		}
	}
	for _, args := range [][]string{{}, {"bad"}, {"serve", "-listen", "localhost:9445"}, {"serve", "-listen", "127.0.0.1:19445", "-ca", "missing"}, {"check-ready", "-upstream", "127.0.0.2:9091"}, {"check-ready", "-ready-format", "auto"}} {
		out, e := cli(t, args...).CombinedOutput()
		if e == nil {
			t.Fatalf("invalid CLI succeeded %v %q", args, out)
		}
	}
}
func cliPKI(t *testing.T) ([]string, *tls.Config) {
	t.Helper()
	now := time.Now()
	k, e := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if e != nil {
		t.Fatal(e)
	}
	a := &x509.Certificate{SerialNumber: big.NewInt(1), NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	der, e := x509.CreateCertificate(rand.Reader, a, a, &k.PublicKey, k)
	if e != nil {
		t.Fatal(e)
	}
	ca, e := x509.ParseCertificate(der)
	if e != nil {
		t.Fatal(e)
	}
	roots := x509.NewCertPool()
	roots.AddCert(ca)
	dir := t.TempDir()
	caPath := filepath.Join(dir, "ca")
	os.WriteFile(caPath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0600)
	var client tls.Certificate
	args := []string{"-ca", caPath}
	for i, role := range []string{"metrics", "prometheus"} {
		lk, e := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if e != nil {
			t.Fatal(e)
		}
		uri, _ := url.Parse("spiffe://pathwatch.local/" + role)
		c := &x509.Certificate{SerialNumber: big.NewInt(int64(i + 2)), NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour), URIs: []*url.URL{uri}, KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}}
		if i == 0 {
			c.DNSNames = []string{"metrics.pathwatch.local"}
			c.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
		}
		b, e := x509.CreateCertificate(rand.Reader, c, ca, &lk.PublicKey, k)
		if e != nil {
			t.Fatal(e)
		}
		if i == 1 {
			client = tls.Certificate{Certificate: [][]byte{b}, PrivateKey: lk}
			continue
		}
		kb, e := x509.MarshalECPrivateKey(lk)
		if e != nil {
			t.Fatal(e)
		}
		cp, kp := filepath.Join(dir, "cert"), filepath.Join(dir, "key")
		os.WriteFile(cp, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: b}), 0600)
		os.WriteFile(kp, pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: kb}), 0600)
		args = append(args, "-cert", cp, "-key", kp)
	}
	return args, &tls.Config{MinVersion: tls.VersionTLS13, RootCAs: roots, ServerName: "metrics.pathwatch.local", Certificates: []tls.Certificate{client}}
}
func TestCLISIGTERMFromReady(t *testing.T) {
	args, trust := cliPKI(t)
	u := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "metric 1\n") }))
	defer u.Close()
	port, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	address := port.Addr().String()
	port.Close()
	args = append([]string{"serve", "-listen", address, "-upstream", u.Listener.Addr().String()}, args...)
	cmd := cli(t, args...)
	var output bytes.Buffer
	cmd.Stdout = &output
	cmd.Stderr = &output
	if e = cmd.Start(); e != nil {
		t.Fatal(e)
	}
	done := make(chan error, 1)
	waited := make(chan struct{})
	go func() { e := cmd.Wait(); close(waited); done <- e }()
	t.Cleanup(func() {
		select {
		case <-waited:
			return
		default:
		}
		cmd.Process.Signal(syscall.SIGTERM)
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			cmd.Process.Kill()
			<-done
		}
	})
	stat, e := os.ReadFile(fmt.Sprintf("/proc/%d/stat", cmd.Process.Pid))
	if e != nil {
		t.Fatal(e)
	}
	fields := strings.Fields(string(stat)[strings.LastIndex(string(stat), ")")+2:])
	startTicks := fields[19]
	exe, _ := os.Readlink(fmt.Sprintf("/proc/%d/exe", cmd.Process.Pid))
	t.Logf("owned CLI pid=%d start_ticks=%s exe=%s", cmd.Process.Pid, startTicks, exe)
	tr := &http.Transport{TLSClientConfig: trust, DisableKeepAlives: true}
	defer tr.CloseIdleConnections()
	client := &http.Client{Transport: tr, Timeout: time.Second}
	ready := false
	until := time.Now().Add(2 * time.Second)
	for time.Now().Before(until) {
		r, e := client.Get("https://" + address + "/metrics")
		if e == nil {
			body, re := io.ReadAll(r.Body)
			r.Body.Close()
			if re == nil && r.StatusCode == 200 && string(body) == "metric 1\n" {
				ready = true
				break
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !ready {
		t.Fatal("CLI not ready")
	}
	started := time.Now()
	if e = cmd.Process.Signal(syscall.SIGTERM); e != nil {
		t.Fatal(e)
	}
	select {
	case e = <-done:
		if e != nil {
			t.Fatalf("SIGTERM exit=%v output=%q", e, output.String())
		}
	case <-time.After(3 * time.Second):
		t.Fatal("CLI shutdown exceeded3s")
	}
	conn, e := net.DialTimeout("tcp", address, time.Second)
	if e == nil {
		conn.Close()
		t.Fatal("CLI port survives")
	}
	if _, e = os.Stat(fmt.Sprintf("/proc/%d", cmd.Process.Pid)); !os.IsNotExist(e) {
		t.Fatal("CLI process survives")
	}
	t.Logf("CLI ready->SIGTERM->clean exit %s, port closed", time.Since(started))
}
