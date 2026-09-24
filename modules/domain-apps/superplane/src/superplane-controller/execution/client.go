// Package execution consumes harness_jobs.execution_rpc. The trusted service
// retains the admitted plan, database, provider credentials and recovery owner.
// This client can request a step ID; it cannot choose or report a provider effect.
package execution

import (
	"bufio"
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const maxMessageBytes = 65536

var (
	ErrUnavailable = errors.New("execution transport unavailable; outcome requires trusted reconciliation")
	ErrRefused     = errors.New("execution authority refused")
	ErrProtocol    = errors.New("execution contract mismatch")
	ErrCancelled   = errors.New("execution cancelled; cleanup belongs to trusted recovery")
)

// Binding comes from the trusted run assignment, never from a Kubernetes label,
// operation-derived attempt ID, or controller-created lease. Every field must
// match the authenticated server's current lease before requesting an effect.
type Binding struct {
	OperationID string `json:"operation_id"`
	OrgID       string `json:"org_id"`
	WorkspaceID string `json:"workspace_id"`
	Holder      string `json:"holder"`
	AttemptID   string `json:"attempt_id"`
	FenceToken  int64  `json:"fence_token"`
}

func (b Binding) valid() bool {
	for _, v := range []string{b.OperationID, b.OrgID, b.WorkspaceID, b.Holder, b.AttemptID} {
		if strings.TrimSpace(v) == "" || len(v) > 2048 || strings.ContainsRune(v, '\x00') {
			return false
		}
	}
	return b.FenceToken > 0
}

type Lease struct {
	Binding
	ExpiresAt       time.Time `json:"expires_at"`
	RuntimeDeadline time.Time `json:"runtime_deadline"`
	Attempts        int       `json:"attempts"`
	MaxAttempts     int       `json:"max_attempts"`
}

type Status struct {
	OperationID     string `json:"operation_id"`
	State           string `json:"state"`
	CancelRequested bool   `json:"cancel_requested"`
	CleanupRequired bool   `json:"cleanup_required"`
}

// Call is the trusted service's durable observation, not a worker report. On a
// successful replay its attempt/fence may refer to an earlier, completed step.
type Call struct {
	Binding
	JobID          string  `json:"job_id"`
	IdempotencyKey string  `json:"idempotency_key"`
	Provider       string  `json:"provider"`
	OperationKind  string  `json:"operation_kind"`
	Target         string  `json:"target"`
	Stage          string  `json:"stage"`
	Outcome        *string `json:"outcome"`
	ProviderRef    *string `json:"provider_ref"`
}

type Result struct {
	Call Call
	// Preserve the service's exact disposition. The owning domain applies this
	// to its ledger; this worker never invents a release on timeout or restart.
	Disposition string
}

type cancellationResponse struct{ result Result }

func (*cancellationResponse) Error() string { return ErrCancelled.Error() }
func (*cancellationResponse) Unwrap() error { return ErrCancelled }

// Client holds only socket location, a revocable token-file location and expected
// identity. No generic RPC method, provider hook or database handle is exposed.
type Client struct {
	socketPath  string
	tokenFile   string
	binding     Binding
	pinnedToken []byte
}

func New(socketPath, tokenFile string, binding Binding) (*Client, error) {
	if !filepath.IsAbs(socketPath) || !filepath.IsAbs(tokenFile) || !binding.valid() {
		return nil, ErrRefused
	}
	return &Client{socketPath: socketPath, tokenFile: tokenFile, binding: binding}, nil
}

func (c *Client) credential() ([]byte, error) {
	// Opening each time permits rotation/removal to revoke the next request.
	f, err := os.Open(c.tokenFile)
	if err != nil {
		return nil, ErrRefused
	}
	token, err := io.ReadAll(io.LimitReader(f, 8194))
	_ = f.Close()
	if err != nil || len(token) > 8193 {
		return nil, ErrRefused
	}
	token = bytes.TrimSpace(token)
	if len(token) == 0 || len(token) > 8192 {
		return nil, ErrRefused
	}
	if c.pinnedToken != nil && subtle.ConstantTimeCompare(token, c.pinnedToken) != 1 {
		return nil, ErrRefused
	}
	return token, nil
}

// Refuse token replacement between binding verification and the effect request.
// Otherwise a rotated token could authorize a different operation with the same
// step ID. The file and server revocation are still checked on every request.
func (c *Client) pinCredential() (*Client, error) {
	token, err := c.credential()
	if err != nil {
		return nil, err
	}
	copy := *c
	copy.pinnedToken = token
	return &copy, nil
}

func (c *Client) request(ctx context.Context, method string, arguments any, result any) error {
	token, err := c.credential()
	if err != nil {
		return err
	}
	payload, err := json.Marshal(struct {
		Token     string `json:"token"`
		Method    string `json:"method"`
		Arguments any    `json:"arguments"`
	}{string(token), method, arguments})
	if err != nil || len(payload)+1 > maxMessageBytes {
		return ErrProtocol
	}
	// A request is finite even if a caller forgot its deadline. There is no
	// retry: a lost reply may follow a durably committed provider intent.
	ctx, cancel := context.WithTimeout(ctx, 15*time.Minute)
	defer cancel()
	conn, err := (&net.Dialer{}).DialContext(ctx, "unix", c.socketPath)
	if err != nil {
		return ErrUnavailable
	}
	defer conn.Close()
	deadline, _ := ctx.Deadline()
	if err := conn.SetDeadline(deadline); err != nil {
		return ErrUnavailable
	}
	stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stop()
	if _, err = io.Copy(conn, bytes.NewReader(append(payload, '\n'))); err != nil {
		return ErrUnavailable
	}
	line, err := bufio.NewReaderSize(io.LimitReader(conn, maxMessageBytes+1), maxMessageBytes+1).ReadSlice('\n')
	if err != nil {
		return ErrUnavailable
	}
	if len(line) > maxMessageBytes {
		return ErrProtocol
	}
	var response struct {
		OK          *bool           `json:"ok"`
		Result      json.RawMessage `json:"result"`
		Error       string          `json:"error"`
		Call        json.RawMessage `json:"call"`
		Disposition string          `json:"disposition"`
	}
	if json.Unmarshal(line, &response) != nil || response.OK == nil {
		return ErrProtocol
	}
	if !*response.OK {
		switch response.Error {
		case "refused":
			return ErrRefused
		case "cancellation_pending":
			var call Call
			if json.Unmarshal(response.Call, &call) != nil {
				return ErrProtocol
			}
			result := Result{Call: call, Disposition: response.Disposition}
			if !c.validResult(result) {
				return ErrProtocol
			}
			return &cancellationResponse{result: result}
		default:
			return ErrUnavailable
		}
	}
	if len(response.Result) == 0 || response.Error != "" || json.Unmarshal(response.Result, result) != nil {
		return ErrProtocol
	}
	return nil
}

// Renew authenticates and verifies the complete current binding. Runtime limits
// remain the server's persisted approval ceiling; renewal cannot extend it.
func (c *Client) Renew(ctx context.Context) (Lease, error) {
	return c.renew(ctx, 60)
}

func (c *Client) renew(ctx context.Context, seconds int) (Lease, error) {
	var lease Lease
	if err := c.request(ctx, "renew", map[string]int{"duration_seconds": seconds}, &lease); err != nil {
		return Lease{}, err
	}
	if lease.Binding != c.binding || !lease.ExpiresAt.After(time.Now()) || !lease.RuntimeDeadline.After(time.Now()) || lease.Attempts < 1 || lease.MaxAttempts < lease.Attempts {
		return Lease{}, ErrRefused
	}
	return lease, nil
}

func (c *Client) Status(ctx context.Context) (*Status, error) {
	var status *Status
	if err := c.request(ctx, "status", struct{}{}, &status); err != nil {
		return nil, err
	}
	if status == nil || status.OperationID != c.binding.OperationID {
		return nil, ErrRefused
	}
	switch status.State {
	case "pending", "running", "succeeded", "failed", "cancelled", "unknown":
	default:
		return nil, ErrProtocol
	}
	return status, nil
}

func (c *Client) Cancel(ctx context.Context) error {
	c, err := c.pinCredential()
	if err != nil {
		return err
	}
	if _, err := c.Renew(ctx); err != nil {
		return err
	}
	var accepted bool
	if err := c.request(ctx, "cancel", struct{}{}, &accepted); err != nil {
		return err
	}
	if !accepted {
		return ErrRefused
	}
	return nil
}

// ExecuteStep requests only a step already present in the immutable admitted plan.
// It never resubmits after a transport failure, releases a lease, reports success,
// substitutes a target, or acquires another attempt. Restart/recovery decisions
// and sealing deliberately retired allocations belong to the shared service.
func (c *Client) ExecuteStep(ctx context.Context, stepID string) (Result, error) {
	c, err := c.pinCredential()
	if err != nil {
		return Result{}, err
	}
	if strings.TrimSpace(stepID) == "" || len(stepID) > 2048 || strings.ContainsRune(stepID, '\x00') {
		return Result{}, ErrRefused
	}
	// The shared protocol bounds a call to 900 seconds. Request that finite
	// lease once; the persisted runtime deadline remains the tighter limit.
	lease, err := c.renew(ctx, 900)
	if err != nil {
		return Result{}, err
	}
	status, err := c.Status(ctx)
	if err != nil {
		return Result{}, err
	}
	if status.CancelRequested || status.CleanupRequired {
		return Result{}, ErrCancelled
	}
	if status.State != "running" && status.State != "pending" {
		return Result{}, ErrRefused
	}
	deadline := lease.ExpiresAt
	if lease.RuntimeDeadline.Before(deadline) {
		deadline = lease.RuntimeDeadline
	}
	ctx, cancel := context.WithDeadline(ctx, deadline)
	defer cancel()
	var pair []json.RawMessage
	if err := c.request(ctx, "execute_step", map[string]string{"step_id": stepID}, &pair); err != nil {
		var cancelled *cancellationResponse
		if errors.As(err, &cancelled) {
			return cancelled.result, err
		}
		return Result{}, err
	}
	var result Result
	if len(pair) != 2 || json.Unmarshal(pair[0], &result.Call) != nil || json.Unmarshal(pair[1], &result.Disposition) != nil {
		return Result{}, ErrProtocol
	}
	if !c.validResult(result) {
		return Result{}, ErrProtocol
	}
	return result, nil
}

func (c *Client) validResult(result Result) bool {
	call := result.Call
	if call.OperationID != c.binding.OperationID || call.OrgID != c.binding.OrgID || call.WorkspaceID != c.binding.WorkspaceID || call.AttemptID == "" || call.FenceToken < 1 || call.JobID == "" || call.IdempotencyKey == "" || call.Provider == "" || call.OperationKind == "" || call.Target == "" {
		return false
	}
	// The real server preserves an uncertain call as intended with a null
	// outcome, so recovery can inspect the existing handle without a new call.
	if call.FenceToken > c.binding.FenceToken || (call.FenceToken == c.binding.FenceToken && call.AttemptID != c.binding.AttemptID) {
		return false
	}
	if call.FenceToken < c.binding.FenceToken && (call.Outcome == nil || *call.Outcome != "succeeded") {
		return false
	}
	if call.Stage == "intended" && call.Outcome == nil && result.Disposition == "retain" {
		return true
	}
	if call.Stage != "observed" && call.Stage != "reconciled" && call.Stage != "unresolved" {
		return false
	}
	if call.Outcome == nil || (call.Stage == "unresolved" && *call.Outcome != "unknown") {
		return false
	}
	switch *call.Outcome {
	case "succeeded":
		return result.Disposition == "settle"
	case "failed", "absent":
		return result.Disposition == "release"
	case "unknown":
		return result.Disposition == "retain"
	default:
		return false
	}
}
