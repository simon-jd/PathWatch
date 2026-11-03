// Package frontend validates and forwards actual Report traffic without durable state.
package frontend

import (
	"context"
	"fmt"
	"google.golang.org/grpc/codes"
	"net/http"
	"pathwatch.local/pathwatch/ingest/internal/corerpc"
	"pathwatch.local/pathwatch/ingest/internal/report"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"sync/atomic"
	"time"
)

type Server struct {
	pb.UnimplementedIngestServer
	core        corerpc.Reporter
	now         func() time.Time
	ack, failed atomic.Uint64
}

func New(c corerpc.Reporter, now func() time.Time) *Server {
	if now == nil {
		now = time.Now
	}
	return &Server{core: c, now: now}
}
func (s *Server) Report(ctx context.Context, r *pb.ReportRequest) (response *pb.ReportResponse, err error) {
	defer func() {
		if err != nil {
			s.failed.Add(1)
		} else {
			s.ack.Add(1)
		}
	}()
	if e := report.Check(r, s.now()); e != nil {
		return nil, e
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	v, e := s.core.Report(ctx, r)
	if e != nil {
		return nil, corerpc.ReportError(e)
	}
	if v == nil || v.BatchId != r.BatchId || v.CommittedResultCount != uint32(len(r.Results)) || (v.Status != pb.AckStatus_ACK_STATUS_ACCEPTED && v.Status != pb.AckStatus_ACK_STATUS_DUPLICATE) {
		return nil, report.Unavailable()
	}
	if ctx.Err() != nil {
		return nil, report.Unavailable()
	}
	return v, nil
}
func (s *Server) Overloaded() error {
	s.failed.Add(1)
	return report.Error(codes.ResourceExhausted, "overloaded", "ingest queue full")
}
func (s *Server) Metrics(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	fmt.Fprintf(w, "# TYPE pathwatch_frontend_report_requests_total counter\npathwatch_frontend_report_requests_total{outcome=\"acknowledged\"} %d\npathwatch_frontend_report_requests_total{outcome=\"error\"} %d\n", s.ack.Load(), s.failed.Load())
}

// SemanticRejected counts a typed predecode validation refusal exactly once.
func (s *Server) SemanticRejected() { s.failed.Add(1) }
