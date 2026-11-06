package providers

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	lakeerrors "github.com/apache/devlake/core/errors"
	"github.com/apache/devlake/core/plugin"
)

type workflowRequest struct {
	Method, Path string
	Query        url.Values
	Body         map[string]interface{}
}
type workflowFixtureClient struct {
	plugin.ApiClient
	base   string
	client *http.Client
	calls  []workflowRequest
}

func (c *workflowFixtureClient) Get(path string, query url.Values, headers http.Header) (*http.Response, lakeerrors.Error) {
	c.calls = append(c.calls, workflowRequest{Method: "GET", Path: path, Query: query})
	response, err := c.client.Get(c.base + "/" + path + "?" + query.Encode())
	return response, lakeerrors.Convert(err)
}
func (c *workflowFixtureClient) Post(path string, query url.Values, body interface{}, headers http.Header) (*http.Response, lakeerrors.Error) {
	payload, _ := json.Marshal(body)
	decoded := map[string]interface{}{}
	_ = json.Unmarshal(payload, &decoded)
	c.calls = append(c.calls, workflowRequest{Method: "POST", Path: path, Query: query, Body: decoded})
	response, err := c.client.Post(c.base+"/"+path, "application/json", strings.NewReader(string(payload)))
	return response, lakeerrors.Convert(err)
}
func workflowFixture(t *testing.T, handler func(http.ResponseWriter, *http.Request)) *workflowFixtureClient {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(handler))
	t.Cleanup(server.Close)
	return &workflowFixtureClient{base: server.URL, client: server.Client()}
}
func workflowJSON(t *testing.T, body string, status int) *workflowFixtureClient {
	return workflowFixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	})
}
func TestGitlabStageResetsProjectPageAndPreservesPaths(t *testing.T) {
	client := workflowFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/groups/42/subgroups" {
			if r.URL.Query().Get("page") == "1" {
				w.Header().Set("X-Next-Page", "2")
				_, _ = io.WriteString(w, `[{"id":51,"name":"First","full_path":"team/first","parent_id":42}]`)
			} else {
				_, _ = io.WriteString(w, `[{"id":52,"name":"Second","full_path":"team/second","parent_id":42}]`)
			}
		} else if r.URL.Path == "/groups/42/projects" {
			if r.URL.Query().Get("page") != "1" || r.URL.Query().Get("with_shared") != "false" || r.URL.Query().Get("include_subgroups") != "false" {
				t.Errorf("wrong project boundary query: %v", r.URL.Query())
			}
			_, _ = io.WriteString(w, `[{"id":900,"name":"API","path_with_namespace":"team/api","namespace":{"id":42,"kind":"group"}}]`)
		} else {
			t.Errorf("unexpected URL %s", r.URL.Path)
			w.WriteHeader(404)
		}
	})
	request := req("gitlab", "42", 1)
	first, err := Discover(client, request)
	if err != nil || first.NextCursor != "groups:2" || first.Items[0].FullName != "team/first" {
		t.Fatalf("first page %+v %v", first, err)
	}
	request.Page, request.Cursor = first.NextPage, first.NextCursor
	second, err := Discover(client, request)
	if err != nil || second.NextCursor != "projects:1" || second.NextPage != 3 {
		t.Fatalf("group-project transition %+v %v", second, err)
	}
	request.Page, request.Cursor = second.NextPage, second.NextCursor
	third, err := Discover(client, request)
	if err != nil || third.Coverage != "complete" || len(third.Items) != 1 || third.Items[0].NativeID != "900" || third.Items[0].Namespace != "42" || third.Items[0].FullName != "team/api" {
		t.Fatalf("project result %+v %v", third, err)
	}
	if len(client.calls) != 3 {
		t.Fatalf("helpers combined native requests: %+v", client.calls)
	}
}
func TestGitlabRootAndPersonalNamespacesNeverRequireGetData(t *testing.T) {
	// Embedded ApiClient.GetData is intentionally nil: the patched group helper
	// must not consult implicit upstream user data or insert a synthetic result.
	client := workflowJSON(t, `[{"id":42,"name":"Team","full_path":"team","parent_id":null}]`, 200)
	page, err := Discover(client, req("gitlab", "", 1))
	if err != nil || len(page.Items) != 1 || page.Items[0].NativeID != "42" || page.Reason != "group_namespaces_only" {
		t.Fatalf("root %+v %v", page, err)
	}
	personal := workflowJSON(t, `[{"id":901,"name":"API","path_with_namespace":"person/api","namespace":{"id":88,"kind":"user"},"owner":{"id":77}}]`, 200)
	page, err = Discover(personal, req("gitlab", "user:77", 1))
	if err != nil || page.Items[0].Namespace != "user:77" || personal.calls[0].Path != "users/77/projects" {
		t.Fatalf("personal %+v %v", page, err)
	}
	if personal.calls[0].Query.Has("with_shared") {
		t.Fatal("group-only query applied to user endpoint")
	}
}
func TestGitlabRejectsInvalidHeadersIdentityAndTraversal(t *testing.T) {
	for _, header := range []string{"1", "0", "three", "03", "999"} {
		client := workflowFixture(t, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("X-Next-Page", header)
			_, _ = io.WriteString(w, `[{"id":42,"name":"Team","full_path":"team"}]`)
		})
		if _, err := Discover(client, req("gitlab", "", 1)); err == nil {
			t.Fatalf("accepted next-page %q", header)
		}
	}
	for _, namespace := range []string{"../42", "user:0", "user:001", "https://example.test", "42/projects", "-1"} {
		client := workflowJSON(t, `[]`, 200)
		if _, err := Discover(client, req("gitlab", namespace, 1)); err != ErrRequest || len(client.calls) != 0 {
			t.Fatalf("unsafe namespace %q dispatched", namespace)
		}
	}
	cases := []string{`null`, `[null]`, `{"error":"denied"}`, `[{"id":42,"name":"Team","full_path":"team","parent_id":99}]`, `[{"id":0,"name":"Team","full_path":"team"}]`}
	for _, body := range cases {
		if _, err := Discover(workflowJSON(t, body, 200), req("gitlab", "", 2)); err == nil {
			t.Fatalf("accepted malformed group: %s", body)
		}
	}
	for _, status := range []int{401, 403, 500} {
		if _, err := Discover(workflowJSON(t, `[]`, status), req("gitlab", "", 2)); err != ErrSource {
			t.Fatalf("HTTP %d error became %v", status, err)
		}
	}
}
func TestPagerDutyNativeOffsetAndStableServiceIDs(t *testing.T) {
	client := workflowJSON(t, `{"offset":0,"limit":2,"more":true,"services":[{"id":"P100","name":"API"},{"id":"P200","name":"API"}]}`, 200)
	page, err := Discover(client, req("pagerduty", "", 2))
	if err != nil || len(page.Items) != 2 || page.Items[0].NativeID != "P100" || page.NextPage != 2 || page.Items[0].Kind != "incident_service" {
		t.Fatalf("services %+v %v", page, err)
	}
	if client.calls[0].Path != "services" || client.calls[0].Query.Get("offset") != "0" || client.calls[0].Query.Get("limit") != "2" {
		t.Fatalf("context-path unsafe request %+v", client.calls)
	}
	request := req("pagerduty", "", 2)
	request.Page = 2
	tail := workflowJSON(t, `{"offset":2,"limit":2,"more":false,"services":[]}`, 200)
	page, err = Discover(tail, request)
	if err != nil || page.NextPage != 0 || page.Coverage != "complete" || tail.calls[0].Query.Get("offset") != "2" {
		t.Fatalf("tail %+v %v", page, err)
	}
	for _, body := range []string{`{"services":[]}`, `{"offset":1,"limit":2,"more":false,"services":[]}`, `{"offset":0,"limit":1,"more":false,"services":[]}`, `{"offset":0,"limit":2,"more":true,"services":[]}`, `{"offset":0,"limit":2,"more":false,"services":[null]}`, `{"offset":0,"limit":2,"more":false,"services":[{"id":"P1","name":"A"},{"id":"P1","name":"B"}]}`} {
		if _, err := Discover(workflowJSON(t, body, 200), req("pagerduty", "", 2)); err == nil {
			t.Fatalf("accepted invalid services %s", body)
		}
	}
}
func TestLinearUsesOnlyBoundedTeamsGraphQLRead(t *testing.T) {
	first := workflowJSON(t, `{"data":{"teams":{"nodes":[{"id":"team-a","name":"Engineering","key":"ENG"}],"pageInfo":{"hasNextPage":true,"endCursor":"opaque+/="}}}}`, 200)
	page, err := Discover(first, req("linear", "", 2))
	if err != nil || page.NextCursor != "opaque+/=" || page.NextPage != 2 || page.Items[0].Kind != "tracker_team" {
		t.Fatalf("teams %+v %v", page, err)
	}
	call := first.calls[0]
	expected := `query { teams(first: 2) { nodes { id name key description } pageInfo { hasNextPage endCursor } } }`
	if call.Method != "POST" || call.Path != "" || call.Body["query"] != expected || len(call.Body) != 1 {
		t.Fatalf("unexpected GraphQL read %+v", call)
	}
	request := req("linear", "", 2)
	request.Page = 2
	request.Cursor = page.NextCursor
	next := workflowJSON(t, `{"data":{"teams":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}}}`, 200)
	page, err = Discover(next, request)
	if err != nil || page.Coverage != "complete" || !strings.Contains(next.calls[0].Body["query"].(string), `after: "opaque+/="`) {
		t.Fatalf("cursor read %+v %v", page, err)
	}
	bounded := &workflowClient{readClient: readClient{ApiClient: first, request: req("linear", "", 2)}}
	if _, err := bounded.Post("", nil, map[string]interface{}{"query": "mutation { issueCreate { success } }"}, nil); err == nil || len(first.calls) != 1 {
		t.Fatal("mutation reached caller client")
	}
}
func TestLinearErrorsMissingDataAndNonprogressingCursorsFail(t *testing.T) {
	cases := []string{`{}`, `{"data":null}`, `{"data":{"teams":null}}`, `{"data":{"teams":{"nodes":null,"pageInfo":{"hasNextPage":false}}}}`, `{"data":{"teams":{"nodes":[],"pageInfo":{}}}}`, `{"data":{"teams":{"nodes":[],"pageInfo":{"hasNextPage":true,"endCursor":null}}}}`, `{"data":{"teams":{"nodes":[],"pageInfo":{"hasNextPage":true,"endCursor":""}}}}`, `{"data":{"teams":{"nodes":[],"pageInfo":{"hasNextPage":false}}},"errors":[{"message":"private provider details"}]}`}
	for _, body := range cases {
		page, err := Discover(workflowJSON(t, body, 200), req("linear", "", 2))
		if err == nil || len(page.Items) != 0 || strings.Contains(err.Error(), "private") {
			t.Fatalf("false/unsafe GraphQL completion %v %+v", err, page)
		}
	}
	request := req("linear", "", 2)
	request.Page = 2
	request.Cursor = "same"
	if _, err := Discover(workflowJSON(t, `{"data":{"teams":{"nodes":[],"pageInfo":{"hasNextPage":true,"endCursor":"same"}}}}`, 200), request); err == nil {
		t.Fatal("repeated GraphQL cursor accepted")
	}
}
func TestSlackChannelsPreserveNativeIdentityAndNoFallback(t *testing.T) {
	client := workflowJSON(t, `{"ok":true,"channels":[{"id":"C123","name":"engineering","is_channel":true,"context_team_id":"T111"},{"id":"G456","name":"engineering","is_group":true,"is_private":true,"context_team_id":"T111"}],"response_metadata":{"next_cursor":"next+/="}}`, 200)
	page, err := Discover(client, req("slack", "", 2))
	if err != nil || len(page.Items) != 2 || page.Items[0].Namespace != "T111" || page.Items[0].NativeID != "C123" || page.Items[1].NativeID != "G456" || page.NextCursor != "next+/=" {
		t.Fatalf("channels %+v %v", page, err)
	}
	if client.calls[0].Path != "conversations.list" || client.calls[0].Query.Get("types") != "public_channel,private_channel" {
		t.Fatalf("unexpected Slack call %+v", client.calls)
	}
	denied := workflowJSON(t, `{"ok":false,"error":"missing_scope","needed":"channels:read","provided":"groups:read"}`, 200)
	if page, err := Discover(denied, req("slack", "", 2)); err == nil || len(page.Items) != 0 || len(denied.calls) != 1 {
		t.Fatalf("private-only fallback occurred %+v %v", denied.calls, err)
	}
	for _, body := range []string{`{"ok":true,"channels":[]}`, `{"ok":true,"channels":[],"response_metadata":{}}`, `{"ok":true,"channels":[],"response_metadata":{"next_cursor":""}}`} {
		if page, err := Discover(workflowJSON(t, body, 200), req("slack", "", 2)); err != nil || page.Coverage != "complete" {
			t.Fatalf("valid terminal Slack response %+v %v", page, err)
		}
	}
	for _, body := range []string{`{"ok":true,"channels":[{"id":"D123","name":"DM","is_im":true}]}`, `{"ok":true,"channels":[{"id":"C123","name":"channel","is_channel":true},{"id":"C123","name":"renamed","is_channel":true}]}`, `{"ok":true,"channels":[],"response_metadata":{"next_cursor":null}}`, `{"ok":true,"channels":null}`} {
		if _, err := Discover(workflowJSON(t, body, 200), req("slack", "", 2)); err == nil {
			t.Fatalf("accepted unsafe Slack response %s", body)
		}
	}
}
func TestWorkflowProviderBoundsAndCursorValidation(t *testing.T) {
	for _, provider := range []string{"gitlab", "pagerduty", "linear", "slack"} {
		if _, err := Discover(workflowJSON(t, strings.Repeat(" ", MaxResponseBytes+1), 200), req(provider, "", 2)); err != ErrLimit {
			t.Fatalf("%s body limit: %v", provider, err)
		}
	}
	for _, provider := range []string{"linear", "slack"} {
		client := workflowJSON(t, `{}`, 200)
		request := req(provider, "", 2)
		request.Page = 2
		if _, err := Discover(client, request); err != ErrRequest || len(client.calls) != 0 {
			t.Fatal("missing native continuation dispatched")
		}
		request.Cursor = "line\nbreak"
		if _, err := Discover(client, request); err != ErrRequest || len(client.calls) != 0 {
			t.Fatal("control-bearing native continuation dispatched")
		}
	}
	request := req("slack", "", 1)
	request.Page = 5
	request.Cursor = "last"
	page, err := Discover(workflowJSON(t, `{"ok":true,"channels":[],"response_metadata":{"next_cursor":"more"}}`, 200), request)
	if err != nil || page.NextPage != 0 || page.NextCursor != "" || page.Reason != "discovery_page_limit" {
		t.Fatalf("unbounded continuation %+v %v", page, err)
	}
	tooMany := `{"offset":0,"limit":1,"more":false,"services":[{"id":"P1","name":"A"},{"id":"P2","name":"B"}]}`
	if _, err := Discover(workflowJSON(t, tooMany, 200), req("pagerduty", "", 1)); err != ErrLimit {
		t.Fatalf("count limit: %v", err)
	}
}
