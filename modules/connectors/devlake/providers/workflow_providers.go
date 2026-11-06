package providers

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"unicode"

	lakeerrors "github.com/apache/devlake/core/errors"
	"github.com/apache/devlake/core/plugin"
	gitlab "github.com/apache/devlake/plugins/gitlab/api"
	linear "github.com/apache/devlake/plugins/linear/api"
	pagerduty "github.com/apache/devlake/plugins/pagerduty/api"
	slack "github.com/apache/devlake/plugins/slack/api"
)

func workflowProvider(provider string) bool {
	return provider == "gitlab" || provider == "pagerduty" || provider == "linear" || provider == "slack"
}

func discoverWorkflow(client plugin.ApiClient, request Request) (Page, error) {
	empty := Page{Items: []Scope{}, Coverage: "unsupported"}
	bounded := &workflowClient{readClient: readClient{ApiClient: client, request: request}}
	if request.Provider == "gitlab" {
		if err := bounded.gitlabStage(); err != nil {
			return empty, err
		}
	} else {
		if request.Namespace != "" {
			return empty, ErrRequest
		}
		if request.Provider == "pagerduty" && request.Cursor != "" {
			return empty, ErrRequest
		}
		if request.Provider == "slack" || request.Provider == "linear" {
			if !safeCursor(request.Cursor, true) || request.Page == 1 && request.Cursor != "" || request.Page > 1 && request.Cursor == "" {
				return empty, ErrRequest
			}
		}
	}
	result := Page{Items: []Scope{}, Coverage: "complete"}
	appendScope := func(kind, namespace, id, name string) error {
		if !nativeIdentifier(id) || len(namespace) > 2048 || strings.IndexFunc(namespace+name, unicode.IsControl) >= 0 || name == "" || len(name) > 1024 {
			return ErrIdentity
		}
		result.Items = append(result.Items, Scope{Provider: request.Provider, Kind: kind, Namespace: namespace, NativeID: id, Name: name, ObservedAt: request.ObservedAt.UTC().Format("2006-01-02T15:04:05.999999999Z07:00"), UpstreamRevision: UpstreamRevision})
		return nil
	}
	switch request.Provider {
	case "gitlab":
		page := gitlab.GitlabRemotePagination{Page: bounded.nativePage, PerPage: request.Limit, Step: bounded.stage}
		// Both helpers run one GET; the ADP path never calls the legacy combined
		// helper, which can return groups and projects in an overfull single page.
		if bounded.stage == "groups" {
			children, next, err := gitlab.ADPListRemoteGroups(bounded, request.Namespace, page)
			if err != nil {
				return empty, bounded.failure()
			}
			for _, child := range children {
				if !positiveDecimal(child.Id) || !safeFullName(child.FullName, false) {
					return empty, ErrIdentity
				}
				if err := appendScope("namespace", request.Namespace, child.Id, child.Name); err != nil {
					return empty, err
				}
				result.Items[len(result.Items)-1].FullName = child.FullName
			}
			if next != nil {
				result.NextCursor = "groups:" + strconv.Itoa(next.Page)
			} else if request.Namespace != "" {
				result.NextCursor = "projects:1"
			}
			if request.Namespace == "" {
				result.Reason = "group_namespaces_only"
			}
		} else {
			children, next, err := gitlab.ADPListRemoteProjects(bounded, request.Namespace, page)
			if err != nil {
				return empty, bounded.failure()
			}
			for _, child := range children {
				if !positiveDecimal(child.Id) || child.ParentId == nil || *child.ParentId != request.Namespace || child.Data == nil || !safeFullName(child.FullName, false) {
					return empty, ErrIdentity
				}
				if err := appendScope("repository", request.Namespace, child.Id, child.Name); err != nil {
					return empty, err
				}
				result.Items[len(result.Items)-1].FullName = child.FullName
			}
			if next != nil {
				result.NextCursor = "projects:" + strconv.Itoa(next.Page)
			}
		}
	case "pagerduty":
		offset := (request.Page - 1) * request.Limit
		children, next, err := pagerduty.ADPListRemoteScopes(bounded, pagerduty.PagerdutyRemotePagination{Offset: offset, Limit: request.Limit})
		if err != nil {
			return empty, bounded.failure()
		}
		for _, child := range children {
			if err := appendScope("incident_service", "", child.Id, child.Name); err != nil {
				return empty, err
			}
		}
		if next != nil {
			if next.Offset != offset+request.Limit || next.Limit != request.Limit {
				return empty, ErrSource
			}
			result.NextPage = request.Page + 1
		}
	case "linear":
		children, next, err := linear.ADPListRemoteScopes(bounded, linear.LinearRemotePagination{Cursor: request.Cursor, Limit: request.Limit})
		if err != nil {
			return empty, bounded.failure()
		}
		for _, child := range children {
			if err := appendScope("tracker_team", "", child.Id, child.Name); err != nil {
				return empty, err
			}
		}
		if next != nil {
			result.NextCursor = next.Cursor
		}
	case "slack":
		children, next, err := slack.ADPListRemoteScopes(bounded, slack.SlackRemotePagination{Cursor: request.Cursor, Limit: request.Limit})
		if err != nil {
			return empty, bounded.failure()
		}
		for _, child := range children {
			if child.Data == nil || child.Data.IsIm || child.Data.IsMpim || (!child.Data.IsChannel && !child.Data.IsGroup) || !slackChannelID(child.Id) || child.Data.ContextTeamId != "" && !nativeIdentifier(child.Data.ContextTeamId) {
				return empty, ErrIdentity
			}
			if err := appendScope("communication_channel", child.Data.ContextTeamId, child.Id, child.Name); err != nil {
				return empty, err
			}
		}
		if next != nil {
			if next.Limit != request.Limit {
				return empty, ErrSource
			}
			result.NextCursor = next.Cursor
		}
	}
	if len(result.Items) > request.Limit {
		return empty, ErrLimit
	}
	sort.Slice(result.Items, func(i, j int) bool { return result.Items[i].IdentityKey() < result.Items[j].IdentityKey() })
	for i := 1; i < len(result.Items); i++ {
		if result.Items[i-1].IdentityKey() == result.Items[i].IdentityKey() {
			return empty, ErrIdentity
		}
	}
	if result.NextCursor != "" {
		result.NextPage = request.Page + 1
	}
	if result.NextPage != 0 {
		result.Coverage = "partial"
		if request.Page == 5 {
			result.NextPage = 0
			result.NextCursor = ""
			result.Reason = "discovery_page_limit"
		}
	}
	return result, nil
}

func nativeIdentifier(value string) bool {
	if value == "" || len(value) > 128 {
		return false
	}
	for _, r := range value {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_') {
			return false
		}
	}
	return true
}
func positiveDecimal(value string) bool {
	number, err := strconv.ParseInt(value, 10, 64)
	return err == nil && number > 0 && strconv.FormatInt(number, 10) == value
}
func safeCursor(value string, empty bool) bool {
	if value == "" {
		return empty
	}
	if len(value) > 2048 {
		return false
	}
	for _, r := range value {
		if r < 33 || r > 126 {
			return false
		}
	}
	return true
}
func slackChannelID(value string) bool {
	return nativeIdentifier(value) && len(value) > 1 && (value[0] == 'C' || value[0] == 'G')
}

type workflowClient struct {
	readClient
	stage      string
	nativePage int
}

func (c *workflowClient) gitlabStage() error {
	namespace := c.request.Namespace
	personal := strings.HasPrefix(namespace, "user:")
	if namespace != "" && !positiveDecimal(strings.TrimPrefix(namespace, "user:")) {
		return ErrRequest
	}
	if c.request.Cursor == "" {
		if c.request.Page != 1 {
			return ErrRequest
		}
		c.stage, c.nativePage = "groups", 1
		if personal {
			c.stage = "projects"
		}
		return nil
	}
	if c.request.Page == 1 {
		return ErrRequest
	}
	stage, number, ok := strings.Cut(c.request.Cursor, ":")
	nativePage, err := strconv.Atoi(number)
	if !ok || err != nil || nativePage < 1 || nativePage > c.request.Page || strconv.Itoa(nativePage) != number || (stage != "groups" && stage != "projects") || personal && stage != "projects" || namespace == "" && stage != "groups" {
		return ErrRequest
	}
	c.stage, c.nativePage = stage, nativePage
	return nil
}
func (c *workflowClient) expectedGET() (string, url.Values) {
	q := url.Values{}
	switch c.request.Provider {
	case "gitlab":
		q.Set("page", strconv.Itoa(c.nativePage))
		q.Set("per_page", strconv.Itoa(c.request.Limit))
		if c.stage == "groups" {
			if c.request.Namespace == "" {
				q.Set("top_level_only", "true")
				return "groups", q
			}
			return "groups/" + c.request.Namespace + "/subgroups", q
		}
		q.Set("archived", "false")
		q.Set("min_access_level", "20")
		if strings.HasPrefix(c.request.Namespace, "user:") {
			return "users/" + strings.TrimPrefix(c.request.Namespace, "user:") + "/projects", q
		}
		q.Set("with_shared", "false")
		q.Set("include_subgroups", "false")
		return "groups/" + c.request.Namespace + "/projects", q
	case "pagerduty":
		q.Set("offset", strconv.Itoa((c.request.Page-1)*c.request.Limit))
		q.Set("limit", strconv.Itoa(c.request.Limit))
		q.Set("query", "")
		return "/services", q
	case "slack":
		q.Set("limit", strconv.Itoa(c.request.Limit))
		q.Set("types", "public_channel,private_channel")
		if c.request.Cursor != "" {
			q.Set("cursor", c.request.Cursor)
		}
		return "conversations.list", q
	}
	return "", nil
}
func (c *workflowClient) Get(path string, query url.Values, headers http.Header) (*http.Response, lakeerrors.Error) {
	c.calls++
	expected, q := c.expectedGET()
	if c.request.Provider == "linear" || c.calls != 1 || path != expected || query.Encode() != q.Encode() || len(headers) != 0 {
		return c.fail(ErrRequest)
	}
	response, err := c.ApiClient.Get(strings.TrimPrefix(path, "/"), query, nil)
	return c.consume(response, err, c.validate)
}
func linearQuery(limit int, cursor string) string {
	after := ""
	if cursor != "" {
		after = fmt.Sprintf(", after: %q", cursor)
	}
	return fmt.Sprintf("query { teams(first: %d%s) { nodes { id name key description } pageInfo { hasNextPage endCursor } } }", limit, after)
}
func (c *workflowClient) Post(path string, query url.Values, body interface{}, headers http.Header) (*http.Response, lakeerrors.Error) {
	c.calls++
	payload, ok := body.(map[string]interface{})
	if c.request.Provider != "linear" || c.calls != 1 || path != "" || len(query) != 0 || len(headers) != 0 || !ok || len(payload) != 1 || payload["query"] != linearQuery(c.request.Limit, c.request.Cursor) {
		return c.fail(ErrRequest)
	}
	response, err := c.ApiClient.Post("", nil, body, nil)
	return c.consume(response, err, c.validate)
}
func object(raw json.RawMessage) (map[string]json.RawMessage, error) {
	var value map[string]json.RawMessage
	if json.Unmarshal(raw, &value) != nil || value == nil {
		return nil, ErrSource
	}
	return value, nil
}
func records(raw json.RawMessage, limit int) ([]json.RawMessage, error) {
	var values []json.RawMessage
	if len(raw) == 0 || bytes.Equal(bytes.TrimSpace(raw), []byte("null")) || json.Unmarshal(raw, &values) != nil {
		return nil, ErrSource
	}
	if len(values) > limit {
		return nil, ErrLimit
	}
	for _, value := range values {
		if _, err := object(value); err != nil {
			return nil, err
		}
	}
	return values, nil
}
func (c *workflowClient) validate(body []byte, headers http.Header) error {
	if c.request.Provider == "gitlab" {
		return c.validateGitlab(body, headers)
	}
	envelope, err := object(body)
	if err != nil {
		return err
	}
	switch c.request.Provider {
	case "pagerduty":
		rows, err := records(envelope["services"], c.request.Limit)
		if err != nil {
			return err
		}
		var paging struct {
			Offset *int  `json:"offset"`
			Limit  *int  `json:"limit"`
			More   *bool `json:"more"`
		}
		if json.Unmarshal(body, &paging) != nil || paging.Offset == nil || paging.Limit == nil || paging.More == nil || *paging.Offset != (c.request.Page-1)*c.request.Limit || *paging.Limit != c.request.Limit || *paging.More && len(rows) == 0 {
			return ErrSource
		}
	case "linear":
		if raw, ok := envelope["errors"]; ok && !bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
			items, err := records(raw, 100)
			if err != nil || len(items) > 0 {
				return ErrSource
			}
		}
		data, err := object(envelope["data"])
		if err != nil {
			return err
		}
		teams, err := object(data["teams"])
		if err != nil {
			return err
		}
		if _, err := records(teams["nodes"], c.request.Limit); err != nil {
			return err
		}
		var info struct {
			Next   *bool   `json:"hasNextPage"`
			Cursor *string `json:"endCursor"`
		}
		if json.Unmarshal(teams["pageInfo"], &info) != nil || info.Next == nil {
			return ErrSource
		}
		if *info.Next && (info.Cursor == nil || !safeCursor(*info.Cursor, false) || *info.Cursor == c.request.Cursor) {
			return ErrSource
		}
	case "slack":
		var ok *bool
		if json.Unmarshal(envelope["ok"], &ok) != nil || ok == nil || !*ok {
			return ErrSource
		}
		if _, err := records(envelope["channels"], c.request.Limit); err != nil {
			return err
		}
		if raw, exists := envelope["response_metadata"]; exists {
			metadata, err := object(raw)
			if err != nil {
				return err
			}
			if raw, exists := metadata["next_cursor"]; exists {
				var cursor *string
				if json.Unmarshal(raw, &cursor) != nil || cursor == nil || !safeCursor(*cursor, true) || *cursor != "" && *cursor == c.request.Cursor {
					return ErrSource
				}
			}
		}
	}
	return nil
}
func (c *workflowClient) validateGitlab(body []byte, headers http.Header) error {
	rows, err := records(body, c.request.Limit)
	if err != nil {
		return err
	}
	for _, row := range rows {
		var item struct {
			ID        int64  `json:"id"`
			Name      string `json:"name"`
			ParentID  *int64 `json:"parent_id"`
			Namespace struct {
				ID   int64  `json:"id"`
				Kind string `json:"kind"`
			} `json:"namespace"`
			Owner struct {
				ID int64 `json:"id"`
			} `json:"owner"`
		}
		if json.Unmarshal(row, &item) != nil || item.ID <= 0 || item.Name == "" {
			return ErrIdentity
		}
		if c.stage == "groups" {
			if c.request.Namespace == "" {
				if item.ParentID != nil {
					return ErrIdentity
				}
			} else if item.ParentID == nil || strconv.FormatInt(*item.ParentID, 10) != c.request.Namespace {
				return ErrIdentity
			}
		} else {
			parent := ""
			if item.Namespace.Kind == "group" && item.Namespace.ID > 0 {
				parent = strconv.FormatInt(item.Namespace.ID, 10)
			} else if item.Namespace.Kind == "user" && item.Owner.ID > 0 {
				parent = "user:" + strconv.FormatInt(item.Owner.ID, 10)
			}
			if parent != c.request.Namespace {
				return ErrIdentity
			}
		}
	}
	for field, expected := range map[string]int{"X-Page": c.nativePage, "X-Per-Page": c.request.Limit} {
		if value := headers.Get(field); value != "" && value != strconv.Itoa(expected) {
			return ErrSource
		}
	}
	next := headers.Get("X-Next-Page")
	if next != "" && (next != strconv.Itoa(c.nativePage+1) || len(rows) == 0) {
		return ErrSource
	}
	if value := headers.Get("X-Total-Pages"); value != "" {
		total, err := strconv.Atoi(value)
		if err != nil || total < 0 || strconv.Itoa(total) != value || total > c.nativePage && next == "" || total <= c.nativePage && next != "" {
			return ErrSource
		}
	}
	return nil
}
