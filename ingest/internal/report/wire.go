package report

import (
	"fmt"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
)

// SemanticLimit is a well-formed Report shape which the original validator
// rejects. It preserves that application status before object allocation.
// For multiply-invalid input, this safety check may precede another validation
// error; it never changes which input is accepted.
type SemanticLimit struct{ category string }

func (e *SemanticLimit) Error() string { return "invalid request: " + e.category }
func (e *SemanticLimit) GRPCStatus() *status.Status {
	return status.Convert(Error(codes.InvalidArgument, "invalid_request", e.Error()))
}

// Charge inspects collection cardinality before protobuf can allocate objects.
// Duplicate singular fields/messages and map keys retain protobuf semantics.
// The accounting includes repeated slice growth, map buckets and overwritten bytes.
func Charge(b []byte) (int64, error) {
	if len(b) > MaxMessageBytes {
		return 0, fmt.Errorf("report exceeds wire limit")
	}
	charge := int64(1<<20) + 4*int64(len(b))
	results := 0
	err := Fields(b, func(n protowire.Number, t protowire.Type, v []byte) error {
		if n != 4 || t != protowire.BytesType {
			return nil
		}
		results++
		if results > 10000 {
			return &SemanticLimit{"identity, batch size or message size"}
		}
		charge += 1024
		hops := 0
		keys := map[string]bool{}
		return Fields(v, func(n protowire.Number, t protowire.Type, v []byte) error {
			if t != protowire.BytesType {
				return nil
			}
			switch n {
			case 10:
				hops++
				if hops > 255 {
					return &SemanticLimit{"result metadata bounds"}
				}
				charge += 160
			case 7:
				var keyBytes []byte
				if e := Fields(v, func(n protowire.Number, t protowire.Type, v []byte) error {
					if n == 1 && t == protowire.BytesType {
						keyBytes = v
					}
					return nil
				}); e != nil {
					return e
				}
				if len(keyBytes) > 128 {
					return &SemanticLimit{"phase name/duration"}
				}
				key := string(keyBytes)
				if !keys[key] {
					keys[key] = true
					charge += 512
				}
				if len(keys) > 16 {
					return &SemanticLimit{"result metadata bounds"}
				}
			}
			return nil
		})
	})
	return charge, err
}

// Fields visits length-delimited values without retaining or allocating payloads.
// ConsumeFieldValue checks all other wire encodings, including bounded groups.
func Fields(b []byte, visit func(protowire.Number, protowire.Type, []byte) error) error {
	for len(b) > 0 {
		n, t, k := protowire.ConsumeTag(b)
		if k < 0 {
			return protowire.ParseError(k)
		}
		b = b[k:]
		k = protowire.ConsumeFieldValue(n, t, b)
		if k < 0 {
			return protowire.ParseError(k)
		}
		var v []byte
		if t == protowire.BytesType {
			v, _ = protowire.ConsumeBytes(b)
		}
		if e := visit(n, t, v); e != nil {
			return e
		}
		b = b[k:]
	}
	return nil
}
