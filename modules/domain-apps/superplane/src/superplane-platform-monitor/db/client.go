// Package db provides the platform monitor's access to cluster state.
//
// # This package no longer talks to a database — issue #5056 (U15)
//
// The name is retained because `db.Querier` is the seam every monitor is written
// against and renaming it would touch every call site for no behavioural gain.
// What changed is everything below the seam: each method used to be SQL executed
// against Aurora over a `pgxpool`, and each is now an authenticated, workspace-
// scoped call to the API's observation endpoints.
//
// That substitution is the whole point of the story. R11 acceptance 1 is not "the
// monitor prefers the API"; it is that monitoring still works *after the monitor's
// direct table grant is withdrawn*, which proves the direct-write path is gone
// rather than merely unused. A client that kept a pool open as a fallback would
// pass a functional test and fail the acceptance, so there is no fallback and no
// pool: `pgxpool` is not imported here any more, and a withdrawn grant is
// therefore unobservable to this process.
//
// # The three invariants a Go sender must get exactly right
//
// The contract signs *the exact transmitted UTF-8 bytes*, so this client:
//
//  1. Marshals the body once, into a `[]byte`, and signs and sends that same
//     slice. It never re-marshals for the signature. Go and Python disagree on
//     Unicode escaping (`<`, `>`, `&`), float formatting and map ordering, so a
//     second marshal can produce different bytes and an unverifiable signature.
//  2. Duplicates the contract version in both the header and the body, because
//     the receiver requires the two to agree.
//  3. Sends a timestamp inside the receiver's freshness window, and treats
//     replay refusals (409) as terminal rather than retrying them.
//
// `tests/fixtures/go-observation-v1.json` in the contracts package pins the
// first invariant from the Python side: it holds bytes produced by Go's
// `encoding/json` for a payload containing `é`, `<`, `&`, `>` and 1e-06, and the
// signature Python must compute over them. `client_test.go` re-derives that
// signature here, so the two languages cannot drift apart silently.
package db

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// ContractVersion is the observation contract version this client speaks. Sent in
// both the header and the body; the receiver refuses a submission whose two
// copies disagree.
const ContractVersion = "v1"

// Contract header names, matching auth.py and version.py. Lowercase because Go's
// http.Header canonicalises on Set and the receiver matches case-insensitively.
const (
	versionHeader   = "x-superplane-contract-version"
	authHeader      = "authorization"
	signatureHeader = "x-superplane-signature"
)

// Check statuses on the wire. These are the contract's `CheckStatus` values, and
// they are lowercase where the monitor's internal HealthStatus* constants are
// capitalised — deliberately not unified, because the internal vocabulary also
// contains values the clusters.health_status column uses.
const (
	statusHealthy     = "healthy"
	statusNotChecked  = "not_checked"
	statusDegraded    = "degraded"
	statusUnknown     = "unknown"
	statusUnreachable = "unreachable"
)

// severityRank mirrors SEVERITY_RANK in the contract's health module. Duplicated
// rather than derived because the wire vocabulary is the contract's and the
// monitor's HealthDimensionSeverity is the monitor's; client_test.go asserts the
// two agree so the duplication cannot rot.
var severityRank = map[string]int{
	statusHealthy:     0,
	statusNotChecked:  1,
	statusDegraded:    2,
	statusUnknown:     3,
	statusUnreachable: 4,
}

// ErrReplayed reports that the receiver refused a submission as already recorded
// or superseded. Retrying it cannot succeed — the receiver's idempotency state
// says this exact submission has been seen — so callers must not treat it as a
// transient failure.
var ErrReplayed = errors.New("observation already recorded or superseded")

// ErrUnauthorized reports that the receiver rejected this client's credential or
// its authority over the subject. Also terminal: no number of retries adds
// authority, and retrying an authorization failure only produces audit noise.
var ErrUnauthorized = errors.New("submission not authorized")

// ClientConfig is what a Client needs in order to speak the contract.
type ClientConfig struct {
	// BaseURL of the API, e.g. https://api.internal. No trailing slash required.
	BaseURL string

	// Credential presented in the Authorization header. Read from the
	// environment by the caller; never logged by this package.
	Credential string

	// SigningKey for the body HMAC. Bound to the credential on the receiver
	// side, so a per-submitter key: a shared key would let any submitter forge
	// any other's bodies and make the credential's identity meaningless.
	SigningKey []byte

	// Reporter is the stable submitter name recorded in the payload. Not an
	// authentication claim — the credential is.
	Reporter string

	// InstanceID distinguishes replicas of the same submitter when leasing. It
	// is not an identity: the lease holder is the submitter plus this suffix,
	// composed by the receiver, so naming another replica here cannot release
	// its lease.
	InstanceID string

	// HTTPClient is optional; a bounded-timeout client is used when nil.
	HTTPClient *http.Client

	// Now is optional and exists for tests. Production leaves it nil and gets
	// time.Now().UTC().
	Now func() time.Time
}

// Client submits observations and reads scoped cluster state over the contract.
//
// The two maps below are not guarded by a mutex because the monitor runner drives
// every Querier call from a single goroutine, sequentially, and the only other
// caller — the /healthz handler — uses Ping, which touches neither map. A guard
// would be needed the moment monitors run concurrently.
type Client struct {
	cfg  ClientConfig
	http *http.Client

	// leases records the fence token the receiver granted for each held scope, so
	// release can present it. Tokens are receiver-assigned; this client never
	// invents or increments one.
	leases map[string]lease

	// knownWorkspaces caches the owning workspace the receiver reported for each
	// cluster, so a submission can name its subject without a second round trip.
	// It is a convenience only: the receiver re-resolves ownership from its own
	// storage and refuses a mismatch, so a stale entry here cannot authorize
	// anything — it produces a refusal, not a cross-tenant write.
	knownWorkspaces map[string]string
}

type lease struct {
	fenceToken int64
	expiresAt  time.Time
}

// NewClient builds a Client from configuration.
//
// Signature note: this deliberately does NOT keep the old
// `NewClient(ctx, connStr)` shape. A drop-in signature would let a caller pass a
// PostgreSQL DSN and get a working object, which is exactly the confusion this
// story removes — the compile error is the point, and it is why main.go's wiring
// had to be revisited rather than silently keeping a database URL.
func NewClient(cfg ClientConfig) (*Client, error) {
	if strings.TrimSpace(cfg.BaseURL) == "" {
		return nil, errors.New("observation API base URL is required")
	}
	if strings.TrimSpace(cfg.Credential) == "" {
		// Fail-closed at construction: an empty credential would produce an
		// unauthenticated request per call, so the misconfiguration surfaces at
		// startup instead of as a stream of 401s.
		return nil, errors.New("observation submitter credential is required")
	}
	if len(cfg.SigningKey) == 0 {
		return nil, errors.New("observation signing key is required")
	}
	if strings.TrimSpace(cfg.Reporter) == "" {
		return nil, errors.New("reporter name is required")
	}
	httpClient := cfg.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 30 * time.Second}
	}
	if strings.TrimSpace(cfg.InstanceID) == "" {
		cfg.InstanceID = "default"
	}
	return &Client{
		cfg:             cfg,
		http:            httpClient,
		leases:          map[string]lease{},
		knownWorkspaces: map[string]string{},
	}, nil
}

// Close releases client resources. Retained on the Querier-adjacent surface so
// main.go's `defer dbClient.Close()` keeps working; there is no pool to drain.
func (c *Client) Close() {
	c.http.CloseIdleConnections()
}

func (c *Client) now() time.Time {
	if c.cfg.Now != nil {
		return c.cfg.Now()
	}
	return time.Now().UTC()
}

// sign computes the contract signature over exactly the bytes passed in.
//
// Takes []byte rather than an object precisely so it cannot re-serialize: an
// object parameter would make it possible to sign a second marshalling of the
// payload instead of the bytes on the wire.
func (c *Client) sign(body []byte) string {
	mac := hmac.New(sha256.New, c.cfg.SigningKey)
	mac.Write(body)
	return fmt.Sprintf("sha256=%x", mac.Sum(nil))
}

// do performs one contract request.
//
// `body` is sent verbatim and signed verbatim. The signature header is only set
// for a signed submission (`signed`), because the lease and read routes
// authenticate by credential alone and a signature over a lease request would be
// meaningless — nothing on the receiver verifies it.
func (c *Client) do(
	ctx context.Context, method, path string, body []byte, signed bool, out any,
) error {
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.cfg.BaseURL+path, reader)
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	req.Header.Set(authHeader, c.cfg.Credential)
	req.Header.Set(versionHeader, ContractVersion)
	if body != nil {
		req.Header.Set("content-type", "application/json")
	}
	if signed {
		req.Header.Set(signatureHeader, c.sign(body))
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	defer resp.Body.Close()

	// Bounded read: a receiver bug or a misrouted response must not be able to
	// exhaust this process's memory.
	payload, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return fmt.Errorf("read response: %w", err)
	}

	switch {
	case resp.StatusCode == http.StatusConflict:
		return ErrReplayed
	case resp.StatusCode == http.StatusUnauthorized,
		resp.StatusCode == http.StatusForbidden:
		// The receiver's refusal reason is non-enumerating by construction, but
		// it is not echoed here either: it is not needed to act on the error, and
		// this keeps the request's headers well away from the log.
		return fmt.Errorf("%w (%s %s: %d)", ErrUnauthorized, method, path, resp.StatusCode)
	case resp.StatusCode < 200 || resp.StatusCode > 299:
		return fmt.Errorf("%s %s: unexpected status %d", method, path, resp.StatusCode)
	}

	if out != nil && len(payload) > 0 {
		if err := json.Unmarshal(payload, out); err != nil {
			return fmt.Errorf("decode response: %w", err)
		}
	}
	return nil
}

// --- observation submission -------------------------------------------------

// checkWire is one dimension's result as the contract carries it.
//
// Pointer fields so an absent value marshals as JSON null rather than "", which
// matters because the contract distinguishes an absent detail from a blank one
// and refuses a not_checked result that carries any detail at all.
type checkWire struct {
	Name       string  `json:"name"`
	Status     string  `json:"status"`
	ObservedAt *string `json:"observed_at"`
	Detail     *string `json:"detail"`
	Error      *string `json:"error"`
	Reason     *string `json:"reason"`
}

// Check is a health dimension result for submission, in the monitor's terms.
type Check struct {
	Name       string
	Status     string
	ObservedAt time.Time
	Detail     string
	Error      string
	Reason     string
}

// wireStatus translates the monitor's capitalised HealthStatus vocabulary to the
// contract's. Unrecognised values become "unknown" rather than "healthy": a
// status this client cannot name is not evidence of health.
func wireStatus(status string) string {
	switch status {
	case "Healthy", statusHealthy:
		return statusHealthy
	case "NotChecked", statusNotChecked:
		return statusNotChecked
	case "Degraded", statusDegraded:
		return statusDegraded
	case "Unreachable", statusUnreachable:
		return statusUnreachable
	default:
		return statusUnknown
	}
}

// aggregate reduces checks to their most severe status, matching
// health.aggregate_status. An empty set is not_checked, never healthy — the
// max-of-empty identity element for a "worst wins" reduction is the healthiest
// value, which is how a cluster with zero probes reports green.
func aggregate(checks []Check) string {
	if len(checks) == 0 {
		return statusNotChecked
	}
	worst := statusHealthy
	for _, ch := range checks {
		if s := wireStatus(ch.Status); severityRank[s] > severityRank[worst] {
			worst = s
		}
	}
	return worst
}

func optional(value string) *string {
	if value == "" {
		return nil
	}
	return &value
}

// toWire builds the checks array, enforcing the contract's honesty rules locally
// so a violation is a local error rather than a remote 401.
func toWire(checks []Check) ([]checkWire, error) {
	wire := make([]checkWire, 0, len(checks))
	for _, ch := range checks {
		status := wireStatus(ch.Status)
		out := checkWire{Name: ch.Name, Status: status}
		if status == statusNotChecked {
			// A not_checked result carries no reading: no observation time, no
			// error, no detail. It must state why, so the receiver can tell an
			// unconfigured probe from a skipped one.
			if strings.TrimSpace(ch.Reason) == "" {
				return nil, fmt.Errorf("check %q is not_checked without a reason", ch.Name)
			}
			out.Reason = optional(ch.Reason)
			wire = append(wire, out)
			continue
		}
		if ch.ObservedAt.IsZero() {
			// The no-op-prober rule: any status other than not_checked asserts an
			// observation, so it must say when it observed. Refusing locally is
			// what stops this client from being the thing that reports health it
			// never checked.
			return nil, fmt.Errorf(
				"check %q reports %s without an observation time; use NotChecked with a reason",
				ch.Name, status,
			)
		}
		observed := ch.ObservedAt.UTC().Format(time.RFC3339Nano)
		out.ObservedAt = &observed
		out.Detail = optional(ch.Detail)
		out.Error = optional(ch.Error)
		out.Reason = optional(ch.Reason)
		wire = append(wire, out)
	}
	return wire, nil
}

// SubmitObservation signs and submits one fleet-health observation.
//
// Returns the exact bytes that were signed and sent, so a caller (and the test
// suite) can assert that the signature belongs to the transmitted body rather
// than to a re-serialization of it.
func (c *Client) SubmitObservation(
	ctx context.Context, clusterID, workspace string, checks []Check, reportedAt time.Time,
) ([]byte, error) {
	wire, err := toWire(checks)
	if err != nil {
		return nil, err
	}
	payload := map[string]any{
		// Duplicated in the header by `do`; the receiver requires them to agree.
		"contract_version": ContractVersion,
		"kind":             "fleet_health",
		"subject": map[string]any{
			"cluster_id": clusterID,
			// A claim, not an authorization. The receiver resolves the cluster's
			// owner from its own storage and refuses a mismatch, which is why
			// sending this is safe and why it cannot be used to grant anything.
			"workspace": workspace,
		},
		"reported_at": reportedAt.UTC().Format(time.RFC3339Nano),
		"reporter":    c.cfg.Reporter,
		"status":      aggregate(checks),
	}
	if len(wire) > 0 {
		payload["checks"] = wire
	}

	// Marshalled exactly once. The same slice is signed by `do` and written to
	// the request body; there is no second marshal that could produce different
	// bytes.
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal observation: %w", err)
	}
	if err := c.do(ctx, http.MethodPost, "/internal/observations", body, true, nil); err != nil {
		return body, err
	}
	return body, nil
}

// --- Querier implementation -------------------------------------------------

// scopedCluster is the receiver's projection of a cluster row.
type scopedCluster struct {
	ClusterID     string     `json:"cluster_id"`
	Workspace     string     `json:"workspace"`
	OrgID         string     `json:"org_id"`
	Name          string     `json:"name"`
	Status        string     `json:"status"`
	HealthStatus  *string    `json:"health_status"`
	LastHeartbeat *time.Time `json:"last_heartbeat"`

	// LastReconciledAt is when a monitor cycle last recorded an observation for
	// this cluster. Distinct from LastHeartbeat, which is when the data-plane
	// controller last reported in — the field `checkHeartbeatFreshness` judges and
	// which a monitor submission deliberately does not touch.
	LastReconciledAt *time.Time      `json:"last_reconciled_at"`
	ActualStateJSON  json.RawMessage `json:"actual_state_json"`
}

// ListActiveClusters returns the clusters this submitter may observe.
//
// The status filter is still sent by the client, but the *workspace* filter is
// not: the receiver derives it from the authenticated credential's grant. So
// unlike the SQL this replaces, there is no request that returns a cluster
// outside the monitor's scope, and the monitor cannot widen its own view.
func (c *Client) ListActiveClusters(ctx context.Context) ([]Cluster, error) {
	var scoped []scopedCluster
	err := c.do(
		ctx, http.MethodGet,
		"/internal/observations/clusters?statuses=Active,Running,Provisioning,Pending",
		nil, false, &scoped,
	)
	if err != nil {
		return nil, fmt.Errorf("list observable clusters: %w", err)
	}

	clusters := make([]Cluster, 0, len(scoped))
	for i := range scoped {
		s := scoped[i]
		workspace := s.Workspace
		clusters = append(clusters, Cluster{
			ID:               s.ClusterID,
			OrgID:            s.OrgID,
			WorkspaceID:      &workspace,
			Name:             s.Name,
			Status:           s.Status,
			HealthStatus:     s.HealthStatus,
			LastHeartbeat:    s.LastHeartbeat,
			LastReconciledAt: s.LastReconciledAt,
			ActualStateJSON:  s.ActualStateJSON,
		})
		// Recorded so a later submission can name its subject without a second
		// round trip. The receiver still resolves ownership itself and refuses a
		// mismatch, so this cache is a convenience and never the authority.
		c.knownWorkspaces[s.ClusterID] = workspace
	}
	return clusters, nil
}

// UpdateClusterHealth submits the health result as a signed observation.
//
// The `details` argument carries the monitor's own HealthCheckResult JSON, which
// this method converts into contract checks. It is parsed rather than forwarded
// because the contract validates each dimension: a dimension claiming a status
// without an observation time is refused, and forwarding an opaque blob would
// skip exactly that guard.
func (c *Client) UpdateClusterHealth(
	ctx context.Context, clusterID string, healthStatus string, details json.RawMessage,
) error {
	var parsed struct {
		Dimensions []struct {
			Name    string `json:"name"`
			Status  string `json:"status"`
			Message string `json:"message"`
		} `json:"dimensions"`
		CheckedAt time.Time `json:"checked_at"`
	}
	if len(details) > 0 {
		if err := json.Unmarshal(details, &parsed); err != nil {
			return fmt.Errorf("parse health details: %w", err)
		}
	}
	observedAt := parsed.CheckedAt
	if observedAt.IsZero() {
		observedAt = c.now()
	}

	checks := make([]Check, 0, len(parsed.Dimensions))
	for _, d := range parsed.Dimensions {
		ch := Check{Name: d.Name, Status: d.Status}
		if wireStatus(d.Status) == statusNotChecked {
			// The dimension's message explains why nothing was checked, which is
			// what the contract requires a not_checked result to state. It must
			// not travel as `detail`: a not_checked result carrying any detail is
			// refused, because a detail is a reading.
			ch.Reason = d.Message
			if strings.TrimSpace(ch.Reason) == "" {
				ch.Reason = "no data was inspected for this dimension"
			}
		} else {
			ch.ObservedAt = observedAt
			ch.Error = errorMessageFor(d.Status, d.Message)
		}
		checks = append(checks, ch)
	}

	workspace, err := c.workspaceFor(clusterID)
	if err != nil {
		return err
	}
	if _, err := c.SubmitObservation(ctx, clusterID, workspace, checks, c.now()); err != nil {
		return fmt.Errorf("submit observation for cluster %s: %w", clusterID, err)
	}
	return nil
}

// errorMessageFor attaches a dimension's message as `error` only for statuses
// that are not positive claims.
//
// The contract refuses a result that pairs a positive claim with an error, and
// the messages this monitor produces for a healthy dimension are descriptions
// ("Heartbeat received 1m0s ago"), not errors. Sending them as `error` would make
// every healthy dimension unrepresentable.
func errorMessageFor(status, message string) string {
	switch wireStatus(status) {
	case statusHealthy, statusNotChecked:
		return ""
	default:
		return message
	}
}

// workspaceFor returns the owning workspace recorded for a cluster by the most
// recent ListActiveClusters.
//
// A cache miss is an error rather than an empty string: the workspace is a
// required subject field, and submitting a blank one would be refused by the
// contract anyway — with a message far less clear than this one.
func (c *Client) workspaceFor(clusterID string) (string, error) {
	if ws, ok := c.knownWorkspaces[clusterID]; ok && ws != "" {
		return ws, nil
	}
	return "", fmt.Errorf(
		"no owning workspace known for cluster %s; list clusters before submitting", clusterID,
	)
}

// InsertEvent records an event through the receiver's scoped event route.
//
// `orgID` is now unused: the receiver reads it from the cluster row it has
// already authorized. The parameter stays because `Querier`'s signatures are
// preserved (see the interface comment), and dropping it would change every
// caller for no gain — but nothing this client sends can influence which org the
// event is attributed to, which is the property that matters.
func (c *Client) InsertEvent(
	ctx context.Context, orgID, resourceID, eventType, message string, details json.RawMessage,
) error {
	payload := map[string]any{"event_type": eventType, "message": message}
	if len(details) > 0 {
		var parsed map[string]any
		if err := json.Unmarshal(details, &parsed); err != nil {
			return fmt.Errorf("parse event details: %w", err)
		}
		payload["details"] = parsed
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal event: %w", err)
	}
	path := "/internal/observations/" + resourceID + "/events"
	if err := c.do(ctx, http.MethodPost, path, body, false, nil); err != nil {
		return fmt.Errorf("record event for cluster %s: %w", resourceID, err)
	}
	return nil
}

// AcquireLock acquires a lease through the receiver, replacing the monitor's
// writes to `reconcile_locks`.
//
// Returns false rather than an error when another instance holds the lease: that
// is the normal outcome of contending for a lock, and the monitors already treat
// false as "skip this cycle". The receiver-assigned fence token is retained for
// release; this client never invents or increments one.
func (c *Client) AcquireLock(
	ctx context.Context, resourceType, resourceID, lockedBy string, ttl time.Duration,
) (bool, error) {
	// `lockedBy` is deliberately not sent. The receiver composes the holder from
	// the authenticated submitter plus InstanceID, so a caller cannot name itself
	// as another party and then release that party's lease.
	body, err := json.Marshal(map[string]any{
		"resource_type":    resourceType,
		"resource_id":      resourceID,
		"instance_id":      c.cfg.InstanceID,
		"duration_seconds": int(ttl.Seconds()),
	})
	if err != nil {
		return false, fmt.Errorf("marshal lease request: %w", err)
	}

	var granted struct {
		Scope      string    `json:"scope"`
		ExpiresAt  time.Time `json:"expires_at"`
		FenceToken int64     `json:"fence_token"`
	}
	err = c.do(ctx, http.MethodPost, "/internal/observations/leases", body, false, &granted)
	if errors.Is(err, ErrReplayed) {
		// 409 from the lease route means "held by someone else", not "replayed".
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("acquire lease %s/%s: %w", resourceType, resourceID, err)
	}

	c.leases[scopeKey(resourceType, resourceID)] = lease{
		fenceToken: granted.FenceToken,
		expiresAt:  granted.ExpiresAt,
	}
	return true, nil
}

// ReleaseLock releases a lease this client holds.
//
// Releasing a lease this client does not hold is not an error: the monitors
// release from a deferred call that also runs when acquisition was skipped, and
// turning that into a warning would produce a log line every cycle for every
// cluster another instance owns.
func (c *Client) ReleaseLock(
	ctx context.Context, resourceType, resourceID, lockedBy string,
) error {
	key := scopeKey(resourceType, resourceID)
	held, ok := c.leases[key]
	if !ok {
		return nil
	}
	body, err := json.Marshal(map[string]any{
		"resource_type": resourceType,
		"resource_id":   resourceID,
		"instance_id":   c.cfg.InstanceID,
		// The receiver checks this against the recorded holder and token, which is
		// what makes a stale holder unable to release a newer one's lease.
		"fence_token": held.fenceToken,
	})
	if err != nil {
		return fmt.Errorf("marshal lease release: %w", err)
	}
	if err := c.do(
		ctx, http.MethodPost, "/internal/observations/leases/release", body, false, nil,
	); err != nil {
		return fmt.Errorf("release lease %s/%s: %w", resourceType, resourceID, err)
	}
	delete(c.leases, key)
	return nil
}

func scopeKey(resourceType, resourceID string) string {
	return resourceType + "/" + resourceID
}

// GetCostHistory reads recent hourly costs through the receiver's scoped route.
func (c *Client) GetCostHistory(
	ctx context.Context, clusterID string, window time.Duration,
) ([]float64, error) {
	var out struct {
		Costs []float64 `json:"costs"`
	}
	path := fmt.Sprintf(
		"/internal/observations/%s/cost-history?window_seconds=%d",
		clusterID, int(window.Seconds()),
	)
	if err := c.do(ctx, http.MethodGet, path, nil, false, &out); err != nil {
		return nil, fmt.Errorf("read cost history for cluster %s: %w", clusterID, err)
	}
	return out.Costs, nil
}

// Ping checks that the receiver is reachable and this client's credential is
// accepted.
//
// It calls the scoped cluster list rather than an unauthenticated health route on
// purpose: after the grant withdrawal, "can I still monitor?" means "is my
// credential still accepted and scoped", and a liveness probe that answers
// healthy without exercising authentication would hide precisely the failure the
// cutover can introduce.
func (c *Client) Ping(ctx context.Context) error {
	var scoped []scopedCluster
	return c.do(
		ctx, http.MethodGet,
		"/internal/observations/clusters?statuses=Active",
		nil, false, &scoped,
	)
}

// Querier is the seam the monitors are written against.
//
// The seven signatures are byte-identical to the pre-#5056 SQL client's, so
// `cluster_health.go`, `budget.go` and the test fakes compile unchanged. That is
// a migration property, not an endorsement of the shape: `lockedBy` and `orgID`
// are now ignored (the receiver derives both from authenticated state), and a
// later story is free to narrow them. Changing them here would have mixed a
// transport substitution with an interface change in one commit, making it much
// harder to review whether the direct-write path was really gone.
type Querier interface {
	ListActiveClusters(ctx context.Context) ([]Cluster, error)
	UpdateClusterHealth(ctx context.Context, clusterID string, healthStatus string, details json.RawMessage) error
	InsertEvent(ctx context.Context, orgID, resourceID, eventType, message string, details json.RawMessage) error
	AcquireLock(ctx context.Context, resourceType, resourceID, lockedBy string, ttl time.Duration) (bool, error)
	ReleaseLock(ctx context.Context, resourceType, resourceID, lockedBy string) error
	GetCostHistory(ctx context.Context, clusterID string, window time.Duration) ([]float64, error)
	Ping(ctx context.Context) error
}

// Verify Client implements Querier.
var _ Querier = (*Client)(nil)
