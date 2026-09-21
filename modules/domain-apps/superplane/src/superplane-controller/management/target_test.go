package management

import (
	"context"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"k8s.io/client-go/tools/clientcmd"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
)

func TestScopedWorkspaceReadAndCredentialRemoval(t *testing.T) {
	allowed := true
	calls := 0
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if !allowed || r.Header.Get("Authorization") != "Bearer workspace-only-token" {
			w.WriteHeader(403)
			return
		}
		if r.Method != "GET" {
			t.Fatal("workspace mutation attempted")
		}
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/api/v1/namespaces/tenant-a":
			_, _ = w.Write([]byte(`{"apiVersion":"v1","kind":"Namespace","metadata":{"name":"tenant-a"},"status":{"phase":"Active"}}`))
		case "/apis/superplane.ai/v1/namespaces/tenant-a/nodepools":
			_, _ = w.Write([]byte(`{"apiVersion":"superplane.ai/v1","kind":"NodePoolList","items":[]}`))
		case "/apis/superplane.ai/v1/namespaces/tenant-a/superplanenodes":
			_, _ = w.Write([]byte(`{"apiVersion":"superplane.ai/v1","kind":"SuperplaneNodeList","items":[]}`))
		default:
			t.Errorf("unscoped read: %s", r.URL.Path)
			w.WriteHeader(403)
		}
	}))
	defer server.Close()
	dir := t.TempDir()
	m := &Manager{config: Config{WorkspaceCredentialsDir: dir, ManagementAPIServer: "https://management.example"}}
	target := Target{WorkspaceID: workspaceID, ClusterID: orgID, Namespace: "tenant-a", WorkspaceStatus: "Ready", ClusterStatus: "Ready", ClusterARN: "arn:aws:eks:us-east-1:123456789012:cluster/workspace", Endpoint: server.URL}
	kubeconfig := clientcmdapi.Config{
		CurrentContext: target.ClusterARN,
		Clusters:       map[string]*clientcmdapi.Cluster{"workspace": {Server: server.URL, CertificateAuthorityData: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw})}},
		Contexts:       map[string]*clientcmdapi.Context{target.ClusterARN: {Cluster: "workspace", AuthInfo: "scoped", Namespace: target.Namespace}},
		AuthInfos:      map[string]*clientcmdapi.AuthInfo{"scoped": {Token: "workspace-only-token"}},
	}
	file := filepath.Join(dir, workspaceID+".kubeconfig")
	write := func() {
		t.Helper()
		data, err := clientcmd.Write(kubeconfig)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(file, data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	write()
	if got := m.inspectTarget(context.Background(), target); got != "observed_execution_unavailable" {
		t.Fatal(got)
	}
	if calls != 3 {
		t.Fatalf("got %d scoped reads", calls)
	}
	allowed = false
	if got := m.inspectTarget(context.Background(), target); got != "namespace_unavailable" {
		t.Fatal(got)
	}
	allowed = true
	if err := os.Remove(file); err != nil {
		t.Fatal(err)
	}
	if got := m.inspectTarget(context.Background(), target); got != "credential_unavailable" {
		t.Fatal(got)
	}
	kubeconfig.AuthInfos["scoped"].Exec = &clientcmdapi.ExecConfig{Command: "/must/never/execute"}
	write()
	if got := m.inspectTarget(context.Background(), target); got != "credential_refused" {
		t.Fatal(got)
	}
	kubeconfig.AuthInfos["scoped"].Exec = nil
	kubeconfig.Contexts[target.ClusterARN].Namespace = "tenant-b"
	write()
	if got := m.inspectTarget(context.Background(), target); got != "credential_refused" {
		t.Fatal(got)
	}
	kubeconfig.Contexts[target.ClusterARN].Namespace = "tenant-a"
	write()
	m.config.ManagementAPIServer = server.URL
	if got := m.inspectTarget(context.Background(), target); got != "credential_refused" {
		t.Fatal(got)
	}
	if calls != 4 {
		t.Fatalf("refused credentials reached workspace: %d calls", calls)
	}
}
