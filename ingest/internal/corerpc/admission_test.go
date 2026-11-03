package corerpc

import (
	"context"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/stats"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/tap"
	"testing"
	"time"
)

func tapInfo(method string) *tap.Info {
	return &tap.Info{FullMethodName: CorePrefix + method, Header: metadata.Pairs("pathwatch-core-major", "1", "pathwatch-core-minor", "0")}
}
func await(t *testing.T, f func() bool) {
	t.Helper()
	end := time.Now().Add(time.Second)
	for !f() {
		if time.Now().After(end) {
			t.Fatal("condition timed out")
		}
		time.Sleep(time.Millisecond)
	}
}
func TestLeaseCancellationOwnership(t *testing.T) {
	a := NewAdmission(true, nil, nil, nil)
	defer a.Stop()
	ctx, cancel := context.WithCancel(context.Background())
	c, e := a.Tap(ctx, tapInfo("Report"))
	if e != nil {
		t.Fatal(e)
	}
	l := c.Value(leaseKey{}).(*lease)
	cancel()
	await(t, func() bool { return len(a.reports) == 0 })
	a.TagRPC(c, &stats.RPCTagInfo{})
	if e := l.reserve(1); e == nil {
		t.Fatal("canceled lease reactivated")
	}
	ctx, cancel = context.WithCancel(context.Background())
	c, e = a.Tap(ctx, tapInfo("Report"))
	if e != nil {
		t.Fatal(e)
	}
	a.TagRPC(c, &stats.RPCTagInfo{})
	l = c.Value(leaseKey{}).(*lease)
	if e = l.reserve(123); e != nil {
		t.Fatal(e)
	}
	cancel()
	time.Sleep(10 * time.Millisecond)
	if len(a.reports) != 1 || a.bytes != 123 {
		t.Fatal("active cancellation released live allocation")
	}
	a.HandleRPC(c, &stats.End{})
	a.HandleRPC(c, &stats.End{})
	if len(a.reports) != 0 || a.bytes != 0 {
		t.Fatal("lease not released exactly once")
	}
	a.Wait()
}
func TestGlobalAdmissionAndUnknownMethod(t *testing.T) {
	a := NewAdmission(true, nil, nil, nil)
	defer a.Stop()
	var calls []context.Context
	for i := 0; i < 32; i++ {
		c, e := a.Tap(context.Background(), tapInfo("Report"))
		if e != nil {
			t.Fatal(e)
		}
		a.TagRPC(c, &stats.RPCTagInfo{})
		calls = append(calls, c)
	}
	if _, e := a.Tap(context.Background(), tapInfo("Report")); status.Code(e) != codes.ResourceExhausted {
		t.Fatal(e)
	}
	for i := 0; i < 4; i++ {
		c, e := a.Tap(context.Background(), tapInfo("ReadRaw"))
		if e != nil {
			t.Fatal(e)
		}
		a.TagRPC(c, &stats.RPCTagInfo{})
		calls = append(calls, c)
	}
	if _, e := a.Tap(context.Background(), tapInfo("Describe")); status.Code(e) != codes.ResourceExhausted {
		t.Fatal(e)
	}
	if _, e := a.Tap(context.Background(), tapInfo("Unknown")); status.Code(e) != codes.Unimplemented {
		t.Fatal(e)
	}
	for _, c := range calls {
		a.HandleRPC(c, &stats.End{})
	}
	a.Wait()
}
func TestDecodeBudgetAndByteBounds(t *testing.T) {
	a := NewAdmission(true, nil, nil, nil)
	defer a.Stop()
	c, e := a.Tap(context.Background(), tapInfo("Report"))
	if e != nil {
		t.Fatal(e)
	}
	a.TagRPC(c, &stats.RPCTagInfo{})
	l := c.Value(leaseKey{}).(*lease)
	if e = l.reserve(a.limit); e != nil {
		t.Fatal(e)
	}
	if e = l.reserve(1); status.Code(e) != codes.ResourceExhausted {
		t.Fatal(e)
	}
	a.HandleRPC(c, &stats.End{})
	if a.bytes != 0 {
		t.Fatal(a.bytes)
	}
}
