package pathwatchv1

import (
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"testing"
)

func TestProbeResult_AdditiveWireContract(t *testing.T) {
	descriptor := (&ProbeResult{}).ProtoReflect().Descriptor()
	// Existing spool/ingest field numbers must survive the Task6 extension.
	names := []string{"target_id", "probe_id", "kind", "family", "addr", "started_at", "phases_nanoseconds", "err", "protocol", "hops", "tls_version", "tls_resumed"}
	for i, name := range names {
		f := descriptor.Fields().ByName(protoreflect.Name(name))
		if f == nil || f.Number() != protoreflect.FieldNumber(i+1) {
			t.Fatalf("field %s renumbered: %v", name, f)
		}
	}
	original := &ProbeResult{TargetId: "t", ProbeId: "p", Kind: "http3", Family: "v6", Addr: "::1", Protocol: "h3", Hops: []*Hop{{Ttl: 1, Addr: "::1", RttNanoseconds: 1234}}, TlsVersion: 0x0304, TlsResumed: true, PhasesNanoseconds: map[string]int64{"ttfb": 1234}}
	wire, err := proto.Marshal(original)
	if err != nil {
		t.Fatal(err)
	}
	var decoded ProbeResult
	if err = proto.Unmarshal(wire, &decoded); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(original, &decoded) {
		t.Fatalf("roundtrip=%+v", &decoded)
	}
}
