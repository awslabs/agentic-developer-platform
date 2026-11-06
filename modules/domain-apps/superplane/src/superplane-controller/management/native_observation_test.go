package management

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"k8s.io/client-go/tools/clientcmd"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
)

func TestNativeManagerPublishesAuthenticatedObservationWithoutExecution(t *testing.T) {
	for _, test := range []struct {
		name     string
		provider []ProviderObservation
		want     string
	}{
		{"missing", nil, "degraded"},
		{"healthy", []ProviderObservation{{SkyPilotHealthy: true, CheckedAt: time.Now()}}, "healthy"},
		{"unhealthy", []ProviderObservation{{SkyPilotHealthy: false, CheckedAt: time.Now()}}, "degraded"},
		{"expired", []ProviderObservation{{SkyPilotHealthy: true, CheckedAt: time.Now().Add(-time.Minute)}}, "degraded"},
	} {
		t.Run(test.name, func(t *testing.T) { nativeManagerObservation(t, test.provider, test.want) })
	}
}

func nativeManagerObservation(t *testing.T, provider []ProviderObservation, want string) {
	t.Helper()
	const credential = "test-observation-credential-at-least-32-bytes"
	const signingKey = "test-observation-signing-key-at-least-32-bytes"
	observations := 0
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/internal/observations" {
			t.Errorf("unexpected control-plane request: %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusForbidden)
			return
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		mac := hmac.New(sha256.New, []byte(signingKey))
		mac.Write(body)
		if r.Header.Get("Authorization") != credential || r.Header.Get("x-superplane-signature") != fmt.Sprintf("sha256=%x", mac.Sum(nil)) {
			t.Error("observation lost its separate authentication")
			w.WriteHeader(http.StatusForbidden)
			return
		}
		var report struct {
			Kind    string            `json:"kind"`
			Status  string            `json:"status"`
			Subject map[string]string `json:"subject"`
		}
		if err := json.Unmarshal(body, &report); err != nil || report.Kind != "fleet_health" || report.Subject["workspace"] != workspaceID || report.Subject["cluster_id"] != orgID {
			t.Error("observation names a different registered workspace")
		}
		if report.Status != want {
			t.Errorf("provider health produced %q, want %q", report.Status, want)
		}
		observations++
		w.WriteHeader(http.StatusAccepted)
	}))
	defer api.Close()

	kube := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer test-workspace-reader" || (r.Method != http.MethodGet && r.URL.Path != "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews") {
			t.Error("workspace mutation or different credential attempted")
			w.WriteHeader(http.StatusForbidden)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		responses := map[string]string{
			"/api":                        `{"kind":"APIVersions","versions":["v1"]}`,
			"/apis":                       `{"kind":"APIGroupList","groups":[]}`,
			"/api/v1":                     `{"kind":"APIResourceList","groupVersion":"v1","resources":[{"name":"nodes","kind":"Node","namespaced":false,"verbs":["get","list","watch"]}]}`,
			"/api/v1/nodes":               `{"apiVersion":"v1","kind":"NodeList","items":[{"metadata":{"name":"ready-node"},"spec":{"providerID":"aws:///test/node"},"status":{"conditions":[{"type":"Ready","status":"True"}]}}]}`,
			"/api/v1/namespaces/tenant-a": `{"apiVersion":"v1","kind":"Namespace","metadata":{"name":"tenant-a"},"status":{"phase":"Active"}}`,
			"/apis/authorization.k8s.io/v1/selfsubjectrulesreviews":      `{"apiVersion":"authorization.k8s.io/v1","kind":"SelfSubjectRulesReview","status":{"resourceRules":[{"verbs":["get","list","watch"],"apiGroups":[""],"resources":["nodes","namespaces","pods"]}],"incomplete":false}}`,
			"/apis/superplane.ai/v1/nodepools":                           `{"apiVersion":"superplane.ai/v1","kind":"NodePoolList","items":[]}`,
			"/apis/superplane.ai/v1/namespaces/tenant-a/superplanenodes": `{"apiVersion":"superplane.ai/v1","kind":"SuperplaneNodeList","items":[]}`,
		}
		if r.URL.Path == "/api/v1/nodes" && r.URL.Query().Get("labelSelector") != "superplane.ai/workspace="+workspaceID {
			t.Error("node observation escaped its workspace")
		}
		body, ok := responses[r.URL.Path]
		if !ok {
			t.Errorf("unexpected workspace request: %s", r.URL.Path)
			w.WriteHeader(http.StatusForbidden)
			return
		}
		_, _ = io.WriteString(w, body)
	}))
	defer kube.Close()

	dir := t.TempDir()
	target := Target{WorkspaceID: workspaceID, ClusterID: orgID, Namespace: "tenant-a", WorkspaceStatus: "Ready", ClusterStatus: "Ready", ClusterARN: "arn:aws:eks:us-east-1:123456789012:cluster/workspace", Endpoint: kube.URL}
	target.ProviderObservations = provider
	config := clientcmdapi.Config{
		CurrentContext: target.ClusterARN,
		Clusters:       map[string]*clientcmdapi.Cluster{"workspace": {Server: kube.URL, CertificateAuthorityData: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: kube.Certificate().Raw})}},
		Contexts:       map[string]*clientcmdapi.Context{target.ClusterARN: {Cluster: "workspace", AuthInfo: "reader", Namespace: target.Namespace}},
		AuthInfos:      map[string]*clientcmdapi.AuthInfo{"reader": {Token: "test-workspace-reader"}},
	}
	data, err := clientcmd.Write(config)
	if err != nil {
		t.Fatal(err)
	}
	for name, content := range map[string][]byte{".kubeconfig": data, ".credential": []byte(credential), ".signing-key": []byte(signingKey)} {
		if err := os.WriteFile(filepath.Join(dir, workspaceID+name), content, 0600); err != nil {
			t.Fatal(err)
		}
	}
	m := &Manager{client: api.Client(), config: Config{APIURL: api.URL, WorkspaceCredentialsDir: dir, ObservationCredentialsDir: dir, ManagementAPIServer: "https://management.example"}}
	if got := m.inspectTarget(context.Background(), target); got != "observed_execution_unavailable" || observations != 1 {
		t.Fatalf("native observation was not published: status=%s observations=%d", got, observations)
	}
	if m.config.EnableExecution || m.config.ExecutionSocket != "" {
		t.Fatal("observation acquired execution authority")
	}
	if err := os.Remove(filepath.Join(dir, workspaceID+".credential")); err != nil {
		t.Fatal(err)
	}
	if got := m.inspectTarget(context.Background(), target); got != "observation_credential_unavailable" || observations != 1 {
		t.Fatal("removed observation credential was reused:", got)
	}
	target.Provisional, target.ClusterID = true, ""
	target.WorkspaceStatus, target.ClusterStatus = "Provisioning", "Provisioning"
	target.BootstrapOperationID, target.RegistrationClaim = "bootstrap-operation", "registration-claim"
	if got := m.inspectTarget(context.Background(), target); got != "observed_execution_unavailable" || observations != 1 {
		t.Fatal("provisional discovery published a readiness observation:", got)
	}
}
