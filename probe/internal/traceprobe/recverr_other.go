//go:build !linux

package traceprobe

import "errors"

func openRecvErr() (PacketConn, error) {
	return nil, errors.New("UDP trace mode (IP_RECVERR) is Linux-only")
}
