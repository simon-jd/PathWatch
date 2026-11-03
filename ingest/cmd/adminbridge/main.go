// Adminbridge exposes only authenticated metrics or checks local readiness.
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"os/signal"
	"pathwatch.local/pathwatch/ingest/internal/adminbridge"
	"syscall"
)

func run(ctx context.Context, args []string, stderr io.Writer) error {
	if len(args) == 0 {
		return fmt.Errorf("mode required: serve or check-ready")
	}
	f := flag.NewFlagSet(args[0], flag.ContinueOnError)
	f.SetOutput(stderr)
	var c adminbridge.Config
	var listen, format string
	f.StringVar(&c.Upstream, "upstream", "127.0.0.1:9091", "canonical local administration IP:port")
	switch args[0] {
	case "serve":
		f.StringVar(&listen, "listen", "0.0.0.0:9445", "literal IP:port")
		f.StringVar(&c.CA, "ca", "", "explicit trust CA PEM")
		f.StringVar(&c.Cert, "cert", "", "server certificate chain PEM")
		f.StringVar(&c.Key, "key", "", "server private key PEM")
		f.StringVar(&c.ServerName, "server-name", "metrics.pathwatch.local", "exact server DNS SAN")
		f.StringVar(&c.ServerURI, "server-uri", "spiffe://pathwatch.local/metrics", "exact server URI SAN")
		f.StringVar(&c.ClientURI, "client-uri", "spiffe://pathwatch.local/prometheus", "exact allowed client URI SAN")
	case "check-ready":
		f.StringVar(&format, "ready-format", "frontend", "explicit readiness body contract")
	default:
		return fmt.Errorf("unknown mode")
	}
	if e := f.Parse(args[1:]); e != nil {
		return e
	}
	if f.NArg() != 0 {
		return fmt.Errorf("unexpected positional arguments")
	}
	if args[0] == "check-ready" {
		return adminbridge.CheckReady(ctx, c.Upstream, format)
	}
	if e := adminbridge.ValidateListen(listen); e != nil {
		return e
	}
	bridge, e := adminbridge.New(c)
	if e != nil {
		return e
	}
	l, e := net.Listen("tcp", listen)
	if e != nil {
		return e
	}
	return bridge.Serve(ctx, l)
}
func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if e := run(ctx, os.Args[1:], os.Stderr); e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
}
