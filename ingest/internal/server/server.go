// Package server validates complete report batches before touching durable state.
package server

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"pathwatch.local/pathwatch/ingest/internal/report"
	"pathwatch.local/pathwatch/ingest/internal/store"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
)

const MaxMessageBytes = report.MaxMessageBytes

var reasons = []string{"invalid_request", "clock_skew", "conflict", "capacity", "unavailable", "overloaded"}

type Server struct {
	pb.UnimplementedIngestServer
	store                *store.Store
	now                  func() time.Time
	slots                chan struct{}
	accepted, duplicates atomic.Uint64
	rejected             [6]atomic.Uint64
	observationMu        sync.Mutex
	windows              map[int64]map[string]bool
}

func New(st *store.Store, now func() time.Time) *Server {
	if now == nil {
		now = time.Now
	}
	return &Server{store: st, now: now, slots: make(chan struct{}, 32), windows: map[int64]map[string]bool{}}
}
func (s *Server) reject(code codes.Code, reason int, msg string) error {
	s.rejected[reason].Add(1)
	st := status.New(code, msg)
	with, e := st.WithDetails(&errdetails.ErrorInfo{Reason: reasons[reason], Domain: "pathwatch.ingest"})
	if e != nil {
		return st.Err()
	}
	return with.Err()
}
func (s *Server) Report(ctx context.Context, r *pb.ReportRequest) (*pb.ReportResponse, error) {
	if e := report.Validate(r); e != nil {
		return nil, s.reject(codes.InvalidArgument, 0, e.Error())
	}
	delta := r.SentAt.AsTime().Sub(s.now())
	if delta > 5*time.Minute || delta < -5*time.Minute {
		return nil, s.reject(codes.FailedPrecondition, 1, "clock_skew")
	}
	select {
	case s.slots <- struct{}{}:
		defer func() { <-s.slots }()
	default:
		return nil, s.reject(codes.ResourceExhausted, 5, "ingest queue full")
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	duplicate, e := s.store.Commit(ctx, r)
	if e != nil {
		switch {
		case errors.Is(e, store.ErrConflict):
			return nil, s.reject(codes.AlreadyExists, 2, e.Error())
		case errors.Is(e, store.ErrCapacity):
			return nil, s.reject(codes.ResourceExhausted, 3, e.Error())
		default:
			return nil, s.reject(codes.Unavailable, 4, "durable write unavailable; retain batch")
		}
	}
	ack := pb.AckStatus_ACK_STATUS_ACCEPTED
	if duplicate {
		ack = pb.AckStatus_ACK_STATUS_DUPLICATE
		s.duplicates.Add(1)
	} else {
		s.accepted.Add(1)
		s.observeCommit(r)
	}
	return &pb.ReportResponse{BatchId: r.BatchId, Status: ack, CommittedResultCount: uint32(len(r.Results))}, nil
}
func (s *Server) Metrics(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	fmt.Fprintf(w, "# TYPE pathwatch_ingest_accepted_batches_total counter\npathwatch_ingest_accepted_batches_total %d\n# TYPE pathwatch_ingest_duplicate_batches_total counter\npathwatch_ingest_duplicate_batches_total %d\n# TYPE pathwatch_ingest_recovered_batches_total counter\npathwatch_ingest_recovered_batches_total %d\n", s.accepted.Load(), s.duplicates.Load(), s.store.Recoveries())
	for i, r := range reasons {
		fmt.Fprintf(w, "pathwatch_ingest_rejected_batches_total{reason=%q} %d\n", r, s.rejected[i].Load())
	}
}

// Overloaded accounts core transport admission at the sole metrics owner.
func (s *Server) Overloaded() error { return s.reject(codes.ResourceExhausted, 5, "ingest queue full") }

// SemanticRejected counts a typed predecode validation refusal exactly once.
func (s *Server) SemanticRejected() { s.rejected[0].Add(1) }
