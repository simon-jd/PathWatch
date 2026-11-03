// Owned fixture and exact private deployment: TLS 1.3 H1/H2/H3 on one port.
package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"github.com/quic-go/quic-go/http3"
	"log"
	"math/big"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"
)

func certificate(cert, key string) error {
	private, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return err
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return err
	}
	template := &x509.Certificate{SerialNumber: serial, Subject: pkix.Name{CommonName: "PathWatch owned loopback fixture"}, NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(24 * time.Hour), IPAddresses: []net.IP{net.ParseIP("127.0.0.1"), net.ParseIP("::1")}, KeyUsage: x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, IsCA: true, BasicConstraintsValid: true}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &private.PublicKey, private)
	if err != nil {
		return err
	}
	raw, err := x509.MarshalPKCS8PrivateKey(private)
	if err != nil {
		return err
	}
	if err = os.WriteFile(key, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: raw}), 0600); err != nil {
		return err
	}
	if err = os.Chmod(key, 0600); err != nil {
		return err
	}
	return os.WriteFile(cert, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0644)
}

var siteIPs = map[string]string{"home": "10.30.0.2", "azure-a": "10.10.1.4", "azure-b": "10.20.1.4"}

func privateCertificate(cert, key, ca, addr, site string, horizon time.Duration) (tls.Certificate, error) {
	var empty tls.Certificate
	host, port, err := net.SplitHostPort(addr)
	if err != nil || siteIPs[site] == "" || host != siteIPs[site] || port != "18443" {
		return empty, fmt.Errorf("exact owned site IP and port 18443 required")
	}
	pair, err := tls.LoadX509KeyPair(cert, key)
	if err != nil {
		return empty, err
	}
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		return empty, err
	}
	if leaf.IsCA {
		return empty, fmt.Errorf("provided server leaf must not be a CA")
	}
	roots := x509.NewCertPool()
	raw, err := os.ReadFile(ca)
	if err != nil {
		return empty, err
	}
	now := time.Now()
	count := 0
	for len(raw) > 0 {
		block, rest := pem.Decode(raw)
		raw = rest
		if block == nil {
			return empty, fmt.Errorf("invalid CA PEM")
		}
		parent, e := x509.ParseCertificate(block.Bytes)
		if e != nil {
			return empty, e
		}
		if !parent.IsCA || parent.KeyUsage&x509.KeyUsageCertSign == 0 || now.Before(parent.NotBefore) || parent.NotAfter.Before(now.Add(horizon)) {
			return empty, fmt.Errorf("CA validity/usage insufficient")
		}
		roots.AddCert(parent)
		count++
	}
	if count == 0 || now.Before(leaf.NotBefore) || leaf.NotAfter.Before(now.Add(horizon)) {
		return empty, fmt.Errorf("certificate validity does not cover requested horizon")
	}
	intermediates := x509.NewCertPool()
	for _, der := range pair.Certificate[1:] {
		c, e := x509.ParseCertificate(der)
		if e != nil {
			return empty, e
		}
		intermediates.AddCert(c)
	}
	if _, err = leaf.Verify(x509.VerifyOptions{Roots: roots, Intermediates: intermediates, DNSName: host, CurrentTime: now, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}); err != nil {
		return empty, err
	}
	return pair, nil
}

func issuePKI(directory string, days int) error {
	if days < 35 || days > 60 || directory == "" {
		return fmt.Errorf("deployment leaf validity must be 35..60 days and new PKI directory required")
	}
	// Mkdir deliberately refuses existing directories, including symlinks: keys are never overwritten.
	if err := os.Mkdir(directory, 0700); err != nil {
		return err
	}
	generate := func() (*ecdsa.PrivateKey, error) { return ecdsa.GenerateKey(elliptic.P256(), rand.Reader) }
	serial := func() *big.Int {
		n, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
		if err != nil {
			panic(err)
		}
		return n
	}
	save := func(name string, der []byte, key *ecdsa.PrivateKey) error {
		raw, e := x509.MarshalPKCS8PrivateKey(key)
		if e != nil {
			return e
		}
		if e = os.WriteFile(filepath.Join(directory, name+"-key.pem"), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: raw}), 0600); e != nil {
			return e
		}
		certName := name + "-cert.pem"
		if name == "ca" {
			certName = "ca.pem"
		}
		return os.WriteFile(filepath.Join(directory, certName), pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0644)
	}
	caKey, err := generate()
	if err != nil {
		return err
	}
	now := time.Now()
	ca := &x509.Certificate{SerialNumber: serial(), Subject: pkix.Name{CommonName: "PathWatch private deployment CA"}, NotBefore: now.Add(-time.Minute), NotAfter: now.Add(90 * 24 * time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageCRLSign}
	der, err := x509.CreateCertificate(rand.Reader, ca, ca, &caKey.PublicKey, caKey)
	if err != nil {
		return err
	}
	if err = save("ca", der, caKey); err != nil {
		return err
	}
	ca, err = x509.ParseCertificate(der)
	if err != nil {
		return err
	}
	for site, ip := range siteIPs {
		key, e := generate()
		if e != nil {
			return e
		}
		leaf := &x509.Certificate{SerialNumber: serial(), Subject: pkix.Name{CommonName: "PathWatch " + site}, NotBefore: now.Add(-time.Minute), NotAfter: now.Add(time.Duration(days) * 24 * time.Hour), BasicConstraintsValid: true, KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, IPAddresses: []net.IP{net.ParseIP(ip)}}
		der, e := x509.CreateCertificate(rand.Reader, leaf, ca, &key.PublicKey, caKey)
		if e != nil {
			return e
		}
		if e = save(site, der, key); e != nil {
			return e
		}
	}
	return nil
}

func serve(ctx context.Context, addr string, pair tls.Certificate, cert, ready, token string) error {
	tcpListener, err := net.Listen("tcp", addr)
	if err != nil {
		return err
	}
	defer tcpListener.Close()
	udpListener, err := net.ListenPacket("udp", addr)
	if err != nil {
		return err
	}
	defer udpListener.Close()
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" || (r.Method != "GET" && r.Method != "HEAD") {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "text/plain")
		w.Header().Set("X-PathWatch-Generation", token)
		w.Header().Set("X-PathWatch-Remote", r.RemoteAddr)
		_, _ = w.Write([]byte("owned PathWatch echo\n"))
	})
	config := &tls.Config{MinVersion: tls.VersionTLS13, Certificates: []tls.Certificate{pair}}
	tcp := &http.Server{Handler: handler, TLSConfig: config.Clone(), ReadHeaderTimeout: 3 * time.Second, WriteTimeout: 3 * time.Second, IdleTimeout: 5 * time.Second, MaxHeaderBytes: 16384}
	udp := &http3.Server{Handler: handler, TLSConfig: config.Clone(), MaxHeaderBytes: 16384, IdleTimeout: 5 * time.Second}
	defer tcp.Close()
	defer udp.Close()
	errorsOut := make(chan error, 2)
	go func() { errorsOut <- tcp.ServeTLS(tcpListener, "", "") }()
	go func() { errorsOut <- udp.Serve(udpListener) }()
	if ready != "" {
		raw, e := os.ReadFile(cert)
		if e != nil {
			return e
		}
		hash := sha256.Sum256(raw)
		state, _ := json.Marshal(map[string]string{"token": token, "address": addr, "cert_sha256": hex.EncodeToString(hash[:])})
		// Atomic generation record after both sockets bind; clients must also verify TLS and token.
		temp, e := os.CreateTemp(filepath.Dir(ready), ".echo-ready-")
		if e != nil {
			return e
		}
		name := temp.Name()
		defer os.Remove(name)
		if _, e = temp.Write(state); e != nil {
			temp.Close()
			return e
		}
		if e = temp.Close(); e != nil {
			return e
		}
		if e = os.Rename(name, ready); e != nil {
			return e
		}
	}
	select {
	case <-ctx.Done():
		return nil
	case err := <-errorsOut:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	}
}

func main() {
	version := flag.Bool("version", false, "print compatible lifecycle capability version")
	mode := flag.String("mode", "fixture", "fixture, provided, issue, or check")
	addr := flag.String("listen", "127.0.0.1:18443", "literal owned endpoint")
	cert := flag.String("cert", "", "server certificate path")
	key := flag.String("key", "", "server private key path")
	ca := flag.String("ca", "", "private trust CA")
	site := flag.String("site", "", "home, azure-a, or azure-b")
	pki := flag.String("pki-dir", "", "new offline PKI directory; never copied to guests")
	days := flag.Int("valid-days", 45, "offline leaf lifetime in days")
	horizon := flag.Int("min-valid-days", 35, "deployment check horizon")
	ready := flag.String("ready-file", "", "atomic generation readiness path")
	token := flag.String("ready-token", "", "generation token validated by supervisor")
	flag.Parse()
	if *version {
		fmt.Println("pathwatch-echo-private-v1")
		return
	}
	if flag.NArg() != 0 {
		log.Fatal("unexpected positional arguments")
	}
	if *mode == "issue" {
		if err := issuePKI(*pki, *days); err != nil {
			log.Fatal(err)
		}
		return
	}
	if (*ready == "") != (*token == "") {
		log.Fatal("readiness file and token must be supplied together")
	}
	if *ready != "" {
		for _, p := range []string{*cert, *key, *ca} {
			if p != "" {
				a, _ := filepath.Abs(p)
				b, _ := filepath.Abs(*ready)
				if a == b {
					log.Fatal("readiness must not overwrite PKI")
				}
				left, e1 := os.Stat(p)
				right, e2 := os.Stat(*ready)
				if e1 == nil && e2 == nil && os.SameFile(left, right) {
					log.Fatal("readiness must not alias PKI")
				}
			}
		}
	}
	var pair tls.Certificate
	var err error
	switch *mode {
	case "fixture":
		host, _, e := net.SplitHostPort(*addr)
		if e != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
			log.Fatal("literal loopback only")
		}
		if err = certificate(*cert, *key); err == nil {
			pair, err = tls.LoadX509KeyPair(*cert, *key)
		}
	case "provided", "check":
		duration := time.Duration(0)
		if *mode == "check" {
			if *horizon < 35 || *horizon > 60 {
				log.Fatal("deployment horizon must be 35..60 days")
			}
			duration = time.Duration(*horizon) * 24 * time.Hour
		}
		pair, err = privateCertificate(*cert, *key, *ca, *addr, *site, duration)
	default:
		log.Fatal("unknown mode")
	}
	if err != nil {
		log.Fatal(err)
	}
	if *mode == "check" {
		fmt.Println("private certificate covers deployment horizon")
		return
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	if err = serve(ctx, *addr, pair, *cert, *ready, *token); err != nil {
		log.Fatal(err)
	}
}
