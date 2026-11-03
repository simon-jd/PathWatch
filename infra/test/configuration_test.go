package test

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// State isolation is independent of mocked provider behavior: backend-free
// integration copies must never address the deployed regional blob state.
func TestNetworkStateIsolation(t *testing.T) {
	keys := map[string]bool{}
	for _, region := range []string{"azure-a", "azure-b"} {
		root := filepath.Join("..", "envs", "dev", region)
		backend, err := os.ReadFile(filepath.Join(root, "backend.tf"))
		if err != nil {
			t.Fatal(err)
		}
		if !regexp.MustCompile(`backend\s+"azurerm"\s*\{`).Match(backend) {
			t.Fatalf("%s lacks actual Azure backend", region)
		}
		example, err := os.ReadFile(filepath.Join(root, "backend.tfbackend.example"))
		if err != nil {
			t.Fatal(err)
		}
		match := regexp.MustCompile(`(?m)^key\s*=\s*"([^"]+)"`).FindSubmatch(example)
		if len(match) != 2 || string(match[1]) != region+".tfstate" || keys[string(match[1])] {
			t.Fatalf("regional blob key collides: %s", example)
		}
		keys[string(match[1])] = true
		if !regexp.MustCompile(`(?m)^use_azuread_auth\s*=\s*true`).Match(example) {
			t.Fatal("backend must use Entra auth")
		}
		for _, forbidden := range []string{"access_key", "sas_token", "connection_string"} {
			if strings.Contains(string(example), forbidden) {
				t.Fatalf("secret-based backend field %s forbidden", forbidden)
			}
		}
	}
	entries, err := os.ReadDir("fixtures/azure")
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if !strings.HasSuffix(entry.Name(), ".tf") {
			continue
		}
		data, err := os.ReadFile(filepath.Join("fixtures/azure", entry.Name()))
		if err != nil {
			t.Fatal(err)
		}
		if regexp.MustCompile(`backend\s+"`).Match(data) {
			t.Fatal("integration fixture must use isolated implicit local state")
		}
	}
}
