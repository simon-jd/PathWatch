package corerpc

import (
	"context"
	"crypto/x509"
	statuspb "google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/stats"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/anypb"
	"pathwatch.local/pathwatch/ingest/internal/store"
	cp "pathwatch.local/pathwatch/proto/pathwatch/core/v1"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"testing"
	"time"
)

type descriptorClient struct {
	cp.CoreClient
	value *cp.DescribeResponse
}

func (c descriptorClient) Describe(context.Context, *cp.DescribeRequest, ...grpc.CallOption) (*cp.DescribeResponse, error) {
	return c.value, nil
}
func TestDescribeExactLimitsAndAdditiveMinor(t *testing.T) {
	good := &cp.DescribeResponse{Major: 1, Minor: 1, Capabilities: 31, MaxReportBytes: 16 << 20, MaxQueryBytes: MaxQuery, MaxReplyBytes: MaxReply, Ready: true}
	if e := (&Client{rpc: descriptorClient{value: good}}).Ready(context.Background()); e != nil {
		t.Fatal(e)
	}
	for _, change := range []func(*cp.DescribeResponse){func(v *cp.DescribeResponse) { v.Major = 2 }, func(v *cp.DescribeResponse) { v.Capabilities = 7 }, func(v *cp.DescribeResponse) { v.MaxReportBytes-- }, func(v *cp.DescribeResponse) { v.MaxQueryBytes++ }, func(v *cp.DescribeResponse) { v.MaxReplyBytes-- }, func(v *cp.DescribeResponse) { v.Ready = false }} {
		bad := proto.Clone(good).(*cp.DescribeResponse)
		change(bad)
		if e := (&Client{rpc: descriptorClient{value: bad}}).Ready(context.Background()); e == nil {
			t.Fatal(bad)
		}
	}
}

type bytesCodec struct{ clientCodec }

func (bytesCodec) Marshal(v any) ([]byte, error) { return v.([]byte), nil }
func TestActualProtocolAndMethodReceiveBounds(t *testing.T) {
	target, p, a := startCore(t, &fakeStore{})
	tr, e := LoadTrust(p.cert(t, "spiffe://pathwatch.local/frontend/stable", false, time.Now().Add(time.Hour)), false)
	if e != nil {
		t.Fatal(e)
	}
	cc, e := grpc.NewClient("passthrough:///"+target, grpc.WithTransportCredentials(tr.Credentials()), grpc.WithNoProxy(), grpc.WithDisableRetry())
	if e != nil {
		t.Fatal(e)
	}
	defer cc.Close()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	bad := metadata.NewOutgoingContext(ctx, metadata.Pairs("pathwatch-core-major", "2", "pathwatch-core-minor", "0"))
	if e = cc.Invoke(bad, CorePrefix+"Describe", &cp.DescribeRequest{}, &cp.DescribeResponse{}); status.Code(e) != codes.FailedPrecondition {
		t.Fatal(e)
	}
	ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs("pathwatch-core-major", "1", "pathwatch-core-minor", "0"))
	for _, test := range []struct {
		method string
		size   int
	}{{"ReadRaw", 4097}, {"Report", (16 << 20) + 1}} {
		wire := make([]byte, test.size)
		e = cc.Invoke(ctx, CorePrefix+test.method, wire, &pb.ReportResponse{}, grpc.ForceCodec(bytesCodec{}), grpc.MaxCallSendMsgSize(len(wire)))
		if status.Code(e) != codes.ResourceExhausted {
			t.Fatalf("%s: %v", test.method, e)
		}
	}
	await(t, func() bool { return len(a.reports) == 0 && len(a.reads) == 0 })
}
func sizedUnknown(n int) []byte {
	// field100 tag2 + length varint; choose payload to make exact inclusive size.
	for k := n - 8; k < n; k++ {
		b := protowire.AppendBytes(protowire.AppendTag(nil, 100, protowire.BytesType), make([]byte, k))
		if len(b) == n {
			return b
		}
	}
	panic("cannot size field")
}
func TestInclusiveCodecBounds(t *testing.T) {
	for _, n := range []int{MaxQuery, MaxQuery + 1} {
		a := NewAdmission(true, nil, nil, nil)
		ctx, e := a.Tap(context.Background(), tapInfo("Describe"))
		if e != nil {
			t.Fatal(e)
		}
		a.TagRPC(ctx, &stats.RPCTagInfo{})
		l := ctx.Value(leaseKey{}).(*lease)
		e = (codec{}).Unmarshal(sizedUnknown(n), &decodeEnvelope{&cp.DescribeRequest{}, l})
		if (e == nil) != (n == MaxQuery) {
			t.Fatal(n, e)
		}
		a.HandleRPC(ctx, &stats.End{})
		a.Stop()
		a.Wait()
	}
	for _, n := range []int{MaxReply, MaxReply + 1} {
		v := &cp.RawResponse{}
		v.ProtoReflect().SetUnknown(sizedUnknown(n))
		b, e := (codec{}).Marshal(v)
		if (e == nil) != (n == MaxReply) {
			t.Fatal(n, e)
		}
		if e == nil && len(b) != MaxReply {
			t.Fatal(len(b))
		}
	}
}
func TestMalformedDetailsNeverFallback(t *testing.T) {
	for _, literal := range []string{"overloaded", "ingest queue full"} {
		for _, detail := range []*anypb.Any{{TypeUrl: "type.googleapis.com/google.rpc.ErrorInfo", Value: []byte{255}}, {TypeUrl: "wrong", Value: []byte{}}} {
			e := status.FromProto(&statuspb.Status{Code: int32(codes.ResourceExhausted), Message: literal, Details: []*anypb.Any{detail}}).Err()
			if earlyOverload(e, literal) || readError(e) != store.ErrQueryResponse {
				t.Fatal(e)
			}
		}
	}
}
func TestExactSANRejectsWildcard(t *testing.T) {
	if exactSAN(&x509.Certificate{DNSNames: []string{"*.test"}}, "core.test") == nil {
		t.Fatal("wildcard matched")
	}
}

func TestEstablishedChainExpiryAndDeadline(t *testing.T) {
	now := time.Now()
	leaf := &x509.Certificate{NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour)}
	intermediate := &x509.Certificate{NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Minute)}
	until, ok := chainUntil([][]*x509.Certificate{{leaf, intermediate}})
	if !ok || !until.Equal(intermediate.NotAfter) {
		t.Fatal(until, ok)
	}
	intermediate.NotAfter = now.Add(-time.Second)
	if _, ok = chainUntil([][]*x509.Certificate{{leaf, intermediate}}); ok {
		t.Fatal("expired intermediate accepted on existing connection")
	}
}

func TestReplyDuplicateKeyFieldUsesFinalValue(t *testing.T) {
	field := func(n protowire.Number, b []byte) []byte {
		return protowire.AppendBytes(protowire.AppendTag(nil, n, protowire.BytesType), b)
	}
	entry := append(field(1, make([]byte, 1024)), field(1, []byte("dns"))...)
	entry = append(entry, 16, 1)
	wire := field(1, field(11, entry))
	var reply cp.RawResponse
	if e := (clientCodec{}).Unmarshal(wire, &reply); e != nil || len(reply.Rows) != 1 || reply.Rows[0].Phases["dns"] != 1 {
		t.Fatal(e, &reply)
	}
}
