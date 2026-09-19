// Tests for the authenticated observation client — issue #5056 (U15), R11.
//
// Written against the properties R11 names, not the client's branches:
//
//   - acceptance 1: no direct database path remains, and monitoring works over
//     the API. The strongest available test in a unit suite is structural — this
//     package must not import a PostgreSQL driver — plus behavioural coverage of
//     every Querier method against a stub receiver.
//   - acceptance 2: every request carries a credential, and a submission carries a
//     signature over exactly the bytes transmitted.
//   - acceptance 3: the client cannot widen its own scope, cannot name another
//     holder, and treats an authorization refusal as terminal.
//   - acceptance 4: the client refuses to send a health claim with no observation
//     behind it, so a dishonest probe cannot reach the receiver at all.
//
// The signing keys below are literals generated for this file. They are not
// credentials and authorize nothing.
package db

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const (
	testCredential = "test-credential-not-a-real-one"
	testReporter   = "test-monitor"
)

var testSigningKey = []byte("test-signing-key-not-a-credential")

// capture records what the stub receiver saw, so assertions can be made about
// the bytes and headers actually sent rather than about what the client meant to
// send.
type capture struct {
	method    string
	path      string
	query     string
	body      []byte
	auth      string
	version   string
	signature string
	calls     int
}

// stub is a receiver that records requests and replies with a canned response.
type stub struct {
	server   *httptest.Server
	seen     []*capture
	status   int
	response string
	// statusFor overrides `status` per request path substring.
	statusFor map[string]int
	// responseFor overrides `response` per request path substring, for tests that
	// drive several routes in one call and need each to return its own shape.
	responseFor map[string]string
}

func newStub(t *testing.T) *stub {
	t.Helper()
	s := &stub{
		status:      http.StatusOK,
		response:    "{}",
		statusFor:   map[string]int{},
		responseFor: map[string]string{},
	}
	s.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body := make([]byte, 0)
		if r.Body != nil {
			buf := make([]byte, 1<<16)
			n, _ := r.Body.Read(buf)
			body = buf[:n]
		}
		c := &capture{
			method:    r.Method,
			path:      r.URL.Path,
			query:     r.URL.RawQuery,
			body:      body,
			auth:      r.Header.Get("authorization"),
			version:   r.Header.Get("x-superplane-contract-version"),
			signature: r.Header.Get("x-superplane-signature"),
		}
		s.seen = append(s.seen, c)

		code := s.status
		for fragment, override := range s.statusFor {
			if strings.Contains(r.URL.Path, fragment) {
				code = override
			}
		}
		body_ := s.response
		for fragment, override := range s.responseFor {
			if strings.Contains(r.URL.Path, fragment) {
				body_ = override
			}
		}
		w.WriteHeader(code)
		fmt.Fprint(w, body_)
	}))
	t.Cleanup(s.server.Close)
	return s
}

func (s *stub) last() *capture {
	if len(s.seen) == 0 {
		return &capture{}
	}
	return s.seen[len(s.seen)-1]
}

func newTestClient(t *testing.T, s *stub) *Client {
	t.Helper()
	client, err := NewClient(ClientConfig{
		BaseURL:    s.server.URL,
		Credential: testCredential,
		SigningKey: testSigningKey,
		Reporter:   testReporter,
		InstanceID: "pod-a",
		Now:        func() time.Time { return time.Date(2026, 9, 16, 12, 0, 0, 0, time.UTC) },
	})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	return client
}

func healthyCheck() Check {
	return Check{
		Name:       "eks_reachability",
		Status:     HealthStatusFor("Healthy"),
		ObservedAt: time.Date(2026, 9, 16, 12, 0, 0, 0, time.UTC),
		Detail:     "reachable",
	}
}

// HealthStatusFor exists only to keep the test readable where a monitor-side
// status string is meant; the client accepts either vocabulary.
func HealthStatusFor(s string) string { return s }

// --- acceptance 1: there is no direct database path left ---------------------

func TestClientPackageDoesNotImportAPostgresDriver(t *testing.T) {
	// Structural, and deliberately so. Every other test here would still pass if
	// the client kept a pool open as a fallback, and a fallback is precisely what
	// would make the grant withdrawal a silent no-op instead of a proof. The
	// non-test sources in this package must not reference a driver at all.
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("read package dir: %v", err)
	}
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		source, err := os.ReadFile(filepath.Clean(name))
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		for _, banned := range []string{"jackc/pgx", "pgxpool", "database/sql", "lib/pq"} {
			// The package doc mentions pgxpool in prose, explaining that it is
			// gone. Only an import line is a real dependency.
			for _, line := range strings.Split(string(source), "\n") {
				trimmed := strings.TrimSpace(line)
				if strings.HasPrefix(trimmed, "//") {
					continue
				}
				if strings.Contains(trimmed, banned) {
					t.Errorf("%s references %s outside a comment: %s", name, banned, trimmed)
				}
			}
		}
	}
}

func TestClientRequiresEveryCredentialSettingAtConstruction(t *testing.T) {
	// Fail-closed at startup: a client built without a credential would issue
	// unauthenticated requests forever, which reads as an API outage rather than
	// as the misconfiguration it is.
	base := ClientConfig{
		BaseURL:    "https://api.invalid",
		Credential: testCredential,
		SigningKey: testSigningKey,
		Reporter:   testReporter,
	}
	tests := []struct {
		name   string
		mutate func(*ClientConfig)
	}{
		{"no base URL", func(c *ClientConfig) { c.BaseURL = "" }},
		{"blank base URL", func(c *ClientConfig) { c.BaseURL = "   " }},
		{"no credential", func(c *ClientConfig) { c.Credential = "" }},
		{"blank credential", func(c *ClientConfig) { c.Credential = "  " }},
		{"no signing key", func(c *ClientConfig) { c.SigningKey = nil }},
		{"empty signing key", func(c *ClientConfig) { c.SigningKey = []byte{} }},
		{"no reporter", func(c *ClientConfig) { c.Reporter = "" }},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := base
			tt.mutate(&cfg)
			client, err := NewClient(cfg)
			if err == nil {
				t.Fatal("expected an error, got a usable client")
			}
			if client != nil {
				t.Error("expected no client alongside the error")
			}
		})
	}
}

func TestClientErrorsNeverContainTheCredentialOrSigningKey(t *testing.T) {
	// A construction error naming the missing setting is useful; one echoing the
	// value would put a credential in the monitor's log at Fatal level.
	_, err := NewClient(ClientConfig{
		BaseURL:    "",
		Credential: testCredential,
		SigningKey: testSigningKey,
		Reporter:   testReporter,
	})
	if err == nil {
		t.Fatal("expected an error")
	}
	if strings.Contains(err.Error(), testCredential) ||
		strings.Contains(err.Error(), string(testSigningKey)) {
		t.Errorf("error leaks a secret: %v", err)
	}
}

// --- acceptance 2: authenticated, and signed over the transmitted bytes ------

func TestEveryRequestCarriesTheCredentialAndContractVersion(t *testing.T) {
	// Enumerated rather than spot-checked: a route added later without the header
	// would be an unauthenticated call, and this is what catches it.
	ctx := context.Background()
	calls := map[string]func(*Client) error{
		"ListActiveClusters": func(c *Client) error {
			_, err := c.ListActiveClusters(ctx)
			return err
		},
		"UpdateClusterHealth": func(c *Client) error {
			c.knownWorkspaces["c1"] = "ws-1"
			return c.UpdateClusterHealth(ctx, "c1", "Healthy", healthDetails())
		},
		"InsertEvent": func(c *Client) error {
			return c.InsertEvent(ctx, "org-1", "c1", "e", "m", nil)
		},
		"AcquireLock": func(c *Client) error {
			_, err := c.AcquireLock(ctx, "cluster_health", "c1", "monitor-1", time.Minute)
			return err
		},
		"ReleaseLock": func(c *Client) error {
			if _, err := c.AcquireLock(ctx, "cluster_health", "c1", "monitor-1", time.Minute); err != nil {
				return err
			}
			return c.ReleaseLock(ctx, "cluster_health", "c1", "monitor-1")
		},
		"GetCostHistory": func(c *Client) error {
			_, err := c.GetCostHistory(ctx, "c1", time.Hour)
			return err
		},
		"Ping": func(c *Client) error { return c.Ping(ctx) },
	}
	for name, call := range calls {
		t.Run(name, func(t *testing.T) {
			s := newStub(t)
			s.response = `{"fence_token": 1, "costs": []}`
			// The cluster list is an array, not an object, so it needs its own
			// canned body — otherwise this test would fail on decoding rather
			// than on the header property it is about.
			s.responseFor["/clusters"] = `[]`
			client := newTestClient(t, s)

			if err := call(client); err != nil {
				t.Fatalf("%s: %v", name, err)
			}
			if len(s.seen) == 0 {
				t.Fatal("no request was made")
			}
			for _, seen := range s.seen {
				if seen.auth != testCredential {
					t.Errorf("%s %s: missing credential", seen.method, seen.path)
				}
				if seen.version != ContractVersion {
					t.Errorf("%s %s: version header %q", seen.method, seen.path, seen.version)
				}
			}
		})
	}
}

func TestSubmissionSignsExactlyTheBytesItSends(t *testing.T) {
	// The invariant the contract rests on. Re-marshalling the payload to sign it
	// would produce a different byte string in several realistic cases (key order,
	// float formatting, HTML escaping), and a signature over those bytes cannot be
	// verified against what arrived.
	s := newStub(t)
	client := newTestClient(t, s)

	sent, err := client.SubmitObservation(
		context.Background(), "c1", "ws-1", []Check{healthyCheck()},
		time.Date(2026, 9, 16, 12, 0, 0, 0, time.UTC),
	)
	if err != nil {
		t.Fatalf("SubmitObservation: %v", err)
	}

	seen := s.last()
	if string(seen.body) != string(sent) {
		t.Fatalf("received body differs from the signed body:\n sent %s\n got  %s", sent, seen.body)
	}
	mac := hmac.New(sha256.New, testSigningKey)
	mac.Write(seen.body)
	want := fmt.Sprintf("sha256=%x", mac.Sum(nil))
	if seen.signature != want {
		t.Errorf("signature does not verify over the received bytes\n got  %s\n want %s",
			seen.signature, want)
	}
}

func TestSignatureIsComputedOverBytesNotOverAReparse(t *testing.T) {
	// Guards the same invariant from the other direction: re-encoding the body
	// that arrived must change it, which is what makes signing a re-marshal
	// unsafe. If this ever stops being true the risk is gone, but so is the
	// reason to trust the current code, so it should be revisited deliberately.
	s := newStub(t)
	client := newTestClient(t, s)

	_, err := client.SubmitObservation(
		context.Background(), "cluster-w1-a", "ws-w1",
		[]Check{{
			Name:       "vault_sync",
			Status:     "Degraded",
			ObservedAt: time.Date(2026, 9, 16, 12, 0, 0, 0, time.UTC),
			Error:      "moniteur-é<&>",
		}},
		time.Date(2026, 9, 16, 12, 0, 0, 0, time.UTC),
	)
	if err != nil {
		t.Fatalf("SubmitObservation: %v", err)
	}

	raw := s.last().body

	// Go's encoder escapes `<`, `&` and `>` as their \u003c forms. Python's
	// json.dumps does not, so the bytes Go sent are NOT what Python would produce
	// from the same object — which is exactly why the receiver verifies against the
	// transmitted bytes and never against a re-encoding of them.
	if !strings.Contains(string(raw), `\u003c`) {
		t.Errorf("expected Go's HTML escaping in the wire bytes, got: %s", raw)
	}
	if strings.Contains(string(raw), "<") {
		t.Errorf("expected `<` to be escaped rather than literal: %s", raw)
	}

	// And a round trip through parse-then-marshal must not be assumed identical.
	var parsed map[string]any
	if err := json.Unmarshal(raw, &parsed); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	mac := hmac.New(sha256.New, testSigningKey)
	mac.Write(raw)
	if s.last().signature != fmt.Sprintf("sha256=%x", mac.Sum(nil)) {
		t.Error("signature does not belong to the transmitted bytes")
	}
}

func TestGoWireBytesMatchTheCrossLanguageFixture(t *testing.T) {
	// The Python side pins these exact bytes and this exact signature in
	// tests/fixtures/go-observation-v1.json and asserts they verify. Recomputing
	// the HMAC here means the two languages cannot drift apart silently: if Go's
	// encoder ever changes its escaping or number formatting, this fails on the
	// Go side rather than only in a Python suite nobody runs when editing Go.
	vectorPath := filepath.Join(
		"..", "..", "..", "tests", "fixtures", "go-observation-v1.json",
	)
	rawVector, err := os.ReadFile(vectorPath)
	if err != nil {
		t.Skipf("cross-language fixture not available: %v", err)
	}
	var vector struct {
		BodyBase64 string `json:"body_base64"`
		Signature  string `json:"signature"`
	}
	if err := json.Unmarshal(rawVector, &vector); err != nil {
		t.Fatalf("parse fixture: %v", err)
	}
	body, err := base64.StdEncoding.DecodeString(vector.BodyBase64)
	if err != nil {
		t.Fatalf("decode fixture body: %v", err)
	}

	client, err := NewClient(ClientConfig{
		BaseURL:    "https://api.invalid",
		Credential: "test-credential",
		SigningKey: testSigningKey,
		Reporter:   testReporter,
	})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	if got := client.sign(body); got != vector.Signature {
		t.Errorf("signature over the fixture bytes differs\n got  %s\n want %s",
			got, vector.Signature)
	}

	// And Go must still produce those bytes from the same payload — the fixture is
	// only meaningful if it is reproducible.
	regenerated, err := json.Marshal(map[string]any{
		"budget": map[string]any{
			"currency":           "USD",
			"observed_spend_usd": 0.000001,
			"window_end":         "2026-09-16T12:00:00Z",
			"window_start":       "2026-09-16T11:00:00Z",
			"workspace":          "ws-w1",
		},
		"contract_version": "v1",
		"kind":             "budget_usage",
		"reported_at":      "2026-09-16T12:00:00Z",
		"reporter":         "moniteur-é<&>",
		"status":           "not_checked",
		"subject": map[string]any{
			"cluster_id": "cluster-w1-a",
			"workspace":  "ws-w1",
		},
	})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if string(regenerated) != string(body) {
		t.Errorf("Go no longer reproduces the fixture bytes\n got  %s\n want %s",
			regenerated, body)
	}
}

func TestTheContractVersionAppearsInBothTheHeaderAndTheBody(t *testing.T) {
	// The receiver requires the two to agree, so sending only one is refused.
	s := newStub(t)
	client := newTestClient(t, s)

	if _, err := client.SubmitObservation(
		context.Background(), "c1", "ws-1", []Check{healthyCheck()}, time.Now().UTC(),
	); err != nil {
		t.Fatalf("SubmitObservation: %v", err)
	}

	seen := s.last()
	var payload map[string]any
	if err := json.Unmarshal(seen.body, &payload); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if payload["contract_version"] != ContractVersion || seen.version != ContractVersion {
		t.Errorf("version mismatch: body %v, header %q", payload["contract_version"], seen.version)
	}
}

func TestAnAuthorizationRefusalIsTerminalAndTyped(t *testing.T) {
	// Retrying an authorization failure cannot succeed — no number of attempts adds
	// authority — and a retry loop against a 403 is an audit-log flood.
	for _, code := range []int{http.StatusUnauthorized, http.StatusForbidden} {
		t.Run(http.StatusText(code), func(t *testing.T) {
			s := newStub(t)
			s.status = code
			client := newTestClient(t, s)

			_, err := client.SubmitObservation(
				context.Background(), "c1", "ws-1", []Check{healthyCheck()}, time.Now().UTC(),
			)
			if !errors.Is(err, ErrUnauthorized) {
				t.Fatalf("expected ErrUnauthorized, got %v", err)
			}
			if len(s.seen) != 1 {
				t.Errorf("expected exactly 1 attempt, got %d", len(s.seen))
			}
		})
	}
}

func TestAnErrorNeverEchoesTheCredentialOrSignature(t *testing.T) {
	// Errors are logged. A refusal that quoted the credential we sent would put it
	// in the log on every failing cycle, and the receiver's refusals are
	// deliberately non-enumerating for the same class of reason.
	s := newStub(t)
	s.status = http.StatusForbidden
	client := newTestClient(t, s)

	_, err := client.SubmitObservation(
		context.Background(), "c1", "ws-1", []Check{healthyCheck()}, time.Now().UTC(),
	)
	if err == nil {
		t.Fatal("expected an error")
	}
	if strings.Contains(err.Error(), testCredential) ||
		strings.Contains(err.Error(), string(testSigningKey)) ||
		strings.Contains(err.Error(), s.last().signature) {
		t.Errorf("error leaks a secret: %v", err)
	}
}

// --- replay and idempotency -------------------------------------------------

func TestAReplayRefusalIsTerminalAndDistinguishable(t *testing.T) {
	// A 409 means the receiver's idempotency state already has this submission.
	// Retrying it cannot change that, so it must not be treated as transient — and
	// it must be distinguishable from a real failure, because it is a success from
	// the monitor's point of view.
	s := newStub(t)
	s.status = http.StatusConflict
	client := newTestClient(t, s)

	_, err := client.SubmitObservation(
		context.Background(), "c1", "ws-1", []Check{healthyCheck()}, time.Now().UTC(),
	)
	if !errors.Is(err, ErrReplayed) {
		t.Fatalf("expected ErrReplayed, got %v", err)
	}
	if errors.Is(err, ErrUnauthorized) {
		t.Error("a replay must not be reported as an authorization failure")
	}
	if len(s.seen) != 1 {
		t.Errorf("expected exactly 1 attempt, got %d", len(s.seen))
	}
}

func TestResubmittingTheSameObservationProducesIdenticalBytes(t *testing.T) {
	// What makes the receiver's idempotency usable: it keys on the body hash, so a
	// retry of the same observation must be byte-identical or it would be recorded
	// as a second, different submission.
	s := newStub(t)
	client := newTestClient(t, s)
	at := time.Date(2026, 9, 16, 12, 0, 0, 0, time.UTC)

	first, err := client.SubmitObservation(context.Background(), "c1", "ws-1", []Check{healthyCheck()}, at)
	if err != nil {
		t.Fatalf("first submit: %v", err)
	}
	second, err := client.SubmitObservation(context.Background(), "c1", "ws-1", []Check{healthyCheck()}, at)
	if err != nil {
		t.Fatalf("second submit: %v", err)
	}

	if string(first) != string(second) {
		t.Errorf("retry is not byte-identical:\n %s\n %s", first, second)
	}
	if s.seen[0].signature != s.seen[1].signature {
		t.Error("identical bodies produced different signatures")
	}
}

// --- acceptance 3: the client cannot widen its own authority -----------------

func TestTheClusterListIsNotFilteredByAWorkspaceTheClientChooses(t *testing.T) {
	// The scope comes from the credential's grant on the receiver. If the client
	// sent a workspace filter, a bug or a config change here could widen what it
	// asks for; as written there is no such parameter to get wrong.
	s := newStub(t)
	s.response = `[]`
	client := newTestClient(t, s)

	if _, err := client.ListActiveClusters(context.Background()); err != nil {
		t.Fatalf("ListActiveClusters: %v", err)
	}

	if strings.Contains(s.last().query, "workspace") {
		t.Errorf("client sent a workspace filter: %s", s.last().query)
	}
}

func TestALeaseRequestDoesNotNameItsOwnHolder(t *testing.T) {
	// `lockedBy` is still in the signature for source compatibility, but sending it
	// would let a caller claim to be another holder — and releasing a lease someone
	// else still holds recreates exactly the concurrent reconcile the lease
	// prevents. The receiver derives the holder from the authenticated submitter.
	s := newStub(t)
	s.response = `{"fence_token": 1}`
	client := newTestClient(t, s)

	if _, err := client.AcquireLock(
		context.Background(), "cluster_health", "c1", "some-other-monitor", time.Minute,
	); err != nil {
		t.Fatalf("AcquireLock: %v", err)
	}

	if strings.Contains(string(s.last().body), "some-other-monitor") {
		t.Errorf("lease request carries a caller-chosen holder: %s", s.last().body)
	}
}

func TestAnEventDoesNotCarryACallerChosenOrg(t *testing.T) {
	// The old INSERT took org_id as an argument. Sending it would let the monitor
	// attribute events to another tenant's audit trail; the receiver reads it from
	// the cluster row it has already authorized.
	s := newStub(t)
	client := newTestClient(t, s)

	if err := client.InsertEvent(
		context.Background(), "org-belonging-to-someone-else", "c1", "e", "m", nil,
	); err != nil {
		t.Fatalf("InsertEvent: %v", err)
	}

	if strings.Contains(string(s.last().body), "org-belonging-to-someone-else") {
		t.Errorf("event body carries a caller-chosen org: %s", s.last().body)
	}
}

func TestSubmissionFailsWhenNoOwningWorkspaceIsKnown(t *testing.T) {
	// The workspace is a required subject field. Guessing one, or sending a blank,
	// would turn a local bug into a refusal from the receiver with a deliberately
	// uninformative reason — so the client says plainly what is missing instead.
	s := newStub(t)
	client := newTestClient(t, s)

	err := client.UpdateClusterHealth(context.Background(), "never-listed", "Healthy", healthDetails())

	if err == nil {
		t.Fatal("expected an error for a cluster whose owner is unknown")
	}
	if len(s.seen) != 0 {
		t.Errorf("expected no request to be made, got %d", len(s.seen))
	}
}

func TestTheOwningWorkspaceComesFromTheReceiverNotFromTheCaller(t *testing.T) {
	s := newStub(t)
	s.response = `[{"cluster_id": "c1", "workspace": "ws-from-receiver", "org_id": "o1",
	                "name": "n", "status": "Active", "health_status": null,
	                "last_heartbeat": null, "actual_state_json": null}]`
	client := newTestClient(t, s)

	if _, err := client.ListActiveClusters(context.Background()); err != nil {
		t.Fatalf("ListActiveClusters: %v", err)
	}
	s.response = "{}"
	if err := client.UpdateClusterHealth(
		context.Background(), "c1", "Healthy", healthDetails(),
	); err != nil {
		t.Fatalf("UpdateClusterHealth: %v", err)
	}

	var payload struct {
		Subject struct {
			Workspace string `json:"workspace"`
		} `json:"subject"`
	}
	if err := json.Unmarshal(s.last().body, &payload); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if payload.Subject.Workspace != "ws-from-receiver" {
		t.Errorf("workspace = %q, want the one the receiver reported", payload.Subject.Workspace)
	}
}

// --- acceptance 4: a probe never reports health it did not check -------------

func TestAStatusWithNoObservationTimeIsRefusedBeforeItIsSent(t *testing.T) {
	// The sender-side half of acceptance 4. Any status other than not_checked
	// asserts an observation, so it must say when the observation happened.
	// Refusing locally means a dishonest claim never reaches the receiver, and the
	// error names the honest alternative.
	s := newStub(t)
	client := newTestClient(t, s)

	_, err := client.SubmitObservation(
		context.Background(), "c1", "ws-1",
		[]Check{{Name: "eks_reachability", Status: "Healthy"}}, time.Now().UTC(),
	)

	if err == nil {
		t.Fatal("expected a refusal for a health claim with no observation time")
	}
	if !strings.Contains(err.Error(), "NotChecked") {
		t.Errorf("error should point at the honest alternative, got: %v", err)
	}
	if len(s.seen) != 0 {
		t.Errorf("nothing should have been sent, got %d requests", len(s.seen))
	}
}

func TestANotCheckedResultMustStateAReason(t *testing.T) {
	// "Not checked" without a reason is not much better than a false healthy: the
	// receiver cannot tell an unconfigured probe from a skipped one.
	s := newStub(t)
	client := newTestClient(t, s)

	_, err := client.SubmitObservation(
		context.Background(), "c1", "ws-1",
		[]Check{{Name: "eks_reachability", Status: "NotChecked"}}, time.Now().UTC(),
	)

	if err == nil {
		t.Fatal("expected a refusal for not_checked without a reason")
	}
	if len(s.seen) != 0 {
		t.Errorf("nothing should have been sent, got %d requests", len(s.seen))
	}
}

func TestANotCheckedResultCarriesNoReading(t *testing.T) {
	// A not_checked result that carried a detail, an error or an observation time
	// would be asserting a reading it does not have, and the contract refuses it.
	s := newStub(t)
	client := newTestClient(t, s)

	body, err := client.SubmitObservation(
		context.Background(), "c1", "ws-1",
		[]Check{{
			Name:       "eks_reachability",
			Status:     "NotChecked",
			Reason:     "EKS probing not configured",
			ObservedAt: time.Now().UTC(),
			Detail:     "reachable",
			Error:      "should not travel",
		}},
		time.Now().UTC(),
	)
	if err != nil {
		t.Fatalf("SubmitObservation: %v", err)
	}

	var payload struct {
		Checks []struct {
			Status     string  `json:"status"`
			ObservedAt *string `json:"observed_at"`
			Detail     *string `json:"detail"`
			Error      *string `json:"error"`
			Reason     *string `json:"reason"`
		} `json:"checks"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	check := payload.Checks[0]
	if check.ObservedAt != nil || check.Detail != nil || check.Error != nil {
		t.Errorf("not_checked carried a reading: %+v", check)
	}
	if check.Reason == nil || *check.Reason == "" {
		t.Error("not_checked lost its reason")
	}
}

func TestTheSeverityRankMatchesTheMonitorsOwnOrdering(t *testing.T) {
	// The wire vocabulary and the monitor's are separate maps by design (one is the
	// contract's, one is the health_status column's), which means they can drift.
	// This is what stops that: unreachable must outrank unknown in both, and
	// not_checked must sit above healthy in both.
	pairs := map[string]string{
		"Healthy":     statusHealthy,
		"NotChecked":  statusNotChecked,
		"Degraded":    statusDegraded,
		"Unknown":     statusUnknown,
		"Unreachable": statusUnreachable,
	}
	expected := map[string]int{
		statusHealthy: 0, statusNotChecked: 1, statusDegraded: 2,
		statusUnknown: 3, statusUnreachable: 4,
	}
	for monitorStatus, wire := range pairs {
		if got := wireStatus(monitorStatus); got != wire {
			t.Errorf("wireStatus(%q) = %q, want %q", monitorStatus, got, wire)
		}
		if severityRank[wire] != expected[wire] {
			t.Errorf("severityRank[%q] = %d, want %d", wire, severityRank[wire], expected[wire])
		}
	}
	if severityRank[statusUnreachable] <= severityRank[statusUnknown] {
		t.Error("unreachable must outrank unknown: a proven failure is worse news than an open question")
	}
	if severityRank[statusNotChecked] <= severityRank[statusHealthy] {
		t.Error("not_checked must never be mistaken for healthy")
	}
}

func TestAnUnrecognisedStatusBecomesUnknownRatherThanHealthy(t *testing.T) {
	// A status this client cannot name is not evidence of health. Defaulting the
	// other way is the same class of bug as the vault-sync default that reported
	// "synced" as healthy.
	for _, status := range []string{"", "synced", "Weird", "healthyish"} {
		if got := wireStatus(status); got == statusHealthy {
			t.Errorf("wireStatus(%q) = healthy; an unrecognised status must not claim health", status)
		}
	}
}

func TestAnEmptyCheckSetAggregatesToNotChecked(t *testing.T) {
	// The identity element for a "worst wins" reduction is the healthiest value, so
	// a naive fold reports a cluster with zero probes as Healthy. That is the
	// aggregate form of the same defect.
	if got := aggregate(nil); got != statusNotChecked {
		t.Errorf("aggregate(nil) = %q, want not_checked", got)
	}
	if got := aggregate([]Check{}); got != statusNotChecked {
		t.Errorf("aggregate(empty) = %q, want not_checked", got)
	}
}

func TestAggregateReportsTheWorstDimension(t *testing.T) {
	checks := []Check{
		{Name: "a", Status: "Healthy", ObservedAt: time.Now()},
		{Name: "b", Status: "Unreachable", ObservedAt: time.Now()},
		{Name: "c", Status: "Unknown", ObservedAt: time.Now()},
	}
	if got := aggregate(checks); got != statusUnreachable {
		t.Errorf("aggregate = %q, want unreachable", got)
	}
}

func TestANotCheckedDimensionDoesNotMakeTheWholeObservationUnhealthy(t *testing.T) {
	// NotChecked must not be mistaken for Healthy, but it must also not mask a real
	// Degraded finding from another dimension by outranking it.
	checks := []Check{
		{Name: "a", Status: "NotChecked", Reason: "not configured"},
		{Name: "b", Status: "Degraded", ObservedAt: time.Now()},
	}
	if got := aggregate(checks); got != statusDegraded {
		t.Errorf("aggregate = %q, want degraded", got)
	}
}

func TestAHealthyDimensionsMessageIsNotSentAsAnError(t *testing.T) {
	// The monitor's healthy messages are descriptions ("Heartbeat received 1m0s
	// ago"), and the contract refuses a positive claim paired with an error. Sending
	// them as `error` would make every healthy dimension unrepresentable.
	if got := errorMessageFor("Healthy", "Heartbeat received 1m0s ago"); got != "" {
		t.Errorf("healthy message became an error: %q", got)
	}
	if got := errorMessageFor("NotChecked", "Vault sync status not reported"); got != "" {
		t.Errorf("not_checked message became an error: %q", got)
	}
	if got := errorMessageFor("Unreachable", "connection refused"); got != "connection refused" {
		t.Errorf("a real failure lost its error text: %q", got)
	}
}

func TestUpdateClusterHealthSendsANotCheckedDimensionAsNotChecked(t *testing.T) {
	// End to end for acceptance 4 across the seam: what the monitor computed as
	// NotChecked must arrive as not_checked with its reason, not as a health claim.
	s := newStub(t)
	client := newTestClient(t, s)
	client.knownWorkspaces["c1"] = "ws-1"

	details := []byte(`{
		"overall_status": "NotChecked",
		"checked_at": "2026-09-16T12:00:00Z",
		"dimensions": [
			{"name": "eks_reachability", "status": "NotChecked", "message": "EKS probing not configured"},
			{"name": "heartbeat_freshness", "status": "Healthy", "message": "Heartbeat received 1m0s ago"}
		]
	}`)
	if err := client.UpdateClusterHealth(context.Background(), "c1", "NotChecked", details); err != nil {
		t.Fatalf("UpdateClusterHealth: %v", err)
	}

	var payload struct {
		Status string `json:"status"`
		Checks []struct {
			Name       string  `json:"name"`
			Status     string  `json:"status"`
			Reason     *string `json:"reason"`
			ObservedAt *string `json:"observed_at"`
		} `json:"checks"`
	}
	if err := json.Unmarshal(s.last().body, &payload); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if payload.Status != statusNotChecked {
		t.Errorf("aggregate status = %q, want not_checked", payload.Status)
	}
	for _, check := range payload.Checks {
		if check.Name != "eks_reachability" {
			continue
		}
		if check.Status != statusNotChecked {
			t.Errorf("unconfigured probe reported %q", check.Status)
		}
		if check.Reason == nil || *check.Reason == "" {
			t.Error("not_checked arrived without a reason")
		}
		if check.ObservedAt != nil {
			t.Error("not_checked arrived with an observation time")
		}
	}
}

func TestANotCheckedDimensionWithNoMessageStillGetsAReason(t *testing.T) {
	// The contract requires a reason and the monitor does not always set a message,
	// so the client supplies a truthful default rather than failing the whole cycle
	// or dropping the dimension (which would look like "not reported" instead of
	// "not checked").
	s := newStub(t)
	client := newTestClient(t, s)
	client.knownWorkspaces["c1"] = "ws-1"

	details := []byte(`{"checked_at": "2026-09-16T12:00:00Z",
	                    "dimensions": [{"name": "cost_anomaly", "status": "NotChecked"}]}`)
	if err := client.UpdateClusterHealth(context.Background(), "c1", "NotChecked", details); err != nil {
		t.Fatalf("UpdateClusterHealth: %v", err)
	}

	if !strings.Contains(string(s.last().body), `"reason"`) {
		t.Errorf("no reason was supplied: %s", s.last().body)
	}
}

// --- Querier behaviour over the API -----------------------------------------

func healthDetails() []byte {
	return []byte(`{
		"overall_status": "Healthy",
		"checked_at": "2026-09-16T12:00:00Z",
		"dimensions": [{"name": "heartbeat_freshness", "status": "Healthy", "message": "fresh"}]
	}`)
}

func TestListActiveClustersMapsTheScopedProjection(t *testing.T) {
	s := newStub(t)
	heartbeat := "2026-09-16T11:59:00Z"
	s.response = fmt.Sprintf(`[{"cluster_id": "c1", "workspace": "ws-1", "org_id": "o1",
	  "name": "alpha", "status": "Active", "health_status": "Degraded",
	  "last_heartbeat": %q, "last_reconciled_at": "2026-09-16T12:00:30Z",
	  "actual_state_json": {"skypilot_healthy": true}}]`, heartbeat)
	client := newTestClient(t, s)

	clusters, err := client.ListActiveClusters(context.Background())
	if err != nil {
		t.Fatalf("ListActiveClusters: %v", err)
	}

	if len(clusters) != 1 {
		t.Fatalf("got %d clusters, want 1", len(clusters))
	}
	got := clusters[0]
	if got.ID != "c1" || got.OrgID != "o1" || got.Name != "alpha" || got.Status != "Active" {
		t.Errorf("unexpected mapping: %+v", got)
	}
	if got.HealthStatus == nil || *got.HealthStatus != "Degraded" {
		t.Errorf("health status not mapped: %+v", got.HealthStatus)
	}
	if got.LastHeartbeat == nil {
		t.Error("last heartbeat not mapped")
	}
	// Mapped separately from LastHeartbeat: the freshness check judges the
	// controller's heartbeat, while this records the monitor's own last cycle.
	if got.LastReconciledAt == nil {
		t.Error("last reconciled timestamp not mapped")
	}
	if !strings.Contains(string(got.ActualStateJSON), "skypilot_healthy") {
		t.Errorf("heartbeat payload not mapped: %s", got.ActualStateJSON)
	}
	if got.WorkspaceID == nil || *got.WorkspaceID != "ws-1" {
		t.Errorf("workspace not mapped: %v", got.WorkspaceID)
	}
}

func TestListActiveClustersRequestsTheStatusesTheMonitorCaresAbout(t *testing.T) {
	s := newStub(t)
	s.response = `[]`
	client := newTestClient(t, s)

	if _, err := client.ListActiveClusters(context.Background()); err != nil {
		t.Fatalf("ListActiveClusters: %v", err)
	}

	for _, want := range []string{"Active", "Provisioning", "Pending"} {
		if !strings.Contains(s.last().query, want) {
			t.Errorf("status %q missing from query %q", want, s.last().query)
		}
	}
}

func TestAcquireLockReportsContentionAsFalseRatherThanAnError(t *testing.T) {
	// Losing a race for a lock is the normal outcome of contention, and the
	// monitors already treat false as "skip this cycle". Reporting it as an error
	// would log an error every cycle for every cluster the sibling replica owns.
	s := newStub(t)
	s.status = http.StatusConflict
	client := newTestClient(t, s)

	acquired, err := client.AcquireLock(
		context.Background(), "cluster_health", "c1", "monitor-1", time.Minute,
	)

	if err != nil {
		t.Fatalf("contention should not be an error: %v", err)
	}
	if acquired {
		t.Error("expected acquired=false when the scope is held")
	}
}

func TestAcquireLockRetainsTheReceiverAssignedFenceToken(t *testing.T) {
	// The token is the receiver's; the client stores it so release can present it.
	// Inventing or incrementing one here would defeat the fence.
	s := newStub(t)
	s.response = `{"scope": "cluster_health/c1", "fence_token": 42,
	               "expires_at": "2026-09-16T12:15:00Z"}`
	client := newTestClient(t, s)

	if _, err := client.AcquireLock(
		context.Background(), "cluster_health", "c1", "monitor-1", time.Minute,
	); err != nil {
		t.Fatalf("AcquireLock: %v", err)
	}
	if err := client.ReleaseLock(context.Background(), "cluster_health", "c1", "monitor-1"); err != nil {
		t.Fatalf("ReleaseLock: %v", err)
	}

	var released struct {
		FenceToken int64 `json:"fence_token"`
	}
	if err := json.Unmarshal(s.last().body, &released); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if released.FenceToken != 42 {
		t.Errorf("released with token %d, want the granted 42", released.FenceToken)
	}
}

func TestReleasingALeaseNeverHeldIsNotAnError(t *testing.T) {
	// The monitors release from a deferred call that also runs when acquisition was
	// skipped. Treating that as an error would emit a warning every cycle for every
	// cluster another replica owns.
	s := newStub(t)
	client := newTestClient(t, s)

	err := client.ReleaseLock(context.Background(), "cluster_health", "never-acquired", "monitor-1")

	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}
	if len(s.seen) != 0 {
		t.Errorf("expected no request for a lease we do not hold, got %d", len(s.seen))
	}
}

func TestALeaseCanBeReacquiredAfterRelease(t *testing.T) {
	// The held-token map must be cleared on release, or the next acquire's token
	// would be shadowed by the stale one.
	s := newStub(t)
	s.response = `{"fence_token": 7}`
	client := newTestClient(t, s)
	ctx := context.Background()

	if _, err := client.AcquireLock(ctx, "cluster_health", "c1", "m", time.Minute); err != nil {
		t.Fatalf("first acquire: %v", err)
	}
	if err := client.ReleaseLock(ctx, "cluster_health", "c1", "m"); err != nil {
		t.Fatalf("release: %v", err)
	}
	s.response = `{"fence_token": 8}`
	if _, err := client.AcquireLock(ctx, "cluster_health", "c1", "m", time.Minute); err != nil {
		t.Fatalf("second acquire: %v", err)
	}
	if err := client.ReleaseLock(ctx, "cluster_health", "c1", "m"); err != nil {
		t.Fatalf("second release: %v", err)
	}

	var released struct {
		FenceToken int64 `json:"fence_token"`
	}
	if err := json.Unmarshal(s.last().body, &released); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if released.FenceToken != 8 {
		t.Errorf("released with token %d, want the newest grant 8", released.FenceToken)
	}
}

func TestALeaseScopeNeedNotBeACluster(t *testing.T) {
	// The budget monitor holds ("budget_monitor", "global"), for which no cluster
	// row exists.
	s := newStub(t)
	s.response = `{"fence_token": 1}`
	client := newTestClient(t, s)

	acquired, err := client.AcquireLock(
		context.Background(), "budget_monitor", "global", "monitor-1", time.Minute,
	)

	if err != nil || !acquired {
		t.Fatalf("acquired=%v err=%v", acquired, err)
	}
}

func TestGetCostHistoryReadsTheScopedRoute(t *testing.T) {
	s := newStub(t)
	s.response = `{"cluster_id": "c1", "costs": [2.5, 2.4]}`
	client := newTestClient(t, s)

	costs, err := client.GetCostHistory(context.Background(), "c1", 24*time.Hour)
	if err != nil {
		t.Fatalf("GetCostHistory: %v", err)
	}

	if len(costs) != 2 || costs[0] != 2.5 {
		t.Errorf("costs = %v", costs)
	}
	if !strings.Contains(s.last().query, "window_seconds=86400") {
		t.Errorf("window not sent: %q", s.last().query)
	}
	if !strings.Contains(s.last().path, "/cost-history") {
		t.Errorf("unexpected path %q", s.last().path)
	}
}

func TestInsertEventForwardsTheDetailsItWasGiven(t *testing.T) {
	s := newStub(t)
	client := newTestClient(t, s)

	err := client.InsertEvent(
		context.Background(), "o1", "c1", "budget.cost_anomaly", "spike",
		[]byte(`{"cost_hourly": 30.0}`),
	)
	if err != nil {
		t.Fatalf("InsertEvent: %v", err)
	}

	var payload struct {
		EventType string         `json:"event_type"`
		Message   string         `json:"message"`
		Details   map[string]any `json:"details"`
	}
	if err := json.Unmarshal(s.last().body, &payload); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if payload.EventType != "budget.cost_anomaly" || payload.Message != "spike" {
		t.Errorf("unexpected event: %+v", payload)
	}
	if payload.Details["cost_hourly"] != 30.0 {
		t.Errorf("details not forwarded: %v", payload.Details)
	}
}

func TestPingExercisesAuthenticationRatherThanMereReachability(t *testing.T) {
	// After the grant withdrawal, "can I still monitor?" means "is my credential
	// still accepted and scoped". A liveness probe that answered healthy without
	// authenticating would hide exactly the failure the cutover can introduce.
	s := newStub(t)
	s.response = `[]`
	client := newTestClient(t, s)

	if err := client.Ping(context.Background()); err != nil {
		t.Fatalf("Ping: %v", err)
	}
	if s.last().auth != testCredential {
		t.Error("Ping did not authenticate")
	}

	s.status = http.StatusUnauthorized
	if err := client.Ping(context.Background()); err == nil {
		t.Error("Ping reported healthy while the credential was rejected")
	}
}

func TestAServerErrorIsReportedWithItsStatus(t *testing.T) {
	s := newStub(t)
	s.status = http.StatusInternalServerError
	client := newTestClient(t, s)

	_, err := client.SubmitObservation(
		context.Background(), "c1", "ws-1", []Check{healthyCheck()}, time.Now().UTC(),
	)

	if err == nil {
		t.Fatal("expected an error")
	}
	if errors.Is(err, ErrReplayed) || errors.Is(err, ErrUnauthorized) {
		t.Error("a 500 must not be classified as a replay or an authorization failure")
	}
	if !strings.Contains(err.Error(), "500") {
		t.Errorf("status missing from error: %v", err)
	}
}

func TestAMalformedResponseIsAnErrorNotAnEmptyResult(t *testing.T) {
	// An empty cluster list and an unparseable one must not look the same: the
	// former means "nothing to check", the latter means the monitor is blind.
	s := newStub(t)
	s.response = `{"not": "an array"}`
	client := newTestClient(t, s)

	clusters, err := client.ListActiveClusters(context.Background())

	if err == nil {
		t.Fatal("expected a decode error")
	}
	if clusters != nil {
		t.Errorf("expected no clusters alongside the error, got %v", clusters)
	}
}

func TestTheContextIsHonoured(t *testing.T) {
	// The runner cancels on SIGTERM; an in-flight call must not outlive it.
	s := newStub(t)
	client := newTestClient(t, s)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	if err := client.Ping(ctx); err == nil {
		t.Error("expected a cancelled context to fail the call")
	}
}

func TestClientSatisfiesQuerier(t *testing.T) {
	// The seam the monitors are written against. Compile-time already, but stated
	// so the reason is recorded: the seven signatures were kept identical on
	// purpose, so cluster_health.go and budget.go compile unchanged and the diff
	// shows a transport substitution rather than an interface change.
	var _ Querier = (*Client)(nil)
}
