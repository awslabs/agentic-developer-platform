package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	lakeerrors "github.com/apache/devlake/core/errors"
	"github.com/apache/devlake/core/plugin"
	githubapi "github.com/apache/devlake/plugins/github/api"
	github "github.com/apache/devlake/plugins/github/models"
	jiraapi "github.com/apache/devlake/plugins/jira/api"
	jira "github.com/apache/devlake/plugins/jira/models"
	"github.com/aws/aws-sdk-go/aws"
	"github.com/aws/aws-sdk-go/aws/credentials/endpointcreds"
	"github.com/aws/aws-sdk-go/aws/credentials/stscreds"
	"github.com/aws/aws-sdk-go/aws/session"
	"github.com/aws/aws-sdk-go/aws/signer/v4"
)

type discoveryItem struct {
	Kind             string `json:"kind"`
	Namespace        string `json:"namespace"`
	NativeID         string `json:"native_id"`
	Name             string `json:"name"`
	FullName         string `json:"full_name,omitempty"`
	DefaultBranch    string `json:"default_branch,omitempty"`
	SourceRevision   string `json:"source_revision"`
	UpstreamRevision string `json:"upstream_revision,omitempty"`
	ObservedAt       string `json:"observed_at"`
	Provenance       string `json:"provenance"`
}

type discoveryPage struct {
	ContractVersion int             `json:"contract_version"`
	Items           []discoveryItem `json:"items"`
	NextCursor      string          `json:"next_cursor"`
	Coverage        string          `json:"coverage"`
	Reason          string          `json:"reason,omitempty"`
}

var errUnsupportedCapability = errors.New("unsupported_capability")
var errInvalidPage = errors.New("invalid_page")
var errSourceUnavailable = errors.New("source_unavailable")
var errFeatureDisabled = errors.New("feature_disabled")

type checkedReadClient struct {
	plugin.ApiClient
}

func (client checkedReadClient) Get(path string, query url.Values, headers http.Header) (*http.Response, lakeerrors.Error) {
	response, err := client.ApiClient.Get(path, query, headers)
	if err != nil {
		return nil, err
	}
	if response == nil {
		return nil, lakeerrors.Convert(errSourceUnavailable)
	}
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		if response.Body != nil {
			_ = response.Body.Close()
		}
		return nil, lakeerrors.Convert(errSourceUnavailable)
	}
	return response, nil
}

func providerPage(client plugin.ApiClient, provider string, installation bool, namespace string, cursor string, limit int, observedAt time.Time) (discoveryPage, error) {
	if os.Getenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED") != "true" {
		return discoveryPage{}, errFeatureDisabled
	}
	if limit < 1 || limit > 100 {
		return discoveryPage{}, errInvalidPage
	}
	if provider != "github" && provider != "jira" {
		return expandedProviderPage(client, provider, installation, namespace, cursor, limit, observedAt)
	}
	if provider == "jira" && (installation || namespace == "") {
		return discoveryPage{}, errUnsupportedCapability
	}
	position, err := pagePosition(cursor, provider, limit)
	if err != nil {
		return discoveryPage{}, err
	}
	client = checkedReadClient{ApiClient: client}
	result := discoveryPage{ContractVersion: 1, Items: []discoveryItem{}, Coverage: "complete"}
	observed := observedAt.UTC().Format(time.RFC3339Nano)
	nextPosition := -1
	switch provider {
	case "github":
		children, next, err := githubapi.ADPListRemoteScopes(client, installation, namespace, githubapi.GithubRemotePagination{Page: position, PerPage: limit})
		if err != nil || len(children) > limit {
			return discoveryPage{}, errSourceUnavailable
		}
		for _, child := range children {
			itemNamespace := namespace
			kind := "repository"
			if child.ParentId == nil {
				kind = "namespace"
				itemNamespace = child.Id
			} else {
				itemNamespace = *child.ParentId
			}
			if child.Id == "" || itemNamespace == "" || child.Name == "" || (namespace != "" && itemNamespace != namespace) {
				return discoveryPage{}, errSourceUnavailable
			}
			result.Items = append(result.Items, observedItem(kind, itemNamespace, child.Id, child.Name, observed))
		}
		if next != nil {
			nextPosition = next.Page
		}
	case "jira":
		children, next, err := jiraapi.ADPListRemoteScopes(client, jiraapi.JiraRemotePagination{StartAt: position, MaxResults: limit})
		if err != nil || len(children) > limit {
			return discoveryPage{}, errSourceUnavailable
		}
		for _, child := range children {
			if child.Id == "" || child.Name == "" {
				return discoveryPage{}, errSourceUnavailable
			}
			result.Items = append(result.Items, observedItem("tracker", namespace, child.Id, child.Name, observed))
		}
		if next != nil {
			nextPosition = next.StartAt
		}
	}
	seen := make(map[[3]string]struct{}, len(result.Items))
	for _, item := range result.Items {
		identity := [3]string{item.Kind, item.Namespace, item.NativeID}
		if _, duplicate := seen[identity]; duplicate {
			return discoveryPage{}, errSourceUnavailable
		}
		seen[identity] = struct{}{}
	}
	if nextPosition != -1 {
		if nextPosition <= position {
			return discoveryPage{}, errSourceUnavailable
		}
		result.NextCursor = fmt.Sprintf("v1:%s:%d:%d", provider, limit, nextPosition)
		result.Coverage = "partial"
	}
	return result, nil
}

func pagePosition(cursor, provider string, limit int) (int, error) {
	first := 1
	if provider == "jira" {
		first = 0
	}
	if cursor == "" {
		return first, nil
	}
	value, valid := strings.CutPrefix(cursor, fmt.Sprintf("v1:%s:%d:", provider, limit))
	if !valid {
		return 0, errInvalidPage
	}
	if len(value) == 0 || len(value) > 7 || value[0] < '1' || value[0] > '9' {
		return 0, errInvalidPage
	}
	position, err := strconv.Atoi(value)
	if err != nil || position <= first || position > 1_000_000 || strconv.Itoa(position) != value {
		return 0, errInvalidPage
	}
	return position, nil
}

func brokerCursorPosition(cursor, provider string) (int, error) {
	if provider == "github" || cursor == "" {
		return pagePosition(cursor, provider, 100)
	}
	suffix, valid := strings.CutPrefix(cursor, "v1:jira:100:")
	if !valid {
		return 0, errInvalidPage
	}
	if !strings.HasPrefix(suffix, "t") {
		return pagePosition(cursor, provider, 100)
	}
	positionText, encoded, valid := strings.Cut(strings.TrimPrefix(suffix, "t"), ".")
	position, err := strconv.Atoi(positionText)
	if !valid || err != nil || position < 1 || position > 1_000_000 ||
		strconv.Itoa(position) != positionText || len(encoded) < 2 || len(encoded) > 256 {
		return 0, errInvalidPage
	}
	token, err := base64.RawURLEncoding.DecodeString(encoded)
	if err != nil || len(token) == 0 || len(token) > 180 || base64.RawURLEncoding.EncodeToString(token) != encoded {
		return 0, errInvalidPage
	}
	for _, character := range token {
		if character < 32 || character > 126 {
			return 0, errInvalidPage
		}
	}
	return position, nil
}

func observedItem(kind, namespace, nativeID, name, observed string) discoveryItem {
	digest := sha256.Sum256([]byte(kind + "\x00" + namespace + "\x00" + nativeID + "\x00" + name))
	return discoveryItem{
		Kind: kind, Namespace: namespace, NativeID: nativeID, Name: name,
		SourceRevision: "observation-sha256:" + hex.EncodeToString(digest[:]),
		ObservedAt:     observed, Provenance: "observation_only",
	}
}

func handler(response http.ResponseWriter, request *http.Request) {
	if request.URL.Path != "/healthz" || request.Method != http.MethodGet {
		http.NotFound(response, request)
		return
	}
	response.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(response).Encode(map[string]interface{}{
		"status":                    "packaged",
		"github_model":              github.GithubRepo{}.TableName(),
		"jira_model":                jira.JiraBoard{}.TableName(),
		"linked_provider_functions": []string{fmt.Sprintf("%T", githubapi.RemoteScopes), fmt.Sprintf("%T", jiraapi.RemoteScopes), fmt.Sprintf("%T", providerPage)},
	})
}

func main() {
	address := os.Getenv("CONNECTOR_API_LISTEN")
	if address == "" {
		address = "127.0.0.1:8080"
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", handler)
	broker, _ := newProfileBroker(os.Getenv("CONNECTOR_GATEWAY_URL"), os.Getenv("AWS_REGION"))
	mux.HandleFunc("/v1/profile-discovery", brokerDiscoveryHandler(broker))
	log.Fatal(http.ListenAndServe(address, mux))
}

type profileRequest struct {
	TenantID    string `json:"tenant_id"`
	ProductID   string `json:"product_id"`
	ProjectID   string `json:"project_id"`
	OperationID string `json:"operation_id"`
	AttemptID   string `json:"attempt_id"`
	BindingID   string `json:"binding_id"`
	Cursor      string `json:"cursor"`
	Limit       int    `json:"limit"`
}

type profileJob struct {
	profileRequest
	Provider string `json:"provider"`
}

type brokerPage struct {
	ContractVersion int                        `json:"contract_version"`
	Work            map[string]json.RawMessage `json:"work"`
	Items           []discoveryItem            `json:"items"`
	NextCursor      string                     `json:"next_cursor"`
	Coverage        string                     `json:"coverage"`
}

type profileBroker struct {
	address string
	client  *http.Client
	sign    func(*http.Request, []byte) error
}

func newProfileBroker(address, region string) (*profileBroker, error) {
	parsed, err := url.Parse(address)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" || region == "" {
		return nil, errSourceUnavailable
	}
	sdk, err := session.NewSessionWithOptions(session.Options{SharedConfigState: session.SharedConfigDisable, Config: aws.Config{Region: aws.String(region)}})
	if err != nil {
		return nil, errSourceUnavailable
	}
	return &profileBroker{address: strings.TrimRight(address, "/"), client: &http.Client{Timeout: 25 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return errSourceUnavailable }},
		sign: func(request *http.Request, payload []byte) error {
			identity, err := sdk.Config.Credentials.GetWithContext(request.Context())
			if err != nil || identity.ProviderName != stscreds.WebIdentityProviderName && identity.ProviderName != endpointcreds.ProviderName {
				return errSourceUnavailable
			}
			_, err = v4.NewSigner(sdk.Config.Credentials).Sign(request, bytes.NewReader(payload), "execute-api", region, time.Now())
			return err
		}}, nil
}

func (broker *profileBroker) read(ctx context.Context, input profileJob) (discoveryPage, error) {
	if input.Provider == "jenkins" {
		return broker.readJenkins(ctx, input)
	}
	if expandedProvider(input.Provider) {
		return broker.readExpanded(ctx, input)
	}
	if input.Provider != "github" && input.Provider != "jira" || input.TenantID == "" || input.ProductID == "" || input.ProjectID == "" || input.OperationID == "" || input.AttemptID == "" || input.BindingID == "" || input.Limit != 100 || len(input.Cursor) > 512 {
		return discoveryPage{}, errInvalidPage
	}
	currentPosition, err := brokerCursorPosition(input.Cursor, input.Provider)
	if err != nil {
		return discoveryPage{}, errInvalidPage
	}
	payload, err := json.Marshal(input.profileRequest)
	if err != nil {
		return discoveryPage{}, errInvalidPage
	}
	path := "/internal/v1/profile-discovery/github-page"
	if input.Provider == "jira" {
		path = "/internal/v1/profile-discovery/jira-boards-page"
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, broker.address+path, bytes.NewReader(payload))
	if err != nil {
		return discoveryPage{}, errSourceUnavailable
	}
	request.Header.Set("Content-Type", "application/json")
	if broker.sign == nil || broker.sign(request, payload) != nil {
		return discoveryPage{}, errSourceUnavailable
	}
	response, err := broker.client.Do(request)
	if err != nil {
		return discoveryPage{}, errSourceUnavailable
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return discoveryPage{}, errSourceUnavailable
	}
	content, err := io.ReadAll(io.LimitReader(response.Body, 1024*1024+1))
	var result brokerPage
	if err != nil || len(content) > 1024*1024 || json.Unmarshal(content, &result) != nil {
		return discoveryPage{}, errSourceUnavailable
	}
	if result.ContractVersion != 1 || len(result.Work) != 14 || len(result.Items) > input.Limit || (result.Coverage != "complete" && result.Coverage != "partial") || (result.Coverage == "partial") != (result.NextCursor != "") {
		return discoveryPage{}, errSourceUnavailable
	}
	var work profileRequest
	if json.Unmarshal(content, &struct {
		Work *profileRequest `json:"work"`
	}{Work: &work}) != nil || work != input.profileRequest {
		return discoveryPage{}, errSourceUnavailable
	}
	for _, field := range []string{"binding_generation", "connection_id", "connection_revision", "provider", "namespace"} {
		if len(result.Work[field]) == 0 || string(result.Work[field]) == `""` || string(result.Work[field]) == "null" {
			return discoveryPage{}, errSourceUnavailable
		}
	}
	var namespace, provider, revision string
	var installed bool
	var generation int
	if json.Unmarshal(result.Work["binding_generation"], &generation) != nil || generation < 1 ||
		json.Unmarshal(result.Work["connection_revision"], &revision) != nil || len(revision) != 64 {
		return discoveryPage{}, errSourceUnavailable
	}
	if json.Unmarshal(result.Work["namespace"], &namespace) != nil || json.Unmarshal(result.Work["provider"], &provider) != nil || json.Unmarshal(result.Work["installation"], &installed) != nil {
		return discoveryPage{}, errSourceUnavailable
	}
	if provider != "github" && provider != "jira" || provider == "github" && !installed || provider == "jira" && installed || provider != input.Provider {
		return discoveryPage{}, errSourceUnavailable
	}
	if provider == "jira" && path != "/internal/v1/profile-discovery/jira-boards-page" || provider == "github" && path != "/internal/v1/profile-discovery/github-page" {
		return discoveryPage{}, errSourceUnavailable
	}
	if result.NextCursor != "" {
		nextPosition, err := brokerCursorPosition(result.NextCursor, provider)
		if err != nil || nextPosition <= currentPosition ||
			provider == "github" && nextPosition != currentPosition+1 ||
			provider == "jira" && nextPosition > currentPosition+input.Limit {
			return discoveryPage{}, errSourceUnavailable
		}
	}
	seen := make(map[string]bool, len(result.Items))
	for index, item := range result.Items {
		if item.Namespace != namespace || item.Kind != map[string]string{"github": "repository", "jira": "tracker"}[provider] || item.NativeID == "" || item.Name == "" || item.SourceRevision == "" || seen[item.NativeID] {
			return discoveryPage{}, errSourceUnavailable
		}
		if _, err := time.Parse(time.RFC3339Nano, item.ObservedAt); err != nil {
			return discoveryPage{}, errSourceUnavailable
		}
		normalized := observedItem(item.Kind, item.Namespace, item.NativeID, item.Name, item.ObservedAt)
		normalized.FullName = item.FullName
		normalized.DefaultBranch = item.DefaultBranch
		normalized.UpstreamRevision = item.SourceRevision
		result.Items[index] = normalized
		seen[item.NativeID] = true
	}
	return discoveryPage{ContractVersion: 1, Items: result.Items, NextCursor: result.NextCursor, Coverage: result.Coverage}, nil
}

func brokerDiscoveryHandler(broker *profileBroker) http.HandlerFunc {
	return func(response http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost {
			http.Error(response, "method_not_allowed", http.StatusMethodNotAllowed)
			return
		}
		if broker == nil || os.Getenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED") != "true" {
			http.Error(response, "discovery_unavailable", http.StatusServiceUnavailable)
			return
		}
		var input profileJob
		decoder := json.NewDecoder(io.LimitReader(request.Body, 4097))
		decoder.DisallowUnknownFields()
		if decoder.Decode(&input) != nil || decoder.Decode(new(any)) != io.EOF {
			http.Error(response, "invalid_work", http.StatusUnprocessableEntity)
			return
		}
		page, err := broker.read(request.Context(), input)
		if err != nil {
			http.Error(response, "discovery_unavailable", http.StatusServiceUnavailable)
			return
		}
		response.Header().Set("Content-Type", "application/json")
		response.Header().Set("Cache-Control", "no-store")
		_ = json.NewEncoder(response).Encode(page)
	}
}
