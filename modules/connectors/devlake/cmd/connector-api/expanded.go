package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/apache/devlake/adp/providers"
	lakeerrors "github.com/apache/devlake/core/errors"
	"github.com/apache/devlake/core/plugin"
)

func expandedProvider(provider string) bool {
	switch provider {
	case "sonarqube", "argocd", "gitlab", "pagerduty", "linear", "slack":
		return true
	}
	return false
}

type expandedProfileRequest struct {
	profileRequest
	Method string          `json:"method"`
	Path   string          `json:"path"`
	Query  url.Values      `json:"query"`
	Body   json.RawMessage `json:"body,omitempty"`
}

type expandedProfileClient struct {
	plugin.ApiClient
	broker *profileBroker
	ctx    context.Context
	work   profileWork
}

func (client *expandedProfileClient) Get(path string, query url.Values, headers http.Header) (*http.Response, lakeerrors.Error) {
	return client.call("GET", path, query, nil, headers)
}

func (client *expandedProfileClient) Post(path string, query url.Values, body interface{}, headers http.Header) (*http.Response, lakeerrors.Error) {
	payload, err := json.Marshal(body)
	if err != nil {
		return nil, lakeerrors.Convert(errInvalidPage)
	}
	return client.call("POST", path, query, payload, headers)
}

func (client *expandedProfileClient) call(method, path string, query url.Values, body json.RawMessage, headers http.Header) (*http.Response, lakeerrors.Error) {
	if len(headers) != 0 || len(path) > 2048 || len(query.Encode()) > 4096 || len(body) > 4096 {
		return nil, lakeerrors.Convert(errInvalidPage)
	}
	var result struct {
		ContractVersion int             `json:"contract_version"`
		Work            profileWork     `json:"work"`
		Data            json.RawMessage `json:"data"`
		Headers         http.Header     `json:"headers,omitempty"`
	}
	request := expandedProfileRequest{profileRequest: client.work.profileRequest, Method: method, Path: path, Query: query, Body: body}
	if err := client.broker.profileCall(client.ctx, "/internal/v1/profile-discovery/provider-page", request, &result); err != nil {
		return nil, lakeerrors.Convert(err)
	}
	if result.ContractVersion != 1 || result.Work != client.work || len(result.Data) == 0 {
		return nil, lakeerrors.Convert(errSourceUnavailable)
	}
	for name := range result.Headers {
		if name != "X-Next-Page" && name != "X-Page" && name != "X-Per-Page" && name != "X-Total-Pages" {
			return nil, lakeerrors.Convert(errSourceUnavailable)
		}
	}
	if result.Headers == nil {
		result.Headers = make(http.Header)
	}
	return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(bytes.NewReader(result.Data)), Header: result.Headers}, nil
}

func (broker *profileBroker) readExpanded(ctx context.Context, input profileJob) (discoveryPage, error) {
	if input.TenantID == "" || input.ProductID == "" || input.ProjectID == "" || input.OperationID == "" || input.AttemptID == "" || input.BindingID == "" || input.Limit != 100 || len(input.Cursor) > 512 {
		return discoveryPage{}, errInvalidPage
	}
	var work profileWork
	if err := broker.profileCall(ctx, "/internal/v1/profile-discovery/resolve", input.profileRequest, &work); err != nil {
		return discoveryPage{}, err
	}
	if work.profileRequest != input.profileRequest || work.Provider != input.Provider || work.Installation || work.ConnectionID == "" || len(work.ConnectionRevision) != 64 || work.BindingGeneration < 1 {
		return discoveryPage{}, errSourceUnavailable
	}
	client := &expandedProfileClient{broker: broker, ctx: ctx, work: work}
	namespace := work.Namespace
	if namespace == "/" || work.Provider == "slack" {
		namespace = ""
	} else if work.Provider == "argocd" {
		namespace = strings.SplitN(namespace, "/", 2)[0]
	}
	page, err := providerPage(client, work.Provider, false, namespace, input.Cursor, input.Limit, time.Now())
	if err != nil || len(page.Items) > input.Limit || len(page.NextCursor) > 512 {
		return discoveryPage{}, errSourceUnavailable
	}
	var current profileWork
	if err := broker.profileCall(ctx, "/internal/v1/profile-discovery/resolve", input.profileRequest, &current); err != nil || current != work {
		return discoveryPage{}, errSourceUnavailable
	}
	return page, nil
}

// expandedProviderPage adapts discovery to the existing connector contract.
// The caller owns connection/delegation checks and the overall deadline. These
// continuation values must stay inside its authorized durable operation.
func expandedProviderPage(client plugin.ApiClient, provider string, installation bool, namespace, cursor string, limit int, observedAt time.Time) (discoveryPage, error) {
	switch provider {
	case "sonarqube", "jenkins", "argocd", "gitlab", "pagerduty", "linear", "slack":
	default:
		return discoveryPage{}, errUnsupportedCapability
	}
	if installation {
		return discoveryPage{}, errUnsupportedCapability
	}
	request := providers.Request{Provider: provider, Namespace: namespace, Page: 1, Limit: limit, ObservedAt: observedAt}
	prefix := fmt.Sprintf("v1:%s:%d:", provider, limit)
	if cursor != "" {
		value, ok := strings.CutPrefix(cursor, prefix)
		parts := strings.SplitN(value, ":", 2)
		if !ok || len(cursor) > 8192 || len(parts) != 2 {
			return discoveryPage{}, errInvalidPage
		}
		page, err := strconv.Atoi(parts[0])
		if err != nil || page < 2 || page > 5 || strconv.Itoa(page) != parts[0] {
			return discoveryPage{}, errInvalidPage
		}
		native, err := url.QueryUnescape(parts[1])
		if err != nil {
			return discoveryPage{}, errInvalidPage
		}
		request.Page, request.Cursor = page, native
	}
	page, err := providers.Discover(client, request)
	if err != nil {
		if err == providers.ErrRequest {
			return discoveryPage{}, errInvalidPage
		}
		return discoveryPage{}, errSourceUnavailable
	}
	result := discoveryPage{ContractVersion: 1, Items: []discoveryItem{}, Coverage: page.Coverage, Reason: page.Reason}
	for _, scope := range page.Items {
		item := observedItem(scope.Kind, scope.Namespace, scope.NativeID, scope.Name, scope.ObservedAt)
		item.FullName = scope.FullName
		// A GitLab path rename is a changed observation even when its display
		// name and stable native ID remain unchanged.
		if scope.FullName != "" {
			item.SourceRevision = observedItem(scope.Kind, scope.Namespace, scope.NativeID, scope.Name+"\x00"+scope.FullName, scope.ObservedAt).SourceRevision
		}
		result.Items = append(result.Items, item)
	}
	if page.NextPage != 0 {
		result.NextCursor = prefix + strconv.Itoa(page.NextPage) + ":" + url.QueryEscape(page.NextCursor)
	}
	return result, nil
}
