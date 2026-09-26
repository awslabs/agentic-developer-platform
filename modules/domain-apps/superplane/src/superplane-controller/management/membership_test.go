package management

import (
	"context"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	authorizationv1 "k8s.io/api/authorization/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/tools/clientcmd"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
)

func TestSharedReaderRequiresProviderIdentityWithoutFleetReads(t *testing.T) {
	namespace := "sp-ws-" + strings.ReplaceAll(workspaceID, "-", "")
	generation := strings.Repeat("a", 64)
	uid := "actual-service-account-uid"
	providerUID := uid
	denyJobs := false
	calls := 0
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Content-Type", "application/json")
		if r.Header.Get("Authorization") != "Bearer member-token" {
			w.WriteHeader(403)
			return
		}
		switch r.URL.Path {
		case "/apis/authentication.k8s.io/v1/selfsubjectreviews":
			_, _ = fmt.Fprintf(w, `{"apiVersion":"authentication.k8s.io/v1","kind":"SelfSubjectReview","status":{"userInfo":{"username":"system:serviceaccount:%s:sp-reader-%s-1","uid":"%s"}}}`, namespace, generation[:24], providerUID)
		case "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews":
			_, _ = w.Write([]byte(`{"apiVersion":"authorization.k8s.io/v1","kind":"SelfSubjectRulesReview","status":{"resourceRules":[{"verbs":["get","list","watch"],"apiGroups":[""],"resources":["pods","events","resourcequotas"]}],"incomplete":false}}`))
		case "/api/v1/namespaces/" + namespace + "/pods":
			_, _ = w.Write([]byte(`{"apiVersion":"v1","kind":"PodList","items":[]}`))
		case "/apis/batch/v1/namespaces/" + namespace + "/jobs":
			if denyJobs {
				w.WriteHeader(403)
				return
			}
			_, _ = w.Write([]byte(`{"apiVersion":"batch/v1","kind":"JobList","items":[]}`))
		case "/apis/superplane.ai/v1/namespaces/" + namespace + "/superplanenodes":
			_, _ = w.Write([]byte(`{"apiVersion":"superplane.ai/v1","kind":"SuperplaneNodeList","items":[]}`))
		default:
			t.Errorf("shared reader attempted unscoped request: %s", r.URL.Path)
			w.WriteHeader(403)
		}
	}))
	defer server.Close()
	member := &MembershipCredential{OrgID: orgID, WorkspaceID: workspaceID, ClusterID: orgID, ClusterARN: "arn:aws:eks:us-east-1:123456789012:cluster/shared", Namespace: namespace, NamespaceUID: "namespace-uid", Generation: generation, ServiceAccountUID: uid, Revision: 1, ExpiresAt: time.Now().Add(10 * time.Minute).UTC().Format(time.RFC3339Nano), Scope: "reader"}
	target := Target{SharedMembership: true, PlatformEligible: true, MembershipCredential: member, WorkspaceID: workspaceID, ClusterID: orgID, ClusterARN: member.ClusterARN, Namespace: namespace, Endpoint: server.URL, WorkspaceStatus: "Ready", ClusterStatus: "Ready"}
	dir := t.TempDir()
	m := &Manager{config: Config{OrgID: orgID, WorkspaceCredentialsDir: dir, ManagementAPIServer: server.URL}}
	raw, _ := json.Marshal(member)
	config := clientcmdapi.Config{
		CurrentContext: target.ClusterARN,
		Clusters:       map[string]*clientcmdapi.Cluster{"target": {Server: server.URL, CertificateAuthorityData: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw})}},
		Contexts:       map[string]*clientcmdapi.Context{target.ClusterARN: {Cluster: "target", AuthInfo: "member", Namespace: namespace}},
		AuthInfos:      map[string]*clientcmdapi.AuthInfo{"member": {Token: "member-token"}},
		Extensions:     map[string]runtime.Object{"superplane.aws-e/membership": &runtime.Unknown{Raw: raw}},
	}
	write := func() {
		t.Helper()
		data, err := clientcmd.Write(config)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, workspaceID+".kubeconfig"), data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	write()
	if got := m.inspectTarget(context.Background(), target); got != "observed_execution_unavailable" {
		t.Fatal(got)
	}
	if calls != 5 {
		t.Fatalf("expected five scoped provider reads, got %d", calls)
	}
	target.PlatformEligible = false
	if got := m.inspectTarget(context.Background(), target); got != "credential_refused" || calls != 5 {
		t.Fatal("management cluster eligibility did not fence transport", got)
	}
	target.PlatformEligible = true
	providerUID = "replacement-account"
	if got := m.inspectTarget(context.Background(), target); got != "namespace_identity_unavailable" {
		t.Fatal("provider identity mismatch accepted", got)
	}
	providerUID = uid
	denyJobs = true
	if got := m.inspectTarget(context.Background(), target); got != "workspace_api_unavailable" {
		t.Fatal("missing observable API evidence accepted", got)
	}
	denyJobs = false
	target.Provisional, target.WorkspaceStatus = true, "Provisioning"
	target.BootstrapOperationID, target.RegistrationClaim = "original-operation", "original-claim"
	if got := m.inspectTarget(context.Background(), target); got != "observed_execution_unavailable" {
		t.Fatal("projected provisional reader refused", got)
	}
	for _, change := range []string{"revision", "expiry", "scope", "namespace", "generation", "missing"} {
		t.Run(change, func(t *testing.T) {
			copy := *member
			switch change {
			case "revision":
				copy.Revision++
			case "expiry":
				copy.ExpiresAt = time.Now().Add(-time.Second).Format(time.RFC3339Nano)
			case "scope":
				copy.Scope = "mutator"
			case "namespace":
				copy.NamespaceUID = "replacement-namespace"
			case "generation":
				copy.Generation = strings.Repeat("b", 64)
			}
			changed := target
			changed.MembershipCredential = &copy
			if change == "missing" {
				changed.MembershipCredential = nil
			}
			before := calls
			if got := m.inspectTarget(context.Background(), changed); got != "credential_refused" || before != calls {
				t.Fatal("stale registry/projection binding used", got)
			}
		})
	}
}

func TestSharedReaderRulesRefuseFleetAndPrivilegeExpansion(t *testing.T) {
	for _, resource := range []string{"nodes", "namespaces", "nodepools", "secrets", "serviceaccounts/token", "roles", "pods/exec"} {
		status := authorizationv1.SubjectRulesReviewStatus{ResourceRules: []authorizationv1.ResourceRule{{APIGroups: []string{""}, Resources: []string{resource}, Verbs: []string{"get", "list"}}}}
		if scopedReaderRules(status, true) {
			t.Fatal("shared reader accepted", resource)
		}
	}
}
