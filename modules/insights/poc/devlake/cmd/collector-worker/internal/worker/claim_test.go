package worker

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestGatewayAdmitBoundsExpandedContextClaims(t *testing.T) {
	for _, scenario := range []struct {
		name      string
		providers []string
		foreign   bool
		stale     bool
		wrongKind bool
		valid     bool
	}{
		{"native pair", []string{"github", "jira"}, false, false, false, true},
		{"nine distinct grants", []string{"github", "jira", "sonarqube", "jenkins", "argocd", "gitlab", "pagerduty", "linear", "slack"}, false, false, false, true},
		{"duplicate grant context", []string{"github", "github"}, false, false, false, false},
		{"foreign tenant", []string{"github", "jira"}, true, false, false, false},
		{"stale source generation", []string{"github", "jira"}, false, true, false, false},
		{"wrong native kind", []string{"github", "jira"}, false, false, true, false},
		{"unsupported provider", []string{"github", "unknown"}, false, false, false, false},
		{"too many contexts", []string{"github", "jira", "sonarqube", "jenkins", "argocd", "gitlab", "pagerduty", "linear", "slack", "github"}, false, false, false, false},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
			t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
			requested := make([]json.RawMessage, 0, len(scenario.providers))
			for _, provider := range scenario.providers {
				requested = append(requested, json.RawMessage(`{"provider":"`+provider+`"}`))
			}
			resolved, pages := 0, 0
			server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
				response.Header().Set("Content-Type", "application/json")
				switch {
				case strings.HasSuffix(request.URL.Path, "/claim"):
					_ = json.NewEncoder(response).Encode(Claim{JobID: "job", TenantID: "tenant", AttemptID: "attempt", Fence: 1, Contexts: requested})
				case strings.HasSuffix(request.URL.Path, "/resolve"):
					resolved++
					var body struct {
						Provider string `json:"provider"`
					}
					_ = json.NewDecoder(request.Body).Decode(&body)
					_ = json.NewEncoder(response).Encode(map[string]string{"handle": body.Provider})
				case strings.HasPrefix(request.URL.Path, "/internal/v1/connector-contexts/") && !strings.HasSuffix(request.URL.Path, "/page"):
					provider := strings.TrimPrefix(request.URL.Path, "/internal/v1/connector-contexts/")
					tenant := "tenant"
					if scenario.foreign && provider == "jira" {
						tenant = "foreign"
					}
					generation := 1
					if scenario.stale && provider == "jira" {
						generation = 0
					}
					scopeKinds := map[string]string{
						"github": "repository", "jira": "tracker", "sonarqube": "quality_project", "jenkins": "ci_job",
						"argocd": "deployment_application", "gitlab": "repository", "pagerduty": "incident_service",
						"linear": "tracker_team", "slack": "communication_channel",
					}
					selectedKind := scopeKinds[provider]
					if scenario.wrongKind && provider == "jira" {
						selectedKind = "repository"
					}
					_ = json.NewEncoder(response).Encode(map[string]any{
						"context_version": 1, "tenant_id": tenant, "project_id": "project", "cell_id": "cell",
						"provider": provider, "attempt_id": "attempt", "lease_fence": 1, "module_generation": 1,
						"binding_id": provider + "-binding", "binding_revision": 1, "source_generation": generation,
						"grant_id": provider + "-grant", "consumer": "worker", "purpose": "collection",
						"expires_at":        time.Now().Add(time.Hour),
						"source_selections": []any{map[string]string{"tenant_id": tenant, "project_id": "project", "source_connection_id": provider + "-connection", "record_id": provider + "-native", "record_type": selectedKind, "record_revision": "r1"}},
					})
				default:
					pages++
					response.WriteHeader(http.StatusNotFound)
				}
			}))
			defer server.Close()
			gateway := &Gateway{URL: server.URL, Client: server.Client(), Sign: func(*http.Request, []byte) error { return nil }}
			job, err := gateway.Admit(t.Context(), "tenant", "job", "claim_key_at_least_16")
			if scenario.valid && (err != nil || len(job.Sources) != len(scenario.providers) || len(gateway.Contexts) != len(scenario.providers)) {
				t.Fatalf("scoped claim: %v, sources %d contexts %d", err, len(job.Sources), len(gateway.Contexts))
			}
			if !scenario.valid && err == nil {
				t.Fatalf("invalid claim admitted: %+v", job)
			}
			if pages != 0 || scenario.name == "too many contexts" && resolved != 0 {
				t.Fatalf("denied claim reached provider or resolution: pages %d resolutions %d", pages, resolved)
			}
		})
	}
}
