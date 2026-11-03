package corerpc

import (
	"context"
	"errors"
	"fmt"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"pathwatch.local/pathwatch/ingest/internal/report"
	"pathwatch.local/pathwatch/ingest/internal/store"
	cp "pathwatch.local/pathwatch/proto/pathwatch/core/v1"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"time"
)

type Client struct {
	conn *grpc.ClientConn
	rpc  cp.CoreClient
}
type callIdentity struct{ trust *Trust }

func (callIdentity) RequireTransportSecurity() bool { return true }
func (c callIdentity) GetRequestMetadata(ctx context.Context, _ ...string) (map[string]string, error) {
	i, ok := credentials.RequestInfoFromContext(ctx)
	if !ok {
		return nil, status.Error(codes.Unauthenticated, "missing connection identity")
	}
	t, ok := i.AuthInfo.(credentials.TLSInfo)
	if !ok {
		return nil, status.Error(codes.Unauthenticated, "missing TLS")
	}
	if _, e := c.trust.check(t.State); e != nil {
		return nil, status.Error(codes.Unauthenticated, "expired connection identity")
	}
	return map[string]string{"pathwatch-core-major": "1", "pathwatch-core-minor": "0"}, nil
}

type clientCodec struct{}

func (clientCodec) Name() string { return "proto" }
func (clientCodec) Marshal(v any) ([]byte, error) {
	m, ok := v.(proto.Message)
	if !ok {
		return nil, fmt.Errorf("invalid request")
	}
	limit := MaxQuery
	if _, ok := m.(*pb.ReportRequest); ok {
		limit = report.MaxMessageBytes
	}
	if proto.Size(m) > limit {
		return nil, store.ErrQueryInvalid
	}
	return proto.Marshal(m)
}
func (clientCodec) Unmarshal(b []byte, v any) error {
	if len(b) > MaxReply {
		return store.ErrQueryResponse
	}
	if e := responseShape(b, v); e != nil {
		return e
	}
	m, ok := v.(proto.Message)
	if !ok {
		return store.ErrQueryResponse
	}
	return proto.Unmarshal(b, m)
}
func Dial(ctx context.Context, target string, t *Trust) (*Client, error) {
	if e := ValidateEndpoint(target); e != nil {
		return nil, e
	}
	if t == nil || t.server {
		return nil, fmt.Errorf("client trust required")
	}
	interceptor := func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, invoke grpc.UnaryInvoker, opts ...grpc.CallOption) error {
		d := time.Now().Add(2 * time.Second)
		if method == CorePrefix+"Report" {
			d = time.Now().Add(30 * time.Second)
		}
		if t.leaf.NotAfter.Before(d) {
			d = t.leaf.NotAfter
		}
		ctx, cancel := context.WithDeadline(ctx, d)
		defer cancel()
		var p peer.Peer
		opts = append(opts, grpc.Peer(&p), grpc.MaxRetryRPCBufferSize(0))
		e := invoke(ctx, method, req, reply, cc, opts...)
		if e == nil || p.AuthInfo != nil {
			tlsInfo, ok := p.AuthInfo.(credentials.TLSInfo)
			if !ok {
				return status.Error(codes.Unavailable, "missing response identity")
			}
			if _, err := t.check(tlsInfo.State); err != nil {
				return status.Error(codes.Unavailable, "expired response identity")
			}
		}
		return e
	}
	conn, e := grpc.NewClient("passthrough:///"+target, grpc.WithTransportCredentials(t.Credentials()), grpc.WithPerRPCCredentials(callIdentity{t}), grpc.WithDisableRetry(), grpc.WithDisableServiceConfig(), grpc.WithNoProxy(), grpc.WithInitialWindowSize(64<<10), grpc.WithInitialConnWindowSize(1<<20), grpc.WithMaxHeaderListSize(16<<10), grpc.WithUnaryInterceptor(interceptor), grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(MaxReply), grpc.MaxCallSendMsgSize(report.MaxMessageBytes), grpc.ForceCodec(clientCodec{})))
	if e != nil {
		return nil, e
	}
	c := &Client{conn: conn, rpc: cp.NewCoreClient(conn)}
	if e = c.Ready(ctx); e != nil {
		conn.Close()
		return nil, e
	}
	return c, nil
}
func (c *Client) Close() error { return c.conn.Close() }
func (c *Client) Ready(ctx context.Context) error {
	r, e := c.rpc.Describe(ctx, &cp.DescribeRequest{})
	if e != nil {
		return e
	}
	if r.Major != 1 || r.Capabilities&Capabilities != Capabilities || r.MaxReportBytes != report.MaxMessageBytes || r.MaxQueryBytes != MaxQuery || r.MaxReplyBytes != MaxReply {
		return wireError(codes.FailedPrecondition, "protocol_mismatch")
	}
	if !r.Ready {
		return store.ErrQueryUnavailable
	}
	return nil
}
func (c *Client) Report(ctx context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
	out, e := c.rpc.Report(ctx, r)
	if e != nil {
		if earlyOverload(e, "ingest queue full") {
			return nil, report.Error(codes.ResourceExhausted, "overloaded", "ingest queue full")
		}
		return nil, ReportError(e)
	}
	if out == nil || out.BatchId != r.BatchId || out.CommittedResultCount != uint32(len(r.Results)) || (out.Status != pb.AckStatus_ACK_STATUS_ACCEPTED && out.Status != pb.AckStatus_ACK_STATUS_DUPLICATE) {
		return nil, report.Unavailable()
	}
	return out, nil
}

// earlyOverload is only used after an authenticated, negotiated core call.
// Pinned grpc tap rejection omits status details; no other string fallback exists.
func earlyOverload(e error, literal string) bool {
	s := status.Convert(e)
	return s.Code() == codes.ResourceExhausted && len(s.Proto().Details) == 0 && s.Message() == literal
}
func ReportError(e error) error {
	if e == nil {
		return nil
	}
	s := status.Convert(e)
	if len(s.Proto().Details) != 1 {
		return report.Unavailable()
	}
	for _, d := range s.Details() {
		x, ok := d.(*errdetails.ErrorInfo)
		if !ok || x.Domain != "pathwatch.ingest" {
			continue
		}
		expected := map[string]codes.Code{"invalid_request": codes.InvalidArgument, "clock_skew": codes.FailedPrecondition, "conflict": codes.AlreadyExists, "capacity": codes.ResourceExhausted, "unavailable": codes.Unavailable, "overloaded": codes.ResourceExhausted}
		if code, ok := expected[x.Reason]; ok && code == s.Code() && safeReportMessage(x.Reason, s.Message()) {
			return report.Error(code, x.Reason, s.Message())
		}
	}
	return report.Unavailable()
}
func safeReportMessage(reason, msg string) bool {
	switch reason {
	case "clock_skew":
		return msg == "clock_skew"
	case "conflict":
		return msg == "batch key has different ordered content"
	case "capacity":
		return msg == "journal capacity reached"
	case "unavailable":
		return msg == "durable write unavailable; retain batch"
	case "overloaded":
		return msg == "ingest queue full"
	case "invalid_request":
		switch msg {
		case "invalid request: sent_at required and valid", "invalid request: identity, batch size or message size", "invalid request: result identity or timestamp outside DateTime TTL range", "invalid request: kind", "invalid request: result metadata bounds", "invalid request: missing address without remote error", "invalid request: address/family", "invalid request: HTTP protocol", "invalid request: TLS metadata", "invalid request: phase name/duration", "invalid request: hop":
			return true
		}
	}
	return false
}
func readError(e error) error {
	if e == nil {
		return nil
	}
	if earlyOverload(e, "overloaded") {
		return store.ErrQueryOverloaded
	}
	if e == nil {
		return nil
	}
	s := status.Convert(e)
	switch s.Code() {
	case codes.Canceled:
		return context.Canceled
	case codes.DeadlineExceeded:
		return context.DeadlineExceeded
	case codes.Unavailable, codes.Unauthenticated, codes.PermissionDenied, codes.FailedPrecondition:
		return store.ErrQueryUnavailable
	}
	if len(s.Proto().Details) != 1 {
		return store.ErrQueryResponse
	}
	for _, d := range s.Details() {
		x, ok := d.(*errdetails.ErrorInfo)
		if !ok || x.Domain != "pathwatch.core" || x.Reason != s.Message() || len(x.Metadata) != 0 {
			continue
		}
		switch {
		case s.Code() == codes.InvalidArgument && x.Reason == "invalid_request":
			return store.ErrQueryInvalid
		case s.Code() == codes.ResourceExhausted && x.Reason == "overloaded":
			return store.ErrQueryOverloaded
		case s.Code() == codes.ResourceExhausted && x.Reason == "result_limit":
			return store.ErrQueryLimit
		case s.Code() == codes.DataLoss && x.Reason == "invalid_upstream_response":
			return store.ErrQueryResponse
		case s.Code() == codes.Internal && x.Reason == "internal_error":
			return errors.New("core internal query error")
		}
	}
	return store.ErrQueryResponse
}
func query(b store.QueryBounds) *cp.QueryRequest {
	return &cp.QueryRequest{PathKey: b.PathKey, From: timestamppb.New(b.From), To: timestamppb.New(b.To), Limit: uint32(b.Limit)}
}
func (c *Client) ReadRaw(ctx context.Context, b store.QueryBounds) ([]store.RawResult, error) {
	if e := b.Validate(false, time.Now()); e != nil {
		return nil, e
	}
	r, e := c.rpc.ReadRaw(ctx, query(b))
	if e != nil {
		return nil, readError(e)
	}
	if r == nil || len(r.Rows) > b.Limit {
		return nil, store.ErrQueryResponse
	}
	out := []store.RawResult{}
	for _, r := range r.Rows {
		v, e := rawFromWire(r)
		if e != nil {
			return nil, e
		}
		out = append(out, v)
	}
	return out, nil
}
func (c *Client) ReadQuantiles(ctx context.Context, b store.QueryBounds) ([]store.Quantile, error) {
	if e := b.Validate(true, time.Now()); e != nil {
		return nil, e
	}
	r, e := c.rpc.ReadQuantiles(ctx, query(b))
	if e != nil {
		return nil, readError(e)
	}
	if r == nil || len(r.Rows) > b.Limit {
		return nil, store.ErrQueryResponse
	}
	out := []store.Quantile{}
	for _, r := range r.Rows {
		v, e := quantileFromWire(r)
		if e != nil {
			return nil, e
		}
		out = append(out, v)
	}
	return out, nil
}
func (c *Client) ReadEvents(ctx context.Context, b store.QueryBounds) ([]store.Event, error) {
	if e := b.Validate(false, time.Now()); e != nil {
		return nil, e
	}
	r, e := c.rpc.ReadEvents(ctx, query(b))
	if e != nil {
		return nil, readError(e)
	}
	if r == nil || len(r.Rows) > b.Limit {
		return nil, store.ErrQueryResponse
	}
	out := []store.Event{}
	for _, r := range r.Rows {
		v, e := eventFromWire(r)
		if e != nil {
			return nil, e
		}
		out = append(out, v)
	}
	return out, nil
}
