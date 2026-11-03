// Package report owns the unchanged public Report validation contract.
package report

import (
	"fmt"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"net"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"regexp"
	"time"
	"unicode/utf8"
)

const MaxMessageBytes = 16 << 20

var identity = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`)
var Reasons = []string{"invalid_request", "clock_skew", "conflict", "capacity", "unavailable", "overloaded"}

func Error(code codes.Code, reason, message string) error {
	s := status.New(code, message)
	d, e := s.WithDetails(&errdetails.ErrorInfo{Reason: reason, Domain: "pathwatch.ingest"})
	if e != nil {
		return s.Err()
	}
	return d.Err()
}
func Check(r *pb.ReportRequest, now time.Time) error {
	if e := Validate(r); e != nil {
		return Error(codes.InvalidArgument, "invalid_request", e.Error())
	}
	delta := r.SentAt.AsTime().Sub(now)
	if delta > 5*time.Minute || delta < -5*time.Minute {
		return Error(codes.FailedPrecondition, "clock_skew", "clock_skew")
	}
	return nil
}
func Unavailable() error {
	return Error(codes.Unavailable, "unavailable", "durable write unavailable; retain batch")
}
func Validate(r *pb.ReportRequest) error {
	bad := func(s string) error { return fmt.Errorf("invalid request: %s", s) }
	if r == nil || r.SentAt == nil || r.SentAt.CheckValid() != nil {
		return bad("sent_at required and valid")
	}
	if !identity.MatchString(r.ProbeId) || !identity.MatchString(r.BatchId) || len(r.Results) < 1 || len(r.Results) > 10000 || proto.Size(r) > MaxMessageBytes {
		return bad("identity, batch size or message size")
	}
	for _, v := range r.Results {
		if v == nil || v.ProbeId != r.ProbeId || !identity.MatchString(v.TargetId) || v.StartedAt == nil || v.StartedAt.CheckValid() != nil || v.StartedAt.Seconds < 0 || v.StartedAt.Seconds > 4294967295-180*86400 {
			return bad("result identity or timestamp outside DateTime TTL range")
		}
		switch v.Kind {
		case "dns", "tcp", "tls", "http1", "http2", "http3", "trace":
		default:
			return bad("kind")
		}
		if len(v.Err) > 4096 || !utf8.ValidString(v.Err) || len(v.PhasesNanoseconds) > 16 || len(v.Hops) > 255 {
			return bad("result metadata bounds")
		}
		if v.Addr == "" {
			if v.Err == "" || (v.Family != "" && v.Family != "v4" && v.Family != "v6") {
				return bad("missing address without remote error")
			}
		} else {
			ip := net.ParseIP(v.Addr)
			if ip == nil || (v.Family != "v4" && v.Family != "v6") || (ip.To4() != nil) != (v.Family == "v4") {
				return bad("address/family")
			}
		}
		if v.Protocol != "" && v.Protocol != "h1" && v.Protocol != "h2" && v.Protocol != "h3" {
			return bad("HTTP protocol")
		}
		if v.TlsVersion > 65535 || (v.TlsResumed && v.TlsVersion == 0) {
			return bad("TLS metadata")
		}
		for k, n := range v.PhasesNanoseconds {
			if !identity.MatchString(k) || n < 0 {
				return bad("phase name/duration")
			}
		}
		for _, h := range v.Hops {
			if h == nil || h.Ttl == 0 || h.Ttl > 255 || h.RttNanoseconds < 0 || (h.Addr == "" && h.RttNanoseconds != 0) || (h.Addr != "" && net.ParseIP(h.Addr) == nil) {
				return bad("hop")
			}
		}
	}
	return nil
}
