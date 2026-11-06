package providers

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	lakeerrors "github.com/apache/devlake/core/errors"
	"github.com/apache/devlake/core/plugin"
)

var fixtureTime = time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)

type fixtureClient struct {
	plugin.ApiClient
	base   string
	client *http.Client
	calls  int
	path   string
	query  url.Values
}

func (c *fixtureClient) Get(path string, query url.Values, headers http.Header) (*http.Response, lakeerrors.Error) {
	c.calls++
	c.path = path
	c.query = query
	response, err := c.client.Get(c.base + "/" + path + "?" + query.Encode())
	return response, lakeerrors.Convert(err)
}
func fixture(t *testing.T, body string, status int) *fixtureClient {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}))
	t.Cleanup(server.Close)
	return &fixtureClient{base: server.URL, client: server.Client()}
}
func req(provider, namespace string, limit int) Request {
	return Request{Provider: provider, Namespace: namespace, Page: 1, Limit: limit, ObservedAt: fixtureTime}
}

func TestPinnedSonarDiscovery(t *testing.T) {
	client := fixture(t, `{"paging":{"pageIndex":1,"pageSize":2,"total":3},"components":[{"key":"team:api","name":"API"},{"key":"other:api","name":"API"}]}`, 200)
	page, err := Discover(client, req("sonarqube", "", 2))
	if err != nil {
		t.Fatal(err)
	}
	if client.path != "components/search_projects" || client.query.Get("p") != "1" || client.query.Get("ps") != "2" || client.calls != 1 {
		t.Fatalf("unexpected extracted request: %+v", client)
	}
	if page.Coverage != "partial" || page.NextPage != 2 || len(page.Items) != 2 || page.Items[0].NativeID != "other:api" || page.Items[1].NativeID != "team:api" {
		t.Fatalf("wrong project identity/page: %+v", page)
	}
	if page.Items[0].Kind != "quality_project" || page.Items[0].UpstreamRevision != UpstreamRevision || page.Items[0].ObservedAt != "2026-01-02T03:04:05Z" {
		t.Fatalf("missing provenance: %+v", page.Items[0])
	}
}
func TestJenkinsNestedIdentityAndRelativeContext(t *testing.T) {
	for _, folder := range []string{"team one", "team two"} {
		body := fmt.Sprintf(`{"jobs":[{"name":"release","fullName":%q,"_class":"hudson.model.FreeStyleProject","url":"https://attacker.invalid/"},{"name":"nested","fullName":%q,"jobs":[]}]}`, folder+"/release", folder+"/nested")
		client := fixture(t, body, 200)
		page, err := Discover(client, req("jenkins", folder, 3))
		if err != nil {
			t.Fatal(err)
		}
		if client.path != "job/"+url.PathEscape(folder)+"/api/json" || client.query.Get("tree") != "jobs[fullName,name,class,url,color,base,jobs,upstreamProjects[name]]{0,3}" {
			t.Fatalf("unexpected upstream request %q %v", client.path, client.query)
		}
		if len(page.Items) != 2 || page.Items[0].NativeID != folder+"/release" || page.Items[1].NativeID != folder+"/nested" || page.Items[1].Kind != "namespace" {
			t.Fatalf("lost folder identity %+v", page)
		}
	}
}
func TestArgoIdentityStableOrderAndPartial(t *testing.T) {
	client := fixture(t, `{"items":[{"metadata":{"name":"api","namespace":"beta"},"spec":{"project":"team"}},{"metadata":{"name":"api","namespace":"alpha"},"spec":{"project":"team"}},{"metadata":{"name":"other","namespace":"alpha"},"spec":{"project":"elsewhere"}}]}`, 200)
	page, err := Discover(client, req("argocd", "team", 1))
	if err != nil {
		t.Fatal(err)
	}
	if client.path != "applications" || page.Coverage != "partial" || page.Reason != "unpaginated_provider_limit" || page.NextPage != 0 || len(page.Items) != 1 || page.Items[0].Namespace != "team/alpha" || page.Items[0].NativeID != "api" {
		t.Fatalf("invalid unpaginated result %+v", page)
	}
	for i := 0; i < 8; i++ {
		groups, err := Discover(fixture(t, `{"items":[{"metadata":{"name":"a","namespace":"n"},"spec":{"project":"z"}},{"metadata":{"name":"b","namespace":"n"},"spec":{}}]}`, 200), req("argocd", "", 100))
		if err != nil || len(groups.Items) != 2 || groups.Items[0].NativeID != "default" || groups.Items[1].NativeID != "z" {
			t.Fatalf("nondeterministic/default projects %+v %v", groups, err)
		}
	}
	fallback, err := Discover(fixture(t, `{"items":[{"metadata":{"name":"api","namespace":"ns"},"spec":{}}]}`, 200), req("argocd", "default", 100))
	if err != nil || fallback.Items[0].Namespace != "default/ns" {
		t.Fatalf("default project identity %+v %v", fallback, err)
	}
}
func TestArgoEmptyProtobufLists(t *testing.T) {
	for _, body := range []string{`{}`, `{"items":null}`, `{"items":[]}`, `{"metadata":{},"items":null}`} {
		result, err := Discover(fixture(t, body, 200), req("argocd", "", 10))
		if err != nil || len(result.Items) != 0 || result.Coverage != "complete" {
			t.Fatalf("empty list %s: %+v %v", body, result, err)
		}
	}
}
func TestMalformedProviderResponsesFailClosed(t *testing.T) {
	cases := []struct {
		name, provider, namespace, body string
		status                          int
	}{
		{"sonar null", "sonarqube", "", `{"paging":{"pageIndex":1,"pageSize":2,"total":1},"components":[null]}`, 200},
		{"sonar malformed", "sonarqube", "", `{"components":[`, 200},
		{"sonar wrong page", "sonarqube", "", `{"paging":{"pageIndex":0,"pageSize":2,"total":1},"components":[{"key":"k","name":"K"}]}`, 200},
		{"sonar duplicate", "sonarqube", "", `{"paging":{"pageIndex":1,"pageSize":2,"total":2},"components":[{"key":"k","name":"K"},{"key":"k","name":"K"}]}`, 200},
		{"jenkins foreign fullname", "jenkins", "team", `{"jobs":[{"name":"api","fullName":"other/api"}]}`, 200},
		{"jenkins traversal", "jenkins", "team", `{"jobs":[{"name":"../api","fullName":"team/../api"}]}`, 200},
		{"argo missing namespace", "argocd", "team", `{"items":[{"metadata":{"name":"a"},"spec":{"project":"team"}}]}`, 200},
		{"argo duplicate", "argocd", "team", `{"items":[{"metadata":{"name":"a","namespace":"n"},"spec":{"project":"team"}},{"metadata":{"name":"a","namespace":"n"},"spec":{"project":"team"}}]}`, 200},
		{"argo error envelope", "argocd", "", `{"error":"not authorized"}`, 200},
		{"jenkins server error", "jenkins", "", `{"jobs":[]}`, 500},
		{"source auth", "sonarqube", "", `{"components":[]}`, 403},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			result, err := Discover(fixture(t, item.body, item.status), req(item.provider, item.namespace, 2))
			if err == nil || len(result.Items) != 0 {
				t.Fatalf("accepted invalid response: %+v", result)
			}
		})
	}
}
func TestBoundsAndInvalidRequests(t *testing.T) {
	client := fixture(t, strings.Repeat(" ", MaxResponseBytes+1), 200)
	if _, err := Discover(client, req("argocd", "", 100)); err != ErrLimit {
		t.Fatalf("body limit %v", err)
	}
	apps := make([]map[string]any, MaxUnpaginatedItems+1)
	for i := range apps {
		apps[i] = map[string]any{"metadata": map[string]string{"name": fmt.Sprint(i), "namespace": "n"}, "spec": map[string]string{"project": "p"}}
	}
	body, _ := json.Marshal(map[string]any{"items": apps})
	if _, err := Discover(fixture(t, string(body), 200), req("argocd", "p", 100)); err != ErrLimit {
		t.Fatalf("count limit %v", err)
	}
	for _, namespace := range []string{"../api", "team//api", "https://example.test/api", "team/..", "team\\api", "/team"} {
		client := fixture(t, `{"jobs":[]}`, 200)
		if _, err := Discover(client, req("jenkins", namespace, 2)); err != ErrRequest || client.calls != 0 {
			t.Fatalf("invalid path dispatched %q", namespace)
		}
	}
	invalid := req("argocd", "", 100)
	invalid.Page = 2
	client = fixture(t, `{"items":[]}`, 200)
	if _, err := Discover(client, invalid); err != ErrRequest || client.calls != 0 {
		t.Fatal("invented Argo pagination")
	}
	request := req("jenkins", "feature%2Fone", 1)
	client = fixture(t, `{"jobs":[{"name":"a","fullName":"feature%2Fone/a"}]}`, 200)
	if _, err := Discover(client, request); err != nil || client.path != "job/feature%252Fone/api/json" {
		t.Fatalf("encoded segment changed: %q %v", client.path, err)
	}
}
func TestFifthPageCannotEscapeBound(t *testing.T) {
	request := req("jenkins", "", 1)
	request.Page = 5
	result, err := Discover(fixture(t, `{"jobs":[{"name":"x","fullName":"x"}]}`, 200), request)
	if err != nil || result.NextPage != 0 || result.Coverage != "partial" || result.Reason != "discovery_page_limit" {
		t.Fatalf("bad final page %+v %v", result, err)
	}
}
