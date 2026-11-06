package main

import (
	"context"
	"encoding/json"
	"encoding/pem"
	stderrors "errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/apache/devlake/core/errors"
	"github.com/apache/devlake/core/plugin"
	githubapi "github.com/apache/devlake/plugins/github/api"
	jiraapi "github.com/apache/devlake/plugins/jira/api"
)

type injectedClient struct {
	plugin.ApiClient
	calls   []string
	replies map[string]string
	queries []url.Values
	respond func(string, url.Values) string
}

func (client *injectedClient) Get(path string, query url.Values, _ http.Header) (*http.Response, errors.Error) {
	client.calls = append(client.calls, path)
	client.queries = append(client.queries, query)
	body := client.replies[path]
	if client.respond != nil {
		body = client.respond(path, query)
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Body:       io.NopCloser(strings.NewReader(body)),
		Request:    &http.Request{URL: &url.URL{Scheme: "https", Host: "provider.example", Path: "/" + path}},
	}, nil
}

func TestBoundedGithubNamespaceTraversal(test *testing.T) {
	for _, limit := range []int{1, 100} {
		test.Run(strconv.Itoa(limit), func(test *testing.T) {
			test.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
			organizations := make([]map[string]string, limit+1)
			expected := []string{"self"}
			for index := range organizations {
				name := fmt.Sprintf("team-%d", index)
				organizations[index] = map[string]string{"login": name}
				expected = append(expected, name)
			}
			client := &injectedClient{respond: func(path string, query url.Values) string {
				if path == "user" {
					return `{"login":"self"}`
				}
				if path != "user/orgs" {
					test.Fatalf("unexpected route: %s", path)
				}
				page, _ := strconv.Atoi(query.Get("page"))
				perPage, _ := strconv.Atoi(query.Get("per_page"))
				start := min((page-1)*perPage, len(organizations))
				body, err := json.Marshal(organizations[start:min(start+perPage, len(organizations))])
				if err != nil {
					test.Fatal(err)
				}
				return string(body)
			}}
			var identities []string
			cursor := ""
			complete := false
			for attempt := 0; attempt < len(expected)+2; attempt++ {
				result, err := providerPage(client, "github", false, "", cursor, limit, time.Now())
				if err != nil || len(result.Items) > limit {
					test.Fatalf("namespace cursor %q: %+v, %v", cursor, result, err)
				}
				for _, item := range result.Items {
					identities = append(identities, item.NativeID)
				}
				if result.NextCursor == "" {
					complete = result.Coverage == "complete"
					break
				}
				if result.Coverage != "partial" {
					test.Fatalf("cursor without partial coverage: %+v", result)
				}
				cursor = result.NextCursor
			}
			if !complete || !reflect.DeepEqual(identities, expected) {
				test.Fatalf("namespace coverage: complete=%v, got %v, want %v", complete, identities, expected)
			}
		})
	}
}

func TestBoundedJiraServerCappedPages(test *testing.T) {
	test.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	client := &injectedClient{respond: func(path string, query url.Values) string {
		if path != "agile/1.0/board" {
			test.Fatalf("unexpected route: %s", path)
		}
		offset, _ := strconv.Atoi(query.Get("startAt"))
		return fmt.Sprintf(`{"startAt":%d,"maxResults":1,"isLast":%t,"values":[{"id":%d,"name":"Board %d"}]}`, offset, offset == 2, offset+1, offset+1)
	}}
	var identities []string
	cursor := ""
	complete := false
	for attempt := 0; attempt < 4; attempt++ {
		result, err := providerPage(client, "jira", false, "registered-instance", cursor, 2, time.Now())
		if err != nil || len(result.Items) > 2 {
			test.Fatalf("board cursor %q: %+v, %v", cursor, result, err)
		}
		for _, item := range result.Items {
			identities = append(identities, item.NativeID)
		}
		if result.NextCursor == "" {
			complete = result.Coverage == "complete"
			break
		}
		cursor = result.NextCursor
	}
	var offsets []string
	for _, query := range client.queries {
		offsets = append(offsets, query.Get("startAt"))
	}
	if !complete || !reflect.DeepEqual(identities, []string{"1", "2", "3"}) || !reflect.DeepEqual(offsets, []string{"0", "1", "2"}) {
		test.Fatalf("board coverage: complete=%v, IDs=%v, offsets=%v", complete, identities, offsets)
	}
}

func TestProviderPageMalformedResponsesAreUnavailable(test *testing.T) {
	test.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	for _, fixture := range []struct {
		name         string
		provider     string
		installation bool
		namespace    string
		replies      map[string]string
	}{
		{"installation", "github", true, "team", map[string]string{"installation/repositories": `{"repositories":`}},
		{"personal-namespace", "github", false, "", map[string]string{"user": `{"login":`}},
		{"organizations", "github", false, "", map[string]string{"user": `{"login":"self"}`, "user/orgs": `[{`}},
		{"empty-body", "github", true, "team", map[string]string{"installation/repositories": ""}},
		{"owner", "github", false, "team", map[string]string{"users/team": `{"type":`}},
		{"authenticated-user", "github", false, "self", map[string]string{"users/self": `{"id":1,"type":"User"}`, "user": `{"id":`}},
		{"organization-repositories", "github", false, "team", map[string]string{"users/team": `{"id":1,"type":"Organization"}`, "orgs/team/repos": `[{`}},
		{"personal-repositories", "github", false, "self", map[string]string{"users/self": `{"id":1,"type":"User"}`, "user": `{"id":1}`, "user/repos": `[{`}},
		{"public-repositories", "github", false, "other", map[string]string{"users/other": `{"id":2,"type":"User"}`, "user": `{"id":1}`, "users/other/repos": `[{`}},
		{"boards", "jira", false, "registered-instance", map[string]string{"agile/1.0/board": `{"values":`}},
	} {
		test.Run(fixture.name, func(test *testing.T) {
			client := &injectedClient{replies: fixture.replies}
			cursor := ""
			if fixture.name == "organizations" {
				first, err := providerPage(client, fixture.provider, fixture.installation, fixture.namespace, cursor, 100, time.Now())
				if err != nil || first.NextCursor == "" {
					test.Fatalf("personal namespace: %+v, %v", first, err)
				}
				cursor = first.NextCursor
			}
			result, err := providerPage(client, fixture.provider, fixture.installation, fixture.namespace, cursor, 100, time.Now())
			if err != errSourceUnavailable || len(result.Items) != 0 || result.NextCursor != "" {
				test.Fatalf("malformed response returned data or exposed provider details: %+v, %v", result, err)
			}
		})
	}
}

func TestProviderPageRejectsDuplicateProviderIdentity(test *testing.T) {
	for _, fixture := range []struct {
		name     string
		provider string
		path     string
		response string
	}{
		{"github", "github", "installation/repositories", `{"repositories":[{"id":18,"name":"first","full_name":"team/first","owner":{"login":"team","id":3}},{"id":18,"name":"renamed","full_name":"team/renamed","owner":{"login":"team","id":3}}]}`},
		{"jira", "jira", "agile/1.0/board", `{"startAt":0,"maxResults":2,"isLast":true,"values":[{"id":12,"name":"Planning"},{"id":12,"name":"Renamed"}]}`},
	} {
		test.Run(fixture.name, func(test *testing.T) {
			test.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
			client := &injectedClient{replies: map[string]string{fixture.path: fixture.response}}
			page, err := providerPage(client, fixture.provider, fixture.provider == "github", "team", "", 2, time.Now())
			if err != errSourceUnavailable || len(page.Items) != 0 || page.Coverage != "" ||
				len(client.calls) != 1 || client.calls[0] != fixture.path {
				test.Fatalf("duplicate provider identity accepted: %+v, %v, %v", page, err, client.calls)
			}
		})
	}
}

func TestProviderPageInvalidJiraBoundaryIsUnavailable(test *testing.T) {
	test.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	for _, response := range []string{
		`{"startAt":0,"maxResults":0,"isLast":false,"values":[]}`,
		`{"startAt":2,"maxResults":1,"isLast":false,"values":[{"id":3,"name":"Board 3"}]}`,
	} {
		client := &injectedClient{replies: map[string]string{"agile/1.0/board": response}}
		result, err := providerPage(client, "jira", false, "registered-instance", "", 2, time.Now())
		if err != errSourceUnavailable || len(result.Items) != 0 || result.NextCursor != "" {
			test.Fatalf("invalid Jira boundary returned data: %+v, %v", result, err)
		}
	}
}

func TestInjectedGithubInstallationScopes(t *testing.T) {
	client := &injectedClient{replies: map[string]string{
		"installation/repositories": `{"repositories":[{"id":18,"name":"alpha","full_name":"team/alpha","owner":{"login":"team","id":3}},{"id":19,"name":"beta","full_name":"other/beta","owner":{"login":"other","id":4}}]}`,
	}}
	groups, _, err := githubapi.ADPListRemoteScopes(client, true, "", githubapi.GithubRemotePagination{Page: 1, PerPage: 100})
	if err != nil || len(groups) != 2 || groups[0].Id != "team" || groups[1].Id != "other" {
		t.Fatalf("installation groups: %v, %v", groups, err)
	}
	repos, _, err := githubapi.ADPListRemoteScopes(client, true, "team", githubapi.GithubRemotePagination{Page: 1, PerPage: 100})
	if err != nil || len(repos) != 1 || repos[0].Id != "18" || repos[0].ParentId == nil || *repos[0].ParentId != "team" {
		t.Fatalf("installation repositories: %v, %v", repos, err)
	}
	if strings.Join(client.calls, ",") != "installation/repositories,installation/repositories" || client.queries[0].Get("per_page") != "100" {
		t.Fatalf("incorrect injected installation route: %v", client.calls)
	}
}

func TestInjectedGithubUserScopes(t *testing.T) {
	client := &injectedClient{replies: map[string]string{
		"user":      `{"login":"self"}`,
		"user/orgs": `[{"login":"team"}]`,
	}}
	groups, next, err := githubapi.ADPListRemoteScopes(client, false, "", githubapi.GithubRemotePagination{Page: 1, PerPage: 100})
	if err != nil || len(groups) != 1 || groups[0].Id != "self" || next == nil || next.Page != 2 || len(client.calls) != 1 || client.calls[0] != "user" {
		t.Fatalf("user groups: %v, %v, %v", groups, next, err)
	}
	groups, next, err = githubapi.ADPListRemoteScopes(client, false, "", *next)
	if err != nil || len(groups) != 1 || groups[0].Id != "team" || next != nil || client.queries[1].Get("page") != "1" {
		t.Fatalf("organization groups: %v, %v, %v", groups, next, err)
	}
	if strings.Join(client.calls, ",") != "user,user/orgs" {
		t.Fatalf("incorrect injected user route: %v", client.calls)
	}
}

func TestInjectedJiraBoardScopes(t *testing.T) {
	client := &injectedClient{replies: map[string]string{
		"agile/1.0/board": `{"startAt":0,"maxResults":1,"isLast":false,"values":[{"id":12,"name":"Planning","type":"scrum"}]}`,
	}}
	boards, next, err := jiraapi.ADPListRemoteScopes(client, jiraapi.JiraRemotePagination{MaxResults: 1})
	if err != nil || len(boards) != 1 || boards[0].Id != "12" || boards[0].Data == nil || boards[0].Data.BoardId != 12 {
		t.Fatalf("jira boards: %v, %v", boards, err)
	}
	if next == nil || next.StartAt != 1 || client.calls[0] != "agile/1.0/board" || client.queries[0].Get("maxResults") != "1" {
		t.Fatalf("jira page: %v, %v", next, client.calls)
	}
}

func TestBoundedProviderPages(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	observedAt := time.Date(2026, time.October, 28, 12, 0, 0, 0, time.UTC)
	client := &injectedClient{replies: map[string]string{
		"installation/repositories": `{"repositories":[{"id":18,"name":"alpha","full_name":"team/alpha","owner":{"login":"team","id":3}},{"id":19,"name":"beta","full_name":"other/beta","owner":{"login":"other","id":4}}]}`,
		"agile/1.0/board":           `{"startAt":1,"maxResults":1,"isLast":false,"values":[{"id":12,"name":"Planning"}]}`,
	}}
	github, err := providerPage(client, "github", true, "team", "", 2, observedAt)
	if err != nil || len(github.Items) != 1 || github.Items[0].Namespace != "team" || github.Items[0].NativeID != "18" || github.Coverage != "partial" || github.NextCursor != "v1:github:2:2" {
		t.Fatalf("bounded installation page: %+v, %v", github, err)
	}
	if github.Items[0].Provenance != "observation_only" || github.Items[0].ObservedAt != observedAt.Format(time.RFC3339Nano) || github.Items[0].SourceRevision == "" {
		t.Fatalf("missing observation provenance: %+v", github.Items[0])
	}
	jira, err := providerPage(client, "jira", false, "registered-instance", "v1:jira:1:1", 1, observedAt)
	if err != nil || len(jira.Items) != 1 || jira.Items[0].Kind != "tracker" || jira.Items[0].Namespace != "registered-instance" || jira.Coverage != "partial" || client.queries[1].Get("startAt") != "1" {
		t.Fatalf("bounded jira page: %+v, %v", jira, err)
	}
}

type throttledClient struct {
	*injectedClient
}

func (client *throttledClient) Get(path string, query url.Values, header http.Header) (*http.Response, errors.Error) {
	client.calls = append(client.calls, path)
	return &http.Response{
		StatusCode: http.StatusTooManyRequests,
		Header:     http.Header{"Retry-After": []string{"1"}},
		Body:       io.NopCloser(strings.NewReader(`{"message":"rate limited"}`)),
	}, nil
}

func TestProviderPageThrottlingIsUnavailable(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	for _, provider := range []string{"github", "jira"} {
		t.Run(provider, func(t *testing.T) {
			client := &throttledClient{injectedClient: &injectedClient{}}
			page, err := providerPage(client, provider, provider == "github", "team", "", 100, time.Now())
			if err != errSourceUnavailable || len(page.Items) != 0 || len(client.calls) != 1 {
				t.Fatalf("throttled provider returned a healthy page: %+v, %v, %v", page, err, client.calls)
			}
		})
	}
}

func TestProviderPageRejectsUnsupportedOrUnboundedBeforeProvider(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	client := &injectedClient{replies: map[string]string{}}
	for _, attempt := range []struct {
		provider     string
		installation bool
		namespace    string
		cursor       string
		limit        int
		want         error
	}{
		{"github", false, "team", "v1:github:100:0", 100, errInvalidPage},
		{"github", false, "team", "v1:github:100:1", 100, errInvalidPage},
		{"github", false, "team", "v1:github:100:+2", 100, errInvalidPage},
		{"github", false, "team", "v1:github:100:02", 100, errInvalidPage},
		{"github", false, "team", "v1:github:100:1000001", 100, errInvalidPage},
		{"github", false, "team", "", 101, errInvalidPage},
		{"github", false, "team", "2", 100, errInvalidPage},
		{"github", false, "team", "v2:github:100:2", 100, errInvalidPage},
		{"github", false, "team", "v1:github:1:2", 100, errInvalidPage},
		{"github", false, "team", "v1:jira:100:2", 100, errInvalidPage},
		{"jira", false, "team", "v1:jira:100:-1", 100, errInvalidPage},
		{"jira", false, "team", "v1:jira:100:0", 100, errInvalidPage},
		{"jira", false, "team", "v1:jira:100:+1", 100, errInvalidPage},
		{"jira", false, "team", "v1:jira:100:01", 100, errInvalidPage},
		{"jira", false, "team", "v1:jira:100:1000001", 100, errInvalidPage},
		{"jira", false, "team", "v1:jira:100:invalid", 100, errInvalidPage},
		{"unknown", false, "team", "", 100, errUnsupportedCapability},
		{"jira", true, "team", "", 100, errUnsupportedCapability},
		{"jira", false, "", "", 100, errUnsupportedCapability},
	} {
		_, err := providerPage(client, attempt.provider, attempt.installation, attempt.namespace, attempt.cursor, attempt.limit, time.Now())
		if err != attempt.want {
			t.Fatalf("%s: got %v, want %v", attempt.provider, err, attempt.want)
		}
	}
	if len(client.calls) != 0 {
		t.Fatalf("invalid page performed provider calls: %v", client.calls)
	}
}

func TestProviderPageDisabledWithoutProviderCalls(t *testing.T) {
	for _, value := range []string{"absent", "", "false", "TRUE", "1", "invalid"} {
		t.Run(value, func(t *testing.T) {
			t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", value)
			if value == "absent" {
				if err := os.Unsetenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED"); err != nil {
					t.Fatal(err)
				}
			}
			client := &injectedClient{replies: map[string]string{
				"installation/repositories": `{"repositories":[{"id":18,"name":"alpha","owner":{"login":"team"}}]}`,
			}}
			page, err := providerPage(client, "github", true, "team", "", 100, time.Now())
			if err != errFeatureDisabled || len(client.calls) != 0 || len(page.Items) != 0 {
				t.Fatalf("flag %q admitted provider work: %+v, %v, %v", value, page, err, client.calls)
			}
		})
	}
}

func TestBrokerDiscoveryOnlyReturnsRevalidatedWork(test *testing.T) {
	test.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	input := profileJob{profileRequest: profileRequest{TenantID: "tenant", ProductID: "product", ProjectID: "project", OperationID: "operation", AttemptID: "attempt", BindingID: "binding", Limit: 100}, Provider: "github"}
	work := map[string]any{"tenant_id": "tenant", "product_id": "product", "project_id": "project", "operation_id": "operation", "attempt_id": "attempt", "binding_id": "binding", "cursor": "", "limit": 100, "binding_generation": 1, "connection_id": "connection", "connection_revision": strings.Repeat("a", 64), "provider": "github", "installation": true, "namespace": "owner"}
	item := discoveryItem{Kind: "repository", Namespace: "owner", NativeID: "123", Name: "repo", SourceRevision: "2026-11-01T00:00:00Z", ObservedAt: "2026-11-01T00:00:01Z"}
	payload := map[string]any{"contract_version": 1, "work": work, "items": []discoveryItem{item}, "coverage": "complete", "next_cursor": ""}
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		calls++
		if request.URL.Path != "/internal/v1/profile-discovery/github-page" || request.Method != http.MethodPost || request.Header.Get("Authorization") != "signed-for-test" {
			test.Errorf("broker boundary: %s %s", request.Method, request.URL.Path)
			response.WriteHeader(403)
			return
		}
		var requestBody profileRequest
		if json.NewDecoder(request.Body).Decode(&requestBody) != nil || requestBody != input.profileRequest {
			test.Errorf("broker work differed: %+v", requestBody)
			response.WriteHeader(403)
			return
		}
		_ = json.NewEncoder(response).Encode(payload)
	}))
	defer server.Close()
	broker := &profileBroker{address: server.URL, client: server.Client(), sign: func(request *http.Request, _ []byte) error {
		request.Header.Set("Authorization", "signed-for-test")
		return nil
	}}
	served := httptest.NewServer(brokerDiscoveryHandler(broker))
	defer served.Close()
	post := func(value any) (*http.Response, discoveryPage) {
		raw, _ := json.Marshal(value)
		reply, err := http.Post(served.URL, "application/json", strings.NewReader(string(raw)))
		if err != nil {
			test.Fatal(err)
		}
		defer reply.Body.Close()
		var page discoveryPage
		_ = json.NewDecoder(reply.Body).Decode(&page)
		return reply, page
	}
	reply, page := post(input)
	if reply.StatusCode != http.StatusOK || len(page.Items) != 1 ||
		page.Items[0].Provenance != "observation_only" ||
		page.Items[0].SourceRevision != observedItem("repository", "owner", "123", "repo", item.ObservedAt).SourceRevision ||
		page.Items[0].UpstreamRevision != item.SourceRevision || page.Coverage != "complete" || calls != 1 {
		test.Fatalf("signed broker page: status=%d page=%+v calls=%d", reply.StatusCode, page, calls)
	}
	for name, mutate := range map[string]func(){
		"changed-operation":  func() { work["operation_id"] = "other" },
		"changed-source":     func() { work["namespace"] = "other" },
		"truncated":          func() { payload["coverage"] = "complete"; payload["next_cursor"] = "v1:github:100:2" },
		"duplicate":          func() { payload["items"] = []discoveryItem{item, item} },
		"rotated-generation": func() { work["binding_generation"] = 0 },
	} {
		work["operation_id"] = "operation"
		work["namespace"] = "owner"
		work["binding_generation"] = 1
		payload["coverage"] = "complete"
		payload["next_cursor"] = ""
		payload["items"] = []discoveryItem{item}
		mutate()
		reply, page = post(input)
		if reply.StatusCode != http.StatusServiceUnavailable || len(page.Items) != 0 {
			test.Fatalf("%s admitted: status=%d page=%+v", name, reply.StatusCode, page)
		}
	}
	reply, _ = post(map[string]any{"tenant_id": "tenant", "unknown": "arbitrary_url"})
	if reply.StatusCode != http.StatusUnprocessableEntity || calls != 6 {
		test.Fatalf("unbounded request reached broker: status=%d calls=%d", reply.StatusCode, calls)
	}
}

func TestBrokerDiscoveryRefusesWithoutIdentityOrConfiguration(test *testing.T) {
	if _, err := newProfileBroker("http://example.invalid", "us-east-1"); err == nil {
		test.Fatal("insecure broker origin accepted")
	}
	test.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	broker := &profileBroker{address: "https://example.invalid", client: &http.Client{Timeout: time.Second}}
	page, err := broker.read(context.Background(), profileJob{profileRequest: profileRequest{TenantID: "tenant", ProductID: "product", ProjectID: "project", OperationID: "operation", AttemptID: "attempt", BindingID: "binding", Limit: 100}, Provider: "github"})
	if err == nil || len(page.Items) != 0 {
		test.Fatal("unsigned read admitted")
	}
	test.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "false")
	response := httptest.NewRecorder()
	brokerDiscoveryHandler(nil)(response, httptest.NewRequest(http.MethodPost, "/v1/profile-discovery", strings.NewReader(`{}`)))
	if response.Code != http.StatusServiceUnavailable {
		test.Fatalf("missing broker admitted: %d", response.Code)
	}
}

func TestBrokerJiraUsesRegisteredPageAndOpaqueContinuation(test *testing.T) {
	test.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	input := profileJob{profileRequest: profileRequest{TenantID: "tenant", ProductID: "product", ProjectID: "project", OperationID: "operation", AttemptID: "attempt", BindingID: "binding", Cursor: "v1:jira:100:t1.b3BhcXVl", Limit: 100}, Provider: "jira"}
	work := map[string]any{"tenant_id": "tenant", "product_id": "product", "project_id": "project", "operation_id": "operation", "attempt_id": "attempt", "binding_id": "binding", "cursor": input.Cursor, "limit": 100, "binding_generation": 1, "connection_id": "connection", "connection_revision": strings.Repeat("a", 64), "provider": "jira", "installation": false, "namespace": "site"}
	page := brokerPage{ContractVersion: 1, Items: []discoveryItem{{Kind: "tracker", Namespace: "site", NativeID: "42", Name: "board", SourceRevision: "observation-sha256:" + strings.Repeat("b", 64), ObservedAt: "2026-11-01T00:00:00Z"}}, NextCursor: "v1:jira:100:t2.bmV4dA", Coverage: "partial"}
	brokerResponse := map[string]any{"contract_version": 1, "work": work, "items": page.Items, "next_cursor": page.NextCursor, "coverage": page.Coverage}
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/internal/v1/profile-discovery/jira-boards-page" {
			test.Errorf("wrong broker endpoint: %s", request.URL.Path)
			response.WriteHeader(404)
			return
		}
		_ = json.NewEncoder(response).Encode(brokerResponse)
	}))
	defer server.Close()
	broker := &profileBroker{address: server.URL, client: server.Client(), sign: func(*http.Request, []byte) error { return nil }}
	result, err := broker.read(context.Background(), input)
	if err != nil || result.NextCursor != page.NextCursor ||
		result.Items[0].Provenance != "observation_only" ||
		result.Items[0].SourceRevision != observedItem("tracker", "site", "42", "board", page.Items[0].ObservedAt).SourceRevision ||
		result.Items[0].UpstreamRevision != page.Items[0].SourceRevision {
		test.Fatalf("Jira continuation: %+v %v", result, err)
	}
	for _, cursor := range []string{"v1:jira:100:t1.bmV4dA", "v1:jira:100:1", "v1:jira:100:t102.bmV4dA", "v1:jira:100:t2.invalid!"} {
		brokerResponse["next_cursor"] = cursor
		if result, err = broker.read(context.Background(), input); err == nil || len(result.Items) != 0 {
			test.Fatalf("Jira nonadvancing or malformed cursor %q admitted: %+v", cursor, result)
		}
	}
	brokerResponse["next_cursor"] = page.NextCursor
	work["connection_revision"] = "rotated"
	if _, err = broker.read(context.Background(), input); err == nil {
		test.Fatal("changed connection accepted")
	}
}

func TestBrokerCursorRefusesReplayAndInvalidInputs(test *testing.T) {
	for _, input := range []struct{ provider, cursor string }{
		{"github", "v1:github:100:1"},
		{"github", "v1:github:100:02"},
		{"jira", "v1:jira:100:0"},
		{"jira", "v1:jira:100:t01.bmV4dA"},
		{"jira", "v1:jira:100:t1.YQBi"},
		{"jira", "v1:jira:100:t1.bmV4dA="},
	} {
		if _, err := brokerCursorPosition(input.cursor, input.provider); !stderrors.Is(err, errInvalidPage) {
			test.Fatalf("accepted invalid %s cursor %q: %v", input.provider, input.cursor, err)
		}
	}
	for _, input := range []struct{ provider, cursor string }{
		{"github", ""},
		{"github", "v1:github:100:2"},
		{"jira", ""},
		{"jira", "v1:jira:100:1"},
		{"jira", "v1:jira:100:t1.b3BhcXVl"},
	} {
		if _, err := brokerCursorPosition(input.cursor, input.provider); err != nil {
			test.Fatalf("refused valid %s cursor %q: %v", input.provider, input.cursor, err)
		}
	}
}

// Exercise main in a separate process with workload credentials supplied by a
// loopback endpoint. No test-only resolver or provider credential reaches Go.
func TestBrokerProductionProcessPreservesCompletePageContract(t *testing.T) {
	if os.Getenv("ADP_TEST_PROFILE_SERVER") == "1" {
		main()
		return
	}
	var credentialReads, pageReads atomic.Int32
	gateway := httptest.NewTLSServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/credentials" {
			credentialReads.Add(1)
			_ = json.NewEncoder(response).Encode(map[string]string{
				"AccessKeyId": "test-access", "SecretAccessKey": "test-secret",
				"Token": "test-session", "Expiration": time.Now().Add(time.Hour).UTC().Format(time.RFC3339),
			})
			return
		}
		if request.Method != http.MethodPost || request.URL.Path != "/internal/v1/profile-discovery/github-page" ||
			!strings.HasPrefix(request.Header.Get("Authorization"), "AWS4-HMAC-SHA256 Credential=test-access/") ||
			request.Header.Get("X-Amz-Security-Token") != "test-session" {
			http.Error(response, "unauthorized", http.StatusForbidden)
			return
		}
		pageReads.Add(1)
		var work map[string]any
		if json.NewDecoder(request.Body).Decode(&work) != nil || work["tenant_id"] != "tenant" || work["operation_id"] != "operation" {
			http.Error(response, "invalid_work", http.StatusUnprocessableEntity)
			return
		}
		for field, value := range map[string]any{"binding_generation": 1, "connection_id": "connection", "connection_revision": strings.Repeat("a", 64), "provider": "github", "installation": true, "namespace": "owner"} {
			work[field] = value
		}
		_ = json.NewEncoder(response).Encode(map[string]any{
			"contract_version": 1, "work": work, "coverage": "complete", "next_cursor": "",
			"items": []discoveryItem{{Kind: "repository", Namespace: "owner", NativeID: "123", Name: "repo", FullName: "owner/repo", SourceRevision: "upstream-revision", ObservedAt: "2026-11-01T00:00:01Z"}},
		})
	}))
	defer gateway.Close()
	certificate := filepath.Join(t.TempDir(), "gateway.pem")
	if err := os.WriteFile(certificate, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: gateway.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	listener.Close()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	command := exec.Command(executable, "-test.run=^TestBrokerProductionProcessPreservesCompletePageContract$")
	for _, entry := range os.Environ() {
		if !strings.HasPrefix(entry, "AWS_") && !strings.HasPrefix(entry, "CONNECTOR_") &&
			!strings.HasPrefix(entry, "FEATURE_DEVLAKE_") && !strings.HasPrefix(entry, "SSL_CERT_FILE=") {
			command.Env = append(command.Env, entry)
		}
	}
	command.Env = append(command.Env, "ADP_TEST_PROFILE_SERVER=1", "CONNECTOR_API_LISTEN="+address,
		"FEATURE_DEVLAKE_CONNECTORS_ENABLED=true", "CONNECTOR_GATEWAY_URL="+gateway.URL,
		"AWS_REGION=us-east-1", "AWS_EC2_METADATA_DISABLED=true", "AWS_CONTAINER_CREDENTIALS_FULL_URI="+gateway.URL+"/credentials",
		"SSL_CERT_FILE="+certificate)
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = command.Process.Kill(); _ = command.Wait() }()
	client := &http.Client{Timeout: time.Second}
	ready := false
	for deadline := time.Now().Add(10 * time.Second); time.Now().Before(deadline); {
		response, err := client.Get("http://" + address + "/healthz")
		if err == nil {
			response.Body.Close()
			ready = response.StatusCode == http.StatusOK
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !ready {
		t.Fatal("connector process did not become healthy")
	}
	input := profileJob{profileRequest: profileRequest{TenantID: "tenant", ProductID: "product", ProjectID: "project", OperationID: "operation", AttemptID: "attempt", BindingID: "binding", Limit: 100}, Provider: "github"}
	payload, _ := json.Marshal(input)
	response, err := client.Post("http://"+address+"/v1/profile-discovery", "application/json", strings.NewReader(string(payload)))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var result map[string]json.RawMessage
	if response.StatusCode != http.StatusOK || json.NewDecoder(response.Body).Decode(&result) != nil {
		t.Fatalf("production discovery status %d", response.StatusCode)
	}
	// The Gateway requires an explicit empty cursor on a complete page.
	if string(result["next_cursor"]) != `""` || string(result["coverage"]) != `"complete"` ||
		credentialReads.Load() != 1 || pageReads.Load() != 1 {
		t.Fatalf("incomplete served contract: %s credentials=%d pages=%d", result, credentialReads.Load(), pageReads.Load())
	}
	var items []discoveryItem
	if json.Unmarshal(result["items"], &items) != nil || len(items) != 1 || items[0].FullName != "owner/repo" ||
		items[0].Provenance != "observation_only" || items[0].UpstreamRevision != "upstream-revision" {
		t.Fatalf("source metadata lost: %+v", items)
	}
}
