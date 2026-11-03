package test

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/gruntwork-io/terratest/modules/terraform"
)

// These tests do not obtain credentials. Default runs compile and SKIP before
// calling any CLI. Only the controller may opt in after reviewed plan+ledger.
func TestNetworkAzure(t *testing.T) {
	if os.Getenv("PATHWATCH_RUN_AZURE_NETWORK_TESTS") != "1" {
		t.Skip("real Azure apply disabled: set explicit opt-in only after plan review and budget reservation; mocks are not cloud validation")
	}
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	run, fixture, manifest, err := reviewedFixture(root)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("network integration local state retained at %s", run)
	plan := filepath.Join(fixture, "reviewed.tfplan")
	if err := verifyDigest(plan, os.Getenv("PATHWATCH_AZURE_TEST_REVIEWED_PLAN_SHA256")); err != nil {
		t.Fatal(err)
	}
	if os.Getenv("AZURE_SUBSCRIPTION_ID") != manifest.Subscription {
		t.Fatal("explicit subscription must equal reviewed fixture")
	}
	// Recheck the actual binary plan and canonical receipt; no raw apply bypass.
	if _, err := command(root, "python3", filepath.Join(root, "scripts/azure_budget.py"), "check", "--plan", plan, "--proposal", filepath.Join(fixture, "proposal.json")); err != nil {
		t.Fatal(err)
	}
	if _, statErr := os.Stat(filepath.Join(fixture, "terraform.tfstate")); statErr == nil {
		before, err := command(fixture, "terraform", "state", "list")
		if err != nil {
			t.Fatal(err)
		}
		for _, address := range strings.Fields(string(before)) {
			if !strings.Contains(address, ".data.") {
				t.Fatalf("fixture must start without managed resources: %s", address)
			}
		}
	} else if !errors.Is(statErr, os.ErrNotExist) {
		t.Fatal(statErr)
	}
	rg, err := arm(root, manifest.Subscription, "group", "show", "--name", "rg-pathwatch-lab")
	if err != nil {
		t.Fatal(err)
	}
	expectedRG := "/subscriptions/" + manifest.Subscription + "/resourceGroups/rg-pathwatch-lab"
	if !strings.EqualFold(stringValue(rg, "id"), expectedRG) {
		t.Fatal("existing RG subscription/scope mismatch")
	}
	ownedTags, _ := rg["tags"].(map[string]any)
	if ownedTags["project"] != "pathwatch" || ownedTags["owner"] != "codex-pathwatch" {
		t.Fatal("existing RG ownership mismatch")
	}
	options := &terraform.Options{TerraformDir: fixture, NoColor: true, PlanFilePath: plan, Lock: true, LockTimeout: "60s"}
	err = exerciseNetwork(
		func() error { _, err := terraform.ApplyE(t, options); return err },
		func() error { return assertARMNetwork(t, root, fixture, manifest) },
		func() error { return cleanupNetwork(root, fixture, manifest, expectedRG) },
	)
	if err != nil {
		t.Fatal(err)
	}
}

type fixtureManifest struct {
	Name         string            `json:"name"`
	Subscription string            `json:"subscription_id"`
	Location     string            `json:"location"`
	CIDR         string            `json:"address_space"`
	TTL          string            `json:"ttl"`
	Peers        []string          `json:"wireguard_peer_cidrs"`
	Files        map[string]string `json:"files"`
}

func reviewedFixture(root string) (string, string, fixtureManifest, error) {
	var manifest fixtureManifest
	run, err := filepath.EvalSymlinks(os.Getenv("PATHWATCH_AZURE_TEST_RUN_DIR"))
	if err != nil {
		return "", "", manifest, err
	}
	base := filepath.Join(root, ".cache", "azure-network-tests") + string(os.PathSeparator)
	if !strings.HasPrefix(run, base) {
		return "", "", manifest, errors.New("run must be a prepared project-local ignored fixture")
	}
	data, err := os.ReadFile(filepath.Join(run, "manifest.json"))
	if err == nil {
		err = json.Unmarshal(data, &manifest)
	}
	if err != nil {
		return "", "", manifest, err
	}
	if !regexp.MustCompile(`^pathwatch-it-[0-9a-f]{12}$`).MatchString(manifest.Name) || filepath.Base(run) != manifest.Name {
		return "", "", manifest, errors.New("test resource prefix mismatch")
	}
	expiry, err := time.Parse(time.RFC3339, manifest.TTL)
	if err != nil || !expiry.After(time.Now()) || time.Until(expiry) > 4*time.Hour {
		return "", "", manifest, errors.New("fixture TTL must be live and within four hours")
	}
	if len(manifest.Files) == 0 || len(manifest.Peers) == 0 {
		return "", "", manifest, errors.New("prepared source manifest and owned peer are required")
	}
	for relative, digest := range manifest.Files {
		path := filepath.Join(run, relative)
		canonical, err := filepath.EvalSymlinks(path)
		if err != nil || !strings.HasPrefix(canonical, run+string(os.PathSeparator)) {
			return "", "", manifest, errors.New("fixture source escaped run directory")
		}
		if err := verifyDigest(canonical, digest); err != nil {
			return "", "", manifest, err
		}
	}
	return run, filepath.Join(run, "infra", "test", "fixtures", "azure"), manifest, nil
}

func verifyDigest(path, expected string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	actual := sha256.Sum256(data)
	if len(expected) != 64 || hex.EncodeToString(actual[:]) != expected {
		return fmt.Errorf("reviewed SHA256 mismatch: %s", path)
	}
	return nil
}

// Cleanup runs after apply success, partial apply error, or assertion failure.
// Returning its error preserves both primary failure and any failed teardown.
func exerciseNetwork(apply, verify, cleanup func() error) (err error) {
	defer func() { err = errors.Join(err, cleanup()) }()
	if err = apply(); err != nil {
		return err
	}
	return verify()
}

func command(dir, name string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Dir = dir
	output, err := cmd.CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("%s %v: %w\n%s", name, args, err, output)
	}
	return output, nil
}
func arm(root, subscription string, args ...string) (map[string]any, error) {
	args = append(args, "--subscription", subscription, "--only-show-errors", "--output", "json")
	data, err := command(root, "az", args...)
	if err != nil {
		return nil, err
	}
	var result map[string]any
	err = json.Unmarshal(data, &result)
	return result, err
}
func stringValue(m map[string]any, key string) string { value, _ := m[key].(string); return value }
func stringsValue(value any) []string {
	result := []string{}
	for _, item := range asSlice(value) {
		if text, ok := item.(string); ok {
			result = append(result, text)
		}
	}
	sort.Strings(result)
	return result
}
func asSlice(value any) []any         { result, _ := value.([]any); return result }
func object(value any) map[string]any { result, _ := value.(map[string]any); return result }
func sameID(value any, expected string) bool {
	actual, _ := value.(string)
	return strings.EqualFold(actual, expected)
}

func assertARMNetwork(t *testing.T, root, fixture string, m fixtureManifest) error {
	outputs, err := terraform.OutputAllE(t, &terraform.Options{TerraformDir: fixture, NoColor: true})
	if err != nil {
		return err
	}
	vnetID, _ := outputs["vnet_id"].(string)
	nsgID, _ := outputs["nsg_id"].(string)
	rtID, _ := outputs["route_table_id"].(string)
	prefix := "/subscriptions/" + m.Subscription + "/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Network/"
	expectedIDs := map[string]string{"virtualNetworks/": "vnet-", "networkSecurityGroups/": "nsg-", "routeTables/": "rt-"}
	for kind, name := range expectedIDs {
		expected := prefix + kind + name + m.Name
		actual := map[string]string{"virtualNetworks/": vnetID, "networkSecurityGroups/": nsgID, "routeTables/": rtID}[kind]
		if !strings.EqualFold(actual, expected) {
			return fmt.Errorf("output escaped owned test prefix: %s", actual)
		}
	}
	vnet, err := arm(root, m.Subscription, "network", "vnet", "show", "--ids", vnetID)
	if err != nil {
		return err
	}
	nsg, err := arm(root, m.Subscription, "network", "nsg", "show", "--ids", nsgID)
	if err != nil {
		return err
	}
	rt, err := arm(root, m.Subscription, "network", "route-table", "show", "--ids", rtID)
	if err != nil {
		return err
	}
	for _, parent := range []map[string]any{vnet, nsg, rt} {
		tags := object(parent["tags"])
		if parent["location"] != m.Location || tags["project"] != "pathwatch" || tags["owner"] != "codex-pathwatch" || tags["ttl"] != m.TTL || tags["est_usd_per_hour"] != "0" {
			return errors.New("ARM parent region/ownership/TTL/free-estimate mismatch")
		}
	}
	if !reflect.DeepEqual(stringsValue(object(vnet["addressSpace"])["addressPrefixes"]), []string{m.CIDR}) {
		return errors.New("ARM VNet CIDR differs from assigned /16")
	}
	stem := strings.TrimSuffix(m.CIDR, "0.0/16")
	expectedSubnets := map[string]string{"snet-workloads": stem + "0.0/24", "snet-nva": stem + "1.0/27", "snet-aks_nodes": stem + "16.0/20"}
	if len(asSlice(vnet["subnets"])) != 3 {
		return errors.New("ARM must have exactly three purpose subnets")
	}
	for _, item := range asSlice(vnet["subnets"]) {
		subnet := object(item)
		name := stringValue(subnet, "name")
		expected, ok := expectedSubnets[name]
		if !ok {
			return fmt.Errorf("unexpected ARM subnet: %s", name)
		}
		prefixes := stringsValue(subnet["addressPrefixes"])
		if len(prefixes) == 0 {
			prefixes = []string{stringValue(subnet, "addressPrefix")}
		}
		if !reflect.DeepEqual(prefixes, []string{expected}) || subnet["defaultOutboundAccess"] != false || !sameID(object(subnet["networkSecurityGroup"])["id"], nsgID) {
			return fmt.Errorf("subnet CIDR/outbound/NSG mismatch: %s", name)
		}
		if name == "snet-nva" {
			if len(object(subnet["routeTable"])) != 0 {
				return errors.New("NVA subnet must not route back to itself")
			}
		} else if !sameID(object(subnet["routeTable"])["id"], rtID) {
			return fmt.Errorf("subnet missing route association: %s", name)
		}
	}
	if len(asSlice(rt["routes"])) != 0 {
		return errors.New("network-only fixture must have no UDR/default-route blackhole")
	}
	rules := asSlice(nsg["securityRules"])
	if len(rules) != 2+len(m.Peers) {
		return errors.New("unexpected custom NSG rules")
	}
	deny, metrics := false, false
	peers := map[string]bool{}
	for _, item := range rules {
		rule := object(item)
		switch stringValue(rule, "name") {
		case "deny-all-inbound":
			deny = rule["access"] == "Deny" && rule["direction"] == "Inbound" && rule["priority"] == float64(4096) && rule["protocol"] == "*" && rule["sourceAddressPrefix"] == "*" && rule["destinationAddressPrefix"] == "*" && rule["destinationPortRange"] == "*"
		case "allow-private-metrics":
			metrics = rule["access"] == "Allow" && rule["direction"] == "Inbound" && rule["sourceAddressPrefix"] == m.CIDR && rule["destinationAddressPrefix"] == stem+"0.0/24" && strings.EqualFold(stringValue(rule, "protocol"), "Tcp") && reflect.DeepEqual(stringsValue(rule["destinationPortRanges"]), []string{"9090"})
		default:
			if !strings.HasPrefix(stringValue(rule, "name"), "allow-wireguard-") || rule["access"] != "Allow" || rule["direction"] != "Inbound" || !strings.EqualFold(stringValue(rule, "protocol"), "Udp") || rule["destinationPortRange"] != "51820" || rule["destinationAddressPrefix"] != stem+"1.0/27" {
				return errors.New("unexpected broad ingress rule")
			}
			peers[stringValue(rule, "sourceAddressPrefix")] = true
		}
	}
	if !deny || !metrics {
		return errors.New("explicit deny/scoped metrics allow absent from ARM")
	}
	for _, peer := range m.Peers {
		if !peers[peer] {
			return errors.New("owned WireGuard /32 missing")
		}
	}
	return nil
}

func cleanupNetwork(root, fixture string, m fixtureManifest, expectedRG string) error {
	// No destroy command can delete the shared RG/backend: copied local state has
	// network-only module addresses, and independent delete-only guard rechecks it.
	destroy := filepath.Join(fixture, "destroy.tfplan")
	if _, err := command(fixture, "terraform", "plan", "-destroy", "-input=false", "-no-color", "-var-file=terraform.tfvars.json", "-out="+destroy); err != nil {
		return fmt.Errorf("cleanup plan (state retained): %w", err)
	}
	data, err := os.ReadFile(destroy)
	if err != nil {
		return err
	}
	digest := sha256.Sum256(data)
	if _, err := command(root, "python3", filepath.Join(root, "scripts/azure_teardown.py"), "--plan", destroy, "--reviewed-sha256", hex.EncodeToString(digest[:])); err != nil {
		return fmt.Errorf("cleanup guard (state retained): %w", err)
	}
	if _, err := command(fixture, "terraform", "apply", "-input=false", "-no-color", destroy); err != nil {
		return fmt.Errorf("cleanup apply (state retained): %w", err)
	}
	rg, err := arm(root, m.Subscription, "group", "show", "--name", "rg-pathwatch-lab")
	if err != nil {
		return err
	}
	if !strings.EqualFold(stringValue(rg, "id"), expectedRG) {
		return errors.New("shared RG did not survive network teardown")
	}
	state, err := command(fixture, "terraform", "state", "list")
	if err != nil {
		return err
	}
	for _, address := range strings.Fields(string(state)) {
		if !strings.Contains(address, ".data.") {
			return fmt.Errorf("managed resource remains after cleanup: %s", address)
		}
	}
	// ARM readback must confirm all three taggable parents are absent; a nonzero
	// CLI error other than ResourceNotFound is not accepted as proof of deletion.
	for kind, name := range map[string]string{"virtualNetworks/": "vnet-", "networkSecurityGroups/": "nsg-", "routeTables/": "rt-"} {
		id := expectedRG + "/providers/Microsoft.Network/" + kind + name + m.Name
		_, err := arm(root, m.Subscription, "resource", "show", "--ids", id)
		if err == nil || !strings.Contains(err.Error(), "ResourceNotFound") {
			return fmt.Errorf("could not prove deleted ARM parent %s: %v", id, err)
		}
	}
	return nil
}

func TestNetworkCleanupOnEveryOutcome(t *testing.T) {
	for _, test := range []struct {
		name                            string
		applyErr, verifyErr, cleanupErr bool
	}{
		{name: "success"}, {name: "partial apply", applyErr: true}, {name: "assertion failure", verifyErr: true}, {name: "cleanup failure", cleanupErr: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			calls := []string{}
			failure := errors.New("injected failure")
			stage := func(name string, fail bool) func() error {
				return func() error {
					calls = append(calls, name)
					if fail {
						return failure
					}
					return nil
				}
			}
			err := exerciseNetwork(stage("apply", test.applyErr), stage("verify", test.verifyErr), stage("cleanup", test.cleanupErr))
			expected := []string{"apply", "verify", "cleanup"}
			if test.applyErr {
				expected = []string{"apply", "cleanup"}
			}
			if !reflect.DeepEqual(calls, expected) {
				t.Fatalf("cleanup/order mismatch: %v", calls)
			}
			if (err != nil) != (test.applyErr || test.verifyErr || test.cleanupErr) {
				t.Fatalf("lost failure: %v", err)
			}
		})
	}
}
