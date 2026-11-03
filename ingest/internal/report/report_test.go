package report

import (
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"testing"
	"time"
)

func TestValidationNil(t *testing.T) {
	if status.Code(Check(nil, time.Now())) != codes.InvalidArgument {
		t.Fatal("nil batch must be invalid")
	}
}
