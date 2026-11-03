// Labpki writes the short-lived lab CA and leaf identities used by the kind
// deployment (deploy/README.md). Output stays under .cache/; the CA key is never
// mounted into a pod. It refuses to overwrite an existing directory.
package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"flag"
	"fmt"
	"math/big"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

const (
	CoreServerName = "core.pathwatch-app.svc"
	EdgeServerName = "api.pathwatch.local"
	FrontendURI    = "spiffe://pathwatch.local/frontend/kind"
)

type leaf struct {
	name  string
	dns   []string
	uri   string
	usage x509.ExtKeyUsage
}

var leaves = []leaf{
	{"core", []string{CoreServerName}, "", x509.ExtKeyUsageServerAuth},
	{"frontend", nil, FrontendURI, x509.ExtKeyUsageClientAuth},
	{"metrics", []string{"metrics.pathwatch.local"}, "spiffe://pathwatch.local/metrics", x509.ExtKeyUsageServerAuth},
	{"prometheus", nil, "spiffe://pathwatch.local/prometheus", x509.ExtKeyUsageClientAuth},
	{"edge", []string{EdgeServerName}, "", x509.ExtKeyUsageServerAuth},
}

func main() {
	out := flag.String("out", "", "new directory for the PKI (must not exist)")
	validity := flag.Duration("validity", 72*time.Hour, "leaf and CA lifetime")
	extra := flag.String("extra-servers", "", "comma-separated DNS names; one serverAuth leaf <name>.pem each (lab services)")
	flag.Parse()
	if *out == "" {
		fmt.Fprintln(os.Stderr, "-out required")
		os.Exit(2)
	}
	var names []string
	if *extra != "" {
		names = strings.Split(*extra, ",")
	}
	if err := generate(*out, *validity, names...); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

var dnsLabel = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)

func generate(dir string, validity time.Duration, extraServers ...string) error {
	all := leaves
	for _, name := range extraServers {
		if !dnsLabel.MatchString(name) {
			return fmt.Errorf("extra server %q is not a DNS label", name)
		}
		all = append(all[:len(all):len(all)], leaf{name, []string{name}, "", x509.ExtKeyUsageServerAuth})
	}
	if err := os.Mkdir(dir, 0o700); err != nil {
		return err
	}
	now := time.Now().Add(-5 * time.Minute)
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return err
	}
	ca := &x509.Certificate{SerialNumber: serial(), Subject: pkix.Name{CommonName: "pathwatch kind lab CA"}, NotBefore: now, NotAfter: now.Add(validity),
		IsCA: true, BasicConstraintsValid: true, MaxPathLenZero: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageCRLSign}
	caDER, err := x509.CreateCertificate(rand.Reader, ca, ca, &caKey.PublicKey, caKey)
	if err != nil {
		return err
	}
	if err := write(dir, "ca", caDER, caKey); err != nil {
		return err
	}
	ca, _ = x509.ParseCertificate(caDER)
	for _, l := range all {
		key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if err != nil {
			return err
		}
		c := &x509.Certificate{SerialNumber: serial(), Subject: pkix.Name{CommonName: l.name}, NotBefore: now, NotAfter: now.Add(validity),
			KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{l.usage}, DNSNames: l.dns}
		if l.uri != "" {
			u, err := url.Parse(l.uri)
			if err != nil {
				return err
			}
			c.URIs = []*url.URL{u}
		}
		der, err := x509.CreateCertificate(rand.Reader, c, ca, &key.PublicKey, caKey)
		if err != nil {
			return err
		}
		if err := write(dir, l.name, der, key); err != nil {
			return err
		}
	}
	return nil
}

func serial() *big.Int {
	n, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 120))
	if err != nil {
		panic(err)
	}
	return n
}

func write(dir, name string, der []byte, key *ecdsa.PrivateKey) error {
	pkcs8, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(dir, name+".pem"), pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o644); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, name+"-key.pem"), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: pkcs8}), 0o600)
}
