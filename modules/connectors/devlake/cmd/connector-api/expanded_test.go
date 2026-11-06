package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/apache/devlake/core/errors"
)

type expandedClient struct{ *injectedClient }

func (c expandedClient) Post(path string, query url.Values, body interface{}, headers http.Header) (*http.Response, errors.Error) {
	return c.Get(path, query, headers)
}

func TestProviderExpandedDispatchAndFlag(t *testing.T) {
	for _, tc := range []struct {
		provider, namespace, path, body, kind, id, fullName string
	}{
		{"sonarqube", "", "components/search_projects", `{"paging":{"pageIndex":1,"pageSize":10,"total":1},"components":[{"key":"team:api","name":"API"}]}`, "quality_project", "team:api", ""},
		{"jenkins", "team", "job/team/api/json", `{"jobs":[{"name":"release","fullName":"team/release","_class":"hudson.model.FreeStyleProject"}]}`, "ci_job", "team/release", ""},
		{"argocd", "team", "applications", `{"items":[{"metadata":{"name":"api","namespace":"ns"},"spec":{"project":"team"}}]}`, "deployment_application", "api", ""},
		{"gitlab", "user:77", "users/77/projects", `[{"id":901,"name":"API","path_with_namespace":"person/api","namespace":{"id":88,"kind":"user"},"owner":{"id":77}}]`, "repository", "901", "person/api"},
		{"pagerduty", "", "services", `{"offset":0,"limit":10,"more":false,"services":[{"id":"P1","name":"API"}]}`, "incident_service", "P1", ""},
		{"linear", "", "", `{"data":{"teams":{"nodes":[{"id":"team-a","name":"Engineering","key":"ENG"}],"pageInfo":{"hasNextPage":false,"endCursor":null}}}}`, "tracker_team", "team-a", ""},
		{"slack", "", "conversations.list", `{"ok":true,"channels":[{"id":"C123","name":"team","is_channel":true}],"response_metadata":{"next_cursor":""}}`, "communication_channel", "C123", ""},
	} {
		t.Run(tc.provider, func(t *testing.T) {
			client := expandedClient{&injectedClient{replies: map[string]string{tc.path: tc.body}}}
			observed := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
			for _, flag := range []string{"", "false", "TRUE", "true"} {
				t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", flag)
				page, err := providerPage(client, tc.provider, false, tc.namespace, "", 10, observed)
				if flag != "true" {
					if err != errFeatureDisabled || len(client.calls) != 0 {
						t.Fatalf("disabled provider called: %+v, %v", client.calls, err)
					}
					continue
				}
				if err != nil || len(page.Items) != 1 || page.Coverage != "complete" || len(client.calls) != 1 || client.calls[0] != tc.path {
					t.Fatalf("dispatch: %+v, %v, calls=%v", page, err, client.calls)
				}
				item := page.Items[0]
				if item.Kind != tc.kind || item.NativeID != tc.id || item.FullName != tc.fullName || item.Provenance != "observation_only" || item.ObservedAt != "2026-01-02T03:04:05Z" || !strings.HasPrefix(item.SourceRevision, "observation-sha256:") {
					t.Fatalf("mapping lost identity/provenance: %+v", item)
				}
			}
		})
	}
}

func TestProviderExpandedCursorAndPartialCoverage(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	client := expandedClient{&injectedClient{replies: map[string]string{
		"conversations.list": `{"ok":true,"channels":[],"response_metadata":{"next_cursor":"opaque+/=:next"}}`,
	}}}
	first, err := providerPage(client, "slack", false, "", "", 10, time.Now())
	if err != nil || first.Coverage != "partial" || first.NextCursor == "" {
		t.Fatalf("first page: %+v, %v", first, err)
	}
	client.replies["conversations.list"] = `{"ok":true,"channels":[],"response_metadata":{"next_cursor":""}}`
	next, err := providerPage(client, "slack", false, "", first.NextCursor, 10, time.Now())
	if err != nil || next.Coverage != "complete" || client.queries[1].Get("cursor") != "opaque+/=:next" {
		t.Fatalf("native cursor changed: %+v, %v", next, err)
	}
	for _, cursor := range []string{"v1:linear:10:2:a", "v1:slack:1:2:a", "v1:slack:10:6:a", "v1:slack:10:1:", "v1:slack:10:2:%invalid", "v1:slack:10:2:"} {
		if _, err := providerPage(client, "slack", false, "", cursor, 10, time.Now()); err != errInvalidPage || len(client.calls) != 2 {
			t.Fatalf("invalid continuation called provider: %q, %v", cursor, err)
		}
	}
	client.replies["conversations.list"] = `{"ok":true,"channels":[],"response_metadata":{"next_cursor":"more"}}`
	last, err := providerPage(client, "slack", false, "", "v1:slack:10:5:previous", 10, time.Now())
	if err != nil || last.NextCursor != "" || last.Coverage != "partial" || last.Reason != "discovery_page_limit" {
		t.Fatalf("page limit hidden: %+v, %v", last, err)
	}
}

func TestExpandedBrokerServesProvidersWithCurrentWork(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	for _, fixture := range []struct {
		provider, namespace, path, data, kind, id string
	}{
		{"sonarqube", "", "components/search_projects", `{"paging":{"pageIndex":1,"pageSize":100,"total":1},"components":[{"key":"team:api","name":"API"}]}`, "quality_project", "team:api"},
		{"argocd", "team", "applications", `{"items":[{"metadata":{"name":"api","namespace":"ns"},"spec":{"project":"team"}}]}`, "deployment_application", "api"},
		{"gitlab", "user:77", "users/77/projects", `[{"id":901,"name":"API","path_with_namespace":"person/api","namespace":{"id":88,"kind":"user"},"owner":{"id":77}}]`, "repository", "901"},
		{"pagerduty", "", "services", `{"offset":0,"limit":100,"more":false,"services":[{"id":"P1","name":"API"}]}`, "incident_service", "P1"},
		{"linear", "", "", `{"data":{"teams":{"nodes":[{"id":"team-a","name":"Engineering","key":"ENG"}],"pageInfo":{"hasNextPage":false,"endCursor":null}}}}`, "tracker_team", "team-a"},
		{"slack", "", "conversations.list", `{"ok":true,"channels":[{"id":"C123","name":"team","is_channel":true}],"response_metadata":{"next_cursor":""}}`, "communication_channel", "C123"},
	} {
		t.Run(fixture.provider, func(t *testing.T) {
			input := profileJob{profileRequest: profileRequest{TenantID: "tenant", ProductID: "product", ProjectID: "project", OperationID: "operation", AttemptID: "attempt", BindingID: "binding", Limit: 100}, Provider: fixture.provider}
			work := profileWork{profileRequest: input.profileRequest, BindingGeneration: 1, ConnectionID: "connection", ConnectionRevision: strings.Repeat("a", 64), Provider: fixture.provider, Namespace: fixture.namespace}
			resolves, reads := 0, 0
			server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
				if request.Method != http.MethodPost || request.Header.Get("Authorization") != "signed-for-test" {
					t.Error("unsigned or unexpected callback")
					response.WriteHeader(http.StatusForbidden)
					return
				}
				switch request.URL.Path {
				case "/internal/v1/profile-discovery/resolve":
					resolves++
					var body profileRequest
					if json.NewDecoder(request.Body).Decode(&body) != nil || body != input.profileRequest {
						t.Error("resolve must use persisted work identifiers")
					}
					_ = json.NewEncoder(response).Encode(work)
				case "/internal/v1/profile-discovery/provider-page":
					reads++
					var body expandedProfileRequest
					if json.NewDecoder(request.Body).Decode(&body) != nil || body.profileRequest != input.profileRequest || strings.TrimPrefix(body.Path, "/") != fixture.path ||
						(fixture.provider == "linear") != (body.Method == http.MethodPost) || (fixture.provider == "linear" && !strings.Contains(string(body.Body), "teams(first: 100")) {
						t.Errorf("unexpected provider request: %+v", body)
						response.WriteHeader(http.StatusBadRequest)
						return
					}
					_ = json.NewEncoder(response).Encode(map[string]any{"contract_version": 1, "work": work, "data": json.RawMessage(fixture.data)})
				default:
					t.Error("unknown callback route")
					response.WriteHeader(http.StatusNotFound)
				}
			}))
			defer server.Close()
			broker := &profileBroker{address: server.URL, client: server.Client(), sign: func(request *http.Request, _ []byte) error {
				request.Header.Set("Authorization", "signed-for-test")
				return nil
			}}
			page, err := broker.read(context.Background(), input)
			if err != nil || resolves != 2 || reads != 1 || len(page.Items) != 1 || page.Items[0].Kind != fixture.kind || page.Items[0].NativeID != fixture.id || page.Items[0].Provenance != "observation_only" {
				t.Fatalf("served discovery: %+v, %v; resolves=%d reads=%d", page, err, resolves, reads)
			}
		})
	}
}

func TestExpandedBrokerRejectsRevocationDuringRead(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	input := profileJob{profileRequest: profileRequest{TenantID: "tenant", ProductID: "product", ProjectID: "project", OperationID: "operation", AttemptID: "attempt", BindingID: "binding", Limit: 100}, Provider: "slack"}
	work := profileWork{profileRequest: input.profileRequest, BindingGeneration: 1, ConnectionID: "connection", ConnectionRevision: strings.Repeat("a", 64), Provider: "slack"}
	resolves, reads := 0, 0
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/internal/v1/profile-discovery/resolve" {
			resolves++
			if resolves == 2 {
				work.BindingGeneration++
			}
			_ = json.NewEncoder(response).Encode(work)
			return
		}
		reads++
		_ = json.NewEncoder(response).Encode(map[string]any{"contract_version": 1, "work": work, "data": json.RawMessage(`{"ok":true,"channels":[{"id":"C123","name":"team","is_channel":true}],"response_metadata":{"next_cursor":""}}`)})
	}))
	defer server.Close()
	broker := &profileBroker{address: server.URL, client: server.Client(), sign: func(*http.Request, []byte) error { return nil }}
	page, err := broker.read(context.Background(), input)
	if err == nil || len(page.Items) != 0 || resolves != 2 || reads != 1 {
		t.Fatalf("revoked result admitted: %+v %v; resolves=%d reads=%d", page, err, resolves, reads)
	}
}
