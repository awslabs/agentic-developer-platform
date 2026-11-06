package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestJenkinsBrokerRunsExtractedProviderAndRejectsChangedWork(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	for _, changed := range []bool{false, true} {
		input := profileJob{profileRequest: profileRequest{TenantID: "tenant", ProductID: "product", ProjectID: "project", OperationID: "operation", AttemptID: "attempt", BindingID: "binding", Limit: 100}, Provider: "jenkins"}
		work := profileWork{profileRequest: input.profileRequest, BindingGeneration: 1, ConnectionID: "connection", ConnectionRevision: strings.Repeat("a", 64), Provider: "jenkins", Namespace: "team space"}
		calls := 0
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls++
			var request profileRequest
			if r.Method != http.MethodPost || json.NewDecoder(r.Body).Decode(&request) != nil || request != input.profileRequest {
				t.Error("callback must contain only persisted work identifiers")
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			if r.URL.Path == "/internal/v1/profile-discovery/resolve" {
				_ = json.NewEncoder(w).Encode(work)
				return
			}
			if r.URL.Path != "/internal/v1/profile-discovery/jenkins-page" {
				t.Error("unexpected callback")
				w.WriteHeader(http.StatusNotFound)
				return
			}
			if changed {
				work.BindingGeneration++
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"contract_version": 1, "work": work,
				"data": map[string]any{"jobs": []map[string]any{{"_class": "hudson.model.FreeStyleProject", "name": "api", "fullName": "team space/api", "url": "https://ignored.example/"}}},
			})
		}))
		broker := &profileBroker{address: server.URL, client: server.Client(), sign: func(*http.Request, []byte) error { return nil }}
		result, err := broker.read(context.Background(), input)
		server.Close()
		if calls != 2 {
			t.Fatalf("expected two bounded authority callbacks, got %d", calls)
		}
		if changed {
			if err == nil || len(result.Items) != 0 {
				t.Fatal("changed authority produced observations")
			}
			continue
		}
		if err != nil || result.Coverage != "complete" || result.NextCursor != "" || len(result.Items) != 1 {
			t.Fatalf("Jenkins discovery failed: %+v %v", result, err)
		}
		item := result.Items[0]
		if item.Kind != "ci_job" || item.Namespace != "team space" || item.NativeID != "team space/api" || item.FullName != item.NativeID || item.Provenance != "observation_only" {
			t.Fatalf("lost native Jenkins identity: %+v", item)
		}
	}
}
