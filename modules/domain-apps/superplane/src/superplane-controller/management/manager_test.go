package management

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

const orgID = "92d4ae6b-c212-4d1f-a50a-7d5a0c7d8870"
const workspaceID = "18563dce-15e9-4c58-8824-ff78744085e4"
const credential = "test-controller-registry-credential-1234567890"

func testManager(t *testing.T, server *httptest.Server) *Manager {
	t.Helper()
	file := filepath.Join(t.TempDir(), "credential")
	if err := os.WriteFile(file, []byte(credential), 0600); err != nil {
		t.Fatal(err)
	}
	m, err := New(Config{APIURL: server.URL, OrgID: orgID, CredentialFile: file})
	if err != nil {
		t.Fatal(err)
	}
	m.client = server.Client()
	m.client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	return m
}

func TestZeroTargetsRegistrationRevocationAndTransport(t *testing.T) {
	targets := []Target{}
	authorized := true
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/internal/controller/reconcile" || r.Method != http.MethodPost {
			t.Error("unexpected registry request")
		}
		if !authorized || r.Header.Get("Authorization") != credential {
			w.WriteHeader(401)
			return
		}
		var request map[string]string
		if json.NewDecoder(r.Body).Decode(&request) != nil || request["org_id"] != orgID || !uuidPattern.MatchString(request["instance_id"]) {
			t.Error("invalid ownership request")
		}
		_ = json.NewEncoder(w).Encode(registry{Version: 1, OrgID: orgID, LeaseExpiresAt: time.Now().Add(45 * time.Second), FenceToken: 1, Targets: targets})
	}))
	defer server.Close()
	m := testManager(t, server)
	probes := 0
	m.inspect = func(context.Context, Target) string { probes++; return "credential_unavailable" }
	if err := m.Reconcile(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !m.Snapshot().RegistryReady || probes != 0 || m.Snapshot().GovernedProvisioning {
		t.Fatal("zero-workspace management confused with execution")
	}
	targets = []Target{{WorkspaceID: workspaceID}}
	if err := m.Reconcile(context.Background()); err != nil {
		t.Fatal(err)
	}
	if probes != 1 || m.Snapshot().Targets[workspaceID] != "credential_unavailable" {
		t.Fatal("new durable registration not observed")
	}
	authorized = false
	if err := m.Reconcile(context.Background()); err == nil {
		t.Fatal("revoked authority accepted")
	}
	if m.Snapshot().RegistryReady || len(m.Snapshot().Targets) != 0 || probes != 1 {
		t.Fatal("revoked registry retained readiness or target activity")
	}
	response := httptest.NewRecorder()
	m.Handler().ServeHTTP(response, httptest.NewRequest("GET", "/readyz", nil))
	if response.Code != 503 {
		t.Fatal("failure reported ready")
	}
	response = httptest.NewRecorder()
	m.Handler().ServeHTTP(response, httptest.NewRequest("GET", "/statusz", nil))
	if response.Code != 401 {
		t.Fatal("anonymous registry status disclosed")
	}
}

func TestRegistryRefusesForeignOrgDuplicateTargetsExpiredLeaseAndRedirect(t *testing.T) {
	for _, change := range []string{"organization", "duplicate", "lease", "redirect", "execution"} {
		t.Run(change, func(t *testing.T) {
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				result := registry{Version: 1, OrgID: orgID, LeaseExpiresAt: time.Now().Add(45 * time.Second), FenceToken: 1, Targets: []Target{}}
				switch change {
				case "organization":
					result.OrgID = workspaceID
				case "duplicate":
					result.Targets = []Target{{WorkspaceID: workspaceID}, {WorkspaceID: workspaceID}}
				case "lease":
					result.LeaseExpiresAt = time.Now().Add(-time.Second)
				case "execution":
					result.GovernedProvisioning = true
				case "redirect":
					w.Header().Set("Location", "https://foreign.example")
					w.WriteHeader(307)
					return
				}
				_ = json.NewEncoder(w).Encode(result)
			}))
			defer server.Close()
			m := testManager(t, server)
			m.inspect = func(context.Context, Target) string { t.Fatal("invalid registry activated target"); return "" }
			if m.Reconcile(context.Background()) == nil || m.Snapshot().RegistryReady {
				t.Fatal("invalid registry accepted")
			}
		})
	}
}

func TestMissingWorkspaceCredentialNeverUsesAmbientConfig(t *testing.T) {
	t.Setenv("KUBECONFIG", "/a/default/config/that/must/not/be/opened")
	m := &Manager{}
	target := Target{WorkspaceID: workspaceID, ClusterID: orgID, Namespace: "tenant-a", WorkspaceStatus: "Ready", ClusterStatus: "Ready", ClusterARN: "arn:aws:eks:us-east-1:123456789012:cluster/workspace", Endpoint: "https://workspace.example"}
	if got := m.inspectTarget(context.Background(), target); got != "credential_unavailable" {
		t.Fatal(got)
	}
	m.config.WorkspaceCredentialsDir = t.TempDir()
	if got := m.inspectTarget(context.Background(), target); got != "credential_unavailable" {
		t.Fatal(got)
	}
}
