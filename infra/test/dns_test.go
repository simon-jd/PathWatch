package test

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestDNSCrossResolution(t *testing.T) {
	if os.Getenv("PATHWATCH_DNS_ACTUAL") != "1" {
		t.Skip("opt-in actual owned DNS lab required; mock/skip is not acceptance")
	}
	inventory := os.Getenv("PATHWATCH_DNS_INVENTORY")
	output := os.Getenv("PATHWATCH_DNS_EVIDENCE")
	if inventory == "" || output == "" {
		t.Fatal("private inventory and fresh evidence parent required")
	}
	cmd := exec.Command("python3", filepath.Join("..", "..", "scripts", "test_dns.py"), "--inventory", inventory, "--out", output, "--confirm-owned-endpoints")
	if data, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("actual cross-resolution failed: %v\n%s", err, data)
	}
}
