package corerpc

import (
	"context"
	"errors"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"pathwatch.local/pathwatch/ingest/internal/store"
	cp "pathwatch.local/pathwatch/proto/pathwatch/core/v1"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"time"
)

type Reporter interface {
	Report(context.Context, *pb.ReportRequest) (*pb.ReportResponse, error)
}
type Service struct {
	cp.UnimplementedCoreServer
	Reporter Reporter
	Reader   store.MeasurementReader
	Ready    func(context.Context) error
}

func (s *Service) Report(ctx context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
	return s.Reporter.Report(ctx, r)
}
func wireError(code codes.Code, reason string) error {
	v, e := status.New(code, reason).WithDetails(&errdetails.ErrorInfo{Domain: "pathwatch.core", Reason: reason})
	if e != nil {
		return status.Error(code, reason)
	}
	return v.Err()
}
func queryError(e error) error {
	switch {
	case e == nil:
		return nil
	case errors.Is(e, context.Canceled):
		return status.Error(codes.Canceled, "canceled")
	case errors.Is(e, context.DeadlineExceeded):
		return status.Error(codes.DeadlineExceeded, "query deadline exceeded")
	case errors.Is(e, store.ErrQueryInvalid):
		return wireError(codes.InvalidArgument, "invalid_request")
	case errors.Is(e, store.ErrQueryOverloaded):
		return wireError(codes.ResourceExhausted, "overloaded")
	case errors.Is(e, store.ErrQueryLimit):
		return wireError(codes.ResourceExhausted, "result_limit")
	case errors.Is(e, store.ErrQueryUnavailable):
		return wireError(codes.Unavailable, "store_unavailable")
	case errors.Is(e, store.ErrQueryResponse):
		return wireError(codes.DataLoss, "invalid_upstream_response")
	default:
		return wireError(codes.Internal, "internal_error")
	}
}
func bounds(r *cp.QueryRequest, q bool) (store.QueryBounds, error) {
	if r == nil || r.From == nil || r.To == nil || r.From.CheckValid() != nil || r.To.CheckValid() != nil {
		return store.QueryBounds{}, store.ErrQueryInvalid
	}
	b := store.QueryBounds{PathKey: r.PathKey, From: r.From.AsTime(), To: r.To.AsTime(), Limit: int(r.Limit)}
	return b, b.Validate(q, time.Now())
}
func (s *Service) ReadRaw(ctx context.Context, r *cp.QueryRequest) (*cp.RawResponse, error) {
	b, e := bounds(r, false)
	if e != nil {
		return nil, queryError(e)
	}
	rows, e := s.Reader.ReadRaw(ctx, b)
	if e != nil {
		return nil, queryError(e)
	}
	out := &cp.RawResponse{}
	for _, v := range rows {
		x, e := rawToWire(v)
		if e != nil {
			return nil, queryError(e)
		}
		out.Rows = append(out.Rows, x)
	}
	return out, nil
}
func (s *Service) ReadQuantiles(ctx context.Context, r *cp.QueryRequest) (*cp.QuantilesResponse, error) {
	b, e := bounds(r, true)
	if e != nil {
		return nil, queryError(e)
	}
	rows, e := s.Reader.ReadQuantiles(ctx, b)
	if e != nil {
		return nil, queryError(e)
	}
	out := &cp.QuantilesResponse{}
	for _, v := range rows {
		x, e := quantileToWire(v)
		if e != nil {
			return nil, queryError(e)
		}
		out.Rows = append(out.Rows, x)
	}
	return out, nil
}
func (s *Service) ReadEvents(ctx context.Context, r *cp.QueryRequest) (*cp.EventsResponse, error) {
	b, e := bounds(r, false)
	if e != nil {
		return nil, queryError(e)
	}
	rows, e := s.Reader.ReadEvents(ctx, b)
	if e != nil {
		return nil, queryError(e)
	}
	out := &cp.EventsResponse{}
	for _, v := range rows {
		x, e := eventToWire(v)
		if e != nil {
			return nil, queryError(e)
		}
		out.Rows = append(out.Rows, x)
	}
	return out, nil
}
func (s *Service) Describe(ctx context.Context, _ *cp.DescribeRequest) (*cp.DescribeResponse, error) {
	ready := s.Ready != nil && s.Ready(ctx) == nil
	return &cp.DescribeResponse{Major: 1, Minor: 0, Capabilities: Capabilities, MaxReportBytes: 16 << 20, MaxQueryBytes: MaxQuery, MaxReplyBytes: MaxReply, Ready: ready}, nil
}
