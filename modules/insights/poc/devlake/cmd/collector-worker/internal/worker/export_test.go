package worker

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
)

func TestDeriveEvidenceUsesRegisteredHandleNotCallerPath(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	root := collection.QualifiedRef{TenantID: "tenant", ProjectID: "project", SourceConnectionID: "registered-connection", RecordType: "issue", RecordID: "1", RecordRevision: "r1"}
	source := collection.Source{TenantID: "tenant", ProjectID: "project", ConnectionID: "registered-connection"}
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		calls++
		if request.Method != "POST" || request.URL.Path != "/internal/v1/connector-contexts/opaque/derive" {
			t.Errorf("unregistered evidence path: %s %s", request.Method, request.URL.Path)
		}
		var body struct {
			Root collection.QualifiedRef `json:"root"`
		}
		if json.NewDecoder(request.Body).Decode(&body) != nil || body.Root != root {
			t.Error("canonical source identity not sent")
		}
		_ = json.NewEncoder(response).Encode(map[string]any{"policy_envelope": map[string]any{"envelope_version": 1, "identity": root, "realm_id": "realm"}, "destinations": map[string]any{"raw": "ignored"}})
	}))
	defer server.Close()
	gateway := &Gateway{URL: server.URL, Client: server.Client(), Sign: func(*http.Request, []byte) error { return nil }, Contexts: map[collection.Source]SourceContext{source: {Handle: "opaque"}}}
	policy, err := gateway.DeriveEvidence(t.Context(), source, root)
	if err != nil || policy.Identity != root || policy.RealmID != "realm" || calls != 1 {
		t.Fatalf("gateway policy read failed: %+v, %v", policy, err)
	}
	for _, forged := range []collection.QualifiedRef{{TenantID: "foreign", ProjectID: "project", SourceConnectionID: "registered-connection"}, {TenantID: "tenant", ProjectID: "project", SourceConnectionID: "foreign"}} {
		if _, err := gateway.DeriveEvidence(t.Context(), source, forged); !errors.Is(err, ErrWork) {
			t.Fatalf("foreign ref used gateway handle: %v", err)
		}
	}
	if calls != 1 {
		t.Fatal("foreign ref sent to gateway")
	}
}
