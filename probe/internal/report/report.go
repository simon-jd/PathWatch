// Package report drains durable batches through a single bounded gRPC worker.
package report

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"math/rand/v2"
	"net"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/types/known/timestamppb"
	"pathwatch.local/pathwatch/probe/internal/result"
	"pathwatch.local/pathwatch/probe/internal/schedule"
	"pathwatch.local/pathwatch/probe/internal/spool"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
)

type Config struct {
	ProbeID, Endpoint, CAFile string
	DevelopmentCleartext      bool
	RPCTimeout                time.Duration
	// Dialer changes only the underlying transport; TLS verification still runs.
	Dialer  func(context.Context, string) (net.Conn, error)
	Now     func() time.Time
	Wait    func(context.Context, time.Duration) error
	Random  func() float64
	OnError func(error)
}
type Stats struct{ Attempts, Acknowledged, Failures uint64 }
type Client struct {
	conn                             *grpc.ClientConn
	rpc                              pb.IngestClient
	probeID                          string
	timeout                          time.Duration
	now                              func() time.Time
	wait                             func(context.Context, time.Duration) error
	random                           func() float64
	onError                          func(error)
	running                          sync.Mutex
	attempts, acknowledged, failures atomic.Uint64
}

// New uses verified TLS and system roots by default. Development cleartext
// requires a literal loopback address; resolver aliases/remote hosts are refused.
func New(cfg Config) (*Client, error) {
	if strings.TrimSpace(cfg.ProbeID) == "" {
		return nil, fmt.Errorf("probe ID is required")
	}
	host, port, err := net.SplitHostPort(cfg.Endpoint)
	if err != nil || host == "" || strings.ContainsAny(host, "/ %@") {
		return nil, fmt.Errorf("ingest endpoint must be host:port")
	}
	p, err := strconv.Atoi(port)
	if err != nil || p < 1 || p > 65535 {
		return nil, fmt.Errorf("ingest port must be in 1..65535")
	}
	var creds credentials.TransportCredentials
	if cfg.DevelopmentCleartext {
		ip := net.ParseIP(host)
		if ip == nil || !ip.IsLoopback() {
			return nil, fmt.Errorf("development cleartext requires a literal loopback endpoint")
		}
		if cfg.CAFile != "" {
			return nil, fmt.Errorf("CA cannot be used with development cleartext")
		}
		creds = insecure.NewCredentials()
	} else {
		tlsCfg := &tls.Config{MinVersion: tls.VersionTLS12, ServerName: host}
		if cfg.CAFile != "" {
			data, e := os.ReadFile(cfg.CAFile)
			if e != nil {
				return nil, e
			}
			roots, e := x509.SystemCertPool()
			if e != nil {
				roots = x509.NewCertPool()
			}
			if !roots.AppendCertsFromPEM(data) {
				return nil, fmt.Errorf("CA file contains no certificates")
			}
			tlsCfg.RootCAs = roots
		}
		creds = credentials.NewTLS(tlsCfg)
	}
	if cfg.RPCTimeout == 0 {
		cfg.RPCTimeout = 10 * time.Second
	}
	if cfg.RPCTimeout < 0 {
		return nil, fmt.Errorf("RPC timeout must be positive")
	}
	opts := []grpc.DialOption{grpc.WithTransportCredentials(creds), grpc.WithDefaultCallOptions(grpc.MaxCallSendMsgSize(32<<20), grpc.MaxCallRecvMsgSize(1<<20))}
	if cfg.Dialer != nil {
		opts = append(opts, grpc.WithContextDialer(cfg.Dialer))
	}
	conn, err := grpc.NewClient(cfg.Endpoint, opts...)
	if err != nil {
		return nil, err
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.Wait == nil {
		cfg.Wait = schedule.RealClock{}.Wait
	}
	if cfg.Random == nil {
		cfg.Random = rand.Float64
	}
	return &Client{conn: conn, rpc: pb.NewIngestClient(conn), probeID: cfg.ProbeID, timeout: cfg.RPCTimeout, now: cfg.Now, wait: cfg.Wait, random: cfg.Random, onError: cfg.OnError}, nil
}
func (c *Client) Close() error { return c.conn.Close() }
func (c *Client) Stats() Stats {
	return Stats{c.attempts.Load(), c.acknowledged.Load(), c.failures.Load()}
}

// Run owns one batch snapshot and performs RPC synchronously. ACK loss retries
// the same ID/content with a new sent_at. Failed/partial ACKs retain the batch.
// Concurrent Run calls are rejected, including across different spool objects.
func (c *Client) Run(ctx context.Context, s *spool.Spool) error {
	if s == nil {
		return fmt.Errorf("spool is required")
	}
	if !c.running.TryLock() {
		return fmt.Errorf("reporter already running")
	}
	defer c.running.Unlock()
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		id, rs, ack, err := s.Next()
		if errors.Is(err, spool.ErrEmpty) {
			if err = c.wait(ctx, 250*time.Millisecond); err != nil {
				return err
			}
			continue
		}
		if err != nil {
			return err
		}
		wire, err := toWire(c.probeID, rs)
		if err != nil {
			return err
		}
		cap := time.Second
		for {
			if err = ctx.Err(); err != nil {
				return err
			}
			request := &pb.ReportRequest{BatchId: id, ProbeId: c.probeID, SentAt: timestamppb.New(c.now()), Results: wire}
			if err = request.SentAt.CheckValid(); err != nil {
				return fmt.Errorf("invalid sent_at: %w", err)
			}
			attempt, cancel := context.WithTimeout(ctx, c.timeout)
			c.attempts.Add(1)
			response, rpcErr := c.rpc.Report(attempt, request)
			cancel()
			if rpcErr == nil {
				if response == nil || response.BatchId != id || (response.Status != pb.AckStatus_ACK_STATUS_ACCEPTED && response.Status != pb.AckStatus_ACK_STATUS_DUPLICATE) || response.CommittedResultCount != uint32(len(wire)) {
					rpcErr = fmt.Errorf("invalid or partial ingest ACK for batch %s", id)
				}
			}
			if rpcErr == nil {
				if err = ack(); err != nil {
					return err
				}
				c.acknowledged.Add(1)
				break
			}
			c.failures.Add(1)
			if c.onError != nil {
				c.onError(rpcErr)
			}
			if err = ctx.Err(); err != nil {
				return err
			}
			sample := c.random()
			if sample < 0 || sample > 1 {
				return fmt.Errorf("random sample outside [0,1]")
			}
			if err = c.wait(ctx, time.Duration(sample*float64(cap))); err != nil {
				return err
			}
			cap = min(cap*2, 5*time.Minute)
		}
	}
}
func toWire(probeID string, rs []result.Result) ([]*pb.ProbeResult, error) {
	out := make([]*pb.ProbeResult, 0, len(rs))
	for _, r := range rs {
		if r.ProbeID != probeID {
			return nil, fmt.Errorf("result probe ID %q differs from reporter %q", r.ProbeID, probeID)
		}
		at := timestamppb.New(r.StartedAt)
		if err := at.CheckValid(); err != nil {
			return nil, fmt.Errorf("invalid measurement timestamp: %w", err)
		}
		phases := make(map[string]int64, len(r.Phases))
		for k, v := range r.Phases {
			phases[k] = int64(v)
		}
		hops := make([]*pb.Hop, 0, len(r.Hops))
		for _, h := range r.Hops {
			if h.TTL < 0 {
				return nil, fmt.Errorf("negative hop TTL")
			}
			hops = append(hops, &pb.Hop{Ttl: uint32(h.TTL), Addr: h.Addr, RttNanoseconds: int64(h.RTT)})
		}
		out = append(out, &pb.ProbeResult{TargetId: r.TargetID, ProbeId: r.ProbeID, Kind: string(r.Kind), Family: r.Family, Addr: r.Addr, StartedAt: at, PhasesNanoseconds: phases, Err: r.Err, Protocol: r.Protocol, Hops: hops, TlsVersion: uint32(r.TLSVersion), TlsResumed: r.TLSResumed})
	}
	return out, nil
}
