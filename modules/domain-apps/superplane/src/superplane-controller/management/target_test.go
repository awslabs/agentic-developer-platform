package management

import (
	"context"
	"encoding/pem"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/execution"

	authorizationv1 "k8s.io/api/authorization/v1"
	"k8s.io/client-go/kubernetes/scheme"
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
		if r.Method != "GET" && r.URL.Path != "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews" {
			t.Fatal("workspace mutation attempted")
		}
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews":
			var review authorizationv1.SelfSubjectRulesReview
			data, err := io.ReadAll(r.Body)
			if err == nil {
				_, _, err = scheme.Codecs.UniversalDeserializer().Decode(data, nil, &review)
			}
			if err != nil || review.Spec.Namespace != "tenant-a" {
				t.Fatal("unscoped permission review")
			}
			_, _ = w.Write([]byte(`{"apiVersion":"authorization.k8s.io/v1","kind":"SelfSubjectRulesReview","status":{"resourceRules":[{"verbs":["get","list","watch"],"apiGroups":[""],"resources":["nodes","namespaces","pods"]}],"incomplete":false}}`))
		case "/api/v1/namespaces/tenant-a":
			_, _ = w.Write([]byte(`{"apiVersion":"v1","kind":"Namespace","metadata":{"name":"tenant-a"},"status":{"phase":"Active"}}`))
		case "/apis/superplane.ai/v1/nodepools":
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
	if calls != 4 {
		t.Fatalf("got %d scoped reads", calls)
	}
	provisional := target
	provisional.Provisional = true
	provisional.ClusterID = ""
	provisional.WorkspaceStatus = "Provisioning"
	provisional.ClusterStatus = "Provisioning"
	provisional.BootstrapOperationID = "bootstrap-operation"
	provisional.RegistrationClaim = "registration-claim"
	m.config.EnableExecution = true
	if got := m.inspectTarget(context.Background(), provisional); got != "observed_execution_unavailable" {
		t.Fatal("provisional probe attempted execution or heartbeat:", got)
	}
	provisional.Assignments = []Assignment{{Binding: execution.Binding{OperationID: "forbidden"}}}
	if got := m.inspectTarget(context.Background(), provisional); got != "registration_incomplete" {
		t.Fatal("provisional target accepted assignments:", got)
	}
	m.config.EnableExecution = false
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
	if calls != 9 {
		t.Fatalf("refused credentials reached workspace: %d calls", calls)
	}
}

func TestWorkspaceManagerRefusesMutationAndCredentialPrivileges(t *testing.T) {
	for _, test := range []struct {
		name    string
		status  authorizationv1.SubjectRulesReviewStatus
		allowed bool
	}{
		{"read", authorizationv1.SubjectRulesReviewStatus{ResourceRules: []authorizationv1.ResourceRule{{Verbs: []string{"get", "list"}, APIGroups: []string{""}, Resources: []string{"nodes", "pods"}}}}, true},
		{"review", authorizationv1.SubjectRulesReviewStatus{ResourceRules: []authorizationv1.ResourceRule{{Verbs: []string{"create"}, APIGroups: []string{"authorization.k8s.io"}, Resources: []string{"selfsubjectrulesreviews"}}}}, true},
		{"workload-write", authorizationv1.SubjectRulesReviewStatus{ResourceRules: []authorizationv1.ResourceRule{{Verbs: []string{"create"}, APIGroups: []string{"batch"}, Resources: []string{"jobs"}}}}, false},
		{"secret-read", authorizationv1.SubjectRulesReviewStatus{ResourceRules: []authorizationv1.ResourceRule{{Verbs: []string{"get"}, APIGroups: []string{""}, Resources: []string{"secrets"}}}}, false},
		{"pod-exec", authorizationv1.SubjectRulesReviewStatus{ResourceRules: []authorizationv1.ResourceRule{{Verbs: []string{"get"}, APIGroups: []string{""}, Resources: []string{"pods/exec"}}}}, false},
		{"wildcard", authorizationv1.SubjectRulesReviewStatus{ResourceRules: []authorizationv1.ResourceRule{{Verbs: []string{"*"}, APIGroups: []string{"*"}, Resources: []string{"*"}}}}, false},
		{"incomplete", authorizationv1.SubjectRulesReviewStatus{Incomplete: true}, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			if readOnlyWorkspaceRules(test.status) != test.allowed {
				t.Fatal("incorrect workspace privilege decision")
			}
		})
	}
}
