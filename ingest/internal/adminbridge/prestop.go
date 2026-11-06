package adminbridge

import (
	"context"
	"errors"
	"io"
	"net/http"
	"time"
)

// PreStop is the frontend container's preStop hook. Pod deletion runs it at once,
// while the frontend still serves: it asks the edge-proxy sidecar to drain (POST
// /drain on its loopback management listener), so HTTP/2 clients such as a
// long-lived Report connection reconnect to another pod, then waits so the pod
// keeps serving while kube-proxy drops its endpoint. The wait always runs in full;
// a failed drain request is returned after it.
func PreStop(ctx context.Context, drain string, wait time.Duration) error {
	result := canonicalEndpoint(drain, true)
	if result == nil {
		result = postDrain(ctx, drain)
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	select {
	case <-timer.C:
	case <-ctx.Done():
	}
	return result
}

func postDrain(ctx context.Context, address string) error {
	ctx, cancel := context.WithTimeout(ctx, requestLimit)
	defer cancel()
	tr := transport()
	defer tr.CloseIdleConnections()
	c := &http.Client{Transport: tr, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	r, e := http.NewRequestWithContext(ctx, "POST", "http://"+address+"/drain", nil)
	if e != nil {
		return e
	}
	resp, e := c.Do(r)
	if e != nil {
		return e
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	resp.Body.Close()
	if resp.StatusCode != 200 {
		return errors.New("edge proxy did not accept the drain request")
	}
	return nil
}
