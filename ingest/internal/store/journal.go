package store

import (
	"encoding/json"
	"fmt"
	"golang.org/x/sys/unix"
	"io"
	"os"
	"path/filepath"
)

func regular(path string, flags int) (*os.File, error) {
	fd, e := unix.Open(path, flags|unix.O_NOFOLLOW|unix.O_CLOEXEC|unix.O_NONBLOCK, 0600)
	if e != nil {
		return nil, e
	}
	f := os.NewFile(uintptr(fd), path)
	st, e := f.Stat()
	if e != nil || !st.Mode().IsRegular() {
		f.Close()
		return nil, fmt.Errorf("journal path must be regular: %s", path)
	}
	return f, nil
}
func readFile(path string, max int64) ([]byte, error) {
	f, e := regular(path, unix.O_RDONLY)
	if e != nil {
		return nil, e
	}
	defer f.Close()
	b, e := io.ReadAll(io.LimitReader(f, max+1))
	if int64(len(b)) > max {
		return nil, fmt.Errorf("journal file exceeds limit")
	}
	return b, e
}
func atomicJSON(dir, name string, v any) error {
	b, e := json.Marshal(v)
	if e != nil {
		return e
	}
	return atomicWrite(dir, name, b)
}
func atomicWrite(dir, name string, b []byte) error {
	f, e := os.CreateTemp(dir, ".pw-tmp-")
	if e != nil {
		return e
	}
	defer func() { f.Close(); os.Remove(f.Name()) }()
	if _, e = f.Write(b); e != nil {
		return e
	}
	if e = f.Sync(); e != nil {
		return e
	}
	if e = f.Close(); e != nil {
		return e
	}
	if e = os.Rename(f.Name(), filepath.Join(dir, name)); e != nil {
		return e
	}
	d, e := os.Open(dir)
	if e != nil {
		return e
	}
	defer d.Close()
	return d.Sync()
}

func syncDirectory(dir string) error {
	d, e := os.Open(dir)
	if e != nil {
		return e
	}
	defer d.Close()
	return d.Sync()
}
