package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	lakeerrors "github.com/apache/devlake/core/errors"
	"github.com/apache/devlake/core/plugin"
)

type profileWork struct {
	profileRequest
	BindingGeneration  int    `json:"binding_generation"`
	ConnectionID       string `json:"connection_id"`
	ConnectionRevision string `json:"connection_revision"`
	Provider           string `json:"provider"`
	Installation       bool   `json:"installation"`
	Namespace          string `json:"namespace"`
}

func (broker *profileBroker) profileCall(ctx context.Context, route string, body any, output any) error {
	payload, err := json.Marshal(body)
	if err != nil {
		return errSourceUnavailable
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, broker.address+route, bytes.NewReader(payload))
	if err != nil {
		return errSourceUnavailable
	}
	request.Header.Set("Content-Type", "application/json")
	if broker.sign == nil || broker.sign(request, payload) != nil {
		return errSourceUnavailable
	}
	response, err := broker.client.Do(request)
	if err != nil {
		return errSourceUnavailable
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return errSourceUnavailable
	}
	content, err := io.ReadAll(io.LimitReader(response.Body, 1024*1024+8193))
	if err != nil || len(content) > 1024*1024+8192 {
		return errSourceUnavailable
	}
	decoder := json.NewDecoder(bytes.NewReader(content))
	decoder.DisallowUnknownFields()
	if decoder.Decode(output) != nil || decoder.Decode(new(any)) != io.EOF {
		return errSourceUnavailable
	}
	return nil
}

func (broker *profileBroker) readJenkins(ctx context.Context, input profileJob) (discoveryPage, error) {
	if input.TenantID == "" || input.ProductID == "" || input.ProjectID == "" || input.OperationID == "" || input.AttemptID == "" || input.BindingID == "" || input.Limit != 100 || input.Cursor != "" {
		return discoveryPage{}, errInvalidPage
	}
	var work profileWork
	if err := broker.profileCall(ctx, "/internal/v1/profile-discovery/resolve", input.profileRequest, &work); err != nil {
		return discoveryPage{}, err
	}
	if work.profileRequest != input.profileRequest || work.Provider != "jenkins" || work.Installation || work.Namespace == "" || work.ConnectionID == "" || len(work.ConnectionRevision) != 64 || work.BindingGeneration < 1 {
		return discoveryPage{}, errSourceUnavailable
	}
	namespace := work.Namespace
	if namespace == "/" {
		namespace = ""
	}
	client := &jenkinsProfileClient{broker: broker, ctx: ctx, work: work, namespace: namespace}
	result, err := providerPage(client, "jenkins", false, namespace, input.Cursor, input.Limit, time.Now())
	if err != nil {
		return discoveryPage{}, err
	}
	if len(result.Items) != 1 || result.Items[0].Kind != "ci_job" || result.Coverage != "complete" || result.NextCursor != "" {
		return discoveryPage{}, errSourceUnavailable
	}
	// ADP uses '/' for the root folder, where upstream uses an empty string.
	item := &result.Items[0]
	item.Namespace = work.Namespace
	item.FullName = item.NativeID
	item.SourceRevision = observedItem(item.Kind, item.Namespace, item.NativeID, item.Name+"\x00"+item.FullName, item.ObservedAt).SourceRevision
	return result, nil
}

// Only the native Jenkins list call is supported. Its URL/query never become
// broker authority: the callback independently derives them from stored work.
type jenkinsProfileClient struct {
	plugin.ApiClient
	broker    *profileBroker
	ctx       context.Context
	work      profileWork
	namespace string
	called    bool
}

func (client *jenkinsProfileClient) Get(path string, query url.Values, headers http.Header) (*http.Response, lakeerrors.Error) {
	var expected string
	if client.namespace != "" {
		for _, segment := range strings.Split(client.namespace, "/") {
			expected += "job/" + url.PathEscape(segment) + "/"
		}
	}
	expected += "api/json"
	tree := fmt.Sprintf("jobs[fullName,name,class,url,color,base,jobs,upstreamProjects[name]]{0,%d}", client.work.Limit)
	if client.called || path != expected || len(headers) != 0 || len(query) != 1 || len(query["tree"]) != 1 || query.Get("tree") != tree {
		return nil, lakeerrors.Convert(errInvalidPage)
	}
	client.called = true
	var result struct {
		ContractVersion int             `json:"contract_version"`
		Work            profileWork     `json:"work"`
		Data            json.RawMessage `json:"data"`
	}
	if err := client.broker.profileCall(client.ctx, "/internal/v1/profile-discovery/jenkins-page", client.work.profileRequest, &result); err != nil {
		return nil, lakeerrors.Convert(err)
	}
	if result.ContractVersion != 1 || result.Work != client.work || len(result.Data) == 0 {
		return nil, lakeerrors.Convert(errSourceUnavailable)
	}
	return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(bytes.NewReader(result.Data)), Header: make(http.Header)}, nil
}
