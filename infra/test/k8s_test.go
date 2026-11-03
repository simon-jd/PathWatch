package test

import (
	"encoding/json"
	"os"
	"os/exec"
	"testing"
)

// TestClusters checks a live cluster: every node Ready and every Argo CD
// application Synced/Healthy. Opt-in: PATHWATCH_KIND_KUBECONFIG names the
// project-local kubeconfig of the kind lab (deploy/README.md). AKS is not covered.
func TestClusters(t *testing.T) {
	kubeconfig := os.Getenv("PATHWATCH_KIND_KUBECONFIG")
	if kubeconfig == "" {
		t.Skip("set PATHWATCH_KIND_KUBECONFIG to the kind lab kubeconfig")
	}
	get := func(args ...string) []byte {
		t.Helper()
		out, err := exec.Command("kubectl", append([]string{"--kubeconfig", kubeconfig, "get", "-o", "json"}, args...)...).Output()
		if err != nil {
			t.Fatalf("kubectl get %v: %v", args, err)
		}
		return out
	}
	var nodes struct {
		Items []struct {
			Metadata struct{ Name string }
			Status   struct {
				Conditions []struct{ Type, Status string }
			}
		}
	}
	if err := json.Unmarshal(get("nodes"), &nodes); err != nil || len(nodes.Items) == 0 {
		t.Fatalf("nodes: %v (%d)", err, len(nodes.Items))
	}
	for _, n := range nodes.Items {
		ready := false
		for _, c := range n.Status.Conditions {
			ready = ready || (c.Type == "Ready" && c.Status == "True")
		}
		if !ready {
			t.Errorf("node %s not Ready", n.Metadata.Name)
		}
	}
	var apps struct {
		Items []struct {
			Metadata struct{ Name string }
			Status   struct {
				Sync   struct{ Status string }
				Health struct{ Status string }
			}
		}
	}
	if err := json.Unmarshal(get("applications.argoproj.io", "-n", "argocd"), &apps); err != nil {
		t.Fatal(err)
	}
	if len(apps.Items) < 4 {
		t.Fatalf("want root + 3 child applications, got %d", len(apps.Items))
	}
	for _, a := range apps.Items {
		t.Logf("%s %s/%s", a.Metadata.Name, a.Status.Sync.Status, a.Status.Health.Status)
		if a.Status.Sync.Status != "Synced" || a.Status.Health.Status != "Healthy" {
			t.Errorf("application %s is %s/%s", a.Metadata.Name, a.Status.Sync.Status, a.Status.Health.Status)
		}
	}
}
