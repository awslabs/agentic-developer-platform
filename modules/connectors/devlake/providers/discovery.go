// Package providers adapts pinned DevLake discovery helpers without owning
// connections, credentials, authorization, durable cursors or a database.
package providers

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
	"unicode"

	lakeerrors "github.com/apache/devlake/core/errors"
	"github.com/apache/devlake/core/plugin"
	argo "github.com/apache/devlake/plugins/argocd/api"
	jenkins "github.com/apache/devlake/plugins/jenkins/api"
	sonar "github.com/apache/devlake/plugins/sonarqube/api"
)

const UpstreamRevision = "6502e549f58eb40c40ccbcd39aab9bc8eea6616f"
const MaxResponseBytes = 1 << 20
const MaxUnpaginatedItems = 1000

var ErrRequest = errors.New("invalid_provider_request")
var ErrSource = errors.New("provider_source_unavailable")
var ErrLimit = errors.New("provider_response_limit_exceeded")
var ErrIdentity = errors.New("invalid_provider_identity")

// Request describes one bounded read. Namespace is a Jenkins parent full name,
// an Argo project, or a GitLab group ID / explicit user:<ID>. Other providers
// use an empty namespace. Page counts reads in the bounded discovery operation;
// GitLab stage/native-page and Linear/Slack cursors use Cursor. Neither is an
// authorization cursor. The caller must bind every read to current authority.
type Request struct {
	Provider   string
	Namespace  string
	Page       int
	Limit      int
	ObservedAt time.Time
	// Cursor is native provider state only, never an authorized ADP cursor.
	Cursor string
}

type Scope struct {
	Provider         string `json:"provider"`
	Kind             string `json:"kind"`
	Namespace        string `json:"namespace"`
	NativeID         string `json:"native_id"`
	Name             string `json:"name"`
	FullName         string `json:"full_name,omitempty"`
	ObservedAt       string `json:"observed_at"`
	UpstreamRevision string `json:"upstream_revision"`
}

type Page struct {
	Items      []Scope `json:"items"`
	NextPage   int     `json:"next_page,omitempty"`
	NextCursor string  `json:"next_cursor,omitempty"`
	Coverage   string  `json:"coverage"`
	Reason     string  `json:"reason,omitempty"`
}

// Discover calls the actual extracted upstream helper using the injected client.
// The caller supplies client deadlines and current authority; this function does
// not construct a network client, accept an endpoint or persist secrets.
func Discover(client plugin.ApiClient, request Request) (Page, error) {
	empty := Page{Items: []Scope{}, Coverage: "unsupported"}
	if client == nil || request.Page < 1 || request.Page > 5 || request.Limit < 1 || request.Limit > 100 || request.ObservedAt.IsZero() {
		return empty, ErrRequest
	}
	if workflowProvider(request.Provider) {
		return discoverWorkflow(client, request)
	}
	if request.Cursor != "" {
		return empty, ErrRequest
	}
	if request.Provider != "sonarqube" && request.Provider != "jenkins" && request.Provider != "argocd" {
		return empty, ErrRequest
	}
	if request.Provider == "sonarqube" && request.Namespace != "" || request.Provider == "argocd" && (request.Page != 1 || !safeName(request.Namespace, true)) || request.Provider == "jenkins" && !safeFullName(request.Namespace, true) {
		return empty, ErrRequest
	}
	bounded := &readClient{ApiClient: client, request: request}
	result := Page{Items: []Scope{}, Coverage: "complete"}
	appendScope := func(kind, namespace, id, name string) error {
		if id == "" || name == "" || len(id) > 2048 || len(namespace) > 2048 || strings.IndexFunc(id+namespace+name, unicode.IsControl) != -1 {
			return ErrIdentity
		}
		result.Items = append(result.Items, Scope{Provider: request.Provider, Kind: kind, Namespace: namespace, NativeID: id, Name: name, ObservedAt: request.ObservedAt.UTC().Format(time.RFC3339Nano), UpstreamRevision: UpstreamRevision})
		return nil
	}
	switch request.Provider {
	case "sonarqube":
		children, next, err := sonar.ADPListRemoteScopes(bounded, "", sonar.SonarqubeRemotePagination{Page: request.Page, PageSize: request.Limit})
		if err != nil {
			return empty, bounded.failure()
		}
		for _, child := range children {
			if err := appendScope("quality_project", "", child.Id, child.Name); err != nil {
				return empty, err
			}
		}
		if next != nil {
			if next.Page != request.Page+1 || next.PageSize != request.Limit {
				return empty, ErrSource
			}
			result.NextPage = next.Page
		}
	case "jenkins":
		group := jenkinsPath(request.Namespace)
		children, next, err := jenkins.ADPListRemoteScopes(bounded, group, jenkins.JenkinsRemotePagination{Page: request.Page, PerPage: request.Limit})
		if err != nil {
			return empty, bounded.failure()
		}
		for _, child := range children {
			if !safeName(child.Name, false) {
				return empty, ErrIdentity
			}
			fullName := child.Name
			if request.Namespace != "" {
				fullName = request.Namespace + "/" + child.Name
			}
			kind := "namespace"
			if child.Data != nil {
				kind = "ci_job"
				if child.Data.FullName != fullName {
					return empty, ErrIdentity
				}
			}
			if err := appendScope(kind, request.Namespace, fullName, child.Name); err != nil {
				return empty, err
			}
		}
		if next != nil {
			if next.Page != request.Page+1 || next.PerPage != request.Limit {
				return empty, ErrSource
			}
			result.NextPage = next.Page
		}
	case "argocd":
		children, _, err := argo.ADPListRemoteScopes(bounded, request.Namespace, argo.ArgocdRemotePagination{Page: 1})
		if err != nil {
			return empty, bounded.failure()
		}
		for _, child := range children {
			if !safeName(child.Name, false) {
				return empty, ErrIdentity
			}
			kind, namespace, id := "namespace", "", child.Id
			if child.Data != nil {
				kind = "deployment_application"
				project := child.Data.Project
				if project == "" {
					project = "default"
				}
				if project != request.Namespace || !safeName(project, false) || !safeName(child.Data.Namespace, false) {
					return empty, ErrIdentity
				}
				namespace = project + "/" + child.Data.Namespace
				id = child.Data.Name
			}
			if err := appendScope(kind, namespace, id, child.Name); err != nil {
				return empty, err
			}
		}
	}
	sort.Slice(result.Items, func(i, j int) bool {
		a, b := result.Items[i], result.Items[j]
		return a.Kind+"\x00"+a.Namespace+"\x00"+a.NativeID < b.Kind+"\x00"+b.Namespace+"\x00"+b.NativeID
	})
	for i := 1; i < len(result.Items); i++ {
		a, b := result.Items[i-1], result.Items[i]
		if a.Kind == b.Kind && a.Namespace == b.Namespace && a.NativeID == b.NativeID {
			return empty, ErrIdentity
		}
	}
	if len(result.Items) > request.Limit {
		if request.Provider != "argocd" {
			return empty, ErrLimit
		}
		result.Items = result.Items[:request.Limit]
		result.Coverage = "partial"
		result.Reason = "unpaginated_provider_limit"
	}
	if result.NextPage != 0 {
		result.Coverage = "partial"
		if request.Page == 5 {
			result.NextPage = 0
			result.Reason = "discovery_page_limit"
		}
	}
	return result, nil
}

func safeName(value string, allowEmpty bool) bool {
	return (allowEmpty || value != "") && value != "." && value != ".." && len(value) <= 1024 && !strings.ContainsAny(value, "/\\?#") && strings.IndexFunc(value, unicode.IsControl) == -1
}
func safeFullName(value string, allowEmpty bool) bool {
	if value == "" {
		return allowEmpty
	}
	if len(value) > 2048 || strings.Contains(value, "://") {
		return false
	}
	for _, part := range strings.Split(value, "/") {
		if !safeName(part, false) {
			return false
		}
	}
	return true
}
func jenkinsPath(fullName string) string {
	if fullName == "" {
		return ""
	}
	var result string
	for _, part := range strings.Split(fullName, "/") {
		result += "/job/" + url.PathEscape(part)
	}
	return result
}

type readClient struct {
	plugin.ApiClient
	request  Request
	rejected error
	calls    int
}

func (c *readClient) failure() error {
	if c.rejected != nil {
		return c.rejected
	}
	return ErrSource
}
func (c *readClient) fail(err error) (*http.Response, lakeerrors.Error) {
	c.rejected = err
	return nil, lakeerrors.Convert(err)
}
func (c *readClient) Get(path string, query url.Values, headers http.Header) (*http.Response, lakeerrors.Error) {
	c.calls++
	expected := map[string]string{"sonarqube": "components/search_projects", "argocd": "applications", "jenkins": jenkinsPath(c.request.Namespace) + "/api/json"}[c.request.Provider]
	if c.calls != 1 || path != expected || len(headers) != 0 {
		return c.fail(ErrRequest)
	}
	// Jenkins' upstream helper uses a root-relative path; the broker client receives
	// a relative path, preserving the registered server's context path.
	response, err := c.ApiClient.Get(strings.TrimPrefix(path, "/"), query, nil)
	return c.consume(response, err, func(body []byte, _ http.Header) error {
		return validateEnvelope(body, c.request)
	})
}

// consume is shared by REST discovery and Linear's fixed read-only GraphQL POST.
func (c *readClient) consume(response *http.Response, err lakeerrors.Error, validate func([]byte, http.Header) error) (*http.Response, lakeerrors.Error) {
	if err != nil {
		if response != nil && response.Body != nil {
			response.Body.Close()
		}
		return c.fail(ErrSource)
	}
	if response == nil || response.Body == nil {
		return c.fail(ErrSource)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return c.fail(ErrSource)
	}
	body, readErr := io.ReadAll(io.LimitReader(response.Body, MaxResponseBytes+1))
	if readErr != nil {
		return c.fail(ErrSource)
	}
	if len(body) > MaxResponseBytes {
		return c.fail(ErrLimit)
	}
	if err := validate(body, response.Header); err != nil {
		return c.fail(err)
	}
	clone := *response
	clone.Body = io.NopCloser(bytes.NewReader(body))
	return &clone, nil
}

func validateEnvelope(body []byte, request Request) error {
	var envelope map[string]json.RawMessage
	if json.Unmarshal(body, &envelope) != nil || envelope == nil {
		return ErrSource
	}
	field := map[string]string{"sonarqube": "components", "jenkins": "jobs", "argocd": "items"}[request.Provider]
	var entries []json.RawMessage
	raw, ok := envelope[field]
	if request.Provider == "argocd" && (!ok || bytes.Equal(raw, []byte("null"))) {
		// Protobuf JSON omits an empty repeated field. Refuse other error-shaped
		// envelopes instead of declaring their content an empty application list.
		for key := range envelope {
			if key != "items" && key != "metadata" {
				return ErrSource
			}
		}
		return nil
	}
	if !ok || bytes.Equal(raw, []byte("null")) || json.Unmarshal(raw, &entries) != nil {
		return ErrSource
	}
	limit := request.Limit
	if request.Provider == "argocd" {
		limit = MaxUnpaginatedItems
	}
	if len(entries) > limit {
		return ErrLimit
	}
	for _, entry := range entries {
		if bytes.Equal(entry, []byte("null")) || len(entry) == 0 || entry[0] != '{' {
			return ErrSource
		}
	}
	if request.Provider == "sonarqube" {
		var paging struct {
			Page  int `json:"pageIndex"`
			Size  int `json:"pageSize"`
			Total int `json:"total"`
		}
		if json.Unmarshal(envelope["paging"], &paging) != nil || paging.Page != request.Page || paging.Size != request.Limit || paging.Total < 0 || paging.Total < (request.Page-1)*request.Limit+len(entries) || paging.Total > request.Page*request.Limit && len(entries) != request.Limit {
			return ErrSource
		}
	}
	return nil
}

// IdentityKey is an unambiguous native identity within the caller's connection.
// Tenant/connection identity must be added by the serving boundary.
func (s Scope) IdentityKey() string {
	return fmt.Sprintf("%s\x00%s\x00%s\x00%s", s.Provider, s.Kind, s.Namespace, s.NativeID)
}
