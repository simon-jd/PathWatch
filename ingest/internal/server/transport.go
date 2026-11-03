package server

import (
	"crypto/tls"
	"fmt"
	"net"
	"strconv"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
)

type TransportConfig struct {
	Listen, CertFile, KeyFile string
	DevelopmentCleartext      bool
}

func Options(c TransportConfig) ([]grpc.ServerOption, error) {
	host, port, e := net.SplitHostPort(c.Listen)
	if e != nil {
		return nil, e
	}
	p, e := strconv.Atoi(port)
	if e != nil || p < 1 || p > 65535 {
		return nil, fmt.Errorf("listen port must be 1..65535")
	}
	opts := []grpc.ServerOption{grpc.MaxRecvMsgSize(MaxMessageBytes), grpc.MaxHeaderListSize(16 << 10), grpc.MaxConcurrentStreams(32)}
	if c.DevelopmentCleartext {
		ip := net.ParseIP(host)
		if ip == nil || !ip.IsLoopback() || c.CertFile != "" || c.KeyFile != "" {
			return nil, fmt.Errorf("development cleartext requires literal loopback and no TLS files")
		}
		return opts, nil
	}
	if c.CertFile == "" || c.KeyFile == "" {
		return nil, fmt.Errorf("TLS certificate and key required")
	}
	cert, e := tls.LoadX509KeyPair(c.CertFile, c.KeyFile)
	if e != nil {
		return nil, e
	}
	return append(opts, grpc.Creds(credentials.NewTLS(&tls.Config{MinVersion: tls.VersionTLS12, Certificates: []tls.Certificate{cert}}))), nil
}
