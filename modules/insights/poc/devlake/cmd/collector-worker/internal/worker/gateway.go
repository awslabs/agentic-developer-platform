package worker

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"reflect"
	"strings"
	"time"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/aws/aws-sdk-go/aws"
	"github.com/aws/aws-sdk-go/aws/credentials/endpointcreds"
	"github.com/aws/aws-sdk-go/aws/credentials/stscreds"
	"github.com/aws/aws-sdk-go/aws/session"
	"github.com/aws/aws-sdk-go/aws/signer/v4"
)

var ErrWork = errors.New("collection_work_unavailable")

type Gateway struct {
	URL      string
	Client   *http.Client
	Sign     func(*http.Request, []byte) error
	JobPath  string
	Claim    Claim
	Contexts map[collection.Source]SourceContext
}

type Claim struct {
	JobID     string            `json:"job_id"`
	TenantID  string            `json:"tenant_id"`
	AttemptID string            `json:"attempt_id"`
	Fence     int               `json:"lease_fence"`
	Cutoff    time.Time         `json:"cutoff"`
	WindowEnd time.Time         `json:"window_end"`
	PageSize  int               `json:"page_size"`
	PageLimit int               `json:"page_limit"`
	Contexts  []json.RawMessage `json:"contexts"`
}

type SourceContext struct {
	Handle           string          `json:"-"`
	Version          int             `json:"context_version"`
	TenantID         string          `json:"tenant_id"`
	ProjectID        string          `json:"project_id"`
	CellID           string          `json:"cell_id"`
	GrantID          string          `json:"grant_id"`
	OperationID      string          `json:"operation_id"`
	AttemptID        string          `json:"attempt_id"`
	Purpose          string          `json:"purpose"`
	Audience         string          `json:"audience"`
	BindingID        string          `json:"binding_id"`
	BindingRevision  int64           `json:"binding_revision"`
	SourceGeneration int64           `json:"source_generation"`
	Provider         string          `json:"provider"`
	Consumer         string          `json:"consumer"`
	LeaseFence       int             `json:"lease_fence"`
	ModuleGeneration int64           `json:"module_generation"`
	RoutingRevision  int64           `json:"routing_revision"`
	PolicyRevision   int64           `json:"policy_revision"`
	PolicyEnvelope   json.RawMessage `json:"policy_envelope"`
	Expires          time.Time       `json:"expires_at"`
	Selections       []struct {
		TenantID     string `json:"tenant_id"`
		ProjectID    string `json:"project_id"`
		ConnectionID string `json:"source_connection_id"`
		RecordID     string `json:"record_id"`
		RecordType   string `json:"record_type"`
		Revision     string `json:"record_revision"`
	} `json:"source_selections"`
	Destination struct {
		DatabaseRef  string `json:"database_ref"`
		RoleRef      string `json:"role_ref"`
		ObjectPrefix string `json:"object_prefix"`
	} `json:"destination"`
}

func NewGateway(address, region string) (*Gateway, error) {
	parsed, err := url.Parse(address)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" || region == "" {
		return nil, ErrWork
	}
	sdk, err := session.NewSessionWithOptions(session.Options{SharedConfigState: session.SharedConfigDisable,
		Config: aws.Config{Region: aws.String(region)}})
	if err != nil {
		return nil, ErrWork
	}
	gateway := &Gateway{URL: strings.TrimRight(address, "/"), Client: &http.Client{Timeout: 40 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return ErrWork }}}
	gateway.Sign = func(request *http.Request, body []byte) error {
		identity, err := sdk.Config.Credentials.GetWithContext(request.Context())
		if err != nil || identity.ProviderName != stscreds.WebIdentityProviderName && identity.ProviderName != endpointcreds.ProviderName {
			return ErrWork
		}
		_, err = v4.NewSigner(sdk.Config.Credentials).Sign(request, bytes.NewReader(body), "execute-api", region, time.Now())
		return err
	}
	return gateway, nil
}

func (gateway *Gateway) call(ctx context.Context, method, path string, input, output any) error {
	if err := collection.CheckFlags(); err != nil {
		return err
	}
	var payload []byte
	var err error
	if input != nil {
		payload, err = json.Marshal(input)
		if err != nil {
			return ErrWork
		}
	}
	request, err := http.NewRequestWithContext(ctx, method, gateway.URL+path, bytes.NewReader(payload))
	if err != nil {
		return ErrWork
	}
	request.Header.Set("Content-Type", "application/json")
	if gateway.Sign == nil || gateway.Sign(request, payload) != nil {
		return ErrWork
	}
	response, err := gateway.Client.Do(request)
	if err != nil {
		return ErrWork
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return ErrWork
	}
	content, err := io.ReadAll(io.LimitReader(response.Body, 12*1024*1024+1))
	if err != nil || len(content) > 12*1024*1024 || json.Unmarshal(content, output) != nil {
		return ErrWork
	}
	return nil
}

func (gateway *Gateway) lease() map[string]any {
	return map[string]any{"attempt_id": gateway.Claim.AttemptID, "lease_fence": gateway.Claim.Fence}
}

func (gateway *Gateway) Recover(ctx context.Context, tenant, jobID, claimKey string) (*Result, error) {
	if tenant == "" || jobID == "" || claimKey == "" {
		return nil, ErrWork
	}
	var stored struct {
		JobID     string `json:"job_id"`
		TenantID  string `json:"tenant_id"`
		State     string `json:"state"`
		AttemptID string `json:"attempt_id"`
		Fence     int    `json:"lease_fence"`
		Receipt   *struct {
			BatchID       string `json:"batch_id"`
			ContentDigest string `json:"content_digest"`
			Items         *int   `json:"items"`
		} `json:"receipt"`
	}
	path := "/internal/v1/collection-jobs/" + url.PathEscape(tenant) + "/" + url.PathEscape(jobID) + "/receipt"
	if err := gateway.call(ctx, "POST", path, map[string]string{"claim_key": claimKey}, &stored); err != nil {
		return nil, err
	}
	if stored.JobID != jobID || stored.TenantID != tenant {
		return nil, ErrWork
	}
	if stored.State == "pending" || stored.State == "running" {
		return nil, nil
	}
	if stored.State != "succeeded" || stored.AttemptID == "" || stored.Fence < 1 || stored.Receipt == nil {
		return nil, ErrWork
	}
	receipt := stored.Receipt
	digest, err := hex.DecodeString(receipt.ContentDigest)
	if err != nil || len(digest) != 32 || receipt.ContentDigest != hex.EncodeToString(digest) ||
		receipt.BatchID != batchIdentity(jobID, stored.AttemptID) || receipt.Items == nil || *receipt.Items < 0 || *receipt.Items > 100000 {
		return nil, ErrWork
	}
	return &Result{BatchID: receipt.BatchID, ContentDigest: receipt.ContentDigest, Items: *receipt.Items}, nil
}

func (gateway *Gateway) Admit(ctx context.Context, tenant, jobID, claimKey string) (collection.Job, error) {
	if tenant == "" || jobID == "" || claimKey == "" {
		return collection.Job{}, ErrWork
	}
	gateway.JobPath = "/internal/v1/collection-jobs/" + url.PathEscape(tenant) + "/" + url.PathEscape(jobID)
	if err := gateway.call(ctx, "POST", gateway.JobPath+"/claim", map[string]string{"claim_key": claimKey}, &gateway.Claim); err != nil {
		return collection.Job{}, err
	}
	if gateway.Claim.JobID != jobID || gateway.Claim.TenantID != tenant || gateway.Claim.AttemptID == "" || gateway.Claim.Fence < 1 || len(gateway.Claim.Contexts) < 1 || len(gateway.Claim.Contexts) > 9 {
		return collection.Job{}, ErrWork
	}
	job := collection.Job{Cutoff: gateway.Claim.Cutoff, PageSize: gateway.Claim.PageSize, PageLimit: gateway.Claim.PageLimit}
	scopeKinds := map[string]string{
		"github": "repository", "jira": "tracker", "sonarqube": "quality_project", "jenkins": "ci_job",
		"argocd": "deployment_application", "gitlab": "repository", "pagerduty": "incident_service",
		"linear": "tracker_team", "slack": "communication_channel",
	}
	gateway.Contexts = make(map[collection.Source]SourceContext)
	for _, requested := range gateway.Claim.Contexts {
		var handle struct {
			Handle string `json:"handle"`
		}
		if err := gateway.call(ctx, "POST", "/internal/v1/connector-contexts/resolve", requested, &handle); err != nil || handle.Handle == "" {
			return collection.Job{}, ErrWork
		}
		var current SourceContext
		if err := gateway.call(ctx, "GET", "/internal/v1/connector-contexts/"+url.PathEscape(handle.Handle), nil, &current); err != nil {
			return collection.Job{}, err
		}
		if current.Version != 1 || current.TenantID != tenant || current.AttemptID != gateway.Claim.AttemptID || current.LeaseFence != gateway.Claim.Fence ||
			current.ModuleGeneration < 1 || current.Purpose != "collection" || len(current.Selections) != 1 || !current.Expires.After(time.Now()) {
			return collection.Job{}, ErrWork
		}
		selected := current.Selections[0]
		if selected.TenantID != tenant || selected.ProjectID != current.ProjectID || selected.Revision == "" ||
			scopeKinds[current.Provider] == "" || selected.RecordType != scopeKinds[current.Provider] ||
			selected.ConnectionID == "" || selected.RecordID == "" || current.CellID == "" ||
			current.BindingID == "" || current.GrantID == "" || current.Consumer == "" ||
			current.BindingRevision < 1 || current.SourceGeneration < 1 {
			return collection.Job{}, ErrWork
		}
		source := collection.Source{Tool: current.Provider, TenantID: tenant, ProjectID: current.ProjectID, CellID: current.CellID,
			ConnectionID: selected.ConnectionID, BindingID: current.BindingID, BindingGeneration: current.SourceGeneration,
			BindingRevision: current.BindingRevision, GrantID: current.GrantID, Consumer: current.Consumer, Scope: selected.RecordID}
		if _, duplicate := gateway.Contexts[source]; duplicate {
			return collection.Job{}, ErrWork
		}
		current.Handle = handle.Handle
		gateway.Contexts[source] = current
		job.Sources = append(job.Sources, source)
	}
	return job, nil
}

func (gateway *Gateway) CurrentSource(ctx context.Context, source collection.Source) (collection.CurrentSource, error) {
	expected, exists := gateway.Contexts[source]
	if !exists {
		return collection.CurrentSource{}, ErrWork
	}
	var renewed map[string]any
	if err := gateway.call(ctx, "POST", gateway.JobPath+"/renew", gateway.lease(), &renewed); err != nil {
		return collection.CurrentSource{}, err
	}
	var current SourceContext
	if err := gateway.call(ctx, "GET", "/internal/v1/connector-contexts/"+url.PathEscape(expected.Handle), nil, &current); err != nil {
		return collection.CurrentSource{}, err
	}
	expires := current.Expires
	current.Handle, current.Expires = expected.Handle, expected.Expires
	if !reflect.DeepEqual(current, expected) || !expires.After(time.Now()) {
		return collection.CurrentSource{}, ErrWork
	}
	return collection.CurrentSource{Source: source, BindingActive: true, CellActive: true, GrantActive: true,
		GrantPurpose: current.Purpose, GrantExpires: expires}, nil
}

type Page struct {
	Payload  []byte `json:"payload"`
	Complete bool   `json:"complete"`
	Private  bool   `json:"private"`
}

func (gateway *Gateway) Page(ctx context.Context, source collection.Source, kind string, page, limit int) (Page, error) {
	var result struct {
		Payload  []byte `json:"payload"`
		Complete *bool  `json:"complete"`
		Private  *bool  `json:"private"`
	}
	query := url.Values{"kind": {kind}, "page": {jsonNumber(page)}, "limit": {jsonNumber(limit)}}
	err := gateway.call(ctx, "GET", "/internal/v1/connector-contexts/"+url.PathEscape(gateway.Contexts[source].Handle)+"/page?"+query.Encode(), nil, &result)
	if err != nil || result.Complete == nil || result.Private == nil || len(result.Payload) == 0 {
		return Page{}, ErrWork
	}
	return Page{Payload: result.Payload, Complete: *result.Complete, Private: *result.Private}, nil
}

func jsonNumber(number int) string { encoded, _ := json.Marshal(number); return string(encoded) }

func (gateway *Gateway) Finish(ctx context.Context, state string, receipt Result) error {
	body := gateway.lease()
	body["state"] = state
	if state == "succeeded" {
		body["content_digest"] = receipt.ContentDigest
		body["batch_id"] = receipt.BatchID
		body["items"] = receipt.Items
	}
	var result map[string]any
	return gateway.call(ctx, "POST", gateway.JobPath+"/finish", body, &result)
}

func (gateway *Gateway) DeriveEvidence(ctx context.Context, source collection.Source, root collection.QualifiedRef) (collection.ExportPolicyEnvelope, error) {
	current, exists := gateway.Contexts[source]
	if !exists || current.Handle == "" || root.TenantID != source.TenantID ||
		root.ProjectID != source.ProjectID || root.SourceConnectionID != source.ConnectionID {
		return collection.ExportPolicyEnvelope{}, ErrWork
	}
	var result struct {
		PolicyEnvelope collection.ExportPolicyEnvelope `json:"policy_envelope"`
	}
	path := "/internal/v1/connector-contexts/" + url.PathEscape(current.Handle) + "/derive"
	if err := gateway.call(ctx, "POST", path, map[string]any{"root": root}, &result); err != nil {
		return collection.ExportPolicyEnvelope{}, err
	}
	return result.PolicyEnvelope, nil
}
