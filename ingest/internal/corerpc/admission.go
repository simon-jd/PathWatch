// Package corerpc carries bounded typed calls to the sole durable owner.
package corerpc

import (
	"context"
	"errors"
	"fmt"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/stats"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/tap"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
	"pathwatch.local/pathwatch/ingest/internal/report"
	cp "pathwatch.local/pathwatch/proto/pathwatch/core/v1"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"sync"
	"sync/atomic"
	"time"
)

const MaxQuery = 4096
const MaxReply = 1 << 20
const Capabilities = 15
const CorePrefix = "/pathwatch.core.v1.Core/"

type leaseKey struct{}
type Admission struct {
	reports, reads         chan struct{}
	mu                     sync.Mutex
	bytes, limit           int64
	private                bool
	closing                atomic.Bool
	authenticate           func(context.Context) (time.Time, error)
	overload               func() error
	semanticReportRejected func()
	queries                context.Context
	cancelQueries          context.CancelFunc
	active                 sync.WaitGroup
}
type lease struct {
	mu      sync.Mutex
	state   int
	owner   *Admission
	slot    chan struct{}
	charge  int64
	cancel  context.CancelFunc
	stop    func() bool
	failure error
}

func NewAdmission(private bool, auth func(context.Context) (time.Time, error), overload func() error, semanticReportRejected func()) *Admission {
	n, limit := 4, int64(768<<20)
	if private {
		n = 32
		limit = 1 << 30
	}
	q, c := context.WithCancel(context.Background())
	return &Admission{reports: make(chan struct{}, n), reads: make(chan struct{}, 4), limit: limit, private: private, authenticate: auth, overload: overload, semanticReportRejected: semanticReportRejected, queries: q, cancelQueries: c}
}
func (a *Admission) Stop() { a.mu.Lock(); a.closing.Store(true); a.mu.Unlock(); a.cancelQueries() }
func (a *Admission) Wait() { a.active.Wait() }
func (a *Admission) Tap(ctx context.Context, i *tap.Info) (context.Context, error) {
	if a.closing.Load() {
		return ctx, status.Error(codes.Unavailable, "draining")
	}
	isReport := i.FullMethodName == CorePrefix+"Report"
	known := isReport
	if a.private {
		switch i.FullMethodName {
		case CorePrefix + "ReadRaw", CorePrefix + "ReadQuantiles", CorePrefix + "ReadEvents", CorePrefix + "Describe":
			known = true
		}
		if !known {
			return ctx, status.Error(codes.Unimplemented, "unknown method")
		}
		if !versionOK(i.Header) {
			return ctx, wireError(codes.FailedPrecondition, "protocol_mismatch")
		}
	} else {
		isReport = i.FullMethodName == "/pathwatch.v1.Ingest/Report"
		if !isReport {
			return ctx, status.Error(codes.Unimplemented, "unknown method")
		}
	}
	if enc := i.Header.Get("grpc-encoding"); len(enc) > 0 && enc[0] != "identity" {
		return ctx, status.Error(codes.Unimplemented, "compression disabled")
	}
	d := time.Now().Add(2 * time.Second)
	slot := a.reads
	if isReport {
		d = time.Now().Add(30 * time.Second)
		slot = a.reports
	}
	if a.authenticate != nil {
		until, e := a.authenticate(ctx)
		if e != nil {
			return ctx, e
		}
		if until.Before(d) {
			d = until
		}
	}
	a.mu.Lock()
	if a.closing.Load() {
		a.mu.Unlock()
		return ctx, status.Error(codes.Unavailable, "draining")
	}
	select {
	case slot <- struct{}{}:
	default:
		a.mu.Unlock()
		if isReport && a.overload != nil {
			return ctx, a.overload()
		}
		return ctx, wireError(codes.ResourceExhausted, "overloaded")
	}
	ctx, cancel := context.WithDeadline(ctx, d)
	l := &lease{owner: a, slot: slot, cancel: cancel}
	a.active.Add(1)
	a.mu.Unlock()
	ctx = context.WithValue(ctx, leaseKey{}, l)
	l.stop = context.AfterFunc(ctx, func() { l.release(false) })
	if !isReport {
		stop := context.AfterFunc(a.queries, cancel)
		context.AfterFunc(ctx, func() { stop() })
	}
	return ctx, nil
}
func versionOK(m metadata.MD) bool {
	return len(m.Get("pathwatch-core-major")) == 1 && m.Get("pathwatch-core-major")[0] == "1" && len(m.Get("pathwatch-core-minor")) == 1 && (m.Get("pathwatch-core-minor")[0] == "0" || m.Get("pathwatch-core-minor")[0] == "1")
}
func (l *lease) release(end bool) {
	l.mu.Lock()
	if l.state == 2 || (!end && l.state == 1) {
		l.mu.Unlock()
		return
	}
	l.state = 2
	charge := l.charge
	l.charge = 0
	l.mu.Unlock()
	l.cancel()
	l.owner.mu.Lock()
	l.owner.bytes -= charge
	l.owner.mu.Unlock()
	<-l.slot
	l.owner.active.Done()
}
func (a *Admission) TagRPC(ctx context.Context, _ *stats.RPCTagInfo) context.Context {
	if l, ok := ctx.Value(leaseKey{}).(*lease); ok {
		l.mu.Lock()
		if l.state == 0 {
			l.state = 1
		}
		l.mu.Unlock()
	}
	return ctx
}
func (a *Admission) HandleRPC(ctx context.Context, s stats.RPCStats) {
	if _, ok := s.(*stats.End); ok {
		if l, ok := ctx.Value(leaseKey{}).(*lease); ok {
			l.release(true)
		}
	}
}
func (a *Admission) TagConn(ctx context.Context, _ *stats.ConnTagInfo) context.Context { return ctx }
func (a *Admission) HandleConn(context.Context, stats.ConnStats)                       {}
func (l *lease) reserve(n int64) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.state != 1 {
		return context.Canceled
	}
	a := l.owner
	a.mu.Lock()
	defer a.mu.Unlock()
	if n > a.limit-a.bytes {
		return wireError(codes.ResourceExhausted, "overloaded")
	}
	a.bytes += n
	l.charge += n
	return nil
}

type decodeEnvelope struct {
	message proto.Message
	lease   *lease
}
type codec struct{}

func (codec) Name() string { return "proto" }
func (codec) Marshal(v any) ([]byte, error) {
	m, ok := v.(proto.Message)
	if !ok {
		return nil, fmt.Errorf("unexpected message")
	}
	if proto.Size(m) > MaxReply {
		return nil, wireError(codes.ResourceExhausted, "result_limit")
	}
	return proto.Marshal(m)
}
func (codec) Unmarshal(b []byte, v any) error {
	e, ok := v.(*decodeEnvelope)
	if !ok {
		return fmt.Errorf("missing decode lease")
	}
	fail := func(err error) error { e.lease.failure = err; return err }
	max := MaxQuery
	_, isReport := e.message.(*pb.ReportRequest)
	if isReport {
		max = report.MaxMessageBytes
	}
	if len(b) > max {
		return fail(wireError(codes.ResourceExhausted, "result_limit"))
	}
	n := int64(len(b)*4 + 1024)
	if isReport {
		var err error
		n, err = report.Charge(b)
		if err != nil {
			var semantic *report.SemanticLimit
			if errors.As(err, &semantic) {
				if e.lease.owner.semanticReportRejected != nil {
					e.lease.owner.semanticReportRejected()
				}
				return fail(semantic)
			}
			return fail(status.Error(codes.InvalidArgument, "invalid protobuf structure"))
		}
	}
	if err := e.lease.reserve(n); err != nil {
		if isReport && e.lease.owner.overload != nil && status.Code(err) == codes.ResourceExhausted {
			return fail(e.lease.owner.overload())
		}
		return fail(err)
	}
	if err := proto.Unmarshal(b, e.message); err != nil {
		return fail(status.Error(codes.InvalidArgument, "invalid protobuf"))
	}
	return nil
}

// Register wraps only this service; protobuf envelopes never appear on the wire.
func Register(g *grpc.Server, desc *grpc.ServiceDesc, impl any) {
	d := *desc
	d.Methods = append([]grpc.MethodDesc(nil), desc.Methods...)
	for i := range d.Methods {
		original := d.Methods[i].Handler
		d.Methods[i].Handler = func(s any, ctx context.Context, dec func(any) error, in grpc.UnaryServerInterceptor) (any, error) {
			l, ok := ctx.Value(leaseKey{}).(*lease)
			if !ok {
				return nil, status.Error(codes.Internal, "missing admission")
			}
			if err := ctx.Err(); err != nil {
				return nil, status.FromContextError(err).Err()
			}
			r, err := original(s, ctx, func(v any) error {
				m, ok := v.(proto.Message)
				if !ok {
					return status.Error(codes.Internal, "invalid decode destination")
				}
				return dec(&decodeEnvelope{m, l})
			}, in)
			if l.failure != nil {
				return nil, l.failure
			}
			if ctx.Err() != nil {
				return nil, status.FromContextError(ctx.Err()).Err()
			}
			if err == nil {
				if m, ok := r.(proto.Message); !ok || proto.Size(m) > MaxReply {
					return nil, wireError(codes.ResourceExhausted, "result_limit")
				}
			}
			return r, err
		}
	}
	g.RegisterService(&d, impl)
}

// Response framing is capped by the client before this bounded structural scan.
func responseShape(b []byte, v any) error {
	count := 0
	return report.Fields(b, func(n protowire.Number, t protowire.Type, x []byte) error {
		if n != 1 || t != protowire.BytesType {
			return nil
		}
		switch v.(type) {
		case *cp.RawResponse, *cp.QuantilesResponse, *cp.EventsResponse:
			count++
			if count > 256 {
				return fmt.Errorf("too many rows")
			}
		default:
			return nil
		}
		if _, ok := v.(*cp.RawResponse); !ok {
			return nil
		}
		keys := map[string]bool{}
		counts := map[protowire.Number]int{}
		return report.Fields(x, func(n protowire.Number, t protowire.Type, y []byte) error {
			switch n {
			case 11:
				if t == protowire.BytesType {
					var keyBytes []byte
					if e := report.Fields(y, func(k protowire.Number, t protowire.Type, z []byte) error {
						if k == 1 && t == protowire.BytesType {
							keyBytes = z
						}
						return nil
					}); e != nil {
						return e
					}
					if len(keyBytes) > 128 {
						return fmt.Errorf("phase key too large")
					}
					keys[string(keyBytes)] = true
					if len(keys) > 16 {
						return fmt.Errorf("too many phases")
					}
				}
			case 14, 16:
				if t == protowire.BytesType {
					for len(y) > 0 {
						_, k := protowire.ConsumeVarint(y)
						if k < 0 {
							return protowire.ParseError(k)
						}
						counts[n]++
						y = y[k:]
					}
				} else {
					counts[n]++
				}
				if counts[n] > 255 {
					return fmt.Errorf("too many hop values")
				}
			case 15:
				counts[n]++
				if counts[n] > 255 {
					return fmt.Errorf("too many hop addresses")
				}
			}
			return nil
		})
	})
}
