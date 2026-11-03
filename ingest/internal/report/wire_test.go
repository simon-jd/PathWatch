package report

import (
	"bytes"
	"encoding/json"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"os"
	pb "pathwatch.local/pathwatch/proto/pathwatch/v1"
	"runtime"
	"strings"
	"testing"
	"time"
)

func field(n protowire.Number, b []byte) []byte {
	return protowire.AppendBytes(protowire.AppendTag(nil, n, protowire.BytesType), b)
}
func TestWireEmptyObjectsAndDuplicateMaps(t *testing.T) {
	b := bytes.Repeat([]byte{34, 0}, 10001)
	if _, e := Charge(b); e == nil {
		t.Fatal("empty result amplification accepted")
	}
	b = field(4, bytes.Repeat([]byte{82, 0}, 256))
	if _, e := Charge(b); e == nil {
		t.Fatal("empty hop amplification accepted")
	}
	entry := append(field(1, []byte("dns")), []byte{16, 1}...)
	b = field(4, bytes.Repeat(field(7, entry), 100))
	if _, e := Charge(b); e != nil {
		t.Fatal("duplicate logical key rejected", e)
	}
	var r pb.ReportRequest
	if e := proto.Unmarshal(b, &r); e != nil || len(r.Results[0].PhasesNanoseconds) != 1 {
		t.Fatal(e)
	}
}
func TestLargeValidShapeAllocation(t *testing.T) {
	if testing.Short() {
		t.Skip("allocates a max-size report; skipped with -short")
	}
	now := time.Now()
	r := &pb.ReportRequest{ProbeId: "probe", BatchId: "shape", SentAt: timestamppb.New(now)}
	head, e := proto.Marshal(r)
	if e != nil {
		t.Fatal(e)
	}
	v := &pb.ProbeResult{ProbeId: "probe", TargetId: "target", Kind: "trace", StartedAt: timestamppb.New(now), Err: "no route"}
	for i := 0; i < 255; i++ {
		v.Hops = append(v.Hops, &pb.Hop{Ttl: 1})
	}
	body, e := proto.Marshal(v)
	if e != nil {
		t.Fatal(e)
	}
	wire := append(head, bytes.Repeat(field(4, body), 10000)...)
	charge, e := Charge(wire)
	if e != nil {
		t.Fatal(e)
	}
	if len(wire) > MaxMessageBytes || charge > 768<<20 {
		t.Fatal(len(wire), charge)
	}
	runtime.GC()
	var before, after runtime.MemStats
	runtime.ReadMemStats(&before)
	var decoded pb.ReportRequest
	if e := proto.Unmarshal(wire, &decoded); e != nil {
		t.Fatal(e)
	}
	if e = Validate(&decoded); e != nil {
		t.Fatal(e)
	}
	runtime.ReadMemStats(&after)
	allocated := after.TotalAlloc - before.TotalAlloc
	if int64(allocated) > charge {
		t.Fatalf("charge understated: allocated=%d charge=%d", allocated, charge)
	}
	b, _ := json.Marshal(map[string]any{"wire_bytes": len(wire), "results": len(decoded.Results), "hops": 2550000, "charge": charge, "allocated": allocated, "heap_inuse": after.HeapInuse})
	t.Log(string(b))
	if stat, e := os.ReadFile("/proc/self/status"); e == nil {
		for _, line := range strings.Split(string(stat), "\n") {
			if strings.HasPrefix(line, "VmHWM:") || strings.HasPrefix(line, "VmRSS:") {
				t.Log(line)
			}
		}
	}
	runtime.KeepAlive(&decoded)
}

func TestDuplicateKeyFieldUsesFinalValue(t *testing.T) {
	now := time.Now()
	v := &pb.ProbeResult{ProbeId: "probe", TargetId: "target", Kind: "dns", StartedAt: timestamppb.New(now), Err: "test"}
	body, e := proto.Marshal(v)
	if e != nil {
		t.Fatal(e)
	}
	entry := append(field(1, bytes.Repeat([]byte("x"), 1024)), field(1, []byte("dns"))...)
	entry = append(entry, 16, 1)
	body = append(body, field(7, entry)...)
	header, e := proto.Marshal(&pb.ReportRequest{ProbeId: "probe", BatchId: "batch", SentAt: timestamppb.New(now)})
	if e != nil {
		t.Fatal(e)
	}
	wire := append(header, field(4, body)...)
	if _, e = Charge(wire); e != nil {
		t.Fatal("overwritten singular key rejected", e)
	}
	var decoded pb.ReportRequest
	if e = proto.Unmarshal(wire, &decoded); e != nil {
		t.Fatal(e)
	}
	if e = Validate(&decoded); e != nil || len(decoded.Results[0].PhasesNanoseconds) != 1 || decoded.Results[0].PhasesNanoseconds["dns"] != 1 {
		t.Fatal(e, &decoded)
	}
}
